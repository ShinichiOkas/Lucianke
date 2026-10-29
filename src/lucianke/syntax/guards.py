"""呼び出し箇所を囲むガード条件の抽出（仕様書 T-07 処理規約 4、設計書 §7.4.6）。

呼び出し箇所から囲みの関数本体まで AST の祖先を遡り、その呼び出しが実行される条件を、
外側から順に `` && `` で連ねた 1 行の文字列にする。

* ``if`` / ``elif`` / ``else``: 分岐の条件。``else`` と、``elif`` より前の条件は ``!(条件)`` で表す
* ``match`` / ``case`` (Python)、``switch`` / ``case`` (TS/JS): ``case パターン``（ガード付きは ``case パターン if 条件``）
* ``try`` / ``except``: ``except`` 側だけ ``on: 例外型``（``try`` 本体・``else``・``finally`` は条件にしない）
* ``while``: ループ条件（Python の ``while ... else`` の ``else`` 側は ``!(条件)``）
* 三項演算: 真側は条件、偽側は ``!(条件)``
* 短絡 ``and`` / ``or`` / ``&&`` / ``||``: 右辺の中なら、``and`` は左辺、``or`` は ``!(左辺)``

条件は常にソーステキストのまま（改行を含む空白だけ 1 個のスペースに畳む）。条件全体を囲む外側の括弧は外す。
``or`` / ``||`` / 三項を最上位に持つ条件は、ほかの条件と連ねるときだけ ``(...)`` で囲む。
Python と TypeScript / JavaScript だけ対応し、ほかの言語は ``None`` を返す。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from lucianke.syntax.types import ParsedFile

__all__ = ["guard_for"]

_ELLIPSIS = "…"
_SEP = " && "
_NEWLINE_WS = re.compile(r"\s*\n\s*")

_PY_FUNCTIONS = frozenset({"function_definition", "lambda"})
_TS_FUNCTIONS = frozenset(
    {
        "function_declaration",
        "function_expression",
        "function",
        "generator_function",
        "generator_function_declaration",
        "arrow_function",
        "method_definition",
    }
)
_TS_LANGUAGES = frozenset({"typescript", "tsx", "javascript", "jsx"})

type _Contrib = Callable[[Any, Any], list[str]]


def _neg(cond: str) -> str:
    return f"!({cond})"


def _text(node: Any, parsed: ParsedFile) -> str:
    raw = parsed.source[node.start_byte : node.end_byte].decode("utf-8", "surrogateescape")
    return _NEWLINE_WS.sub(" ", raw).strip()


class _Loose(str):
    """最上位に ``or`` / ``||`` / 三項などの弱い演算子を持つ条件。ほかの条件と連ねるときは括弧で囲む。"""

    __slots__ = ()


_LOOSE_TYPES = frozenset(
    {
        "conditional_expression",
        "ternary_expression",
        "named_expression",
        "assignment_expression",
        "sequence_expression",
    }
)


def _is_loose(node: Any) -> bool:
    if node.type in _LOOSE_TYPES:
        return True
    if node.type in ("boolean_operator", "binary_expression"):
        op = node.child_by_field_name("operator")
        return op is not None and op.type in ("or", "||", "??")
    return False


def _cond_text(node: Any, parsed: ParsedFile) -> str:
    """条件式のテキスト。条件全体を囲む外側の括弧（``if (...)`` の括弧など）は外す。"""
    while node.type == "parenthesized_expression":
        inner = [c for c in node.named_children if c.type != "comment"]
        if len(inner) != 1:
            break
        node = inner[0]
    text = _text(node, parsed)
    return _Loose(text) if _is_loose(node) else text


def _is(a: Any, b: Any) -> bool:
    return a is not None and b is not None and a == b


# --- Python -----------------------------------------------------------------------------


def _py_prior_negations(clause: Any, parsed: ParsedFile) -> list[str]:
    """``if`` 文の ``clause``（``elif`` / ``else``）より前の分岐条件の否定。"""
    owner = clause.parent
    out = [_neg(_cond_text(owner.child_by_field_name("condition"), parsed))]
    for alt in owner.children_by_field_name("alternative"):
        if alt == clause:
            break
        if alt.type == "elif_clause":
            out.append(_neg(_cond_text(alt.child_by_field_name("condition"), parsed)))
    return out


def _py_exception_type(clause: Any, parsed: ParsedFile) -> str:
    for child in clause.named_children:
        if child.type in ("block", "comment"):
            continue
        target = child.named_children[0] if child.type == "as_pattern" and child.named_child_count else child
        return _text(target, parsed)  # 別名（``as e``）は落とす
    return "*"  # 型なしの ``except:``（すべて）


def _make_py(parsed: ParsedFile) -> _Contrib:
    def contrib(parent: Any, child: Any) -> list[str]:
        kind = parent.type
        if kind == "if_statement":
            if _is(child, parent.child_by_field_name("consequence")):
                return [_cond_text(parent.child_by_field_name("condition"), parsed)]
            return []
        if kind == "elif_clause":
            out = _py_prior_negations(parent, parsed)
            if _is(child, parent.child_by_field_name("consequence")):
                out.append(_cond_text(parent.child_by_field_name("condition"), parsed))
                return out
            if _is(child, parent.child_by_field_name("condition")):
                return out
            return []
        if kind == "else_clause":
            owner = parent.parent
            if owner is None:
                return []
            if owner.type == "if_statement":
                return _py_prior_negations(parent, parsed)
            if owner.type == "while_statement":
                return [_neg(_cond_text(owner.child_by_field_name("condition"), parsed))]
            return []
        if kind == "conditional_expression":
            parts = [c for c in parent.named_children if c.type != "comment"]
            if len(parts) != 3:
                return []
            cond = _cond_text(parts[1], parsed)
            if child == parts[0]:
                return [cond]
            if child == parts[2]:
                return [_neg(cond)]
            return []
        if kind == "boolean_operator":
            if _is(child, parent.child_by_field_name("right")):
                op = parent.child_by_field_name("operator")
                left = _cond_text(parent.child_by_field_name("left"), parsed)
                if op is not None and op.type == "or":
                    return [_neg(left)]
                return [left]
            return []
        if kind == "while_statement":
            if _is(child, parent.child_by_field_name("body")):
                return [_cond_text(parent.child_by_field_name("condition"), parsed)]
            return []
        if kind in ("except_clause", "except_group_clause"):
            if child.type == "block":
                return [f"on: {_py_exception_type(parent, parsed)}"]
            return []
        if kind == "case_clause":
            if child.type == "block":
                header = parsed.source[parent.start_byte : child.start_byte].decode(
                    "utf-8", "surrogateescape"
                )
                return [_NEWLINE_WS.sub(" ", header).strip().rstrip(":").rstrip()]
            return []
        return []

    return contrib


# --- TypeScript / JavaScript ------------------------------------------------------------


def _make_ts(parsed: ParsedFile) -> _Contrib:
    def contrib(parent: Any, child: Any) -> list[str]:
        kind = parent.type
        if kind == "if_statement":
            if _is(child, parent.child_by_field_name("consequence")):
                return [_cond_text(parent.child_by_field_name("condition"), parsed)]
            return []
        if kind == "else_clause":
            owner = parent.parent
            if owner is not None and owner.type == "if_statement":
                return [_neg(_cond_text(owner.child_by_field_name("condition"), parsed))]
            return []
        if kind == "ternary_expression":
            cond = parent.child_by_field_name("condition")
            if cond is None:
                return []
            if _is(child, parent.child_by_field_name("consequence")):
                return [_cond_text(cond, parsed)]
            if _is(child, parent.child_by_field_name("alternative")):
                return [_neg(_cond_text(cond, parsed))]
            return []
        if kind == "binary_expression":
            if _is(child, parent.child_by_field_name("right")):
                op = parent.child_by_field_name("operator")
                left = _cond_text(parent.child_by_field_name("left"), parsed)
                if op is not None and op.type == "||":
                    return [_neg(left)]
                if op is not None and op.type == "&&":
                    return [left]
            return []
        if kind == "while_statement":
            if _is(child, parent.child_by_field_name("body")):
                return [_cond_text(parent.child_by_field_name("condition"), parsed)]
            return []
        if kind == "catch_clause":
            if _is(child, parent.child_by_field_name("body")):
                annotation = parent.child_by_field_name("type")
                if annotation is not None:
                    return [f"on: {_text(annotation, parsed).lstrip(':').strip()}"]
                return ["on: catch"]
            return []
        if kind == "switch_case":
            value = parent.child_by_field_name("value")
            if value is not None and child != value:
                return [f"case {_cond_text(value, parsed)}"]
            return []
        if kind == "switch_default":
            return ["default"]
        return []

    return contrib


# --- 公開関数 ---------------------------------------------------------------------------


def guard_for(call_node: Any, parsed: ParsedFile, *, language_id: str, max_chars: int = 120) -> str | None:
    """``call_node`` を囲むガード条件を `` && `` で連ねた文字列にする。条件が無ければ ``None``。

    ``max_chars`` を超えたら、全体が ``max_chars`` 文字になるよう末尾を ``…`` で省略する。
    対応しない言語は ``None``。
    """
    if language_id == "python":
        stop, contrib = _PY_FUNCTIONS, _make_py(parsed)
    elif language_id in _TS_LANGUAGES:
        stop, contrib = _TS_FUNCTIONS, _make_ts(parsed)
    else:
        return None

    segments: list[list[str]] = []
    child = call_node
    parent = child.parent
    while parent is not None and parent.type not in stop:
        found = contrib(parent, child)
        if found:
            segments.append(found)
        child, parent = parent, parent.parent

    if not segments:
        return None
    # 内側から集めたので、外側から順に並べ直す（1 つの祖先が出す複数の条件はその順のまま）
    conds = [cond for group in reversed(segments) for cond in group]
    if len(conds) > 1:  # 連ねるとき、``or`` などを含む条件は括弧で囲んで優先順位を保つ
        conds = [f"({c})" if isinstance(c, _Loose) else c for c in conds]
    result = _SEP.join(conds)
    if len(result) > max_chars:
        result = result[: max(max_chars - 1, 0)] + _ELLIPSIS
    return result
