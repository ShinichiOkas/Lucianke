"""``Indexer`` の PageRank の再計算（監視・編集による 1 ファイルの更新の後）。

``index_file`` / ``remove`` は直接は再計算せず、``engine.rank_scheduler`` のデバウンスで最後の更新の後に 1 回だけ計算し直す。
待ちは ``GatedSleep``（テストが解くまで返らない）に差し替えるので、固定の ``sleep`` には頼らない。
結果は「同じ最終状態を ``scan`` で全部取り込んだ DB」と一致する。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

WS_ID = "ranked"
LIB = "lib.py"
BASE_FILES = {
    LIB: "def helper():\n    return 1\n\n\ndef other():\n    return 2\n",
    "a.py": "from lib import helper\n\n\ndef use_a():\n    return helper()\n",
}


def _user(i: int) -> str:
    return f"from lib import other\n\n\ndef use_b{i}():\n    return other()\n"


class GatedSleep:
    """``asyncio.sleep`` の代わり。``release()`` が呼ばれるまで返らない。"""

    def __init__(self) -> None:
        self._gates: list[asyncio.Event] = []

    async def __call__(self, _delay: float) -> None:
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()

    def release(self) -> None:
        for gate in self._gates:
            gate.set()
        self._gates.clear()


@dataclass
class Env:
    root: Path
    db: Path
    indexer: Indexer
    sleep: GatedSleep
    refreshes: list[int] = field(default_factory=list)  # refresh_ranks を呼んだ回数を 1 件ずつ数える


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _snapshot(db: Path) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    conn = sqlite3.connect(db)
    try:
        ranks = conn.execute("SELECT urn, score, fan_in FROM _symbol_rank ORDER BY urn").fetchall()
        files = conn.execute("SELECT path, pagerank_score FROM files ORDER BY path").fetchall()
    finally:
        conn.close()
    return ranks, files


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = (tmp_path / WS_ID).resolve()
    _write(root, BASE_FILES)
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    sleep = GatedSleep()
    ws = Workspace(workspace_id=WS_ID, root=root)
    indexer = Indexer(ws, writer, readers, TreeCache(), rank_sleep=sleep)
    e = Env(root, db, indexer, sleep)
    original = indexer.refresh_ranks

    async def counting() -> int:
        e.refreshes.append(1)
        return await original()

    indexer.refresh_ranks = counting  # type: ignore[method-assign]
    report = await indexer.scan()
    assert not report.errors, report.errors
    e.refreshes.clear()  # scan 自身の末尾の計算は数えない
    try:
        yield e
    finally:
        await indexer.aclose()
        readers.close()
        writer.close()


async def _settle() -> None:
    """待ちに入るべきタスクを走らせる（時間は経たない）。"""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_scan_alone_does_not_schedule_a_second_refresh(env: Env) -> None:
    assert not env.indexer.rank_scheduler.pending
    assert env.refreshes == []
    ranks, files = _snapshot(env.db)
    assert ranks and any(score for _, score in files)  # 辺があり、scan が書いている


async def test_ten_index_file_calls_refresh_ranks_once_and_match_a_full_scan(
    env: Env, tmp_path: Path
) -> None:
    before_ranks, _ = _snapshot(env.db)
    for i in range(10):
        _write(env.root, {f"b{i}.py": _user(i)})
        stats = await env.indexer.index_file(f"b{i}.py")
        assert stats is not None
    await _settle()
    assert env.indexer.rank_scheduler.pending
    assert env.refreshes == []  # 待っているあいだは計算しない
    assert _snapshot(env.db)[0] == before_ranks  # 順位は scan 直後のまま

    env.sleep.release()
    await env.indexer.rank_scheduler.idle()
    assert env.refreshes == [1]  # 連続 10 回で 1 回
    assert env.indexer.rank_scheduler.runs == 1

    after_ranks, after_files = _snapshot(env.db)
    assert after_ranks != before_ranks  # 新しい参照が順位に入った

    # 同じ最終状態を scan で全部取り込んだ DB と一致する
    db2 = tmp_path / "db2" / "paad.sqlite"
    writer2 = IndexWriter(db2)
    readers2 = ReaderPool(db2, size=2)
    try:
        full = Indexer(Workspace(workspace_id=WS_ID, root=env.root), writer2, readers2, TreeCache())
        report = await full.scan()
        assert not report.errors, report.errors
        await full.aclose()
    finally:
        readers2.close()
        writer2.close()
    full_ranks, full_files = _snapshot(db2)
    assert after_ranks == full_ranks
    assert after_files == full_files


async def test_data_version_is_not_bumped_by_the_refresh(env: Env) -> None:
    _write(env.root, {"b0.py": _user(0)})
    await env.indexer.index_file("b0.py")
    await _settle()

    def version() -> int:
        conn = sqlite3.connect(env.db)
        try:
            return int(conn.execute("SELECT data_version FROM _meta").fetchone()[0])
        finally:
            conn.close()

    v = version()
    env.sleep.release()
    await env.indexer.rank_scheduler.idle()
    assert env.refreshes == [1]
    assert version() == v


async def test_remove_schedules_a_refresh(env: Env) -> None:
    _write(env.root, {"b0.py": _user(0)})
    await env.indexer.index_file("b0.py")
    await _settle()
    env.sleep.release()
    await env.indexer.rank_scheduler.idle()
    assert env.refreshes == [1]
    with_b0, _ = _snapshot(env.db)

    assert await env.indexer.remove("b0.py") is True
    await _settle()
    assert env.indexer.rank_scheduler.pending
    env.sleep.release()
    await env.indexer.rank_scheduler.idle()
    assert env.refreshes == [1, 1]
    assert _snapshot(env.db)[0] != with_b0

    # 元から無いファイルを消しても何も予約しない
    assert await env.indexer.remove("nope.py") is False
    await _settle()
    assert not env.indexer.rank_scheduler.pending


async def test_unchanged_file_does_not_schedule(env: Env) -> None:
    assert await env.indexer.index_file(LIB) is None  # sha256 が同じ
    await _settle()
    assert not env.indexer.rank_scheduler.pending
    assert env.refreshes == []


async def test_aclose_cancels_the_pending_refresh(env: Env) -> None:
    _write(env.root, {"b0.py": _user(0)})
    await env.indexer.index_file("b0.py")
    await _settle()
    assert env.indexer.rank_scheduler.pending
    await env.indexer.aclose()
    assert not env.indexer.rank_scheduler.pending
    env.sleep.release()
    await _settle()
    assert env.refreshes == []
