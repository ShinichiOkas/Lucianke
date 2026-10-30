"""走査の途中の索引（BUG-0070）: ``Indexer.building`` の進みと、``Indexer.ingest_ahead`` の先取り、走査の後の収束。

走査は時間ではなく門で止める（決定的）。``_walk``（列挙。スレッドで走る）を ``threading.Event`` で、``_submit_batch``（2 つ目の
バッチの書き込み）を ``asyncio.Event`` で止め、その間に先取りし、門を開けて走査を終わらせる。

収束: 先取りした走査の後の ``files`` / ``symbols`` / ``relations`` が、先取りしない普通の走査（同じ内容の別の写し）と同じになる。
先取りは関係を整えない（まだ取り込まれていない定義元を指す行を消さない）。整えるのは走査の仕事。
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import lucianke.engine.indexer as indexer_mod
from lucianke.engine.indexer import Indexer, ScanProgress
from lucianke.index.connection import ReaderPool
from lucianke.index.ingest import IngestStats
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS_ID = "poly"
PAYMENT = "src/shop/services/payment.py"
ROUTES = (
    "src/shop/api/routes.py"  # payment.py を import して process_charge を呼ぶ（列挙では payment.py より前）
)
GATE_S = 60.0  # 門の待ちの上限（開け忘れのときにテストを止めない保険。通常は即座）
# 列挙の後に作るファイル。payment.py に無い名前（``Missing``）を指す行は、整えると消える（整えなければ残る）
LATE_BODY = (
    "from shop.services.payment import Missing, PaymentProcessor\n\n\n"
    "def late(p: PaymentProcessor):\n    Missing()\n    return p.apply_discount(1, 2)\n"
)


@dataclass
class Env:
    root: Path
    indexer: Indexer
    readers: ReaderPool
    writer: IndexWriter

    async def close(self) -> None:
        await self.indexer.aclose()
        self.readers.close()
        self.writer.close()


async def _open(tmp_path: Path, name: str) -> Env:
    root = tmp_path / name / WS_ID
    shutil.copytree(POLY, root)
    db = tmp_path / name / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache())
    return Env(root.resolve(), indexer, readers, writer)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e = await _open(tmp_path, "a")
    yield e
    await e.close()


def snapshot(e: Env) -> dict[str, list[tuple[Any, ...]]]:
    """索引の中身（行の順に依らない形）。``file_id`` は写しごとに違うので、パスに直して比べる。"""
    with e.readers.reader() as conn:
        files = conn.execute(
            "SELECT path, sha256, language, tier FROM files WHERE workspace_id = ?", (WS_ID,)
        )
        symbols = conn.execute(
            "SELECT s.urn, s.kind, s.start_line, s.end_line, s.signature FROM symbols s"
            " JOIN files f ON f.file_id = s.file_id WHERE f.workspace_id = ?",
            (WS_ID,),
        )
        relations = conn.execute(
            "SELECT r.source_urn, r.target_urn, r.relation_kind, r.call_site_line, r.call_site_col, r.use_kind,"
            " (SELECT path FROM files WHERE file_id = r.target_file_id)"
            " FROM relations r JOIN files f ON f.file_id = r.source_file_id WHERE f.workspace_id = ?",
            (WS_ID,),
        )
        return {
            "files": sorted(tuple(r) for r in files.fetchall()),
            "symbols": sorted(tuple(r) for r in symbols.fetchall()),
            "relations": sorted(tuple(map(str, r)) for r in relations.fetchall()),
        }


async def baseline(
    tmp_path: Path, prepare: Callable[[Path], None] | None = None
) -> dict[str, list[tuple[Any, ...]]]:
    """先取りしない普通の走査の索引（同じ内容の別の写し）。"""
    e = await _open(tmp_path, "baseline")
    try:
        if prepare is not None:
            prepare(e.root)
        report = await e.indexer.scan()
        assert not report.errors, report.errors
        return snapshot(e)
    finally:
        await e.close()


class WalkGate:
    """``_walk``（ファイルの列挙）を止める門。走査は進み ``ScanProgress(0, None)`` のまま、``files`` を読む前で止まる。"""

    def __init__(self, indexer: Indexer) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        inner = indexer._walk

        def walk() -> tuple[list[Any], bool]:
            self.entered.set()
            if not self.release.wait(GATE_S):
                raise TimeoutError("the walk gate was never opened")
            return inner()

        indexer._walk = walk  # type: ignore[method-assign]

    async def wait_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, GATE_S), "the scan never reached the walk"


class BatchGate:
    """2 つ目のバッチの書き込みで止める門（1 バッチ 2 ファイルにする）。列挙の後、``files`` を読んだ後の走査の途中で止まる。"""

    def __init__(self, indexer: Indexer, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(indexer_mod, "INGEST_BATCH_FILES", 2)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        inner: Callable[[list[Any]], Awaitable[list[IngestStats]]] = indexer._submit_batch
        calls = 0

        async def submit(batch: list[Any]) -> list[IngestStats]:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.entered.set()
                await asyncio.wait_for(self.release.wait(), GATE_S)
            return await inner(batch)

        indexer._submit_batch = submit  # type: ignore[method-assign]

    async def wait_entered(self) -> None:
        await asyncio.wait_for(self.entered.wait(), GATE_S)


def known(e: Env, path: str) -> bool:
    with e.readers.reader() as conn:
        return (
            conn.execute("SELECT 1 FROM files WHERE workspace_id = ? AND path = ?", (WS_ID, path)).fetchone()
            is not None
        )


async def finish(e: Env, job_id: str) -> None:
    status = await asyncio.wait_for(e.indexer.wait_job(job_id), GATE_S)
    assert status is not None and status.state == "done", status
    assert status.report is not None and not status.report.errors, status.report.errors


# --- 進み ---------------------------------------------------------------------------------------


async def test_building_is_none_before_any_scan_and_after_the_scan(env: Env) -> None:
    assert env.indexer.building() is None
    await finish(env, env.indexer.start_scan_job())
    assert env.indexer.building() is None


async def test_building_reports_listing_while_the_walk_runs(env: Env) -> None:
    gate = WalkGate(env.indexer)
    job = env.indexer.start_scan_job()
    assert env.indexer.building() == ScanProgress(0, None)  # 投入した直後（queued）から走査の途中
    await gate.wait_entered()
    progress = env.indexer.building()
    assert progress == ScanProgress(0, None) and progress.percent == 0
    gate.release.set()
    await finish(env, job)
    assert env.indexer.building() is None


async def test_building_counts_files_in_the_middle_of_the_scan(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = BatchGate(env.indexer, monkeypatch)
    job = env.indexer.start_scan_job()
    await gate.wait_entered()
    listing, _ = await asyncio.to_thread(env.indexer._walk)
    progress = env.indexer.building()
    assert progress is not None and progress.files_total == len(listing) > 10
    assert (
        0 < progress.files_done < progress.files_total
    )  # 2 つ目のバッチで止まっている（6 ファイル目より先へ進めない）
    assert 0 <= progress.percent < 100
    gate.release.set()
    await finish(env, job)
    assert env.indexer.building() is None


def test_percent_stays_below_100_until_the_scan_ends() -> None:
    assert ScanProgress(1300, 1300).percent == 99  # 全ファイルを見た後も、関係の整えと順位の計算が残る
    assert ScanProgress(412, 1300).percent == 31
    assert ScanProgress(0, 0).percent == 0 and ScanProgress(0, None).percent == 0


# --- 先取り -------------------------------------------------------------------------------------


async def test_ingest_ahead_while_the_walk_runs_then_the_scan_converges(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列挙の途中（``files`` を読む前）に先取り → 走査は先取りしたファイルも取り込み直して整える。

    先取りした 1 ファイルがあっても、空の索引からの走査は初回のまま（差分の解き直し ``after_scan`` を全ファイルに走らせない）。
    """
    differential: list[object] = []
    real_after_scan = indexer_mod.reresolve.after_scan

    async def spy(*args: Any, **kwargs: Any) -> None:
        differential.append(kwargs)
        await real_after_scan(*args, **kwargs)

    monkeypatch.setattr(indexer_mod.reresolve, "after_scan", spy)
    gate = WalkGate(env.indexer)
    job = env.indexer.start_scan_job()
    await gate.wait_entered()
    assert not known(env, PAYMENT)
    assert await env.indexer.ingest_ahead(PAYMENT) == "indexed"
    assert known(env, PAYMENT)
    with env.readers.reader() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT s.qualified_name FROM symbols s JOIN files f ON f.file_id = s.file_id WHERE f.path = ?", (PAYMENT,)
        )}  # fmt: skip
    assert "PaymentProcessor.process_charge" in names
    assert await env.indexer.ingest_ahead(PAYMENT) == "present"  # 2 回目は何もしない
    gate.release.set()
    await finish(env, job)
    assert differential == []
    assert snapshot(env) == await baseline(tmp_path)


async def test_ingest_ahead_in_the_middle_of_the_scan_keeps_relations_to_files_not_yet_indexed(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """走査の途中（``files`` を読んだ後）に先取り。先取りは整えないので、まだ取り込まれていない定義元への行も残り、走査の後に一致する。"""
    gate = BatchGate(env.indexer, monkeypatch)
    job = env.indexer.start_scan_job()
    await gate.wait_entered()
    assert not known(env, PAYMENT) and not known(env, ROUTES)
    assert await env.indexer.ingest_ahead(ROUTES) == "indexed"
    with (
        env.readers.reader() as conn
    ):  # routes.py → payment.py の呼び出しの行（payment.py はまだ索引に無い）が残っている
        calls = conn.execute(
            "SELECT count(*) FROM relations WHERE target_urn = ?",
            (f"lk://{WS_ID}/{PAYMENT}#PaymentProcessor.process_charge",),
        ).fetchone()[0]
    assert calls >= 2
    gate.release.set()
    await finish(env, job)
    assert snapshot(env) == await baseline(tmp_path)


async def test_a_file_ingested_ahead_that_the_scan_never_listed_is_settled_at_the_end(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列挙の後に作られたファイルを先取り → 走査は取り込み直さないので、走査の終わりに新しいファイルとして取り込み直す（整える）。"""
    extra = "src/shop/services/late.py"

    def add(root: Path) -> None:
        (root / extra).write_text(LATE_BODY, encoding="utf-8", newline="\n")

    gate = BatchGate(env.indexer, monkeypatch)
    job = env.indexer.start_scan_job()
    await gate.wait_entered()
    add(env.root)
    assert await env.indexer.ingest_ahead(extra) == "indexed"
    gate.release.set()
    await finish(env, job)
    assert snapshot(env) == await baseline(tmp_path, add)


async def test_ingest_ahead_skips_what_the_index_must_not_hold(env: Env) -> None:
    (env.root / ".gitignore").write_text("ignored/\n", encoding="utf-8", newline="\n")
    (env.root / "ignored").mkdir()
    (env.root / "ignored" / "x.py").write_text("def x():\n    return 1\n", encoding="utf-8", newline="\n")
    gate = WalkGate(env.indexer)
    job = env.indexer.start_scan_job()
    await gate.wait_entered()
    assert await env.indexer.ingest_ahead("ignored/x.py") == "skipped"  # .gitignore
    assert await env.indexer.ingest_ahead("src/shop/nope.py") == "skipped"  # ディスクに無い
    assert await env.indexer.ingest_ahead("src/shop") == "skipped"  # ディレクトリ
    assert not known(env, "ignored/x.py") and not known(env, "src/shop/nope.py")
    gate.release.set()
    await finish(env, job)


async def test_ingest_ahead_without_a_scan_indexes_and_settles_like_index_file(
    env: Env, tmp_path: Path
) -> None:
    """走査が無いときは ``index_file`` と同じ（関係も整える）。"""
    await finish(env, env.indexer.start_scan_job())
    extra = "src/shop/services/late.py"

    def add(root: Path) -> None:
        (root / extra).write_text(LATE_BODY, encoding="utf-8", newline="\n")

    add(env.root)
    assert env.indexer.building() is None
    assert await env.indexer.ingest_ahead(extra) == "indexed"
    assert snapshot(env) == await baseline(tmp_path, add)
