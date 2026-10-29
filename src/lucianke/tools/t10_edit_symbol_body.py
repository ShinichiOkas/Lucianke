"""T-10 ``edit_symbol_body``（仕様書 §11.5 T-10、第 10 章）。シンボルを単位にコードを置換し、同じ応答で構文妥当性と診断差分を返す。

範囲: 5 操作すべて（Python・TypeScript・JavaScript など文法のある言語）。ビルダーは ``replace_body`` が ``edit/builders.py``、
``insert_before`` / ``insert_after`` / ``insert_member`` が ``edit/builders_insert.py``、``delete_symbol`` が
``edit/builders_delete.py``。
Tier 2・3（セレクタ）は M2。

編集そのもの（ロック・前提検証・降格インターロック・構文検証・ジャーナル・書き込み・再索引・診断差分）は
``edit.transaction.EditTransaction`` の仕事で、このモジュールは引数の検証、``TargetRef`` の組み立て、ビルダーの選択、
応答（compact の kv 行と json の ``data``）への整形だけを行う。

``ctx.services`` に求めるもの（結線は合成ルート。このモジュールはどちらの形も受け付ける）:

* ``edit_transaction(ws) -> EditTransaction``（呼べるものがあればこれを使う）。無ければ、次の属性から組む:
* 必須: ``locks``（``WorkspaceLocks``）、``writer_index``（``IndexWriter``）、``journal_dir``（``Path``）
* 任意（無い・``None`` なら LSP / 索引 / 監視なしとして動く）: ``indexer``、``watcher``、``documents``、``diagnostics_hub``、
  ``reader_pool``、``trees``、``lsp_status``、``has_ctags``、``lock_wait_ms``
* それに加えて、他のツールと同じ ``workspace(workspace_id)``・``lines``（compact の行の組み立て）を持つ ``ToolServices``。

精度: 最高水準は ``structural``（LSP 不在でも降格ではない。仕様書 §10.5「structural 水準での T-10 は通常どおり」）。
応答の水準は、トランザクションが選んだ水準（LSP が使えれば ``exact``）をそのまま使う。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice, LspStatus, Range, TargetRef, Tier
from lucianke.core.records import Record, ToolOutput
from lucianke.core.textpos import detect_indent
from lucianke.core.urn import parse
from lucianke.edit.builders import replace_body_builder
from lucianke.edit.builders_delete import delete_symbol_builder
from lucianke.edit.builders_insert import INSERT_OPERATIONS, InsertOutcome, insert_builder, relocate
from lucianke.edit.normalize import default_indent
from lucianke.edit.transaction import EditResult, EditTransaction, PlanBuilder, PlanContext
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._services import ToolServices, estimate_tokens, services_of
from lucianke.tools.t05_get_symbol_bodies import TargetRefArg

__all__ = ["SPEC", "EditSymbolBodyArgs", "handler"]

Operation = Literal["replace_body", "insert_before", "insert_after", "insert_member", "delete_symbol"]

_REQUIRED_SERVICES = ("locks", "writer_index", "journal_dir")
_OPTIONAL_SERVICES = (
    "indexer",
    "watcher",
    "documents",
    "diagnostics_hub",
    "reader_pool",
    "trees",
    "lsp_status",
    "has_ctags",
    "lock_wait_ms",
)
_SEVERITY_LETTER = {"error": "E", "warning": "W", "information": "I", "info": "I", "hint": "H"}
_SHA_SHOWN = 12


class EditSymbolBodyArgs(CommonArgs):
    target: TargetRefArg = Field(
        description="The symbol to edit (urn, or path + symbol_name, or path + line)."
    )
    operation: Operation = Field(
        description="replace_body | insert_before | insert_after | insert_member | delete_symbol. "
        "replace_body takes the WHOLE definition (signature included).",
    )
    new_code: str | None = Field(
        default=None,
        description="The code to put in (required unless delete_symbol). Indentation is normalized by the server.",
    )
    position: Literal["start", "end"] = Field(
        default="end", description="insert_member only: start or end of the body."
    )
    dry_run: bool = Field(
        default=False, description="Do not write; return the diff and the syntax check only."
    )
    allow_heuristic_mutation: bool = Field(
        default=False, description="Allow writing to a degraded (heuristic / lexical_fallback) file."
    )
    expected_file_sha256: str | None = Field(
        default=None,
        description="sha256 of the file as you read it (64 hex, or a prefix of 12+). Mismatch is E_PRECONDITION_FAILED.",
    )
    diagnostics_wait_ms: int = Field(
        default=2000,
        ge=0,
        le=10000,
        description="How long to wait for the language server's diagnostics after the write.",
    )


# --- 引数の検査と TargetRef ---------------------------------------------------------------------


def _invalid(message: str, **details: object) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, message, details=details)


def _target_of(args: EditSymbolBodyArgs) -> tuple[TargetRef, str | None]:
    """``TargetRef``（内部表現）と、対象ファイルの相対パス（``expected_file_sha256`` の鍵）。"""
    try:
        target = TargetRef.from_json(args.target.model_dump(exclude_none=True))
        form, _ignored = target.resolution_form()
    except ValueError as exc:
        raise _invalid(f"target: {exc}", param="target") from None
    if form == "path":
        raise _invalid(
            "target must name a symbol: give urn, or path with symbol_name or line", param="target"
        )
    if form == "urn":
        assert target.urn is not None
        try:
            return target, str(parse(target.urn).path)
        except ValueError as exc:
            raise _invalid(f"target.urn: {exc}", param="target.urn") from None
    return target, target.path


def _check_arguments(args: EditSymbolBodyArgs) -> None:
    if args.operation != "delete_symbol" and (args.new_code is None or not args.new_code.strip()):
        raise _invalid(
            f"new_code is required for operation {args.operation}",
            param="new_code",
            reason="missing" if args.new_code is None else "empty",
        )
    if args.operation == "delete_symbol" and args.new_code is not None:
        raise _invalid("new_code must not be given for delete_symbol", param="new_code", reason="unexpected")


def _plan_builder(
    args: EditSymbolBodyArgs, target: TargetRef, outcome: InsertOutcome | None = None
) -> PlanBuilder:
    if args.operation == "replace_body":
        assert args.new_code is not None
        return replace_body_builder(target, args.new_code)
    if args.operation in INSERT_OPERATIONS:
        assert args.new_code is not None
        return insert_builder(args.operation, target, args.new_code, position=args.position, outcome=outcome)
    if args.operation == "delete_symbol":
        return delete_symbol_builder(target)
    raise LuciankeError(
        ErrorCode.E_UNSUPPORTED,
        f"operation {args.operation} is not implemented",
        details={"reason": "operation_not_implemented", "operation": args.operation},
    )


# --- トランザクション -----------------------------------------------------------------------------


def _transaction(svc: ToolServices, ws: Any) -> EditTransaction:
    factory = getattr(svc, "edit_transaction", None)
    if callable(factory):
        return factory(ws)
    missing = [name for name in _REQUIRED_SERVICES if getattr(svc, name, None) is None]
    if missing:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            "editing is not available in this build (the edit services are not wired)",
            details={"reason": "edit_services_missing", "missing": missing},
        )
    optional = {
        name: getattr(svc, name) for name in _OPTIONAL_SERVICES if getattr(svc, name, None) is not None
    }
    return EditTransaction(
        ws,
        locks=svc.locks,  # type: ignore[attr-defined]
        writer_index=svc.writer_index,  # type: ignore[attr-defined]
        journal_dir=svc.journal_dir,  # type: ignore[attr-defined]
        **optional,
    )


@dataclass(slots=True)
class _Seen:
    """ビルダーに渡った文脈のうち、応答に要るもの（同じ読み込みから取るので、読み直しの競合がない）。"""

    indent_unit: str = "4sp"
    language_id: str = "n/a"
    tier: Tier = Tier.T1
    confidence: Confidence = Confidence.STRUCTURAL
    extra: dict[str, Any] = field(default_factory=dict)


def _indent_name(source: bytes, language_id: str) -> str:
    spec = detect_indent(source) or default_indent(language_id)
    return "tab" if spec.kind == "tab" else f"{spec.width}sp"


def _guarded(inner: PlanBuilder, args: EditSymbolBodyArgs, seen: _Seen) -> PlanBuilder:
    """ビルダーを包む。文脈を記録し、``lexical_fallback`` への実書き込みには ``expected_file_sha256`` を要求する（仕様書 T-10 精度と降格）。"""

    async def build(ctx: PlanContext) -> Any:
        seen.indent_unit = _indent_name(ctx.source, ctx.language_id)
        seen.language_id, seen.tier, seen.confidence = ctx.language_id, ctx.tier, ctx.confidence
        if (
            ctx.confidence is Confidence.LEXICAL_FALLBACK
            and args.allow_heuristic_mutation
            and not args.dry_run
            and not args.expected_file_sha256
        ):
            raise _invalid(
                "writing to a lexical_fallback file needs expected_file_sha256",
                param="expected_file_sha256",
                reason="required_for_lexical_fallback",
            )
        return await inner(ctx)

    return build


# --- 応答の整形 -----------------------------------------------------------------------------------


def _diff_counts(diff: str) -> tuple[int, int]:
    plus = minus = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return plus, minus


def _range_json(rng: Range | None) -> dict[str, int] | None:
    if rng is None:
        return None
    start, end = rng.line_span_external()
    return {"start_line": start, "end_line": end}


def _diag_line(svc: ToolServices, obj: dict[str, Any]) -> str:
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


def _diff_record(svc: ToolServices, diff: str, confidence: Confidence) -> Record:
    rows = diff.rstrip("\n").split("\n")
    hunk_starts = [i for i, row in enumerate(rows) if row.startswith("@@")]

    def make(count: int | None) -> Record:
        shown = rows if count is None or count >= len(hunk_starts) else rows[: hunk_starts[count]]
        lines = svc.lines.code_block(shown, lang="diff", with_line_numbers=False)
        omitted = 0 if count is None else max(0, len(hunk_starts) - count)
        if omitted:
            lines.append(svc.lines.note(f"diff truncated: {omitted} of {len(hunk_starts)} hunks omitted"))
        data: dict[str, Any] = {"section": "diff", "hunks": len(hunk_starts), "omitted_hunks": omitted}
        return Record(
            kind="code", key="diff", priority=0, lines=tuple(lines), data=data, confidence=confidence, group="diff",
            shrink=shrink if count is None and len(hunk_starts) > 1 else None,
        )  # fmt: skip

    def shrink(budget: int) -> Record | None:
        for count in range(len(hunk_starts) - 1, 0, -1):  # 入る最大の数の先頭ハンクを残す
            rec = make(count)
            if estimate_tokens("\n".join(rec.lines)) <= budget:
                return rec
        return None

    return make(None)


def _diag_records(
    svc: ToolServices, name: str, items: tuple[Any, ...], priority: int, confidence: Confidence
) -> list[Record]:
    if not items:
        return []
    objs = [dict(o) for o in items]
    lines = (
        f"{name}:",
        *(_diag_line(svc, o) for o in objs),
    )  # 値のない kv 行（"added:"）の後ろに診断行が続く
    return [
        Record(
            kind="diag",
            key=name,
            priority=priority,
            lines=lines,
            data={"section": name, "count": len(objs)},
            confidence=confidence,
            group=name,
        )  # fmt: skip
    ]


def _short(sha: str) -> str:
    return sha[:_SHA_SHOWN] + "…"


def _build_output(
    svc: ToolServices, args: EditSymbolBodyArgs, result: EditResult, seen: _Seen, before_urn: str | None
) -> ToolOutput:
    conf = result.confidence
    delta = result.new_diagnostics
    plus, minus = _diff_counts(result.diff)
    urns = result.target_urns_after
    target_urn = urns[0] if urns else (before_urn or "")
    new_range = _range_json(result.new_range)
    files = [
        {
            "path": str(c.path),
            "sha256_before": c.sha256_before,
            "sha256_after": c.sha256_after,
            "hunks": c.hunks,
        }
        for c in result.files_changed
    ]

    kv: list[tuple[str, str]] = [
        ("applied", f"{str(result.applied).lower()}  edit_id={result.edit_id or '-'}  op={args.operation}"),
    ]
    if target_urn:
        span = f" @L{new_range['start_line']}-{new_range['end_line']}" if new_range else ""
        kv.append(("target", f"{target_urn}{span}"))
    for c in result.files_changed:
        change = (
            f"{c.path} (+{plus} -{minus})"
            if len(result.files_changed) == 1
            else f"{c.path} ({c.hunks} hunks)"
        )
        kv.append(("files_changed", f"{change}  sha256 {_short(c.sha256_before)}→{_short(c.sha256_after)}"))
    kv.append(("syntax_check", result.syntax_check))
    if delta.status == "not_computed":
        kv.append(("new_diagnostics", "not_computed"))
    else:
        kv.append(
            ("new_diagnostics",
             f"{delta.status} (+{len(delta.added)} -{len(delta.resolved)}) checked={len(delta.checked_files)} files")
        )  # fmt: skip

    records: list[Record] = []
    if result.diff:
        records.append(_diff_record(svc, result.diff, conf))
    records += _diag_records(svc, "added", delta.added, 1, conf)
    records += _diag_records(svc, "resolved", delta.resolved, 2, conf)

    lsp = LspStatus.READY if conf is Confidence.EXACT else LspStatus.UNAVAILABLE
    return ToolOutput(
        records=records,
        head_kv=kv,
        data_static={
            "applied": result.applied,
            "edit_id": result.edit_id,
            "operation": args.operation,
            "target_urn": target_urn or None,
            "new_range": new_range,
            "indent_unit": seen.indent_unit,
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
        max_confidence=Confidence.STRUCTURAL,
        engine=EngineChoice(engine="tree-sitter", confidence=conf, lsp_status=lsp.value),
        page_unit="section",
        tier=seen.tier,
        detected_language=seen.language_id,
        warnings=(*result.warnings, *delta.warnings),
    )


# --- ハンドラ -------------------------------------------------------------------------------------


async def handler(ctx: ToolContext, args: EditSymbolBodyArgs) -> ToolOutput:
    svc = services_of(ctx.services)
    ws = svc.workspace(args.workspace_id)
    _check_arguments(args)
    target, rel = _target_of(args)
    # insert_* が挿入したシンボルの URN と範囲を残す入れ物（応答の target_urn / new_range）
    outcome = InsertOutcome()
    builder = _plan_builder(args, target, outcome)
    expected = {rel: args.expected_file_sha256} if args.expected_file_sha256 and rel else None
    seen = _Seen()
    tx = _transaction(svc, ws)
    run: Callable[..., Any] = tx.run
    result: EditResult = await run(
        _guarded(builder, args, seen),
        target=target,
        dry_run=args.dry_run,
        allow_heuristic_mutation=args.allow_heuristic_mutation,
        expected_shas=expected,
        diagnostics_wait_ms=args.diagnostics_wait_ms,
        revision=args.revision,
    )
    return _build_output(svc, args, relocate(result, outcome), seen, target.urn)


SPEC = ToolSpec(
    name="edit_symbol_body",
    tid="T-10",
    args_model=EditSymbolBodyArgs,
    handler=handler,
    default_max_tokens=1500,
    description=(
        "Replace a symbol (function/method/class) with new code, cut at AST node boundaries, and get the diff, the syntax "
        "check and the new diagnostics in the same response. operation=replace_body takes the WHOLE definition "
        "(signature included); indentation, newlines and BOM are normalized to the file. Target by urn, or path + "
        "symbol_name, or path + line. Pass expected_file_sha256 (the sha= from get_file_skeleton / get_symbol_bodies) "
        "to refuse a stale write; use dry_run to preview. An edit that adds a syntax error is refused and nothing is written. "
        "insert_before / insert_after add a sibling next to the symbol; insert_member adds a member to the class/module given as target (position start|end)."
    ),
    writes=True,
    max_confidence=Confidence.STRUCTURAL,
    milestone="M1",
    page_unit="section",
    records_key="records",
)
