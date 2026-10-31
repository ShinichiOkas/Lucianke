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
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal, cast

from lucianke.core.foreground import FOREGROUND, background
from lucianke.core.limits import MAX_FILE_BYTES
from lucianke.core.model import Confidence, RelPath, SymbolRecord, Tier
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import CodeSel, Urn, parse
from lucianke.engine import reresolve
from lucianke.engine.analysis_pool import POOL_MIN_FILES, AnalysisPool, default_workers
from lucianke.engine.rank_scheduler import RANK_DEBOUNCE_S, RankScheduler
from lucianke.index import inherited, queries, rank, reexports
from lucianke.index.connection import ReaderPool
from lucianke.index.ingest import (
    AttrDefFact,
    FileFacts,
    IngestStats,
    NameCallFact,
    ReexportFact,
    RelationFact,
    SuperRefFact,
    SymbolExtra,
    SyntaxErrorFact,
    TypeAliasFact,
    VarTypeFact,
    ingest_file,
    remove_file,
    upsert_workspace,
)
from lucianke.index.writer import IndexWriter
from lucianke.syntax import heuristic, relations
from lucianke.syntax.builtin_names import BUILTINS_PACKAGE, GLOBALS_PACKAGE, PYTHON_BUILTINS
from lucianke.syntax.imports import (
    NAMESPACE_PACKAGE,
    ImportFact,
    dotted_module_path,
    external_name_map,
    extract_imports,
    imported_name_map,
    js_local_exports,
    js_module_exports,
    make_resolver,
    module_all_dynamic,
    module_all_names,
    namespace_name_map,
    wildcard_sources,
)
from lucianke.syntax.parsers import TreeCache
from lucianke.syntax.support import get_adapter, get_support
from lucianke.syntax.symbols import extract_detailed, load_query_source
from lucianke.workspace.languages import Detected, detect, is_test
from lucianke.workspace.paths import confine, is_excluded
from lucianke.workspace.pyenv import PytestImports, detect_pytest_imports
from lucianke.workspace.types import Workspace

__all__ = [
    "INGEST_BATCH_FILES",
    "SETTLE_BATCH_FILES",
    "Indexer",
    "JobState",
    "JobStatus",
    "ScanProgress",
    "ScanReport",
]

_log = logging.getLogger(__name__)

INGEST_BATCH_FILES: Final = 50  # 設計書 §7.6.1 項 4: 起動時の差分索引は 1 トランザクションに最大 50 ファイル
SETTLE_BATCH_FILES: Final = 50
"""走査の後の関係の整えを、この数のファイルずつの短い書き込みのジョブに割る（BUG-0112。1 ジョブで全ファイルを整えると、
大きなワークスペースで書き手を長くふさぎ、その間の先取り・編集の書き込みが待たされる）。"""
_CHUNK: Final = 1024 * 1024
_HEAD_BYTES: Final = 8 * 1024
_SYNTAX_ERROR_MESSAGE: Final = "syntax error"
_JS_LANGUAGES: Final = frozenset({"typescript", "tsx", "javascript", "jsx"})  # バレルの公開する名前を書く言語（BUG-0104）
MINIFIED_SUFFIXES: Final = (".min.js", ".min.mjs", ".min.cjs")
"""この名前の JS は圧縮済みとみなし、構文木を作らず ``files`` 行だけにする（BUG-0119）。"""
MINIFIED_MIN_BYTES: Final = 32 * 1024
MINIFIED_AVG_LINE_BYTES: Final = 300
"""名前で分からない JS / TS でも、``MINIFIED_MIN_BYTES`` 以上で 1 行の平均がこのバイト数以上なら圧縮済み・束ねた生成物とみなす
（手で書いたコードの行の平均は 30〜50 バイト）。"""

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
    ``phase`` は全ファイルを取り込んだ後の段（``settling relations 3/170`` など。BUG-0112）。ファイルの取り込みの間は ``None``。
    """

    files_done: int
    files_total: int | None
    phase: str | None = None

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
    phase: str | None = None  # 走っている走査の、ファイルの取り込みの後の段（``ScanProgress.phase``。BUG-0112）


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

    def snapshot(self, phase: str | None = None) -> JobStatus:
        return JobStatus(
            self.job_id,
            self.kind,
            self.state,
            self.progress,
            self.started_at,
            self.finished_at,
            self.error,
            self.report,
            phase if self.state == "running" else None,
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


@dataclass(frozen=True, slots=True)
class _Extracted:
    """``Indexer._relations`` の結果（``FileFacts`` の関係の列と内部表の行）。"""

    relations: list[RelationFact]
    name_calls: list[NameCallFact]
    super_refs: list[SuperRefFact]
    attr_defs: list[AttrDefFact]
    var_types: list[VarTypeFact]
    reexports: list[ReexportFact] = field(default_factory=list)
    type_aliases: list[TypeAliasFact] = field(default_factory=list)


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
        analysis_workers: 走査の解析に使う子プロセスの数（0 は同じプロセスで解析する）。``None`` は ``default_workers()``
            （``LUCIANKE_INDEX_WORKERS``、無ければ論理 CPU の半分。BUG-0127。engine/analysis_pool.py）。
        pool_min_files: 走査で解析の要るファイルがこの数を超えたら、残りを子プロセスに渡す（小さな差分の走査では起こさない）。
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
        analysis_workers: int | None = None,
        pool_min_files: int = POOL_MIN_FILES,
    ) -> None:
        self._ws = ws
        self._writer = writer
        self._readers = reader_pool
        self._trees = tree_cache
        self._max = max_file_bytes
        self._workers = default_workers() if analysis_workers is None else max(0, analysis_workers)
        self._pool_min_files = pool_min_files
        self.pooled_files = 0  # 最後の走査で子プロセスが解析したファイル数（観測・試験用）
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
        self._scan_phase: str | None = None  # ファイルの取り込みの後の段（``ScanProgress.phase``）
        self._ahead: set[str] = set()  # 先取りしたが、まだ走査が取り込み直して整えていないパス
        self._scan_ingested: set[str] = set()  # 最後に終わった走査が取り込んだパス
        # プロジェクトのディレクトリ（ワークスペースのルート ``""``・テストフィクスチャのルート）→ pytest の設定のうち import の根に
        # 効くもの（BUG-0082）。設定ファイルの取り込み・削除（engine/reresolve.py）と走査の始まりで捨てる（``forget_pytest_imports``）
        self._pytest_imports: dict[str, PytestImports] = {}

    @classmethod
    def for_analysis(cls, ws: Workspace, *, max_file_bytes: int = MAX_FILE_BYTES) -> Indexer:
        """解析だけをする索引器（走査の子プロセスの中で使う。書き手・読み手を持たないので ``_analyze_sync`` 以外は呼ばない）。"""
        return cls(
            ws,
            cast("IndexWriter", None),
            cast("ReaderPool", None),
            TreeCache(max_files=_WORKER_TREE_CACHE_FILES),
            max_file_bytes=max_file_bytes,
            analysis_workers=0,
        )

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

    def pytest_imports(self, project: str) -> PytestImports:
        """ディレクトリ ``project``（ワークスペース相対）の pytest の設定のうち import の根に効くもの（覚えて使い回す。BUG-0082）。"""
        found = self._pytest_imports.get(project)
        if found is None:
            found = self._pytest_imports[project] = detect_pytest_imports(self._ws.root, project)
        return found

    def forget_pytest_imports(self, projects: Iterable[str] | None = None) -> None:
        """覚えた pytest の設定を捨てる（``None`` はすべて）。次に使うときディスクから読み直す。"""
        if projects is None:
            self._pytest_imports.clear()
        else:
            for project in projects:
                self._pytest_imports.pop(project, None)

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
            return ScanProgress(self._scan_done, self._scan_total, self._scan_phase)
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
        # 消えたファイルを import していた側・そのファイルの名前をたどって整えた側（BUG-0100）の関係を同じトランザクションで解き直す（BUG-0025）
        importers = await reresolve.prepare_importers(
            self, [path], export_changes={path: {reexports.ALL_NAMES}}
        )

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
            self._scan_done, self._scan_total, self._scan_phase = 0, None, None
        try:
            # 走査は背景の仕事: 区切りごとに、前景（ツールの呼び出し）に譲る（core/foreground.py。BUG-0112）
            with background():
                report = await self._scan(progress_cb)
                if self._scans == 1:  # 最後の走査（重なっていない）。整え終わるまで ``building()`` は進みを返し続ける
                    if self._ahead:
                        self._scan_phase = "settling files ingested ahead"
                    await self._settle_ahead()
            return report
        finally:
            self._scans -= 1
            if not self._scans:
                self._scan_phase = None

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
        self.forget_pytest_imports()  # この走査で解析するファイルは、いまの pytest の設定で import を解く（BUG-0082）
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
        # 既存で「公開する名前」が変わったファイル → 変わった名前（その名前をたどって整えた側を解き直す。BUG-0100）
        exports_changed: dict[str, frozenset[str]] = {}

        async def flush() -> None:
            nonlocal pending, batch
            if pending is not None:
                for s in await self._collect(pending, report):
                    file_ids.append(s.file_id)
                    ingested.add(s.path)
                    if s.symbol_set_changed and not s.file_created:
                        names_changed.append(s.path)
                    if s.export_names_changed:
                        exports_changed[s.path] = s.export_names_changed
                pending = None
            if batch:
                pending = asyncio.ensure_future(self._submit_batch(batch))
                report.transactions += 1
                batch = []

        # 解析の要るファイルが ``pool_min_files`` を超えたら、残りは子プロセスで解析する（BUG-0127。engine/analysis_pool.py）。
        # 結果は列挙の順に受け取って取り込む（file_id の振り方・バッチの切れ目は同じプロセスで解析したときと同じ）
        pool = (
            AnalysisPool(self._workers, _analysis_worker_init, (self._ws, self._max))
            if self._workers
            else None
        )
        in_flight: deque[tuple[RelPath, asyncio.Future[_Prepared | None | Literal["gone"]]]] = deque()
        analysed = done = 0
        self.pooled_files = 0

        def pool_for_next() -> AnalysisPool | None:
            nonlocal analysed
            analysed += 1
            if pool is None or pool.broken or analysed <= self._pool_min_files:
                return None
            return pool

        async def take() -> None:
            nonlocal done
            rel, future = in_flight.popleft()
            try:
                prepared = await future
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
            done += 1
            progress(5 + (done * 90) // total)
            self._scan_done = done

        try:
            for rel in listing:
                # 前景のツールの呼び出しの間は、次のファイルの解析を始めない（BUG-0112）
                await FOREGROUND.apause()
                in_flight.append((rel, await self._start_prepare(rel, known.get(rel), pool_for_next)))
                limit = pool.window if pool is not None and pool.submitted else 0
                while len(in_flight) > limit:
                    await take()
            while in_flight:
                await take()
        finally:
            for _, future in in_flight:  # 取り消された走査: 渡したままの解析を捨てる
                future.cancel()
            if in_flight:
                await asyncio.gather(*(f for _, f in in_flight), return_exceptions=True)
            if pool is not None:
                self.pooled_files = pool.submitted
                await pool.aclose()
        await flush()
        await flush()  # 最後に積んだバッチの完了を待つ
        self._scan_ingested |= ingested
        report.unchanged -= len(report.errors)  # 失敗は「変わらなかった」に数えない
        if file_ids:
            await self._settle_in_batches(file_ids, lambda done, total: progress(95 + (done * 3) // max(total, 1)))
        if known:  # 初回の全体走査は、取り込む側が最後のファイルシステムを見て解決済み。差分のときだけ、取り込まなかった側を解き直す
            self._scan_phase = "re-resolving importers"
            created = [r for r in listing if r not in known]
            await reresolve.after_scan(
                self,
                created=created,
                removed=gone,
                skip_file_ids=set(file_ids),
                changed=names_changed,
                export_changes=exports_changed,
                configs=sorted(p for p in ingested if reresolve.is_pytest_config(p)),
            )
        if report.indexed or report.removed or not await asyncio.to_thread(self._ranks_ready):
            progress(99)
            self._scan_phase = "ranking symbols"
            await FOREGROUND.apause()
            await self.refresh_ranks_quietly()  # 全体の取り込みの後に 1 回（index/rank.py。T-01 の順位）
        progress(100)
        report.duration_s = time.monotonic() - t0
        return report

    async def _settle_in_batches(self, file_ids: Sequence[int], on_step: Callable[[int, int], None]) -> None:
        """走査で取り込んだファイルの関係を整える（``_settle_relations`` と同じ結果）。``SETTLE_BATCH_FILES`` ずつの短いジョブに割り、
        ジョブの間で前景に譲る（BUG-0112）。

        段は 2 つ: (1) ファイルごとの整え（再び公開した名前・オーバーロード・継承したメンバーの退避・無い目標の削除・``target_file_id``）
        をすべてのバッチで、(2) 退避した行を基底から結ぶ（``inherited.resolve_member_refs``）をすべてのバッチで。(2) は (1) が全ファイルを
        終えてから（別のファイルの ``INHERITS`` 行の書き換えを見てから結ぶ）。(2) をバッチに割っても結ぶ行の和集合は同じで（各バッチの
        ファイルの退避行と、そのクラス・派生クラス・変数を受け手にする退避行）、2 回結ばれる行は同じ答えになる。
        """
        chunks = list(_chunks(list(file_ids), SETTLE_BATCH_FILES))
        steps: list[tuple[str, Callable[[object, Sequence[int]], object], list[int]]] = [
            ("settle_relations", _settle_files, chunk) for chunk in chunks
        ] + [("settle_member_refs", inherited.resolve_member_refs, chunk) for chunk in chunks]  # type: ignore[misc]
        total = len(steps)
        for step, (label, fn, chunk) in enumerate(steps):
            self._scan_phase = f"settling relations {step}/{total}"
            on_step(step, total)
            await FOREGROUND.apause()
            await self._writer.asubmit(
                lambda conn, fn=fn, chunk=chunk: fn(conn, chunk),  # type: ignore[misc]
                bumps_version=False,
                label=label,
            )
        self._scan_phase = f"settling relations {total}/{total}"
        on_step(total, total)

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
        return None if job is None else job.snapshot(self._scan_phase)

    def jobs(self) -> list[JobStatus]:
        """全ジョブの状態（新しい順）。"""
        return [j.snapshot(self._scan_phase) for j in reversed(self._jobs.values())]

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
        """``rel``（ファイル）が索引の対象外か。上位ディレクトリの除外・無視・入れ子の Git の作業ツリー（BUG-0118）も含める。"""
        parts = str(rel).split("/")
        if ".git" in parts or is_excluded(self._ws, rel):
            return True
        if len(parts) > 1:
            submodules = _submodule_paths(self._ws.root)
            for k in range(1, len(parts)):
                if _nested_repo(self._ws.root, "/".join(parts[:k]), submodules):
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
        """ワークスペースのファイルを列挙する（昇順）。シンボリックリンクと ``.git`` は辿らない。

        入れ子の Git の作業ツリー・リポジトリ（``.git`` のファイルかディレクトリを持つ根以外のディレクトリ。``.claude/worktrees/*``
        など）には降りない（BUG-0118。Git もそれを親の作業ツリーの一部として扱わない）。根の ``.gitmodules`` が挙げるサブモジュールは
        今までどおり辿る（プロジェクトのソースとして取り込んでいたもの）。
        """
        ignore = self._make_ignore()
        submodules = _submodule_paths(self._ws.root)
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
                    if _nested_repo(self._ws.root, rel, submodules):
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
        return await self._analyze_loaded(rel, loaded)

    async def _start_prepare(
        self, rel: RelPath, known_sha: str | None, pool_for_next: Callable[[], AnalysisPool | None]
    ) -> asyncio.Future[_Prepared | None | Literal["gone"]]:
        """走査の 1 ファイル（``_prepare`` と同じ結果）。読み込みはここで済ませ、解析は ``pool_for_next()`` が子プロセスの組を返せば
        そこへ渡す（返す Future は子の結果を待つ）。そうでなければ同じプロセスで解析し終えた Future を返す。失敗も Future に載せる。"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_Prepared | None | Literal["gone"]] = loop.create_future()
        try:
            loaded = await asyncio.to_thread(self._load, rel, known_sha)
            if not isinstance(loaded, _Loaded):
                future.set_result(None if loaded == "unchanged" else "gone")
                return future
            pool = pool_for_next()
            remote = pool.submit(_analysis_worker_run, rel, loaded) if pool is not None else None
            if pool is not None and remote is not None:
                return asyncio.ensure_future(self._finish_remote(rel, loaded, remote, pool))
            future.set_result(await self._analyze_loaded(rel, loaded))
        except Exception as exc:
            future.set_exception(exc)
        return future

    async def _finish_remote(
        self, rel: RelPath, loaded: _Loaded, remote: asyncio.Future[_Prepared | None], pool: AnalysisPool
    ) -> _Prepared:
        """子プロセスの解析の結果を待つ。子が落ちた・結果を渡せなかったときは、そのファイルを同じプロセスで解析し直す（結果は同じ）。"""
        try:
            prepared = await remote
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            pool.failed(exc)
            _log.warning("analysis worker failed for %s; analysing it in-process", rel, exc_info=True)
            return await self._analyze_loaded(rel, loaded)
        return prepared if prepared is not None else await self._outline(rel, loaded)

    async def _analyze_loaded(self, rel: RelPath, loaded: _Loaded) -> _Prepared:
        """読み込んだファイルを同じプロセスで解析する（構文解析はスレッドで。ctags の外形はイベントループで）。"""
        prepared = await asyncio.to_thread(self._analyze_sync, rel, loaded)
        return prepared if prepared is not None else await self._outline(rel, loaded)

    def _analyze_sync(self, rel: RelPath, loaded: _Loaded) -> _Prepared | None:
        """読み込んだファイルの ``FileFacts``（スレッド・走査の子プロセスで走る。DB に触れない）。

        ``None`` は ctags の外形が要る（Tier 4 と、タグクエリの無い Tier 1〜2。子プロセスを起こすので ``_outline`` が
        イベントループで作る）。解析に失敗したら ``files`` 行だけ（``degraded`` に理由）。
        """
        detected = detect(rel, loaded.head)
        # 秘密・バイナリ・大きすぎるファイルは files 行だけ（仕様書 §12.4、§13.5）
        if not detected.index_content or loaded.data is None:
            return self._files_only(rel, loaded, detected)
        if _is_minified(rel, detected.language_id, loaded.data):
            # 圧縮済みの JS（``*.min.js``・1 行 数 MB の束）は読む必要が無く、1〜2 文字の名前のシンボル・関係を数千作り、1 本に
            # 数十秒かかる。``files`` 行だけにする（行数・大きさ・``sha256`` は残す。BUG-0119）
            return self._files_only(rel, loaded, detected)
        data = loaded.data
        lang = detected.language_id
        try:
            if detected.tier <= Tier.T2 and _grammar_support(lang) is not None and _has_symbol_queries(lang):
                return _Prepared(self._analyze_code(rel, loaded, data, detected))
            if detected.tier == Tier.T3 or get_adapter(lang) is not None:  # .ipynb（Tier 2）もアダプタの言語
                return _Prepared(self._analyze_adapter(rel, loaded, data, detected))
        except Exception as exc:
            _log.warning("analysis failed for %s (%s); indexing the file row only", rel, lang, exc_info=True)
            return self._files_only(rel, loaded, detected, degraded=f"{type(exc).__name__}: {exc}")
        return None

    async def _outline(self, rel: RelPath, loaded: _Loaded) -> _Prepared:
        """``_analyze_sync`` が ``None`` を返したファイルの外形（ctags、無ければブロック走査）。"""
        detected = detect(rel, loaded.head)
        assert loaded.data is not None
        try:
            return _Prepared(await self._analyze_outline(rel, loaded, loaded.data, detected))
        except Exception as exc:
            _log.warning(
                "analysis failed for %s (%s); indexing the file row only",
                rel,
                detected.language_id,
                exc_info=True,
            )
            return self._files_only(rel, loaded, detected, degraded=f"{type(exc).__name__}: {exc}")

    def _files_only(
        self, rel: RelPath, loaded: _Loaded, detected: Detected, *, degraded: str | None = None
    ) -> _Prepared:
        lines = 0
        if detected.index_content:
            lines = loaded.line_count if loaded.data is None else LineIndex(loaded.data).content_line_count
        return _Prepared(
            FileFacts(
                workspace_id=self._ws.workspace_id,
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
        super_refs: Sequence[SuperRefFact] = (),
        attr_defs: Sequence[AttrDefFact] = (),
        var_types: Sequence[VarTypeFact] = (),
        reexports: Sequence[ReexportFact] = (),
        type_aliases: Sequence[TypeAliasFact] = (),
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
            super_refs=tuple(super_refs),
            attr_defs=tuple(attr_defs),
            var_types=tuple(var_types),
            reexports=tuple(reexports),
            type_aliases=tuple(type_aliases),
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
        extracted = self._relations(parsed, lang, support, records, rel, probes)
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
            relations=extracted.relations,
            syntax_errors=errors,
            import_stems=sorted(probes),
            name_calls=extracted.name_calls,
            super_refs=extracted.super_refs,
            attr_defs=extracted.attr_defs,
            var_types=extracted.var_types,
            reexports=extracted.reexports,
            type_aliases=extracted.type_aliases,
        )

    def _relations(
        self,
        parsed: object,
        lang: str,
        support: object,
        records: list[SymbolRecord],
        rel: RelPath,
        probes: set[str],
    ) -> _Extracted:
        """関係の候補 → ``_Extracted``。目標は 同じファイル内 → import 表 → 外部（未解決の import・組み込み名）の順。

        解決できない候補は ``relations`` に入れない。そのうち受け手の型が分からない属性呼び出し・属性の読み書き（``name_only``）だけは、
        位置と名前と使用種別を ``NameCallFact``（内部表 ``_name_calls``）にする（T-06 / T-07 が名前で結ぶ。BUG-0071・BUG-0095）。
        ``super().name``（``super_of``）は ``SuperRefFact``（内部表 ``_member_refs`` の ``via_super = 1``）にし、整えが基底の C3 線形化から
        先を決める（BUG-0081）。インスタンス属性の定義（``_attr_defs``。BUG-0095）とモジュール直下の変数の型（``_var_types``。BUG-0094）も添える。
        点つきのモジュールのパス（``pkg.helpers``）は、import 表とファイルの在り処からサブモジュールに解く（BUG-0093。調べたパスは語幹に残す）。
        名前空間パッケージ（``__init__.py`` の無いディレクトリ）を束ねた名前とそのディレクトリは、参照にも外部にもしない（BUG-0101。
        調べたディレクトリは ``?dir:`` の語幹に残す）。Python はモジュール直下の import が束ねた名前・``from x import *``・``__all__`` を
        ``ReexportFact``（内部表 ``_reexports``）にし、整えが再び公開した名前を定義元までたどる（BUG-0100。``index/reexports.py``）。
        """
        ws_id = self._ws.workspace_id
        external: dict[str, str] = {}
        namespaces: dict[str, str] = {}
        module_facts: list[ImportFact] = []
        resolver: Callable[[ImportFact], RelPath | None] | None = None
        exports: list[ReexportFact] = []
        star_sources: list[RelPath] = []  # Python の ``from x import *`` のワークスペースの先（BUG-0105）
        # def / class の中の import 文（開始位置ごと）
        nested: dict[relations.ImportKey, list[ImportFact]] = {}
        try:
            facts = extract_imports(parsed, language_id=lang)  # type: ignore[arg-type]

            def pytest_imports(project: str) -> PytestImports:
                # 使った設定の指紋も語幹として残す（設定が変わったら、違う指紋で解いた側だけを解き直す。engine/reresolve.py）
                config = self.pytest_imports(project)
                probes.add(reresolve.config_stem(project, config))
                return config

            resolver = make_resolver(
                from_rel=rel,
                language_id=lang,
                exists=reresolve.ProbeRecorder(self._exists, probes),
                pytest_imports=pytest_imports,
                is_dir=reresolve.DirProbeRecorder(self._is_dir, probes),
            )
            module_facts = [f for f in facts if not f.nested]
            for f in facts:
                if f.nested:
                    nested.setdefault((f.range.start.line, f.range.start.col), []).append(f)
            name_map = imported_name_map(module_facts, resolver)
            external = external_name_map(module_facts, language_id=lang, internal=name_map)
            if lang == "python":
                namespaces = namespace_name_map(module_facts, resolver, internal=name_map)
                star_sources = wildcard_sources(module_facts, resolver)
                exports = self._exports(parsed, star_sources, name_map, external)
            elif lang in _JS_LANGUAGES:
                exports = self._js_exports(parsed, module_facts, resolver, name_map, external, records, rel)
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
                # ES の既定 import（``import main from './helpers'``）は ``helpers.ts#default``。整え（index/reexports.py）が既定の
                # export の束ね先（``export default function main`` → ``helpers.ts#main``）までたどる（BUG-0109）
                return str(Urn(ws_id, str(path), CodeSel(original)))
            except ValueError:
                return None

        def resolve_name(name: str) -> str | None:
            return urn_of(name_map, external, name)

        def resolve_star(name: str) -> str | None:
            """``from x import *`` を持つ Python のファイルで、どこでも決まらなかった名前 → ``<このファイル>#name``（BUG-0105）。
            整え（``index/reexports.py``）が ``*`` の先が公開する定義に書き換える。組み込み名・``__x__`` は引かない。"""
            if not star_sources or not name.isidentifier() or name in PYTHON_BUILTINS:
                return None
            if name.startswith("__") and name.endswith("__"):
                return None
            try:
                return str(Urn(ws_id, str(rel), CodeSel(name)))
            except ValueError:
                return None

        def resolve_dotted(dotted: str) -> str | None:
            if resolver is None:
                return None
            try:
                found = dotted_module_path(
                    dotted,
                    facts=module_facts,
                    names=name_map,
                    resolve=resolver,
                    exists=reresolve.ProbeRecorder(self._exists, probes),
                    namespaces=namespaces,
                )
                if found is None or found == NAMESPACE_PACKAGE:
                    return found
                return str(Urn(ws_id, str(found)))
            except Exception:
                _log.warning("dotted module resolution failed for %s in %s", dotted, rel, exc_info=True)
                return None

        scoped: dict[relations.ImportKey, tuple[dict[str, tuple[RelPath, str]], dict[str, str]]] = {}

        def resolve_import(key: relations.ImportKey, name: str) -> str | None:
            """def / class の中の import 文（開始位置 ``key``）が束ねた ``name``。同じスコープの別の import や、モジュール直下の同名は引かない。"""
            if key not in scoped:
                group = nested.get(key, [])
                try:
                    if resolver is None:
                        raise LookupError("import resolution failed for the module")
                    names = imported_name_map(group, resolver)
                    scoped[key] = (names, external_name_map(group, language_id=lang, internal=names))
                except Exception:
                    _log.warning("scoped import resolution failed for %s", rel, exc_info=True)
                    scoped[key] = ({}, {})
            return urn_of(*scoped[key], name)

        detailed = relations.extract_relations_detailed(
            parsed,  # type: ignore[arg-type]
            language_id=lang,
            language=support.grammar(),  # type: ignore[attr-defined]
            symbols=records,
            workspace_id=ws_id,
            rel=rel,
            resolve_name=resolve_name,
            builtins_package=BUILTINS_PACKAGE if lang == "python" else GLOBALS_PACKAGE,
            resolve_import=resolve_import,
            resolve_dotted=resolve_dotted,
            resolve_star=resolve_star if star_sources else None,
        )
        candidates = detailed.relations
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
                use_kind=c.use_kind,
            )
            for c in candidates
            if c.target_urn is None and c.name_only
        ]
        super_refs = [
            SuperRefFact(
                source_urn=c.source_urn,
                class_urn=c.super_of,
                name=c.target_name,
                relation_kind=c.relation_kind,
                use_kind=c.use_kind,
                call_site_line=c.call_site_line,
                call_site_col=c.call_site_col,
                guard_condition=c.guard_condition,
                confidence=c.confidence,
            )
            for c in candidates
            if c.target_urn is None and c.super_of is not None
        ]
        attr_defs = [
            AttrDefFact(class_urn=a.class_urn, name=a.name, line=a.line, col=a.col) for a in detailed.attr_defs
        ]
        var_types = [VarTypeFact(var_urn=v.var_urn, type_urn=v.type_urn) for v in detailed.var_types]
        type_aliases = [
            TypeAliasFact(alias_urn=a.alias_urn, op=a.op, ord=a.ord, kind=a.kind, target_urn=a.target_urn)
            for a in detailed.type_aliases
        ]
        return _Extracted(rel_facts, name_calls, super_refs, attr_defs, var_types, exports, type_aliases)

    def _exports(
        self,
        parsed: object,
        star_sources: Sequence[RelPath],
        name_map: dict[str, tuple[RelPath, str]],
        external: dict[str, str],
    ) -> list[ReexportFact]:
        """Python のモジュール直下の import が束ねた名前（他のファイルが ``from この import name`` で使える）・``from x import *`` の
        ワークスペースの先・``__all__`` の名前（BUG-0100。内部表 ``_reexports``）。束ねた先は ``urn_of`` と同じ URN（モジュールは ``#`` なし）。
        ``__all__`` がリテラルでなければ ``dynamic_all`` の印（公開する名前が決まらない。BUG-0105）。"""
        ws_id = self._ws.workspace_id
        out: list[ReexportFact] = []
        for name, (path, original) in name_map.items():
            if original == "default":
                continue
            try:
                urn = Urn(ws_id, str(path)) if original == "*" else Urn(ws_id, str(path), CodeSel(original))
            except ValueError:
                continue
            out.append(ReexportFact(name=name, target_urn=str(urn), kind="import", ord=len(out)))
        for name, urn_text in external.items():
            out.append(ReexportFact(name=name, target_urn=urn_text, kind="import", ord=len(out)))
        for source in star_sources:
            try:
                urn_text = str(Urn(ws_id, str(source)))
            except ValueError:
                continue
            out.append(ReexportFact(name="*", target_urn=urn_text, kind="star", ord=len(out)))
        for name in module_all_names(parsed) or ():  # type: ignore[arg-type]
            out.append(ReexportFact(name=name, target_urn="", kind="all", ord=len(out)))
        if module_all_dynamic(parsed):  # type: ignore[arg-type]
            out.append(ReexportFact(name="*", target_urn="", kind="dynamic_all", ord=len(out)))
        return out

    def _js_exports(
        self,
        parsed: object,
        module_facts: list[ImportFact],
        resolver: Callable[[ImportFact], RelPath | None],
        name_map: dict[str, tuple[RelPath, str]],
        external: dict[str, str],
        records: Sequence[SymbolRecord],
        rel: RelPath,
    ) -> list[ReexportFact]:
        """TS / JS のモジュールが公開する名前（BUG-0104。内部表 ``_reexports``。``syntax/imports.js_module_exports``）: 別の定義を
        公開する名前の束ね先（``import``。``export { a as b } from``・``export * as ns from``・import した名前の ``export { a }``・
        既定の export）、``export * from`` のワークスペースの先（``star``）、公開する名前（``export``。既定の export は入れない）。"""
        top_level = {r.qualified_name for r in records if "." not in r.qualified_name}
        return [
            ReexportFact(name=name, target_urn=target, kind=kind, ord=i)
            for i, (kind, name, target) in enumerate(
                js_module_exports(
                    module_facts,
                    resolver,
                    js_local_exports(parsed),  # type: ignore[arg-type]
                    workspace_id=self._ws.workspace_id,
                    rel=rel,
                    names=name_map,
                    external=external,
                    top_level=top_level,
                )
            )
        ]

    def _exists(self, rel: RelPath) -> bool:
        return (self._ws.root / str(rel)).is_file() and not is_excluded(self._ws, rel)

    def _is_dir(self, rel: RelPath) -> bool:
        return bool(str(rel)) and (self._ws.root / str(rel)).is_dir() and not is_excluded(self._ws, rel)

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


# --- 走査の子プロセス（BUG-0127。engine/analysis_pool.py） ------------------------------

_WORKER_TREE_CACHE_FILES: Final = 8
"""子プロセスの構文木のキャッシュ（解析した木は使い回さない。ツールの読み取りは親の ``TreeCache`` が受け持つ）。"""
_WORKER: Indexer | None = None


def _analysis_worker_init(ws: Workspace, max_file_bytes: int) -> None:
    """子プロセスの起動時に 1 回: 解析だけをする索引器を作る（pytest の設定の覚えは、走査ごとに起こす子の中で新しい）。"""
    global _WORKER
    _WORKER = Indexer.for_analysis(ws, max_file_bytes=max_file_bytes)


def _analysis_worker_run(rel: RelPath, loaded: _Loaded) -> _Prepared | None:
    """子プロセスで 1 ファイルを解析する（``Indexer._analyze_sync``。``None`` は親が ctags の外形を作る）。"""
    if _WORKER is None:
        raise RuntimeError("analysis worker is not initialised")
    return _WORKER._analyze_sync(rel, loaded)  # noqa: SLF001  同じモジュールの子の入口


# --- 補助 -------------------------------------------------------------------------------


def _chunks[T](items: Sequence[T], size: int) -> Iterable[list[T]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def _is_minified(rel: RelPath, language_id: str, data: bytes) -> bool:
    """圧縮済み・束ねた生成物の JS / TS か（``MINIFIED_SUFFIXES`` の名前、または大きくて 1 行の平均が長い。BUG-0119）。"""
    if language_id not in _JS_LANGUAGES:
        return False
    if str(rel).rsplit("/", 1)[-1].lower().endswith(MINIFIED_SUFFIXES):
        return True
    if len(data) < MINIFIED_MIN_BYTES:
        return False
    return len(data) // (data.count(b"\n") + 1) >= MINIFIED_AVG_LINE_BYTES


def _nested_repo(root: Path, rel_dir: str, submodules: frozenset[str]) -> bool:
    """ワークスペースの中のディレクトリ ``rel_dir`` が入れ子の Git の作業ツリー・リポジトリか（``.git`` を持つ。サブモジュールは除く）。"""
    return rel_dir not in submodules and os.path.lexists(os.path.join(root, rel_dir, ".git"))


def _submodule_paths(root: Path) -> frozenset[str]:
    """根の ``.gitmodules`` が挙げるサブモジュールのパス（``path = ...``。``/`` 区切り）。無い・読めなければ空。"""
    try:
        text = (root / ".gitmodules").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    out: set[str] = set()
    for line in text.splitlines():
        key, eq, value = line.strip().partition("=")
        if eq and key.strip() == "path" and value.strip():
            out.add(value.strip().replace("\\", "/").strip("/"))
    return frozenset(out)


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
    * いちばん先に、再び公開した名前（``from pkg import flush`` の ``pkg/__init__.py#flush``）を定義元までたどって目標と変数の型を
      書き換える（BUG-0100。``index/reexports.py``）。たどった先が継承したメンバーなら、続く退避で基底から結ばれる。
    """
    _settle_files(conn, file_ids)
    inherited.resolve_member_refs(conn, file_ids)  # type: ignore[arg-type]


def _settle_files(conn: object, file_ids: Sequence[int]) -> None:
    """``_settle_relations`` のうち、ファイルごとの段（退避した行を基底から結ぶ前まで）。"""
    exports = reexports.ReexportResolver(conn)  # type: ignore[arg-type]
    for file_id in file_ids:
        reexports.redirect_reexports(conn, file_id, exports)  # type: ignore[arg-type]
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
