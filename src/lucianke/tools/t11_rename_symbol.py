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

実装していないもの（仕様書 T-11「精度と降格」）: ``structural``（同一ファイル内のローカル変数・引数の改名、MAY）と
``heuristic`` / ``lexical_fallback``（単語境界の字句置換、``scope_paths``、``E_MUTATION_BLOCKED``）。LSP が使えなければ
``E_LSP_UNAVAILABLE``、文法の無い対象は ``E_UNSUPPORTED``。``scope_paths`` は無視して ``warnings`` に書く。
ファイル改名（``RenameFile``）は writer が対応するまで ``E_UNSUPPORTED``（``allow_renames=False``）。

``ctx.services`` に求めるもの（duck typing。結線は合成ルート）:

* ``workspace(workspace_id)``・``lines``（``ToolServices``）。
* ``lsp_pool``: ``LspPool``（``status`` / ``wait_ready`` / ``get``）。
* ``edit_transaction(ws, *, documents, diagnostics_hub, lsp_status) -> EditTransaction``（呼べるものがあればこれを使う。
  ``documents`` / ``diagnostics_hub`` は **LSP ハンドル自身のもの**〈``handle.documents`` / ``handle.diagnostics``〉で、
  ``lsp_status`` は ``language_id -> LspStatus`` の関数）。無ければ T-10 と同じ属性（``locks`` / ``writer_index`` /
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
from lucianke.lsp import features
from lucianke.lsp.pool import LspHandle
from lucianke.syntax.parsers import parse_source
from lucianke.syntax.support import get_support
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
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
_SEVERITY_LETTER = {"error": "E", "warning": "W", "information": "I", "info": "I", "hint": "H"}
_WORD = re.compile(r"\w+")


class RenameSymbolArgs(CommonArgs):
    target: TargetRefArg = Field(
        description="The symbol to rename (urn, or path + symbol_name, or path + line [+ character])."
    )
    new_name: str = Field(description="The new identifier.")
    scope_paths: list[str] | None = Field(
        default=None,
        description="Only for degraded (lexical) renames; ignored when the language server is used.",
    )
    dry_run: bool = Field(
        default=False, description="Do not write; return the planned edits and the urn_map."
    )
    allow_heuristic_mutation: bool = Field(
        default=False,
        description="Allow writing in degraded (heuristic / lexical_fallback) mode. Not used for exact renames.",
    )
    expected_file_sha256s: dict[str, str] | None = Field(
        default=None,
        description="path -> sha256 (64 hex, or a prefix of 12+) of the files as you read them. Mismatch is E_PRECONDITION_FAILED.",
    )
    diagnostics_wait_ms: int = Field(
        default=2000,
        ge=0,
        le=10000,
        description="How long to wait for the language server's diagnostics after the write.",
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
            f"rename_symbol has no language server for {language_id!r} (the lexical fallback is not implemented)",
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


def _transaction(svc: ToolServices, ws: Workspace, handle: LspHandle) -> EditTransaction:
    overrides: dict[str, Any] = {
        "documents": handle.documents,
        "diagnostics_hub": handle.diagnostics,
        "lsp_status": lambda _language_id: LspStatus.READY,
    }
    factory = getattr(svc, "edit_transaction", None)
    if callable(factory):
        tx: EditTransaction = factory(ws, **overrides)
        return tx
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


# --- 応答 -------------------------------------------------------------------------------------


def _diag_kv(svc: ToolServices, obj: dict[str, Any]) -> str:
    sev = _SEVERITY_LETTER.get(str(obj.get("severity", "")).lower(), "I")
    source, code = obj.get("source"), obj.get("code")
    pair: dict[str, Any] = {}
    if source:
        pair = {"source": str(source), "code": str(code) if code else "-"}
    return svc.lines.diag_line(
        sev,
        str(obj["path"]),
        int(obj["start_line"]),
        int(obj["start_col"]),
        message=str(obj["message"]),
        **pair,
    )


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
    conf = result.confidence
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
    if delta.status == "not_computed":
        kv.append(("new_diagnostics", "not_computed"))
    else:
        kv.append(
            (
                "new_diagnostics",
                f"{delta.status} (+{len(delta.added)} -{len(delta.resolved)}) checked={len(delta.checked_files)} files",
            )
        )
    kv += [("added", _diag_kv(svc, dict(o))) for o in delta.added]
    kv += [("resolved", _diag_kv(svc, dict(o))) for o in delta.resolved]

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
            "new_diagnostics": {
                "status": delta.status,
                "added": [dict(o) for o in delta.added],
                "resolved": [dict(o) for o in delta.resolved],
                "checked_files": [dict(o) for o in delta.checked_files],
            },
        },
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="lsp", confidence=conf, lsp_status=LspStatus.READY.value),
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
    if (
        not det.index_content
        or det.tier > Tier.T2
        or spec is None
        or not callable(getattr(spec, "grammar", None))
    ):
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"rename_symbol supports code files with a grammar and a language server; {rel} is {det.language_id!r} "
            f"(tier {int(det.tier)})",
            details={"reason": "tier_not_supported", "language_id": det.language_id, "tier": int(det.tier)},
        )
    check_identifier(det.language_id, args.new_name)
    seen = _Seen()
    if args.scope_paths:
        seen.warnings.append(
            "scope_paths ignored: the language server renames every reference in the workspace"
        )
    handle = await _lsp_handle(svc, ws, det.language_id)
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


TOOL_SPEC = ToolSpec(
    name="rename_symbol",
    tid="T-11",
    args_model=RenameSymbolArgs,
    handler=handler,
    default_max_tokens=1500,
    description=(
        "Rename a symbol and every reference to it across the workspace with the language server (prepareRename + rename), "
        "atomically: all files are written or none. Does not touch same-named symbols in other scopes, comments or strings. "
        "Target by urn, or path + symbol_name, or path + line [+ character]. Returns every changed site, the urn_map "
        "(old URN -> new URN for the symbol and its members) and the new diagnostics. Pass expected_file_sha256s to refuse "
        "a stale write; use dry_run to preview. Needs a ready language server (E_LSP_UNAVAILABLE otherwise)."
    ),
    writes=True,
    max_confidence=Confidence.EXACT,
    milestone="M1",
    page_unit="edit",
    records_key="edits",
)
SPEC = TOOL_SPEC
