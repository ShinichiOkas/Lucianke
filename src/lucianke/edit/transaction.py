"""編集トランザクション（仕様書 第 10 章、設計書 ADR-11・§7.7・§8.4）。全編集ツール（T-10〜T-13）が使う唯一の実装。

``EditTransaction.run(plan_builder, target=...)`` が 8 段を **この順で** 実行する。ツールは計画（hunk 列）を作る関数
``PlanBuilder`` だけを渡す。範囲の決定・本文の正規化・LSP との往復（rename / codeAction）はビルダーの仕事で、
ロック・前提検証・降格インターロック・構文検証・ジャーナルと書き込み・再索引・診断差分・``diff``・``EditResult`` はここの仕事。

状態（設計書 §7.7.1）と、ファイルが変わりうる場所::

    Resolved → Planned → (ロック) → PreconditionsVerified → SyntaxChecked
        → DryRunDone                                   （dry_run・インターロック・恒等編集。ファイルは不変）
        → Journaled → Written → Reindexed → (ロック解放) → Diagnosed → Done

* ファイルが変わるのは ``Written`` の中だけ（I-1）。``Written`` は全ファイル成功か全ファイルをジャーナルから戻すか（I-2）。
* ロックは ``Planned`` の直後に取り、``Reindexed`` の直後に放す。診断待ち（最大 10 s）はロックの外（他の編集と読み取りを止めない）。
* ``Written`` に入る直前（writer の ``_prepare`` と改名直前）に、ディスクの sha を ``resolved_shas`` と再照合する（I-3, EP-4）。
* 恒等編集（適用後のバイト列が元と同一）は ``Journaled`` 以降を飛ばし、``applied: true``・``edit_id: null``（I-6, EP-6）。
* 降格ファイル（``heuristic`` / ``lexical_fallback``）で ``allow_heuristic_mutation`` が無ければ ``dry_run`` を強制し、
  成功応答に警告を書く（仕様書 §10.5。エラーにはしない）。
* 編集前の診断の写し（``Planned`` の直前。ロックの外）は、**LSP がそのファイルのいまの内容の診断を出し終えるのを待ってから** 取る
  （上限 ``diagnostics_wait_ms``。書き込みをしない呼び出しと 0 では待たない）。待っても出なかったサーバーは「基準が不明」として
  差分を ``partial`` にし、その診断を ``added`` と言わない（BUG-0098。設計書 §7.7.6-1）。

依存はすべてコンストラクタ引数で注入する（テストで偽物に差し替える）。``indexer`` / ``watcher`` / ``documents`` の呼び出しは
同期でも非同期でもよい。再索引（``Reindexed``）の失敗は応答を止めず ``warnings`` に残す。監視にこだまを捨てさせる
（``watcher.expect``）のは索引への取り込みが済んだファイルだけで、取り込めなかったファイルは ``watcher.forget`` して監視の
通常の道（こだまのイベント → ``sink.changed``）で取り込み直させる（BUG-0087。古い索引のまま残さない）。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import (
    DIAGNOSTICS_WAIT_DEFAULT_MS,
    DIAGNOSTICS_WAIT_MAX_MS,
    DIRECT_REFERRERS_MAX,
    EDIT_LOCK_WAIT_MS,
    MAX_FILE_BYTES,
)
from lucianke.core.model import (
    Capability,
    Confidence,
    FileInfo,
    LspStatus,
    Range,
    RelPath,
    SymbolRecord,
    TargetRef,
    Tier,
)
from lucianke.core.urn import CodeSel, parse, relax
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.plan import Plan, apply_hunks, check_no_overlap, unified_diff
from lucianke.edit.syntax_check import check_after, to_error_details
from lucianke.edit.verify import (
    ANY_LSP_SOURCE,
    DiagDelta,
    Snapshot,
    compute_delta,
    not_computed,
    pending,
    structural_delta,
)
from lucianke.edit.writer import FileWrite, write_all
from lucianke.engine.selector import select
from lucianke.index.queries import DiagnosticRow, diagnostics_for, references_to
from lucianke.syntax.boundaries import definition_range
from lucianke.syntax.parsers import parse_source
from lucianke.syntax.support import get_support
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import ParsedFile
from lucianke.workspace.languages import HEAD_BYTES, detect
from lucianke.workspace.paths import confine, to_abs
from lucianke.workspace.types import Workspace

__all__ = [
    "DocumentsLike",
    "EditResult",
    "EditTransaction",
    "FileChange",
    "HubLike",
    "IndexerLike",
    "LoadedFile",
    "PlanBuilder",
    "PlanContext",
    "Stage",
    "WatcherLike",
]

_log = logging.getLogger("lucianke.edit.transaction")

WORKING_TREE = "WORKING_TREE"
_SHA_PREFIX_MIN = 12
_HEX = re.compile(r"[0-9a-f]+")
_SELECTOR_PREFIXES = ("xpath:", "jsonpath:", "jq:", "toml:", "yaml:", "cell:")
_INTERLOCK_WARNING = "降格のため書き込みを抑止した。確認のうえ `allow_heuristic_mutation: true` で再実行せよ"


class Stage(StrEnum):
    """状態機械の状態名（設計書 §7.7.1）。``stage_hook`` に渡される。"""

    RESOLVED = "Resolved"
    PLANNED = "Planned"
    LOCKED = "Locked"
    PRECONDITIONS_VERIFIED = "PreconditionsVerified"
    SYNTAX_CHECKED = "SyntaxChecked"
    DRY_RUN_DONE = "DryRunDone"
    JOURNALED = "Journaled"
    WRITTEN = "Written"
    REINDEXED = "Reindexed"
    DIAGNOSED = "Diagnosed"
    DONE = "Done"


# --- 注入する依存の形 -----------------------------------------------------------------------


class IndexerLike(Protocol):
    """``engine.indexer.Indexer`` の一部。書き込んだファイルを索引に取り込む（完了まで待つ）。"""

    def index_file(self, rel: RelPath) -> Awaitable[Any] | Any: ...


class WatcherLike(Protocol):
    """``workspace.watcher.Watcher`` の一部。"""

    def drain(self) -> Awaitable[None] | None: ...

    def expect(self, rel: RelPath, sha256_after: str) -> None: ...

    def forget(self, rel: RelPath) -> None: ...


class DocumentsLike(Protocol):
    """``lsp.documents.DocumentSync`` の一部（LSP サーバー 1 つ分）。"""

    def sync(self, path: RelPath, text: str, language_id: str) -> int: ...

    def sent_at_of(self, path: RelPath) -> float | None: ...

    def is_open(self, path: RelPath) -> bool: ...


class HubLike(Protocol):
    """``lsp.diagnostics.DiagnosticsHub`` の一部。

    ``wait_for`` が ``True`` で返ったとき、その診断の ``diagnostics`` 表への書き込みは終わっていること（EP-7）。
    任意で ``wait_sources``（同じ引数で ``{サーバー名: 揃ったか}`` を返す。``lsp.documents.RoutedDiagnostics``）を持てば、
    編集前の基準が分からないサーバーをサーバーごとに区別する（BUG-0098）。無ければファイルの LSP 全体（``"*"``）で扱う。
    """

    async def wait_for(
        self,
        path: RelPath,
        *,
        min_version: int | None,
        sent_at: float,
        timeout_ms: int | float,
        settle_ms: int | float = 0,
        min_publishes: int = 1,
    ) -> bool: ...


# --- ビルダーに渡す文脈・結果 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedFile:
    """読み込み済みのファイル 1 つ（``Resolved`` で作る）。``parsed`` は文法が無ければ ``None``。"""

    rel: RelPath
    abs_path: Path
    data: bytes
    sha256: str
    language_id: str
    tier: Tier
    parsed: ParsedFile | None

    @property
    def has_grammar(self) -> bool:
        return self.parsed is not None


@dataclass(frozen=True, slots=True)
class PlanContext:
    """トランザクションが ``plan_builder`` に渡す読み取り専用の文脈。``plan_builder`` はファイルを書いてはならない（MUST）。

    ``symbol`` は解決した対象（``path`` だけの指定や文法の無い言語では ``None``）。``node`` は ``symbol`` の定義ノード
    （tree-sitter の ``Node``。無ければ ``None``）。``source`` は読み込んだバイト列、``sha256`` はその SHA-256。
    ``confidence`` はこのファイルへの編集（``SYMBOL_EDIT``）の精度水準。
    """

    workspace: Workspace
    target: TargetRef
    path: RelPath
    symbol: SymbolRecord | None
    node: Any | None
    parsed: ParsedFile | None
    source: bytes
    sha256: str
    language_id: str
    confidence: Confidence
    tier: Tier = Tier.T1


PlanBuilder = Callable[[PlanContext], Awaitable[Plan]]


@dataclass(frozen=True, slots=True)
class FileChange:
    path: RelPath
    sha256_before: str
    sha256_after: str
    hunks: int
    renamed_from: RelPath | None = None


@dataclass(frozen=True, slots=True)
class EditResult:
    """編集の結果（設計書 §7.7.1）。``dry_run`` でも ``diff`` を作る。

    ``new_range`` は再索引後の対象の定義範囲（``delete`` では ``None``）、``target_urns_after`` はその URN。
    """

    applied: bool
    edit_id: str | None
    files_changed: tuple[FileChange, ...]
    diff: str
    syntax_check: Literal["ok", "skipped"]
    new_diagnostics: DiagDelta
    target_urns_after: tuple[str, ...]
    confidence: Confidence
    warnings: tuple[str, ...]
    new_range: Range | None = None
    forced_dry_run: bool = False
    stages: tuple[str, ...] = field(default=(), repr=False)
    """通過した状態の名前（テストと診断用）。"""


# --- 小さな部品 ----------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _precondition_failed(path: RelPath | str, expected: str | None, actual: str | None) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_PRECONDITION_FAILED,
        f"file changed since it was read: {path}",
        details={"path": str(path), "expected": expected, "actual": actual},
    )


def _invalid(message: str, **details: object) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, message, details=details)


def _candidate(s: SymbolRecord) -> dict[str, Any]:
    return {"urn": str(s.urn), "signature": s.signature, "path": str(s.path), "line": s.range.start.line + 1}


def _ambiguous(name: str, found: Sequence[SymbolRecord]) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_TARGET_AMBIGUOUS,
        f"{name!r} matches {len(found)} symbols; use a longer symbol_name, a urn, or a line",
        candidates=[_candidate(s) for s in found[:10]],
    )


def _by_urn(symbols: Sequence[SymbolRecord], urn_text: str) -> SymbolRecord:
    for s in symbols:
        if str(s.urn) == urn_text:
            return s
    loose = str(relax(parse(urn_text)))
    found = [s for s in symbols if str(relax(s.urn)) == loose]
    if len(found) == 1:
        return found[0]
    if found:
        raise _ambiguous(urn_text, found)
    raise LuciankeError(
        ErrorCode.E_TARGET_NOT_FOUND, f"no symbol with urn {urn_text}", details={"urn": urn_text}
    )


def _by_name(symbols: Sequence[SymbolRecord], rel: RelPath, name: str) -> SymbolRecord:
    exact = [s for s in symbols if s.qualified_name == name]
    if len(exact) == 1:
        return exact[0]
    found = exact or [s for s in symbols if s.qualified_name.endswith("." + name)]
    if not found:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"symbol {name!r} not found in {rel}",
            details={"path": str(rel), "symbol_name": name},
        )
    if len(found) > 1:
        raise _ambiguous(name, found)
    return found[0]


def _by_line(symbols: Sequence[SymbolRecord], rel: RelPath, line: int) -> SymbolRecord:
    """``line`` は 0 始まり（``TargetRef`` の内部表現）。最内（行数が最小）のシンボル。"""
    inside = [
        s for s in symbols if s.range.line_span_external()[0] <= line + 1 <= s.range.line_span_external()[1]
    ]
    if not inside:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"no symbol contains line {line + 1} of {rel}",
            details={"path": str(rel), "line": line + 1},
        )

    def span(s: SymbolRecord) -> tuple[int, int]:
        a, b = s.range.line_span_external()
        return (b - a, -s.range.start.line)

    return min(inside, key=span)


def _node_of(parsed: ParsedFile, sym: SymbolRecord) -> Any | None:
    li = parsed.line_index
    start = li.position_to_offset(sym.range.start).value
    end = li.position_to_offset(sym.range.end).value
    return parsed.tree.root_node.descendant_for_byte_range(start, end)


# --- 本体 ---------------------------------------------------------------------------------


class EditTransaction:
    """編集トランザクション（モジュールの docstring が仕様）。

    Args:
        ws: 対象のワークスペース。
        locks: ワークスペース書き込みロック（``edit/lock.py``）。
        writer_index: ``IndexWriter``（``anext_edit_id``）。
        journal_dir: ジャーナルの置き場（``<cache_root>/<ws_hash>/journal``）。
        indexer: 書き込んだファイルを取り込む（``index_file(rel)``）。``None`` なら取り込まない。
        watcher: 前提検証の前に ``drain()``。書き込み後、``indexer`` がそのファイルを取り込めたら ``expect(rel, sha)``
            （こだまを捨てる）、取り込めなかった（失敗・``indexer`` なし）なら ``forget(rel)``（こだまを監視の通常の道で
            取り込ませる。BUG-0087）。``None`` なら何もしない。
        documents: LSP への ``didOpen`` / ``didChange``。``None`` なら LSP 不在として扱う。
        diagnostics_hub: LSP の診断の受け皿。``None`` なら LSP 不在（Tree-sitter の構文エラー差分だけを返す）。
        reader_pool: ``ReaderPool``。診断スナップショットと直接の参照元の読み取りに使う。``None`` なら空。
        trees: ``TreeCache``。書き込んだファイルの旧キーを無効化する。``None`` なら何もしない。
        lsp_status: 言語 ID → LSP の状態（``select`` の入力）。既定は、``diagnostics_hub`` があれば ``READY``、無ければ ``UNAVAILABLE``。
        has_ctags: 文法の無い言語で ctags が使えるか（``select`` の入力。降格はどちらでも ``heuristic`` 以下）。
        stage_hook: 各状態に入るたびに ``await`` される ``(状態名) -> None``。テストが外部書き込みを差し込む。
    """

    def __init__(
        self,
        ws: Workspace,
        *,
        locks: WorkspaceLocks,
        writer_index: Any,
        journal_dir: Path,
        indexer: IndexerLike | None = None,
        watcher: WatcherLike | None = None,
        documents: DocumentsLike | None = None,
        diagnostics_hub: HubLike | None = None,
        reader_pool: Any | None = None,
        trees: Any | None = None,
        lsp_status: Callable[[str], LspStatus] | None = None,
        has_ctags: bool = False,
        stage_hook: Callable[[str], Awaitable[None] | None] | None = None,
        lock_wait_ms: int = EDIT_LOCK_WAIT_MS,
    ) -> None:
        self._ws = ws
        self._locks = locks
        self._writer = writer_index
        self._journal_dir = Path(journal_dir)
        self._indexer = indexer
        self._watcher = watcher
        self._documents = documents
        self._hub = diagnostics_hub
        self._pool = reader_pool
        self._trees = trees
        self._lsp_status = lsp_status or self._default_lsp_status
        self._has_ctags = has_ctags
        self.stage_hook = stage_hook
        self._lock_wait_ms = lock_wait_ms

    # --- 公開 ---------------------------------------------------------------------------

    async def run(
        self,
        plan_builder: PlanBuilder,
        *,
        target: TargetRef,
        dry_run: bool = False,
        allow_heuristic_mutation: bool = False,
        expected_shas: Mapping[str, str] | None = None,
        diagnostics_wait_ms: int = DIAGNOSTICS_WAIT_DEFAULT_MS,
        revision: str = WORKING_TREE,
    ) -> EditResult:
        """8 段を実行して ``EditResult`` を返す。失敗は ``LuciankeError``（ファイルは変更されないか、ロールバック済み）。"""
        if revision != WORKING_TREE:
            raise _invalid("revision must be WORKING_TREE for edits", param="revision", value=revision)
        if not 0 <= diagnostics_wait_ms <= DIAGNOSTICS_WAIT_MAX_MS:
            raise _invalid(
                f"diagnostics_wait_ms must be 0..{DIAGNOSTICS_WAIT_MAX_MS}", param="diagnostics_wait_ms"
            )
        stages: list[str] = []
        warnings: list[str] = []
        expected = self._normalize_expected(expected_shas)

        # 1. Resolved
        rel = self._target_path(target)
        loaded = await self._load(rel)
        symbol = await self._resolve_symbol(target, loaded)
        confidence, engine_warning = self._engine(loaded)
        await self._enter(Stage.RESOLVED, stages)

        # 2. Planned
        ctx = PlanContext(
            workspace=self._ws,
            target=target,
            path=rel,
            symbol=symbol,
            node=_node_of(loaded.parsed, symbol)
            if loaded.parsed is not None and symbol is not None
            else None,
            parsed=loaded.parsed,
            source=loaded.data,
            sha256=loaded.sha256,
            language_id=loaded.language_id,
            confidence=confidence,
            tier=loaded.tier,
        )
        plan = await plan_builder(ctx)
        plan = self._complete_plan(plan, loaded, symbol)
        # 書かない呼び出し（dry_run・インターロック）は診断を取らないので、編集前の基準も待たない
        writes = not dry_run and (confidence >= Confidence.STRUCTURAL or allow_heuristic_mutation)
        snapshot = await self._snapshot(plan, loaded, diagnostics_wait_ms if writes else 0)
        await self._enter(Stage.PLANNED, stages)

        async with self._locks.acquire(self._ws.workspace_id, timeout_ms=self._lock_wait_ms):
            await self._enter(Stage.LOCKED, stages)

            # 3. PreconditionsVerified
            sources = await self._verify_preconditions(plan, loaded, expected)
            degraded = confidence < Confidence.STRUCTURAL
            forced_dry = degraded and not allow_heuristic_mutation
            if forced_dry:
                warnings.append(_INTERLOCK_WARNING)
            elif degraded and engine_warning:
                warnings.append(engine_warning)
            effective_dry = dry_run or forced_dry
            await self._enter(Stage.PRECONDITIONS_VERIFIED, stages)

            # 4. SyntaxChecked
            new_bytes = {p: apply_hunks(sources[p], plan.hunks_for(p)) for p in plan.hunk_paths}
            syntax = await self._check_syntax(plan, loaded, sources, new_bytes)
            identity = all(new_bytes[p] == sources[p] for p in new_bytes)
            diff = "".join(unified_diff(p, sources[p], new_bytes[p]) for p in new_bytes)
            changes = tuple(
                FileChange(
                    p,
                    plan.resolved_shas[p],
                    _sha256(new_bytes[p]),
                    len(plan.hunks_for(p)) if new_bytes[p] != sources[p] else 0,
                )
                for p in new_bytes
            )
            await self._enter(Stage.SYNTAX_CHECKED, stages)

            if effective_dry or identity:
                await self._enter(Stage.DRY_RUN_DONE, stages)
                if identity:
                    warnings.append("no-op: the edit does not change the file")
                return EditResult(
                    applied=identity and not effective_dry,
                    edit_id=None,
                    files_changed=changes,
                    diff="" if identity else diff,
                    syntax_check=syntax,
                    new_diagnostics=not_computed(confidence=confidence),
                    target_urns_after=plan.target_urns,
                    confidence=confidence,
                    warnings=tuple(warnings),
                    forced_dry_run=forced_dry,
                    stages=tuple(stages),
                )

            # 6. Journaled / Written
            edit_id = await self._writer.anext_edit_id()
            await self._enter(Stage.JOURNALED, stages)
            writes = [FileWrite(to_abs(self._ws, p), new_bytes[p], plan.resolved_shas[p]) for p in new_bytes]
            written = await asyncio.to_thread(
                write_all, writes, journal_dir=self._journal_dir, edit_id=edit_id
            )
            after_sha = {p: w.sha256_after for p, w in zip(new_bytes, written, strict=True)}
            changes = tuple(replace(c, sha256_after=after_sha[c.path]) for c in changes)
            await self._enter(Stage.WRITTEN, stages)

            # 7. Reindexed
            sent = await self._reindex(plan, loaded, new_bytes, after_sha, warnings)
            await self._enter(Stage.REINDEXED, stages)
        # ロック解放

        # 8. Diagnosed（ロックの外）
        urns_after, new_range = await self._locate_after(plan, loaded, symbol, new_bytes)
        delta = await self._diagnose(
            snapshot, plan, loaded, sources, new_bytes, sent, diagnostics_wait_ms, confidence
        )
        await self._enter(Stage.DIAGNOSED, stages)
        await self._enter(Stage.DONE, stages)
        return EditResult(
            applied=True,
            edit_id=edit_id,
            files_changed=changes,
            diff=diff,
            syntax_check=syntax,
            new_diagnostics=delta,
            target_urns_after=urns_after,
            confidence=confidence,
            warnings=tuple(warnings),
            new_range=new_range,
            stages=tuple(stages),
        )

    # --- 状態の通過 ---------------------------------------------------------------------

    async def _enter(self, stage: Stage, stages: list[str]) -> None:
        stages.append(stage.value)
        if self.stage_hook is not None:
            await _maybe_await(self.stage_hook(stage.value))

    # --- 1. Resolved --------------------------------------------------------------------

    def _target_path(self, target: TargetRef) -> RelPath:
        try:
            form, _ignored = target.resolution_form()
        except ValueError as exc:
            raise _invalid(str(exc), param="target") from None
        if form == "urn":
            assert target.urn is not None
            try:
                urn = parse(target.urn)
            except ValueError as exc:
                raise _invalid(f"target.urn: {exc}", param="target.urn") from None
            if urn.workspace_id != self._ws.workspace_id or not isinstance(urn.selector, CodeSel):
                raise LuciankeError(
                    ErrorCode.E_TARGET_NOT_FOUND,
                    f"urn does not name a code symbol of workspace {self._ws.workspace_id!r}: {target.urn}",
                    details={"urn": target.urn},
                )
            path = urn.path
        else:
            assert target.path is not None
            path = target.path
        return confine(self._ws, path, for_write=True, param="target")

    async def _load(self, rel: RelPath) -> LoadedFile:
        abs_path = to_abs(self._ws, rel)
        try:
            data = await asyncio.to_thread(self._read_checked, abs_path, rel)
        except FileNotFoundError:
            raise LuciankeError(
                ErrorCode.E_TARGET_NOT_FOUND, f"file not found: {rel}", details={"path": str(rel)}
            ) from None
        det = detect(rel, data[:HEAD_BYTES])
        if not det.index_content:
            reason = "secret_file" if det.is_secret else "binary_file"
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"{rel}: content is not editable ({reason})",
                details={"reason": reason},
            )
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"{rel}: not a UTF-8 file",
                details={"reason": "non_utf8_file", "path": str(rel)},
            ) from None
        sha = _sha256(data)
        language_id = det.language_id
        spec = get_support(language_id)
        parsed: ParsedFile | None = None
        if spec is not None and callable(getattr(spec, "grammar", None)):
            if self._trees is not None:
                parsed = await self._trees.aget_or_parse(
                    self._ws.workspace_id, rel, sha, language_id, lambda: data
                )
            else:
                parsed, _timed_out = await asyncio.to_thread(parse_source, data, language_id, sha)
        return LoadedFile(rel, abs_path, data, sha, language_id, det.tier, parsed)

    @staticmethod
    def _read_checked(abs_path: Path, rel: RelPath) -> bytes:
        size = abs_path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"file too large to edit: {size} bytes > {MAX_FILE_BYTES}",
                details={"reason": "file_too_large", "path": str(rel), "size": size},
            )
        return abs_path.read_bytes()

    async def _resolve_symbol(self, target: TargetRef, loaded: LoadedFile) -> SymbolRecord | None:
        form, _ignored = target.resolution_form()
        if form == "path" or loaded.parsed is None:
            return None  # ファイル全体、または文法が無い（範囲はビルダーが別の手段で決める）
        if target.symbol_name is not None and target.symbol_name.startswith(_SELECTOR_PREFIXES):
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"selector {target.symbol_name.split(':', 1)[0]}: is not supported for code files",
                details={"reason": "selector_not_supported"},
            )
        parsed, spec = loaded.parsed, get_support(loaded.language_id)
        assert spec is not None
        try:
            symbols = await asyncio.to_thread(
                extract,
                parsed,
                language_id=loaded.language_id,
                language=spec.grammar(),  # pyright: ignore[reportAttributeAccessIssue]
                query_source=load_query_source(loaded.language_id),
                workspace_id=self._ws.workspace_id,
                rel=loaded.rel,
            )
        except ValueError:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"{loaded.rel}: symbol extraction is not supported for {loaded.language_id!r}",
                details={"reason": "language_not_supported", "language_id": loaded.language_id},
            ) from None
        if form == "urn":
            assert target.urn is not None
            return _by_urn(symbols, target.urn)
        if form == "line":
            assert target.line is not None
            return _by_line(symbols, loaded.rel, target.line)
        assert target.symbol_name is not None
        return _by_name(symbols, loaded.rel, target.symbol_name)

    def _default_lsp_status(self, _language_id: str) -> LspStatus:
        return (
            LspStatus.READY
            if self._hub is not None and self._documents is not None
            else LspStatus.UNAVAILABLE
        )

    def _engine(self, loaded: LoadedFile) -> tuple[Confidence, str | None]:
        info = FileInfo(
            file_id=0,
            workspace_id=self._ws.workspace_id,
            path=loaded.rel,
            language=loaded.language_id,
            tier=loaded.tier,
            sha256=loaded.sha256,
            line_count=loaded.data.count(b"\n") + 1,
            byte_size=len(loaded.data),
            is_test=False,
            confidence=Confidence.EXACT,
        )
        choice = select(
            info,
            Capability.SYMBOL_EDIT,
            lsp_status=self._lsp_status(loaded.language_id),
            lsp_progress=None,
            has_grammar=loaded.has_grammar,
            has_ctags=self._has_ctags,
        )
        return Confidence(choice.confidence), choice.warning

    # --- 2. Planned ---------------------------------------------------------------------

    def _complete_plan(self, plan: Plan, loaded: LoadedFile, symbol: SymbolRecord | None) -> Plan:
        if plan.renames:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                "file renames are not supported by the transaction yet",
                details={"reason": "file_rename_not_supported"},
            )
        check_no_overlap(plan.hunks)
        shas = dict(plan.resolved_shas)
        shas.setdefault(loaded.rel, loaded.sha256)
        urns = plan.target_urns or ((str(symbol.urn),) if symbol is not None else ())
        return replace(
            plan, resolved_shas=shas, target_urns=urns, touched_paths=plan.touched_paths or (loaded.rel,)
        )

    # --- 3. PreconditionsVerified -------------------------------------------------------

    def _normalize_expected(self, expected: Mapping[str, str] | None) -> dict[RelPath, str]:
        out: dict[RelPath, str] = {}
        for path, value in (expected or {}).items():
            sha = str(value).strip().lower()
            if len(sha) < _SHA_PREFIX_MIN or len(sha) > 64 or not _HEX.fullmatch(sha):
                raise _invalid(
                    f"expected sha256 must be 64 hex digits or a prefix of at least {_SHA_PREFIX_MIN}",
                    param="expected_file_sha256",
                    path=str(path),
                )
            out[confine(self._ws, path, param="expected_file_sha256")] = sha
        return out

    @staticmethod
    def _read_now(abs_path: Path) -> bytes | None:
        try:
            return abs_path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None

    async def _verify_preconditions(
        self, plan: Plan, loaded: LoadedFile, expected: Mapping[RelPath, str]
    ) -> dict[RelPath, bytes]:
        """⑴ ``expected_shas`` の照合 ⑵ 監視の未処理イベントを処理して ``resolved_shas`` と再照合。現在の内容を返す。"""
        paths = list(dict.fromkeys([*plan.hunk_paths, *plan.touched_paths, *expected]))

        async def read_all() -> dict[RelPath, bytes | None]:
            contents = await asyncio.gather(
                *(asyncio.to_thread(self._read_now, to_abs(self._ws, p)) for p in paths)
            )
            return dict(zip(paths, contents, strict=True))

        now = await read_all()
        for path, prefix in expected.items():
            data = now[path]
            actual = _sha256(data) if data is not None else None
            if actual is None or not actual.startswith(prefix):
                raise _precondition_failed(path, prefix, actual)
        if self._watcher is not None:
            await _maybe_await(self._watcher.drain())
            now = await read_all()
        sources: dict[RelPath, bytes] = {}
        for path in paths:
            data = now[path]
            actual = _sha256(data) if data is not None else None
            want = plan.resolved_shas.get(path)
            if path in plan.touched_paths and want is not None and actual != want:
                raise _precondition_failed(path, want, actual)
            if data is not None:
                sources[path] = data
        for path in plan.hunk_paths:
            if path not in sources:
                raise _precondition_failed(path, plan.resolved_shas.get(path), None)
            plan.resolved_shas.setdefault(path, _sha256(sources[path]))  # type: ignore[attr-defined]
        return sources

    # --- 4. SyntaxChecked ---------------------------------------------------------------

    async def _check_syntax(
        self,
        plan: Plan,
        loaded: LoadedFile,
        sources: Mapping[RelPath, bytes],
        new_bytes: Mapping[RelPath, bytes],
    ) -> Literal["ok", "skipped"]:
        if not plan.expects_syntax:
            return "skipped"
        checked = False
        for path in new_bytes:
            language_id = (
                loaded.language_id
                if path == loaded.rel
                else detect(path, new_bytes[path][:HEAD_BYTES]).language_id
            )
            spec = get_support(language_id)
            if spec is None or not callable(getattr(spec, "grammar", None)):
                continue
            result = await asyncio.to_thread(check_after, language_id, sources[path], new_bytes[path])
            if result.status == "skipped":
                continue
            checked = True
            if result.status == "failed":
                raise LuciankeError(
                    ErrorCode.E_SYNTAX_INVALID,
                    f"the edit would introduce syntax errors in {path}",
                    details={"path": str(path), **to_error_details(result)},
                )
        return "ok" if checked else "skipped"

    # --- 7. Reindexed -------------------------------------------------------------------

    async def _reindex(
        self,
        plan: Plan,
        loaded: LoadedFile,
        new_bytes: Mapping[RelPath, bytes],
        after_sha: Mapping[RelPath, str],
        warnings: list[str],
    ) -> dict[RelPath, tuple[int | None, float | None]]:
        """索引・LSP・監視へ反映する。失敗は応答を止めず ``warnings`` に残す。返り値は ``path → (LSP 文書の版, 送信時刻)``。"""
        sent: dict[RelPath, tuple[int | None, float | None]] = {}
        for path, data in new_bytes.items():
            language_id = (
                loaded.language_id if path == loaded.rel else detect(path, data[:HEAD_BYTES]).language_id
            )
            if self._trees is not None:
                try:
                    self._trees.evict(self._ws.workspace_id, path)
                except Exception as exc:
                    _log.warning("tree cache eviction failed for %s: %s", path, exc)
            indexed = False
            if self._indexer is not None:
                try:
                    await _maybe_await(self._indexer.index_file(path))
                    indexed = True
                except Exception as exc:
                    _log.warning("reindex failed for %s: %s", path, exc, exc_info=True)
                    warnings.append(f"reindex failed for {path}: {type(exc).__name__}")
            if self._documents is not None:
                try:
                    text = data.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
                    version = self._documents.sync(path, text, language_id)
                    sent[path] = (version, self._documents.sent_at_of(path))
                except Exception as exc:
                    _log.warning("LSP document sync failed for %s: %s", path, exc, exc_info=True)
                    warnings.append(f"LSP didChange failed for {path}: {type(exc).__name__}")
            if self._watcher is not None:
                try:
                    if indexed:
                        # 索引が書いた内容を取り込み終えたときだけ、こだま（自分の書き込みのイベント）を捨てさせる
                        self._watcher.expect(path, after_sha[path])
                    else:
                        # 取り込めなかった（失敗・索引なし）: こだまを捨てさせず、監視の通常の道で取り込み直させる。
                        # 前の編集の期待が残っていれば消す（同じ内容に戻す編集のこだまを、古い期待が捨てないように。BUG-0087）
                        self._watcher.forget(path)
                except Exception as exc:
                    _log.warning("watcher.expect/forget failed for %s: %s", path, exc)
        return sent

    async def _locate_after(
        self,
        plan: Plan,
        loaded: LoadedFile,
        symbol: SymbolRecord | None,
        new_bytes: Mapping[RelPath, bytes],
    ) -> tuple[tuple[str, ...], Range | None]:
        """再索引後の対象 URN と定義範囲（書き込んだ内容を解析し直して対応付ける。``delete`` などで無ければ空と ``None``）。"""
        hunks = plan.hunks_for(loaded.rel)
        # 取り除くだけの編集（delete_symbol）。対象はもう無い（同名の定義が残っていても、それは別のシンボル）
        if hunks and all(h.new_text == "" for h in hunks):
            return plan.target_urns, None
        data = new_bytes.get(loaded.rel)
        spec = get_support(loaded.language_id)
        if symbol is None or data is None or spec is None or not callable(getattr(spec, "grammar", None)):
            return plan.target_urns, None

        def work() -> tuple[tuple[str, ...], Range | None]:
            parsed, _ = parse_source(data, loaded.language_id, _sha256(data))
            syms = extract(
                parsed,
                language_id=loaded.language_id,
                language=spec.grammar(),  # pyright: ignore[reportAttributeAccessIssue]
                query_source=load_query_source(loaded.language_id),
                workspace_id=self._ws.workspace_id,
                rel=loaded.rel,
            )
            hunks = plan.hunks_for(loaded.rel)
            first_line = min((h.range.start.line for h in hunks), default=symbol.range.start.line)
            found = [s for s in syms if s.qualified_name == symbol.qualified_name and s.kind == symbol.kind]
            if not found:
                later = [s for s in syms if s.range.start.line >= first_line]
                found = [min(later, key=lambda s: s.range.start.line)] if later else []
            if not found:
                return (), None
            node = _node_of(parsed, found[0])
            rng = definition_range(node, parsed, language_id=loaded.language_id) if node is not None else None
            return (str(found[0].urn),), rng

        try:
            return await asyncio.to_thread(work)
        except Exception as exc:
            _log.warning("could not locate the edited symbol after the write: %s", exc, exc_info=True)
            return plan.target_urns, None

    # --- 8. Diagnosed -------------------------------------------------------------------

    async def _read_diagnostics(self, paths: Sequence[RelPath]) -> dict[RelPath, list[DiagnosticRow]]:
        """``diagnostics`` 表の現在行（``paths`` ごと）。DB が無ければ空。"""
        out: dict[RelPath, list[DiagnosticRow]] = {p: [] for p in paths}
        pool = self._pool
        if pool is None or not paths:
            return out

        def work() -> list[DiagnosticRow]:
            with pool.reader() as conn:
                return diagnostics_for(conn, self._ws.workspace_id, [str(p) for p in paths])

        for row in await asyncio.to_thread(work):
            out.setdefault(row.path, []).append(row)
        return out

    async def _direct_referrers(
        self, urns: Sequence[str], exclude: Sequence[RelPath]
    ) -> tuple[tuple[RelPath, ...], int]:
        """``urns`` を ``target_urn`` に持つ関係の参照元ファイル（多い順、上限 ``DIRECT_REFERRERS_MAX``）と、除いた件数。"""
        pool = self._pool
        if pool is None or not urns:
            return (), 0

        def work() -> tuple[tuple[RelPath, ...], int]:
            counts: dict[RelPath, int] = {}
            with pool.reader() as conn:
                for urn in urns:
                    for row in references_to(conn, urn):
                        if row.path is not None and row.path not in exclude:
                            counts[row.path] = counts.get(row.path, 0) + 1
            ordered = sorted(counts, key=lambda p: (-counts[p], str(p)))
            return tuple(ordered[:DIRECT_REFERRERS_MAX]), max(0, len(ordered) - DIRECT_REFERRERS_MAX)

        return await asyncio.to_thread(work)

    async def _snapshot(self, plan: Plan, loaded: LoadedFile, wait_ms: int) -> Snapshot:
        """編集前の診断（対象ファイルと直接の参照元）。取れなければ空で ``failed``（編集は止めない。基準が不明として扱う）。

        ``wait_ms > 0`` なら、表を読む前に各ファイルのいまの内容を LSP に見せ、その診断が出て表に書かれるのを待つ（BUG-0098）。
        """
        if self._hub is None:
            return Snapshot()  # LSP 不在: 構文エラー差分は解析済みの木から作る
        try:
            referrers, omitted = await self._direct_referrers(plan.target_urns, plan.touched_paths)
            paths = [*plan.touched_paths, *referrers]
            baseline = (
                await self._baseline(paths, loaded, wait_ms)
                if wait_ms > 0 and self._documents is not None
                else None
            )
            rows = await self._read_diagnostics(paths)
            return Snapshot({p: tuple(r) for p, r in rows.items()}, referrers, omitted, baseline)
        except Exception as exc:
            _log.warning("could not take the diagnostics snapshot: %s", exc, exc_info=True)
            return Snapshot(failed=True)

    async def _baseline(
        self, paths: Sequence[RelPath], loaded: LoadedFile, wait_ms: int
    ) -> dict[RelPath, dict[str, bool]]:
        """編集前の基準を待つ（BUG-0098）: ``paths`` のいまの内容を LSP に見せ（同じ内容なら何も送らない）、サーバーごとに
        その版の診断が出て表に書き終わるのを待つ（並べて、各 ``wait_ms`` まで。出ていれば即戻る）。``{パス: {サーバー名: 揃ったか}}``。

        まだ無いファイルは載せない（編集前の診断は無い＝基準は空で確か）。LSP に見せられなかった（動いているサーバーが無い）
        ファイルは ``{"*": False}``。
        """
        assert self._documents is not None
        docs = self._documents
        out: dict[RelPath, dict[str, bool]] = {}
        waits: dict[RelPath, tuple[int, float]] = {}
        for path in dict.fromkeys(paths):
            data = (
                loaded.data
                if path == loaded.rel
                else await asyncio.to_thread(self._read_now, to_abs(self._ws, path))
            )
            if data is None:
                continue
            language_id = (
                loaded.language_id if path == loaded.rel else detect(path, data[:HEAD_BYTES]).language_id
            )
            try:
                text = data.removeprefix(b"\xef\xbb\xbf").decode("utf-8", "replace")
                version = docs.sync(path, text, language_id)
            except Exception as exc:  # ConnectionError: その言語の LSP が動いていない
                _log.debug("baseline: could not show %s to the LSP: %s", path, exc)
                out[path] = {ANY_LSP_SOURCE: False}
                continue
            sent_at = docs.sent_at_of(path)
            waits[path] = (version, sent_at if sent_at is not None else 0.0)
        results = await asyncio.gather(
            *(
                self._wait_sources(path, min_version=version, sent_at=sent_at, timeout_ms=wait_ms)
                for path, (version, sent_at) in waits.items()
            ),
            return_exceptions=True,
        )
        for path, got in zip(waits, results, strict=True):
            out[path] = got if isinstance(got, dict) else {ANY_LSP_SOURCE: False}
        return out

    async def _wait_sources(
        self, path: RelPath, *, min_version: int | None, sent_at: float, timeout_ms: int
    ) -> dict[str, bool]:
        """``path`` の診断の待ちを ``{サーバー名: 揃ったか}`` で。口が ``wait_sources`` を持たなければ ``{"*": 揃ったか}``。"""
        assert self._hub is not None
        each: Callable[..., Awaitable[Mapping[str, bool]]] | None = getattr(self._hub, "wait_sources", None)
        if callable(each):
            got = await each(path, min_version=min_version, sent_at=sent_at, timeout_ms=timeout_ms)
            return {str(k): v is True for k, v in got.items()}
        ok = await self._hub.wait_for(path, min_version=min_version, sent_at=sent_at, timeout_ms=timeout_ms)
        return {ANY_LSP_SOURCE: ok is True}

    async def _diagnose(
        self,
        snapshot: Snapshot,
        plan: Plan,
        loaded: LoadedFile,
        sources: Mapping[RelPath, bytes],
        new_bytes: Mapping[RelPath, bytes],
        sent: Mapping[RelPath, tuple[int | None, float | None]],
        wait_ms: int,
        confidence: Confidence,
    ) -> DiagDelta:
        edited = list(new_bytes)
        lsp = self._hub is not None and self._documents is not None
        try:
            if not lsp:
                return await self._structural(loaded, sources, new_bytes)
            if wait_ms == 0:
                return pending([*edited, *snapshot.referrers], confidence=confidence)
            return await self._lsp_delta(snapshot, edited, sent, wait_ms, confidence)
        except LuciankeError:
            raise
        except Exception as exc:
            _log.warning("diagnostics could not be collected: %s", exc, exc_info=True)
            return pending(
                edited, confidence=confidence, warnings=(f"diagnostics unavailable: {type(exc).__name__}",)
            )

    async def _structural(
        self, loaded: LoadedFile, sources: Mapping[RelPath, bytes], new_bytes: Mapping[RelPath, bytes]
    ) -> DiagDelta:
        def parsed_of(path: RelPath, data: bytes) -> ParsedFile | None:
            language_id = (
                loaded.language_id if path == loaded.rel else detect(path, data[:HEAD_BYTES]).language_id
            )
            spec = get_support(language_id)
            if spec is None or not callable(getattr(spec, "grammar", None)):
                return None
            return parse_source(data, language_id, _sha256(data))[0]

        def work() -> dict[RelPath, tuple[ParsedFile | None, ParsedFile | None]]:
            return {p: (parsed_of(p, sources[p]), parsed_of(p, new_bytes[p])) for p in new_bytes}

        return structural_delta(await asyncio.to_thread(work))

    async def _lsp_delta(
        self,
        snapshot: Snapshot,
        edited: Sequence[RelPath],
        sent: Mapping[RelPath, tuple[int | None, float | None]],
        wait_ms: int,
        confidence: Confidence,
    ) -> DiagDelta:
        assert self._hub is not None and self._documents is not None
        docs = self._documents
        edit_times = [t for _v, t in sent.values() if t is not None]
        edit_sent_at = min(edit_times) if edit_times else 0.0
        waits: dict[RelPath, tuple[int | None, float]] = {}
        for path in edited:
            version, sent_at = sent.get(path, (None, None))
            waits[path] = (version, sent_at if sent_at is not None else edit_sent_at)
        for ref in snapshot.referrers:
            # 参照元を LSP に見せる（開いていない文書にだけ診断を再発行しないサーバーが多い。S-4 / S-5）
            was_open = docs.is_open(ref)
            data = await asyncio.to_thread(self._read_now, to_abs(self._ws, ref))
            if data is None:
                continue
            language_id = detect(ref, data[:HEAD_BYTES]).language_id
            docs.sync(ref, data.removeprefix(b"\xef\xbb\xbf").decode("utf-8", "replace"), language_id)
            opened_at = None if was_open else docs.sent_at_of(ref)
            waits[ref] = (None, opened_at if opened_at is not None else edit_sent_at)
        results = await asyncio.gather(
            *(
                self._wait_sources(path, min_version=version, sent_at=sent_at, timeout_ms=wait_ms)
                for path, (version, sent_at) in waits.items()
            ),
            return_exceptions=True,
        )
        each = {path: got if isinstance(got, dict) else {} for path, got in zip(waits, results, strict=True)}
        fresh = {path: bool(got) and all(got.values()) for path, got in each.items()}
        # 揃った（``True``）時点で、その診断の index への書き込みは済んでいる（設計書 §7.7.6-4、EP-7）。
        after = await self._read_diagnostics([p for p, ok in fresh.items() if ok])
        return compute_delta(
            snapshot, after, fresh, confidence=confidence, unknown_baseline=_unknown_baseline(snapshot, each)
        )


def _unknown_baseline(
    snapshot: Snapshot, after: Mapping[RelPath, Mapping[str, bool]]
) -> dict[RelPath, frozenset[str]]:
    """編集前の基準が分からないサーバー（BUG-0098）: 編集の前後どちらかで待ったサーバーのうち、編集前に揃わなかったもの。

    編集前に揃ったサーバーと、どちらでも待たなかった source（索引の行・止まったサーバーの古い行。前後で同じ）は比べてよい。
    編集の前に無かったファイル（基準は空で確か）と、基準を確かめなかった呼び出し（``baseline is None``）は空。
    """
    if snapshot.failed:
        return {path: frozenset({ANY_LSP_SOURCE}) for path in after}
    if snapshot.baseline is None:
        return {}
    out: dict[RelPath, frozenset[str]] = {}
    for path, post in after.items():
        pre = snapshot.baseline.get(path)
        if pre is None:
            continue
        unknown = frozenset(s for s in (*pre, *post) if pre.get(s) is not True)
        if unknown:
            out[path] = unknown
    return out
