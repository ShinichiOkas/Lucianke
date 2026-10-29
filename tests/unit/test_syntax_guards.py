"""``syntax/guards.py``（仕様書 T-07 処理規約 4）の単体テスト。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

from lucianke.core.textpos import LineIndex
from lucianke.syntax.guards import guard_for
from lucianke.syntax.types import ParsedFile

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"

_LANGS = {
    "python": Language(tree_sitter_python.language()),
    "typescript": Language(tree_sitter_typescript.language_typescript()),
}
_CALL_TYPE = {"python": "call", "typescript": "call_expression"}


def parse(source: bytes | str, language_id: str) -> ParsedFile:
    data = source.encode() if isinstance(source, str) else source
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


def call(parsed: ParsedFile, callee: str, nth: int = 0) -> Any:
    """``callee(...)`` の呼び出しノード（``nth`` 番目）。callee は呼ぶ式のテキスト。"""
    found = [
        n
        for n in walk(parsed.tree.root_node)
        if n.type == _CALL_TYPE[parsed.language_id]
        and n.child_by_field_name("function").text.decode() == callee
    ]
    return found[nth]


def py(src: str, callee: str, nth: int = 0, **kw: Any) -> str | None:
    parsed = parse(src, "python")
    return guard_for(call(parsed, callee, nth), parsed, language_id="python", **kw)


def ts(src: str, callee: str, nth: int = 0, **kw: Any) -> str | None:
    parsed = parse(src, "typescript")
    return guard_for(call(parsed, callee, nth), parsed, language_id="typescript", **kw)


# --- フィクスチャ（routes.py） ---------------------------------------------------------------


def test_fixture_create_order_condition_is_the_if_text() -> None:
    parsed = parse((POLY / "src/shop/api/routes.py").read_bytes(), "python")
    node = call(parsed, "processor.process_charge", 0)
    assert guard_for(node, parsed, language_id="python") == "req.amount > 0 and not paid"


def test_fixture_retry_order_inside_try_has_only_the_if() -> None:
    parsed = parse((POLY / "src/shop/api/routes.py").read_bytes(), "python")
    node = call(parsed, "processor.process_charge", 1)
    chain = []
    p = node.parent
    while p is not None:
        chain.append(p.type)
        p = p.parent
    assert "try_statement" in chain  # try の中にいる
    assert (
        guard_for(node, parsed, language_id="python") == "attempt < 3"
    )  # try 本体は条件にしない（on: は except 側だけ）


def test_fixture_calls_without_condition_have_none() -> None:
    parsed = parse((POLY / "src/shop/api/routes.py").read_bytes(), "python")
    assert guard_for(call(parsed, "PaymentRequest", 0), parsed, language_id="python") is None


# --- Python -----------------------------------------------------------------------------


def test_py_no_condition_is_none() -> None:
    assert py("def f():\n    g()\n", "g") is None


def test_py_if_condition_and_call_inside_condition() -> None:
    src = "def f(x):\n    if check(x):\n        g()\n"
    assert py(src, "g") == "check(x)"
    assert py(src, "check") is None  # 条件式の中の呼び出しは、その if では守られない


def test_py_else_is_negation() -> None:
    src = "def f(x):\n    if x > 1:\n        a()\n    else:\n        b()\n"
    assert py(src, "a") == "x > 1"
    assert py(src, "b") == "!(x > 1)"


def test_py_elif_chain() -> None:
    src = "def f(a, b):\n    if a:\n        p()\n    elif b:\n        q()\n    else:\n        r()\n"
    assert py(src, "p") == "a"
    assert py(src, "q") == "!(a) && b"
    assert py(src, "r") == "!(a) && !(b)"


def test_py_nested_conditions_are_joined_outside_in() -> None:
    src = "def f(a, b):\n    if a:\n        while b:\n            if c:\n                g()\n"
    assert py(src, "g") == "a && b && c"


def test_py_ternary() -> None:
    src = "def f(x):\n    y = a() if x else b()\n"
    assert py(src, "a") == "x"
    assert py(src, "b") == "!(x)"


def test_py_short_circuit() -> None:
    src = "def f(x, y):\n    x and a()\n    y or b()\n    z = x and k() or m()\n"
    assert py(src, "a") == "x"
    assert py(src, "b") == "!(y)"
    assert py(src, "k") == "x"
    assert py(src, "m") == "!(x and k())"  # 左辺の式全体の否定


def test_py_while_and_while_else() -> None:
    src = "def f(x):\n    while x > 1:\n        p()\n    else:\n        q()\n"
    assert py(src, "p") == "x > 1"
    assert py(src, "q") == "!(x > 1)"


def test_py_try_except_on_is_only_in_except() -> None:
    src = (
        "def f():\n    try:\n        a()\n    except (ValueError, KeyError) as e:\n        b()\n"
        "    except TypeError:\n        c()\n    except:\n        d()\n    else:\n        e()\n"
        "    finally:\n        z()\n"
    )
    assert py(src, "a") is None
    assert py(src, "b") == "on: (ValueError, KeyError)"
    assert py(src, "c") == "on: TypeError"
    assert py(src, "d") == "on: *"
    assert py(src, "e") is None
    assert py(src, "z") is None


def test_py_except_inside_if_keeps_outer_order() -> None:
    src = "def f(x):\n    if x:\n        try:\n            a()\n        except OSError:\n            b()\n"
    assert py(src, "b") == "x && on: OSError"


def test_py_match_case() -> None:
    src = "def f(x):\n    match x:\n        case 1 | 2 if x > 0:\n            n()\n        case _:\n            o()\n"
    assert py(src, "n") == "case 1 | 2 if x > 0"
    assert py(src, "o") == "case _"


def test_py_stops_at_enclosing_function() -> None:
    src = (
        "def outer(a):\n    if a:\n        def inner(b):\n            if b:\n                g()\n"
        "        f = lambda: h()\n"
    )
    assert py(src, "g") == "b"  # 外側の関数の条件は含めない
    assert py(src, "h") is None  # lambda の本体は別の関数


def test_py_multiline_condition_is_collapsed_to_one_line() -> None:
    src = "def f(a, b):\n    if (a and\n            b):\n        g()\n"
    assert py(src, "g") == "a and b"  # 条件全体を囲む括弧は外す


def test_py_or_condition_is_parenthesized_only_when_joined() -> None:
    src = "def f(a, b, c):\n    if a or b:\n        g()\n        if c:\n            h()\n"
    assert py(src, "g") == "a or b"
    assert py(src, "h") == "(a or b) && c"


def test_py_truncation_to_120_chars() -> None:
    long_cond = " and ".join(f"condition_number_{i}" for i in range(12))
    src = f"def f():\n    if {long_cond}:\n        g()\n"
    assert len(long_cond) > 120
    got = py(src, "g")
    assert got is not None
    assert len(got) == 120 and got.endswith("…")
    assert got[:-1] == long_cond[:119]
    # 上限ちょうどは省略しない
    exact = py(src, "g", max_chars=len(long_cond))
    assert exact == long_cond
    assert py(src, "g", max_chars=10) == long_cond[:9] + "…"


def test_py_truncation_counts_whole_joined_string() -> None:
    a, b = "a" * 70, "b" * 70
    src = f"def f():\n    if {a}:\n        if {b}:\n            g()\n"
    got = py(src, "g")
    assert got == f"{a} && {b}"[:119] + "…"


def test_py_module_level_call_under_if() -> None:
    assert py("import os\nif os.name == 'nt':\n    setup()\n", "setup") == "os.name == 'nt'"


# --- TypeScript -------------------------------------------------------------------------


def test_ts_if_strips_parentheses() -> None:
    src = "function f(x: number) {\n  if (x > 0 && ok) {\n    g();\n  }\n}\n"
    assert ts(src, "g") == "x > 0 && ok"


def test_ts_no_condition_is_none() -> None:
    assert ts("function f() { g(); }\n", "g") is None


def test_ts_fixture_if_in_charge() -> None:
    # web/src/app.ts には if が無い。payment.ts の throw は呼び出しでないので、ソース文字列で確かめる
    src = (
        (POLY / "web/src/payment.ts")
        .read_text(encoding="utf-8")
        .replace('throw new Error("amount must be > 0");', 'fail("amount must be > 0");')
    )
    assert ts(src, "fail") == "req.amount <= 0"


def test_ts_else_if_else_chain() -> None:
    src = "function f(a: boolean, b: boolean) {\n  if (a) { p(); } else if (b) { q(); } else { r(); }\n}\n"
    assert ts(src, "p") == "a"
    assert ts(src, "q") == "!(a) && b"
    assert ts(src, "r") == "!(a) && !(b)"


def test_ts_or_condition_is_parenthesized_only_when_joined() -> None:
    src = "function f(a: boolean, b: boolean, c: boolean) {\n  if (a || b) {\n    g();\n    if (c) { h(); }\n  }\n}\n"
    assert ts(src, "g") == "a || b"
    assert ts(src, "h") == "(a || b) && c"


def test_ts_ternary_and_short_circuit() -> None:
    src = "function f(x: boolean, y: boolean) {\n  const v = x ? a() : b();\n  const w = x && k() || m();\n  y || n();\n}\n"
    assert ts(src, "a") == "x"
    assert ts(src, "b") == "!(x)"
    assert ts(src, "k") == "x"
    assert ts(src, "m") == "!(x && k())"
    assert ts(src, "n") == "!(y)"


def test_ts_while_catch_switch() -> None:
    src = (
        "function f(x: number) {\n  while (x > 1) { u(); }\n"
        "  try { a(); } catch (e) { b(); } finally { c(); }\n"
        "  try { a2(); } catch (e: unknown) { b2(); }\n"
        "  switch (x) { case 1: p(); break; default: q(); }\n}\n"
    )
    assert ts(src, "u") == "x > 1"
    assert ts(src, "a") is None
    assert ts(src, "b") == "on: catch"
    assert ts(src, "b2") == "on: unknown"
    assert ts(src, "c") is None
    assert ts(src, "p") == "case 1"
    assert ts(src, "q") == "default"


def test_ts_stops_at_arrow_and_method() -> None:
    src = (
        "class A {\n  m(a: boolean) {\n    if (a) {\n      const h = () => g();\n      k();\n    }\n  }\n}\n"
    )
    assert ts(src, "g") is None
    assert ts(src, "k") == "a"


def test_ts_truncation() -> None:
    cond = " && ".join(f"flag_number_{i}" for i in range(20))
    got = ts(f"function f() {{\n  if ({cond}) {{ g(); }}\n}}\n", "g")
    assert got is not None and len(got) == 120 and got.endswith("…")


# --- 非対応言語 -------------------------------------------------------------------------


def test_unsupported_language_returns_none() -> None:
    parsed = parse("def f(x):\n    if x:\n        g()\n", "python")
    assert guard_for(call(parsed, "g"), parsed, language_id="go") is None
