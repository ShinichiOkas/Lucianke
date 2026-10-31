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
from lucianke.tools._edit_output import diag_records, diagnostics_json, diagnostics_kv, diff_record, short_sha
from lucianke.tools._services import ToolServices, services_of
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


class EditSymbolBodyArgs(CommonArgs):
    target: TargetRefArg
    operation: Operation
    new_code: str | None = Field(
        default=None,
        description="Required unless delete_symbol.",
    )
    position: Literal["start", "end"] = Field(default="end", description="insert_member only.")
    dry_run: bool = Field(default=False)
    allow_heuristic_mutation: bool = Field(
        default=False, description="Allow writing a degraded (heuristic) file."
    )
    expected_file_sha256: str | None = Field(
        default=None,
        description="sha= from get_file_skeleton / get_symbol_bodies (or a 12+ prefix).",
    )
    diagnostics_wait_ms: int = Field(
        default=2000,
        ge=0,
        le=10000,
        description="ms to wait for diagnostics after the write.",
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
        kv.append(
            ("files_changed", f"{change}  sha256 {short_sha(c.sha256_before)}→{short_sha(c.sha256_after)}")
        )
    kv.append(("syntax_check", result.syntax_check))
    kv.append(("new_diagnostics", diagnostics_kv(delta)))

    # 診断を先に、縮められる diff を最後に置く（縮んだ diff はページの最後の 1 件になる。仕様書 §5.6-5、BUG-0067）
    records: list[Record] = []
    records += diag_records(svc, "added", delta.added, 0, conf)
    records += diag_records(svc, "present_after", delta.present_after, 1, conf)
    records += diag_records(svc, "resolved", delta.resolved, 1, conf)
    if result.diff:
        records.append(diff_record(svc, result.diff, conf, priority=2))

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
            "new_diagnostics": diagnostics_json(delta),
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
        "Replace, insert next to, or delete a symbol cut at AST boundaries; returns the diff, syntax check and new diagnostics. replace_body takes the WHOLE definition (signature included). insert_before / insert_after add a sibling; insert_member adds a member to the class/module given as target. expected_file_sha256 refuses a stale write; dry_run previews. A syntax error is refused and nothing is written."
    ),
    writes=True,
    max_confidence=Confidence.STRUCTURAL,
    milestone="M1",
    page_unit="section",
    records_key="records",
)
