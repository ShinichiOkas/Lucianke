"""import 文の解析と、モジュールパス → ワークスペース内ファイルの対応（設計書 §7.4.6、仕様書 §8.3 ``structural``）。

LSP なしの定義解決（T-04 処理規約 3）と呼び出し関係の ``structural`` 行（T-06・T-07）が使う、クロスファイル名前解決の土台。

* ``extract_imports``: ``ParsedFile`` から ``ImportFact`` を文書順に集める。Python（``import`` / ``from ... import``、``__future__`` を含む）と
  TypeScript / JavaScript（``import`` / ``export ... from`` / ``import x = require()`` / ``require()``）。
* ``resolve_module``: モジュール名 1 つをワークスペース内のファイルに対応づける。ファイルの存在は呼び手が ``exists`` で教える
  （workspace に依存しない）。``node_modules``・tsconfig の ``paths``・Python の site-packages は扱わない（外部は ``None``）。
* ``imported_name_map``: ファイル内で使える名前 → ``(定義元ファイル, 元の名前)``。解決できた import だけが入る。

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

import posixpath
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from tree_sitter import Node, Query, QueryCursor

from lucianke.core.model import Range, RelPath
from lucianke.syntax.types import ParsedFile

__all__ = [
    "DEFAULT_SOURCE_ROOTS",
    "ImportFact",
    "ImportKind",
    "extract_imports",
    "imported_name_map",
    "make_resolver",
    "resolve_module",
    "wildcard_sources",
]

ImportKind = Literal["import", "from", "es_import", "require"]

DEFAULT_SOURCE_ROOTS: tuple[RelPath, ...] = (RelPath(""), RelPath("src"))  # "" = ワークスペースのルート

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


def _text(node: Node | None) -> str:
    return "" if node is None or node.text is None else node.text.decode("utf-8", errors="replace")


def _string_value(node: Node | None) -> str | None:
    """``string`` ノードの中身（引用符を除く）。"""
    if node is None or node.type != "string":
        return None
    text = _text(node)
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
    for _key, node in sorted(nodes.items(), key=lambda kv: kv[0][:2]):
        if family == "python":
            facts.extend(_python_facts(node, rng(node)))
        elif node.type == "call_expression":
            facts.extend(_require_facts(node, rng))
        else:
            facts.extend(_es_facts(node, rng(node)))
    return facts


# --- Python -----------------------------------------------------------------------------


def _python_facts(node: Node, rng: Range) -> list[ImportFact]:
    if node.type == "import_statement":
        out: list[ImportFact] = []
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                module = _text(child.child_by_field_name("name"))
                alias = _text(child.child_by_field_name("alias"))
                if module and alias:
                    out.append(ImportFact(module, [("*", alias)], False, 0, rng, "import"))
            elif child.type == "dotted_name":
                out.append(ImportFact(_text(child), [], False, 0, rng, "import"))
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
            level = _text(prefix).count(".")
            module = _text(next((c for c in mod.children if c.type == "dotted_name"), None))
        else:
            module, level = _text(mod), 0
    names: list[tuple[str, str | None]] = []
    for child in node.children_by_field_name("name"):
        if child.type == "aliased_import":
            names.append(
                (_text(child.child_by_field_name("name")), _text(child.child_by_field_name("alias")) or None)
            )
        elif child.type == "dotted_name":
            names.append((_text(child), None))
    if any(c.type == "wildcard_import" for c in node.children):
        names.append(("*", None))
    return [ImportFact(module, names, level > 0, level, rng, "from")]


# --- TypeScript / JavaScript ------------------------------------------------------------


def _js_fact(
    module: str,
    names: list[tuple[str, str | None]],
    rng: Range,
    kind: ImportKind,
    *,
    reexport: bool = False,
) -> ImportFact:
    return ImportFact(module, names, module.startswith("."), 0, rng, kind, reexport)


def _specifier(node: Node) -> tuple[str, str | None]:
    """``import_specifier`` / ``export_specifier`` → ``(name, alias)``（名前が文字列リテラルのこともある）。"""
    name_node = node.child_by_field_name("name")
    name = (
        _string_value(name_node) if name_node is not None and name_node.type == "string" else _text(name_node)
    )
    alias = node.child_by_field_name("alias")
    return (name or "", _text(alias) or None)


def _es_facts(node: Node, rng: Range) -> list[ImportFact]:
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "import_require_clause":  # ``import x = require("y")``（source は節の中）
                ident = next((c for c in child.children if c.type == "identifier"), None)
                src = _string_value(child.child_by_field_name("source"))
                return (
                    [_js_fact(src, [("*", _text(ident))], rng, "require")]
                    if ident is not None and src is not None
                    else []
                )
    module = _string_value(node.child_by_field_name("source"))
    if module is None:
        return []
    names: list[tuple[str, str | None]] = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "import_clause":
                for part in child.children:
                    if part.type == "identifier":
                        names.append(("default", _text(part)))
                    elif part.type == "namespace_import":
                        ident = next((c for c in part.children if c.type == "identifier"), None)
                        names.append(("*", _text(ident)))
                    elif part.type == "named_imports":
                        names.extend(_specifier(s) for s in part.children if s.type == "import_specifier")
        return [_js_fact(module, names, rng, "es_import")]
    # export_statement（source つき = 再エクスポート）
    for child in node.children:
        if child.type == "export_clause":
            names.extend(_specifier(s) for s in child.children if s.type == "export_specifier")
        elif child.type == "namespace_export":
            ident = next((c for c in child.children if c.type == "identifier"), None)
            names.append(("*", _text(ident) or None))
        elif child.type == "*":
            names.append(("*", None))
    return [_js_fact(module, names, rng, "es_import", reexport=True)]


def _require_facts(call: Node, rng_of: Callable[[Node], Range]) -> list[ImportFact]:
    args = call.child_by_field_name("arguments")
    module = _string_value(args.named_children[0] if args is not None and args.named_children else None)
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
            member, value, parent = _text(prop), parent, parent.parent
    if (
        parent is not None
        and parent.type == "variable_declarator"
        and parent.child_by_field_name("value") == value
    ):
        target = parent.child_by_field_name("name")
        if target is not None:
            if target.type == "identifier":
                names.append((member, _text(target)) if member else ("*", _text(target)))
            elif target.type == "object_pattern" and member is None:
                names.extend(_object_pattern_names(target))
        decl = parent.parent
        holder = decl if decl is not None else parent
    elif parent is not None and parent.type == "expression_statement":
        holder = parent
    return [_js_fact(module, names, rng_of(holder), "require")]


def _object_pattern_names(pattern: Node) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for child in pattern.named_children:
        if child.type == "shorthand_property_identifier_pattern":
            out.append((_text(child), None))
        elif child.type == "pair_pattern":
            key, val = child.child_by_field_name("key"), child.child_by_field_name("value")
            if val is not None and val.type == "assignment_pattern":
                val = val.child_by_field_name("left")
            if key is not None and val is not None and val.type == "identifier":
                out.append((_text(key), _text(val)))
        elif child.type == "object_assignment_pattern":
            left = child.child_by_field_name("left")
            if left is not None:
                out.append((_text(left), None))
    return out


# --- モジュールパス → ファイル ------------------------------------------------------------


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def _norm_root(root: str) -> str:
    root = posixpath.normpath(str(root).replace("\\", "/")) if str(root) else ""
    return "" if root == "." else root.strip("/")


def resolve_module(
    module: str,
    *,
    from_rel: RelPath,
    language_id: str,
    is_relative: bool = False,
    level: int = 0,
    source_roots: Sequence[RelPath] | None = None,
    exists: Callable[[RelPath], bool],
) -> RelPath | None:
    """``module`` をワークスペース内のファイルに対応づける。見つからない・外部・対応外の言語は ``None``。

    * Python: 絶対は ``source_roots``（既定 ``""``（ルート）と ``src``）の順に、相対は ``from_rel`` の親から ``level - 1`` 段上の
      ディレクトリで、``<mod>.py`` → ``<mod>/__init__.py`` → ``<mod>.pyi`` → ``<mod>/__init__.pyi`` の順に探す。
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
    source_roots: Sequence[RelPath] | None,
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
    seen: set[str] = set()
    for root in roots:
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


def make_resolver(
    *,
    from_rel: RelPath,
    language_id: str,
    exists: Callable[[RelPath], bool],
    source_roots: Sequence[RelPath] | None = None,
) -> Callable[[ImportFact], RelPath | None]:
    """``imported_name_map`` に渡す ``resolve`` を作る（1 つのファイルの import を解決する）。"""

    def resolve(fact: ImportFact) -> RelPath | None:
        return resolve_module(
            fact.module,
            from_rel=from_rel,
            language_id=language_id,
            is_relative=fact.is_relative,
            level=fact.level,
            source_roots=source_roots,
            exists=exists,
        )

    return resolve


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
