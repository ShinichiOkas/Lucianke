"""``def`` / ``class`` の中の import が束縛した名前の使用（BUG-0048）。

関数の中の ``from x import Y`` で束縛した名前は、その関数（と内側の関数）の使用を ``Y`` への参照にする。以前は
「関数のローカル」として使用箇所を落としていた（import の行だけが残る）。モジュール直下の同名の import や、別の関数の
同名の import は巻き込まない。``engine/indexer.py`` と同じ配線（``ImportFact.nested`` で名前表を分け、文の位置で引く）で確かめる。

後半は TS / JS の関数の中の ``const { foo } = require("./foo")``（BUG-0052）。Python と同じ仕組みで、関数の中の ``require`` が束縛した
名前の使用を、その ``require`` の先への参照にする（ローカル変数として落とさない）。
"""

from __future__ import annotations

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

from lucianke.core.model import RelPath
from lucianke.core.textpos import LineIndex
from lucianke.syntax.builtin_names import BUILTINS_PACKAGE, GLOBALS_PACKAGE
from lucianke.syntax.imports import (
    ImportFact,
    external_name_map,
    extract_imports,
    imported_name_map,
    make_resolver,
)
from lucianke.syntax.relations import ImportKey, RelationCandidate, extract_relations
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import ParsedFile

WS = "ws"
FILES = {"app/errors.py", "app/other.py", "app/__init__.py"}
ERR = f"lk://{WS}/app/errors.py#AppError"
OTHER = f"lk://{WS}/app/other.py#AppError"
LANGUAGE = Language(tree_sitter_python.language())


def _parsed(source: bytes) -> ParsedFile:
    tree = Parser(LANGUAGE).parse(source)
    return ParsedFile(
        language_id="python",
        sha256="0" * 64,
        source=source,
        tree=tree,
        line_index=LineIndex(source),
        has_errors=tree.root_node.has_error,
    )


def _relations(text: str, *, scoped: bool = True) -> list[RelationCandidate]:
    """索引器と同じ配線: モジュール直下の import は名前表、``def`` / ``class`` の中の import は文の位置ごとの名前表。"""
    source = text.encode()
    parsed = _parsed(source)
    rel = RelPath("tests/test_x.py")
    facts = extract_imports(parsed, language_id="python")
    resolver = make_resolver(from_rel=rel, language_id="python", exists=lambda p: str(p) in FILES)
    module_facts = [f for f in facts if not f.nested]
    nested: dict[ImportKey, list[ImportFact]] = {}
    for f in facts:
        if f.nested:
            nested.setdefault((f.range.start.line, f.range.start.col), []).append(f)
    names = imported_name_map(module_facts, resolver)
    external = external_name_map(module_facts, language_id="python", internal=names)

    def urn_of(table: dict[str, tuple[RelPath, str]], ext: dict[str, str], name: str) -> str | None:
        hit = table.get(name)
        if hit is None:
            return ext.get(name)
        path, original = hit
        return f"lk://{WS}/{path}" if original == "*" else f"lk://{WS}/{path}#{original}"

    def resolve_import(key: ImportKey, name: str) -> str | None:
        group = nested.get(key, [])
        table = imported_name_map(group, resolver)
        return urn_of(table, external_name_map(group, language_id="python", internal=table), name)

    symbols = extract(
        parsed,
        language_id="python",
        language=LANGUAGE,
        query_source=load_query_source("python"),
        workspace_id=WS,
        rel=rel,
    )
    return extract_relations(
        parsed,
        language_id="python",
        language=LANGUAGE,
        symbols=symbols,
        workspace_id=WS,
        rel=rel,
        resolve_name=lambda n: urn_of(names, external, n),
        builtins_package=BUILTINS_PACKAGE,
        resolve_import=resolve_import if scoped else None,
    )


def _uses(rels: list[RelationCandidate], name: str) -> list[tuple[int, str, str | None]]:
    """``name`` の使用（IMPORTS を除く）: (行, 種別, 目標 URN)。行は 0 始まり。"""
    return [
        (r.call_site_line, r.relation_kind, r.target_urn)
        for r in rels
        if r.target_name == name and r.relation_kind != "IMPORTS"
    ]


def _imports(rels: list[RelationCandidate], name: str) -> list[tuple[int, str | None]]:
    return [
        (r.call_site_line, r.target_urn)
        for r in rels
        if r.target_name == name and r.relation_kind == "IMPORTS"
    ]


def test_use_after_a_function_local_import_is_a_reference():
    """BUG-0048 の形: 関数の中の import で束縛した名前を ``pytest.raises(...)`` で使う。import の行も使用の行も同じ対象。"""
    rels = _relations(
        "import pytest\n"
        "\n"
        "\n"
        "def test_raises():\n"
        "    from app.errors import AppError\n"
        "\n"
        "    with pytest.raises(AppError):\n"
        "        raise AppError()\n"
    )
    assert _imports(rels, "AppError") == [(4, ERR)]
    assert _uses(rels, "AppError") == [(6, "REFERENCES", ERR), (7, "CALLS", ERR)]
    source = {r.source_urn for r in rels if r.target_name == "AppError" and r.relation_kind != "IMPORTS"}
    assert source == {f"lk://{WS}/tests/test_x.py#test_raises"}


def test_use_after_a_function_local_import_with_alias_and_module_import():
    rels = _relations(
        "def f():\n"
        "    from app.errors import AppError as E\n"
        "    import app.other as o\n"
        "    import os.path\n"
        "    return E(), o.AppError(), os.path.join('a')\n"
    )
    assert _uses(rels, "E") == [(4, "CALLS", ERR)]
    assert _uses(rels, "AppError") == [(4, "CALLS", OTHER)]  # モジュール別名 o の属性
    assert _uses(rels, "join") == [(4, "CALLS", "lk://_ext/os#path.join")]


def test_nested_function_sees_the_import_of_the_enclosing_function():
    rels = _relations(
        "def outer():\n    from app.errors import AppError\n\n    def inner():\n        return AppError()\n\n    return inner\n"
    )
    assert _uses(rels, "AppError") == [(4, "CALLS", ERR)]


def test_class_body_import_is_seen_in_the_class_body_not_in_its_methods():
    rels = _relations(
        "class C:\n"
        "    from app.errors import AppError\n"
        "\n"
        "    x = AppError\n"
        "\n"
        "    def m(self):\n"
        "        return AppError()\n"
    )
    assert _imports(rels, "AppError") == [(1, ERR)]
    assert _uses(rels, "AppError")[0] == (3, "REFERENCES", ERR)
    # メソッドからは見えない（モジュールにも束縛が無いので解決先は無い）
    assert _uses(rels, "AppError")[1:] == [(6, "CALLS", None)]


def test_type_checking_import_at_module_level_still_resolves():
    """``if TYPE_CHECKING:`` のモジュール直下の import は従来どおり名前表（関数の中ではない）。"""
    rels = _relations(
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from app.errors import AppError\n"
        "\n"
        "\n"
        "def f(e: 'AppError') -> AppError:\n"
        "    return AppError()\n"
    )
    assert _imports(rels, "AppError") == [(3, ERR)]
    assert (7, "CALLS", ERR) in _uses(rels, "AppError")
    assert (6, "TYPE_USES", ERR) in _uses(rels, "AppError")


def test_type_checking_import_inside_a_function_is_scoped_to_it():
    rels = _relations(
        "from typing import TYPE_CHECKING\n"
        "\n"
        "\n"
        "def f():\n"
        "    if TYPE_CHECKING:\n"
        "        from app.errors import AppError\n"
        "\n"
        "    x: AppError = AppError()\n"
        "    return x\n"
        "\n"
        "\n"
        "def g():\n"
        "    return AppError()\n"
    )
    assert _imports(rels, "AppError") == [(5, ERR)]
    uses = _uses(rels, "AppError")
    assert (7, "TYPE_USES", ERR) in uses and (7, "CALLS", ERR) in uses
    assert (12, "CALLS", None) in uses  # g には束縛が無い


def test_same_name_in_another_scope_is_not_mixed_up():
    """モジュール直下・関数 f・関数 g が同じ名前を別の対象に束ねる。それぞれの使用はそれぞれの import の先。"""
    rels = _relations(
        "from app.errors import AppError\n"
        "\n"
        "\n"
        "def f():\n"
        "    from app.other import AppError\n"
        "\n"
        "    return AppError()\n"
        "\n"
        "\n"
        "def g():\n"
        "    return AppError()\n"
        "\n"
        "\n"
        "def h():\n"
        "    from app.errors import AppError\n"
        "\n"
        "    return AppError()\n"
        "\n"
        "\n"
        "x = AppError()\n"
    )
    assert _imports(rels, "AppError") == [(0, ERR), (4, OTHER), (14, ERR)]
    assert _uses(rels, "AppError") == [
        (6, "CALLS", OTHER),
        (10, "CALLS", ERR),  # g: モジュール直下の import
        (16, "CALLS", ERR),
        (19, "CALLS", ERR),  # f の import に引きずられない
    ]


def test_a_parameter_of_the_inner_function_hides_the_outer_import():
    rels = _relations(
        "def outer():\n"
        "    from app.errors import AppError\n"
        "\n"
        "    def inner(AppError):\n"
        "        return AppError()\n"
        "\n"
        "    return AppError()\n"
    )
    assert _uses(rels, "AppError") == [(6, "CALLS", ERR)]  # inner の使用はパラメータ（参照に数えない）


def test_receiver_through_a_function_local_import_resolves_the_member():
    rels = _relations("def f():\n    from app.errors import AppError\n\n    return AppError.create()\n")
    assert _uses(rels, "create") == [(3, "CALLS", f"{ERR}.create")]


def test_a_local_import_hides_a_same_named_module_variable_type():
    """モジュール直下の ``x = Foo()`` の型が、関数の中の ``import x`` の使用に漏れない。"""
    rels = _relations(
        "class Foo:\n    def run(self): ...\n\n\nx = Foo()\n\n\ndef f():\n    import x\n\n    return x.run()\n"
    )
    assert _uses(rels, "run") == [(10, "CALLS", "lk://_ext/x#run")]


def test_without_resolve_import_the_use_is_still_a_candidate():
    """``resolve_import`` を渡さない呼び手（名前表がスコープを区別しない）でも、使用箇所は落ちずに名前表で引かれる。"""
    rels = _relations(
        "def f():\n    from app.errors import AppError\n\n    return AppError()\n", scoped=False
    )
    # モジュール直下の名前表に無い（nested は名前表に入れない）ので、解決先は無いが、使用は候補に残る
    assert [(line, kind) for line, kind, _ in _uses(rels, "AppError")] == [(3, "CALLS")]


# --- TS / JS: 関数の中の require（BUG-0052） ------------------------------------------------

JS_LANGUAGES = {
    "javascript": Language(tree_sitter_javascript.language()),
    "typescript": Language(tree_sitter_typescript.language_typescript()),
}
JS_FILES = {"javascript": {"app/foo.js", "app/bar.js"}, "typescript": {"app/foo.ts", "app/bar.ts"}}
FOO = f"lk://{WS}/app/foo.js"
BAR = f"lk://{WS}/app/bar.js"
TS_FOO = f"lk://{WS}/app/foo.ts"


def _js_parsed(language_id: str, source: bytes) -> ParsedFile:
    tree = Parser(JS_LANGUAGES[language_id]).parse(source)
    return ParsedFile(
        language_id=language_id,
        sha256="0" * 64,
        source=source,
        tree=tree,
        line_index=LineIndex(source),
        has_errors=tree.root_node.has_error,
    )


def _js_relations(
    text: str, *, language_id: str = "javascript", rel: str = "app/m.js", scoped: bool = True
) -> list[RelationCandidate]:
    """索引器と同じ配線（Python の ``_relations`` と同じ）: モジュール直下の import は名前表、関数の中の ``require`` は文の位置ごと。"""
    parsed = _js_parsed(language_id, text.encode())
    rel_path = RelPath(rel)
    facts = extract_imports(parsed, language_id=language_id)
    resolver = make_resolver(
        from_rel=rel_path, language_id=language_id, exists=lambda p: str(p) in JS_FILES[language_id]
    )
    module_facts = [f for f in facts if not f.nested]
    nested: dict[ImportKey, list[ImportFact]] = {}
    for f in facts:
        if f.nested:
            nested.setdefault((f.range.start.line, f.range.start.col), []).append(f)
    names = imported_name_map(module_facts, resolver)
    external = external_name_map(module_facts, language_id=language_id, internal=names)

    def urn_of(table: dict[str, tuple[RelPath, str]], ext: dict[str, str], name: str) -> str | None:
        hit = table.get(name)
        if hit is None:
            return ext.get(name)
        path, original = hit
        return f"lk://{WS}/{path}" if original == "*" else f"lk://{WS}/{path}#{original}"

    def resolve_import(key: ImportKey, name: str) -> str | None:
        group = nested.get(key, [])
        table = imported_name_map(group, resolver)
        return urn_of(table, external_name_map(group, language_id=language_id, internal=table), name)

    symbols = extract(
        parsed,
        language_id=language_id,
        language=JS_LANGUAGES[language_id],
        query_source=load_query_source(language_id),
        workspace_id=WS,
        rel=rel_path,
    )
    return extract_relations(
        parsed,
        language_id=language_id,
        language=JS_LANGUAGES[language_id],
        symbols=symbols,
        workspace_id=WS,
        rel=rel_path,
        resolve_name=lambda n: urn_of(names, external, n),
        builtins_package=GLOBALS_PACKAGE,
        resolve_import=resolve_import if scoped else None,
    )


def test_js_use_after_a_function_local_require_is_a_call():
    """BUG-0052 の形: 関数の中の ``const { foo } = require(...)`` の ``foo(1)`` が、モジュール直下の ``bar(2)`` と同じく返る。"""
    rels = _js_relations(
        "function f() {\n"
        '  const { foo } = require("./foo");\n'
        "  return foo(1);\n"
        "}\n"
        'const { bar } = require("./foo");\n'
        "bar(2);\n"
    )
    assert _uses(rels, "foo") == [(2, "CALLS", f"{FOO}#foo")]
    assert _uses(rels, "bar") == [(5, "CALLS", f"{FOO}#bar")]
    # 宣言側の名前（束縛位置）は参照に数えない
    assert [r.call_site_line for r in rels if r.target_name == "foo"] == [2]
    assert {r.source_urn for r in rels if r.target_name == "foo"} == {f"lk://{WS}/app/m.js#f"}


def test_js_function_local_require_forms():
    """分割代入・別名・既定値つき・名前空間・``.member`` つき、2 つの宣言子（別のモジュール）を 1 文に。"""
    rels = _js_relations(
        "function f() {\n"
        '  const { foo: renamed, bar = 1 } = require("./foo");\n'
        '  const ns = require("./foo");\n'
        '  const pick = require("./bar").pick, other = require("./foo").other;\n'
        "  return [renamed(), bar, ns.baz(), pick(), other()];\n"
        "}\n"
    )
    assert _uses(rels, "renamed") == [(4, "CALLS", f"{FOO}#foo")]
    assert _uses(rels, "bar") == [(4, "REFERENCES", f"{FOO}#bar")]
    assert _uses(rels, "baz") == [(4, "CALLS", f"{FOO}#baz")]  # ``ns`` はモジュール（ファイル）
    # 3 行目の ``.pick`` / ``.other`` は ``require(...)`` の属性名（束縛でなく、使用でもない）。使用は 4 行目
    assert [u for u in _uses(rels, "pick") if u[0] == 4] == [(4, "CALLS", f"{BAR}#pick")]
    assert [u for u in _uses(rels, "other") if u[0] == 4] == [(4, "CALLS", f"{FOO}#other")]


def test_js_function_local_require_matches_the_module_level_result():
    """同じ形をモジュール直下と関数の中で書いて、使用の解決先が同じになる（種別・URN）。"""
    body = (
        'const { foo, bar: b } = require("./foo");\n'
        'const ns = require("./bar");\n'
        "foo(1);\n"
        "b(2);\n"
        "ns.x();\n"
        "new ns.K();\n"
        "foo.prop;\n"
    )
    names = {"foo", "b", "x", "K", "prop"}

    def shape(rels: list[RelationCandidate], shift: int) -> list[tuple[int, str, str, str | None]]:
        return sorted(
            (r.call_site_line - shift, r.target_name, r.relation_kind, r.target_urn)
            for r in rels
            if r.target_name in names
        )

    top = shape(_js_relations(body), 0)
    inner = shape(_js_relations("function f() {\n" + body + "}\n"), 1)
    assert inner == top
    assert len(top) == 6 and all(urn is not None for *_, urn in top)  # ``foo.prop`` は foo と prop の 2 件


def test_js_same_name_in_another_scope_is_not_mixed_up():
    rels = _js_relations(
        'const { foo } = require("./foo");\n'
        "function f() {\n"
        '  const { foo } = require("./bar");\n'
        "  return foo();\n"
        "}\n"
        "function g() {\n"
        "  return foo();\n"
        "}\n"
        "function h() {\n"
        '  const { foo } = require("./foo");\n'
        "  return foo();\n"
        "}\n"
        "foo();\n"
    )
    assert _uses(rels, "foo") == [
        (3, "CALLS", f"{BAR}#foo"),
        (6, "CALLS", f"{FOO}#foo"),  # g: モジュール直下の require
        (10, "CALLS", f"{FOO}#foo"),
        (12, "CALLS", f"{FOO}#foo"),  # f の require に引きずられない
    ]


def test_js_name_bound_only_in_a_function_does_not_leak_to_module_level():
    rels = _js_relations(
        "function f() {\n"
        '  const { foo } = require("./foo");\n'
        "  return foo();\n"
        "}\n"
        "foo();\n"
        "function g() { return foo(); }\n"
    )
    assert _uses(rels, "foo") == [(2, "CALLS", f"{FOO}#foo"), (4, "CALLS", None), (5, "CALLS", None)]


def test_js_inner_functions_see_the_enclosing_require_and_parameters_hide_it():
    rels = _js_relations(
        "function outer() {\n"
        '  const { foo } = require("./foo");\n'
        "  const arrow = () => foo(1);\n"
        "  const inner = function (foo) { return foo(2); };\n"
        "  class K { m() { return foo(3); } n(foo) { return foo(4); } }\n"
        "  return [arrow, inner, K];\n"
        "}\n"
    )
    # 2 行目 (arrow) と 4 行目 (K.m) は外側の require。3 行目の inner と K.n はパラメータ（参照に数えない）
    assert _uses(rels, "foo") == [(2, "CALLS", f"{FOO}#foo"), (4, "CALLS", f"{FOO}#foo")]


def test_js_method_arrow_and_generator_bodies_are_function_scopes():
    rels = _js_relations(
        "class C {\n"
        "  m() {\n"
        '    const { foo } = require("./foo");\n'
        "    return foo();\n"
        "  }\n"
        "}\n"
        "const a = () => {\n"
        '  const { bar } = require("./bar");\n'
        "  return bar();\n"
        "};\n"
        "function* gen() {\n"
        '  const { foo: g } = require("./foo");\n'
        "  yield g();\n"
        "}\n"
    )
    assert _uses(rels, "foo") == [(3, "CALLS", f"{FOO}#foo")]
    assert _uses(rels, "bar") == [(8, "CALLS", f"{BAR}#bar")]
    assert _uses(rels, "g") == [(12, "CALLS", f"{FOO}#foo")]


def test_js_non_require_local_still_hides_a_module_level_import():
    """require でない束縛（分割代入・配列・入れ子）は従来どおりただのローカル変数（参照に数えない）。"""
    rels = _js_relations(
        'const { foo, bar, baz } = require("./foo");\n'
        "function f(obj) {\n"
        "  const { foo } = obj;\n"
        '  const [bar] = require("./bar");\n'
        '  const { x: { baz } } = require("./bar");\n'
        "  return [foo(), bar(), baz()];\n"
        "}\n"
    )
    assert _uses(rels, "foo") == []
    assert _uses(rels, "bar") == []
    assert _uses(rels, "baz") == []


def test_js_require_that_is_not_a_binding_stays_out():
    """``require`` を代入以外で使う文（副作用だけ・引数・計算プロパティ）は名前を束縛しない（x・y はただのローカル変数）。"""
    rels = _js_relations(
        "function f() {\n"
        '  require("./foo");\n'
        '  const x = wrap(require("./foo"));\n'
        '  const y = require("./foo")["k"];\n'
        "  return [x(), y()];\n"
        "}\n"
    )
    assert _uses(rels, "x") == [] and _uses(rels, "y") == []


def test_js_external_package_required_in_a_function():
    rels = _js_relations('function f() {\n  const _ = require("lodash");\n  return _.map([], g);\n}\n')
    assert _uses(rels, "map") == [(2, "CALLS", "lk://_ext/lodash#map")]


def test_js_without_resolve_import_the_use_is_still_a_candidate():
    rels = _js_relations(
        'function f() {\n  const { foo } = require("./foo");\n  return foo(1);\n}\n', scoped=False
    )
    assert [(line, kind) for line, kind, _ in _uses(rels, "foo")] == [(2, "CALLS")]


def test_ts_use_after_a_function_local_require():
    rels = _js_relations(
        "function f(): number {\n"
        '  const { foo } = require("./foo");\n'
        '  const lib = require("./bar");\n'
        '  const typed: Typed = require("./foo");\n'
        "  return foo(1) + lib.size() + typed();\n"
        "}\n"
        'const { foo: top } = require("./foo");\n'
        "top();\n",
        language_id="typescript",
        rel="app/m.ts",
    )
    assert _uses(rels, "foo") == [(4, "CALLS", f"{TS_FOO}#foo")]
    assert _uses(rels, "top") == [(7, "CALLS", f"{TS_FOO}#foo")]
    assert _uses(rels, "size") == [(4, "CALLS", f"lk://{WS}/app/bar.ts#size")]
    assert _uses(rels, "typed") == [(4, "CALLS", TS_FOO)]  # 型注釈つきの束縛も同じ


def test_extract_imports_marks_only_function_local_requires_as_nested():
    src = (
        'const a = require("./foo");\n'
        "function f() {\n"
        '  const b = require("./foo");\n'
        "}\n"
        'class K { static { const c = require("./foo"); } }\n'
        'const g = () => require("./foo");\n'
        'import d from "./foo";\n'
    )
    facts = extract_imports(_js_parsed("javascript", src.encode()), language_id="javascript")
    assert {f.range.start.line: f.nested for f in facts} == {0: False, 2: True, 4: False, 5: True, 6: False}
