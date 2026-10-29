"""S-2 共通: 言語読み込み表・サンプル・タグ風クエリ（run_grammars.py などが import する）。

使い方: 直接実行しない。`uv run python spikes/s02_treesitter/run_grammars.py` から読み込まれる。
置き場所: spikes/s02_treesitter/（同じフォルダのスクリプトは sys.path[0] 経由で `import grammars` できる）。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from importlib import metadata

import tree_sitter as ts


@dataclass(frozen=True)
class Grammar:
    lang_id: str  # Lucianke 内の言語 ID（案）
    dist: str  # PyPI 配布名
    module: str  # import 名
    func: str  # 言語取得関数の実名
    sample: str
    own_tags: str  # このスパイクで書いた「タグ風」クエリ（@name と @definition.* / @reference.*）
    bundled: bool = False  # パッケージに TAGS_QUERY が同梱されているはずか


PY = """import os

class A:
    def m(self):
        return 1

def f(x):
    return x
"""

TS = """interface I { a: number }

class C {
  m(): void {}
}

function f(x: number): number { return x }
"""

TSX = TS + "\nconst e = <div>{f(1)}</div>;\n"

JS = """class A {
  m() { return 1 }
}

function f(x) { return x }
const g = (y) => y
"""

SAMPLES = {
    "rust": "struct S { a: i32 }\n\nimpl S {\n    fn m(&self) -> i32 { self.a }\n}\n\nfn f(x: i32) -> i32 { x }\n",
    "go": "package main\n\ntype S struct{ a int }\n\nfunc (s S) M() int { return s.a }\n\nfunc f(x int) int { return x }\n",
    "c": "struct S { int a; };\n\nint f(int x) { return x; }\n",
    "cpp": "class A {\npublic:\n  int m() { return 1; }\n};\n\nint f(int x) { return x; }\n",
    "c_sharp": "namespace N {\n  class A {\n    void M() { }\n  }\n}\n",
}

G: list[Grammar] = [
    Grammar("python", "tree-sitter-python", "tree_sitter_python", "language", PY,
            "(function_definition name: (identifier) @name) @definition.function\n"
            "(class_definition name: (identifier) @name) @definition.class", True),
    Grammar("typescript", "tree-sitter-typescript", "tree_sitter_typescript", "language_typescript", TS,
            "(function_declaration name: (identifier) @name) @definition.function\n"
            "(class_declaration name: (type_identifier) @name) @definition.class\n"
            "(method_definition name: (property_identifier) @name) @definition.method\n"
            "(interface_declaration name: (type_identifier) @name) @definition.interface", True),
    Grammar("tsx", "tree-sitter-typescript", "tree_sitter_typescript", "language_tsx", TSX,
            "(function_declaration name: (identifier) @name) @definition.function\n"
            "(class_declaration name: (type_identifier) @name) @definition.class\n"
            "(method_definition name: (property_identifier) @name) @definition.method\n"
            "(interface_declaration name: (type_identifier) @name) @definition.interface", True),
    Grammar("javascript", "tree-sitter-javascript", "tree_sitter_javascript", "language", JS,
            "(function_declaration name: (identifier) @name) @definition.function\n"
            "(class_declaration name: (identifier) @name) @definition.class\n"
            "(method_definition name: (property_identifier) @name) @definition.method\n"
            "(variable_declarator name: (identifier) @name value: (arrow_function)) @definition.function", True),
    Grammar("json", "tree-sitter-json", "tree_sitter_json", "language",
            '{"a": [1, 2], "b": {"c": null}}\n',
            "(pair key: (string (string_content) @name)) @definition.pair"),
    Grammar("yaml", "tree-sitter-yaml", "tree_sitter_yaml", "language",
            "a: 1\nb:\n  - x\n  - y\nc: {d: 2}\n",
            "(block_mapping_pair key: (flow_node) @name) @definition.pair"),
    Grammar("toml", "tree-sitter-toml", "tree_sitter_toml", "language",
            '[pkg]\nname = "x"\n[[bin]]\na = 1\n',
            "(table (bare_key) @name) @definition.table\n"
            "(table_array_element (bare_key) @name) @definition.table\n"
            "(pair (bare_key) @name) @definition.pair"),
    Grammar("xml", "tree-sitter-xml", "tree_sitter_xml", "language_xml",
            '<?xml version="1.0"?>\n<root a="1"><item id="x">t</item></root>\n',
            "(element (STag (Name) @name)) @definition.element"),
    Grammar("dtd", "tree-sitter-xml", "tree_sitter_xml", "language_dtd",
            "<!ELEMENT note (to,from)>\n<!ATTLIST note id CDATA #REQUIRED>\n",
            "(elementdecl (Name) @name) @definition.element\n(AttlistDecl (Name) @name) @definition.attlist"),
    Grammar("markdown", "tree-sitter-markdown", "tree_sitter_markdown", "language",
            "# Head\n\ntext *em*\n\n## Sub\n\n- a\n- b\n",
            "(atx_heading heading_content: (inline) @name) @definition.heading"),
    Grammar("markdown_inline", "tree-sitter-markdown", "tree_sitter_markdown", "inline_language",
            "text *em* `code` [l](u)",
            "(inline_link (link_text) @name) @reference.link"),
    Grammar("rust", "tree-sitter-rust", "tree_sitter_rust", "language", SAMPLES["rust"],
            "(function_item name: (identifier) @name) @definition.function\n"
            "(struct_item name: (type_identifier) @name) @definition.class\n"
            "(impl_item type: (type_identifier) @name) @definition.impl", True),
    Grammar("go", "tree-sitter-go", "tree_sitter_go", "language", SAMPLES["go"],
            "(function_declaration name: (identifier) @name) @definition.function\n"
            "(method_declaration name: (field_identifier) @name) @definition.method\n"
            "(type_spec name: (type_identifier) @name) @definition.type", True),
    Grammar("c", "tree-sitter-c", "tree_sitter_c", "language", SAMPLES["c"],
            "(function_definition declarator: (function_declarator declarator: (identifier) @name)) @definition.function\n"
            "(struct_specifier name: (type_identifier) @name body: (_)) @definition.class", True),
    Grammar("cpp", "tree-sitter-cpp", "tree_sitter_cpp", "language", SAMPLES["cpp"],
            "(function_definition declarator: (function_declarator declarator: [(identifier) (field_identifier)] @name)) @definition.function\n"
            "(class_specifier name: (type_identifier) @name) @definition.class", True),
    Grammar("c_sharp", "tree-sitter-c-sharp", "tree_sitter_c_sharp", "language", SAMPLES["c_sharp"],
            "(class_declaration name: (identifier) @name) @definition.class\n"
            "(method_declaration name: (identifier) @name) @definition.method\n"
            "(namespace_declaration name: (identifier) @name) @definition.module", True),
    Grammar("rst", "tree-sitter-rst", "tree_sitter_rst", "language",
            "Title\n=====\n\nSome *text*.\n\n.. note:: hello\n\nSub\n---\n\nbody\n",
            "(section (title) @name) @definition.section"),
    Grammar("sql", "tree-sitter-sql", "tree_sitter_sql", "language",
            "CREATE TABLE t (id int);\nCREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql;\nSELECT * FROM t;\n",
            "(create_table (object_reference name: (identifier) @name)) @definition.table\n"
            "(create_function (object_reference name: (identifier) @name)) @definition.function"),
]

BY_ID = {g.lang_id: g for g in G}
REQUIRED_7 = ("python", "typescript", "javascript", "json", "yaml", "toml", "xml")


def dist_version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "?"


def load_language(g: Grammar) -> ts.Language:
    mod = importlib.import_module(g.module)
    return ts.Language(getattr(mod, g.func)())


def run_tag_query(lang: ts.Language, query_src: str, source: bytes, parser: ts.Parser | None = None):
    """タグ風クエリを matches() で実行し [(pattern_index, kind, name_text, (srow, scol), (erow, ecol))] を返す。

    kind は `@definition.*` / `@reference.*` の捕捉名。`@name` が複数ある場合は先頭のみ。
    """
    parser = parser or ts.Parser(lang)
    tree = parser.parse(source)
    q = ts.Query(lang, query_src)
    out = []
    for pat, caps in ts.QueryCursor(q).matches(tree.root_node):
        names = caps.get("name", [])
        for cap_name, nodes in caps.items():
            if cap_name.startswith(("definition.", "reference.")):
                for n in nodes:
                    name = names[0].text.decode("utf-8", "replace") if names else None
                    out.append((pat, cap_name, name, tuple(n.start_point), tuple(n.end_point)))
    return out
