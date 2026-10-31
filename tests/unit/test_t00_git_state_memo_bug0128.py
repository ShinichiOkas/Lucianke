"""T-00 ``status`` の Git の状態の覚え（BUG-0128）。

``dulwich`` の ``status`` は作業ツリーを歩き、設定・``.gitignore``・索引・パックを読む（F-PY で 1 回 20〜30 ms、CPU が混んでいると
130〜350 ms）。前に調べてからルートの下が変わっていなければ（監視のイベントの数・``data_version``・``.git/HEAD``・``.git/index``
が同じで、``GIT_STATE_MAX_AGE_S`` 以内）覚えから返す。監視が動いていない・``.git`` がディレクトリでないときは毎回調べる。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from dulwich import porcelain
from dulwich.repo import Repo

from lucianke.core.model import RelPath
from lucianke.tools import t00_configure_workspace as t00
from lucianke.workspace.types import Workspace
from lucianke.workspace.watcher import Watcher

WAIT_S = 60.0


def make_ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    Repo.init(str(root)).close()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    return Workspace(workspace_id="w", root=Path(os.path.realpath(root)), case_insensitive=os.name == "nt")


class Counting:
    """``_git_info`` を数える（中身は本物）。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = t00._git_info

        def counted(root: Path) -> dict[str, Any] | None:
            self.calls += 1
            return real(root)

        monkeypatch.setattr(t00, "_git_info", counted)


def fake_runtime(*, live: bool = True) -> SimpleNamespace:
    return SimpleNamespace(watcher=SimpleNamespace(live=live, generation=0))


def test_an_unchanged_root_reuses_the_git_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = make_ws(tmp_path)
    count = Counting(monkeypatch)
    memos: dict[str, t00._GitMemo] = {}
    rt = fake_runtime()
    first = t00._git_state(memos, rt, ws, 7)
    assert first is not None and first["dirty_files"] == 1  # 未追跡の a.py
    for _ in range(5):
        assert t00._git_state(memos, rt, ws, 7) == first
    assert count.calls == 1


def test_an_event_a_new_data_version_or_a_git_file_change_looks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_ws(tmp_path)
    count = Counting(monkeypatch)
    memos: dict[str, t00._GitMemo] = {}
    rt = fake_runtime()
    t00._git_state(memos, rt, ws, 1)
    rt.watcher.generation += 1  # 監視がイベントを届けた
    t00._git_state(memos, rt, ws, 1)
    assert count.calls == 2
    t00._git_state(memos, rt, ws, 2)  # このサーバーの編集が索引の版を進めた
    assert count.calls == 3
    (ws.root / "b.py").write_text("y = 2\n", encoding="utf-8")
    porcelain.add(str(ws.root), paths=[str(ws.root / "b.py")])  # イベントが届く前のステージ（.git/index が変わる）
    after = t00._git_state(memos, rt, ws, 2)
    assert count.calls == 4 and after is not None and after["dirty_files"] == 2
    rt2 = fake_runtime()  # 束を作り直した（別の監視）: 前の覚えは使わない
    t00._git_state(memos, rt2, ws, 2)
    assert count.calls == 5


def test_without_a_live_watcher_or_a_git_directory_it_always_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_ws(tmp_path)
    count = Counting(monkeypatch)
    memos: dict[str, t00._GitMemo] = {}
    for rt in (fake_runtime(live=False), SimpleNamespace(), None):
        t00._git_state(memos, rt, ws, 1)
        t00._git_state(memos, rt, ws, 1)
    assert count.calls == 6
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {ws.root / '.git'}\n", encoding="utf-8")  # リンクされた作業ツリーの形
    ws2 = Workspace(workspace_id="l", root=Path(os.path.realpath(linked)), case_insensitive=os.name == "nt")
    rt = fake_runtime()
    t00._git_state(memos, rt, ws2, 1)
    t00._git_state(memos, rt, ws2, 1)
    assert count.calls == 8


def test_the_memo_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = make_ws(tmp_path)
    count = Counting(monkeypatch)
    monkeypatch.setattr(t00, "GIT_STATE_MAX_AGE_S", 0.0)
    memos: dict[str, t00._GitMemo] = {}
    rt = fake_runtime()
    t00._git_state(memos, rt, ws, 1)
    t00._git_state(memos, rt, ws, 1)
    assert count.calls == 2


class NullSink:
    async def changed(self, rel: RelPath) -> None:
        return None

    async def removed(self, rel: RelPath) -> None:
        return None

    async def rescan(self) -> None:
        return None

    def known_paths_under(self, prefix: str) -> list[RelPath]:
        return []


async def until(pred: Any, timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.005)


async def test_a_new_file_reported_by_the_real_watcher_changes_the_dirty_count(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    watcher = Watcher(ws, NullSink())
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop))
    memos: dict[str, t00._GitMemo] = {}
    rt = SimpleNamespace(watcher=watcher)
    try:
        await asyncio.wait_for(watcher.started.wait(), WAIT_S)
        assert watcher.live
        (ws.root / "zz_probe.txt").write_bytes(b"")  # 監視が動き出したのを確かめる（イベントが届くまで待つ）
        await until(lambda: watcher.generation > 0)
        before = t00._git_state(memos, rt, ws, 1)
        assert before is not None
        seen = watcher.generation
        (ws.root / "new.py").write_text("z = 3\n", encoding="utf-8")
        await until(lambda: watcher.generation > seen)
        after = t00._git_state(memos, rt, ws, 1)
        assert after is not None and after["dirty_files"] == before["dirty_files"] + 1
        seen = watcher.generation
        (ws.root / ".git" / "zz_lucianke_probe").write_bytes(b"")  # .git/ の中のイベントも数える（索引には渡さない）
        await until(lambda: watcher.generation > seen)
    finally:
        stop.set()
        await asyncio.wait_for(task, WAIT_S)
    assert not watcher.live  # 止まった監視では覚えを使わない
