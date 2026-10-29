"""索引の取り込み器（設計書 §7.6.2、§7.6.5、§6.1、§8.1。仕様書 §9.4、§12.4、§13.5）。

ワークスペースのファイルを走査し、解析・シンボル・関係を作って PaaD に取り込む。

* ``index_file(rel)``: 1 ファイルの再取り込み（読み込み → ``sha256`` → DB と同じなら何もしない → 言語判定 → 解析 →
  ``FileFacts`` → ``IndexWriter``）。ファイルが消えていた・除外対象になっていたら ``remove`` に回す。
* ``remove(rel)``: ``files`` から消す。
* ``scan()``: 起動時の差分索引と全体の再索引（同じ処理）。``.gitignore`` と ``exclude_globs`` を尊重して列挙し、
  DB にあって消えたファイルは消し、``sha256`` が変わった・新しいファイルだけ取り込む。1 トランザクションに最大
  ``INGEST_BATCH_FILES`` ファイルをまとめ、``data_version`` はトランザクションごとに 1 回だけ進む（設計書 §7.6.1 項 4）。
* ``start_scan_job()`` / ``job_status()``: 背景ジョブ（T-00 ``reindex``、``status`` の ``jobs[]``）。

並列度（設計書 §6.1、スパイク S-2）: 構文解析は GIL を握るので並列化しない。ファイル 1 つの解析はスレッド
（``asyncio.to_thread``）で走らせてイベントループを止めず、書き込みは ``IndexWriter`` の 1 本のキューに積む。
走査中は、1 バッチの書き込みを待たずに次のバッチの解析へ進む（書き込みは常に 1 バッチだけ先行させる）。

ティアごとの取り込み:

* Tier 1〜2（タグクエリのある言語）: 構文木 → ``symbols`` → ``relations``（``structural``）→ 構文エラーの診断。
* Tier 3: ``files`` 行。アダプタが ``symbols(source, *, rel, workspace_id)`` を持てばそのシンボルも入れる。
* Tier 4（と、タグクエリの無い Tier 1〜2）: ``heuristic.outline``（ctags、無ければブロック走査）。
* 秘密ファイル・バイナリ・``max_file_bytes`` 超過: ``files`` 行だけ（``lexical_fallback``。本文は解析しない）。

関係の ``target_urn``: 同じファイル内の定義 → import 表（``imported_name_map``）→ 解決できなければ捨てる。
import 表の解決は定義元の存在を確かめずに URN を作る（走査の順序に依存させないため）ので、取り込みの後に、
``symbols`` に無い目標を指す行を消し、``target_file_id`` を埋める（``_settle_relations``）。
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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from lucianke.core.limits import MAX_FILE_BYTES
from lucianke.core.model import Confidence, RelPath, SymbolRecord, Tier
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import CodeSel, Urn
from lucianke.index import queries
from lucianke.index.connection import ReaderPool
from lucianke.index.ingest import (
    FileFacts,
    IngestStats,
    RelationFact,
    SymbolExtra,
    SyntaxErrorFact,
    ingest_file,
    remove_file,
    upsert_workspace,
)
from lucianke.index.writer import IndexWriter
from lucianke.syntax import heuristic
from lucianke.syntax.imports import extract_imports, imported_name_map, make_resolver
from lucianke.syntax.parsers import TreeCache
from lucianke.syntax.support import get_support
from lucianke.syntax.symbols import extract_detailed, load_query_source
from lucianke.workspace.languages import Detected, detect, is_test
from lucianke.workspace.paths import confine, is_excluded
from lucianke.workspace.types import Workspace

try:  # 関係の抽出は別の役が作る。無ければ relations は空で進める
    from lucianke.syntax import relations as _relations_mod
except ImportError:  # pragma: no cover - 取り込み前の暫定
    _relations_mod = None  # type: ignore[assignment]

__all__ = [
    "INGEST_BATCH_FILES",
    "Indexer",
    "JobState",
    "JobStatus",
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

    @property
    def workspace(self) -> Workspace:
        return self._ws

    # --- 公開 API ---------------------------------------------------------------------

    async def index_file(self, rel: str | RelPath) -> IngestStats | None:
        """1 ファイルを再取り込みする。``sha256`` が DB と同じ・読めない・対象外のときは ``None``。

        ファイルが無くなっていた、または ``exclude_globs`` / ``.gitignore`` の対象になっていたら ``remove`` する。
        """
        path = await asyncio.to_thread(confine, self._ws, str(rel))
        if str(path) == ".":
            return None
        ignore = await asyncio.to_thread(self._make_ignore)
        if self._is_skipped(path, ignore):
            await self.remove(path)
            return None
        known = await asyncio.to_thread(self._known_sha, path)
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

        def job(conn: object) -> IngestStats:
            stats = ingest_file(conn, facts)  # type: ignore[arg-type]
            _settle_relations(conn, [stats.file_id])  # type: ignore[arg-type]
            return stats

        return await self._writer.asubmit(job, bumps_version=True, label=f"ingest:{path}")

    async def remove(self, rel: str | RelPath) -> bool:
        """``rel`` を PaaD から消す。消したら ``True``、元から無ければ ``False``。"""
        path = str(rel)
        await self._ensure_workspace()
        self._trees.evict(self._ws.workspace_id, RelPath(path))
        ws_id = self._ws.workspace_id

        def job(conn: object) -> bool:
            return remove_file(conn, ws_id, path)  # type: ignore[arg-type]

        existed = await asyncio.to_thread(self._known_sha, RelPath(path)) is not None
        if not existed:
            return False
        return await self._writer.asubmit(job, bumps_version=True, label=f"remove:{path}")

    async def scan(self, *, progress_cb: Callable[[int], None] | None = None) -> ScanReport:
        """差分索引。進捗 0〜100 を ``progress_cb`` に渡す（単調増加。最後は必ず 100）。"""
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
        progress(2)

        gone = sorted(set(known) - set(listing))
        for chunk in _chunks(gone, INGEST_BATCH_FILES):
            await self._remove_batch(chunk, report)
        progress(5)

        total = max(len(listing), 1)
        pending: asyncio.Future[list[IngestStats]] | None = None
        batch: list[FileFacts] = []
        file_ids: list[int] = []

        async def flush() -> None:
            nonlocal pending, batch
            if pending is not None:
                file_ids.extend(await self._collect(pending, report))
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
        await flush()
        await flush()  # 最後に積んだバッチの完了を待つ
        report.unchanged -= len(report.errors)  # 失敗は「変わらなかった」に数えない
        if file_ids:
            await self._writer.asubmit(
                lambda conn: _settle_relations(conn, file_ids),  # type: ignore[arg-type]
                bumps_version=False,
                label="settle_relations",
            )
        progress(100)
        report.duration_s = time.monotonic() - t0
        return report

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
        """走っているジョブを取り消して待つ。"""
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
            if detected.tier == Tier.T3:
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
        relations = self._relations(parsed, lang, support, records, rel)
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
        )

    def _relations(
        self, parsed: object, lang: str, support: object, records: list[SymbolRecord], rel: RelPath
    ) -> list[RelationFact]:
        """関係の候補 → ``RelationFact``。目標は 同じファイル内 → import 表 の順で、解決できなければ捨てる。"""
        if _relations_mod is None:
            return []
        ws_id = self._ws.workspace_id
        try:
            facts = extract_imports(parsed, language_id=lang)  # type: ignore[arg-type]
            resolver = make_resolver(from_rel=rel, language_id=lang, exists=self._exists)
            name_map = imported_name_map(facts, resolver)
        except Exception:
            _log.warning("import resolution failed for %s", rel, exc_info=True)
            name_map = {}

        def resolve_name(name: str) -> str | None:
            hit = name_map.get(name)
            if hit is None:
                return None
            path, original = hit
            try:
                if original == "*":  # モジュールそのもの
                    return str(Urn(ws_id, str(path)))
                if original == "default":
                    return None
                return str(Urn(ws_id, str(path), CodeSel(original)))
            except ValueError:
                return None

        candidates = _relations_mod.extract_relations(
            parsed,  # type: ignore[arg-type]
            language_id=lang,
            language=support.grammar(),  # type: ignore[attr-defined]
            symbols=records,
            workspace_id=ws_id,
            rel=rel,
            resolve_name=resolve_name,
        )
        return [
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

    def _exists(self, rel: RelPath) -> bool:
        return (self._ws.root / str(rel)).is_file() and not is_excluded(self._ws, rel)

    def _analyze_adapter(self, rel: RelPath, loaded: _Loaded, data: bytes, detected: Detected) -> FileFacts:
        """Tier 3: ``files`` 行。アダプタが ``symbols(...)`` を持てばそのシンボルも入れる。"""
        support = get_support(detected.language_id)
        symbols_fn = getattr(support, "symbols", None)
        records: list[SymbolRecord] = []
        if callable(symbols_fn):
            records = list(symbols_fn(data, rel=rel, workspace_id=self._ws.workspace_id))
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

    async def _collect(self, pending: asyncio.Future[list[IngestStats]], report: ScanReport) -> list[int]:
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
        return [s.file_id for s in stats]

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
    """
    for file_id in file_ids:
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
