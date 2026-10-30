"""T-03 ``search_workspace_symbols``: 同順位（一致の質もコード/データの別も同じ）の並びは、ファイルの PageRank の高い順。

``FileInfo.pagerank``（``files.pagerank_score``）が ``index/queries.py`` から載るようになったことの契約。小さなワークスペースを
実 ``Indexer`` で取り込み、同じ名前のシンボルを 2 つのファイルに置いて、``pagerank_score`` を直接書き換えて並びを見る。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t03_search_workspace_symbols as t03
from lucianke.tools._services import ToolServices
from lucianke.workspace.types import Workspace

WS_ID = "ranked"
A_PY = "alpha/mod.py"
Z_PY = "zeta/mod.py"
IS_WINDOWS = os.name == "nt"


@dataclass(slots=True)
class _Svc(ToolServices):
    """``ToolServices`` に ``reader_pool`` を足したもの（合成ルートが同じ形で組む想定）。"""

    reader_pool: ReaderPool | None = None


async def _build(tmp_path: Path) -> tuple[Path, Path]:
    root = Path(os.path.realpath(tmp_path / WS_ID))
    for rel in (A_PY, Z_PY):
        (root / rel).parent.mkdir(parents=True)
        (root / rel).write_text("def helper():\n    return 1\n", encoding="utf-8")
    db = tmp_path / "db" / "paad.sqlite"

    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    try:
        ws = Workspace(workspace_id=WS_ID, root=root, case_insensitive=IS_WINDOWS)
        indexer = Indexer(ws, writer, readers, TreeCache())
        report = await indexer.scan()
        assert not report.errors, report.errors
        await indexer.aclose()
    finally:
        readers.close()
        writer.close()
    return root, db


def _set_scores(db: Path, scores: dict[str, float | None]) -> None:
    conn = sqlite3.connect(db)
    try:
        for path, score in scores.items():
            conn.execute("UPDATE files SET pagerank_score = ? WHERE path = ?", (score, path))
        conn.commit()
    finally:
        conn.close()


async def _paths(root: Path, db: Path) -> list[str]:
    svc = _Svc(roots=lambda: [str(root)], trees=TreeCache(), lines=compact)
    pool = ReaderPool(db)
    svc.reader_pool = pool
    registry = ToolRegistry()
    registry.register(t03.TOOL_SPEC)
    try:
        r = await Pipeline(registry, services=svc).call_tool(
            "search_workspace_symbols", {"query": "helper", "match": "exact", "format": "json"}
        )
    finally:
        pool.close()
    assert not r.is_error, r.text
    return [s["path"] for s in r.structured["data"]["symbols"]]


async def test_t03_same_tier_hits_come_in_descending_file_pagerank(tmp_path: Path) -> None:
    root, db = await _build(tmp_path)

    # スコアが無い（None）か同じなら、これまでどおりパスの辞書順
    assert await _paths(root, db) == [A_PY, Z_PY]
    _set_scores(db, {A_PY: 0.5, Z_PY: 0.5})
    assert await _paths(root, db) == [A_PY, Z_PY]

    # 辞書順で後ろのファイルの PageRank が高ければ、先に来る
    _set_scores(db, {A_PY: 0.1, Z_PY: 0.9})
    assert await _paths(root, db) == [Z_PY, A_PY]

    # 逆にすれば逆になる（None は 0 として最後）
    _set_scores(db, {A_PY: 0.9, Z_PY: None})
    assert await _paths(root, db) == [A_PY, Z_PY]
    _set_scores(db, {A_PY: None, Z_PY: 0.01})
    assert await _paths(root, db) == [Z_PY, A_PY]


async def test_file_info_carries_pagerank_from_the_index(tmp_path: Path) -> None:
    from lucianke.index import queries

    _, db = await _build(tmp_path)
    _set_scores(db, {A_PY: 0.25, Z_PY: None})
    pool = ReaderPool(db, size=1)
    try:
        with pool.reader() as conn:
            a = queries.file_by_path(conn, WS_ID, A_PY)
            z = queries.file_by_path(conn, WS_ID, Z_PY)
    finally:
        pool.close()
    assert a is not None and a.pagerank == pytest.approx(0.25)
    assert z is not None and z.pagerank is None
