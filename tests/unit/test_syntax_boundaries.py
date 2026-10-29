"""``syntax/boundaries.py``（仕様書 §10.8）の単体テスト。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_rust
import tree_sitter_typescript
from tree_sitter import Language, Parser

from lucianke.core.model import Position, Range
from lucianke.core.textpos import LineIndex, strip_bom
from lucianke.syntax.boundaries import (
    blank_lines_around,
    definition_range,
    insertion_point,
)
from lucianke.syntax.types import ParsedFile

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"

_LANGS = {
    "python": Language(tree_sitter_python.language()),
    "typescript": Language(tree_sitter_typescript.language_typescript()),
    "javascript": Language(tree_sitter_javascript.language()),
    "rust": Language(tree_sitter_rust.language()),
}


def parse(source: bytes | str, language_id: str, *, keep_bom: bool = True) -> ParsedFile:
    data = source.encode() if isinstance(source, str) else source
    if not keep_bom:
        data = strip_bom(data)
    tree = Parser(_LANGS[language_id]).parse(data)
    return ParsedFile(
        language_id=language_id,
        sha256=hashlib.sha256(data).hexdigest(),
        source=data,
        tree=tree,
        line_index=LineIndex(data),
        has_errors=tree.root_node.has_error,
    )


def walk(node: Any) -> Iterator[Any]:
    yield node
    for child in node.children:
        yield from walk(child)


def find(parsed: ParsedFile, node_type: str, name: str) -> Any:
    """``name`` フィールドが ``name`` の ``node_type`` ノード（最初の 1 件）。"""
    for node in walk(parsed.tree.root_node):
        if node.type == node_type:
            ident = node.child_by_field_name("name")
            if ident is not None and ident.text.decode() == name:
                return node
    raise AssertionError(f"{node_type} {name!r} not found")


def find_method(parsed: ParsedFile, cls: str, name: str) -> Any:
    owner = find(parsed, "class_definition", cls)
    for node in walk(owner):
        if node.type == "function_definition":
            ident = node.child_by_field_name("name")
            if ident is not None and ident.text.decode() == name:
                return node
    raise AssertionError(f"{cls}.{name} not found")


def cut(parsed: ParsedFile, rng: Range) -> str:
    """範囲の文字列。終了は行末であることも確かめる。"""
    li = parsed.line_index
    lines = [li.line_text(i) for i in range(rng.start.line, rng.end.line + 1)]
    assert rng.end.col == len(lines[-1]), (rng, lines[-1])
    lines[0] = lines[0][rng.start.col :]
    return "\n".join(lines)


def py_range(src: str, node_type: str, name: str) -> tuple[Range, ParsedFile]:
    parsed = parse(src, "python")
    return definition_range(find(parsed, node_type, name), parsed, language_id="python"), parsed


def ts_range(src: str, node_type: str, name: str) -> tuple[Range, ParsedFile]:
    parsed = parse(src, "typescript")
    return definition_range(find(parsed, node_type, name), parsed, language_id="typescript"), parsed


# --- Python: フィクスチャ -----------------------------------------------------------------


@pytest.fixture(scope="module")
def payment() -> ParsedFile:
    return parse((POLY / "src/shop/services/payment.py").read_bytes(), "python")


def test_decorator_and_docstring_are_included(payment: ParsedFile) -> None:
    node = find_method(payment, "PaymentProcessor", "process_charge")
    rng = definition_range(node, payment, language_id="python")
    lines = cut(payment, rng).split("\n")
    assert rng.start.col == 4  # 開始列はデコレータの列
    assert lines[0] == "@transactional"
    assert lines[1] == "    async def process_charge(self, req: PaymentRequest) -> Receipt:"
    assert lines[2] == '        """注文に対する決済を実行し領収書を発行する。'
    assert lines[-1] == "        return Receipt(order_id=req.order_id, amount=req.amount)"


def test_decorated_definition_node_and_inner_node_give_same_range(payment: ParsedFile) -> None:
    inner = find_method(payment, "PaymentProcessor", "process_charge")
    outer = inner.parent
    assert outer.type == "decorated_definition"
    assert definition_range(inner, payment, language_id="python") == definition_range(
        outer, payment, language_id="python"
    )


def test_comment_inside_body_is_part_of_body_not_leading(payment: ParsedFile) -> None:
    node = find_method(payment, "PaymentProcessor", "_validate_limit")
    lines = cut(payment, definition_range(node, payment, language_id="python")).split("\n")
    assert lines[0] == "def _validate_limit(self, amount: Decimal) -> bool:"
    assert lines[1] == "        # 通貨ごとの上限を確認する"  # 本体の最初の行のコメントは本体の一部
    assert lines[-1] == "        return amount <= limit"


def test_range_excludes_following_blank_line(payment: ParsedFile) -> None:
    node = find_method(payment, "PaymentProcessor", "_validate_limit")
    rng = definition_range(node, payment, language_id="python")
    assert payment.line_index.line_text(rng.end.line + 1) == ""  # 直後は空行。範囲に含めない


def test_toplevel_function_and_class_ranges(payment: ParsedFile) -> None:
    fn = find(payment, "function_definition", "transactional")
    first = cut(payment, definition_range(fn, payment, language_id="python")).split("\n")[0]
    assert first == "def transactional(fn):"
    cls = find(payment, "class_definition", "PaymentRequest")
    lines = cut(payment, definition_range(cls, payment, language_id="python")).split("\n")
    assert lines[0] == "@dataclass"
    assert lines[-1] == '    currency: str = "JPY"'


# --- Python: コメントの規則 ------------------------------------------------------------


def test_ordinary_comment_after_blank_line_is_excluded() -> None:
    src = "# 別の話\n\n# 直前\ndef f():\n    return 1\n"
    rng, parsed = py_range(src, "function_definition", "f")
    assert cut(parsed, rng) == "# 直前\ndef f():\n    return 1"


def test_comment_chain_stops_at_blank_line() -> None:
    src = "# A\n\n# B1\n# B2\ndef f():\n    pass\n"
    rng, parsed = py_range(src, "function_definition", "f")
    assert cut(parsed, rng).split("\n")[0] == "# B1"


def test_comment_separated_by_blank_line_is_excluded() -> None:
    src = "# 離れたコメント\n\ndef f():\n    pass\n"
    rng, _ = py_range(src, "function_definition", "f")
    assert rng.start == Position(2, 0)


def test_comment_above_decorator_is_included() -> None:
    src = "import os\n# メモ\n@dec\n@dec2(1)\ndef f():\n    pass\n"
    rng, parsed = py_range(src, "function_definition", "f")
    assert cut(parsed, rng) == "# メモ\n@dec\n@dec2(1)\ndef f():\n    pass"


def test_trailing_comment_of_previous_statement_is_not_leading() -> None:
    src = "x = 1  # 直前の行の末尾コメント\ndef f():\n    pass\n"
    rng, _ = py_range(src, "function_definition", "f")
    assert rng.start == Position(1, 0)


def test_trailing_same_line_comment_inside_function() -> None:
    src = "def f(): return 1  # 末尾\n\nx = 2\n"
    rng, parsed = py_range(src, "function_definition", "f")
    assert cut(parsed, rng) == "def f(): return 1  # 末尾"


def test_trailing_same_line_comment_sibling_of_statement() -> None:
    # 文の直後のコメントは兄弟ノード。モジュール直下の代入でも含める
    parsed = parse("X = 1  # 注\nY = 2\n", "python")
    assign = parsed.tree.root_node.children[0]
    assert cut(parsed, definition_range(assign, parsed, language_id="python")) == "X = 1  # 注"


def test_own_line_comment_after_definition_is_not_trailing() -> None:
    src = "def f():\n    pass\n# 次の定義のコメント\ndef g():\n    pass\n"
    rng, parsed = py_range(src, "function_definition", "f")
    assert cut(parsed, rng) == "def f():\n    pass"


def test_async_def_and_nested_method_range() -> None:
    src = (
        "class A:\n    x = 1\n\n    # doc of m\n    @property\n    async def m(self):\n"
        "        return 1\n\n    def n(self): ...\n"
    )
    parsed = parse(src, "python")
    m = find_method(parsed, "A", "m")
    assert cut(parsed, definition_range(m, parsed, language_id="python")) == (
        "# doc of m\n    @property\n    async def m(self):\n        return 1"
    )
    n = find_method(parsed, "A", "n")  # 前の空行で隔てられ、直前にコメントは無い
    assert cut(parsed, definition_range(n, parsed, language_id="python")) == "def n(self): ..."


def test_columns_are_codepoints_not_bytes() -> None:
    src = 'def f(): return "あ😀"  # 日本語\n'
    rng, _ = py_range(src, "function_definition", "f")
    assert rng.end == Position(0, len(src.rstrip("\n")))
    assert rng.end.col != len(src.encode())  # バイト数ではない


# --- BOM・CRLF -----------------------------------------------------------------------------


@pytest.mark.parametrize("keep_bom", [True, False])
def test_crlf_bom_fixture_ranges_do_not_shift(keep_bom: bool) -> None:
    data = (POLY / "src/shop/models/crlf_bom.py").read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and b"\r\n" in data
    parsed = parse(data, "python", keep_bom=keep_bom)

    fn = find(parsed, "function_definition", "crlf_function")
    rng = definition_range(fn, parsed, language_id="python")
    assert cut(parsed, rng) == 'def crlf_function(x: int) -> int:\n    """Doubles x."""\n    return x * 2'
    assert rng.start == Position(3, 0)

    cls = find(parsed, "class_definition", "CrlfClass")
    crng = definition_range(cls, parsed, language_id="python")
    assert cut(parsed, crng) == 'class CrlfClass:\n    def method(self) -> str:\n        return "crlf"'
    assert crng.start.line == 8

    # 1 行目（BOM つき）の docstring: 開始は (0, 0)。BOM の 3 バイトぶんずれない
    doc = parsed.tree.root_node.children[0]
    assert doc.type == "expression_statement"
    drng = definition_range(doc, parsed, language_id="python")
    assert drng == Range(Position(0, 0), Position(0, len('"""CRLF and BOM fixture."""')))


@pytest.mark.parametrize("keep_bom", [True, False])
def test_bom_on_first_line_definition(keep_bom: bool) -> None:
    data = b"\xef\xbb\xbfdef f():\r\n    return 1  # c\r\n"
    parsed = parse(data, "python", keep_bom=keep_bom)
    rng = definition_range(find(parsed, "function_definition", "f"), parsed, language_id="python")
    assert rng == Range(Position(0, 0), Position(1, len("    return 1  # c")))


def test_crlf_comment_and_decorator_chain() -> None:
    data = b"# c1\r\n@d\r\ndef f():\r\n    pass\r\n\r\nx = 1\r\n"
    parsed = parse(data, "python")
    rng = definition_range(find(parsed, "function_definition", "f"), parsed, language_id="python")
    assert rng == Range(Position(0, 0), Position(3, len("    pass")))


# --- TypeScript / JavaScript ----------------------------------------------------------


def test_ts_jsdoc_and_export_async() -> None:
    src = "const a = 1;\n\n/** 説明 */\nexport async function foo(): Promise<void> {}\n"
    rng, parsed = ts_range(src, "function_declaration", "foo")
    assert cut(parsed, rng) == "/** 説明 */\nexport async function foo(): Promise<void> {}"


def test_ts_jsdoc_separated_by_blank_line_is_still_included() -> None:
    src = "/** 常に含める */\n\nexport function foo() {}\n"
    rng, _ = ts_range(src, "function_declaration", "foo")
    assert rng.start == Position(0, 0)


def test_ts_plain_comment_rules() -> None:
    src = "// 遠いコメント\n\n// 近いコメント\nexport function foo() {}\n"
    rng, parsed = ts_range(src, "function_declaration", "foo")
    assert cut(parsed, rng) == "// 近いコメント\nexport function foo() {}"
    rng2, _ = ts_range("// 遠いコメント\n\nexport function foo() {}\n", "function_declaration", "foo")
    assert rng2.start == Position(2, 0)


def test_ts_trailing_comment_is_included_and_previous_trailing_is_not_leading() -> None:
    src = "const x = 1; // x の末尾\nexport default function z() {} // z の末尾\n"
    rng, parsed = ts_range(src, "function_declaration", "z")
    assert cut(parsed, rng) == "export default function z() {} // z の末尾"


def test_ts_class_decorator_before_export() -> None:
    src = "/** doc */\n@Component({ selector: 'x' })\nexport class B {\n  a = 1;\n}\n\nconst y = 2;\n"
    rng, parsed = ts_range(src, "class_declaration", "B")
    assert cut(parsed, rng) == "/** doc */\n@Component({ selector: 'x' })\nexport class B {\n  a = 1;\n}"


def test_ts_member_decorators_are_siblings_in_class_body() -> None:
    src = (
        "class A {\n  x = 1;\n\n  /** d */\n  @dec()\n  @other\n  static async n() {}\n"
        "  @Input() name: string;\n}\n"
    )
    parsed = parse(src, "typescript")
    n = find(parsed, "method_definition", "n")
    assert cut(parsed, definition_range(n, parsed, language_id="typescript")) == (
        "/** d */\n  @dec()\n  @other\n  static async n() {}"
    )
    field = next(x for x in walk(parsed.tree.root_node) if x.type == "public_field_definition" and b"name" in x.text)
    assert cut(parsed, definition_range(field, parsed, language_id="typescript")) == "@Input() name: string;"


def test_ts_const_arrow_function_promotes_to_declaration_and_export() -> None:
    src = "/** a */\nexport const arrow = () => 1; // 末尾\nconst b = 1, c = 2;\n"
    parsed = parse(src, "typescript")
    decl = find(parsed, "variable_declarator", "arrow")
    assert cut(parsed, definition_range(decl, parsed, language_id="typescript")) == (
        "/** a */\nexport const arrow = () => 1; // 末尾"
    )
    # 宣言子が複数なら、宣言子そのもの
    c = find(parsed, "variable_declarator", "c")
    assert cut(parsed, definition_range(c, parsed, language_id="typescript")).startswith("c = 2")


def test_js_uses_same_rules() -> None:
    parsed = parse("/** d */\nexport function f() {}\n", "javascript")
    fn = next(x for x in walk(parsed.tree.root_node) if x.type == "function_declaration")
    assert definition_range(fn, parsed, language_id="javascript").start == Position(0, 0)


def test_ts_fixture_method_with_jsdoc_and_async() -> None:
    parsed = parse((POLY / "web/src/payment.ts").read_bytes(), "typescript")
    charge = next(x for x in walk(parsed.tree.root_node) if x.type == "method_definition" and b"retries = 0" in x.text)
    lines = cut(parsed, definition_range(charge, parsed, language_id="typescript")).split("\n")
    assert lines[0] == "/** 決済を要求する。amount は 0 より大きいこと。 */"
    assert lines[1] == "  async charge(req: PaymentRequest, retries = 0): Promise<string> {"


# --- Rust と既定 ------------------------------------------------------------------------


def test_rust_attributes_and_rustdoc() -> None:
    src = "// 遠い\n\n/// doc\n#[derive(Debug)]\n#[allow(dead_code)]\npub struct S;\n"
    parsed = parse(src, "rust")
    node = next(x for x in walk(parsed.tree.root_node) if x.type == "struct_item")
    assert cut(parsed, definition_range(node, parsed, language_id="rust")) == (
        "/// doc\n#[derive(Debug)]\n#[allow(dead_code)]\npub struct S;"
    )


def test_unknown_language_gives_node_range_and_line_end() -> None:
    parsed = parse("fn f() { 1 } // t\n", "rust")
    node = next(x for x in walk(parsed.tree.root_node) if x.type == "function_item")
    rng = definition_range(node, parsed, language_id="no_such_language")
    assert rng == Range(Position(0, 0), Position(0, len("fn f() { 1 } // t")))  # 既定は付帯要素なし。終了は行末


# --- insertion_point ---------------------------------------------------------------------


def test_insert_member_end_of_nonempty_class(payment: ParsedFile) -> None:
    cls = find(payment, "class_definition", "PaymentProcessor")
    ip = insertion_point(cls, payment, position="end", language_id="python")
    last = find_method(payment, "PaymentProcessor", "apply_discount")
    assert ip.anchor == "after_member" and ip.replace is None
    assert ip.position == definition_range(last, payment, language_id="python").end
    assert payment.line_index.line_text(ip.position.line).strip().startswith("return price *")


def test_insert_member_start_skips_docstring(payment: ParsedFile) -> None:
    cls = find(payment, "class_definition", "PaymentProcessor")
    ip = insertion_point(cls, payment, position="start", language_id="python")
    init = find_method(payment, "PaymentProcessor", "__init__")
    assert ip.anchor == "before_member"
    assert ip.position == definition_range(init, payment, language_id="python").start


def test_insert_member_pass_only_body_is_replaced() -> None:
    src = "class A:\n    pass\n\nclass B:\n    pass  # todo\n"
    parsed = parse(src, "python")
    a = insertion_point(find(parsed, "class_definition", "A"), parsed, position="end", language_id="python")
    assert a.anchor == "empty_body" and not a.inline
    assert a.replace == Range(Position(1, 4), Position(1, 8))
    assert a.position == a.replace.start
    b = insertion_point(find(parsed, "class_definition", "B"), parsed, position="start", language_id="python")
    assert b.replace == Range(Position(4, 4), Position(4, len("    pass  # todo")))  # 同じ行の末尾コメントごと


def test_insert_member_inline_pass_and_ellipsis_and_docstring() -> None:
    src = 'class C: pass\nclass P(Protocol): ...\nclass D:\n    """doc"""\n    pass\nclass E:\n    """only doc"""\n'
    parsed = parse(src, "python")
    c = insertion_point(find(parsed, "class_definition", "C"), parsed, position="end", language_id="python")
    assert c.inline and c.replace == Range(Position(0, 9), Position(0, 13))
    p = insertion_point(find(parsed, "class_definition", "P"), parsed, position="end", language_id="python")
    assert p.inline and p.replace == Range(Position(1, 19), Position(1, 22))
    d = insertion_point(find(parsed, "class_definition", "D"), parsed, position="end", language_id="python")
    assert d.replace == Range(Position(4, 4), Position(4, 8)) and not d.inline
    e_node = find(parsed, "class_definition", "E")
    for where in ("start", "end"):
        e = insertion_point(e_node, parsed, position=where, language_id="python")  # type: ignore[arg-type]
        assert e.anchor == "after_member" and e.replace is None
        assert e.position == Position(6, len('    """only doc"""'))


def test_insert_member_toplevel_module(payment: ParsedFile) -> None:
    root = payment.tree.root_node
    end = insertion_point(root, payment, position="end", language_id="python")
    cls = find(payment, "class_definition", "PaymentProcessor")
    assert end.position == definition_range(cls, payment, language_id="python").end
    start = insertion_point(root, payment, position="start", language_id="python")
    assert start.anchor == "before_member"
    assert payment.line_index.line_text(start.position.line) == "from __future__ import annotations"


def test_insert_member_decorated_class_parent() -> None:
    parsed = parse("@dataclass\nclass A:\n    x: int = 0\n", "python")
    deco = parsed.tree.root_node.children[0]
    ip = insertion_point(deco, parsed, position="end", language_id="python")
    assert ip.position == Position(2, len("    x: int = 0"))


def test_insert_member_ts_class() -> None:
    src = "export class A {\n  x = 1;\n  m() {} // t\n}\nclass E {}\nclass F {\n}\n"
    parsed = parse(src, "typescript")
    a = find(parsed, "class_declaration", "A")
    end = insertion_point(a, parsed, position="end", language_id="typescript")
    assert end.anchor == "after_member" and end.position == Position(2, len("  m() {} // t"))
    start = insertion_point(a, parsed, position="start", language_id="typescript")
    assert start.anchor == "before_member" and start.position == Position(1, 2)
    e = insertion_point(find(parsed, "class_declaration", "E"), parsed, position="end", language_id="typescript")
    assert e.anchor == "empty_body" and e.position == Position(4, len("class E {"))
    f = insertion_point(find(parsed, "class_declaration", "F"), parsed, position="start", language_id="typescript")
    assert f.anchor == "empty_body" and f.position == Position(5, len("class F {"))


def test_insert_member_ts_start_includes_decorator() -> None:
    parsed = parse("class A {\n  @dec()\n  m() {}\n}\n", "typescript")
    a = find(parsed, "class_declaration", "A")
    ip = insertion_point(a, parsed, position="start", language_id="typescript")
    assert ip.position == Position(1, 2)


# --- blank_lines_around ------------------------------------------------------------------


def test_blank_lines_around(payment: ParsedFile) -> None:
    fn = find(payment, "function_definition", "transactional")
    assert blank_lines_around(fn, payment, language_id="python", toplevel=True) == 2
    cls = find(payment, "class_definition", "PaymentRequest")  # デコレータつき
    assert blank_lines_around(cls.parent, payment, language_id="python", toplevel=True) == 2
    method = find_method(payment, "PaymentProcessor", "_validate_limit")
    assert blank_lines_around(method, payment, language_id="python", toplevel=False) == 1
    imp = payment.tree.root_node.children[2]  # import 文
    assert blank_lines_around(imp, payment, language_id="python", toplevel=True) == 1  # 関数・クラス以外


def test_blank_lines_around_ts_is_one() -> None:
    parsed = parse("function f() {}\n", "typescript")
    fn = parsed.tree.root_node.children[0]
    assert blank_lines_around(fn, parsed, language_id="typescript", toplevel=True) == 1
