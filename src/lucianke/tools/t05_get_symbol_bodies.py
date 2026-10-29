"""T-05 ``get_symbol_bodies``（仕様書 §11.3）。指定したシンボルの実装本体だけを AST ノード境界で切り出して返す。

縦切りの範囲: Python・TypeScript・JavaScript を構文木だけで（``structural``）。TargetRef は ``urn`` と ``path`` +
``symbol_name`` / ``line``。セレクタ（``xpath:`` など）は項目単位で ``E_UNSUPPORTED``。``include_referenced_symbols`` は
索引が無いので ``refs:`` を出さない。``revision`` は ``WORKING_TREE`` のみ。

返す範囲は ``syntax.boundaries.definition_range``（デコレータ・直前の Doc コメントを含む）の前に ``context_lines`` 行を足したもの。
1 対象が残り予算に収まらないときは ``Record.shrink`` が先頭から詰めた版を返し、``truncated_at_line`` を記録する
（続きは ``line_range`` で取る）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import BATCH_MAX_ITEMS, CANDIDATES_MAX
from lucianke.core.model import Confidence, EngineChoice, SymbolRecord, Tier
from lucianke.core.records import Record, ToolOutput
from lucianke.core.urn import CodeSel, Urn, parse, relax
from lucianke.syntax.boundaries import definition_range
from lucianke.tools._base import WORKING_TREE, CommonArgs, ToolContext, ToolSpec
from lucianke.tools._services import FileAnalysis, ToolServices, analyze_file, estimate_tokens, services_of
from lucianke.tools.t02_get_file_skeleton import failure_record

_SELECTOR_PREFIXES = ("xpath:", "jsonpath:", "jq:", "toml:", "yaml:", "cell:")


class TargetRefArg(BaseModel):
    """仕様書 §5.3 の TargetRef。外部の ``line`` / ``character`` は 1 始まり。"""

    model_config = ConfigDict(extra="forbid")

    urn: str | None = Field(default=None, description='e.g. "lk://app/src/a.py#Cls.method"')
    path: str | None = Field(default=None, description="Workspace-relative path with '/'.")
    symbol_name: str | None = Field(default=None, description='"name", "Outer.name" (suffix match on qualified_name).')
    line: int | None = Field(default=None, ge=1, description="1-based line; selects the innermost symbol containing it.")
    character: int | None = Field(default=None, ge=1, description="1-based column (optional with line).")


class LineRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> LineRange:
        if self.start > self.end:
            raise ValueError("line_range: start must be <= end")
        return self


class GetSymbolBodiesArgs(CommonArgs):
    targets: list[TargetRefArg] = Field(min_length=1, max_length=BATCH_MAX_ITEMS, description="1-20 TargetRefs.")
    context_lines: int = Field(default=2, ge=0, le=20, description="Lines added before the body (decorators/comments).")
    with_line_numbers: bool = Field(default=True, description="Prefix each code line with '@L<n>: '.")
    include_referenced_symbols: bool = Field(
        default=True, description="Add '- refs:' (needs the index; not available in the syntax-only build)."
    )
    line_range: LineRange | None = Field(default=None, description="Only this 1-based line range, clamped to the body.")


# --- 解決（仕様書 §5.3） ---------------------------------------------------------------------


def _candidate(s: SymbolRecord) -> dict[str, Any]:
    return {"urn": str(s.urn), "signature": s.signature, "path": str(s.path), "line": s.range.start.line + 1}


def _ambiguous(name: str, found: list[SymbolRecord]) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_TARGET_AMBIGUOUS,
        f"{name!r} matches {len(found)} symbols; use a longer symbol_name, a urn, or a line",
        candidates=[_candidate(s) for s in found[:CANDIDATES_MAX]],
    )


def _by_name(an: FileAnalysis, name: str) -> SymbolRecord:
    """``qualified_name`` の末尾一致。完全一致が 1 件ならそれを優先する。0 件は NOT_FOUND、2 件以上は AMBIGUOUS。"""
    exact = [s for s in an.symbols if s.qualified_name == name]
    if len(exact) == 1:
        return exact[0]
    found = exact or [s for s in an.symbols if s.qualified_name.endswith("." + name)]
    if not found:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"symbol {name!r} not found in {an.rel}",
            details={"path": str(an.rel), "symbol_name": name},
        )
    if len(found) > 1:
        raise _ambiguous(name, found)
    return found[0]


def _by_line(an: FileAnalysis, line: int) -> SymbolRecord:
    inside = [s for s in an.symbols if s.range.line_span_external()[0] <= line <= s.range.line_span_external()[1]]
    if not inside:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"no symbol contains line {line} of {an.rel}",
            details={"path": str(an.rel), "line": line},
        )
    return min(inside, key=lambda s: (s.range.line_span_external()[1] - s.range.line_span_external()[0], -s.range.start.line))


def _by_urn(an: FileAnalysis, urn: Urn) -> SymbolRecord:
    wanted = str(urn)
    for s in an.symbols:
        if str(s.urn) == wanted:
            return s
    loose = str(relax(urn))
    found = [s for s in an.symbols if str(relax(s.urn)) == loose]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise _ambiguous(wanted, found)
    raise LuciankeError(ErrorCode.E_TARGET_NOT_FOUND, f"no symbol with urn {wanted}", details={"urn": wanted})


async def _resolve(svc: ToolServices, ws: Any, ref: TargetRefArg, cache: dict[str, FileAnalysis]) -> tuple[FileAnalysis, SymbolRecord]:
    async def load(path: str) -> FileAnalysis:
        if path not in cache:
            cache[path] = await analyze_file(svc, ws, path, param="targets")
        return cache[path]

    if ref.urn is not None:  # urn > line > symbol_name（§5.3 規則 4）
        try:
            urn = parse(ref.urn)
        except ValueError as exc:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT, f"targets.urn: {exc}", details={"param": "targets.urn"}
            ) from None
        if urn.workspace_id != ws.workspace_id or not isinstance(urn.selector, CodeSel):
            raise LuciankeError(
                ErrorCode.E_TARGET_NOT_FOUND,
                f"urn does not name a code symbol of workspace {ws.workspace_id!r}: {ref.urn}",
                details={"urn": ref.urn},
            )
        an = await load(urn.path)
        return an, _by_urn(an, urn)
    if ref.path is None:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            "targets: give urn, or path with symbol_name or line",
            details={"param": "targets"},
        )
    if ref.line is not None:
        an = await load(ref.path)
        return an, _by_line(an, ref.line)
    if ref.symbol_name is not None:
        if ref.symbol_name.startswith(_SELECTOR_PREFIXES):
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"selector {ref.symbol_name.split(':', 1)[0]}: is not supported in this build (code symbols only)",
                details={"reason": "selector_not_supported"},
            )
        an = await load(ref.path)
        return an, _by_name(an, ref.symbol_name)
    raise LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT,
        "targets: path needs symbol_name or line (or use urn)",
        details={"param": "targets"},
    )


# --- 本体の切り出し -------------------------------------------------------------------------


def body_lines(an: FileAnalysis, sym: SymbolRecord) -> tuple[int, int]:
    """本体の行範囲（1 始まり・両端を含む）。デコレータ・直前の Doc コメントを含む（``context_lines`` は含まない）。"""
    index = an.parsed.line_index
    start = index.position_to_offset(sym.range.start).value
    end = index.position_to_offset(sym.range.end).value
    node = an.parsed.tree.root_node.descendant_for_byte_range(start, end)
    return definition_range(node, an.parsed, language_id=an.language_id).line_span_external()


def _record(
    svc: ToolServices,
    an: FileAnalysis,
    sym: SymbolRecord,
    args: GetSymbolBodiesArgs,
    request: dict[str, Any],
    priority: int,
    *,
    first: int,
    last: int,
    context_before: int,
    cut_at: int | None = None,
) -> Record:
    """``first``〜``last``（1 始まり・両端を含む）を返す 1 レコード。``cut_at`` は予算で切った最後の行の次の行。"""
    L = svc.lines
    index = an.parsed.line_index
    stop = last if cut_at is None else cut_at - 1
    code_rows = [index.line_text(i - 1) for i in range(first, stop + 1)]
    rel = str(an.rel)
    head = L.symbol_line(sym.kind, f"{rel}#{sym.qualified_name}", start_line=first, end_line=stop)
    lines = [head, L.attr_line("sha", an.sha256[:12], indent=0)]
    lines += L.code_block(code_rows, lang=an.language_id, start_line=first, with_line_numbers=args.with_line_numbers)
    if cut_at is not None:
        lines.append(L.note(f"truncated at line {cut_at}: pass line_range to continue"))
    item = {
        "ok": True,
        "request": request,
        "symbol": sym.to_json(),
        "range_returned": {"start_line": first, "end_line": stop},
        "file_sha256": an.sha256,
        "context_before_lines": context_before,
        "code": "\n".join(code_rows),
        "truncated": cut_at is not None,
        "referenced_symbols": [],
        "confidence_level": Confidence.STRUCTURAL.value,
    }
    if cut_at is not None:
        item["truncated_at_line"] = cut_at
    shrink = None
    if cut_at is None and stop > first:

        def shrink(budget: int) -> Record | None:
            """先頭から詰める。1 行も入らなければ ``None``（次のページへ）。"""
            lo, hi, best = first, stop - 1, None
            while lo <= hi:  # 入る最大の行数を二分探索
                mid = (lo + hi) // 2
                rec = _record(svc, an, sym, args, request, priority, first=first, last=last,
                              context_before=context_before, cut_at=mid + 1)  # fmt: skip
                if estimate_tokens("\n".join(rec.lines)) <= budget:
                    best, lo = rec, mid + 1
                else:
                    hi = mid - 1
            return best

    return Record(
        kind="code",
        key=f"{rel}#{sym.qualified_name}",
        priority=priority,
        lines=tuple(lines),
        data=item,
        confidence=Confidence.STRUCTURAL,
        group=rel,
        shrink=shrink,
    )


def build_record(svc: ToolServices, an: FileAnalysis, sym: SymbolRecord, args: GetSymbolBodiesArgs,
                 request: dict[str, Any], priority: int) -> Record:  # fmt: skip
    first, last = body_lines(an, sym)
    context_before = 0
    if args.line_range is not None:
        first, last = max(first, args.line_range.start), min(last, args.line_range.end)
        if first > last:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT,
                f"line_range {args.line_range.start}-{args.line_range.end} is outside the body "
                f"({first}-{last} after clamping)",
                details={"param": "line_range"},
            )
    else:
        context_before = min(args.context_lines, first - 1)
        while context_before and not an.parsed.line_index.line_text(first - context_before - 1).strip():
            context_before -= 1  # 前置きの先頭の空行は価値が低いので付けない
        first -= context_before
    return _record(svc, an, sym, args, request, priority, first=first, last=last, context_before=context_before)


async def handler(ctx: ToolContext, args: GetSymbolBodiesArgs) -> ToolOutput:
    if args.revision != WORKING_TREE:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"revision {args.revision!r} is not supported yet (WORKING_TREE only)",
            details={"reason": "revision_not_supported", "revision": args.revision},
        )
    svc = services_of(ctx.services)
    ws = svc.workspace(args.workspace_id)
    cache: dict[str, FileAnalysis] = {}
    records: list[Record] = []
    errors: list[tuple[str, LuciankeError]] = []
    languages: list[str] = []
    tier = Tier.T1
    for i, ref in enumerate(args.targets):
        request = ref.model_dump(exclude_none=True)
        label = ref.urn or (f"{ref.path}#{ref.symbol_name}" if ref.symbol_name else f"{ref.path}:{ref.line}")
        try:
            an, sym = await _resolve(svc, ws, ref, cache)
            records.append(build_record(svc, an, sym, args, request, i))
            if an.language_id not in languages:
                languages.append(an.language_id)
            tier = max(tier, an.tier)
        except LuciankeError as err:
            errors.append((label, err))
            rec = failure_record(label, err, i, svc.lines.banner)
            if err.candidates:  # 曖昧のときは候補を 1 行 1 件で添える（仕様書 §5.4.2）
                rec = Record(
                    kind=rec.kind, key=rec.key, priority=rec.priority, group=rec.group, confidence=rec.confidence,
                    lines=(*rec.lines, *(svc.lines.candidate_line(c) for c in err.candidates)), data=rec.data,
                )  # fmt: skip
            records.append(rec)
    if errors and len(errors) == len(args.targets):
        _, first = errors[0]
        raise LuciankeError(
            first.code,
            first.message,
            details={**first.details, "items": [{"ok": False, "request": r.model_dump(exclude_none=True), "error": e.to_json()}
                                                for r, (_, e) in zip(args.targets, errors, strict=True)]},
            candidates=first.candidates,
        )  # fmt: skip
    return ToolOutput(
        records=records,
        max_confidence=Confidence.STRUCTURAL,
        engine=EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL),
        page_unit="target",
        tier=tier,
        detected_language=",".join(languages) if languages else "n/a",
    )


SPEC = ToolSpec(
    name="get_symbol_bodies",
    tid="T-05",
    args_model=GetSymbolBodiesArgs,
    handler=handler,
    default_max_tokens=3000,
    description=(
        "Return the implementation body of 1-20 symbols (function/method/class) cut at AST node boundaries, including "
        "decorators and the doc comment above, with @L line numbers and the file sha. Target by urn, or path + "
        "symbol_name (suffix match on the qualified name), or path + line. Python/TypeScript/JavaScript "
        "(syntax tree only). Prefer this over reading the whole file."
    ),
    max_confidence=Confidence.STRUCTURAL,
    milestone="M1",
    page_unit="target",
    records_key="items",
)
