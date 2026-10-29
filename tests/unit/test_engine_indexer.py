"""engine.indexer のテスト（設計書 §7.6.2、§7.6.5、仕様書 §9.4、§12.4、§13.5）。

``tests/fixtures/poly`` を ``tmp_path`` にコピーしてワークスペースにし、``tmp_path`` に実 DB を作る。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool, read_data_version
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS_ID = "poly"
PAYMENT = "src/shop/services/payment.py"
ROUTES = "src/shop/api/routes.py"


@dataclass
class Env:
    root: Path
    indexer: Indexer
    writer: IndexWriter
    readers: ReaderPool

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def version(self) -> int:
        with self.readers.reader() as conn:
            return read_data_version(conn)

    def count(self, table: str) -> int:
        return int(self.rows(f"SELECT COUNT(*) FROM {table}")[0][0])  # noqa: S608 - 固定の表名


def _make(root: Path, db: Path, **kwargs: int) -> Env:
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    ws = Workspace(workspace_id=WS_ID, root=root.resolve())
    return Env(root, Indexer(ws, writer, readers, TreeCache(), **kwargs), writer, readers)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root)
    e = _make(root, tmp_path / "db" / "paad.sqlite")
    yield e
    await e.indexer.aclose()
    e.readers.close()
    e.writer.close()


def _all_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


async def test_first_scan_indexes_everything(env: Env) -> None:
    report = await env.indexer.scan()

    expected = _all_files(env.root)
    assert not report.errors, report.errors
    assert report.files_seen == len(expected)
    assert report.indexed == len(expected)
    assert report.created == len(expected)
    paths = [r[0] for r in env.rows("SELECT path FROM files ORDER BY path")]
    assert paths == expected
    assert env.count("workspaces") == 1

    # シンボル
    qualified = {
        r[0]
        for r in env.rows(
            "SELECT qualified_name FROM symbols s JOIN files f USING(file_id) WHERE f.path = ?", PAYMENT
        )
    }
    assert "PaymentProcessor.process_charge" in qualified
    assert "PaymentProcessor" in qualified
    # body_sha256 が入る
    (sha,) = env.rows(
        "SELECT body_sha256 FROM symbols WHERE qualified_name = 'PaymentProcessor.process_charge'"
    )[0]
    assert isinstance(sha, str) and len(sha) == 64

    # Tier 4 は heuristic（ctags 有りでも無しでも）
    (conf, tier) = env.rows("SELECT confidence_level, tier FROM files WHERE path = 'legacy/calc.cob'")[0]
    assert tier == 4
    assert conf in ("heuristic", "lexical_fallback")


async def test_first_scan_relations_calls(env: Env) -> None:
    await env.indexer.scan()
    rows = env.rows(
        "SELECT source_urn, target_urn, relation_kind, confidence_level FROM relations"
        " WHERE relation_kind = 'CALLS' AND target_urn LIKE '%PaymentProcessor.process_charge%'"
    )
    sources = {r[0] for r in rows}
    assert any(s.endswith("#create_order") for s in sources), rows
    assert all(r[3] == "structural" for r in rows)
    # 目標のシンボルが symbols に実在する（宙に浮いた行が残らない）
    dangling = env.rows(
        "SELECT COUNT(*) FROM relations WHERE instr(target_urn, '#') > 0 AND target_urn NOT IN (SELECT urn FROM symbols)"
    )
    assert dangling == [(0,)]
    # target_file_id が埋まる
    (file_id,) = env.rows(
        "SELECT target_file_id FROM relations WHERE relation_kind = 'CALLS'"
        " AND target_urn LIKE '%PaymentProcessor.process_charge%' LIMIT 1"
    )[0]
    assert file_id is not None


async def test_secret_file_has_row_but_no_content(env: Env) -> None:
    (env.root / "conf" / ".env").write_text("API_KEY=hunter2\nPASSWORD=swordfish\n", encoding="utf-8")
    await env.indexer.scan()
    rows = env.rows(
        "SELECT f.language, f.line_count, f.confidence_level, COUNT(s.urn) FROM files f LEFT JOIN symbols s USING(file_id) WHERE f.path = 'conf/.env' GROUP BY f.file_id"
    )
    assert len(rows) == 1
    language, line_count, conf, n_symbols = rows[0]
    assert language == "secret"
    assert line_count == 0
    assert conf == "lexical_fallback"
    assert n_symbols == 0
    # 診断にも本文が漏れない
    assert env.rows(
        "SELECT COUNT(*) FROM diagnostics d JOIN files f USING(file_id) WHERE f.path = 'conf/.env'"
    ) == [(0,)]


async def test_oversize_file_is_files_row_only(tmp_path: Path) -> None:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root)
    big = root / "src" / "shop" / "big.py"
    big.write_text("def big_one():\n    return 1\n" * 100, encoding="utf-8")
    e = _make(root, tmp_path / "paad.sqlite", max_file_bytes=1000)
    try:
        await e.indexer.scan()
        rows = e.rows(
            "SELECT f.byte_size, f.line_count, f.confidence_level, COUNT(s.urn) FROM files f"
            " LEFT JOIN symbols s USING(file_id) WHERE f.path = 'src/shop/big.py' GROUP BY f.file_id"
        )
        size, lines, conf, n_symbols = rows[0]
        assert size == big.stat().st_size > 1000
        assert lines == 200
        assert conf == "lexical_fallback"
        assert n_symbols == 0
        # 2 回目は流し読みの sha256 が同じなので取り込まない
        again = await e.indexer.scan()
        assert again.indexed == 0
    finally:
        await e.indexer.aclose()
        e.readers.close()
        e.writer.close()


async def test_second_scan_ingests_nothing(env: Env) -> None:
    first = await env.indexer.scan()
    calls = env.indexer.ingest_calls
    version = env.version()

    second = await env.indexer.scan()

    assert second.indexed == 0
    assert second.removed == 0
    assert second.unchanged == first.files_seen
    assert env.indexer.ingest_calls == calls  # ingest_file が 1 回も呼ばれない（設計書 §7.6.5）
    assert env.version() == version


async def test_changed_file_is_the_only_one_ingested_and_bumps_version_once(env: Env) -> None:
    await env.indexer.scan()
    version = env.version()
    calls = env.indexer.ingest_calls
    target = env.root / PAYMENT
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\ndef added_helper(x):\n    return x\n", encoding="utf-8"
    )

    report = await env.indexer.scan()

    assert report.indexed == 1
    assert report.created == 0
    assert env.indexer.ingest_calls == calls + 1
    assert env.version() == version + 1
    qualified = {
        r[0] for r in env.rows("SELECT qualified_name FROM symbols WHERE qualified_name = 'added_helper'")
    }
    assert qualified == {"added_helper"}


async def test_deleted_file_disappears(env: Env) -> None:
    await env.indexer.scan()
    version = env.version()
    (env.root / PAYMENT).unlink()

    report = await env.indexer.scan()

    assert report.removed == 1
    assert report.indexed == 0
    assert env.rows("SELECT COUNT(*) FROM files WHERE path = ?", PAYMENT) == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM symbols WHERE qualified_name LIKE 'PaymentProcessor%'") == [(0,)]
    # 消えたシンボルを指していた関係も残らない
    assert env.rows("SELECT COUNT(*) FROM relations WHERE target_urn LIKE ?", f"%{PAYMENT}%") == [(0,)]
    assert env.version() == version + 1


async def test_index_file_unchanged_none_changed_stats_gone_removes(env: Env) -> None:
    await env.indexer.scan()
    assert await env.indexer.index_file(PAYMENT) is None  # sha256 が同じ
    target = env.root / PAYMENT
    target.write_text(target.read_text(encoding="utf-8") + "\nX = 1\n", encoding="utf-8")
    version = env.version()

    stats = await env.indexer.index_file(PAYMENT)

    assert stats is not None and not stats.file_created
    assert env.version() == version + 1
    target.unlink()
    assert await env.indexer.index_file(PAYMENT) is None
    assert env.rows("SELECT COUNT(*) FROM files WHERE path = ?", PAYMENT) == [(0,)]


async def test_index_file_rejects_escape(env: Env) -> None:
    from lucianke.core.errors import LuciankeError

    with pytest.raises(LuciankeError):
        await env.indexer.index_file("../outside.py")


async def test_syntax_error_becomes_tree_sitter_diagnostic(env: Env) -> None:
    (env.root / "src" / "shop" / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    await env.indexer.scan()
    rows = env.rows(
        "SELECT d.source, d.severity FROM diagnostics d JOIN files f USING(file_id) WHERE f.path = 'src/shop/broken.py'"
    )
    assert rows
    assert {r[0] for r in rows} == {"tree-sitter"}


async def test_exclude_globs_and_gitignore(tmp_path: Path) -> None:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root)
    (root / ".gitignore").write_text("reports/\n*.j2\n", encoding="utf-8")
    ws = Workspace(workspace_id=WS_ID, root=root.resolve(), exclude_globs=("docs/",))
    db = tmp_path / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(ws, writer, readers, TreeCache())
    try:
        report = await indexer.scan()
        with readers.reader() as conn:
            paths = {r[0] for r in conn.execute("SELECT path FROM files")}
        assert report.gitignore
        assert not any(p.startswith(("reports/", "docs/")) for p in paths), paths
        assert "templates/receipt.j2" not in paths
        assert ".gitignore" in paths
        assert PAYMENT in paths
        # 除外に変わったファイルは index_file が消す
        assert await indexer.index_file("reports/junit.xml") is None
    finally:
        await indexer.aclose()
        readers.close()
        writer.close()


async def test_scan_job_lifecycle(env: Env) -> None:
    job_id = env.indexer.start_scan_job()

    first = env.indexer.job_status(job_id)
    assert first is not None
    assert first.state in ("queued", "running")
    assert first.kind == "reindex"
    assert env.indexer.job_status("job_9999") is None

    seen: set[str] = {first.state}
    while True:
        status = env.indexer.job_status(job_id)
        assert status is not None
        seen.add(status.state)
        if status.state in ("done", "failed"):
            break
        await asyncio.sleep(0)
    assert status.state == "done", status.error
    assert status.progress == 100
    assert status.report is not None and status.report.indexed == len(_all_files(env.root))
    assert status.started_at and status.finished_at
    assert "running" in seen or "queued" in seen
    assert env.count("files") == len(_all_files(env.root))


async def test_progress_is_monotonic_and_ends_at_100(env: Env) -> None:
    values: list[int] = []
    await env.indexer.scan(progress_cb=values.append)
    assert values[0] == 0
    assert values[-1] == 100
    assert values == sorted(values)
    assert all(0 <= v <= 100 for v in values)


async def test_failed_job_reports_error(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*, progress_cb: object = None) -> object:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(env.indexer, "scan", boom)
    job_id = env.indexer.start_scan_job()
    status = await env.indexer.wait_job(job_id)
    assert status is not None
    assert status.state == "failed"
    assert status.error is not None and "disk on fire" in status.error


def test_batch_size_matches_design() -> None:
    from lucianke.engine.indexer import INGEST_BATCH_FILES

    assert INGEST_BATCH_FILES == 50


async def test_large_scan_batches_files_and_bumps_once_per_transaction(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    for i in range(120):
        (root / "pkg" / f"m{i:03d}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    e = _make(root, tmp_path / "paad.sqlite")
    try:
        before = e.version()
        report = await e.indexer.scan()
        assert report.indexed == 120
        assert report.transactions == 3  # 50 + 50 + 20
        assert e.version() == before + 3
        assert e.count("symbols") == 120
    finally:
        await e.indexer.aclose()
        e.readers.close()
        e.writer.close()


async def test_index_file_resolves_cross_file_calls_for_a_new_file(env: Env) -> None:
    await env.indexer.scan()
    new = env.root / "src" / "shop" / "api" / "extra.py"
    new.write_text(
        "from shop.services.payment import PaymentProcessor, PaymentRequest\n\n\n"
        "async def pay(p: PaymentProcessor, req: PaymentRequest):\n"
        "    return await p.process_charge(req)\n",
        encoding="utf-8",
    )

    stats = await env.indexer.index_file("src/shop/api/extra.py")

    assert stats is not None and stats.file_created
    rows = env.rows(
        "SELECT source_urn, target_urn FROM relations WHERE relation_kind = 'CALLS' AND source_file_id = ?",
        stats.file_id,
    )
    assert any(
        s.endswith("#pay") and t.endswith("payment.py#PaymentProcessor.process_charge") for s, t in rows
    ), rows
