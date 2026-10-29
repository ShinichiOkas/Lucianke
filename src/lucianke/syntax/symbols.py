"""構文木 → ``SymbolRecord``（設計書 §7.4.3）。

``queries/<言語>/tags.scm`` の捕捉（``@definition.<種別>`` と ``@name``）を ``QueryCursor.matches()`` で読み、
定義ノードごとに次を決める（S-2: ``captures()`` では ``@name`` と ``@definition.*`` の対応が失われるので ``matches()``）。

* 種別: タグ + 囲みの定義（クラスの中の関数は Method、``__init__`` / ``constructor`` は Constructor、
  全大文字の名前は Constant、関数の中の変数は捨てる、など）。
* 修飾名: 囲みの定義の修飾名 + ``.`` + 名前。モジュールパスは入れない（URN の path が持つ。仕様書 §7.2）。
* range: 定義ノード。Python では ``decorated_definition``、TS / JS では ``export_statement`` /
  ``ambient_declaration`` / 単一宣言の ``lexical_declaration`` まで広げる。Doc コメントやデコレータ（TS のクラスメンバー）を
  含める境界規則は ``boundaries.py`` の仕事で、ここでは扱わない。位置は Tree-sitter のバイトオフセットから
  ``LineIndex.offset_to_position`` で作る（行の数え方・BOM・非 ASCII の列はそちらに任せる。ADR-14）。
* URN: ``lk://<ws>/<rel>#<qualified>``。同じファイルで修飾名が衝突したときだけ ``/<arity>`` → ``~<sighash>`` を足す。
  シグネチャまで同一の再定義は **最後** が素の URN、それ以前は ``~sighash(正規化シグネチャ + 開始行)``（仕様書 §7.2）。

``SymbolRecord`` には ``body_sha256`` の欄が無いので、``extract_detailed()`` が ``ExtractedSymbol``（レコード + ``body_sha256``）と
警告を返し、``extract()`` はその薄い包み（レコードだけ）。
"""

from __future__ import annotations

import ast
import functools
import hashlib
import inspect
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources

from tree_sitter import Language, Node, Query, QueryCursor

from lucianke.core.model import Confidence, Kind, Range, RelPath, SymbolRecord
from lucianke.core.urn import CodeSel, Urn, normalize_signature, sighash
from lucianke.syntax.types import ParsedFile

__all__ = ["ExtractedSymbol", "Extraction", "extract", "extract_detailed", "load_query_source"]

DOC_SUMMARY_MAX = 200

_PYTHON = "python"
_JS = "js"  # typescript / tsx / javascript は同じ処理（TS 固有のノードは JS の文法には現れない）
_FAMILIES = {
    "python": _PYTHON,
    "typescript": _JS,
    "tsx": _JS,
    "javascript": _JS,
    "jsx": _JS,
}
_QUERY_DIRS = {
    "python": "python",
    "typescript": "typescript",
    "tsx": "typescript",
    "javascript": "javascript",
    "jsx": "javascript",
}

# 変数・メソッドの「囲み」を越える関数ノード（定義として拾われないもの = 無名関数・オブジェクトリテラルのメソッド）
_FUNCTION_NODES = {
    _PYTHON: frozenset({"function_definition", "lambda"}),
    _JS: frozenset(
        {
            "function_declaration",
            "function_expression",
            "function",
            "generator_function",
            "generator_function_declaration",
            "arrow_function",
            "method_definition",
        }
    ),
}
_JS_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})
_JS_DECLARATIONS = frozenset({"lexical_declaration", "variable_declaration"})
_CALLABLE_KINDS = frozenset({Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR, Kind.PROPERTY})
_MEMBER_PARENTS = frozenset({Kind.CLASS, Kind.INTERFACE})
_PY_PROPERTY_DECORATORS = frozenset({"property", "cached_property", "abstractproperty"})
_PY_ACCESSOR_DECORATOR = re.compile(r"^[\w.]+\.(setter|deleter|getter)$")
_JSDOC_PREFIX = re.compile(r"^\s*\*+\s?")


# --- 公開型 -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractedSymbol:
    """抽出した 1 件。``body_sha256`` は本体ノード（無ければ定義ノード）のバイト列の SHA-256（設計書 §7.4.3 項目 7）。"""

    record: SymbolRecord
    body_sha256: str


@dataclass(frozen=True, slots=True)
class Extraction:
    """ファイル 1 つぶんの抽出結果。``symbols`` は文書順。``warnings`` は同名の再定義など（設計書 §7.4.3 項目 8）。"""

    symbols: tuple[ExtractedSymbol, ...]
    warnings: tuple[str, ...] = ()


def load_query_source(language_id: str) -> str:
    """``queries/<言語>/tags.scm`` の原文。``tsx`` は typescript、``jsx`` は javascript のものを使う。"""
    try:
        directory = _QUERY_DIRS[language_id]
    except KeyError:
        raise ValueError(f"no tags query for language {language_id!r}") from None
    path = resources.files("lucianke.syntax") / "queries" / directory / "tags.scm"
    return path.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=32)
def _compile(language: Language, query_source: str) -> Query:
    return Query(language, query_source)


def extract(
    parsed: ParsedFile,
    *,
    language_id: str,
    language: Language,
    query_source: str,
    workspace_id: str,
    rel: RelPath,
) -> list[SymbolRecord]:
    """``extract_detailed`` のレコードだけ版。"""
    result = extract_detailed(
        parsed,
        language_id=language_id,
        language=language,
        query_source=query_source,
        workspace_id=workspace_id,
        rel=rel,
    )
    return [s.record for s in result.symbols]


# --- 抽出 -------------------------------------------------------------------------------


@dataclass(slots=True)
class _Def:
    node: Node  # @definition.* の捕捉（タグの定義ノード）
    name_node: Node
    name: str
    kind: Kind
    parent: _Def | None
    qualified: str
    outer: Node  # range に使うノード（デコレータ・export などを含む）
    modifiers: tuple[str, ...]
    signature: str | None
    return_type: str | None
    arity: int | None
    doc_summary: str | None
    body_sha256: str
    accessor: str | None = None  # TS の get / set
    selector: CodeSel | None = None
    index: int = field(default=0)


def extract_detailed(
    parsed: ParsedFile,
    *,
    language_id: str,
    language: Language,
    query_source: str,
    workspace_id: str,
    rel: RelPath,
) -> Extraction:
    """タグクエリで定義を拾い、``SymbolRecord`` と ``body_sha256``・警告を返す。構文エラーがあっても拾えた分を返す。"""
    family = _FAMILIES.get(language_id)
    if family is None:
        raise ValueError(f"symbol extraction is not supported for language {language_id!r}")
    ctx = _Ctx(parsed, family)
    root = parsed.tree.root_node

    found: dict[int, tuple[Node, Node, str]] = {}
    for _pattern, caps in QueryCursor(_compile(language, query_source)).matches(root):
        names = caps.get("name")
        if not names:
            continue
        for cap, nodes in caps.items():
            if cap.startswith("definition."):
                found.setdefault(nodes[0].id, (nodes[0], names[0], cap[len("definition.") :]))
    ordered = sorted(found.values(), key=lambda t: (t[0].start_byte, -t[0].end_byte))

    kept: dict[int, _Def] = {}
    defs: list[_Def] = []
    for node, name_node, tag in ordered:
        parent, crossed = ctx.enclosing(node, kept)
        d = ctx.build(node, name_node, tag, parent, crossed)
        if d is None:
            continue
        kept[node.id] = d
        defs.append(d)

    # TS: 同名の get があるときの set は別シンボルにしない（Python の @x.setter と同じ扱い）
    getters = {d.qualified for d in defs if d.accessor == "get"}
    defs = [d for d in defs if not (d.accessor == "set" and d.qualified in getters)]
    for i, d in enumerate(defs):
        d.index = i

    warnings = _assign_selectors(defs, ctx, rel)
    declared_in = "stub" if str(rel).endswith(".pyi") else "source"
    out: list[ExtractedSymbol] = []
    for d in defs:
        assert d.selector is not None
        record = SymbolRecord(
            urn=Urn(workspace_id, str(rel), d.selector),
            path=rel,
            name=d.name,
            qualified_name=d.qualified,
            kind=d.kind,
            modifiers=d.modifiers,
            range=ctx.range_of(d.outer),
            selection=ctx.range_of(d.name_node),
            signature=d.signature,
            return_type=d.return_type,
            doc_summary=d.doc_summary,
            declared_in=declared_in,
            confidence=Confidence.STRUCTURAL,
        )
        out.append(ExtractedSymbol(record, d.body_sha256))
    return Extraction(tuple(out), tuple(warnings))


# --- 文脈 -------------------------------------------------------------------------------


class _Ctx:
    """1 ファイルぶんの抽出の作業場（原文・行表・言語系統）。"""

    def __init__(self, parsed: ParsedFile, family: str) -> None:
        self.parsed = parsed
        self.src = parsed.source
        self.family = family
        self.line_index = parsed.line_index
        self.function_nodes = _FUNCTION_NODES[family]
        root = parsed.tree.root_node
        # JS / TS: import / export があれば ES モジュール（トップレベルの非 export は外から見えない = priv）
        self.is_module = family == _JS and any(
            c.type in ("import_statement", "export_statement") for c in root.children
        )

    # --- 基本 ---

    def text(self, node: Node) -> str:
        return self.src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def range_of(self, node: Node) -> Range:
        return Range(
            self.line_index.offset_to_position(node.start_byte).value,
            self.line_index.offset_to_position(node.end_byte).value,
        )

    def squash(self, node: Node) -> str:
        return _tidy(" ".join(self.text(node).split()))

    def enclosing(self, node: Node, kept: dict[int, _Def]) -> tuple[_Def | None, bool]:
        """囲む定義と、そこへ至る途中で関数ノード（定義でないもの）を越えたか。"""
        crossed = False
        p = node.parent
        while p is not None:
            d = kept.get(p.id)
            if d is not None:
                return d, crossed
            if p.type in self.function_nodes:
                crossed = True
            p = p.parent
        return None, crossed

    # --- 定義の組み立て ---

    def build(self, node: Node, name_node: Node, tag: str, parent: _Def | None, crossed: bool) -> _Def | None:
        name_full = self.text(name_node)
        name = name_full.rsplit(".", 1)[-1]
        if not name_full:
            return None
        if self.family == _PYTHON:
            info = self._classify_python(node, tag, name, parent, crossed)
        else:
            info = self._classify_js(node, tag, name, parent, crossed)
        if info is None:
            return None
        kind, accessor, decorators = info
        outer, exported = self._outer(node)
        sig_node = self._signature_node(node, kind)
        signature, return_type, arity = self._signature(sig_node)
        qualified = f"{parent.qualified}.{name_full}" if parent is not None else name_full
        return _Def(
            node=node,
            name_node=name_node,
            name=name,
            kind=kind,
            parent=parent,
            qualified=qualified,
            outer=outer,
            modifiers=self._modifiers(node, name, parent, crossed, exported, decorators, sig_node),
            signature=signature,
            return_type=return_type,
            arity=arity,
            doc_summary=self._doc(node, outer),
            body_sha256=self._body_sha(node),
            accessor=accessor,
        )

    # --- 種別 ---

    def _classify_python(
        self, node: Node, tag: str, name: str, parent: _Def | None, crossed: bool
    ) -> tuple[Kind, str | None, list[str]] | None:
        pk = parent.kind if parent is not None else None
        decorators = _py_decorators(self, node)
        if tag == "class":
            return Kind.CLASS, None, decorators
        if tag == "type":
            return Kind.TYPE, None, decorators
        if tag == "function":
            if any(_PY_ACCESSOR_DECORATOR.match(d) for d in decorators):
                return None  # @x.setter / @x.deleter は property の一部
            if pk is Kind.CLASS and not crossed:
                last = {d.rsplit(".", 1)[-1] for d in decorators}
                if name == "__init__":
                    return Kind.CONSTRUCTOR, None, decorators
                if last & _PY_PROPERTY_DECORATORS:
                    return Kind.PROPERTY, None, decorators
                return Kind.METHOD, None, decorators
            return Kind.FUNCTION, None, decorators
        if tag == "variable":
            if crossed or pk in _CALLABLE_KINDS:
                return None  # ローカル変数
            if name.isupper():
                return Kind.CONSTANT, None, decorators
            return (Kind.FIELD if pk is Kind.CLASS else Kind.VARIABLE), None, decorators
        return None

    def _classify_js(
        self, node: Node, tag: str, name: str, parent: _Def | None, crossed: bool
    ) -> tuple[Kind, str | None, list[str]] | None:
        pk = parent.kind if parent is not None else None
        fixed = {
            "class": Kind.CLASS,
            "interface": Kind.INTERFACE,
            "type": Kind.TYPE,
            "enum": Kind.ENUM,
            "module": Kind.MODULE,
            "function": Kind.FUNCTION,
        }
        if tag in fixed:
            return fixed[tag], None, []
        container = node.parent.type if node.parent is not None else ""
        if tag == "method":
            if container not in ("class_body", "interface_body") or pk not in _MEMBER_PARENTS:
                return None  # オブジェクトリテラルのメソッド・型リテラルのメンバー
            if name == "constructor" and pk is Kind.CLASS:
                return Kind.CONSTRUCTOR, None, []
            keywords = {c.type for c in node.children}
            if node.type == "method_definition" and keywords & {"get", "set"}:
                return Kind.PROPERTY, ("get" if "get" in keywords else "set"), []
            return Kind.METHOD, None, []
        if tag == "field":
            if container != "class_body" or pk is not Kind.CLASS:
                return None
            value = node.child_by_field_name("value")
            if value is not None and value.type in _JS_FUNCTION_VALUES:
                return Kind.METHOD, None, []
            return (Kind.CONSTANT if name.isupper() else Kind.FIELD), None, []
        if tag == "property":
            if container != "interface_body" or pk is not Kind.INTERFACE:
                return None
            return Kind.PROPERTY, None, []
        if tag == "variable":
            if crossed or pk in _CALLABLE_KINDS or not _is_js_declaration_site(node):
                return None
            value = node.child_by_field_name("value")
            if value is not None and value.type in _JS_FUNCTION_VALUES:
                return Kind.FUNCTION, None, []
            if value is not None and value.type == "class":
                return Kind.CLASS, None, []
            return (Kind.CONSTANT if name.isupper() else Kind.VARIABLE), None, []
        return None

    # --- 範囲・export ---

    def _outer(self, node: Node) -> tuple[Node, bool]:
        """range に使うノードと、export されているか。"""
        cur = node
        if self.family == _PYTHON:
            p = cur.parent
            if p is not None and p.type == "decorated_definition":
                cur = p
            return cur, False
        exported = False
        if cur.type == "variable_declarator":
            p = cur.parent
            if p is not None and p.type in _JS_DECLARATIONS:
                single = sum(1 for c in p.named_children if c.type == "variable_declarator") == 1
                if single:
                    cur = p
                # 複数宣言（const a = 1, b = 2）は range を宣言子にとどめるが、export の有無は宣言から見る
                probe: Node | None = p
                while (
                    probe is not None
                    and probe.parent is not None
                    and probe.parent.type
                    in (
                        "ambient_declaration",
                        "export_statement",
                    )
                ):
                    exported = exported or probe.parent.type == "export_statement"
                    probe = probe.parent
        if cur.type in ("internal_module", "module"):
            p = cur.parent
            if p is not None and p.type == "expression_statement" and p.named_child_count == 1:
                cur = p
        while cur.parent is not None and cur.parent.type in ("ambient_declaration", "export_statement"):
            exported = exported or cur.parent.type == "export_statement"
            cur = cur.parent
        return cur, exported

    # --- シグネチャ ---

    def _signature_node(self, node: Node, kind: Kind) -> Node | None:
        """シグネチャを持つノード（関数・メソッド。関数を束縛する変数は値の関数式）。"""
        if self.family == _PYTHON:
            return node if node.type == "function_definition" else None
        if (
            node.type == "variable_declarator"
            or node.type == "public_field_definition"
            or node.type == "field_definition"
        ):
            value = node.child_by_field_name("value")
            if value is not None and value.type in _JS_FUNCTION_VALUES:
                return value
            return None
        if kind in (Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR, Kind.PROPERTY):
            return node
        return None

    def _signature(self, fn: Node | None) -> tuple[str | None, str | None, int | None]:
        """(signature, return_type, arity)。引数リストと戻り値注釈の原文（空白は 1 つに畳む）。"""
        if fn is None:
            return None, None, None
        params = fn.child_by_field_name("parameters")
        if params is None:
            single = fn.child_by_field_name("parameter")  # x => x
            params_text = f"({self.squash(single)})" if single is not None else None
            arity = 1 if single is not None else None
        else:
            params_text = self.squash(params)
            arity = sum(
                1
                for c in params.named_children
                if c.type not in ("comment", "positional_separator", "keyword_separator")
            )
        if params_text is None:
            return None, None, None
        ret = fn.child_by_field_name("return_type")
        if self.family == _PYTHON:
            ret_text = self.squash(ret) if ret is not None else None
            sig = f"{params_text} -> {ret_text}" if ret_text else params_text
            return sig, ret_text, arity
        tparams = fn.child_by_field_name("type_parameters")
        head = self.squash(tparams) if tparams is not None else ""
        ret_text = None
        if ret is not None:
            ret_text = self.squash(ret).lstrip(":").strip() or None
        sig = f"{head}{params_text}" + (f": {ret_text}" if ret_text else "")
        return sig, ret_text, arity

    # --- 修飾子 ---

    def _modifiers(
        self,
        node: Node,
        name: str,
        parent: _Def | None,
        crossed: bool,
        exported: bool,
        decorators: list[str],
        sig_node: Node | None,
    ) -> tuple[str, ...]:
        mods: list[str] = []
        if self.family == _PYTHON:
            private = name.startswith("_") and not (name.startswith("__") and name.endswith("__"))
            mods.append("priv" if private else "pub")
            last = {d.rsplit(".", 1)[-1] for d in decorators}
            if "staticmethod" in last:
                mods.append("static")
            if "abstractmethod" in last or "abstractproperty" in last:
                mods.append("abstract")
            if any(c.type == "async" for c in node.children):
                mods.append("async")
            return tuple(mods)

        keywords = {c.type for c in node.children}
        accessibility = next((self.text(c) for c in node.children if c.type == "accessibility_modifier"), "")
        pk = parent.kind if parent is not None else None
        if pk in _MEMBER_PARENTS:
            if name.startswith("#") or accessibility == "private":
                mods.append("priv")
            elif accessibility == "protected":
                mods.append("prot")
            else:
                mods.append("pub")
        elif (parent is None or pk is Kind.MODULE) and not crossed:
            if exported:
                mods.append("pub")
            else:
                mods.append("priv" if (self.is_module or parent is not None) else "pub")
        else:
            mods.append("priv")  # 関数の中（無名関数のコールバックを含む）の定義
        if "static" in keywords:
            mods.append("static")
        if "abstract" in keywords or node.type in ("abstract_class_declaration", "abstract_method_signature"):
            mods.append("abstract")
        async_holder = sig_node if sig_node is not None else node
        if any(c.type == "async" for c in async_holder.children):
            mods.append("async")
        if exported:
            mods.append("export")
        return tuple(mods)

    # --- doc ---

    def _doc(self, node: Node, outer: Node) -> str | None:
        if self.family == _PYTHON:
            return self._py_docstring(node)
        return self._jsdoc(outer)

    def _py_docstring(self, node: Node) -> str | None:
        if node.type not in ("function_definition", "class_definition"):
            return None
        body = node.child_by_field_name("body")
        if body is None or body.type != "block":
            return None
        first = next((c for c in body.named_children if c.type != "comment"), None)
        if first is None or first.type != "expression_statement" or first.named_child_count != 1:
            return None
        literal = first.named_children[0]
        if literal.type != "string":
            return None
        try:
            value = ast.literal_eval(self.text(literal))
        except (ValueError, SyntaxError, MemoryError):
            return None
        if not isinstance(value, str):
            return None
        return _first_line(inspect.cleandoc(value))

    def _jsdoc(self, outer: Node) -> str | None:
        prev = outer.prev_sibling
        while prev is not None and prev.type == "decorator":
            prev = prev.prev_sibling
        if prev is None or prev.type != "comment":
            return None
        text = self.text(prev)
        if not text.startswith("/**") or text.startswith("/**/"):
            return None
        # 定義との間に空行を挟まないものだけを Doc コメントとみなす
        first_row = min([outer.start_point[0]] + [d.start_point[0] for d in _leading_decorators(outer)])
        if prev.end_point[0] + 1 < first_row:
            return None
        body = text[3:]
        if body.endswith("*/"):
            body = body[:-2]
        for line in body.splitlines():
            stripped = _JSDOC_PREFIX.sub("", line).strip()
            if stripped and not stripped.startswith("@"):
                return _truncate(stripped)
        return None

    # --- body ---

    def _body_sha(self, node: Node) -> str:
        body = node.child_by_field_name("body")
        if body is None:
            for name in ("value", "right"):
                body = node.child_by_field_name(name)
                if body is not None:
                    break
        if body is None and node.type == "expression_statement" and node.named_child_count:
            assign = node.named_children[0]
            body = assign.child_by_field_name("right")
        if body is None and node.type == "variable_declarator":
            body = node.child_by_field_name("value")
        target = body if body is not None else node
        return hashlib.sha256(self.src[target.start_byte : target.end_byte]).hexdigest()


# --- ノード・テキストの補助 ----------------------------------------------------------------


def _py_decorators(ctx: _Ctx, node: Node) -> list[str]:
    """デコレータの名前（``functools.cached_property`` のような点つきの名前。呼び出しの引数は除く）。"""
    p = node.parent
    if p is None or p.type != "decorated_definition":
        return []
    out: list[str] = []
    for c in p.children:
        if c.type != "decorator":
            continue
        expr = next((x for x in c.named_children if x.type != "comment"), None)
        if expr is None:
            continue
        if expr.type == "call":
            fn = expr.child_by_field_name("function")
            expr = fn if fn is not None else expr
        out.append(ctx.text(expr))
    return out


def _leading_decorators(outer: Node) -> list[Node]:
    """TS のクラスメンバーのデコレータは兄弟ノードとして直前に並ぶ。"""
    out: list[Node] = []
    prev = outer.prev_sibling
    while prev is not None and prev.type == "decorator":
        out.append(prev)
        prev = prev.prev_sibling
    return out


def _is_js_declaration_site(declarator: Node) -> bool:
    """トップレベルまたは namespace 直下の ``const`` / ``let`` / ``var`` か（ブロックや ``for`` の中を除く）。"""
    decl = declarator.parent
    if decl is None or decl.type not in _JS_DECLARATIONS:
        return False
    p = decl.parent
    while p is not None and p.type in ("export_statement", "ambient_declaration"):
        p = p.parent
    if p is None:
        return False
    if p.type == "program":
        return True
    return (
        p.type == "statement_block"
        and p.parent is not None
        and p.parent.type in ("internal_module", "module")
    )


_OPEN_SPACE = re.compile(r"([(\[{])\s+")
_CLOSE_SPACE = re.compile(r"\s+([)\]}])")
_TRAILING_COMMA = re.compile(r",\s*([)\]}])")


def _tidy(text: str) -> str:
    """畳んだシグネチャの括弧まわりの余白と末尾カンマを整える（複数行の引数リスト用）。"""
    text = _OPEN_SPACE.sub(r"\1", text)
    text = _CLOSE_SPACE.sub(r"\1", text)
    return _TRAILING_COMMA.sub(r"\1", text)


def _truncate(text: str) -> str:
    return text if len(text) <= DOC_SUMMARY_MAX else text[: DOC_SUMMARY_MAX - 1] + "…"


def _first_line(doc: str) -> str | None:
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return _truncate(stripped)
    return None


# --- URN の付与 ---------------------------------------------------------------------------


def _assign_selectors(defs: Iterable[_Def], ctx: _Ctx, rel: RelPath) -> list[str]:
    """衝突した修飾名にだけ ``/arity`` → ``~sighash`` を足す（仕様書 §7.2）。警告の列を返す。"""
    groups: dict[str, list[_Def]] = defaultdict(list)
    for d in defs:
        groups[d.qualified].append(d)
    warnings: list[str] = []
    for qualified, members in groups.items():
        if len(members) == 1:
            members[0].selector = CodeSel(qualified)
            continue
        by_arity: dict[int | None, list[_Def]] = defaultdict(list)
        for d in members:
            by_arity[d.arity].append(d)
        # 引数の数で区別がつくのは、アリティが 2 種類以上あるときだけ（1 種類なら素の URN を再定義の「最後」に残す）
        use_arity = len(by_arity) > 1
        for arity, same_arity in by_arity.items():
            base_arity = arity if use_arity else None
            if len(same_arity) == 1:
                same_arity[0].selector = CodeSel(qualified, base_arity)
                continue
            by_sig: dict[str, list[_Def]] = defaultdict(list)
            for d in same_arity:
                by_sig[normalize_signature(d.signature or "")].append(d)
            for sig, same_sig in by_sig.items():
                if len(same_sig) == 1:
                    same_sig[0].selector = CodeSel(qualified, base_arity, sighash(sig))
                    continue
                same_sig.sort(key=lambda d: d.node.start_byte)
                same_sig[-1].selector = CodeSel(qualified, base_arity)
                for d in same_sig[:-1]:
                    line = ctx.range_of(d.outer).start.line + 1
                    d.selector = CodeSel(qualified, base_arity, sighash(sig, start_line=line))
                    warnings.append(
                        f"{rel}:{line}: duplicate definition of {qualified} with an identical signature; "
                        f"the last definition keeps the plain URN, this one is #{_selector_text(d.selector)}"
                    )
    return warnings


def _selector_text(sel: CodeSel) -> str:
    out = sel.qualified
    if sel.arity is not None:
        out += f"/{sel.arity}"
    if sel.sighash is not None:
        out += f"~{sel.sighash}"
    return out
