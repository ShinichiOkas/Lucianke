"""走査の後段の整えが遅くても、書き手・読み手・進みが待たされない（BUG-0112）。

* 整え（``_settle_relations``）は ``SETTLE_BATCH_FILES`` ずつの短いジョブに割る。わざと遅くした整えの途中でも、別の書き込みの
  ジョブ（先取り・編集）と読み取りは 1 バッチ分の遅れで答え、``building()`` とジョブの状態が段（``settling relations k/n``）を出す。
* 割っても、整えた結果は割らないときと同じ（再び公開した名前・継承したメンバーのフィクスチャで比べる）。
* 前景（ツールの呼び出し。``FOREGROUND.active()``）の間は、走査は次のファイルの解析を始めない。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

import lucianke.engine.indexer as indexer_mod
from lucianke.core.foreground import FOREGROUND
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SLOW_SETTLE_S = 0.25  # わざと遅くした整えの 1 バッチ
WAIT_S = 60.0


class Env:
    def __init__(self, root: Path, db: Path, ws_id: str) -> None:
        self.root = root
        self.writer = IndexWriter(db)
        self.readers = ReaderPool(db, size=2)
        self.indexer = Indexer(Workspace(workspace_id=ws_id, root=root.resolve()), self.writer, self.readers, TreeCache())

    async def close(self) -> None:
        await self.indexer.aclose()
        self.readers.close()
        self.writer.close()

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        with self.readers.reader() as conn:
            q = {
                "relations": "SELECT r.source_urn, r.target_urn, r.relation_kind, r.call_site_line, r.call_site_col,"
                " r.use_kind, r.confidence_level, (SELECT path FROM files WHERE file_id = r.target_file_id) FROM relations r",
                "member_refs": "SELECT source_urn, class_urn, name, call_site_line, call_site_col, resolved_urn FROM _member_refs",
                "name_calls": "SELECT source_urn, name, call_site_line, call_site_col, use_kind FROM _name_calls",
                "var_types": "SELECT var_urn, type_urn FROM _var_types",
                "reexport_deps": "SELECT f.path, d.path, d.name FROM _reexport_deps d JOIN files f ON f.file_id = d.file_id",
            }
            return {k: sorted(tuple(r) for r in conn.execute(sql).fetchall()) for k, sql in q.items()}


def _copy(fixture: str, tmp_path: Path, name: str) -> tuple[Path, Path]:
    root = tmp_path / name / fixture
    shutil.copytree(FIXTURES / fixture, root)
    return root, tmp_path / name / "db" / "paad.sqlite"


async def _scan(e: Env) -> None:
    status = await e.indexer.wait_job(e.indexer.start_scan_job())
    assert status is not None and status.state == "done", status


async def _until(what: str, check: Any) -> Any:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        got = check()
        if got:
            return got
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.parametrize("fixture", ["inherited_calls", "reexports", "poly"])
async def test_settling_in_small_batches_gives_the_same_index(
    fixture: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_id = fixture.replace("_", "-")
    whole = Env(*_copy(fixture, tmp_path, "whole"), ws_id)
    try:
        await _scan(whole)
        expected = whole.snapshot()
    finally:
        await whole.close()
    monkeypatch.setattr(indexer_mod, "SETTLE_BATCH_FILES", 1)  # 1 ファイルずつのジョブ（最も細かい割り方）
    split = Env(*_copy(fixture, tmp_path, "split"), ws_id)
    try:
        await _scan(split)
        assert split.snapshot() == expected
    finally:
        await split.close()


async def test_slow_settle_does_not_block_writes_reads_or_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indexer_mod, "SETTLE_BATCH_FILES", 2)
    real = indexer_mod._settle_files

    def slow(conn: object, file_ids: list[int]) -> None:
        time.sleep(SLOW_SETTLE_S)  # 整えが遅いワークスペースの代わり（書き手スレッドの中で眠る）
        real(conn, file_ids)

    monkeypatch.setattr(indexer_mod, "_settle_files", slow)
    e = Env(*_copy("poly", tmp_path, "slow"), "poly")
    try:
        job_id = e.indexer.start_scan_job()
        progress = await _until(
            "the settle phase",
            lambda: (p := e.indexer.building()) is not None and (p.phase or "").startswith("settling relations") and p,
        )
        assert progress.files_done == progress.files_total
        status = e.indexer.job_status(job_id)
        assert status is not None and status.state == "running" and (status.phase or "").startswith("settling relations")
        assert 95 <= status.progress < 100

        # 遅い整えの途中の書き込み（先取り・編集の代わり）と読み取りは、1 バッチ分の遅れで答える
        t0 = time.perf_counter()
        files = await e.writer.asubmit(
            lambda conn: conn.execute("SELECT count(*) FROM files").fetchone()[0], bumps_version=False, label="probe"
        )
        write_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        with e.readers.reader() as conn:
            relations = conn.execute("SELECT count(*) FROM relations").fetchone()[0]
        read_s = time.perf_counter() - t0
        assert files > 0 and relations > 0
        assert write_s < 4 * SLOW_SETTLE_S, write_s
        assert read_s < 1.0, read_s
        assert e.indexer.building() is not None  # まだ整えている（全バッチは 1 バッチより長い）

        final = await e.indexer.wait_job(job_id)
        assert final is not None and final.state == "done" and final.progress == 100 and final.phase is None
        assert e.indexer.building() is None
    finally:
        await e.close()


async def test_scan_yields_to_a_foreground_call(tmp_path: Path) -> None:
    e = Env(*_copy("poly", tmp_path, "fg"), "poly")
    try:
        with FOREGROUND.active():
            job_id = e.indexer.start_scan_job()
            await _until("the listing", lambda: (p := e.indexer.building()) is not None and p.files_total)
            await asyncio.sleep(0.5)  # 1 回に譲る上限（MAX_YIELD_S = 2 秒）より短く見る
            progress = e.indexer.building()
            assert progress is not None and progress.files_done <= 1  # 前景の間は次のファイルの解析を始めない
        final = await e.indexer.wait_job(job_id)  # 前景が終われば走査は進んで終わる
        assert final is not None and final.state == "done"
    finally:
        await e.close()
