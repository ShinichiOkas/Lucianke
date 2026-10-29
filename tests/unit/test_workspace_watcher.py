"""workspace.watcher（設計書 §7.3.6、スパイク S-06）のテスト。

実際の ``watchfiles`` を ``tmp_path`` の中で動かし、取り込み側は **偽の sink**（呼び出しを記録し、索引に載っているパスを
持つ）に差し替える。時間に依存する待ちはすべて「条件が満たされるまでのポーリング（上限つき）」にする（固定 sleep は使わない）。
「届かない」ことの確かめは、後から書いた見張りのファイルが届くのを待ってから行う（イベントは書いた順に届くため）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

import lucianke.workspace.watcher as watcher_mod
from lucianke.core.limits import INDEX_REFLECT_MS, WATCH_BURST_FILES
from lucianke.core.model import RelPath
from lucianke.workspace.types import Workspace
from lucianke.workspace.watcher import Watcher

WAIT_S = 5.0  # 条件待ちの上限（失敗したときだけ使い切る）


class FakeSink:
    """呼び出しを記録する sink。``known`` は索引に載っているパスで、changed / removed に追従する。"""

    def __init__(self, known: set[str] | None = None) -> None:
        self.known: set[str] = set(known or ())
        self.calls: list[tuple[str, str | None, float]] = []
        self.fail_on: set[str] = set()
        self.gate: asyncio.Event | None = None  # 立つまで changed を保留する
        self.root: Path | None = (
            None  # 設定すると rescan が「ディスク上の全ファイルを索引に載せる」動きをする
        )
        self.started: list[str] = []
        self.finished: list[str] = []

    def of(self, kind: str) -> list[str]:
        return [rel for k, rel, _ in self.calls if k == kind and rel is not None]

    def time_of(self, kind: str, rel: str) -> float:
        return next(t for k, r, t in self.calls if k == kind and r == rel)

    async def changed(self, rel: RelPath) -> None:
        self.started.append(rel)
        if self.gate is not None:
            await self.gate.wait()
        if rel in self.fail_on:
            raise RuntimeError(f"boom {rel}")
        self.known.add(rel)
        self.calls.append(("changed", rel, time.perf_counter()))
        self.finished.append(rel)

    async def removed(self, rel: RelPath) -> None:
        self.known.discard(rel)
        self.calls.append(("removed", rel, time.perf_counter()))

    async def rescan(self) -> None:
        self.calls.append(("rescan", None, time.perf_counter()))
        if self.root is not None:
            self.known = {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()}

    def known_paths_under(self, prefix: str) -> list[RelPath]:
        return [RelPath(p) for p in sorted(self.known) if p.startswith(prefix)]


async def until(pred: Callable[[], bool], timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.005)


def make_ws(tmp_path: Path, *, exclude: tuple[str, ...] = ()) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(
        workspace_id="t",
        root=Path(os.path.realpath(root)),
        exclude_globs=exclude,
        case_insensitive=os.name == "nt",
    )


def put(root: Path, rel: str, data: bytes = b"x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextlib.asynccontextmanager
async def running(ws: Workspace, sink: FakeSink, **kw: int) -> AsyncIterator[Watcher]:
    w = Watcher(ws, sink, **kw)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.wait_for(w.started.wait(), WAIT_S)
    try:
        yield w
    finally:
        stop.set()
        await asyncio.wait_for(task, WAIT_S)


async def barrier(ws: Workspace, sink: FakeSink, name: str = "zz_barrier.py") -> None:
    """見張りのファイルを書いて届くのを待つ。それより前に書いたもののイベントは、届くなら届き終わっている。"""
    put(ws.root, name, os.urandom(8))
    await until(lambda: name in sink.of("changed"))


# --- 基本の反映 ------------------------------------------------------------------------------


async def test_single_write_changed_once_within_budget(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    put(ws.root, "a.py")
    sink = FakeSink({"a.py"})
    async with running(ws, sink) as w:
        t0 = time.perf_counter()
        put(ws.root, "a.py", b"y = 2\n")
        await until(lambda: "a.py" in sink.of("changed"))
        elapsed_ms = (sink.time_of("changed", "a.py") - t0) * 1000
        assert elapsed_ms < INDEX_REFLECT_MS
        await barrier(ws, sink)
        assert sink.of("changed").count("a.py") == 1
        assert sink.of("removed") == []
        assert w.mode == "native"


async def test_atomic_replace_is_a_change_not_a_removal(tmp_path: Path) -> None:
    """既存ファイルの os.replace は deleted も届く（S-06）。種別は捨てて stat で判定するので removed にならない。"""
    ws = make_ws(tmp_path)
    put(ws.root, "a.py", b"old")
    sink = FakeSink({"a.py"})
    async with running(ws, sink):
        tmp = put(ws.root, "a.py.new", b"new")
        os.replace(tmp, ws.root / "a.py")
        await until(lambda: "a.py" in sink.of("changed"))
        await barrier(ws, sink)
        assert "a.py" not in sink.of("removed")


async def test_delete_is_removed(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    put(ws.root, "a.py")
    sink = FakeSink({"a.py"})
    async with running(ws, sink):
        (ws.root / "a.py").unlink()
        await until(lambda: "a.py" in sink.of("removed"))
        await barrier(ws, sink)
        assert sink.of("removed") == ["a.py"]
        assert "a.py" not in sink.of("changed")


async def test_file_rename_removes_old_and_changes_new(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    put(ws.root, "old.py")
    sink = FakeSink({"old.py"})
    async with running(ws, sink):
        os.rename(ws.root / "old.py", ws.root / "new.py")
        await until(lambda: "old.py" in sink.of("removed") and "new.py" in sink.of("changed"))
        assert sink.known == {"new.py"}


# --- ディレクトリ（S-06: 移動・改名は子のイベントが来ない） ----------------------------------------


async def test_directory_rename_removes_old_children_and_changes_new(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    for rel in ("pkg/a.py", "pkg/b.py", "pkg/sub/c.py"):
        put(ws.root, rel)
    sink = FakeSink({"pkg/a.py", "pkg/b.py", "pkg/sub/c.py"})
    async with running(ws, sink):
        os.rename(ws.root / "pkg", ws.root / "lib")
        await until(lambda: sink.known == {"lib/a.py", "lib/b.py", "lib/sub/c.py"})
        await barrier(ws, sink)
        assert {"pkg/a.py", "pkg/b.py", "pkg/sub/c.py"} <= set(sink.of("removed"))
        assert {"lib/a.py", "lib/b.py", "lib/sub/c.py"} <= set(sink.of("changed"))
        assert not any(p.startswith("pkg/") for p in sink.of("changed"))


async def test_directory_moved_in_from_outside_is_expanded(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, exclude=("build/",))
    outside = tmp_path / "outside"
    for rel in ("proj/src/a.py", "proj/src/deep/b.py", "proj/build/gen.py", "proj/README.md"):
        put(outside, rel)
    sink = FakeSink()
    async with running(ws, sink):
        os.rename(outside / "proj", ws.root / "proj")
        await until(
            lambda: {"proj/src/a.py", "proj/src/deep/b.py", "proj/README.md"} <= set(sink.of("changed"))
        )
        await barrier(ws, sink)
        assert "proj/build/gen.py" not in sink.of("changed")  # 除外ディレクトリの配下は展開しない


async def test_rmtree_removes_all_known_children(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    for rel in ("d/a.py", "d/e/b.py"):
        put(ws.root, rel)
    sink = FakeSink({"d/a.py", "d/e/b.py"})
    async with running(ws, sink):
        shutil.rmtree(ws.root / "d")
        await until(lambda: not sink.known)
        assert {"d/a.py", "d/e/b.py"} <= set(sink.of("removed"))


async def test_editing_a_file_does_not_expand_its_directory(tmp_path: Path) -> None:
    """ファイルの書き換えには親ディレクトリの modified が付いてくる。索引に載っているディレクトリは展開しない。"""
    ws = make_ws(tmp_path)
    for rel in ("sub/a.py", "sub/b.py", "sub/c.py"):
        put(ws.root, rel)
    sink = FakeSink({"sub/a.py", "sub/b.py", "sub/c.py"})
    async with running(ws, sink):
        put(ws.root, "sub/a.py", b"changed")
        await until(lambda: "sub/a.py" in sink.of("changed"))
        await barrier(ws, sink)
        assert sorted(sink.of("changed")) == ["sub/a.py", "zz_barrier.py"]


# --- 対象外 ------------------------------------------------------------------------------------


async def test_git_tmp_and_excluded_paths_never_reach_the_sink(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, exclude=("*.log", "build/"))
    sink = FakeSink()
    async with running(ws, sink):
        put(ws.root, ".git/HEAD")
        put(ws.root, ".git/objects/ab/cd")
        put(ws.root, ".git/index.lock")
        os.replace(ws.root / ".git" / "index.lock", ws.root / ".git" / "index")
        put(ws.root, ".lucianke-tmp-123")
        put(ws.root, "src/.lucianke-tmp-456")
        put(ws.root, "x.log")
        put(ws.root, "build/y.py")
        put(ws.root, "real.py")
        await until(lambda: "real.py" in sink.of("changed"))
        await barrier(ws, sink)
        assert sorted(sink.of("changed")) == ["real.py", "zz_barrier.py"]
        assert sink.of("removed") == []


# --- 自分の書き込み（expect） ------------------------------------------------------------------


async def test_expected_self_write_is_dropped_once(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    put(ws.root, "a.py", b"v0")
    sink = FakeSink({"a.py"})
    async with running(ws, sink) as w:
        w.expect(RelPath("a.py"), sha(b"v1"))
        put(ws.root, "a.py", b"v1")  # 自分の書き込み
        await barrier(ws, sink, "b1.py")
        assert "a.py" not in sink.of("changed")
        put(ws.root, "a.py", b"v2")  # 期待は 1 回で消費済み: 次の書き込みは届く
        await until(lambda: "a.py" in sink.of("changed"))


async def test_expectation_with_different_content_is_not_dropped(tmp_path: Path) -> None:
    """期待と内容が違う（別のプロセスが先に書き換えた）なら捨てない。"""
    ws = make_ws(tmp_path)
    put(ws.root, "a.py", b"v0")
    sink = FakeSink({"a.py"})
    async with running(ws, sink) as w:
        w.expect(RelPath("a.py"), sha(b"mine"))
        put(ws.root, "a.py", b"somebody else")
        await until(lambda: "a.py" in sink.of("changed"))


async def test_expected_deletion_is_dropped(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    put(ws.root, "a.py")
    sink = FakeSink({"a.py"})
    async with running(ws, sink) as w:
        w.expect(RelPath("a.py"), "")
        (ws.root / "a.py").unlink()
        await barrier(ws, sink)
        assert sink.of("removed") == []


# --- バースト ------------------------------------------------------------------------------------


async def test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes(tmp_path: Path) -> None:
    assert WATCH_BURST_FILES < 600
    ws = make_ws(tmp_path)
    sink = FakeSink()
    async with running(ws, sink):
        for d in range(6):
            (ws.root / f"d{d}").mkdir()
        for i in range(600):
            os.close(os.open(ws.root / f"d{i % 6}" / f"f{i}.py", os.O_CREAT | os.O_WRONLY))
        await until(lambda: "rescan" in [k for k, _, _ in sink.calls])
        await barrier(ws, sink)
        assert [k for k, _, _ in sink.calls].count("rescan") == 1
        assert sink.of("changed") == ["zz_barrier.py"]  # 個別の changed は出ない（見張りだけ）
        assert sink.of("removed") == []


async def test_expansion_of_a_huge_moved_in_directory_becomes_a_rescan(tmp_path: Path) -> None:
    """1 つのイベント（ディレクトリの移動）でも、展開した件数が上限を超えたら rescan 1 回にする。"""
    ws = make_ws(tmp_path)
    outside = tmp_path / "outside"
    for i in range(WATCH_BURST_FILES + 20):
        put(outside, f"big/f{i % 5}/g{i}.py")
    sink = FakeSink()
    sink.root = (
        ws.root
    )  # 本物の rescan と同じく、終わると索引にファイルが載る（同じディレクトリの続報を再展開しない）
    async with running(ws, sink):
        os.rename(outside / "big", ws.root / "big")
        await until(lambda: "rescan" in [k for k, _, _ in sink.calls])
        await barrier(ws, sink)
        assert [k for k, _, _ in sink.calls].count("rescan") == 1
        assert sink.of("changed") == ["zz_barrier.py"]


# --- drain ---------------------------------------------------------------------------------------


async def test_drain_waits_for_the_sink_to_finish(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    sink = FakeSink()
    sink.gate = asyncio.Event()
    async with running(ws, sink) as w:
        put(ws.root, "a.py")
        await until(lambda: "a.py" in sink.started)  # 処理が始まり、gate で保留されている
        drain = asyncio.create_task(w.drain())
        for _ in range(
            20
        ):  # 保留中は drain も終わらない（この待ちは否定の確認なので、短くても揺れて落ちることはない）
            await asyncio.sleep(0.005)
        assert not drain.done()
        assert sink.finished == []
        sink.gate.set()
        await asyncio.wait_for(drain, WAIT_S)
        assert sink.finished == ["a.py"]


async def test_drain_with_paths_syncs_without_waiting_for_the_event(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    put(ws.root, "a.py", b"v0")
    put(ws.root, "gone.py")
    sink = FakeSink({"a.py", "gone.py"})
    async with running(ws, sink) as w:
        put(ws.root, "a.py", b"v1")
        (ws.root / "gone.py").unlink()
        await asyncio.wait_for(w.drain([RelPath("a.py"), RelPath("gone.py")]), WAIT_S)
        # drain が返った時点で、イベントの配信（≈ 20 ms 後）を待たずに両方とも反映されている
        assert "a.py" in sink.of("changed")
        assert "gone.py" in sink.of("removed")


async def test_drain_cuts_short_the_burst_merge_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """100 件以上の窓は静穏を待って合算するが、後ろに drain が来たらその待ちを打ち切って処理し切る。"""
    monkeypatch.setattr(watcher_mod, "WATCH_BURST_QUIET_S", 30.0)  # 打ち切れるのは drain だけ
    ws = make_ws(tmp_path)
    for i in range(150):
        put(ws.root, f"f{i}.py")
    sink = FakeSink()
    w = Watcher(ws, sink)
    q: asyncio.Queue[watcher_mod._Item] = asyncio.Queue()
    consumer = asyncio.create_task(w._consume(q, asyncio.Event()))
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    q.put_nowait({f"f{i}.py" for i in range(150)})
    q.put_nowait(watcher_mod._Flush(frozenset(), done))
    await asyncio.wait_for(done, 5.0)
    assert len(sink.of("changed")) == 150  # drain の完了より前に、窓は個別に処理されている
    assert "rescan" not in [k for k, _, _ in sink.calls]
    q.put_nowait(None)
    await asyncio.wait_for(consumer, WAIT_S)


async def test_drain_when_not_running_only_syncs_given_paths(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    put(ws.root, "a.py")
    sink = FakeSink({"gone.py"})
    w = Watcher(ws, sink)
    await w.drain()  # 何もしない
    assert sink.calls == []
    await w.drain([RelPath("a.py"), RelPath("gone.py")])
    assert sink.of("changed") == ["a.py"]
    assert sink.of("removed") == ["gone.py"]


# --- 停止・失敗・堅牢さ --------------------------------------------------------------------------


async def test_stop_ends_run_within_a_second(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    w = Watcher(ws, FakeSink())
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.wait_for(w.started.wait(), WAIT_S)
    t0 = time.perf_counter()
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert time.perf_counter() - t0 < 1.0
    assert w.mode == "native"


async def test_cancelling_run_does_not_set_the_callers_stop_event(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    w = Watcher(ws, FakeSink())
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.wait_for(w.started.wait(), WAIT_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1.0)
    assert not stop.is_set()


async def test_start_failure_falls_back_to_polling_and_does_nothing(tmp_path: Path) -> None:
    ws = Workspace(workspace_id="t", root=tmp_path / "does-not-exist")
    sink = FakeSink()
    w = Watcher(ws, sink)
    await asyncio.wait_for(w.run(asyncio.Event()), WAIT_S)  # 返る（何もしない）
    assert w.mode == "polling"
    assert w.started.is_set()
    assert sink.calls == []


async def test_polling_mode_drain_still_syncs_given_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: object, **_k: object) -> object:
        raise OSError("watch limit reached")

    monkeypatch.setattr(watcher_mod, "awatch", broken)
    ws = make_ws(tmp_path)
    put(ws.root, "a.py")
    sink = FakeSink()
    w = Watcher(ws, sink)
    await asyncio.wait_for(w.run(asyncio.Event()), WAIT_S)
    assert w.mode == "polling"
    await w.drain([RelPath("a.py")])  # 遅延検証: 呼び手の文脈で今の状態を sink に渡す
    assert sink.of("changed") == ["a.py"]


async def test_sink_failure_does_not_stop_the_watcher(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    sink = FakeSink()
    sink.fail_on = {"bad.py"}
    async with running(ws, sink):
        put(ws.root, "bad.py")
        await until(lambda: "bad.py" in sink.started)
        put(ws.root, "good.py")
        await until(lambda: "good.py" in sink.of("changed"))


async def test_run_twice_is_rejected(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    async with running(ws, FakeSink()) as w:
        with pytest.raises(RuntimeError):
            await w.run(asyncio.Event())
