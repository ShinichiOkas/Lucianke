"""T-11 ``rename_symbol``（仕様書 §11.5 T-11、第 10 章）。LSP の ``rename`` で定義と全参照を一括で改名する。

流れ（仕様書 T-11 処理規約 1〜7）:

1. 引数の検査。``new_name`` が対象言語の識別子として Tree-sitter で解析できなければ ``E_INVALID_ARGUMENT``。
2. 対象ファイルの言語の LSP を ``LspPool.wait_ready`` で待つ。文法の無い言語（Tier 3・4）は ``E_UNSUPPORTED``、LSP が
   無い・``ready`` にならないときは ``E_LSP_UNAVAILABLE``。
3. ``EditTransaction.run`` に ``plan_builder`` を渡す。ビルダーは対象の名前トークンの位置（``selection.start``）で
   ``lsp.features.rename``（``prepareRename`` → ``rename``）を呼び、返った ``WorkspaceEdit`` を
   ``edit.workspace_edit.plan_from_workspace_edit`` で ``Plan`` にする（位置の単位は交渉した ``negotiated_encoding``）。
   丸めが要る位置は送らず ``E_LSP_UNAVAILABLE``（``details.reason="position_clamped"``）。編集ツールは降格しない。
4. 同じビルダーの中で、変更するファイルの **適用前後のシンボル** を Tree-sitter で抜き、位置の順に突き合わせて
   ``urn_map``（``qualified_name`` が変わったすべてのシンボルの旧 URN → 新 URN。改名したクラスのメンバを含む）を作る。
   ``dry_run`` でも予測の ``urn_map`` と変更箇所を返す。
5. ロック・前提検証（``expected_file_sha256s`` と ``resolved_shas``）・構文検証・ジャーナル・原子的書き込み・再索引・診断差分は
   トランザクションの仕事（全ファイルを書くか、1 つも書かないか）。

降格（仕様書 T-11「精度と降格」。計画は ``_t11_degrade`` / ``_t11_scope``）:

* ``heuristic`` / ``lexical_fallback``: 文法の無いファイル（Tier 4、文法の無い Tier 1）。LSP を使わず、同じ拡張子のファイル
  （``scope_paths`` 指定時はその配下）の単語境界の字句置換。``allow_heuristic_mutation`` が無ければ強制 ``dry_run``（トランザクションの
  降格インターロック）。``new_name`` が書き換え対象に単語として既にあれば ``E_MUTATION_BLOCKED``。
* ``structural``: 文法のある Python で LSP が使えず、対象が ``path`` + ``line`` + ``character`` で指す関数内のローカル変数・引数のとき、
  同一ファイル内の改名（MAY）。それ以外は LSP の失敗（``E_LSP_UNAVAILABLE`` など）をそのまま返す。
* Tier 3 と、文法の無い Tier 2 は ``E_UNSUPPORTED``。``scope_paths`` は LSP の経路では無視して ``warnings`` に書く。

ファイル改名（``RenameFile``）は writer が対応するまで ``E_UNSUPPORTED``（``allow_renames=False``）。

``ctx.services`` に求めるもの（duck typing。結線は合成ルート）:

* ``workspace(workspace_id)``・``lines``（``ToolServices``）。
* ``lsp_pool``: ``LspPool``（``status`` / ``wait_ready`` / ``get``）。
* ``edit_transaction(ws, *, lsp_status) -> EditTransaction``（呼べるものがあればこれを使う。``lsp_status`` は
  ``language_id -> LspStatus`` の関数。文書の同期と診断の待ちはファクトリが ``DocumentRouter`` 経由で言語の全サーバーに行う〈BUG-0023〉）。
  無ければ ``documents`` / ``diagnostics_hub`` を **LSP ハンドル自身のもの**〈``handle.documents`` / ``handle.diagnostics``〉にして、
  T-10 と同じ属性（``locks`` / ``writer_index`` /
  ``journal_dir`` と任意の ``indexer`` / ``watcher`` / ``reader_pool`` / ``trees`` / ``has_ctags`` / ``lock_wait_ms``）から組む。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import (
    Confidence,
    EngineChoice,
    LspStatus,
    Position,
    RelPath,
    SymbolRecord,
    TargetRef,
    Tier,
)
from lucianke.core.records import Record, ToolOutput
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import parse
from lucianke.edit.plan import Plan, apply_hunks
from lucianke.edit.transaction import EditResult, EditTransaction, PlanBuilder, PlanContext
from lucianke.edit.workspace_edit import new_positions, plan_from_workspace_edit
from lucianke.engine.selector import LEVEL_TEMPLATES, WarningReason, warning_text
from lucianke.lsp import features
from lucianke.lsp.pool import LspHandle
from lucianke.syntax.parsers import parse_source
from lucianke.syntax.support import get_support
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.tools import _t11_degrade
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._edit_output import diag_line, diagnostics_json, diagnostics_kv
from lucianke.tools._nav import TargetRefArg
from lucianke.tools._services import ToolServices, services_of
from lucianke.workspace.languages import HEAD_BYTES, detect
from lucianke.workspace.paths import confine, to_abs
from lucianke.workspace.types import Workspace

__all__ = ["SPEC", "TOOL_SPEC", "RenameSymbolArgs", "handler"]

LSP_READY_WAIT_MS = 8000  # LSP が起動中のときに待つ上限。超えたら E_LSP_UNAVAILABLE（reason=indexing）
_OPTIONAL_SERVICES = ("indexer", "watcher", "reader_pool", "trees", "has_ctags", "lock_wait_ms")
_IDENTIFIER_NODES = frozenset(
    {"identifier", "property_identifier", "type_identifier", "shorthand_property_identifier"}
)
_WORD = re.compile(r"\w+")


class RenameSymbolArgs(CommonArgs):
    target: TargetRefArg
    new_name: str
    scope_paths: list[str] | None = Field(
        default=None,
        description="Lexical (degraded) renames only.",
    )
    dry_run: bool = Field(default=False)
    allow_heuristic_mutation: bool = Field(
        default=False,
        description="Allow writing a degraded (heuristic) file.",
    )
    expected_file_sha256s: dict[str, str] | None = Field(
        default=None,
        description="path -> sha256 (or a 12+ prefix).",
    )
    diagnostics_wait_ms: int = Field(
        default=2000,
        ge=0,
        le=10000,
        description="ms to wait for diagnostics after the write.",
    )


# --- 引数 -------------------------------------------------------------------------------------


def _invalid(message: str, **details: object) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, message, details=dict(details))


def _target_of(args: RenameSymbolArgs) -> tuple[TargetRef, str]:
    """``TargetRef``（内部表現）と対象ファイルの相対パス（ユーザーの綴り）。"""
    try:
        target = TargetRef.from_json(args.target.model_dump(exclude_none=True))
        form, _ignored = target.resolution_form()
    except ValueError as exc:
        raise _invalid(f"target: {exc}", param="target") from None
    if form == "urn":
        assert target.urn is not None
        try:
            return target, str(parse(target.urn).path)
        except ValueError as exc:
            raise _invalid(f"target.urn: {exc}", param="target.urn") from None
    if form == "path":
        raise _invalid("target: name the symbol (symbol_name or line), not just a path", param="target")
    assert target.path is not None
    return target, target.path


def check_identifier(language_id: str, name: str) -> None:
    """``name`` が ``language_id`` の識別子として Tree-sitter で解析できなければ ``E_INVALID_ARGUMENT``（予約語を含む）。"""
    if not name or name[0].isdigit() or not re.fullmatch(r"[\w$]+", name):
        raise _invalid(
            f"new_name is not an identifier: {name!r}", param="new_name", reason="not_an_identifier"
        )
    data = name.encode("utf-8")
    parsed, _timed_out = parse_source(data, language_id, hashlib.sha256(data).hexdigest())
    root = parsed.tree.root_node
    node = root
    while not root.has_error and node.named_child_count == 1 and node.type not in _IDENTIFIER_NODES:
        node = node.named_children[0]
    ok = (
        not root.has_error
        and node.type in _IDENTIFIER_NODES
        and node.start_byte == 0
        and node.end_byte == len(data)
    )
    if not ok:
        raise _invalid(
            f"new_name is not a valid identifier in {language_id} (reserved word?): {name!r}",
            param="new_name",
            reason="not_an_identifier",
        )


# --- LSP ---------------------------------------------------------------------------------------


async def _lsp_handle(svc: ToolServices, ws: Workspace, language_id: str) -> LspHandle:
    pool = getattr(svc, "lsp_pool", None)
    if pool is None:
        raise LuciankeError(
            ErrorCode.E_LSP_UNAVAILABLE,
            "no language server pool is configured",
            details={"reason": "no_lsp_pool", "language": language_id},
        )
    status = pool.status(language_id, ws)
    if status.status is LspStatus.NOT_APPLICABLE:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"rename_symbol has no language server for {language_id!r} (without one only a function's local variable or parameter can be renamed; target it by path + line + character)",
            details={"reason": "no_lsp_for_language", "language": language_id},
        )
    await pool.wait_ready(language_id, ws, LSP_READY_WAIT_MS)
    handle: LspHandle | None = pool.get(language_id, ws, start_if_needed=False)
    if handle is None:
        now = pool.status(language_id, ws)
        raise LuciankeError(
            ErrorCode.E_LSP_UNAVAILABLE,
            f"the {language_id} language server is not ready ({now.reason or now.status.value})",
            details={
                "reason": now.reason or now.status.value,
                "server": now.server_name,
                "language": language_id,
                "progress": now.progress,
            },
        )
    return handle


def _transaction(svc: ToolServices, ws: Workspace, handle: LspHandle | None) -> EditTransaction:
    """``handle`` が無いとき（降格の経路）は ``lsp_status`` を ``unavailable`` にする。

    ``edit_transaction`` のファクトリがあれば ``lsp_status`` だけを渡す。文書の同期と診断の待ちは、ファクトリが
    ``DocumentRouter`` 経由でその言語の全サーバー（basedpyright と ruff）に行う（BUG-0023）。ハンドル 1 つの
    ``documents`` / ``diagnostics`` を渡すと、ruff に同期しない・待たない片手落ちになる。
    ファクトリが無いときだけ、従来どおり ``handle`` の ``documents`` / ``diagnostics`` で組む。
    """
    status = LspStatus.READY if handle is not None else LspStatus.UNAVAILABLE

    def lsp_status(_language_id: str) -> LspStatus:
        return status

    factory = getattr(svc, "edit_transaction", None)
    if callable(factory):
        tx: EditTransaction = factory(ws, lsp_status=lsp_status)
        return tx
    overrides: dict[str, Any] = (
        {"documents": handle.documents, "diagnostics_hub": handle.diagnostics} if handle is not None else {}
    )
    overrides["lsp_status"] = lsp_status
    missing = [n for n in ("locks", "writer_index", "journal_dir") if getattr(svc, n, None) is None]
    if missing:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            "editing is not available in this build (the edit services are not wired)",
            details={"reason": "edit_services_missing", "missing": missing},
        )
    optional = {n: getattr(svc, n) for n in _OPTIONAL_SERVICES if getattr(svc, n, None) is not None}
    return EditTransaction(
        ws,
        locks=svc.locks,  # type: ignore[attr-defined]
        writer_index=svc.writer_index,  # type: ignore[attr-defined]
        journal_dir=svc.journal_dir,  # type: ignore[attr-defined]
        **optional,
        **overrides,
    )


# --- 計画（ビルダー） ----------------------------------------------------------------------------


@dataclass(slots=True)
class _Site:
    """変更箇所 1 件（適用後の座標。行・列は 0 始まり）。"""

    path: RelPath
    position: Position
    is_definition: bool
    snippet: str
    use: str
    enclosing_urn: str | None
    enclosing_name: str | None


@dataclass(slots=True)
class _Seen:
    """ビルダーが作って応答に使うもの（同じ読み込みから取るので、読み直しの競合がない）。"""

    old_name: str = ""
    sites: list[_Site] = field(default_factory=list)
    urn_map: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    language_id: str = "n/a"
    tier: Tier = Tier.T1
    confidence: Confidence = Confidence.EXACT
    engine: EngineChoice | None = None  # 降格経路のエンジン（LSP の経路では None）
    site_confidence: Confidence | None = None  # 降格経路の変更箇所の水準（LSP の経路では None）


def _identifier_at(index: LineIndex, pos: Position) -> str | None:
    if pos.line >= index.line_count:
        return None
    for m in _WORD.finditer(index.line_text(pos.line)):
        if m.start() <= pos.col <= m.end():
            return m.group()
    return None


def _name_position(ctx: PlanContext, target: TargetRef) -> tuple[Position, str]:
    """LSP に送る位置（名前トークンの先頭）と、いまの名前。``def`` の列は送らない。"""
    index = LineIndex(ctx.source)
    if target.line is not None and target.character is not None:
        pos = Position(target.line, target.character)
        name = _identifier_at(index, pos)
        if name is None:
            raise LuciankeError(
                ErrorCode.E_TARGET_NOT_FOUND,
                f"no identifier at {ctx.path}:{target.line + 1}:{target.character + 1}",
                details={"path": str(ctx.path), "line": target.line + 1, "character": target.character + 1},
            )
        start = pos
        text = index.line_text(pos.line)
        while start.col > 0 and (text[start.col - 1].isalnum() or text[start.col - 1] == "_"):
            start = Position(start.line, start.col - 1)
        return start, name
    sym = ctx.symbol
    assert sym is not None
    if sym.selection is not None:
        return sym.selection.start, sym.name
    pattern = re.compile(rf"(?<!\w){re.escape(sym.name)}(?!\w)")
    last = min(sym.range.end.line, sym.range.start.line + 3)
    for line in range(sym.range.start.line, last + 1):
        m = pattern.search(index.line_text(line))
        if m is not None:
            return Position(line, m.start()), sym.name
    raise LuciankeError(
        ErrorCode.E_TARGET_NOT_FOUND,
        f"cannot locate the name of {sym.qualified_name} in {ctx.path}",
        details={"path": str(ctx.path), "symbol_name": sym.name},
    )


def _symbols_of(ws: Workspace, rel: RelPath, data: bytes) -> list[SymbolRecord] | None:
    """``data`` のシンボル（開始位置順）。文法の無いファイルは ``None``。"""
    language_id = detect(rel, data[:HEAD_BYTES]).language_id
    spec = get_support(language_id)
    if spec is None or not callable(getattr(spec, "grammar", None)):
        return None
    parsed, _timed_out = parse_source(data, language_id, hashlib.sha256(data).hexdigest())
    try:
        found = extract(
            parsed,
            language_id=language_id,
            language=spec.grammar(),  # pyright: ignore[reportAttributeAccessIssue]
            query_source=load_query_source(language_id),
            workspace_id=ws.workspace_id,
            rel=rel,
        )
    except ValueError:
        return None
    return sorted(
        found,
        key=lambda s: (
            s.range.start.line,
            s.range.start.col,
            s.range.end.line,
            s.range.end.col,
            s.kind.value,
        ),
    )


def _use_of(line_text: str, col: int, length: int) -> str:
    stripped = line_text.lstrip()
    if stripped.startswith(("import ", "from ")):
        return "import"
    rest = line_text[col + length :].lstrip()
    return "call" if rest.startswith("(") else "r"


def _innermost(symbols: Sequence[SymbolRecord], pos: Position) -> SymbolRecord | None:
    inside = [s for s in symbols if s.range.start <= pos <= s.range.end]
    if not inside:
        return None
    return min(
        inside,
        key=lambda s: (s.range.end.line - s.range.start.line, s.range.end.col - s.range.start.col),
    )


def _analyse(
    ws: Workspace,
    plan: Plan,
    sources: dict[RelPath, bytes],
    seen: _Seen,
    ctx: PlanContext,
    name_pos: Position,
) -> None:
    """適用前後のシンボルから ``urn_map`` を、新しい座標から変更箇所を作る（スレッドで呼ぶ）。"""
    for path in plan.hunk_paths:
        before_src = sources[path]
        hunks = plan.hunks_for(path)
        after_src = apply_hunks(before_src, hunks)
        after_index = LineIndex(after_src)
        before = _symbols_of(ws, path, before_src)
        after = _symbols_of(ws, path, after_src)
        if before is not None and after is not None:
            if len(before) == len(after) and all(
                o.kind == n.kind for o, n in zip(before, after, strict=True)
            ):
                for o, n in zip(before, after, strict=True):
                    if str(o.urn) != str(n.urn):
                        seen.urn_map[str(o.urn)] = str(n.urn)
            else:
                seen.warnings.append(f"urn_map is incomplete for {path}: its symbols changed shape")
        for hunk, pos in new_positions(before_src, hunks):
            text = after_index.line_text(pos.line) if pos.line < after_index.line_count else ""
            is_def = path == ctx.path and hunk.range.start == name_pos
            enclosing = _innermost(after, pos) if after else None
            if is_def:
                enclosing = None
            seen.sites.append(
                _Site(
                    path=path,
                    position=pos,
                    is_definition=is_def,
                    snippet=text.strip(),
                    use="def" if is_def else _use_of(text, pos.col, len(hunk.new_text)),
                    enclosing_urn=str(enclosing.urn) if enclosing is not None else None,
                    enclosing_name=enclosing.qualified_name if enclosing is not None else None,
                )
            )


def _ensure_unmodified_since(ws: Workspace, paths: Sequence[RelPath], started_ns: int) -> None:
    """要求の開始より後に更新されたファイルがあれば ``E_PRECONDITION_FAILED``（best-effort）。

    LSP は自分の見ている内容で位置を決める。LSP の応答のあとで読んだ内容は、LSP が見た内容とは限らない（外部の保存が
    要求の途中に入ると、位置が別の場所を指す）。更新時刻が要求の開始より後なら、位置を信用せずに失敗する。
    """
    for path in paths:
        try:
            mtime = to_abs(ws, path).stat().st_mtime_ns
        except OSError:
            continue
        if mtime > started_ns:
            raise LuciankeError(
                ErrorCode.E_PRECONDITION_FAILED,
                f"file changed while the language server computed the rename: {path}",
                details={"path": str(path), "reason": "modified_during_rename"},
            )


def _rename_builder(
    ws: Workspace, handle: LspHandle, target: TargetRef, args: RenameSymbolArgs, seen: _Seen
) -> PlanBuilder:
    async def build(ctx: PlanContext) -> Plan:
        seen.language_id, seen.tier, seen.confidence = ctx.language_id, ctx.tier, ctx.confidence
        if ctx.symbol is None:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"rename_symbol needs a syntax tree and a resolved symbol: {ctx.path}",
                details={"reason": "no_symbol", "path": str(ctx.path), "language_id": ctx.language_id},
            )
        name_pos, old_name = _name_position(ctx, target)
        seen.old_name = old_name
        if old_name == args.new_name:
            raise _invalid("new_name is the same as the current name", param="new_name", reason="same_name")
        started_ns = time.time_ns()
        try:
            we = await features.rename(handle, ctx.path, name_pos, args.new_name)
        except TimeoutError:
            raise LuciankeError(
                ErrorCode.E_TIMEOUT,
                "the language server did not answer rename in time",
                details={"reason": "lsp_rename_timeout", "path": str(ctx.path)},
            ) from None
        client = handle.client
        encoding = client.negotiated_encoding.value if client is not None else "utf-16"
        plan = await plan_from_workspace_edit(
            ws, we, encoding=encoding, documents=handle.documents, target_urns=(str(ctx.symbol.urn),)
        )
        await asyncio.to_thread(_ensure_unmodified_since, ws, plan.touched_paths, started_ns)
        if not plan.hunks:
            raise LuciankeError(
                ErrorCode.E_LSP_UNAVAILABLE,
                "the language server returned no edits for this rename",
                details={"reason": "empty_workspace_edit", "path": str(ctx.path)},
            )
        if plan.resolved_shas.get(ctx.path, ctx.sha256) != ctx.sha256:
            raise LuciankeError(
                ErrorCode.E_PRECONDITION_FAILED,
                f"file changed while the rename was computed: {ctx.path}",
                details={
                    "path": str(ctx.path),
                    "expected": ctx.sha256,
                    "actual": plan.resolved_shas.get(ctx.path),
                },
            )
        sources: dict[RelPath, bytes] = {}
        for path in plan.hunk_paths:
            data = await asyncio.to_thread(lambda p=path: to_abs(ws, p).read_bytes())
            if hashlib.sha256(data).hexdigest() != plan.resolved_shas.get(path):
                raise LuciankeError(
                    ErrorCode.E_PRECONDITION_FAILED,
                    f"file changed while the rename was computed: {path}",
                    details={"path": str(path), "expected": plan.resolved_shas.get(path)},
                )
            sources[path] = data
        await asyncio.to_thread(_analyse, ws, plan, sources, seen, ctx, name_pos)
        return plan

    return build


def _degraded_builder(
    ws: Workspace,
    target: TargetRef,
    args: RenameSymbolArgs,
    seen: _Seen,
    mode: str,
    lsp_error: LuciankeError | None,
) -> PlanBuilder:
    """降格の経路（``lexical`` か ``structural``）の計画。``structural`` で対象にならなければ ``lsp_error`` をそのまま返す。"""

    async def build(ctx: PlanContext) -> Plan:
        seen.language_id, seen.tier, seen.confidence = ctx.language_id, ctx.tier, ctx.confidence
        if mode == "lexical":
            got = await _t11_degrade.lexical_plan(
                ctx,
                target=target,
                new_name=args.new_name,
                scope_paths=args.scope_paths,
                allow_heuristic_mutation=args.allow_heuristic_mutation,
                dry_run=args.dry_run,
                expected_file_sha256s=args.expected_file_sha256s,
            )
            seen.engine = EngineChoice(
                engine=_t11_degrade.LEXICAL_ENGINE,
                confidence=Confidence.LEXICAL_FALLBACK,
                lsp_status=LspStatus.NOT_APPLICABLE.value,
                warning=LEVEL_TEMPLATES[Confidence.LEXICAL_FALLBACK] + _t11_degrade.LEXICAL_WARNING_EXTRA,
            )
        else:
            structural = await _t11_degrade.structural_plan(ctx, target=target, new_name=args.new_name)
            if structural is None:
                assert lsp_error is not None
                raise lsp_error
            got = structural
            seen.engine = EngineChoice(
                engine="tree-sitter",
                confidence=Confidence.STRUCTURAL,
                lsp_status=LspStatus.UNAVAILABLE.value,
                warning=warning_text(WarningReason.LSP_UNAVAILABLE),
            )
            if args.scope_paths:
                seen.warnings.append("scope_paths ignored: a structural rename stays inside one function")
        seen.site_confidence = seen.engine.confidence
        seen.old_name = got.old_name
        seen.warnings.extend(got.warnings)
        await asyncio.to_thread(_analyse, ws, got.plan, got.sources, seen, ctx, got.name_pos)
        return got.plan

    return build


# --- 応答 -------------------------------------------------------------------------------------


def _edit_record(svc: ToolServices, site: _Site, confidence: Confidence) -> Record:
    line, col = site.position.line + 1, site.position.col + 1
    text = svc.lines.ref_line(
        str(site.path),
        line,
        col,
        use=None if site.is_definition else site.use,
        enclosing=None if site.is_definition else site.enclosing_name,
        snippet=site.snippet,
    )
    return Record(
        kind="ref",
        key=f"{site.path}:{line}:{col}",
        priority=0 if site.is_definition else 1,
        lines=(text,),
        data={
            "path": str(site.path),
            "line": line,
            "character": col,
            "use": site.use,
            "enclosing_urn": site.enclosing_urn,
            "snippet_after": site.snippet,
        },
        confidence=confidence,
        group=str(site.path),
    )


def _summarize(rest: Sequence[Record]) -> str:
    counts: dict[str, int] = {}
    for rec in rest:
        counts[rec.group or "?"] = counts.get(rec.group or "?", 0) + 1
    shown = ", ".join(
        f"{path} ({n})" for path, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    )
    return f"{len(rest)} more edits in {len(counts)} files: {shown}"


def _build_output(svc: ToolServices, args: RenameSymbolArgs, result: EditResult, seen: _Seen) -> ToolOutput:
    conf = seen.site_confidence or result.confidence
    delta = result.new_diagnostics
    edits_count = len(seen.sites)
    ordered = sorted(
        seen.sites, key=lambda s: (not s.is_definition, str(s.path), s.position.line, s.position.col)
    )
    records = [_edit_record(svc, s, conf) for s in ordered]
    files = [
        {
            "path": str(c.path),
            "sha256_before": c.sha256_before,
            "sha256_after": c.sha256_after,
            "hunks": c.hunks,
            "renamed_from": str(c.renamed_from) if c.renamed_from is not None else None,
        }
        for c in result.files_changed
    ]
    kv: list[tuple[str, str]] = [
        (
            "applied",
            f"{str(result.applied).lower()}  edit_id={result.edit_id or '-'}  {seen.old_name} -> {args.new_name}",
        ),
        ("files_changed", f"{len(result.files_changed)} files, {edits_count} edits"),
    ]
    kv += [("urn_map", f"{old} -> {new}") for old, new in seen.urn_map.items()]
    kv.append(("syntax_check", result.syntax_check))
    kv.append(("new_diagnostics", diagnostics_kv(delta)))
    kv += [("added", diag_line(svc, o)) for o in delta.added]
    kv += [("present_after", diag_line(svc, o)) for o in delta.present_after]
    kv += [("resolved", diag_line(svc, o)) for o in delta.resolved]

    return ToolOutput(
        records=records,
        head_kv=kv,
        data_static={
            "applied": result.applied,
            "edit_id": result.edit_id,
            "old_name": seen.old_name,
            "new_name": args.new_name,
            "edits_count": edits_count,
            "urn_map": dict(seen.urn_map),
            "files_changed": files,
            "diff": result.diff,
            "syntax_check": result.syntax_check,
            "new_diagnostics": diagnostics_json(delta),
        },
        max_confidence=Confidence.EXACT,
        engine=seen.engine or EngineChoice(engine="lsp", confidence=conf, lsp_status=LspStatus.READY.value),
        page_unit="edit",
        summarize=_summarize,
        tier=seen.tier,
        detected_language=seen.language_id,
        total_estimate=edits_count,
        warnings=(*seen.warnings, *result.warnings, *delta.warnings),
    )


# --- ハンドラ -----------------------------------------------------------------------------------


async def handler(ctx: ToolContext, args: RenameSymbolArgs) -> ToolOutput:
    svc = services_of(ctx.services)
    ws = svc.workspace(args.workspace_id)
    target, user_path = _target_of(args)
    rel = confine(ws, user_path, for_write=True, param="target")
    try:
        head = await asyncio.to_thread(lambda: to_abs(ws, rel).open("rb").read(HEAD_BYTES))
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND, f"file not found: {rel}", details={"path": str(rel)}
        ) from None
    det = detect(rel, head)
    spec = get_support(det.language_id)
    has_grammar = spec is not None and callable(getattr(spec, "grammar", None))
    if not det.index_content or det.tier is Tier.T3 or (not has_grammar and det.tier is Tier.T2):
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"rename_symbol supports code files with a grammar and a language server; {rel} is {det.language_id!r} "
            f"(tier {int(det.tier)})",
            details={"reason": "tier_not_supported", "language_id": det.language_id, "tier": int(det.tier)},
        )
    seen = _Seen()
    # 文法の無いファイル（Tier 4、文法の無い Tier 1）: 単語境界の字句置換（LSP は使わない）
    if not has_grammar:
        _t11_degrade.check_lexical_identifier(args.new_name)
        return await _run_degraded(svc, ws, args, target, seen, "lexical", None)
    check_identifier(det.language_id, args.new_name)
    if args.scope_paths:
        seen.warnings.append(
            "scope_paths ignored: the language server renames every reference in the workspace"
        )
    try:
        handle = await _lsp_handle(svc, ws, det.language_id)
    except LuciankeError as exc:
        # LSP が使えない。参照が 1 つの関数に閉じるローカル変数・引数なら、構文木だけで同一ファイル内を改名する（structural）
        if (
            _lsp_is_down(exc)
            and det.language_id == "python"
            and target.line is not None
            and target.character is not None
        ):
            seen.warnings.clear()
            return await _run_degraded(svc, ws, args, target, seen, "structural", exc)
        raise
    tx = _transaction(svc, ws, handle)
    run: Callable[..., Any] = tx.run
    result: EditResult = await run(
        _rename_builder(ws, handle, target, args, seen),
        target=target,
        dry_run=args.dry_run,
        allow_heuristic_mutation=args.allow_heuristic_mutation,
        expected_shas=args.expected_file_sha256s,
        diagnostics_wait_ms=args.diagnostics_wait_ms,
        revision=args.revision,
    )
    return _build_output(svc, args, result, seen)


def _lsp_is_down(exc: LuciankeError) -> bool:
    """LSP が使えないための失敗か（降格してよい失敗）。"""
    if exc.code is ErrorCode.E_LSP_UNAVAILABLE:
        return True
    return exc.code is ErrorCode.E_UNSUPPORTED and exc.details.get("reason") == "no_lsp_for_language"


async def _run_degraded(
    svc: ToolServices,
    ws: Workspace,
    args: RenameSymbolArgs,
    target: TargetRef,
    seen: _Seen,
    mode: str,
    lsp_error: LuciankeError | None,
) -> ToolOutput:
    """LSP を使わない経路（``lexical`` か ``structural``）。降格インターロックはトランザクションが行う（文法の無いファイルは強制 dry_run）。"""
    tx = _transaction(svc, ws, None)
    run: Callable[..., Any] = tx.run
    result: EditResult = await run(
        _degraded_builder(ws, target, args, seen, mode, lsp_error),
        target=target,
        dry_run=args.dry_run,
        allow_heuristic_mutation=args.allow_heuristic_mutation,
        expected_shas=args.expected_file_sha256s,
        diagnostics_wait_ms=0,  # この経路では LSP の診断を待たない（待っても来ない）
        revision=args.revision,
    )
    return _build_output(svc, args, result, seen)


TOOL_SPEC = ToolSpec(
    name="rename_symbol",
    tid="T-11",
    args_model=RenameSymbolArgs,
    handler=handler,
    default_max_tokens=1500,
    description=(
        "Rename a symbol and all its references via the language server, atomically. Returns changes, urn_map (old -> new URN) and new diagnostics. Needs a ready language server (E_LSP_UNAVAILABLE); except a Python local variable/parameter (path + line + character) is renamed structurally, and a file without a grammar gets a word-boundary replacement in same-extension files (dry_run unless allow_heuristic_mutation and expected_file_sha256s are given). expected_file_sha256s refuses a stale write; dry_run previews."
    ),
    writes=True,
    max_confidence=Confidence.EXACT,
    milestone="M1",
    page_unit="edit",
    records_key="edits",
)
SPEC = TOOL_SPEC
