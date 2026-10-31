"""import 文の解析と、モジュールパス → ワークスペース内ファイルの対応（設計書 §7.4.6、仕様書 §8.3 ``structural``）。

LSP なしの定義解決（T-04 処理規約 3）と呼び出し関係の ``structural`` 行（T-06・T-07）が使う、クロスファイル名前解決の土台。

* ``extract_imports``: ``ParsedFile`` から ``ImportFact`` を文書順に集める。Python（``import`` / ``from ... import``、``__future__`` を含む）と
  TypeScript / JavaScript（``import`` / ``export ... from`` / ``import x = require()`` / ``require()``）。
* ``resolve_module``: モジュール名 1 つをワークスペース内のファイルに対応づける。ファイルの存在は呼び手が ``exists`` で教える
  （ファイルシステムを直接読まない）。``node_modules``・tsconfig の ``paths``・Python の site-packages は扱わない（外部は ``None``）。
* ``python_import_roots``: Python の絶対 import を探す根を、そのファイルについて実際に ``sys.path`` に載るものの順に決める
  （pytest が足すテストのディレクトリ・``pythonpath`` の設定・テストフィクスチャのルート・ルートと ``src``。BUG-0082）。
  ``make_resolver`` は ``source_roots`` を渡されなければこれを使う。
* ``imported_name_map``: ファイル内で使える名前 → ``(定義元ファイル, 元の名前)``。解決できた import だけが入る。
* ``external_name_map``: 解決できなかった import の名前 → 外部シンボルの URN（``lk://_ext/<pkg>#<qualified>``。設計書 §7.4.6 項 3、
  仕様書 §9.3）。パッケージは Python が最上位のモジュール名（``from os.path import join`` → ``lk://_ext/os#path.join``）、
  TS / JS が裸の指定子のパッケージ名（``@scope/name`` を含む。サブパスは捨てる）。TS / JS の既定 import・名前空間 import は
  モジュールそのもの（``lk://_ext/react``）。
* ``namespace_name_map``: Python の名前空間パッケージ（``__init__.py`` の無いワークスペースのディレクトリ）を束ねた名前 → その
  モジュールのパス（``import nsp.tools.fmt`` の ``nsp``・``import nsp.tools as nt``・``from nsp import tools``。BUG-0101）。
  ``dotted_module_path`` は、そのディレクトリそのものを ``NAMESPACE_PACKAGE``（参照でも外部でもない）と答える。
* ``module_all_names``: Python のモジュール直下の ``__all__``（文字列のリテラルだけで書かれたもの）。``from x import *`` が
  再び公開する名前を決める（BUG-0100）。``module_all_dynamic`` はリテラルでない ``__all__``（公開する名前が決まらない。BUG-0105）。
* ``js_local_exports``: TS / JS のモジュール直下の ``from`` の無い export（宣言の export・``export { a as b }``・既定の export の
  ローカルの名前）。バレル（``index.ts``）が公開する名前を決める（BUG-0104）。

``ImportFact.names`` の約束（``(name, alias)`` の列。``alias`` は無ければ ``None``）:

* 具体名: ``from x import y as z`` → ``("y", "z")``、``import {a as b}`` → ``("a", "b")``。
* ``"*"``: ``alias`` ありはモジュールオブジェクトそのものを ``alias`` に束ねる（``import a.b as c``・``import * as ns``・
  ``const ns = require("x")``）。``alias`` なしはワイルドカード（``from x import *``・``export * from``）。
* ``"default"``: ES の既定 import（``import x from "y"`` → ``("default", "x")``）。
* ``import a.b``（別名なし）は ``names`` が空で、``a`` が束ねられる（``imported_name_map`` が補う）。

``level`` は Python の相対 import の「先頭のドットの数」（``from ..x import y`` → 2）。TS / JS では常に 0 で、``module`` に
指定子（``"./payment"``）をそのまま持つ。``range`` は文全体（``const x = require(...)`` は宣言文）の 0 始まり・コードポイントの範囲。
"""

from __future__ import annotations

import ast
import functools
import posixpath
import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from tree_sitter import Node, Query, QueryCursor

from lucianke.core.model import Position, Range, RelPath
from lucianke.core.urn import CodeSel, Urn
from lucianke.syntax.support import get_support
from lucianke.syntax.types import ParsedFile
from lucianke.workspace.languages import fixture_roots, is_test
from lucianke.workspace.pyenv import PytestImports

__all__ = [
    "DEFAULT_SOURCE_ROOTS",
    "NAMESPACE_PACKAGE",
    "PYTEST_PRELOADED",
    "ImportFact",
    "ImportKind",
    "ImportResolver",
    "ImportRoot",
    "JsLocalExports",
    "dotted_module_path",
    "external_name_map",
    "extract_imports",
    "imported_name_map",
    "js_local_exports",
    "js_module_exports",
    "js_require_bindings",
    "make_resolver",
    "module_all_dynamic",
    "module_all_names",
    "namespace_name_map",
    "python_import_roots",
    "resolve_module",
    "wildcard_sources",
]

ImportKind = Literal["import", "from", "es_import", "require"]

DEFAULT_SOURCE_ROOTS: tuple[RelPath, ...] = (RelPath(""), RelPath("src"))  # "" = ワークスペースのルート

NAMESPACE_PACKAGE: Final = "lk:namespace-package"
"""``dotted_module_path`` の答えの 1 つ: その点つきのパスはワークスペースの名前空間パッケージのディレクトリ（``__init__.py`` の無い
ディレクトリ。ファイルが無いのでシンボルでもファイルでもない）。呼び手は参照を作らず、外部にもしない（BUG-0101）。URN ではない。"""

_NO_RANGE: Final = Range(Position(0, 0), Position(0, 0))  # 解くためだけに作る ImportFact の範囲（位置は使わない）

_PY_FAMILY = frozenset({"python"})
_JS_FAMILY = frozenset({"typescript", "tsx", "javascript", "jsx"})

# 補う拡張子（設計書 §7.4.6 項目 2）。指定子がすでに拡張子を持つ場合の読み替えは _JS_SWAPS
_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs")
_JS_SWAPS: dict[str, tuple[str, ...]] = {
    ".js": (".ts", ".tsx", ".js"),  # TS の ESM 流儀: ``import "./x.js"`` が実体は x.ts
    ".jsx": (".tsx", ".jsx"),
    ".mjs": (".mts", ".mjs"),
    ".cjs": (".cts", ".cjs"),
    ".ts": (".ts",),
    ".tsx": (".tsx",),
    ".mts": (".mts",),
    ".cts": (".cts",),
    ".json": (".json",),
}


@dataclass(frozen=True, slots=True)
class ImportFact:
    """import 文 1 件（1 モジュール分）。``import a, b`` は ``a`` と ``b`` の 2 件になる。"""

    module: str
    names: list[tuple[str, str | None]]
    is_relative: bool
    level: int
    range: Range
    kind: ImportKind
    reexport: bool = False  # ``export ... from``: このファイルの名前空間には束ねない（再エクスポート）
    # 関数の中の import（束縛はそのスコープの中だけ。ファイルの名前表には入れない）。Python: ``def`` / ``class`` の中の import。
    # TS / JS: 関数（アロー・メソッドを含む）の中の ``require``（``import`` 文はモジュール直下にしか書けない）
    nested: bool = False


# --- 抽出 -------------------------------------------------------------------------------

_PY_QUERY = "[(import_statement) (import_from_statement) (future_import_statement)] @imp"
_JS_QUERY = """
(import_statement) @imp
(export_statement source: (string)) @exp
(call_expression
  function: (identifier) @fn
  arguments: (arguments . (string))
  (#eq? @fn "require")) @call
"""
_QUERIES: dict[str, Query] = {}  # language_id -> Query（遅延コンパイル。競合しても同じものができるだけ）


def _query_for(parsed: ParsedFile, language_id: str, family: str) -> Query:
    q = _QUERIES.get(language_id)
    if q is None:
        q = Query(parsed.tree.language, _PY_QUERY if family == "python" else _JS_QUERY)
        _QUERIES[language_id] = q
    return q


def _text(node: Node | None, src: bytes) -> str:
    # Node.text は使わない（read_callback で解析した木では読み取り関数を呼び直し、解放済みの領域を読みうる。BUG-0020・BUG-0051）。
    return "" if node is None else src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _string_value(node: Node | None, src: bytes) -> str | None:
    """``string`` ノードの中身（引用符を除く）。"""
    if node is None or node.type != "string":
        return None
    text = _text(node, src)
    return text[1:-1] if len(text) >= 2 else ""


def extract_imports(parsed: ParsedFile, *, language_id: str | None = None) -> list[ImportFact]:
    """``parsed`` の import 文を文書順に集める。対応していない言語は空のリスト。

    構文エラーを含むファイルでも、読み取れた文だけを返す（読み取れない文は黙って飛ばす）。
    """
    lang = language_id or parsed.language_id
    if lang in _PY_FAMILY:
        family = "python"
    elif lang in _JS_FAMILY:
        family = "js"
    else:
        return []
    caps = QueryCursor(_query_for(parsed, lang, family)).captures(parsed.tree.root_node)
    nodes: dict[tuple[int, int, str], Node] = {}
    for name in ("imp", "exp", "call"):
        for node in caps.get(name, ()):
            nodes[(node.start_byte, node.end_byte, node.type)] = node

    def rng(node: Node) -> Range:
        li = parsed.line_index
        return Range(li.offset_to_position(node.start_byte).value, li.offset_to_position(node.end_byte).value)

    facts: list[ImportFact] = []
    src = parsed.source
    for _key, node in sorted(nodes.items(), key=lambda kv: kv[0][:2]):
        if family == "python":
            found = _python_facts(node, rng(node), src)
            facts.extend([replace(f, nested=True) for f in found] if _in_scope_body(node) else found)
        elif node.type == "call_expression":
            found = _require_facts(node, rng, src)
            facts.extend([replace(f, nested=True) for f in found] if _in_js_function(node) else found)
        else:
            facts.extend(_es_facts(node, rng(node), src))
    return facts


# --- Python -----------------------------------------------------------------------------

_PY_SCOPE_BODIES = frozenset({"function_definition", "class_definition"})


def _in_scope_body(node: Node) -> bool:
    """``node`` が ``def`` / ``class`` の中にあるか（``if TYPE_CHECKING:`` や ``try:`` のモジュール直下は含まない）。"""
    anc = node.parent
    while anc is not None:
        if anc.type in _PY_SCOPE_BODIES:
            return True
        anc = anc.parent
    return False


def _python_facts(node: Node, rng: Range, src: bytes) -> list[ImportFact]:
    if node.type == "import_statement":
        out: list[ImportFact] = []
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                module = _text(child.child_by_field_name("name"), src)
                alias = _text(child.child_by_field_name("alias"), src)
                if module and alias:
                    out.append(ImportFact(module, [("*", alias)], False, 0, rng, "import"))
            elif child.type == "dotted_name":
                out.append(ImportFact(_text(child, src), [], False, 0, rng, "import"))
        return out
    # import_from_statement / future_import_statement
    if node.type == "future_import_statement":
        module, level = "__future__", 0
    else:
        mod = node.child_by_field_name("module_name")
        if mod is None:
            return []
        if mod.type == "relative_import":
            prefix = next((c for c in mod.children if c.type == "import_prefix"), None)
            level = _text(prefix, src).count(".")
            module = _text(next((c for c in mod.children if c.type == "dotted_name"), None), src)
        else:
            module, level = _text(mod, src), 0
    names: list[tuple[str, str | None]] = []
    for child in node.children_by_field_name("name"):
        if child.type == "aliased_import":
            names.append(
                (
                    _text(child.child_by_field_name("name"), src),
                    _text(child.child_by_field_name("alias"), src) or None,
                )
            )
        elif child.type == "dotted_name":
            names.append((_text(child, src), None))
    if any(c.type == "wildcard_import" for c in node.children):
        names.append(("*", None))
    return [ImportFact(module, names, level > 0, level, rng, "from")]


# --- TypeScript / JavaScript ------------------------------------------------------------

# 関数のノード型。``syntax/relations.py`` の ``_JS_FUNCTIONS`` はこの集合そのもの（関数の中の束縛を import として扱う側と、ここで
# ``nested`` にする側がずれると、使用が落ちるか解決できなくなるので、定義は 1 か所に置く）
JS_FUNCTION_NODES = frozenset(
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


def _in_js_function(node: Node) -> bool:
    """``node`` が関数（アロー・メソッド・ジェネレータを含む）の中にあるか。"""
    anc = node.parent
    while anc is not None:
        if anc.type in JS_FUNCTION_NODES:
            return True
        anc = anc.parent
    return False


def _js_fact(
    module: str,
    names: list[tuple[str, str | None]],
    rng: Range,
    kind: ImportKind,
    *,
    reexport: bool = False,
) -> ImportFact:
    return ImportFact(module, names, module.startswith("."), 0, rng, kind, reexport)


def _specifier(node: Node, src: bytes) -> tuple[str, str | None]:
    """``import_specifier`` / ``export_specifier`` → ``(name, alias)``（名前が文字列リテラルのこともある）。"""
    name_node = node.child_by_field_name("name")
    name = (
        _string_value(name_node, src)
        if name_node is not None and name_node.type == "string"
        else _text(name_node, src)
    )
    alias = node.child_by_field_name("alias")
    return (name or "", _text(alias, src) or None)


def _es_facts(node: Node, rng: Range, src: bytes) -> list[ImportFact]:
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "import_require_clause":  # ``import x = require("y")``（source は節の中）
                ident = next((c for c in child.children if c.type == "identifier"), None)
                mod = _string_value(child.child_by_field_name("source"), src)
                return (
                    [_js_fact(mod, [("*", _text(ident, src))], rng, "require")]
                    if ident is not None and mod is not None
                    else []
                )
    module = _string_value(node.child_by_field_name("source"), src)
    if module is None:
        return []
    names: list[tuple[str, str | None]] = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "import_clause":
                for part in child.children:
                    if part.type == "identifier":
                        names.append(("default", _text(part, src)))
                    elif part.type == "namespace_import":
                        ident = next((c for c in part.children if c.type == "identifier"), None)
                        names.append(("*", _text(ident, src)))
                    elif part.type == "named_imports":
                        names.extend(_specifier(s, src) for s in part.children if s.type == "import_specifier")
        return [_js_fact(module, names, rng, "es_import")]
    # export_statement（source つき = 再エクスポート）
    for child in node.children:
        if child.type == "export_clause":
            names.extend(_specifier(s, src) for s in child.children if s.type == "export_specifier")
        elif child.type == "namespace_export":
            ident = next((c for c in child.children if c.type == "identifier"), None)
            names.append(("*", _text(ident, src) or None))
        elif child.type == "*":
            names.append(("*", None))
    return [_js_fact(module, names, rng, "es_import", reexport=True)]


def _require_facts(call: Node, rng_of: Callable[[Node], Range], src: bytes) -> list[ImportFact]:
    args = call.child_by_field_name("arguments")
    module = _string_value(args.named_children[0] if args is not None and args.named_children else None, src)
    if module is None:
        return []
    names: list[tuple[str, str | None]] = []
    holder = call  # 範囲に使う文
    member: str | None = None
    value: Node = call
    parent = call.parent
    if (
        parent is not None
        and parent.type == "member_expression"
        and parent.child_by_field_name("object") == call
    ):
        prop = parent.child_by_field_name("property")
        if prop is not None:
            member, value, parent = _text(prop, src), parent, parent.parent
    if (
        parent is not None
        and parent.type == "variable_declarator"
        and parent.child_by_field_name("value") == value
    ):
        target = parent.child_by_field_name("name")
        if target is not None:
            names.extend(_declared_names(target, member, src))
        decl = parent.parent
        holder = decl if decl is not None else parent
    elif parent is not None and parent.type == "expression_statement":
        holder = parent
    return [_js_fact(module, names, rng_of(holder), "require")]


def _declared_names(target: Node, member: str | None, src: bytes) -> list[tuple[str, str | None]]:
    """``const <target> = require("x")``（``member`` があれば ``require("x").member``）が束縛する名前。"""
    if target.type == "identifier":
        return [(member, _text(target, src)) if member else ("*", _text(target, src))]
    if target.type == "object_pattern" and member is None:
        return _object_pattern_names(target, src)
    return []


def js_require_bindings(declarator: Node, src: bytes) -> list[str]:
    """``variable_declarator`` が ``require("x")``（か ``require("x").y``）の結果として束縛する名前（別名があれば別名）。

    ``extract_imports`` が ``require`` の ``ImportFact`` に入れる名前と同じ形だけを返す（分割代入の入れ子・配列・rest は含まない。
    それらはただのローカル変数）。``syntax/relations.py`` が、関数の中のこの束縛を「ローカル変数」でなく「import」として
    扱うための共有。
    """
    value = declarator.child_by_field_name("value")
    target = declarator.child_by_field_name("name")
    if value is None or target is None:
        return []
    member: str | None = None
    if value.type == "member_expression":
        prop = value.child_by_field_name("property")
        obj = value.child_by_field_name("object")
        if prop is None or obj is None:
            return []
        member, value = _text(prop, src), obj
    if value.type != "call_expression" or not _is_require_call(value, src):
        return []
    return [alias or name for name, alias in _declared_names(target, member, src)]


def _is_require_call(call: Node, src: bytes) -> bool:
    fn = call.child_by_field_name("function")
    args = call.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    return (
        fn is not None
        and fn.type == "identifier"
        and _text(fn, src) == "require"
        and _string_value(first, src) is not None
    )


def _object_pattern_names(pattern: Node, src: bytes) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for child in pattern.named_children:
        if child.type == "shorthand_property_identifier_pattern":
            out.append((_text(child, src), None))
        elif child.type == "pair_pattern":
            key, val = child.child_by_field_name("key"), child.child_by_field_name("value")
            if val is not None and val.type == "assignment_pattern":
                val = val.child_by_field_name("left")
            if key is not None and val is not None and val.type == "identifier":
                out.append((_text(key, src), _text(val, src)))
        elif child.type == "object_assignment_pattern":
            left = child.child_by_field_name("left")
            if left is not None:
                out.append((_text(left, src), None))
    return out


# --- モジュールパス → ファイル ------------------------------------------------------------


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def _norm_root(root: str) -> str:
    root = posixpath.normpath(str(root).replace("\\", "/")) if str(root) else ""
    return "" if root == "." else root.strip("/")


@dataclass(frozen=True, slots=True)
class ImportRoot:
    """Python の絶対 import を探すディレクトリ 1 つ（ワークスペース相対。``""`` = ルート）。

    ``stdlib=False`` は pytest が ``sys.path`` に足す根（テストのディレクトリ・``pythonpath``）。ここでは標準ライブラリと pytest
    自身の名前（``PYTEST_PRELOADED``）を解かない: pytest がその根を足すのはテストを集める時点で、そのときには標準ライブラリの多くが
    ``sys.modules`` に読み込み済みなので、``import json`` は手元の ``json.py`` でなく標準ライブラリを返す（``sys.modules`` が
    ``sys.path`` より先）。実測（pytest 9.2・Python 3.12）で標準ライブラリの最上位 301 個のうち 159 個（``json``・``types`` を含む）が
    収集の前に読み込み済み。まだ読み込まれていない名前（``sqlite3`` など）では実行時に手元のファイルが勝つが、どれが読み込み済みかは
    版で変わるので、決定的に保つため一律に標準ライブラリ側（外部）に倒す。
    """

    path: str
    stdlib: bool = True


PYTEST_PRELOADED: frozenset[str] = frozenset(sys.stdlib_module_names) | {"pytest", "_pytest", "pluggy"}
"""``ImportRoot.stdlib`` が偽の根で解かない最上位のモジュール名（標準ライブラリと、pytest の実行中は必ず読み込み済みのもの）。"""


@functools.cache
def _py_test_globs() -> tuple[str, ...]:
    return tuple(getattr(get_support("python"), "test_path_globs", ()) or ())


def _pytest_basedir(directory: str, project: str, exists: Callable[[RelPath], bool]) -> str:
    """``directory`` から上へ、``__init__.py`` を持たない（か名前が識別子でない）最初のディレクトリ。``project`` より上には出ない。

    pytest の ``resolve_package_path`` の親（prepend のとき ``sys.path`` に足すディレクトリ）と同じ。
    """
    cur = directory
    while (
        cur
        and cur != project
        and posixpath.basename(cur).isidentifier()
        and exists(RelPath(_join(cur, "__init__.py")))
    ):
        cur = posixpath.dirname(cur)
    return cur


def _pytest_inserted(rel: str, project: str, exists: Callable[[RelPath], bool]) -> list[str]:
    """pytest（prepend / append）が ``rel`` を集めるときに ``sys.path`` に足すディレクトリ（先に探すものから）。

    テストモジュール自身の basedir、続いて ``rel`` のディレクトリから ``project`` までの祖先にある ``conftest.py`` の basedir
    （近いものから。conftest は外側から順に読まれて先頭に足されるので、内側が先になる）。
    """
    start = posixpath.dirname(rel)
    dirs = [_pytest_basedir(start, project, exists)]
    cur = start
    while True:
        if exists(RelPath(_join(cur, "conftest.py"))):
            dirs.append(_pytest_basedir(cur, project, exists))
        if not cur or cur == project:
            break
        cur = posixpath.dirname(cur)
    return dirs


def python_import_roots(
    from_rel: RelPath | str,
    *,
    exists: Callable[[RelPath], bool],
    pytest_imports: Callable[[str], PytestImports] | None = None,
) -> tuple[ImportRoot, ...]:
    """``from_rel`` の Python の絶対 import を探す根（先に探すものから。BUG-0082）。

    根の「プロジェクト」は、``from_rel`` を含むテストフィクスチャのルート（``workspace.languages.fixture_roots``。内側から）と、
    最後にワークスペースのルート ``""``。フィクスチャは入れ子のワークスペースとして自分を根に動くので、中の絶対 import
    （``from app.engine import Packer``）はまずフィクスチャの中で探す。各プロジェクト ``P`` について次の順:

    1. （いちばん内側の ``P`` だけ）``from_rel`` が ``P`` から見てテストのパス（``is_test``。``conftest.py`` を含む）で、
       ``P`` の pytest の ``--import-mode`` が ``importlib`` でなければ、pytest が足すディレクトリ（``_pytest_inserted``:
       テストモジュールの basedir ＝ ``__init__.py`` を持たない最初の祖先、続いて祖先の ``conftest.py`` の basedir）。
       ``tests/unit/test_a.py`` の ``import helpers`` は ``tests/unit/helpers.py``（``tests/unit/__init__.py`` があれば、
       その上の ``__init__.py`` の無いディレクトリから ``unit.helpers`` で探す）。
    2. ``P`` の pytest の設定の ``pythonpath``（``pytest_imports(P)``。``workspace.pyenv.detect_pytest_imports``）。
    3. ``P`` と ``P/src``（``DEFAULT_SOURCE_ROOTS``）。

    1 と 2 は ``ImportRoot(stdlib=False)``（標準ライブラリと同じ名前はそこで解かない。``ImportRoot`` の説明）。同じディレクトリが
    2 度出たら最初の位置に置き、どちらかが ``stdlib=True`` なら ``True``（ルート直下のテストファイルでも、ルートの扱いは今までどおり）。
    ``pytest_imports`` を渡さなければ設定は既定値（``pythonpath`` なし・``prepend``）。
    """
    rel = str(from_rel)
    globs = _py_test_globs()
    projects = [*reversed(fixture_roots(rel, globs)), ""]
    out: list[ImportRoot] = []
    for depth, project in enumerate(projects):
        config = pytest_imports(project) if pytest_imports is not None else PytestImports()
        if depth == 0 and config.import_mode != "importlib":
            inner = rel[len(project) + 1 :] if project else rel
            if is_test(inner, globs):
                out.extend(ImportRoot(d, stdlib=False) for d in _pytest_inserted(rel, project, exists))
        out.extend(ImportRoot(p, stdlib=False) for p in config.pythonpath)
        out.extend(ImportRoot(_join(project, str(r))) for r in DEFAULT_SOURCE_ROOTS)
    merged: dict[str, bool] = {}
    for root in out:
        merged[root.path] = merged.get(root.path, False) or root.stdlib
    return tuple(ImportRoot(path, stdlib) for path, stdlib in merged.items())


def resolve_module(
    module: str,
    *,
    from_rel: RelPath,
    language_id: str,
    is_relative: bool = False,
    level: int = 0,
    source_roots: Sequence[RelPath | ImportRoot] | None = None,
    exists: Callable[[RelPath], bool],
) -> RelPath | None:
    """``module`` をワークスペース内のファイルに対応づける。見つからない・外部・対応外の言語は ``None``。

    * Python: 絶対は ``source_roots``（既定 ``""``（ルート）と ``src``。``make_resolver`` は ``python_import_roots``）の順に、
      相対は ``from_rel`` の親から ``level - 1`` 段上のディレクトリで、``<mod>.py`` → ``<mod>/__init__.py`` → ``<mod>.pyi`` →
      ``<mod>/__init__.pyi`` の順に探す。``ImportRoot(stdlib=False)`` の根は、最上位の名前が ``PYTEST_PRELOADED`` なら飛ばす。
      ``module == ""``（``from . import x``）は、そのパッケージの ``__init__.py(i)``。
    * TS / JS: 相対指定子（``./`` ``../`` で始まるもの）だけ。拡張子を補い（``.ts .tsx .js .jsx .mjs``）、``/index.*`` を試す。
      ``"./x.js"`` は ``x.ts`` / ``x.tsx`` も探す。裸の指定子（``react``）・tsconfig の ``paths`` は ``None``（M1 の範囲外）。
    """
    if language_id in _PY_FAMILY:
        return _resolve_python(module, from_rel, is_relative or level > 0, level, source_roots, exists)
    if language_id in _JS_FAMILY:
        return _resolve_js(module, from_rel, exists)
    return None


def _first_existing(candidates: Iterable[str], exists: Callable[[RelPath], bool]) -> RelPath | None:
    for cand in candidates:
        rel = RelPath(cand)
        if exists(rel):
            return rel
    return None


def _py_candidates(base: str, parts: list[str]) -> list[str]:
    if not parts:
        return [_join(base, "__init__.py"), _join(base, "__init__.pyi")]
    stem = _join(base, *parts)
    return [f"{stem}.py", f"{stem}/__init__.py", f"{stem}.pyi", f"{stem}/__init__.pyi"]


def _resolve_python(
    module: str,
    from_rel: RelPath,
    relative: bool,
    level: int,
    source_roots: Sequence[RelPath | ImportRoot] | None,
    exists: Callable[[RelPath], bool],
) -> RelPath | None:
    parts = module.split(".") if module else []
    if any(not p or "/" in p or "\\" in p or p in {".", ".."} for p in parts):
        return None
    if relative:
        base = posixpath.dirname(str(from_rel))
        for _ in range(max(level, 1) - 1):
            if not base:
                return None  # ルートより上
            base = posixpath.dirname(base)
        return _first_existing(_py_candidates(base, parts), exists)
    if not parts:
        return None
    roots = DEFAULT_SOURCE_ROOTS if source_roots is None else source_roots
    preloaded = parts[0] in PYTEST_PRELOADED
    seen: set[str] = set()
    for root in roots:
        if isinstance(root, ImportRoot):
            if preloaded and not root.stdlib:
                continue
            base = _norm_root(root.path)
        else:
            base = _norm_root(str(root))
        if base in seen:
            continue
        seen.add(base)
        found = _first_existing(_py_candidates(base, parts), exists)
        if found is not None:
            return found
    return None


def _resolve_js(spec: str, from_rel: RelPath, exists: Callable[[RelPath], bool]) -> RelPath | None:
    if not (spec in {".", ".."} or spec.startswith(("./", "../"))):
        return None  # 裸の指定子（node_modules・paths）と絶対パスは M1 の範囲外
    target = posixpath.normpath(posixpath.join(posixpath.dirname(str(from_rel)), spec))
    if target == ".":
        target = ""
    elif target == ".." or target.startswith("../") or target.startswith("/"):
        return None  # ワークスペースの外
    candidates: list[str] = []
    ext = posixpath.splitext(target)[1].lower()
    if target and ext in _JS_SWAPS:
        stem = target[: -len(ext)]
        candidates.extend(stem + e for e in _JS_SWAPS[ext])
    if target:
        candidates.extend(target + e for e in _JS_EXTENSIONS)
    candidates.extend(_join(target, "index" + e) for e in _JS_EXTENSIONS)
    return _first_existing(dict.fromkeys(candidates), exists)


class ImportResolver:
    """1 つのファイルの import を解決する（``make_resolver`` が作る）。呼ぶと ``ImportFact`` → ファイル（``imported_name_map`` の
    ``resolve``）。``namespace_dir`` は Python の名前空間パッケージのディレクトリ（BUG-0101）。

    Python の絶対 import の根は、``source_roots`` を渡せばそれ、渡さなければ ``python_import_roots``（``pytest_imports`` は
    プロジェクトのディレクトリ → pytest の設定。最初の絶対 import で 1 回だけ決める）。``exists`` / ``is_dir`` の答えはこの中で
    覚えて使い回す（同じパスを何度も調べない）。
    """

    __slots__ = ("_computed", "_dirs", "_exists", "_from_rel", "_is_dir", "_known", "_language_id", "_pytest", "_roots")

    def __init__(
        self,
        *,
        from_rel: RelPath,
        language_id: str,
        exists: Callable[[RelPath], bool],
        source_roots: Sequence[RelPath | ImportRoot] | None,
        pytest_imports: Callable[[str], PytestImports] | None,
        is_dir: Callable[[RelPath], bool] | None,
    ) -> None:
        self._from_rel = from_rel
        self._language_id = language_id
        self._exists = exists
        self._roots = source_roots
        self._pytest = pytest_imports
        self._is_dir = is_dir
        self._known: dict[str, bool] = {}
        self._dirs: dict[str, bool] = {}
        self._computed: tuple[ImportRoot, ...] | None = None

    def exists(self, rel: RelPath) -> bool:
        key = str(rel)
        hit = self._known.get(key)
        if hit is None:
            hit = self._known[key] = bool(self._exists(rel))
        return hit

    def _dir(self, rel: str) -> bool:
        if self._is_dir is None:
            return False
        hit = self._dirs.get(rel)
        if hit is None:
            hit = self._dirs[rel] = bool(self._is_dir(RelPath(rel)))
        return hit

    def _roots_for(self, relative: bool) -> Sequence[RelPath | ImportRoot] | None:
        if self._roots is not None or self._language_id not in _PY_FAMILY or relative:
            return self._roots
        if self._computed is None:
            self._computed = python_import_roots(self._from_rel, exists=self.exists, pytest_imports=self._pytest)
        return self._computed

    def __call__(self, fact: ImportFact) -> RelPath | None:
        return resolve_module(
            fact.module,
            from_rel=self._from_rel,
            language_id=self._language_id,
            is_relative=fact.is_relative,
            level=fact.level,
            source_roots=self._roots_for(fact.is_relative or fact.level > 0),
            exists=self.exists,
        )

    def namespace_dir(self, module: str) -> RelPath | None:
        """絶対のモジュールのパス ``module`` がワークスペースの名前空間パッケージなら、そのディレクトリ（BUG-0101）。

        Python の探し方と同じく、どの根にもファイル（``<mod>.py``・``<mod>/__init__.py``・``.pyi``）が無いときだけ、根の順に
        ``<根>/<mod>`` がディレクトリで ``__init__.py(i)`` を持たない最初のもの（モジュール・普通のパッケージは後ろの根にあっても
        名前空間パッケージの部分より勝つ）。標準ライブラリの名前は解かない（``json/`` があっても ``import json`` は標準ライブラリ）。
        ``is_dir`` を渡さなかった resolver・Python 以外は常に ``None``。
        """
        parts = module.split(".") if module else []
        if (
            self._is_dir is None
            or self._language_id not in _PY_FAMILY
            or not parts
            or any(not p.isidentifier() for p in parts)
            or parts[0] in sys.stdlib_module_names
        ):
            return None
        fact = ImportFact(module, [], False, 0, _NO_RANGE, "import")
        if self(fact) is not None:
            return None
        roots = self._roots_for(False)
        for root in DEFAULT_SOURCE_ROOTS if roots is None else roots:
            if isinstance(root, ImportRoot) and not root.stdlib and parts[0] in PYTEST_PRELOADED:
                continue
            base = _norm_root(root.path if isinstance(root, ImportRoot) else str(root))
            found = _namespace_dir_at(_join(base, *parts), self.exists, self._dir)
            if found is not None:
                return found
        return None

    def namespace_subdir(self, base: str, parts: Sequence[str]) -> RelPath | None:
        """パッケージのディレクトリ ``base`` の下の ``parts`` が名前空間パッケージのディレクトリなら、そのパス（``import pkg`` の
        ``pkg.nsdir``。``nsdir`` に ``__init__.py`` が無い）。"""
        if any(not p.isidentifier() for p in parts):
            return None
        return _namespace_dir_at(_join(base, *parts), self.exists, self._dir)


def _namespace_dir_at(
    path: str, exists: Callable[[RelPath], bool], is_dir: Callable[[str], bool]
) -> RelPath | None:
    if not path or not is_dir(path):
        return None
    if exists(RelPath(_join(path, "__init__.py"))) or exists(RelPath(_join(path, "__init__.pyi"))):
        return None
    return RelPath(path)


def make_resolver(
    *,
    from_rel: RelPath,
    language_id: str,
    exists: Callable[[RelPath], bool],
    source_roots: Sequence[RelPath | ImportRoot] | None = None,
    pytest_imports: Callable[[str], PytestImports] | None = None,
    is_dir: Callable[[RelPath], bool] | None = None,
) -> ImportResolver:
    """``imported_name_map`` に渡す ``resolve`` を作る（1 つのファイルの import を解決する。``ImportResolver``）。

    ``is_dir`` は名前空間パッケージのディレクトリを見分けるときだけ使う（``ImportResolver.namespace_dir``。渡さなければ見分けない）。
    """
    return ImportResolver(
        from_rel=from_rel,
        language_id=language_id,
        exists=exists,
        source_roots=source_roots,
        pytest_imports=pytest_imports,
        is_dir=is_dir,
    )


# --- 名前の表 ---------------------------------------------------------------------------


def imported_name_map(
    facts: Iterable[ImportFact],
    resolve: Callable[[ImportFact], RelPath | None],
    *,
    include_reexports: bool = False,
) -> dict[str, tuple[RelPath, str]]:
    """ファイル内で使える名前 → ``(定義元ファイル, 元の名前)``。

    ``resolve`` は ``ImportFact`` 1 件をファイルに対応づける（``make_resolver`` で作る）。解決できない import（外部・存在しない）は入らない。
    同じ名前が複数あれば、文書順で後のものが勝つ（Python の再束縛と同じ）。元の名前が ``"*"`` のときはモジュールそのもの、
    ``"default"`` は ES の既定エクスポート。ワイルドカード（``from x import *``）は名前を列挙できないので入れない
    （``wildcard_sources`` で別に取る）。``export ... from`` は既定では入れない（``include_reexports=True`` で、公開される名前を入れる）。

    Python の ``from pkg import name`` は、``pkg.name`` がモジュール（ファイル）として解決できるならサブモジュールを優先する
    （``from . import x`` の典型）。``__init__`` が同名の別物を定義している場合の取り違えは、構文だけの推定の限界。
    """
    out: dict[str, tuple[RelPath, str]] = {}
    for fact in facts:
        if fact.reexport and not include_reexports:
            continue
        if fact.kind == "import" and not fact.names:  # ``import a.b`` は先頭の ``a`` を束ねる
            top = fact.module.split(".")[0]
            target = resolve(replace(fact, module=top)) if top else None
            if target is not None:
                out[top] = (target, "*")
            continue
        base = resolve(fact)
        for name, alias in fact.names:
            bound = alias or name
            if name == "*" and alias is None:
                continue  # ワイルドカード
            if fact.kind == "from":
                sub = resolve(replace(fact, module=f"{fact.module}.{name}" if fact.module else name))
                if sub is not None:
                    out[bound] = (sub, "*")
                    continue
            if base is not None:
                out[bound] = (base, name)
    return out


def namespace_name_map(
    facts: Iterable[ImportFact],
    resolve: Callable[[ImportFact], RelPath | None],
    *,
    internal: Collection[str] = (),
) -> dict[str, str]:
    """名前空間パッケージ（ワークスペースの ``__init__.py`` の無いディレクトリ）を束ねた名前 → その絶対のモジュールのパス（BUG-0101）。

    ``import nsp.tools.fmt`` の ``nsp`` → ``"nsp"``、``import nsp.tools as nt`` の ``nt`` → ``"nsp.tools"``、``from nsp import tools`` の
    ``tools`` → ``"nsp.tools"``。``internal``（``imported_name_map`` の名前。ファイルに解けたもの）と相対 import・再エクスポートは入れない。

    import したパスそのもの（最上位より下）がワークスペースにあるときだけ数える: ``import nsp.tools.fmt`` は ``nsp/tools/fmt.py`` が
    ファイルか名前空間パッケージのディレクトリ、``import nsp.tools as nt`` / ``from nsp import tools`` は ``nsp/tools`` が名前空間
    パッケージのディレクトリ。最上位だけ（``import packaging``）や、その下がワークスペースに無い（``import packaging.version``）ものは、
    同名の外部のパッケージ（Python では普通のパッケージが名前空間パッケージの部分より勝つ）とみなして今までどおり外部にする
    （Lucianke 自身の ``packaging/`` と外部の ``packaging``）。``resolve`` が ``ImportResolver`` で ``is_dir`` を持つときだけ見分ける
    （それ以外は空）。Python のモジュール直下の import だけを渡す。
    """
    out: dict[str, str] = {}
    if not isinstance(resolve, ImportResolver):
        return out
    for fact in facts:
        if fact.reexport or fact.is_relative or fact.level > 0 or (fact.kind == "import" and "." not in fact.module):
            continue
        if fact.kind == "import" and not fact.names:
            head = fact.module.split(".")[0]
            full = ImportFact(fact.module, [], False, 0, _NO_RANGE, "import")
            if (
                head not in internal
                and resolve.namespace_dir(head) is not None
                and (resolve(full) is not None or resolve.namespace_dir(fact.module) is not None)
            ):
                out[head] = head
            continue
        if fact.kind == "import":
            bindings = [(alias, fact.module) for name, alias in fact.names if name == "*" and alias]
        elif fact.kind == "from" and fact.module:
            bindings = [(alias or name, f"{fact.module}.{name}") for name, alias in fact.names if name != "*"]
        else:
            continue
        for bound, module in bindings:
            if bound and bound not in internal and resolve.namespace_dir(module) is not None:
                out[bound] = module
    return out


def dotted_module_path(
    dotted: str,
    *,
    facts: Iterable[ImportFact],
    names: Mapping[str, tuple[RelPath, str]],
    resolve: Callable[[ImportFact], RelPath | None],
    exists: Callable[[RelPath], bool],
    namespaces: Mapping[str, str] | None = None,
) -> RelPath | str | None:
    """点つきのモジュールのパス ``dotted``（``pkg.helpers``。先頭はモジュール直下の import が束ねた名前）→ そのサブモジュールのファイル（BUG-0093）。

    * 先頭の名前がモジュールのファイルに束ねられている（``import pkg.x`` の ``pkg``・``import a.b as ab``・``from pkg import sub``。
      ``names`` は ``imported_name_map``）: そのファイルがパッケージ（``__init__.py(i)``）なら、そのディレクトリの下で残りの名前を
      ``<mod>.py`` → ``<mod>/__init__.py`` → ``<mod>.pyi`` → ``<mod>/__init__.pyi`` の順に探す（``import`` と同じ）。
    * 先頭の名前が名前空間パッケージを束ねている（``namespaces``。``namespace_name_map``）か、``import head.x`` の ``head`` で
      ``head`` 自身はワークスペースのファイルに解けない: 束ねたモジュールのパスに残りを足した点つきのパスを絶対 import として解く
      （``resolve``）。標準ライブラリの名前は解かない（``os.path`` は外部のまま）。

    ファイルが無く、その点つきのパスがワークスペースの名前空間パッケージのディレクトリ（``__init__.py`` の無いディレクトリ）なら
    ``NAMESPACE_PACKAGE``（参照でも外部でもない。BUG-0101）。先頭の名前だけ（``dotted`` に点が無い）は、名前空間パッケージを
    束ねた名前なら ``NAMESPACE_PACKAGE``、それ以外は ``None``。ディレクトリを見分けるのは ``resolve`` が ``ImportResolver`` で
    ``is_dir`` を持つときだけ。見つからなければ ``None``（呼び手は今までどおり外部・名前だけにする）。Python 以外は呼ばない。
    """
    head, _, rest = dotted.partition(".")
    if not head:
        return None
    parts = rest.split(".") if rest else []
    if any(not p.isidentifier() for p in parts):
        return None
    bound = names.get(head)
    if bound is not None:
        if not parts:
            return None
        path, original = bound
        if original != "*":
            return None  # 名前（クラス・関数・変数）の属性。モジュールではない
        base, leaf = posixpath.split(str(path))
        if leaf not in ("__init__.py", "__init__.pyi"):
            return None  # パッケージでないモジュールはサブモジュールを持たない
        found = _first_existing(_py_candidates(base, parts), exists)
        if found is None and isinstance(resolve, ImportResolver) and resolve.namespace_subdir(base, parts) is not None:
            return NAMESPACE_PACKAGE  # 普通のパッケージの下の名前空間パッケージ（``pkg/nsdir/`` に ``__init__.py`` が無い）
        return found
    if namespaces and head in namespaces:
        module = namespaces[head]
    elif head in sys.stdlib_module_names or not parts:
        return None
    elif any(
        f.kind == "import" and not f.names and f.module.split(".")[0] == head and not f.reexport for f in facts
    ):
        module = head
    else:
        return None
    if not parts:
        return NAMESPACE_PACKAGE  # 名前空間パッケージを束ねた名前そのもの（``namespaces`` が見分け済み）
    full = ".".join([module, *parts])
    found = resolve(ImportFact(full, [], False, 0, _NO_RANGE, "import"))
    if found is None and isinstance(resolve, ImportResolver) and resolve.namespace_dir(full) is not None:
        return NAMESPACE_PACKAGE
    return found


def wildcard_sources(
    facts: Iterable[ImportFact],
    resolve: Callable[[ImportFact], RelPath | None],
) -> list[RelPath]:
    """``from x import *`` / ``export * from`` の解決先ファイル（文書順、重複なし）。名前は呼び手が公開シンボルから引く。"""
    out: list[RelPath] = []
    for fact in facts:
        if ("*", None) in fact.names:
            target = resolve(fact)
            if target is not None and target not in out:
                out.append(target)
    return out


def module_all_names(parsed: ParsedFile) -> tuple[str, ...] | None:
    """Python のモジュール直下の ``__all__`` の名前（文書順・重複なし。BUG-0100 の ``from x import *`` の再公開）。

    読む文: ``__all__ = [...]`` / ``(...)``（注釈つきも）、``__all__ += [...]``、``__all__.extend([...])``、``__all__.append("x")``。
    どれも文字列のリテラルだけで書かれていればその名前、``__all__`` が無い・リテラルでない部分がある（``__all__ = base.__all__ + [...]``）
    なら ``None``（呼び手は公開名〈``_`` で始まらない名前〉の規則に倒す。どちらかは ``module_all_dynamic`` が見分ける）。
    """
    return _module_all(parsed)[1]


def module_all_dynamic(parsed: ParsedFile) -> bool:
    """Python のモジュール直下に ``__all__`` を書く文があり、文字列のリテラルだけでは書かれていない（``__all__ = base.__all__ + [...]``・
    内包表記など）か。``from x import *`` が公開する名前の集合が構文から決まらない（BUG-0105。``index/reexports.py`` は、公開名の規則で
    見つけた名前を ``heuristic`` で結ぶ）。"""
    seen, names = _module_all(parsed)
    return seen and names is None


def _module_all(parsed: ParsedFile) -> tuple[bool, tuple[str, ...] | None]:
    """``(__all__ を書く文があるか, リテラルだけならその名前・それ以外は None)``。"""
    src = parsed.source
    names: list[str] = []
    seen = False
    for stmt in parsed.tree.root_node.named_children:
        if stmt.type != "expression_statement" or not stmt.named_children:
            continue
        expr = stmt.named_children[0]
        value: Node | None = None
        mode = ""
        if expr.type in ("assignment", "augmented_assignment"):
            left = expr.child_by_field_name("left")
            if left is None or left.type != "identifier" or _text(left, src) != "__all__":
                continue
            value = expr.child_by_field_name("right")
            if expr.type == "assignment":
                mode = "set"
            else:
                op = expr.child_by_field_name("operator")
                mode = "extend" if op is not None and _text(op, src) == "+=" else "bad"
        elif expr.type == "call":
            fn = expr.child_by_field_name("function")
            obj = fn.child_by_field_name("object") if fn is not None and fn.type == "attribute" else None
            attr = fn.child_by_field_name("attribute") if fn is not None and fn.type == "attribute" else None
            if obj is None or attr is None or obj.type != "identifier" or _text(obj, src) != "__all__":
                continue
            mode = {"extend": "extend", "append": "append"}.get(_text(attr, src), "bad")
            args = expr.child_by_field_name("arguments")
            items = [a for a in args.named_children if a.type != "comment"] if args is not None else []
            value = items[0] if len(items) == 1 else None
        else:
            continue
        seen = True
        literal = _literal(value, src)
        if mode == "append" and isinstance(literal, str):
            literal = [literal]
        if mode == "bad" or not isinstance(literal, (list, tuple)) or not all(isinstance(x, str) for x in literal):
            return True, None
        if mode == "set":
            names = []
        for x in literal:
            if x not in names:
                names.append(x)
    return seen, (tuple(names) if seen else None)


@dataclass(frozen=True, slots=True)
class JsLocalExports:
    """TS / JS のモジュール直下の、``from`` の無い export（BUG-0104。``export ... from`` は ``ImportFact.reexport``）。

    * ``declared``: 宣言につけた export が公開する名前（``export function f``・``export const a = 1, b = 2``・``export class C``・
      ``export interface I``・``export type T``・``export enum E``・``export namespace N``・``export declare ...``）。``export default`` は入れない。
    * ``clauses``: ``export { a, b as c }`` の ``(ローカルの名前, 公開する名前)``（``("a", "a")``・``("b", "c")``）。
    * ``default_local``: 既定の export が束ねるローカルの名前（``export default function g``・``export default class G``・
      ``export default h``・``export { x as default }``）。無名・式なら ``None``。
    """

    declared: tuple[str, ...] = ()
    clauses: tuple[tuple[str, str], ...] = ()
    default_local: str | None = None


_JS_NAMED_DECLARATIONS = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_signature",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "internal_module",
        "module",
    }
)


def _js_declared_names(decl: Node, src: bytes) -> list[str]:
    """宣言 ``decl`` が束ねる名前（分割代入のパターンは数えない）。"""
    if decl.type == "ambient_declaration":
        return [n for c in decl.named_children for n in _js_declared_names(c, src)]
    if decl.type in _JS_NAMED_DECLARATIONS:
        name = decl.child_by_field_name("name")
        return [_text(name, src)] if name is not None and name.type in ("identifier", "type_identifier") else []
    if decl.type in ("lexical_declaration", "variable_declaration"):
        out: list[str] = []
        for d in decl.named_children:
            if d.type != "variable_declarator":
                continue
            name = d.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                out.append(_text(name, src))
        return out
    return []


def js_local_exports(parsed: ParsedFile) -> JsLocalExports:
    """TS / JS のモジュール直下の ``from`` の無い export（``JsLocalExports``。BUG-0104 のバレルの公開する名前）。"""
    src = parsed.source
    declared: list[str] = []
    clauses: list[tuple[str, str]] = []
    default_local: str | None = None
    for stmt in parsed.tree.root_node.named_children:
        if stmt.type != "export_statement" or stmt.child_by_field_name("source") is not None:
            continue
        is_default = any(c.type == "default" for c in stmt.children)
        decl = stmt.child_by_field_name("declaration")
        if is_default:
            value = stmt.child_by_field_name("value")
            if decl is not None:
                names = _js_declared_names(decl, src)
                default_local = names[0] if names else None
            elif value is not None and value.type == "identifier":
                default_local = _text(value, src)
            continue
        if decl is not None:
            declared.extend(n for n in _js_declared_names(decl, src) if n and n not in declared)
            continue
        for child in stmt.children:
            if child.type != "export_clause":
                continue
            for spec in child.children:
                if spec.type != "export_specifier":
                    continue
                name, alias = _specifier(spec, src)
                if not name:
                    continue
                exported = alias or name
                if exported == "default":
                    default_local = name
                else:
                    clauses.append((name, exported))
    return JsLocalExports(tuple(declared), tuple(clauses), default_local)


def js_module_exports(
    facts: Iterable[ImportFact],
    resolve: Callable[[ImportFact], RelPath | None],
    local: JsLocalExports,
    *,
    workspace_id: str,
    rel: RelPath,
    names: Mapping[str, tuple[RelPath, str]],
    external: Mapping[str, str],
    top_level: Collection[str],
) -> list[tuple[str, str, str]]:
    """TS / JS のモジュールが公開する名前 → ``(kind, name, target_urn)`` の列（BUG-0104。内部表 ``_reexports`` の行）。

    * ``import``（``name`` を ``target_urn`` に束ねて公開する。別の定義を公開する名前だけ）: ``export { a as b } from './m'`` → ``b`` →
      ``m#a``、``export * as ns from './m'`` → ``ns`` → モジュール ``m``（``#`` なし）、import した名前の ``export { a }``（``import { a }
      from './m'`` → ``m#a``、``import * as ns`` → ``m``、``import d from './m'`` → ``m#default``）、同じファイルの定義の別名
      ``export { f as g }`` → ``g`` → ``<このファイル>#f``、既定の export（``export default function g``・``export default h``・
      ``export { x as default }``）→ ``default`` → そのローカルの名前の先。外部の指定子は ``lk://_ext/<pkg>#a``。
    * ``star``（``name`` は ``*``）: ``export * from './m'`` のワークスペースの先 ``m``。外部の ``export *`` は名前を列挙できないので書かない。
    * ``export``（``target_urn`` は空）: 公開する名前（宣言の export・``export { … }``・``export … from`` の名前）。既定の export は入れない
      （``export *`` は既定の export を公開しない）。
    """
    out: list[tuple[str, str, str]] = []

    def ws_urn(path: RelPath, qualified: str | None = None) -> str | None:
        try:
            return str(Urn(workspace_id, str(path), CodeSel(qualified) if qualified else None))
        except ValueError:
            return None

    def bind(name: str, target: str | None) -> None:
        if target:
            out.append(("import", name, target))

    def export(name: str) -> None:
        if name and name != "default":
            out.append(("export", name, ""))

    for fact in facts:
        if not fact.reexport:
            continue
        base = resolve(fact)
        package = None if base is not None or fact.is_relative else _js_package(fact.module)
        for name, alias in fact.names:
            if name == "*" and alias is None:
                if base is not None and (urn := ws_urn(base)) is not None:
                    out.append(("star", "*", urn))
                continue
            bound = alias or name
            if name == "*":  # ``export * as ns from``
                target = ws_urn(base) if base is not None else (_external_urn(package) if package else None)
            elif base is not None:
                target = ws_urn(base, name)
            else:
                target = _external_urn(package, name) if package else None
            bind(bound, target)
            export(bound)

    def local_target(name: str) -> str | None:
        hit = names.get(name)
        if hit is not None:
            path, original = hit
            return ws_urn(path) if original == "*" else ws_urn(path, original)
        if name in external:
            return external[name]
        return ws_urn(rel, name) if name in top_level else None

    for name in local.declared:
        export(name)
    for name, exported in local.clauses:
        if name != exported or name not in top_level:
            bind(exported, local_target(name))
        export(exported)
    if local.default_local:
        bind("default", local_target(local.default_local))
    return out


def _literal(node: Node | None, src: bytes) -> object:
    if node is None:
        return None
    try:
        return ast.literal_eval(_text(node, src))
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None


def _external_urn(package: str, qualified: str | None = None) -> str | None:
    try:
        return str(Urn.external(package, None, qualified or None))
    except ValueError:
        return None


def _js_package(spec: str) -> str | None:
    """裸の指定子のパッケージ名。相対・絶対の指定子は ``None``。``node:fs`` は ``fs``、``@a/b/c`` は ``@a/b``、``a/b`` は ``a``。"""
    spec = spec.removeprefix("node:")
    if not spec or spec[0] in "./" or spec.startswith("\\"):
        return None
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 and parts[1] else None
    return parts[0]


def external_name_map(
    facts: Iterable[ImportFact],
    *,
    language_id: str,
    internal: Collection[str] = (),
) -> dict[str, str]:
    """解決できなかった import が束ねる名前 → 外部シンボルの URN。同じ名前は文書順で後のものが勝つ。

    ``internal`` は ``imported_name_map`` の名前（ワークスペース内に解決できたもの。同じ名前は入れない）。
    相対 import・``__future__``・ワイルドカード・``export ... from``（再エクスポート）は入れない。
    Python の ``import a.b.c``（別名なし）は ``a`` を ``lk://_ext/a`` に、``import a.b as c`` は ``c`` を ``lk://_ext/a#b`` に、
    ``from a.b import c as d`` は ``d`` を ``lk://_ext/a#b.c`` に束ねる。
    """
    out: dict[str, str] = {}
    if language_id in _PY_FAMILY:
        for fact in facts:
            if fact.reexport or fact.is_relative or fact.level > 0:
                continue
            top, _, rest = fact.module.partition(".")
            if not top or top == "__future__":
                continue
            if fact.kind == "import" and not fact.names:  # ``import a.b.c`` は先頭の ``a`` を束ねる
                if top not in internal and (urn := _external_urn(top)) is not None:
                    out[top] = urn
                continue
            for name, alias in fact.names:
                if fact.kind == "import":  # ``import a.b as c``: ``("*", "c")``
                    bound, qualified = alias, rest
                elif name == "*":
                    continue
                else:  # ``from a.b import c``
                    bound, qualified = alias or name, ".".join(p for p in (rest, name) if p)
                if bound and bound not in internal and (urn := _external_urn(top, qualified)) is not None:
                    out[bound] = urn
    elif language_id in _JS_FAMILY:
        for fact in facts:
            if fact.reexport or fact.is_relative:
                continue
            package = _js_package(fact.module)
            if package is None:
                continue
            for name, alias in fact.names:
                if name in ("default", "*"):  # 既定 import・名前空間 import / require は、モジュールそのもの
                    bound, qualified = alias, None
                else:
                    bound, qualified = alias or name, name
                if bound and bound not in internal and (urn := _external_urn(package, qualified)) is not None:
                    out[bound] = urn
    return out
