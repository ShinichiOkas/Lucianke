"""索引の取り込み器（設計書 §7.6.2、§7.6.5、§6.1、§8.1。仕様書 §9.4、§12.4、§13.5）。

ワークスペースのファイルを走査し、解析・シンボル・関係を作って PaaD に取り込む。

* ``index_file(rel)``: 1 ファイルの再取り込み（読み込み → ``sha256`` → DB と同じなら何もしない → 言語判定 → 解析 →
  ``FileFacts`` → ``IndexWriter``）。ファイルが消えていた・除外対象になっていたら ``remove`` に回す。
* ``remove(rel)``: ``files`` から消す。
* ``scan()``: 起動時の差分索引と全体の再索引（同じ処理）。``.gitignore`` と ``exclude_globs`` を尊重して列挙し、
  DB にあって消えたファイルは消し、``sha256`` が変わった・新しいファイルだけ取り込む。1 トランザクションに最大
  ``INGEST_BATCH_FILES`` ファイルをまとめ、``data_version`` はトランザクションごとに 1 回だけ進む（設計書 §7.6.1 項 4）。
* ``start_scan_job()`` / ``job_status()``: 背景ジョブ（T-00 ``reindex``、``status`` の ``jobs[]``）。
* ``building()`` / ``ingest_ahead(rel)``: 走査の途中の進み（ファイル数）と、走査がまだ届いていない 1 ファイルの先取り（BUG-0070）。
  ツールは対象のファイルを先取りしてから解決し、「索引に無い」とは答えない。先取りは関係を整えない（走査が取り込み直して整える）。

並列度（設計書 §6.1、スパイク S-2）: 構文解析は GIL を握るので並列化しない。ファイル 1 つの解析はスレッド
（``asyncio.to_thread``）で走らせてイベントループを止めず、書き込みは ``IndexWriter`` の 1 本のキューに積む。
走査中は、1 バッチの書き込みを待たずに次のバッチの解析へ進む（書き込みは常に 1 バッチだけ先行させる）。

ティアごとの取り込み:

* Tier 1〜2（タグクエリのある言語）: 構文木 → ``symbols`` → ``relations``（``structural``）→ 構文エラーの診断。
* Tier 3 と ``.ipynb``（データ形式アダプタ。``syntax.support.get_adapter``）: ``files`` 行と、アダプタの ``symbols(source, *, rel, workspace_id)``
  （骨格の最上位の節 = JSON / YAML / TOML のキー、XML の根と直下のタグ。``Kind.KEY`` / ``Kind.TAG``。壊れたファイル・大きいファイル・
  JSONL・ipynb は空）。
* Tier 4（と、タグクエリの無い Tier 1〜2）: ``heuristic.outline``（ctags、無ければブロック走査）。
* 秘密ファイル・バイナリ・``max_file_bytes`` 超過: ``files`` 行だけ（``lexical_fallback``。本文は解析しない）。

関係の ``target_urn``: 同じファイル内の定義 → import 表（``imported_name_map``）→ 解決できなかった import は外部シンボル
``lk://_ext/<パッケージ>#<名前>``（``external_name_map``。設計書 §7.4.6 項 3）→ 言語の組み込み名は ``lk://_ext/builtins#<名前>``（Python）・``lk://_ext/globals#<名前>``（TS / JS）
→ それでも解決できなければ捨てる。import 表の解決は定義元の存在を確かめずに URN を作る（走査の順序に依存させないため）ので、
取り込みの後に、``symbols`` に無い目標を指す行を消し、``target_file_id`` を埋める（``_settle_relations``）。
オーバーロードのあるシンボル（TS の ``charge/1`` ``charge/2~hash``）への ``arity`` なしの目標は、同じ修飾名の最後の宣言
（実装シグネチャ）の URN に直す。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import logging
import os
import stat
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from lucianke.core.limits import MAX_FILE_BYTES
from lucianke.core.model import Confidence, RelPath, SymbolRecord, Tier
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import CodeSel, Urn, parse
from lucianke.engine import reresolve
from lucianke.engine.rank_scheduler import RANK_DEBOUNCE_S, RankScheduler
from lucianke.index import inherited, queries, rank
from lucianke.index.connection import ReaderPool
from lucianke.index.ingest import (
    FileFacts,
    IngestStats,
    NameCallFact,
    RelationFact,
    SymbolExtra,
    SyntaxErrorFact,
    ingest_file,
    remove_file,
    upsert_workspace,
)
from lucianke.index.writer import IndexWriter
from lucianke.syntax import heuristic, relations
from lucianke.syntax.builtin_names import BUILTINS_PACKAGE, GLOBALS_PACKAGE
from lucianke.syntax.imports import (
    ImportFact,
    external_name_map,
    extract_imports,
    imported_name_map,
    make_resolver,
)
from lucianke.syntax.parsers import TreeCache
from lucianke.syntax.support import get_adapter, get_support
from lucianke.syntax.symbols import extract_detailed, load_query_source
from lucianke.workspace.languages import Detected, detect, is_test
from lucianke.workspace.paths import confine, is_excluded
from lucianke.workspace.types import Workspace

__all__ = [
    "INGEST_BATCH_FILES",
    "Indexer",
    "JobState",
    "JobStatus",
    "ScanProgress",
    "ScanReport",
]

_log = logging.getLogger(__name__)

INGEST_BATCH_FILES: Final = 50  # 設計書 §7.6.1 項 4: 起動時の差分索引は 1 トランザクションに最大 50 ファイル
_CHUNK: Final = 1024 * 1024
_HEAD_BYTES: Final = 8 * 1024
_SYNTAX_ERROR_MESSAGE: Final = "syntax error"

JobState = Literal["queued", "running", "done", "failed"]


# --- 公開型 -----------------------------------------------------------------------------


@dataclass(slots=True)
class ScanReport:
    """``scan`` の結果（ログと受入検査用）。"""

    files_seen: int = 0  # 走査で見つけたファイル数（除外を除く）
    unchanged: int = 0  # ``sha256`` が DB と同じで取り込まなかった数
    indexed: int = 0  # 取り込んだ数（新規 + 変更）
    created: int = 0  # うち新規
    removed: int = 0  # DB にあって消えていたため消した数
    transactions: int = 0  # 取り込み・削除のトランザクション数（``data_version`` の増分）
    gitignore: bool = True  # ``.gitignore`` を評価できたか。False なら ``exclude_globs`` だけで走査した
    errors: list[tuple[str, str]] = field(default_factory=list)  # (パス, 理由)。取り込み自体に失敗したもの
    degraded: list[tuple[str, str]] = field(
        default_factory=list
    )  # (パス, 理由)。解析に失敗して files 行だけにしたもの
    duration_s: float = 0.0


@dataclass(frozen=True, slots=True)
class ScanProgress:
    """走査（初回・差分の索引）の途中の進み（``Indexer.building``。BUG-0070）。

    ``files_total`` は列挙の前は ``None``。``percent`` は終わるまで 99 で止める（全ファイルを見た後も、関係の整えと順位の計算が残る）。
    """

    files_done: int
    files_total: int | None

    @property
    def percent(self) -> int:
        if not self.files_total:
            return 0
        return min(99, (self.files_done * 100) // self.files_total)


@dataclass(frozen=True, slots=True)
class JobStatus:
    """``job_status`` の戻り値（T-00 の ``jobs[]`` の元。仕様書 §11 T-00）。時刻は UTC の ISO 8601。"""

    job_id: str
    kind: str
    state: JobState
    progress: int  # 0〜100
    started_at: str | None
    finished_at: str | None
    error: str | None
    report: ScanReport | None


@dataclass(slots=True)
class _Job:
    job_id: str
    kind: str
    state: JobState = "queued"
    progress: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    report: ScanReport | None = None

    def snapshot(self) -> JobStatus:
        return JobStatus(
            self.job_id,
            self.kind,
            self.state,
            self.progress,
            self.started_at,
            self.finished_at,
            self.error,
            self.report,
        )


# --- 内部型 -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Loaded:
    """読み込んだファイル。``data`` は ``max_file_bytes`` 以下のときだけ（超過は流し読みで ``sha256`` と行数だけ）。"""

    data: bytes | None
    sha256: str
    size: int
    line_count: int
    head: bytes


_Unchanged = Literal["unchanged"]
_Gone = Literal["gone"]


@dataclass(frozen=True, slots=True)
class _Prepared:
    facts: FileFacts
    degraded: str | None = None  # 解析に失敗して files 行だけにしたときの理由


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@lru_cache(maxsize=32)
def _has_symbol_queries(language_id: str) -> bool:
    try:
        load_query_source(language_id)
    except (ValueError, OSError):
        return False
    return True


def _grammar_support(language_id: str) -> object | None:
    """文法つきの登録（``LanguageSpec``）。アダプタだけの言語・未登録は ``None``。"""
    support = get_support(language_id)
    return support if support is not None and hasattr(support, "grammar") else None


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Indexer ----------------------------------------------------------------------------


class Indexer:
    """1 つのワークスペースの索引の取り込み器。

    Args:
        ws: 対象のワークスペース。
        writer: PaaD の唯一の書き手。
        reader_pool: 既存の ``files`` を引く読み取り接続のプール。
        tree_cache: 構文木のキャッシュ（ツール側の読み取りと共有する）。
        max_file_bytes: これを超えるファイルは構文木を作らず ``files`` 行だけにする（仕様書 §12.4）。
    """

    def __init__(
        self,
        ws: Workspace,
        writer: IndexWriter,
        reader_pool: ReaderPool,
        tree_cache: TreeCache,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        rank_delay_s: float = RANK_DEBOUNCE_S,
        rank_sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._ws = ws
        self._writer = writer
        self._readers = reader_pool
        self._trees = tree_cache
        self._max = max_file_bytes
        self._ws_ready = False
        self._ws_lock = asyncio.Lock()
        self._scan_lock = asyncio.Lock()
        self._jobs: dict[str, _Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._job_seq = itertools.count(1)
        self._ctags: list[str] | None = None
        self._ctags_probed = False
        self._ctags_lock = asyncio.Lock()
        self.ingest_calls = 0  # ``ingest_file`` に渡したファイル数（受入検査のカウンタ。設計書 §7.6.5）
        self.reingested_importers = 0  # 定義元の変化で取り込み直した import 元のファイル数（``ingest_calls`` には入れない。engine/reresolve.py）
        self._rank_scheduler = RankScheduler(self._scheduled_refresh, delay=rank_delay_s, sleep=rank_sleep)
        # 走査の途中の進み（``building``）と、走査より先に取り込んだファイル（``ingest_ahead``。BUG-0070）
        self._scans = 0  # 走っている ``scan`` の数（ジョブと監視のバーストの走査が重なりうる）
        self._scan_done = 0
        self._scan_total: int | None = None
        self._ahead: set[str] = set()  # 先取りしたが、まだ走査が取り込み直して整えていないパス
        self._scan_ingested: set[str] = set()  # 最後に終わった走査が取り込んだパス

    @property
    def workspace(self) -> Workspace:
        return self._ws

    @property
    def rank_scheduler(self) -> RankScheduler:
        """1 ファイルの更新（``index_file`` / ``remove``）の後の PageRank の再計算の予約（テストの待ち合わせ用）。"""
        return self._rank_scheduler

    def rebind(self, ws: Workspace) -> None:
        """設定（除外規則・Python 環境など）を変えた同じワークスペースに差し替える。以降の走査・取り込みは新しい規則で判定する。

        ``workspace_id`` と ``root`` は変えられない（束はこの 2 つで結ばれている）。走っているジョブは止めない
        （次の走査から新しい規則。除外の変更で ``files`` から外す再走査は呼び出し側が始める）。
        """
        if ws.workspace_id != self._ws.workspace_id or ws.root != self._ws.root:
            raise ValueError("rebind needs the same workspace_id and root")
        self._ws = ws

    # --- 公開 API ---------------------------------------------------------------------

    async def index_file(self, rel: str | RelPath) -> IngestStats | None:
        """1 ファイルを再取り込みする。``sha256`` が DB と同じ・読めない・対象外のときは ``None``。

        ファイルが無くなっていた、または ``exclude_globs`` / ``.gitignore`` の対象になっていたら ``remove`` する。
        """
        return await self._index_file(rel)

    async def _index_file(self, rel: str | RelPath, *, as_new: bool = False) -> IngestStats | None:
        """``index_file`` の本体。``as_new`` は DB の ``sha256`` を見ず、新しいファイルとして取り込む（import 元も解き直す）。"""
        path = await asyncio.to_thread(confine, self._ws, str(rel))
        if str(path) == ".":
            return None
        ignore = await asyncio.to_thread(self._make_ignore)
        if self._is_skipped(path, ignore):
            await self.remove(path)
            return None
        known = None if as_new else await asyncio.to_thread(self._known_sha, path)
        prepared = await self._prepare(path, known)
        if prepared == "gone":
            await self.remove(path)
            return None
        if prepared is None:
            return None
        if prepared.degraded:
            _log.warning("index_file %s: degraded to a files row (%s)", path, prepared.degraded)
        await self._ensure_workspace()
        self.ingest_calls += 1
        facts = prepared.facts

        # 新しいファイル、またはシンボルの集合が変わるなら、それを import している側の関係を同じトランザクションで解き直す
        # （BUG-0025・BUG-0034。本文だけの編集では触れない。engine/reresolve.py）
        importers = await reresolve.importers_to_redo(self, str(path), facts, is_new=known is None)

        def job(conn: object) -> IngestStats:
            stats = ingest_file(conn, facts)  # type: ignore[arg-type]
            _settle_relations(conn, [stats.file_id, *reresolve.ingest_importers(conn, importers)])  # type: ignore[arg-type]
            return stats

        stats = await self._writer.asubmit(job, bumps_version=True, label=f"ingest:{path}")
        self.reingested_importers += len(importers)
        self._rank_scheduler.touch()  # 順位は約 1 秒おとなしくなってから 1 回だけ計算し直す（engine/rank_scheduler.py）
        return stats

    def building(self) -> ScanProgress | None:
        """走査（初回・差分の索引）の途中なら、その進み。走査も待っているジョブも無ければ ``None``（索引は走査を終えている）。

        ジョブを投入してから走査が始まるまで（``queued``）は ``ScanProgress(0, None)``。
        """
        if self._scans:
            return ScanProgress(self._scan_done, self._scan_total)
        if any(j.state in ("queued", "running") for j in self._jobs.values()):
            return ScanProgress(0, None)
        return None

    async def ingest_ahead(self, rel: str | RelPath) -> Literal["present", "indexed", "skipped"]:
        """まだ ``files`` に無い 1 ファイルを、走査が届く前に取り込む（ツールの対象を「索引に無い」と答えないため。BUG-0070）。

        * ``"present"``: 既に索引にある（何もしない）。``"indexed"``: 取り込んだ。
        * ``"skipped"``: 対象外（``exclude_globs`` / ``.gitignore``）・ディスクに無い・ファイルでない。索引に無いのは正しい。

        走査の途中は ``ingest_file`` だけを行い、関係の整え（``_settle_relations``）・import 元の解き直し・順位の予約はしない。
        整えると、まだ取り込まれていない定義元を指す行が消える。走査は、始めに読んだ ``files`` に無かったファイルを必ず取り込み直して
        最後に整える。取り込み直さなかった分（列挙の後に作られた等）は、走査の終わりに新しいファイルとして取り込み直す（``scan``）。
        走査が無いときは ``index_file`` と同じ（関係も整える）。読めない・書けないときは例外。
        """
        path = await asyncio.to_thread(confine, self._ws, str(rel))
        if str(path) == ".":
            return "skipped"
        if await asyncio.to_thread(self._known_sha, path) is not None:
            return "present"
        ignore = await asyncio.to_thread(self._make_ignore)
        if self._is_skipped(path, ignore):
            return "skipped"
        if self.building() is None:
            await self._index_file(path)
        else:
            prepared = await self._prepare(path, None)
            if not isinstance(prepared, _Prepared):
                return "skipped"  # 消えていた・通常ファイルでない
            await self._ensure_workspace()
            self.ingest_calls += 1
            self._ahead.add(str(path))
            facts = prepared.facts
            await self._writer.asubmit(
                lambda conn: ingest_file(conn, facts),  # type: ignore[arg-type]
                bumps_version=True,
                label=f"ingest_ahead:{path}",
            )
            _log.info("ingested %s ahead of the running scan", path, extra={"event": "ingest_ahead"})
        return "indexed" if await asyncio.to_thread(self._known_sha, path) is not None else "skipped"

    async def remove(self, rel: str | RelPath) -> bool:
        """``rel`` を PaaD から消す。消したら ``True``、元から無ければ ``False``。"""
        path = str(rel)
        await self._ensure_workspace()
        self._trees.evict(self._ws.workspace_id, RelPath(path))
        ws_id = self._ws.workspace_id

        existed = await asyncio.to_thread(self._known_sha, RelPath(path)) is not None
        if not existed:
            return False
        # 消えたファイルを import していた側の関係を同じトランザクションで解き直す（BUG-0025）
        importers = await reresolve.prepare_importers(self, [path])

        def job(conn: object) -> bool:
            removed = remove_file(conn, ws_id, path)  # type: ignore[arg-type]
            _settle_relations(conn, reresolve.ingest_importers(conn, importers))  # type: ignore[arg-type]
            return removed

        removed = await self._writer.asubmit(job, bumps_version=True, label=f"remove:{path}")
        self.reingested_importers += len(importers)
        if removed:
            self._rank_scheduler.touch()
        return removed

    async def scan(self, *, progress_cb: Callable[[int], None] | None = None) -> ScanReport:
        """差分索引。進捗 0〜100 を ``progress_cb`` に渡す（単調増加。最後は必ず 100）。

        走っている間は ``building()`` が進み（ファイル数）を返す。終わったら、走査より先に取り込んだ（``ingest_ahead``）のに
        走査が取り込み直さなかったファイルを、新しいファイルとして取り込み直す（関係を整え、import 元を解き直す。BUG-0070）。
        """
        self._scans += 1
        if self._scans == 1:
            self._scan_done, self._scan_total = 0, None
        try:
            report = await self._scan(progress_cb)
            if self._scans == 1:  # 最後の走査（重なっていない）。整え終わるまで ``building()`` は進みを返し続ける
                await self._settle_ahead()
            return report
        finally:
            self._scans -= 1

    async def _settle_ahead(self) -> None:
        """先取りしたが走査が取り込み直さなかったファイルを、新しいファイルとして取り込み直す（その間に先取りされた分も）。"""
        ingested, self._scan_ingested = self._scan_ingested, set()
        while self._ahead:
            paths, self._ahead = self._ahead - ingested, set()
            ingested = set()
            for path in sorted(paths):
                try:
                    await self._index_file(path, as_new=True)
                except Exception:  # 1 ファイルの失敗で走査の終わりを止めない（次の監視のイベントか走査で直る）
                    _log.warning("could not settle %s (ingested ahead of the scan)", path, exc_info=True)

    async def _scan(self, progress_cb: Callable[[int], None] | None) -> ScanReport:
        t0 = time.monotonic()
        report = ScanReport()
        last = -1

        def progress(value: int) -> None:
            nonlocal last
            value = max(last, min(100, value))
            if value != last and progress_cb is not None:
                progress_cb(value)
            last = value

        progress(0)
        await self._ensure_workspace()
        listing, report.gitignore = await asyncio.to_thread(self._walk)
        report.files_seen = len(listing)
        known = await asyncio.to_thread(self._known_files)
        for path in self._ahead:  # 先取りしたまま整えていないファイルは、この走査で取り込み直して整える（BUG-0070）
            known.pop(RelPath(path), None)
        self._scan_total = len(listing)
        progress(2)

        gone = sorted(set(known) - set(listing))
        for chunk in _chunks(gone, INGEST_BATCH_FILES):
            await self._remove_batch(chunk, report)
        progress(5)

        total = max(len(listing), 1)
        pending: asyncio.Future[list[IngestStats]] | None = None
        batch: list[FileFacts] = []
        file_ids: list[int] = []
        ingested: set[str] = set()
        # 既存でシンボルの集合が変わったファイル（import している側を解き直す。BUG-0034）
        names_changed: list[str] = []

        async def flush() -> None:
            nonlocal pending, batch
            if pending is not None:
                for s in await self._collect(pending, report):
                    file_ids.append(s.file_id)
                    ingested.add(s.path)
                    if s.symbol_set_changed and not s.file_created:
                        names_changed.append(s.path)
                pending = None
            if batch:
                pending = asyncio.ensure_future(self._submit_batch(batch))
                report.transactions += 1
                batch = []

        for i, rel in enumerate(listing, 1):
            try:
                prepared = await self._prepare(rel, known.get(rel))
            except Exception as exc:  # 1 ファイルの失敗で走査全体を止めない
                _log.warning("scan: could not prepare %s", rel, exc_info=True)
                report.errors.append((str(rel), f"{type(exc).__name__}: {exc}"))
                prepared = None
            if prepared == "gone":
                prepared = None
            if prepared is None:
                report.unchanged += 1
            else:
                if prepared.degraded:
                    report.degraded.append((str(rel), prepared.degraded))
                batch.append(prepared.facts)
                if len(batch) >= INGEST_BATCH_FILES:
                    await flush()
            progress(5 + (i * 90) // total)
            self._scan_done = i
        await flush()
        await flush()  # 最後に積んだバッチの完了を待つ
        self._scan_ingested |= ingested
        report.unchanged -= len(report.errors)  # 失敗は「変わらなかった」に数えない
        if file_ids:
            await self._writer.asubmit(
                lambda conn: _settle_relations(conn, file_ids),  # type: ignore[arg-type]
                bumps_version=False,
                label="settle_relations",
            )
        if known:  # 初回の全体走査は、取り込む側が最後のファイルシステムを見て解決済み。差分のときだけ、取り込まなかった側を解き直す
            created = [r for r in listing if r not in known]
            await reresolve.after_scan(
                self, created=created, removed=gone, skip_file_ids=set(file_ids), changed=names_changed
            )
        if report.indexed or report.removed or not await asyncio.to_thread(self._ranks_ready):
            await self.refresh_ranks_quietly()  # 全体の取り込みの後に 1 回（index/rank.py。T-01 の順位）
        progress(100)
        report.duration_s = time.monotonic() - t0
        return report

    async def refresh_ranks(self) -> int:
        """参照グラフの重要度（PageRank・fan_in）を計算し直して書く（``index/rank.py``）。書いたシンボルの行数を返す。

        計算は読み取り接続でスレッドに逃がし（書き手を止めない）、書き込みは短いジョブ 1 つ。``data_version`` は進めない。
        ``scan`` が全体の取り込みの後に 1 回呼ぶ。1 ファイルの再取り込み（``index_file`` / ``remove``）は直接は呼ばず、
        ``rank_scheduler`` のデバウンスで最後の更新の約 1 秒後に 1 回だけ呼ぶ（編集のたびに全体を計算し直さないため）。
        """
        await self._ensure_workspace()
        ws_id = self._ws.workspace_id

        def compute() -> rank.RankResult:
            with self._readers.reader() as conn:
                return rank.compute_ranks(conn, ws_id)

        result = await asyncio.to_thread(compute)
        return await self._writer.asubmit(
            lambda conn: rank.store_ranks(conn, ws_id, result),  # type: ignore[arg-type]
            bumps_version=False,
            label="pagerank",
        )

    async def refresh_ranks_quietly(self) -> None:
        """``refresh_ranks`` の失敗を握って記録する（順位は付加情報で、取り込みの成否に影響させない）。"""
        try:
            await self.refresh_ranks()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("pagerank refresh failed", exc_info=True)

    async def _scheduled_refresh(self) -> None:
        """デバウンスの後の再計算。走査のジョブが走っていれば終わるのを待つ（走査の最後の計算より古い結果で上書きしない）。"""
        async with self._scan_lock:
            await self.refresh_ranks_quietly()

    def _ranks_ready(self) -> bool:
        with self._readers.reader() as conn:
            return rank.ranked_symbol_count(conn, self._ws.workspace_id) > 0

    def start_scan_job(self) -> str:
        """``scan`` を背景タスクとして始め、``job_id`` を返す（すぐ戻る。仕様書 §11 T-00 処理規約 6）。

        走っている ``scan`` があれば、その後ろで ``queued`` のまま待つ。イベントループの中から呼ぶこと。
        """
        job = _Job(job_id=f"job_{next(self._job_seq):04d}", kind="reindex")
        self._jobs[job.job_id] = job
        task = asyncio.get_running_loop().create_task(self._run_job(job), name=f"lucianke-scan-{job.job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job.job_id

    def job_status(self, job_id: str) -> JobStatus | None:
        """ジョブの状態。未知の ``job_id`` は ``None``。"""
        job = self._jobs.get(job_id)
        return None if job is None else job.snapshot()

    def jobs(self) -> list[JobStatus]:
        """全ジョブの状態（新しい順）。"""
        return [j.snapshot() for j in reversed(self._jobs.values())]

    async def wait_job(self, job_id: str) -> JobStatus | None:
        """ジョブが ``done`` / ``failed`` になるまで待つ（テストと終了処理用）。"""
        while (status := self.job_status(job_id)) is not None and status.state in ("queued", "running"):
            await asyncio.sleep(0.01)
        return self.job_status(job_id)

    async def aclose(self) -> None:
        """走っているジョブと、PageRank の再計算の予約を取り消して待つ。"""
        await self._rank_scheduler.aclose()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # --- ジョブ -----------------------------------------------------------------------

    async def _run_job(self, job: _Job) -> None:
        try:
            async with self._scan_lock:
                job.state = "running"
                job.started_at = _now()

                def on_progress(value: int) -> None:
                    job.progress = value

                job.report = await self.scan(progress_cb=on_progress)
                job.progress = 100
                job.state = "done"
        except asyncio.CancelledError:
            job.state = "failed"
            job.error = "cancelled"
            raise
        except Exception as exc:
            _log.exception("scan job %s failed", job.job_id)
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = _now()

    # --- workspaces 行 ----------------------------------------------------------------

    async def _ensure_workspace(self) -> None:
        if self._ws_ready:
            return
        async with self._ws_lock:
            if self._ws_ready:
                return
            import json

            ws = self._ws
            row = {
                "workspace_id": ws.workspace_id,
                "root_path": ws.root.as_posix(),
                "python_env": ws.python_env.interpreter if ws.python_env is not None else None,
                "default_base_ref": ws.default_base_ref,
                "exclude_globs_json": json.dumps(list(ws.exclude_globs)),
                "indexed_at": _now(),
            }
            await self._writer.asubmit(
                lambda conn: upsert_workspace(conn, row),  # type: ignore[arg-type]
                bumps_version=False,
                label="upsert_workspace",
            )
            self._ws_ready = True

    # --- DB の読み取り ----------------------------------------------------------------

    def _known_sha(self, rel: RelPath) -> str | None:
        with self._readers.reader() as conn:
            info = queries.file_by_path(conn, self._ws.workspace_id, str(rel))
        return None if info is None else info.sha256

    def _known_files(self) -> dict[RelPath, str]:
        with self._readers.reader() as conn:
            rows = conn.execute(
                "SELECT path, sha256 FROM files WHERE workspace_id = ?", (self._ws.workspace_id,)
            ).fetchall()
        return {RelPath(str(p)): str(s) for p, s in rows}

    # --- 走査 -------------------------------------------------------------------------

    def _make_ignore(self) -> object | None:
        """``.gitignore`` の評価器（dulwich）。作れなければ ``None``（``exclude_globs`` だけで走査する）。"""
        try:
            from dulwich.ignore import IgnoreFilter, IgnoreFilterManager

            root = str(self._ws.root)
            global_filters = []
            exclude = self._ws.root / ".git" / "info" / "exclude"
            if exclude.is_file():
                with contextlib.suppress(OSError):
                    global_filters.append(IgnoreFilter.from_path(str(exclude)))
            return IgnoreFilterManager(root, global_filters, self._ws.case_insensitive)
        except Exception:
            _log.warning("could not set up .gitignore evaluation; using exclude_globs only", exc_info=True)
            return None

    def _is_skipped(self, rel: RelPath, ignore: object | None) -> bool:
        """``rel``（ファイル）が索引の対象外か。上位ディレクトリの除外・無視も含める。"""
        parts = str(rel).split("/")
        if ".git" in parts or is_excluded(self._ws, rel):
            return True
        if ignore is None:
            return False
        try:
            for k in range(1, len(parts)):  # 上位ディレクトリが無視されていれば配下も無視
                if ignore.is_ignored("/".join(parts[:k]) + "/"):  # type: ignore[attr-defined]
                    return True
            return bool(ignore.is_ignored(str(rel)))  # type: ignore[attr-defined]
        except Exception:
            return False

    def _walk(self) -> tuple[list[RelPath], bool]:
        """ワークスペースのファイルを列挙する（昇順）。シンボリックリンクと ``.git`` は辿らない。"""
        ignore = self._make_ignore()
        root = str(self._ws.root)
        out: list[RelPath] = []
        stack: list[str] = [""]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(os.path.join(root, directory) if directory else root) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                name = entry.name
                if name == ".git":
                    continue
                rel = f"{directory}/{name}" if directory else name
                try:
                    if entry.is_symlink() or entry.is_junction():
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if is_excluded(self._ws, RelPath(rel + "/.")):
                        continue
                    if ignore is not None and _safe_ignored(ignore, rel + "/"):
                        continue
                    stack.append(rel)
                elif is_file:
                    if is_excluded(self._ws, RelPath(rel)):
                        continue
                    if ignore is not None and _safe_ignored(ignore, rel):
                        continue
                    out.append(RelPath(rel))
        out.sort()
        return out, ignore is not None

    # --- 読み込み ---------------------------------------------------------------------

    def _load(self, rel: RelPath, known_sha: str | None) -> _Loaded | _Unchanged | _Gone:
        path = self._ws.root / str(rel)
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                return "gone"
            if st.st_size > self._max:
                return self._load_streaming(path, known_sha)
            data = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError, PermissionError, IsADirectoryError):
            return "gone"
        sha = _hash_bytes(data)
        if sha == known_sha:
            return "unchanged"
        return _Loaded(data, sha, len(data), -1, data[:_HEAD_BYTES])

    def _load_streaming(self, path: Path, known_sha: str | None) -> _Loaded | _Unchanged | _Gone:
        digest = hashlib.sha256()
        size = newlines = 0
        last = b""
        head = b""
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                if not head:
                    head = chunk[:_HEAD_BYTES]
                digest.update(chunk)
                size += len(chunk)
                newlines += chunk.count(b"\n")
                last = chunk[-1:]
        sha = digest.hexdigest()
        if sha == known_sha:
            return "unchanged"
        lines = newlines + (1 if size and last != b"\n" else 0)
        return _Loaded(None, sha, size, lines, head)

    # --- 1 ファイルの事実 -------------------------------------------------------------

    async def _prepare(self, rel: RelPath, known_sha: str | None) -> _Prepared | None | Literal["gone"]:
        """``FileFacts`` を作る。変わっていなければ ``None``、読めなければ ``"gone"``。DB には書かない。"""
        loaded = await asyncio.to_thread(self._load, rel, known_sha)
        if loaded == "unchanged":
            return None
        if loaded == "gone":
            return "gone"
        assert isinstance(loaded, _Loaded)
        detected = detect(rel, loaded.head)
        ws_id = self._ws.workspace_id

        def files_only(*, degraded: str | None = None) -> _Prepared:
            lines = 0
            if detected.index_content:
                lines = (
                    loaded.line_count if loaded.data is None else LineIndex(loaded.data).content_line_count
                )
            return _Prepared(
                FileFacts(
                    workspace_id=ws_id,
                    path=rel,
                    language=detected.language_id,
                    tier=detected.tier,
                    line_count=lines,
                    byte_size=loaded.size,
                    sha256=loaded.sha256,
                    is_test=is_test(rel),
                    confidence=Confidence.LEXICAL_FALLBACK,
                ),
                degraded,
            )

        # 秘密・バイナリ・大きすぎるファイルは files 行だけ（仕様書 §12.4、§13.5）
        if not detected.index_content or loaded.data is None:
            return files_only()
        data = loaded.data
        lang = detected.language_id
        try:
            if detected.tier <= Tier.T2 and _grammar_support(lang) is not None and _has_symbol_queries(lang):
                facts = await asyncio.to_thread(self._analyze_code, rel, loaded, data, detected)
                return _Prepared(facts)
            if detected.tier == Tier.T3 or get_adapter(lang) is not None:  # .ipynb（Tier 2）もアダプタの言語
                return _Prepared(await asyncio.to_thread(self._analyze_adapter, rel, loaded, data, detected))
            return _Prepared(await self._analyze_outline(rel, loaded, data, detected))
        except Exception as exc:
            _log.warning("analysis failed for %s (%s); indexing the file row only", rel, lang, exc_info=True)
            return files_only(degraded=f"{type(exc).__name__}: {exc}")

    def _base_facts(
        self,
        rel: RelPath,
        loaded: _Loaded,
        data: bytes,
        detected: Detected,
        *,
        confidence: Confidence,
        symbols: Sequence[SymbolRecord] = (),
        extras: dict[str, SymbolExtra] | None = None,
        relations: Sequence[RelationFact] = (),
        syntax_errors: Sequence[SyntaxErrorFact] = (),
        import_stems: Sequence[str] = (),
        name_calls: Sequence[NameCallFact] = (),
    ) -> FileFacts:
        support = get_support(detected.language_id)
        extra_globs = tuple(getattr(support, "test_path_globs", ()) or ())
        return FileFacts(
            workspace_id=self._ws.workspace_id,
            path=rel,
            language=detected.language_id,
            tier=detected.tier,
            line_count=LineIndex(data).content_line_count,
            byte_size=loaded.size,
            sha256=loaded.sha256,
            is_test=is_test(rel, extra_globs),
            confidence=confidence,
            symbols=tuple(symbols),
            relations=tuple(relations),
            syntax_errors=tuple(syntax_errors),
            symbol_extras=extras or {},
            import_stems=tuple(import_stems),
            name_calls=tuple(name_calls),
        )

    def _analyze_code(self, rel: RelPath, loaded: _Loaded, data: bytes, detected: Detected) -> FileFacts:
        """Tier 1〜2: 解析 → シンボル → 関係 → 構文エラー（スレッドで走る）。"""
        lang = detected.language_id
        ws_id = self._ws.workspace_id
        support = _grammar_support(lang)
        assert support is not None
        parsed = self._trees.get_or_parse(ws_id, rel, loaded.sha256, lang, lambda: data)
        extraction = extract_detailed(
            parsed,
            language_id=lang,
            language=support.grammar(),  # type: ignore[attr-defined]
            query_source=load_query_source(lang),
            workspace_id=ws_id,
            rel=rel,
        )
        records = [e.record for e in extraction.symbols]
        extras = {str(e.record.urn): SymbolExtra(body_sha256=e.body_sha256) for e in extraction.symbols}
        probes: set[str] = set()
        relations, name_calls = self._relations(parsed, lang, support, records, rel, probes)
        errors: list[SyntaxErrorFact] = [
            (r.start.line, r.start.col, r.end.line, r.end.col, _SYNTAX_ERROR_MESSAGE)
            for r in parsed.error_ranges
        ]
        return self._base_facts(
            rel,
            loaded,
            data,
            detected,
            confidence=Confidence.STRUCTURAL,
            symbols=records,
            extras=extras,
            relations=relations,
            syntax_errors=errors,
            import_stems=sorted(probes),
            name_calls=name_calls,
        )

    def _relations(
        self,
        parsed: object,
        lang: str,
        support: object,
        records: list[SymbolRecord],
        rel: RelPath,
        probes: set[str],
    ) -> tuple[list[RelationFact], list[NameCallFact]]:
        """関係の候補 → ``(RelationFact, NameCallFact)``。目標は 同じファイル内 → import 表 → 外部（未解決の import・組み込み名）の順。

        解決できない候補は ``relations`` に入れない。そのうち受け手の型が分からない属性呼び出し（``name_only``）だけは、位置と名前を
        ``NameCallFact``（内部表 ``_name_calls``）にする（T-06 / T-07 が名前で結ぶ。BUG-0071）。
        """
        ws_id = self._ws.workspace_id
        external: dict[str, str] = {}
        # def / class の中の import 文（開始位置ごと）
        nested: dict[relations.ImportKey, list[ImportFact]] = {}
        try:
            facts = extract_imports(parsed, language_id=lang)  # type: ignore[arg-type]
            resolver = make_resolver(
                from_rel=rel, language_id=lang, exists=reresolve.ProbeRecorder(self._exists, probes)
            )
            module_facts = [f for f in facts if not f.nested]
            for f in facts:
                if f.nested:
                    nested.setdefault((f.range.start.line, f.range.start.col), []).append(f)
            name_map = imported_name_map(module_facts, resolver)
            external = external_name_map(module_facts, language_id=lang, internal=name_map)
        except Exception:
            _log.warning("import resolution failed for %s", rel, exc_info=True)
            name_map = {}

        def urn_of(names: dict[str, tuple[RelPath, str]], externals: dict[str, str], name: str) -> str | None:
            hit = names.get(name)
            if hit is None:
                return externals.get(name)
            path, original = hit
            try:
                if original == "*":  # モジュールそのもの
                    return str(Urn(ws_id, str(path)))
                if original == "default":
                    return None
                return str(Urn(ws_id, str(path), CodeSel(original)))
            except ValueError:
                return None

        def resolve_name(name: str) -> str | None:
            return urn_of(name_map, external, name)

        scoped: dict[relations.ImportKey, tuple[dict[str, tuple[RelPath, str]], dict[str, str]]] = {}

        def resolve_import(key: relations.ImportKey, name: str) -> str | None:
            """def / class の中の import 文（開始位置 ``key``）が束ねた ``name``。同じスコープの別の import や、モジュール直下の同名は引かない。"""
            if key not in scoped:
                group = nested.get(key, [])
                try:
                    names = imported_name_map(group, resolver)
                    scoped[key] = (names, external_name_map(group, language_id=lang, internal=names))
                except Exception:
                    _log.warning("scoped import resolution failed for %s", rel, exc_info=True)
                    scoped[key] = ({}, {})
            return urn_of(*scoped[key], name)

        candidates = relations.extract_relations(
            parsed,  # type: ignore[arg-type]
            language_id=lang,
            language=support.grammar(),  # type: ignore[attr-defined]
            symbols=records,
            workspace_id=ws_id,
            rel=rel,
            resolve_name=resolve_name,
            builtins_package=BUILTINS_PACKAGE if lang == "python" else GLOBALS_PACKAGE,
            resolve_import=resolve_import,
        )
        rel_facts = [
            RelationFact(
                source_urn=c.source_urn,
                target_urn=c.target_urn,
                relation_kind=c.relation_kind,
                call_site_line=c.call_site_line,
                call_site_col=c.call_site_col,
                use_kind=c.use_kind,
                guard_condition=c.guard_condition,
                confidence=c.confidence,
            )
            for c in candidates
            if c.target_urn is not None
        ]
        name_calls = [
            NameCallFact(
                source_urn=c.source_urn,
                name=c.target_name,
                call_site_line=c.call_site_line,
                call_site_col=c.call_site_col,
                guard_condition=c.guard_condition,
            )
            for c in candidates
            if c.target_urn is None and c.name_only
        ]
        return rel_facts, name_calls

    def _exists(self, rel: RelPath) -> bool:
        return (self._ws.root / str(rel)).is_file() and not is_excluded(self._ws, rel)

    def _analyze_adapter(self, rel: RelPath, loaded: _Loaded, data: bytes, detected: Detected) -> FileFacts:
        """Tier 3 と ``.ipynb``: ``files`` 行と、データ形式アダプタのシンボル（最上位の節。無ければ ``files`` 行だけ）。

        json / yaml / toml / xml は文法つきの登録（``get_support``）が先に返るので、アダプタは ``get_adapter`` で引く。
        """
        adapter = get_adapter(detected.language_id)
        records: list[SymbolRecord] = []
        if adapter is not None:
            records = adapter.symbols(data, rel=str(rel), workspace_id=self._ws.workspace_id)
        return self._base_facts(
            rel,
            loaded,
            data,
            detected,
            confidence=Confidence.STRUCTURAL if records else Confidence.LEXICAL_FALLBACK,
            symbols=records,
        )

    async def _analyze_outline(
        self, rel: RelPath, loaded: _Loaded, data: bytes, detected: Detected
    ) -> FileFacts:
        """Tier 4（と、タグクエリの無い Tier 1〜2）: ctags、無ければブロック走査。"""
        ctags = await self._ctags_cmd()
        records, confidence = await heuristic.outline(
            data, rel=rel, workspace_id=self._ws.workspace_id, ctags_cmd=ctags
        )
        return await asyncio.to_thread(
            self._base_facts, rel, loaded, data, detected, confidence=confidence, symbols=records
        )

    async def _ctags_cmd(self) -> list[str] | None:
        if self._ctags_probed:
            return self._ctags
        async with self._ctags_lock:
            if not self._ctags_probed:
                try:
                    self._ctags = await heuristic.find_ctags()
                except Exception:
                    _log.warning("ctags probe failed", exc_info=True)
                    self._ctags = None
                self._ctags_probed = True
        return self._ctags

    # --- 書き込み ---------------------------------------------------------------------

    async def _submit_batch(self, batch: list[FileFacts]) -> list[IngestStats]:
        """バッチを 1 トランザクションで取り込む。失敗したら 1 ファイルずつやり直す（失敗した分は例外にまとめる）。"""
        self.ingest_calls += len(batch)

        def job(conn: object) -> list[IngestStats]:
            return [ingest_file(conn, f) for f in batch]  # type: ignore[arg-type]

        label = f"ingest_batch:{batch[0].path}+{len(batch) - 1}"
        try:
            return await self._writer.asubmit(job, bumps_version=True, label=label)
        except Exception:
            if len(batch) == 1:
                raise
            _log.warning("batch ingest failed; retrying file by file", exc_info=True)
        stats: list[IngestStats] = []
        failures: list[tuple[str, str]] = []
        for facts in batch:
            try:
                stats.extend(
                    await self._writer.asubmit(
                        lambda conn, f=facts: [ingest_file(conn, f)],  # type: ignore[arg-type]
                        bumps_version=True,
                        label=f"ingest:{facts.path}",
                    )
                )
            except Exception as exc:
                failures.append((str(facts.path), f"{type(exc).__name__}: {exc}"))
        if failures:
            raise _BatchFailure(stats, failures)
        return stats

    async def _collect(
        self, pending: asyncio.Future[list[IngestStats]], report: ScanReport
    ) -> list[IngestStats]:
        try:
            stats = await pending
        except _BatchFailure as exc:
            report.errors.extend(exc.failures)
            stats = exc.stats
        except Exception as exc:
            _log.warning("scan: batch failed", exc_info=True)
            report.errors.append(("<batch>", f"{type(exc).__name__}: {exc}"))
            return []
        report.indexed += len(stats)
        report.created += sum(1 for s in stats if s.file_created)
        return stats

    async def _remove_batch(self, paths: list[RelPath], report: ScanReport) -> None:
        ws_id = self._ws.workspace_id
        for p in paths:
            self._trees.evict(ws_id, p)

        def job(conn: object) -> int:
            return sum(1 for p in paths if remove_file(conn, ws_id, str(p)))  # type: ignore[arg-type]

        try:
            report.removed += await self._writer.asubmit(
                job, bumps_version=True, label=f"remove_batch:{paths[0]}"
            )
            report.transactions += 1
        except Exception as exc:
            _log.warning("scan: remove batch failed", exc_info=True)
            report.errors.append(("<remove>", f"{type(exc).__name__}: {exc}"))


class _BatchFailure(Exception):
    def __init__(self, stats: list[IngestStats], failures: list[tuple[str, str]]) -> None:
        super().__init__(f"{len(failures)} file(s) failed")
        self.stats = stats
        self.failures = failures


# --- 補助 -------------------------------------------------------------------------------


def _chunks[T](items: Sequence[T], size: int) -> Iterable[list[T]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def _safe_ignored(ignore: object, path: str) -> bool:
    try:
        return bool(ignore.is_ignored(path))  # type: ignore[attr-defined]
    except Exception:
        return False


def _settle_relations(conn: object, file_ids: Sequence[int]) -> None:
    """取り込んだファイルの ``relations`` を整える（書き手スレッドのトランザクションの中で呼ぶ）。

    * ``symbols`` に無いシンボルを指す目標（``#`` を含む URN）の行を消す。import 表から作った URN は定義元の存在を
      確かめていないため。外部シンボル（``lk://_ext/``）は ``ingest_file`` が ``symbols`` に行を作るので残る。
    * ``target_file_id`` を目標のシンボルの持ち主で埋める（目標のファイルが後から取り込まれた場合に備える）。
    * 消す前に、受け手のクラスに無いメンバー（``self.helper()`` の ``Sub.helper``）を指す行を ``_member_refs`` に退避し、最後に
      基底クラスを C3 の順にたどって結ぶ。整えるファイルのクラスの派生クラスを受け手にする退避行も結び直す（BUG-0075。``index/inherited.py``）。
    """
    for file_id in file_ids:
        _redirect_overloads(conn, file_id)
        inherited.park_member_refs(conn, file_id)  # type: ignore[arg-type]
        conn.execute(  # type: ignore[attr-defined]
            "DELETE FROM relations WHERE source_file_id = ? AND instr(target_urn, '#') > 0"
            " AND target_urn NOT IN (SELECT urn FROM symbols)",
            (file_id,),
        )
        conn.execute(  # type: ignore[attr-defined]
            "UPDATE relations SET target_file_id = (SELECT file_id FROM symbols WHERE urn = relations.target_urn)"
            " WHERE source_file_id = ? AND target_file_id IS NULL",
            (file_id,),
        )
    inherited.resolve_member_refs(conn, file_ids)  # type: ignore[arg-type]


def _redirect_overloads(conn: object, file_id: int) -> None:
    """``symbols`` に無い目標のうち、``arity`` / ``sighash`` なしの URN を、同じ修飾名の最後の宣言の URN に直す。

    オーバーロード（TS の ``charge(req)`` / ``charge(req, retries)`` と実装、Python の ``@overload``）の URN は
    ``Class.charge/1`` ``Class.charge/2~2797d533`` のように arity と sighash がつく。構文だけの抽出器は呼び出しの先を
    ``Class.charge`` までしか決められないので、ここで実在する URN に結ぶ。同じ修飾名が複数あるときは、ファイルの中で最後のもの
    （オーバーロード宣言は実装シグネチャの前に並ぶ）。
    """
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT DISTINCT target_urn FROM relations WHERE source_file_id = ? AND instr(target_urn, '#') > 0"
        " AND target_urn NOT IN (SELECT urn FROM symbols)",
        (file_id,),
    ).fetchall()
    for (target,) in rows:
        try:
            urn = parse(target)
        except ValueError:
            continue
        sel = urn.selector
        if (
            urn.is_external
            or not isinstance(sel, CodeSel)
            or sel.arity is not None
            or sel.sighash is not None
        ):
            continue
        hit = conn.execute(  # type: ignore[attr-defined]
            "SELECT s.urn FROM symbols s JOIN files f ON f.file_id = s.file_id"
            " WHERE f.workspace_id = ? AND f.path = ? AND s.qualified_name = ?"
            " ORDER BY s.start_line DESC, s.urn DESC LIMIT 1",
            (urn.workspace_id, urn.path, sel.qualified),
        ).fetchone()
        if hit is not None:
            conn.execute(  # type: ignore[attr-defined]
                "UPDATE relations SET target_urn = ? WHERE source_file_id = ? AND target_urn = ?",
                (hit[0], file_id, target),
            )
