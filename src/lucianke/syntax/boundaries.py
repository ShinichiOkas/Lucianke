"""定義ノードの境界（仕様書 §10.8、設計書 §7.4.5）。

シンボル境界の規則の唯一の実装。``symbols``（``range``）、T-05（``context_lines: 0`` の返却範囲）、
T-10（置換・削除・挿入）、T-18（差分の範囲）がすべてこれを使う。

返す ``Range`` は 0 始まりの行・コードポイント列。

* 開始: 付帯要素（デコレータ・属性・直前のコメント）を含めた最初の要素の開始位置（行の途中の列を保つ）
* 終了: 末尾の同一行コメントまで含めた最後の要素の **行末**（終了列はその行の最後の文字の次）
* 直後の空行は含めない

位置は ``parsed.line_index``（``LineIndex``）でバイトオフセットから変換する。Tree-sitter の列はバイト単位で、
CRLF では ``\\r`` が行末に残り、BOM 付きでは 1 行目の列が 3 ずれるため、``Node.start_point`` は使わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from lucianke.core.model import Position, Range
from lucianke.core.textpos import Unit
from lucianke.syntax.types import ParsedFile

__all__ = ["InsertionPoint", "blank_lines_around", "definition_range", "insertion_point"]


# --- 言語ごとの表 -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Profile:
    wrappers: frozenset[str] = frozenset()
    """親がこの型なら、定義ノードの代わりに親を範囲の核にする（装飾つき定義・export 文など）。"""
    prefix_types: frozenset[str] = frozenset()
    """直前の兄弟として現れるデコレータ・属性ノードの型（常に含める）。"""
    comment_types: frozenset[str] = frozenset({"comment"})
    doc_prefixes: tuple[str, ...] = ()
    """この接頭辞で始まるコメントは Doc コメント（空行を挟んでいても含める）。"""
    root_types: frozenset[str] = frozenset({"module", "program", "source_file", "translation_unit"})
    """本体を持たず、自身の子がメンバーになるノード（モジュール）。"""
    docstring: bool = False
    """本体先頭の文字列式を docstring（本体の一部）として扱う（Python）。"""


_PYTHON = _Profile(
    wrappers=frozenset({"decorated_definition"}),
    docstring=True,
)
_TS = _Profile(
    wrappers=frozenset({"export_statement", "ambient_declaration"}),
    prefix_types=frozenset({"decorator"}),
    doc_prefixes=("/**",),
)
_RUST = _Profile(
    prefix_types=frozenset({"attribute_item"}),
    comment_types=frozenset({"line_comment", "block_comment"}),
    doc_prefixes=("///", "/**"),
)
_CSHARP = _Profile(doc_prefixes=("///", "/**"))
_DEFAULT = _Profile()

_PROFILES: dict[str, _Profile] = {
    "python": _PYTHON,
    "typescript": _TS,
    "tsx": _TS,
    "javascript": _TS,
    "jsx": _TS,
    "rust": _RUST,
    "c_sharp": _CSHARP,
}

_DECLARATION_TYPES = frozenset({"lexical_declaration", "variable_declaration"})


def _profile(language_id: str) -> _Profile:
    return _PROFILES.get(language_id, _DEFAULT)


# --- 低レベル補助 ------------------------------------------------------------------------


def _last_line(node: Any, parsed: ParsedFile) -> int:
    """ノードの最後の文字がある行（終端の改行・``\\r`` は数えない）。"""
    end = node.end_byte
    src = parsed.source
    while end > node.start_byte and src[end - 1] in (10, 13):
        end -= 1
    return parsed.line_index.line_of_offset(end)


def _first_line(node: Any, parsed: ParsedFile) -> int:
    return parsed.line_index.line_of_offset(node.start_byte)


def _is_comment(node: Any, prof: _Profile) -> bool:
    return node.type in prof.comment_types


def _comment_text(node: Any) -> bytes:
    return node.text or b""


def _is_doc_comment(node: Any, prof: _Profile) -> bool:
    text = _comment_text(node)
    for prefix in prof.doc_prefixes:
        p = prefix.encode()
        if text.startswith(p):
            if prefix == "///" and text.startswith(b"////"):  # ``////`` は通常コメントの飾り線
                continue
            if prefix == "/**" and text.startswith(b"/**/"):  # 空のブロックコメント
                continue
            return True
    return False


def _is_standalone(node: Any, parsed: ParsedFile) -> bool:
    """ノードの前に、同じ行に空白以外が無いか（行末コメントを次の定義の前置きと取り違えない）。"""
    li = parsed.line_index
    line = li.line_of_offset(node.start_byte)
    return parsed.source[li.line_start(line) : node.start_byte].strip() == b""


def _promote(node: Any, prof: _Profile) -> Any:
    """定義ノードを、範囲の核になる外側のノード（装飾つき定義・export 文）に置き換える。"""
    if node.type == "variable_declarator":
        parent = node.parent
        if parent is not None and parent.type in _DECLARATION_TYPES and parent.named_child_count == 1:
            node = parent  # ``const f = () => 1`` は宣言全体が定義（宣言子が複数ならそのまま）
    while node.parent is not None and node.parent.type in prof.wrappers:
        node = node.parent
    return node


def _line_end(line: int, parsed: ParsedFile) -> Position:
    return Position(line, parsed.line_index.line_length(line, Unit.CODEPOINT))


def _start_position(byte_offset: int, parsed: ParsedFile) -> Position:
    return parsed.line_index.offset_to_position(byte_offset).value


def _trailing_end_line(core: Any, parsed: ParsedFile, prof: _Profile) -> int:
    """核の終端行。同じ行で続く末尾コメント（兄弟）があれば、その最後の行まで延ばす。"""
    line = _last_line(core, parsed)
    nxt = core.next_sibling
    while nxt is not None and _is_comment(nxt, prof) and _first_line(nxt, parsed) == line:
        line = _last_line(nxt, parsed)
        nxt = nxt.next_sibling
    return line


# --- definition_range ------------------------------------------------------------------


def _leading_start(core: Any, parsed: ParsedFile, prof: _Profile) -> int:
    """核の前に付く要素（デコレータ・属性・コメント）を含めた開始バイトオフセット。"""
    start_node = core
    start_line = _first_line(core, parsed)
    prev = core.prev_sibling
    while prev is not None:
        if prev.type in prof.prefix_types:
            start_node = prev
        elif _is_comment(prev, prof):
            if not _is_standalone(prev, parsed):
                break  # 直前の定義の行末コメント
            if not _is_doc_comment(prev, prof) and start_line - _last_line(prev, parsed) > 1:
                break  # 通常コメントは空行を挟んだら含めない
            start_node = prev
        else:
            break
        start_line = _first_line(start_node, parsed)
        prev = start_node.prev_sibling
    return start_node.start_byte


def definition_range(node: Any, parsed: ParsedFile, *, language_id: str) -> Range:
    """``node``（定義ノード）の範囲を、仕様書 §10.8 の付帯要素込みで返す。

    ``node`` が装飾つき定義の内側（Python ``function_definition`` など）でも外側でもよい。
    """
    prof = _profile(language_id)
    core = _promote(node, prof)
    start = _start_position(_leading_start(core, parsed, prof), parsed)
    end = _line_end(_trailing_end_line(core, parsed, prof), parsed)
    if end < start:  # 起こらない想定（空ノードの保険）
        end = start
    return Range(start, end)


# --- insertion_point -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InsertionPoint:
    """``insert_member`` の挿入位置。

    ``anchor`` ごとの意味（``position`` は挿入するテキストの先頭を置く位置）:

    * ``"after_member"``: ``position`` は直前のメンバー（Python では docstring）の行末。改行 + 空行 +
      インデント + コードの順に足す
    * ``"before_member"``: ``position`` は最初のメンバー（付帯要素込み）の開始位置。コード + 改行（+ 空行）を足す
    * ``"empty_body"``: 本体が空。波括弧の本体は ``{`` の直後、Python の ``pass`` / ``...`` だけの本体は
      ``replace`` の開始位置（``position == replace.start``）

    ``replace`` が空でないとき、その範囲（``pass`` と同じ行の末尾コメント）を置換してから挿入する。
    ``inline`` は、その ``pass`` が見出しと同じ行にある（``class C: pass``）ことを示す。この場合、置換テキストは
    改行から始めなければならない。
    """

    position: Position
    replace: Range | None = None
    anchor: Literal["after_member", "before_member", "empty_body"] = "after_member"
    inline: bool = False


def _unwrap_definition(node: Any, prof: _Profile) -> Any:
    """装飾つき定義・export 文を、中の定義ノードに戻す。"""
    while node.type in prof.wrappers:
        inner = node.child_by_field_name("definition") or node.child_by_field_name("declaration")
        if inner is None:
            named = [c for c in node.named_children if c.type not in prof.prefix_types]
            inner = named[-1] if named else None
        if inner is None:
            break
        node = inner
    return node


def _body_of(container: Any, prof: _Profile) -> Any:
    if container.type in prof.root_types:
        return container
    return container.child_by_field_name("body") or container


def _members(body: Any, prof: _Profile) -> list[Any]:
    return [c for c in body.named_children if not _is_comment(c, prof) and c.type not in prof.prefix_types]


def _is_docstring(node: Any) -> bool:
    if node.type != "expression_statement" or node.named_child_count != 1:
        return False
    return node.named_children[0].type in ("string", "concatenated_string")


def _is_placeholder(node: Any) -> bool:
    """Python の ``pass`` / ``...`` だけの文。"""
    if node.type == "pass_statement":
        return True
    return (
        node.type == "expression_statement"
        and node.named_child_count == 1
        and node.named_children[0].type == "ellipsis"
    )


def _plain_span(node: Any, parsed: ParsedFile, prof: _Profile) -> Range:
    """付帯要素を含めない範囲（開始はノード、終了は末尾コメントを含む行末）。"""
    return Range(
        _start_position(node.start_byte, parsed),
        _line_end(_trailing_end_line(node, parsed, prof), parsed),
    )


def insertion_point(
    parent_node: Any,
    parsed: ParsedFile,
    *,
    position: Literal["start", "end"],
    language_id: str,
) -> InsertionPoint:
    """親（クラス・構造体・モジュール）への ``insert_member`` の挿入位置（仕様書 §10.8）。

    * ``"end"``: 最後のメンバーの直後
    * ``"start"``: 最初のメンバーの前（Python の docstring があればその直後）
    * メンバーが無ければ本体の先頭。Python の ``pass`` だけの本体は ``pass`` を置換対象にする
    """
    prof = _profile(language_id)
    container = _unwrap_definition(parent_node, prof)
    body = _body_of(container, prof)
    members = _members(body, prof)

    doc = members[0] if prof.docstring and members and _is_docstring(members[0]) else None
    effective = members[1:] if doc is not None else members

    if prof.docstring and len(effective) == 1 and _is_placeholder(effective[0]):
        span = _plain_span(effective[0], parsed, prof)
        colon = body.prev_sibling
        inline = (
            colon is not None
            and colon.type == ":"
            and _first_line(colon, parsed) == _first_line(effective[0], parsed)
        )
        return InsertionPoint(span.start, replace=span, anchor="empty_body", inline=inline)

    if not members:
        brace = next((c for c in body.children if c.type == "{"), None)
        if brace is not None:
            return InsertionPoint(_start_position(brace.end_byte, parsed), anchor="empty_body")
        return InsertionPoint(_start_position(body.start_byte, parsed), anchor="empty_body")

    if position == "end":
        end = definition_range(members[-1], parsed, language_id=language_id).end
        return InsertionPoint(end, anchor="after_member")

    if effective:
        start = definition_range(effective[0], parsed, language_id=language_id).start
        return InsertionPoint(start, anchor="before_member")
    assert doc is not None
    return InsertionPoint(definition_range(doc, parsed, language_id=language_id).end, anchor="after_member")


# --- blank_lines_around ----------------------------------------------------------------


def blank_lines_around(node: Any, parsed: ParsedFile, *, language_id: str, toplevel: bool) -> int:
    """``insert_before`` / ``insert_after`` で ``node`` の前後に置く空行の数。

    言語の慣例で 2 行が標準の Python のトップレベル関数・クラス（デコレータつきを含む）は 2、それ以外は 1。
    """
    if language_id == "python" and toplevel:
        target = _unwrap_definition(node, _PYTHON)
        if target.type in ("function_definition", "class_definition"):
            return 2
    return 1
