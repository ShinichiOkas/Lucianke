"""走査の解析の子プロセス（BUG-0127。engine/analysis_pool.py、engine/indexer.py）。

* 子プロセスで解析した走査の索引は、同じプロセスで解析した走査の索引と行ごとに同じ（``file_id`` の振り方も）。
* 子プロセスの数の決め方（``LUCIANKE_INDEX_WORKERS``、論理 CPU の半分、上限）。
* 子が落ちたら（``BrokenProcessPool``）、そのファイルは同じプロセスで解析し直し、以降は子に渡さない。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.model import RelPath
from lucianke.engine.analysis_pool import MAX_WORKERS, WORKERS_ENV, AnalysisPool, default_workers
from lucianke.engine.indexer import Indexer, _Loaded
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS_ID = "poly"
TABLES = {
    "files": "path, language, tier, line_count, byte_size, sha256, is_test, confidence_level, file_id",
    "symbols": "urn, file_id, is_external, parent_urn, name, qualified_name, kind, start_line, end_line",
    "relations": "source_urn, target_urn, relation_kind, source_file_id, target_file_id, call_site_line,"
    " call_site_col, use_kind, confidence_level",
    "_name_calls": "file_id, source_urn, name, call_site_line, call_site_col, use_kind",
    "_member_refs": "file_id, source_urn, class_urn, name, relation_kind, call_site_line, resolved_urn",
    "_import_probes": "file_id, stem",
    "_reexports": "file_id, name, target_urn, kind, ord",
    "_attr_defs": "file_id, class_urn, name, line, col",
    "_var_types": "file_id, var_urn, type_urn",
    "diagnostics": "file_id, severity, source, start_line, start_col, message",
}


async def _scan_rows(
    tmp_path: Path, name: str, **kwargs: Any
) -> tuple[dict[str, list[tuple[Any, ...]]], int]:
    root = tmp_path / name / WS_ID
    shutil.copytree(POLY, root)
    db = tmp_path / name / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(
        Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache(), **kwargs
    )
    try:
        report = await indexer.scan()
        assert not report.errors, report.errors
        rows: dict[str, list[tuple[Any, ...]]] = {}
        with readers.reader() as conn:
            for table, cols in TABLES.items():
                rows[table] = sorted(
                    tuple(r)
                    for r in conn.execute(f"SELECT {cols} FROM {table}").fetchall()  # noqa: S608
                )
        return rows, indexer.pooled_files
    finally:
        await indexer.aclose()
        readers.close()
        writer.close()


async def test_pooled_scan_matches_in_process_scan(tmp_path: Path) -> None:
    pooled, pooled_files = await _scan_rows(tmp_path, "pooled", analysis_workers=2, pool_min_files=3)
    local, local_files = await _scan_rows(tmp_path, "local", analysis_workers=0)

    assert pooled_files > 0  # 子プロセスが解析した
    assert local_files == 0
    for table in TABLES:
        assert pooled[table] == local[table], table
    assert pooled["relations"]  # 空どうしの一致ではない


async def test_small_scan_does_not_start_the_pool(tmp_path: Path) -> None:
    _, pooled_files = await _scan_rows(tmp_path, "small", analysis_workers=2, pool_min_files=10_000)
    assert pooled_files == 0


def test_default_workers() -> None:
    assert default_workers({WORKERS_ENV: "0"}, cpu_count=16) == 0
    assert default_workers({WORKERS_ENV: "3"}, cpu_count=16) == 3
    assert default_workers({WORKERS_ENV: " 5 "}, cpu_count=2) == 5  # 明示はそのまま
    assert default_workers({WORKERS_ENV: "-1"}, cpu_count=16) == 0
    assert default_workers({WORKERS_ENV: "many"}, cpu_count=8) == 4  # 読めなければ既定
    assert default_workers({}, cpu_count=2) == 0
    assert default_workers({}, cpu_count=4) == 2
    assert default_workers({}, cpu_count=14) == 7
    assert default_workers({}, cpu_count=64) == MAX_WORKERS
    assert default_workers({}, cpu_count=0) == 0


async def test_pool_breaks_when_a_worker_dies() -> None:
    pool = AnalysisPool(1, os._exit, (3,))  # 子は起動の途中で終わる
    try:
        future = pool.submit(len, "abc")
        assert future is not None
        with pytest.raises(BrokenProcessPool):
            await asyncio.wait_for(future, 120)
        pool.failed(BrokenProcessPool("x"))
        assert pool.broken
        assert pool.submit(len, "abc") is None  # 以降は渡さない（呼び手が同じプロセスで解析する）
    finally:
        await pool.aclose()


async def test_broken_worker_falls_back_to_in_process_analysis(tmp_path: Path) -> None:
    root = tmp_path / WS_ID
    shutil.copytree(POLY, root)
    indexer = Indexer.for_analysis(Workspace(workspace_id=WS_ID, root=root.resolve()))
    rel = RelPath("src/shop/services/payment.py")
    data = (root / rel).read_bytes()
    loaded = _Loaded(data, "0" * 64, len(data), -1, data[:8192])
    remote: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    remote.set_exception(BrokenProcessPool("worker died"))
    pool = AnalysisPool(1, os._exit, (3,))

    prepared = await indexer._finish_remote(rel, loaded, remote, pool)

    assert pool.broken
    assert prepared.degraded is None
    assert any(s.qualified_name == "PaymentProcessor" for s in prepared.facts.symbols)
