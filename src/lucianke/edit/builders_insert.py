"""T-10 の挿入 3 操作（``insert_before`` / ``insert_after`` / ``insert_member``）の ``plan_builder``（仕様書 T-10 処理規約 2〜4、§10.8・§10.9）。

``replace_body``（``builders.py``）と同じく、トランザクションに渡す「計画を作る関数」を返す。どれも **1 つの hunk** の計画で、
位置は適用前の座標。範囲・位置は ``syntax.boundaries``（``definition_range`` / ``insertion_point`` / ``blank_lines_around``）、
本文の整形は ``edit.normalize.normalize_new_code`` に任せ、ここでは「どの行に、前後の空行をいくつ付けて差し込むか」だけを決める。

* ``insert_before``: 定義ノード（付帯要素込み）の **行頭** に ``new_code`` + 空行 ``N`` 行を差し込む。直前の兄弟が居れば、既存の空行と
  合わせて ``N`` 行になるよう足す（本体の先頭なら足さない）。
* ``insert_after``: 定義ノードの **行末** に、改行 + 空行 ``N`` 行 + ``new_code`` を差し込む（元の行の終端は後ろに残るので、末尾改行の
  有無が変わらない）。直後の兄弟が居れば、既存の空行と合わせて ``N`` 行にする。``N`` は ``blank_lines_around``（Python のトップレベルの
  関数・クラスだけ 2、他は 1）。
* ``insert_member``: ``insertion_point`` の位置（``position`` が ``end`` なら最後のメンバーの直後、``start`` なら最初のメンバーの前。
  Python の docstring は飛ばす）。本体が空なら、Python は ``pass`` / ``...`` を置換、波括弧の言語は ``{`` の直後に差し込む。
  基準列は既存メンバーの先頭行の列（無ければ親の列 + 1 単位）。メンバーの間は空行 1 行。

基準列（インデント）は仕様書 T-10 処理規約 4(c) のとおり。改行コード・BOM・末尾改行は ``apply_hunks`` が保つ。

``new_code`` 単体の構文検証（``check_fragment``）は、断片を入れる容器（クラス本体 / 名前空間）で包んで行う。適用後の構文検証は
トランザクション（``check_after``）の仕事。

**応答の ``target_urn`` / ``new_range``**: トランザクションは「解決した対象」（兄弟・親）を再索引後に探して返す。挿入の応答は
**挿入したシンボル** の URN と範囲であるべき（処理規約 5）なので、ビルダーは適用後のバイト列を解析して挿入位置のシンボルを探し、
``InsertOutcome`` に残す。``relocate`` がそれを ``EditResult`` に載せ替える。挿入した断片がシンボルを含まなければ（文だけなど）
トランザクションの答え（対象の URN と範囲）のままにする。
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any, Literal

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import NEW_CODE_MAX_BYTES
from lucianke.core.model import Confidence, Kind, Position, Range, TargetRef
from lucianke.core.textpos import detect_indent
from lucianke.edit.normalize import default_indent, normalize_new_code
from lucianke.edit.plan import Hunk, Plan, apply_hunks
from lucianke.edit.syntax_check import Wrap, check_fragment, to_error_details
from lucianke.edit.transaction import EditResult, PlanBuilder, PlanContext
from lucianke.syntax.boundaries import InsertionPoint, blank_lines_around, definition_range, insertion_point
from lucianke.syntax.parsers import parse_source
from lucianke.syntax.support import get_support
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import ParsedFile

__all__ = [
    "INSERT_HINT",
    "INSERT_OPERATIONS",
    "InsertOutcome",
    "insert_after_builder",
    "insert_before_builder",
    "insert_builder",
    "insert_member_builder",
    "relocate",
]

_log = logging.getLogger(__name__)

INSERT_OPERATIONS = ("insert_before", "insert_after", "insert_member")
INSERT_HINT = "`new_code` は挿入する断片だけを渡す（それ自体が構文として完結していること）"

# クラス・構造体・enum・trait・モジュール（``insert_member`` の親になれる種別）。
_CONTAINER_KINDS = frozenset({Kind.CLASS, Kind.INTERFACE, Kind.STRUCT, Kind.ENUM, Kind.TRAIT, Kind.MODULE})
# クラス本体に置かれる種別。これらの兄弟に挿入する断片は、単体ではなくクラス本体の容器で包んで検証する。
_MEMBER_KINDS = frozenset({Kind.METHOD, Kind.CONSTRUCTOR, Kind.PROPERTY, Kind.FIELD})

_WRAPPERS = frozenset({"decorated_definition", "export_statement", "ambient_declaration"})
_DECLARATIONS = frozenset({"lexical_declaration", "variable_declaration"})
_ROOTS = frozenset({"module", "program", "source_file", "translation_unit"})
_NOT_MEMBER = frozenset({"comment", "line_comment", "block_comment", "decorator", "attribute_item"})


@dataclass(slots=True)
class InsertOutcome:
    """ビルダーが残す「挿入したシンボル」の URN と範囲（適用後の座標）。見つからなければ ``urn`` は ``None``。"""

    urn: str | None = None
    range: Range | None = None


def relocate(result: EditResult, outcome: InsertOutcome | None) -> EditResult:
    """書き込んだ応答の ``target_urns_after`` / ``new_range`` を、挿入したシンボルのものに載せ替える。"""
    if outcome is None or outcome.urn is None or not result.applied or result.edit_id is None:
        return result
    return dataclasses.replace(result, target_urns_after=(outcome.urn,), new_range=outcome.range)


# --- エラー ----------------------------------------------------------------------------------


def _syntax_error(message: str, **details: object) -> LuciankeError:
    return LuciankeError(ErrorCode.E_SYNTAX_INVALID, message, details={"hint": INSERT_HINT, **details})


def _unsupported(message: str, reason: str, ctx: PlanContext) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_UNSUPPORTED,
        message,
        details={"reason": reason, "path": str(ctx.path), "language_id": ctx.language_id},
    )


def _check_new_code_size(new_code: str) -> None:
    if len(new_code.encode("utf-8")) > NEW_CODE_MAX_BYTES:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            f"new_code is larger than {NEW_CODE_MAX_BYTES} bytes",
            details={"param": "new_code", "reason": "too_large"},
        )


def _require_tree(ctx: PlanContext, operation: str) -> tuple[Any, ParsedFile]:
    if ctx.symbol is None or ctx.node is None or ctx.parsed is None:
        raise _unsupported(
            f"{operation} needs a syntax tree and a resolved symbol: {ctx.path}", "no_symbol_range", ctx
        )
    if ctx.confidence is Confidence.LEXICAL_FALLBACK:
        raise _unsupported(
            f"{operation} is not allowed on a lexical_fallback target: {ctx.path}",
            "insert_not_allowed_for_lexical_fallback",
            ctx,
        )
    return ctx.node, ctx.parsed


def _check_fragment(ctx: PlanContext, new_code: str, wrap: Wrap | None) -> None:
    """``new_code`` 単体の構文検証（``wrap`` が ``None`` なら容器が無いので適用後の検証だけに任せる）。"""
    if wrap is None:
        return
    result = check_fragment(ctx.language_id, new_code, wrap=wrap)
    if result.status == "failed":
        raise _syntax_error("new_code is not valid syntax", path=str(ctx.path), **to_error_details(result))


# --- 木と行の補助 ------------------------------------------------------------------------------


def _outer(node: Any) -> Any:
    """定義ノードを、範囲の核になる外側のノード（デコレータつき定義・export 文・宣言全体）に置き換える。"""
    parent = node.parent
    if (
        node.type == "variable_declarator"
        and parent is not None
        and parent.type in _DECLARATIONS
        and parent.named_child_count == 1
    ):
        node = parent
    while node.parent is not None and node.parent.type in _WRAPPERS:
        node = node.parent
    return node


def _inner(node: Any) -> Any:
    """デコレータつき定義・export 文を、中の定義ノードに戻す。"""
    while node.type in _WRAPPERS:
        inner = node.child_by_field_name("definition") or node.child_by_field_name("declaration")
        if inner is None:
            named = [c for c in node.named_children if c.type != "decorator"]
            inner = named[-1] if named else None
        if inner is None:
            break
        node = inner
    return node


def _is_toplevel(outer: Any) -> bool:
    return outer.parent is None or outer.parent.type in _ROOTS


def _line_of(node: Any, parsed: ParsedFile) -> int:
    return parsed.line_index.line_of_offset(node.start_byte)


def _has_sibling_before(outer: Any, first_line: int, parsed: ParsedFile) -> bool:
    """範囲の前（付帯要素のデコレータ・Doc コメントを除く）に兄弟が居るか。"""
    prev = outer.prev_named_sibling
    while prev is not None and _line_of(prev, parsed) >= first_line:
        prev = prev.prev_named_sibling
    return prev is not None


def _has_sibling_after(outer: Any, last_line: int, parsed: ParsedFile) -> bool:
    """範囲の後ろ（末尾の同じ行のコメントを除く）に兄弟が居るか。"""
    nxt = outer.next_named_sibling
    while nxt is not None and _line_of(nxt, parsed) <= last_line:
        nxt = nxt.next_named_sibling
    return nxt is not None


def _blank(parsed: ParsedFile, line: int) -> bool:
    return parsed.line_index.line_text(line).strip() == ""


def _blank_run_above(parsed: ParsedFile, line: int) -> int:
    n, i = 0, line - 1
    while i >= 0 and _blank(parsed, i):
        n, i = n + 1, i - 1
    return n


def _blank_run_below(parsed: ParsedFile, line: int) -> int:
    n, i, last = 0, line + 1, parsed.line_index.line_count - 1
    while i <= last and _blank(parsed, i):
        n, i = n + 1, i + 1
    return n


def _members(container: Any) -> list[Any]:
    inner = _inner(container)
    body = inner if inner.type in _ROOTS else (inner.child_by_field_name("body") or inner)
    return [c for c in body.named_children if c.type not in _NOT_MEMBER]


def _is_docstring(node: Any) -> bool:
    return (
        node.type == "expression_statement"
        and node.named_child_count == 1
        and node.named_children[0].type in ("string", "concatenated_string")
    )


def _canonical(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalized(
    ctx: PlanContext, new_code: str, *, base_column: int, mode: Literal["replace", "insert"]
) -> str:
    """``normalize_new_code`` の結果を ``"\\n"`` 区切りにしたもの（``apply_hunks`` が対象ファイルの改行に揃える）。"""
    return _canonical(
        normalize_new_code(
            new_code,
            target_source=ctx.source,
            base_column=base_column,
            language_id=ctx.language_id,
            mode=mode,
        )
    )


def _require_whitespace_before(ctx: PlanContext, parsed: ParsedFile, pos: Position) -> None:
    if parsed.line_index.line_text(pos.line)[: pos.col].strip():
        raise _unsupported(
            f"the definition does not start its line (line {pos.line + 1}); cannot insert next to it",
            "definition_shares_line",
            ctx,
        )


@dataclass(frozen=True, slots=True)
class _Placed:
    """組み立てた hunk と、挿入した断片が占める行（適用後の座標、0 始まり、両端を含む）。"""

    hunk: Hunk
    first_line: int
    last_line: int


def _placed(ctx: PlanContext, label: str, rng: Range, text: str, code: str) -> _Placed:
    """``text`` を ``rng`` に差し込む hunk。``code`` は ``text`` に含まれる断片本体（行数を数える）。"""
    leading = len(text) - len(text.lstrip("\n"))
    first = rng.start.line + leading
    return _Placed(Hunk(ctx.path, rng, text, label), first, first + len(code.strip("\n").split("\n")) - 1)


def _plan_of(ctx: PlanContext, placed: _Placed, outcome: InsertOutcome | None) -> Plan:
    if outcome is not None:
        _record_outcome(ctx, placed, outcome)
    return Plan(
        hunks=(placed.hunk,),
        touched_paths=(ctx.path,),
        resolved_shas={ctx.path: ctx.sha256},
        target_urns=(str(ctx.symbol.urn),) if ctx.symbol is not None else (),
        expects_syntax=True,
    )


def _record_outcome(ctx: PlanContext, placed: _Placed, outcome: InsertOutcome) -> None:
    """適用後のバイト列を解析し、挿入した行に始まる最も外側のシンボルを ``outcome`` に残す（失敗は黙って諦める）。"""
    try:
        spec = get_support(ctx.language_id)
        if spec is None or not callable(getattr(spec, "grammar", None)):
            return
        new = apply_hunks(ctx.source, [placed.hunk])
        parsed, _ = parse_source(new, ctx.language_id, "")
        symbols = extract(
            parsed,
            language_id=ctx.language_id,
            language=spec.grammar(),  # pyright: ignore[reportAttributeAccessIssue]
            query_source=load_query_source(ctx.language_id),
            workspace_id=ctx.workspace.workspace_id,
            rel=ctx.path,
        )
        inside = [s for s in symbols if placed.first_line <= s.range.start.line <= placed.last_line]
        if not inside:
            return
        found = min(inside, key=lambda s: (s.range.start.line, -s.range.end.line))
        outcome.urn, outcome.range = str(found.urn), found.range
    except Exception as exc:
        _log.warning("could not locate the inserted symbol for %s: %s", ctx.path, exc)


# --- insert_before / insert_after -------------------------------------------------------------


def _sibling_builder(
    operation: Literal["insert_before", "insert_after"], new_code: str, outcome: InsertOutcome | None
) -> PlanBuilder:
    async def build(ctx: PlanContext) -> Plan:
        _check_new_code_size(new_code)
        node, parsed = _require_tree(ctx, operation)
        assert ctx.symbol is not None
        rng = definition_range(node, parsed, language_id=ctx.language_id)
        outer = _outer(node)
        gap = blank_lines_around(node, parsed, language_id=ctx.language_id, toplevel=_is_toplevel(outer))
        wrap: Wrap = "class_body" if ctx.symbol.kind in _MEMBER_KINDS else "none"
        text_code = _normalized(ctx, new_code, base_column=rng.start.col, mode="insert")  # 改行 1 つで終わる
        _check_fragment(ctx, new_code, wrap)

        if operation == "insert_before":
            _require_whitespace_before(ctx, parsed, rng.start)
            line = rng.start.line
            lead = ""
            if _has_sibling_before(outer, line, parsed):
                lead = "\n" * max(0, gap - _blank_run_above(parsed, line))
            at = Position(line, 0)
            text = lead + text_code + "\n" * gap
        else:
            line = rng.end.line
            at = rng.end
            code = text_code.removesuffix("\n")
            trail = ""
            if _has_sibling_after(outer, line, parsed):
                trail = "\n" * max(0, gap - _blank_run_below(parsed, line))
            text = "\n" * (gap + 1) + code + trail
        placed = _placed(ctx, operation, Range(at, at), text, text_code)
        return _plan_of(ctx, placed, outcome)

    return build


def insert_before_builder(
    target: TargetRef, new_code: str, outcome: InsertOutcome | None = None
) -> PlanBuilder:
    """T-10 ``insert_before``: 対象の定義ノード（付帯要素込み）の直前に、同じ親の兄弟として ``new_code`` を挿入する。"""
    del target  # 解決は EditTransaction が済ませ、PlanContext.symbol / node に入っている
    return _sibling_builder("insert_before", new_code, outcome)


def insert_after_builder(
    target: TargetRef, new_code: str, outcome: InsertOutcome | None = None
) -> PlanBuilder:
    """T-10 ``insert_after``: 対象の定義ノードの直後に、同じ親の兄弟として ``new_code`` を挿入する。"""
    del target
    return _sibling_builder("insert_after", new_code, outcome)


# --- insert_member ---------------------------------------------------------------------------


def _unit_width(ctx: PlanContext) -> int:
    spec = detect_indent(ctx.source) or default_indent(ctx.language_id)
    return spec.width  # タブは 1（1 段 = 1 文字）


def _member_builder(
    new_code: str, position: Literal["start", "end"], outcome: InsertOutcome | None
) -> PlanBuilder:
    async def build(ctx: PlanContext) -> Plan:
        _check_new_code_size(new_code)
        node, parsed = _require_tree(ctx, "insert_member")
        assert ctx.symbol is not None
        if ctx.symbol.kind not in _CONTAINER_KINDS:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT,
                f"insert_member needs a class, struct, enum, trait or module as target, not {ctx.symbol.kind.value}",
                details={
                    "param": "target",
                    "reason": "not_a_container",
                    "kind": ctx.symbol.kind.value,
                    "hint": "メンバーを足す親（クラス・構造体・モジュールなど）を target にする。兄弟として足すなら insert_after / insert_before",
                },
            )
        kind = ctx.symbol.kind
        wrap: Wrap | None = "module" if kind is Kind.MODULE else None if kind is Kind.ENUM else "class_body"
        _check_fragment(ctx, new_code, wrap)

        li = parsed.line_index
        lang = ctx.language_id
        parent_range = definition_range(node, parsed, language_id=lang)
        members = _members(node)
        ip: InsertionPoint = insertion_point(node, parsed, position=position, language_id=lang)
        gap = 1  # 親はクラス・構造体・名前空間（Python のモジュール直下は target にできない）
        member_col = definition_range(members[0], parsed, language_id=lang).start.col if members else None
        body_col = parent_range.start.col + _unit_width(ctx)

        if ip.anchor == "empty_body" and ip.replace is not None:
            rep = ip.replace
            if ip.inline:  # ``class C: pass``: 見出しの直後で改行し、本文を次の行に置く
                line_text = li.line_text(rep.start.line)
                col = rep.start.col
                while col > 0 and line_text[col - 1] in " \t":
                    col -= 1
                code = _normalized(ctx, new_code, base_column=body_col, mode="insert").removesuffix("\n")
                text = "\n" + code
                rng = Range(Position(rep.start.line, col), rep.end)
            else:  # ``pass`` だけの本体: その文を置換する（インデントは元のまま残る）
                code = _normalized(ctx, new_code, base_column=rep.start.col, mode="replace").removesuffix(
                    "\n"
                )
                text = code
                rng = rep
            return _plan_of(ctx, _placed(ctx, "insert_member", rng, text, code), outcome)

        if ip.anchor == "empty_body":  # 波括弧の本体が空: ``{`` の直後に差し込む
            at = ip.position
            line_text = li.line_text(at.line)
            if line_text[max(at.col - 1, 0) : at.col] != "{":
                raise _unsupported(
                    "cannot find the opening brace of the empty body", "empty_body_unsupported", ctx
                )
            code = _normalized(ctx, new_code, base_column=body_col, mode="insert").removesuffix("\n")
            rest = line_text[at.col :]
            if not rest.strip():
                text, rng = "\n" + code, Range(at, at)
            else:  # ``class E {}``: 閉じ括弧を次の行（親の字下げ）に送る
                skip = len(rest) - len(rest.lstrip())
                indent = line_text[: len(line_text) - len(line_text.lstrip())]
                text = "\n" + code + "\n" + indent
                rng = Range(at, Position(at.line, at.col + skip))
            return _plan_of(ctx, _placed(ctx, "insert_member", rng, text, code), outcome)

        col = member_col if member_col is not None else body_col
        text_code = _normalized(ctx, new_code, base_column=col, mode="insert")
        if ip.anchor == "after_member":
            code = text_code.removesuffix("\n")
            text = "\n" * (gap + 1) + code
            rng = Range(ip.position, ip.position)
            return _plan_of(ctx, _placed(ctx, "insert_member", rng, text, code), outcome)

        # before_member: 最初のメンバー（付帯要素込み）の行頭に差し込む
        _require_whitespace_before(ctx, parsed, ip.position)
        line = ip.position.line
        lead = ""
        if lang == "python" and members and _is_docstring(members[0]):  # docstring の後ろ
            lead = "\n" * max(0, gap - _blank_run_above(parsed, line))
        at = Position(line, 0)
        text = lead + text_code + "\n" * gap
        return _plan_of(ctx, _placed(ctx, "insert_member", Range(at, at), text, text_code), outcome)

    return build


def insert_member_builder(
    target: TargetRef,
    new_code: str,
    position: Literal["start", "end"] = "end",
    outcome: InsertOutcome | None = None,
) -> PlanBuilder:
    """T-10 ``insert_member``: ``target``（クラス・構造体・enum・モジュールなど）の本体の先頭または末尾にメンバーを足す。"""
    del target
    return _member_builder(new_code, position, outcome)


def insert_builder(
    operation: str,
    target: TargetRef,
    new_code: str,
    *,
    position: Literal["start", "end"] = "end",
    outcome: InsertOutcome | None = None,
) -> PlanBuilder:
    """``operation``（``INSERT_OPERATIONS`` のどれか）に対応するビルダー。"""
    if operation == "insert_before":
        return insert_before_builder(target, new_code, outcome)
    if operation == "insert_after":
        return insert_after_builder(target, new_code, outcome)
    if operation == "insert_member":
        return insert_member_builder(target, new_code, position, outcome)
    raise ValueError(f"not an insert operation: {operation!r}")
