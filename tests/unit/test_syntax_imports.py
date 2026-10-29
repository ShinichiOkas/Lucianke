"""``syntax/imports.py``（import 文の解析とモジュールパス → ファイルの対応。設計書 §7.4.6）の単体テスト。

フィクスチャ F-POLY（``tests/fixtures/poly``）を ``tree_sitter.Parser`` で直接解析して使う。ファイルの存在は集合で与える。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

from lucianke.core.model import Position, RelPath
from lucianke.core.textpos import LineIndex
from lucianke.syntax.imports import (
    ImportFact,
    extract_imports,
    imported_name_map,
    make_resolver,
    resolve_module,
    wildcard_sources,
)
from lucianke.syntax.types import ParsedFile

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"

_LANGUAGES: dict[str, Callable[[], object]] = {
    "python": tree_sitter_python.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "javascript": tree_sitter_javascript.language,
}


def _parsed(language_id: str, source: bytes | str) -> ParsedFile:
    data = source.encode() if isinstance(source, str) else source
    tree = Parser(Language(_LANGUAGES[language_id]())).parse(data)  # type: ignore[arg-type]
    return ParsedFile(
        language_id=language_id,
        sha256="0" * 64,
        source=data,
        tree=tree,
        line_index=LineIndex(data),
        has_errors=tree.root_node.has_error,
    )


def _facts(language_id: str, source: str) -> list[ImportFact]:
    return extract_imports(_parsed(language_id, source), language_id=language_id)


def _exists(*files: str) -> Callable[[RelPath], bool]:
    present = set(files)
    return lambda rel: str(rel) in present


def _rel(path: str) -> RelPath:
    return RelPath(path)


# --- Python: 抽出 ------------------------------------------------------------------------


def test_python_fixture_routes_from_import() -> None:
    parsed = _parsed("python", (POLY / "src/shop/api/routes.py").read_bytes())
    facts = extract_imports(parsed, language_id="python")
    assert [(f.module, f.kind, f.names) for f in facts] == [
        ("__future__", "from", [("annotations", None)]),
        ("decimal", "from", [("Decimal", None)]),
        ("shop.services.limits", "from", [("LimitTable", None)]),
        ("shop.services.payment", "from", [("PaymentProcessor", None), ("PaymentRequest", None)]),
    ]
    last = facts[-1]
    assert (last.is_relative, last.level) == (False, 0)
    assert last.range.start == Position(7, 0)  # 0 始まりの行（8 行目）
    assert last.range.end == Position(
        7, len("from shop.services.payment import PaymentProcessor, PaymentRequest")
    )


def test_python_import_forms() -> None:
    facts = _facts(
        "python",
        "import os, a.b as c\nimport x.y\nfrom .x import y as z, w\nfrom . import q\nfrom ..p import (r,\n    s)\n"
        "from m import *\nfrom __future__ import annotations\n",
    )
    summary = [(f.module, f.kind, f.names, f.is_relative, f.level) for f in facts]
    assert summary == [
        ("os", "import", [], False, 0),
        ("a.b", "import", [("*", "c")], False, 0),
        ("x.y", "import", [], False, 0),
        ("x", "from", [("y", "z"), ("w", None)], True, 1),
        ("", "from", [("q", None)], True, 1),
        ("p", "from", [("r", None), ("s", None)], True, 2),
        ("m", "from", [("*", None)], False, 0),
        ("__future__", "from", [("annotations", None)], False, 0),
    ]
    # 複数行の括弧つき import は文全体の範囲
    multi = facts[5]
    assert (multi.range.start.line, multi.range.end.line) == (4, 5)
    # ``import os, a.b as c`` は 2 件で同じ範囲
    assert facts[0].range == facts[1].range


def test_python_nested_and_conditional_imports_are_found() -> None:
    facts = _facts(
        "python",
        "try:\n    import ujson as json\nexcept ImportError:\n    import json\n\n"
        "def f():\n    from a import b\n",
    )
    assert [f.module for f in facts] == ["ujson", "json", "a"]


def test_python_broken_source_returns_what_it_can() -> None:
    facts = _facts("python", "from a import b\ndef (:\nimport c\n")
    assert {f.module for f in facts} >= {"a"}


def test_unsupported_language_returns_empty() -> None:
    assert extract_imports(_parsed("python", "import os\n"), language_id="rust") == []


# --- Python: 解決 ------------------------------------------------------------------------

ROUTES = _rel("src/shop/api/routes.py")
SHOP_FILES = (
    "src/shop/__init__.py",
    "src/shop/api/__init__.py",
    "src/shop/api/routes.py",
    "src/shop/services/__init__.py",
    "src/shop/services/payment.py",
    "src/shop/services/limits.py",
    "src/shop/models/receipt.py",
)


def _py(module: str, **kw: object) -> RelPath | None:
    files = kw.pop("files", SHOP_FILES)
    return resolve_module(
        module,
        from_rel=kw.pop("from_rel", ROUTES),  # type: ignore[arg-type]
        language_id="python",
        is_relative=bool(kw.pop("is_relative", False)),
        level=int(kw.pop("level", 0)),  # type: ignore[call-overload]
        source_roots=kw.pop("source_roots", [_rel(""), _rel("src")]),  # type: ignore[arg-type]
        exists=_exists(*files),  # type: ignore[arg-type]
    )


def test_python_absolute_resolves_under_src_root() -> None:
    assert _py("shop.services.payment") == "src/shop/services/payment.py"
    assert _py("shop.services") == "src/shop/services/__init__.py"
    assert _py("shop") == "src/shop/__init__.py"


def test_python_absolute_prefers_earlier_source_root_and_module_over_package() -> None:
    files = ("a.py", "a/__init__.py", "src/a.py")
    assert _py("a", files=files) == "a.py"
    assert _py("a", files=files, source_roots=[_rel("src"), _rel("")]) == "src/a.py"
    assert _py("a", files=("a/__init__.py", "a.pyi")) == "a/__init__.py"  # .py → /__init__.py → .pyi
    assert _py("a", files=("a.pyi",)) == "a.pyi"


def test_python_default_source_roots_are_root_and_src() -> None:
    rel = resolve_module(
        "shop.services.limits",
        from_rel=ROUTES,
        language_id="python",
        source_roots=None,
        exists=_exists(*SHOP_FILES),
    )
    assert rel == "src/shop/services/limits.py"


def test_python_missing_and_external_modules_are_none() -> None:
    assert _py("shop.services.nope") is None
    assert _py("decimal") is None
    assert _py("") is None
    assert _py("a..b") is None
    assert _py("../etc/passwd") is None


def test_python_relative_import_goes_up_by_level() -> None:
    assert _py("payment", from_rel=_rel("src/shop/services/limits.py"), is_relative=True, level=1) == (
        "src/shop/services/payment.py"
    )
    assert (
        _py("services.payment", from_rel=ROUTES, is_relative=True, level=2) == "src/shop/services/payment.py"
    )
    assert _py("", from_rel=_rel("src/shop/services/limits.py"), is_relative=True, level=1) == (
        "src/shop/services/__init__.py"
    )
    assert _py("", from_rel=ROUTES, is_relative=True, level=2) == "src/shop/__init__.py"
    # __init__.py からの `from . import x` は自分のパッケージ
    assert _py("payment", from_rel=_rel("src/shop/services/__init__.py"), is_relative=True, level=1) == (
        "src/shop/services/payment.py"
    )


def test_python_relative_import_above_root_is_none() -> None:
    assert _py("x", from_rel=_rel("a.py"), is_relative=True, level=2, files=("x.py",)) is None
    assert _py("x", from_rel=_rel("a/b.py"), is_relative=True, level=3, files=("x.py",)) is None
    assert _py("x", from_rel=_rel("a.py"), is_relative=True, level=1, files=("x.py",)) == "x.py"


# --- Python: 名前の表 --------------------------------------------------------------------


def test_python_name_map_for_routes_fixture() -> None:
    parsed = _parsed("python", (POLY / "src/shop/api/routes.py").read_bytes())
    facts = extract_imports(parsed, language_id="python")
    resolve = make_resolver(
        from_rel=ROUTES, language_id="python", exists=_exists(*SHOP_FILES), source_roots=[_rel("src")]
    )
    names = imported_name_map(facts, resolve)
    assert names == {
        "PaymentProcessor": ("src/shop/services/payment.py", "PaymentProcessor"),
        "PaymentRequest": ("src/shop/services/payment.py", "PaymentRequest"),
        "LimitTable": ("src/shop/services/limits.py", "LimitTable"),
    }  # decimal は外部なので入らない


def test_python_name_map_aliases_modules_and_wildcards() -> None:
    files = (
        "src/shop/api/routes.py",
        "src/shop/services/payment.py",
        "src/shop/services/__init__.py",
        "src/shop/models/receipt.py",
        "src/shop/models/__init__.py",
        "src/shop/api/helpers.py",
        "src/shop/api/__init__.py",
        "src/shop/__init__.py",
    )
    facts = _facts(
        "python",
        "import shop.models.receipt as rc\n"
        "import shop.services.payment\n"
        "from shop.services import payment as pay\n"
        "from . import helpers\n"
        "from .helpers import h as hh\n"
        "from shop.models import *\n"
        "import os\n",
    )
    resolve = make_resolver(
        from_rel=ROUTES, language_id="python", exists=_exists(*files), source_roots=[_rel("src")]
    )
    names = imported_name_map(facts, resolve)
    assert names == {
        "rc": ("src/shop/models/receipt.py", "*"),
        "shop": ("src/shop/__init__.py", "*"),  # `import shop.services.payment` は先頭の shop を束ねる
        "pay": ("src/shop/services/payment.py", "*"),  # サブモジュール優先
        "helpers": ("src/shop/api/helpers.py", "*"),
        "hh": ("src/shop/api/helpers.py", "h"),
    }
    assert wildcard_sources(facts, resolve) == ["src/shop/models/__init__.py"]


def test_python_name_map_later_binding_wins_and_unresolved_dropped() -> None:
    facts = _facts("python", "from a import x\nfrom b import x\nfrom nope import y\n")
    resolve = make_resolver(from_rel=_rel("m.py"), language_id="python", exists=_exists("a.py", "b.py"))
    assert imported_name_map(facts, resolve) == {"x": ("b.py", "x")}


# --- TypeScript / JavaScript: 抽出 --------------------------------------------------------


def test_ts_fixture_app_import() -> None:
    parsed = _parsed("typescript", (POLY / "web/src/app.ts").read_bytes())
    facts = extract_imports(parsed, language_id="typescript")
    assert len(facts) == 1
    fact = facts[0]
    assert (fact.module, fact.kind, fact.names, fact.is_relative, fact.level) == (
        "./payment",
        "es_import",
        [("PaymentClient", None)],
        True,
        0,
    )
    assert fact.range.start == Position(0, 0)
    assert fact.range.end == Position(0, len('import { PaymentClient } from "./payment";'))


def test_ts_import_forms() -> None:
    facts = _facts(
        "typescript",
        'import React, {useState as us, type T} from "react";\n'
        'import * as ns from "z";\n'
        'import "./side";\n'
        'import x = require("y");\n'
        'import type { Q } from "./q";\n'
        "import def from '../d';\n",
    )
    summary = [(f.module, f.kind, f.names, f.is_relative, f.reexport) for f in facts]
    assert summary == [
        ("react", "es_import", [("default", "React"), ("useState", "us"), ("T", None)], False, False),
        ("z", "es_import", [("*", "ns")], False, False),
        ("./side", "es_import", [], True, False),
        ("y", "require", [("*", "x")], False, False),
        ("./q", "es_import", [("Q", None)], True, False),
        ("../d", "es_import", [("default", "def")], True, False),
    ]


def test_ts_export_from_is_a_reexport() -> None:
    facts = _facts(
        "typescript",
        'export { a as b, c } from "./x";\nexport * from "./y";\nexport * as q from "./z";\nexport { local };\n',
    )
    assert [(f.module, f.kind, f.names, f.reexport) for f in facts] == [
        ("./x", "es_import", [("a", "b"), ("c", None)], True),
        ("./y", "es_import", [("*", None)], True),
        ("./z", "es_import", [("*", "q")], True),
    ]


def test_js_require_forms() -> None:
    facts = _facts(
        "javascript",
        'const x = require("y");\n'
        'const { a, b: c } = require("./m");\n'
        'const f = require("./n").foo;\n'
        'require("./side");\n'
        'foo(require("./arg"));\n'
        "require(dynamic);\n"
        'var v = require("v"), w = 1;\n',
    )
    summary = [(f.module, f.kind, f.names, f.is_relative) for f in facts]
    assert summary == [
        ("y", "require", [("*", "x")], False),
        ("./m", "require", [("a", None), ("b", "c")], True),
        ("./n", "require", [("foo", "f")], True),
        ("./side", "require", [], True),
        ("./arg", "require", [], True),
        ("v", "require", [("*", "v")], False),
    ]
    assert facts[0].range.start == Position(0, 0)  # 宣言文全体
    assert facts[0].range.end == Position(0, len('const x = require("y");'))
    assert facts[3].range.end == Position(3, len('require("./side");'))


def test_tsx_and_javascript_language_ids() -> None:
    assert _facts("tsx", 'import A from "./a";\nconst b = <A />;\n')[0].module == "./a"
    assert _facts("javascript", 'import A from "./a";\n')[0].names == [("default", "A")]


# --- TypeScript / JavaScript: 解決 ---------------------------------------------------------

APP = _rel("web/src/app.ts")


def _js(spec: str, *files: str, from_rel: RelPath = APP, language_id: str = "typescript") -> RelPath | None:
    return resolve_module(
        spec,
        from_rel=from_rel,
        language_id=language_id,
        is_relative=spec.startswith("."),
        level=0,
        source_roots=[],
        exists=_exists(*files),
    )


def test_ts_fixture_app_resolves_to_payment_ts() -> None:
    existing = [
        str(p.relative_to(POLY)).replace("\\", "/")
        for p in POLY.rglob("*")
        if p.is_file() and "node_modules" not in p.parts
    ]
    assert _js("./payment", *existing) == "web/src/payment.ts"
    parsed = _parsed("typescript", (POLY / "web/src/app.ts").read_bytes())
    resolve = make_resolver(from_rel=APP, language_id="typescript", exists=_exists(*existing))
    assert imported_name_map(extract_imports(parsed, language_id="typescript"), resolve) == {
        "PaymentClient": ("web/src/payment.ts", "PaymentClient"),
    }


def test_ts_extension_order_and_index_completion() -> None:
    assert _js("./p", "web/src/p.js", "web/src/p.tsx", "web/src/p.ts") == "web/src/p.ts"
    assert _js("./p", "web/src/p.js", "web/src/p.tsx") == "web/src/p.tsx"
    assert _js("./p", "web/src/p.mjs") == "web/src/p.mjs"
    assert _js("./p", "web/src/p.jsx", "web/src/p.mjs") == "web/src/p.jsx"
    assert _js("./lib", "web/src/lib/index.ts") == "web/src/lib/index.ts"
    assert _js("./lib", "web/src/lib.ts", "web/src/lib/index.ts") == "web/src/lib.ts"
    assert _js("./lib/", "web/src/lib/index.js") == "web/src/lib/index.js"
    assert _js("../shared", "web/shared/index.tsx") == "web/shared/index.tsx"
    assert _js("../shared/x", "web/shared/x.ts") == "web/shared/x.ts"
    assert _js(".", "web/src/index.ts") == "web/src/index.ts"
    assert _js("..", "web/index.ts") == "web/index.ts"


def test_ts_specifier_with_extension() -> None:
    assert _js("./x.js", "web/src/x.ts") == "web/src/x.ts"  # ESM 流儀: .js の指定子が実体は .ts
    assert _js("./x.js", "web/src/x.js") == "web/src/x.js"
    assert _js("./x.ts", "web/src/x.ts") == "web/src/x.ts"
    assert _js("./data.json", "web/src/data.json") == "web/src/data.json"
    assert _js("./app.service", "web/src/app.service.ts") == "web/src/app.service.ts"


def test_ts_bare_absolute_and_outside_workspace_are_none() -> None:
    files = ("web/src/react.ts", "node_modules/react/index.js", "react/index.ts", "x.ts")
    assert _js("react", *files) is None  # node_modules / paths は M1 の範囲外
    assert _js("@app/x", *files) is None
    assert _js("/abs/x", *files) is None
    assert _js("../../../x", *files, from_rel=_rel("a/b.ts")) is None
    assert _js("./missing", *files) is None


def test_js_resolution_at_workspace_root() -> None:
    assert _js("./x", "x.ts", from_rel=_rel("app.ts")) == "x.ts"
    assert _js("./", "index.js", from_rel=_rel("app.js"), language_id="javascript") == "index.js"


def test_unsupported_language_resolve_is_none() -> None:
    assert (
        resolve_module(
            "x",
            from_rel=_rel("a.rs"),
            language_id="rust",
            is_relative=False,
            level=0,
            source_roots=[],
            exists=lambda _r: True,
        )
        is None
    )


# --- TS / JS: 名前の表 -------------------------------------------------------------------


def test_js_name_map_default_namespace_require_and_reexport() -> None:
    files = ("web/src/a.ts", "web/src/b.ts", "web/src/c.js", "web/src/d/index.ts", "web/src/e.ts")
    source = (
        'import A, { b as bee, c } from "./a";\n'
        'import * as ns from "./b";\n'
        'const k = require("./c");\n'
        'const { m, n: nn } = require("./c");\n'
        'import ext from "react";\n'
        'export { z as zed } from "./d";\n'
        'export * from "./e";\n'
    )
    facts = _facts("typescript", source)
    resolve = make_resolver(from_rel=APP, language_id="typescript", exists=_exists(*files))
    assert imported_name_map(facts, resolve) == {
        "A": ("web/src/a.ts", "default"),
        "bee": ("web/src/a.ts", "b"),
        "c": ("web/src/a.ts", "c"),
        "ns": ("web/src/b.ts", "*"),
        "k": ("web/src/c.js", "*"),
        "m": ("web/src/c.js", "m"),
        "nn": ("web/src/c.js", "n"),
    }
    with_reexports = imported_name_map(facts, resolve, include_reexports=True)
    assert with_reexports["zed"] == ("web/src/d/index.ts", "z")
    assert wildcard_sources(facts, resolve) == ["web/src/e.ts"]


def test_facts_are_in_document_order() -> None:
    facts = _facts("javascript", 'const a = require("./a");\nimport b from "./b";\nexport * from "./c";\n')
    assert [f.module for f in facts] == ["./a", "./b", "./c"]
    assert [f.range.start.line for f in facts] == [0, 1, 2]


@pytest.mark.parametrize("language_id", ["python", "typescript", "tsx", "javascript"])
def test_empty_source_has_no_imports(language_id: str) -> None:
    assert _facts(language_id, "") == []
