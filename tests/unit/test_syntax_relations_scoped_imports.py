"""``def`` / ``class`` の中の import が束縛した名前の使用（BUG-0048）。

関数の中の ``from x import Y`` で束縛した名前は、その関数（と内側の関数）の使用を ``Y`` への参照にする。以前は
「関数のローカル」として使用箇所を落としていた（import の行だけが残る）。モジュール直下の同名の import や、別の関数の
同名の import は巻き込まない。``engine/indexer.py`` と同じ配線（``ImportFact.nested`` で名前表を分け、文の位置で引く）で確かめる。
"""

from __future__ import annotations

import tree_sitter_python
from tree_sitter import Language, Parser

from lucianke.core.model import RelPath
from lucianke.core.textpos import LineIndex
from lucianke.syntax.builtin_names import BUILTINS_PACKAGE
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
