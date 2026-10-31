"""BUG-0134: LSP が ``initialize`` を済ませた後に、参照元になりうるファイルを背景でまとめて開かせる（``tools/_lsp_preopen``）。

偽のハンドル（本物の ``DocumentSync`` に送った通知を記録する）と偽の索引で確かめる:
選ぶ規則（``preopen_set`` は ``prime_set`` がどの名前で選びうるファイルの和）・上限・まとめて送る（``preload_documents``）・
呼び出しを待たせない（合図はタスクを投げるだけ、読み込みはスレッド、前景が忙しい間は送らない）・起動し直したら何もしない・
送る時機（tsserver 系は ``indexing`` の間に、basedpyright は ``ready`` の後）・``LspPool`` の ``on_initialized`` の合図。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lucianke.core.foreground import Foreground
from lucianke.core.model import Confidence, FileInfo, RelPath, Tier
from lucianke.lsp import features
from lucianke.lsp.documents import DocumentSync
from lucianke.lsp.pool import LspHandle, LspPool, LspState
from lucianke.lsp.servers import LspServerDef
from lucianke.tools import _lsp_exact as lx
from lucianke.tools import _lsp_preopen as po
from lucianke.tools._runtime import RuntimeHub
from lucianke.workspace.types import Workspace

FILES = {
    "source/guard.ts": "export function isThing() {}",
    "source/use.ts": "import {isThing} from './guard'; isThing()",
    "test/a.test.ts": "isThing()",
    "test/b.test.tsx": "<div>{isThing()}</div>",
    "test/c.test.ts": "// nothing",
    "scripts/run.js": "other()",
    "README.md": "isThing",
}


def info(path: str, *, language: str = "typescript", tier: Tier = Tier.T1, test: bool = False) -> FileInfo:
    return FileInfo(
        file_id=1,
        workspace_id="w",
        path=RelPath(path),
        language=language,
        tier=tier,
        sha256="",
        line_count=1,
        byte_size=1,
        is_test=test,
        confidence=Confidence.STRUCTURAL,
    )


LISTING = [
    info("source/guard.ts"),
    info("source/use.ts"),
    info("test/a.test.ts", test=True),
    info("test/b.test.tsx", language="tsx", test=True),
    info("test/c.test.ts", test=True),
    info("scripts/run.js", language="javascript", tier=Tier.T2),
    info("README.md", language="markdown", tier=Tier.T3),
]
TS_ORDER = ["test/a.test.ts", "test/b.test.tsx", "test/c.test.ts", "scripts/run.js", "source/guard.ts", "source/use.ts"]


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Workspace(workspace_id="w", root=tmp_path.resolve())


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> list[FileInfo]:
    monkeypatch.setattr(lx.queries, "files_in_scope", lambda conn, ws_id, globs=(), **kw: list(LISTING))
    return LISTING


class Readers:
    """``ReaderPool`` の代わり（接続は使わない: ``files_in_scope`` を差し替えている）。"""

    @contextlib.contextmanager
    def reader(self) -> Iterator[None]:
        yield None


class Client:
    is_connected = True


def handle(
    ws: Workspace, *, max_open: int = 200, language_id: str = "typescript", state: LspState = LspState.READY
) -> SimpleNamespace:
    sent: list[tuple[str, str]] = []
    docs = DocumentSync(ws, lambda method, params: sent.append((method, params["textDocument"]["uri"])), max_open=max_open)
    return SimpleNamespace(
        ws=ws,
        state=state,
        client=Client(),
        documents=docs,
        language_id=language_id,
        role="types",
        analysis_scope=None,
        sent=sent,
    )


def opened(h: SimpleNamespace) -> list[str]:
    return [str(p) for p in h.documents.open_paths()]


# --- 選ぶ規則（preopen_set） ---------------------------------------------------------------------


def test_preopen_set_is_every_file_prime_set_could_pick_for_any_name(ws: Workspace, listing: list[FileInfo]) -> None:
    """tsserver 系は同系の Tier 1〜2 のファイルすべて（名前では絞らない。対象のファイルも入る）。テストが先、次にパス順。"""
    got = lx.preopen_set(None, ws, "typescript", is_open=lambda rel: False, open_count=0)  # type: ignore[arg-type]
    assert [str(p) for p, _ in got.files] == TS_ORDER
    assert dict((str(p), lid) for p, lid in got.files)["test/b.test.tsx"] == "typescriptreact"
    assert got.skipped == 0


def test_after_preopen_prime_set_has_nothing_left_to_open(ws: Workspace, listing: list[FileInfo]) -> None:
    """先開きの後は、どの対象の prime_set も開くファイルが無い（tsserver が呼び出しの経路で組み直さない）。"""
    got = lx.preopen_set(None, ws, "typescript", is_open=lambda rel: False, open_count=0)  # type: ignore[arg-type]
    done = {p for p, _ in got.files}
    for name, exclude in (("isThing", "source/guard.ts"), ("other", "scripts/run.js")):
        rest = lx.prime_set(None, ws, "typescript", name=name, exclude=exclude, is_open=done.__contains__)  # type: ignore[arg-type]
        assert rest.files == ()


def test_preopen_set_skips_open_files_and_other_languages(ws: Workspace, listing: list[FileInfo]) -> None:
    got = lx.preopen_set(
        None, ws, "typescript", is_open=lambda rel: str(rel) == "test/a.test.ts", open_count=1  # type: ignore[arg-type]
    )
    assert "test/a.test.ts" not in [str(p) for p, _ in got.files]
    assert lx.preopen_set(None, ws, "rust", is_open=lambda rel: False, open_count=0).files == ()  # type: ignore[arg-type]
    assert lx.preopen_set(None, ws, "python", is_open=lambda rel: False, open_count=0).files == ()  # type: ignore[arg-type]  # scope なし


def test_preopen_set_for_python_is_the_files_outside_the_analysis_scope(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python は basedpyright の解析の対象の外の Python のファイルだけ（prime_set と同じ規則）。絞っていない設定なら開かせない。"""
    files = [info("src/app.py", language="python"), info("tests/test_app.py", language="python", test=True)]
    monkeypatch.setattr(lx.queries, "files_in_scope", lambda conn, ws_id, globs=(), **kw: list(files))
    inside_src = SimpleNamespace(narrowed=True, contains=lambda path: "src" in Path(path).parts)
    got = lx.preopen_set(None, ws, "python", is_open=lambda rel: False, open_count=0, scope=inside_src)  # type: ignore[arg-type]
    assert [str(p) for p, _ in got.files] == ["tests/test_app.py"]
    wide = SimpleNamespace(narrowed=False, contains=lambda path: True)
    assert lx.preopen_set(None, ws, "python", is_open=lambda rel: False, open_count=0, scope=wide).files == ()  # type: ignore[arg-type]


def test_preopen_set_over_the_limit_keeps_room_for_one_call_and_prefers_files_with_more_relations(
    ws: Workspace, listing: list[FileInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限に収まらなければ、1 回の問い合わせの分（PRIME_MAX）の余地を残し、関係の多いファイルから選ぶ。"""
    monkeypatch.setattr(lx, "PRIME_MAX", 2)
    weights = {"source/use.ts": 9, "scripts/run.js": 5, "test/c.test.ts": 5}
    monkeypatch.setattr(lx.queries, "relation_counts_by_source", lambda conn, ws_id: dict(weights))
    # 6 件の候補 + 開いている 1 件 > 上限 6: 6 - 2 - 1 = 3 件を選ぶ
    got = lx.preopen_set(None, ws, "typescript", is_open=lambda rel: False, open_count=1, max_open=6)  # type: ignore[arg-type]
    assert [str(p) for p, _ in got.files] == ["source/use.ts", "test/c.test.ts", "scripts/run.js"]  # 同数はテストが先
    assert got.skipped == 3
    # 収まるなら全部（関係の数は見ない）
    got = lx.preopen_set(None, ws, "typescript", is_open=lambda rel: False, open_count=1, max_open=7)  # type: ignore[arg-type]
    assert [str(p) for p, _ in got.files] == TS_ORDER


# --- まとめて送る（preload_documents） ----------------------------------------------------------


def test_preload_opens_everything_in_one_go_without_evicting_and_skips_open_files(ws: Workspace) -> None:
    h = handle(ws, max_open=4)
    h.documents.did_open(RelPath("source/use.ts"), "x", "typescript")  # 前景が先に開いた
    docs = features.read_documents(ws, [(RelPath(p), "typescript") for p in TS_ORDER])
    assert len(docs) == len(TS_ORDER)
    n = features.preload_documents(h, docs, h.client)  # type: ignore[arg-type]
    assert n == 3  # 上限 4 - 開いていた 1
    assert opened(h) == ["source/use.ts", *TS_ORDER[:3]]  # 前景の文書を追い出さない
    assert [m for m, _ in h.sent] == ["textDocument/didOpen"] * 4


def test_preload_does_nothing_after_a_restart_or_when_stopped(ws: Workspace) -> None:
    docs = features.read_documents(ws, [(RelPath("test/a.test.ts"), "typescript")])
    h = handle(ws)
    assert features.preload_documents(h, docs, Client()) == 0  # type: ignore[arg-type]  # クライアントが変わった
    for state in (LspState.STARTING, LspState.STANDBY, LspState.UNAVAILABLE):
        h.state = state
        assert features.preload_documents(h, docs, h.client) == 0  # type: ignore[arg-type]
    assert h.sent == []
    h.state = LspState.INDEXING  # initialize の後なら文書を受け付ける
    assert features.preload_documents(h, docs, h.client) == 1  # type: ignore[arg-type]


def test_read_documents_skips_unreadable_files(ws: Workspace) -> None:
    got = features.read_documents(ws, [(RelPath("nope.ts"), "typescript"), (RelPath("test/a.test.ts"), "typescript")])
    assert [(str(p), text) for p, _, text in got] == [("test/a.test.ts", "isThing()")]


# --- 背景の先開き（preopen） -----------------------------------------------------------------------


async def test_preopen_opens_the_whole_set_once_right_after_initialize_for_tsserver(
    ws: Workspace, listing: list[FileInfo]
) -> None:
    """tsserver 系は ``indexing`` の間に送る（プロジェクトの読み込みと検査器の作成が ready の前に済み、ready は本物の合図で来る）。"""
    h = handle(ws, state=LspState.INDEXING)
    report = await po.preopen(h, Readers())  # type: ignore[arg-type]
    assert report.opened == len(TS_ORDER) and report.reason is None
    assert sorted(opened(h)) == sorted(TS_ORDER)
    again = await po.preopen(h, Readers())  # type: ignore[arg-type]
    assert again.opened == 0 and again.reason == "nothing_to_open"


async def test_preopen_reads_off_the_event_loop(
    ws: Workspace, listing: list[FileInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """本文の読み込みはスレッドで: 読んでいる間もイベントループは前景の仕事を進める（ループの上で読むと、ここで待ち合わせが詰まる）。"""
    loop_ran = threading.Event()
    real = features.read_documents
    threads: list[bool] = []

    def slow_read(w: Workspace, files: Any) -> Any:
        threads.append(threading.current_thread() is threading.main_thread())
        assert loop_ran.wait(5.0), "the event loop was blocked while reading"
        return real(w, files)

    monkeypatch.setattr(po.features, "read_documents", slow_read)
    h = handle(ws)
    task = asyncio.create_task(po.preopen(h, Readers()))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    while not threads:
        await asyncio.sleep(0.005)
    loop_ran.set()  # 前景の仕事（ループの上）が、読み込みの途中に走った
    report = await asyncio.wait_for(task, 10)
    assert threads == [False]
    assert report.opened == len(TS_ORDER)


async def test_preopen_waits_while_a_call_is_in_flight_and_does_not_reopen_what_the_call_opened(
    ws: Workspace, listing: list[FileInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """前景の呼び出しの最中は送らない。呼び出しが自分で開いた文書（prime_set）は二重に開かない。"""
    fg = Foreground(window_s=60.0, max_yield_s=30.0)
    read_done = asyncio.Event()
    real = features.read_documents
    loop = asyncio.get_running_loop()

    def read(w: Workspace, files: Any) -> Any:
        out = real(w, files)
        loop.call_soon_threadsafe(read_done.set)
        return out

    monkeypatch.setattr(po.features, "read_documents", read)
    h = handle(ws)
    with fg.active():  # T-06 の呼び出しの最中
        task = asyncio.create_task(po.preopen(h, Readers(), foreground=fg))  # type: ignore[arg-type]
        await asyncio.wait_for(read_done.wait(), 10)
        await asyncio.sleep(0.05)
        assert h.sent == []  # 呼び出しの最中は送らない
        h.documents.did_open(RelPath("test/a.test.ts"), "isThing()", "typescript")  # 呼び出しが自分で開かせた
    report = await asyncio.wait_for(task, 10)
    assert report.opened == len(TS_ORDER) - 1
    uris = [uri for _, uri in h.sent]
    assert len(uris) == len(set(uris)) == len(TS_ORDER)  # どのファイルも 1 回だけ


async def test_preopen_during_the_scan_picks_from_the_listing_without_waiting(
    ws: Workspace, listing: list[FileInfo]
) -> None:
    """走査の途中でも、列挙があれば待たずに列挙から選ぶ（tsserver の読み込みと検査を早く始める）。列挙もまだなら列挙を待つ。"""
    listed: list[RelPath] | None = None
    h = handle(ws, state=LspState.INDEXING)
    task = asyncio.create_task(
        po.preopen(h, Readers(), building=lambda: object(), listing=lambda: listed, poll_s=0.01)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)
    assert h.sent == [] and not task.done()  # 列挙し終えるまで待つ
    listed = [RelPath(p) for p in FILES]  # 走査は続いている（building が真のまま）
    report = await asyncio.wait_for(task, 10)
    assert report.opened == len(TS_ORDER) and opened(h) == TS_ORDER


def test_preopen_set_from_the_listing_is_the_same_as_from_the_index(ws: Workspace, listing: list[FileInfo]) -> None:
    """列挙からの判定（拡張子・名前による言語とティア、is_test）は索引の記録と同じ集合・順を選ぶ。"""
    from_index = lx.preopen_set(None, ws, "typescript", is_open=lambda rel: False, open_count=0)  # type: ignore[arg-type]
    from_listing = lx.preopen_set(
        None, ws, "typescript", is_open=lambda rel: False, open_count=0, listing=[RelPath(p) for p in FILES]  # type: ignore[arg-type]
    )
    assert from_listing == from_index


async def test_preopen_after_the_scan_uses_the_index(ws: Workspace, listing: list[FileInfo]) -> None:
    scanning = [True]
    planned: list[object] = []
    real = lx.preopen_set

    def spy(*a: Any, **kw: Any) -> Any:
        planned.append(kw.get("listing"))
        return real(*a, **kw)

    h = handle(ws)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(po.lx, "preopen_set", spy)
        task = asyncio.create_task(
            po.preopen(h, Readers(), building=lambda: object() if scanning[0] else None, poll_s=0.01)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.05)
        assert planned == [] and not task.done()  # 走査の途中で列挙も無い: 待つ
        scanning[0] = False
        report = await asyncio.wait_for(task, 10)
    assert planned == [None] and report.opened == len(TS_ORDER)  # 走査が済んだら索引から


async def test_preopen_for_python_waits_until_ready(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """basedpyright は ready を待ってから送る（起動直後の解析の合図を変えない）。"""
    files = [info("src/app.py", language="python"), info("tests/test_app.py", language="python", test=True)]
    monkeypatch.setattr(lx.queries, "files_in_scope", lambda conn, ws_id, globs=(), **kw: list(files))
    (ws.root / "tests").mkdir(exist_ok=True)
    (ws.root / "tests" / "test_app.py").write_text("import app\n", encoding="utf-8")
    h = handle(ws, language_id="python", state=LspState.INDEXING)
    h.analysis_scope = SimpleNamespace(narrowed=True, contains=lambda path: "src" in Path(path).parts)
    task = asyncio.create_task(po.preopen(h, Readers(), poll_s=0.01))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert h.sent == [] and not task.done()
    h.state = LspState.READY
    report = await asyncio.wait_for(task, 10)
    assert report.opened == 1 and opened(h) == ["tests/test_app.py"]


async def test_preopen_gives_up_when_the_lsp_restarts(
    ws: Workspace, listing: list[FileInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = handle(ws)
    real = features.read_documents

    def read_then_restart(w: Workspace, files: Any) -> Any:
        h.client = Client()  # 読んでいる間に起動し直した（次の ready でまた先開きが走る）
        return real(w, files)

    monkeypatch.setattr(po.features, "read_documents", read_then_restart)
    report = await po.preopen(h, Readers())  # type: ignore[arg-type]
    assert report.reason == "restarted" and h.sent == []


# --- 合図（LspPool.on_initialized）と結線（RuntimeHub.on_lsp_initialized） ------------------------------


FAKE = LspServerDef(language_id="fake", name="fakels", argv_candidates=(("fakels",),))


async def test_pool_calls_on_initialized_once_per_launch_including_after_a_restart(ws: Workspace) -> None:
    seen: list[LspHandle] = []
    pool = LspPool(None, servers=(FAKE,), on_initialized=seen.append)  # type: ignore[arg-type]
    h = pool._handle("fake", ws, "types")
    h._set_state(LspState.STARTING)
    assert seen == []
    h._set_state(LspState.INDEXING)  # initialize の応答を受けた
    assert seen == [h]
    h._become_ready()
    assert seen == [h]  # ready では呼ばない
    h._set_state(LspState.STARTING, "restarting")
    h._set_state(LspState.INDEXING)
    assert seen == [h, h]


async def test_pool_survives_a_failing_on_initialized(ws: Workspace) -> None:
    def boom(_h: LspHandle) -> None:
        raise RuntimeError("boom")

    pool = LspPool(None, servers=(FAKE,), on_initialized=boom)  # type: ignore[arg-type]
    h = pool._handle("fake", ws, "types")
    h._set_state(LspState.INDEXING)
    h._become_ready()
    assert h.state is LspState.READY


async def test_hub_schedules_the_preopen_without_waiting_and_only_for_type_servers(
    ws: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    calls: list[Any] = []

    async def fake_preopen(h: Any, readers: Any, **kw: Any) -> po.PreopenReport:
        calls.append(h)
        await gate.wait()
        return po.PreopenReport()

    monkeypatch.setattr("lucianke.tools._runtime.preopen", fake_preopen)
    hub = RuntimeHub(tmp_path / "cache", trees=None)  # type: ignore[arg-type]
    hub._runtimes["w"] = SimpleNamespace(readers=Readers(), indexer=SimpleNamespace(building=lambda: None))  # type: ignore[assignment]
    ts = handle(ws)
    hub.on_lsp_initialized(ts)  # type: ignore[arg-type]  # 同期で戻る（待たない）
    hub.on_lsp_initialized(SimpleNamespace(ws=ws, role="lint", language_id="python"))  # type: ignore[arg-type]
    hub.on_lsp_initialized(SimpleNamespace(ws=ws, role="types", language_id="rust"))  # type: ignore[arg-type]
    other = Workspace(workspace_id="other", root=tmp_path)
    hub.on_lsp_initialized(SimpleNamespace(ws=other, role="types", language_id="typescript"))  # type: ignore[arg-type]
    assert len(hub._preopens) == 1
    await asyncio.sleep(0)
    assert calls == [ts]
    await hub.cancel_preopens()  # 終了の段（stop_watchers）で止める
    assert not hub._preopens
