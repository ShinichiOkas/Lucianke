"""構文木 → ``relations`` の ``structural`` 行の候補（設計書 §7.4.6、仕様書 §9.4・§8.3、T-06 処理規約 2・3、T-07 処理規約 2・4）。

``queries/<言語>/refs.scm`` の捕捉（``@call`` ``@write`` ``@rw`` ``@base`` ``@impl`` ``@type`` ``@read`` ``@import.*``）を
``QueryCursor.matches()`` で読み、参照箇所 1 つにつき ``RelationCandidate`` を 1 件作る。

* ``source_urn``: 参照箇所を含む最も内側の定義シンボル（Function / Method / Constructor / Class など。Variable・Constant・Field は
  囲みとして数えない）。モジュール直下ならファイルの URN。
* 種別（``relation_kind`` / ``use_kind``）: 呼び出し（``new`` を含む）は ``CALLS`` / ``call``、代入の左辺は ``REFERENCES`` / ``w``、複合代入・
  ``++`` は ``REFERENCES`` / ``rw``、型注釈・ジェネリクス引数・キャストは ``TYPE_USES`` / ``type``、import は ``IMPORTS`` / ``import``、
  基底クラスは ``INHERITS`` / ``impl``（TS の ``implements`` は ``IMPLEMENTS`` / ``impl``）、それ以外は ``REFERENCES`` / ``r``。
* 数えないもの: 定義の名前そのもの、キーワード引数の名前、パラメータ・ローカル変数・for/with/except の束縛（その名前の参照も含む）、
  import 文の中の識別子（``IMPORTS`` として別に出す）。位置ごとに 1 件。
* ``target_urn`` の解決（``structural``。型は推論せず、構文から読める範囲だけ）:

  1. 属性アクセス（``recv.name``）: レシーバの型（``self`` / ``this`` の囲みクラス、注釈つきパラメータ・変数、``x = Foo(...)`` /
     ``new Foo()`` で束ねた変数、``self.attr`` の型）が分かれば、そのクラスのメンバーを探す（同じファイルの定義、または
     ``resolve_name(型名)`` の URN + ``.name``）。レシーバがモジュール（``resolve_name`` が ``#`` なしの URN を返す名前）なら ``<URN>#name``。
     同じファイルのクラスに無ければ、同じファイルの基底クラスを C3 の順（``core/mro.py``）にたどる。基底が別ファイル（または
     この場で解決できない名前・``mod.Base`` の形）なら ``<このクラス>.name`` を返し、索引の整え（``index/inherited.py``）が
     ``INHERITS`` 行をファイルをまたいでたどって基底のメンバーに結ぶ（BUG-0075）。
  2. 同じファイルの定義の修飾名の末尾一致（一意のときだけ）。
  3. ``resolve_name(name)``（呼び手が import 表から作る。解決できない import は呼び手が ``lk://_ext/<pkg>#<name>`` にして返す）。
  4. 素の名前（と属性アクセスのレシーバに使う名前）だけ: ``builtins_package`` が渡されていて、言語の組み込み名
     （Python の ``builtins``、TS / JS の標準グローバル。``syntax/builtin_names.py``）なら ``lk://_ext/<builtins_package>#<name>``。
  5. 解決できなければ ``None``（``target_name`` だけ残す。取り込み側は ``relations`` に入れない）。ただし **受け手の型が分からない
     属性呼び出し**（``x.name(...)`` で ``x`` の型が構文から読めない。注釈が ``Any`` のもの、``self.x`` の型が不明なもの、呼び出しの
     戻り値などを含む）は ``name_only=True`` にする。取り込み側は内部表 ``_name_calls`` に位置と名前を残し、T-06 / T-07 が問い合わせの
     ときに名前で対象に結ぶ（BUG-0071。``tools/_name_calls.py``）。受け手の型が分かっていてメンバーが無い（同じファイルのクラスに
     その名前が無い）ものは ``name_only`` にしない（別のクラスの同名に結ばない）。

  外部 URN は属性アクセスの連鎖にも伸びる（``import os`` の ``os.path.join(...)`` → ``lk://_ext/os#path.join``、
  ``from decimal import Decimal`` の ``Decimal(x).quantize()`` → ``lk://_ext/decimal#Decimal.quantize``）。レシーバの型が分からない
  ``obj.name`` の最後の手段（``resolve_name(name)``）では外部 URN を使わない（``config.load()`` を ``from json import load`` に
  結ばないため）。

  素の識別子は、囲みの定義を内側から外へたどった修飾名（``outer.name``）→ トップレベルの定義 → ``resolve_name`` の順。
  ローカルに束縛された名前は参照に数えない。ただし ``def`` / ``class`` の中の ``import`` が束縛した名前は、そのスコープ
  （内側の関数とクラス直下を含む）の使用を、その import の先への参照にする（``resolve_import``。モジュール直下の同名の
  import や、別の関数の同名の import は巻き込まない）。
* ``guard_condition`` は ``CALLS`` のとき ``guards.guard_for`` の結果。``confidence`` は ``structural``。

``syntax`` は ``index`` を import できないので、``index.ingest.RelationFact`` と同じ形の ``RelationCandidate`` をここで持つ。
位置は 0 始まりの行・0 始まりのコードポイント列（ADR-14）。
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from tree_sitter import Language, Node, Query, QueryCursor

from lucianke.core.model import Confidence, Kind, Position, RelPath, SymbolRecord
from lucianke.core.mro import TRANSPARENT_EXTERNAL_BASES, c3_linearize
from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, SCHEME, CodeSel, Urn, parse
from lucianke.syntax.builtin_names import JS_GLOBALS, PYTHON_BUILTINS
from lucianke.syntax.guards import guard_for
from lucianke.syntax.imports import JS_FUNCTION_NODES, js_require_bindings
from lucianke.syntax.types import ParsedFile

__all__ = ["ImportKey", "RelationCandidate", "extract_relations", "load_refs_source"]

# import 文の開始位置（0 始まりの行, コードポイント列）。``ImportFact.range.start`` と同じ
ImportKey = tuple[int, int]

_EXTERNAL_PREFIX = f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"
_PYTHON = "python"
_JS = "js"  # typescript / tsx / javascript / jsx は同じ処理（TS 固有のノードは JS の文法には現れない）
_FAMILIES = {"python": _PYTHON, "typescript": _JS, "tsx": _JS, "javascript": _JS, "jsx": _JS}
_QUERY_DIRS = {
    "python": "python",
    "typescript": "typescript",
    "tsx": "typescript",
    "javascript": "javascript",
    "jsx": "javascript",
}

# 同じ識別子が複数の種別で捕捉されたときの優先（大きい方が勝つ）
_PRIORITY = {
    "base": 7,
    "impl": 7,
    "type": 6,
    "call": 5,
    "rw": 4,
    "write": 3,
    "read.attr": 2,
    "read": 1,
}
_KIND_OF = {
    "call": ("CALLS", "call"),
    "write": ("REFERENCES", "w"),
    "rw": ("REFERENCES", "rw"),
    "read": ("REFERENCES", "r"),
    "read.attr": ("REFERENCES", "r"),
    "type": ("TYPE_USES", "type"),
    "base": ("INHERITS", "impl"),
    "impl": ("IMPLEMENTS", "impl"),
}

# 囲みとして数えないシンボル種別（その中の参照は外側の定義かファイルに属する）
_NOT_CONTAINERS = frozenset({Kind.VARIABLE, Kind.CONSTANT, Kind.FIELD})
_CLASS_KINDS = frozenset({Kind.CLASS, Kind.INTERFACE, Kind.STRUCT, Kind.TRAIT, Kind.ENUM})
_RECEIVER_SELF = frozenset({"self", "cls", "this"})
_ANNOTATION_SKIP = frozenset(
    {
        "Optional",
        "Union",
        "Final",
        "ClassVar",
        "Annotated",
        "Required",
        "NotRequired",
        "Literal",
        "None",
        "Self",
        # ``x: Any`` は型が分からないのと同じ（``lk://_ext/typing#Any.name`` という実在しない外部メソッドに結ばない。BUG-0071）
        "Any",
    }
)

_PY_FUNCTIONS = frozenset({"function_definition", "lambda"})
# ``imports.extract_imports`` が関数の中の ``require`` を ``nested`` にする基準と同じ集合
_JS_FUNCTIONS = JS_FUNCTION_NODES
_PY_CLASSES = frozenset({"class_definition"})
_JS_CLASSES = frozenset({"class_declaration", "abstract_class_declaration", "class"})
_PY_SKIP_ANCESTORS = frozenset(
    {
        "import_statement",
        "import_from_statement",
        "future_import_statement",
        "global_statement",
        "nonlocal_statement",
    }
)
_JS_SKIP_ANCESTORS = frozenset({"import_statement", "import_require_clause", "import_alias"})
_CALL_NODES = frozenset({"call", "call_expression", "new_expression"})
_ATTRIBUTE_NODES = frozenset({"attribute", "member_expression"})


# --- 公開型 -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RelationCandidate:
    """``index.ingest.RelationFact`` と同じ形の候補。URN は正準形の文字列、位置は 0 始まり。

    ``target_urn`` が ``None`` のときは解決できなかった（``target_name`` は参照された名前そのまま）。
    ``name_only`` は、``target_urn`` が ``None`` の属性呼び出しのうち、受け手の型が分からないもの（名前でしか結べない。BUG-0071）。
    """

    source_urn: str
    target_name: str
    target_urn: str | None
    relation_kind: str
    call_site_line: int
    call_site_col: int
    use_kind: str
    guard_condition: str | None = None
    confidence: Confidence = field(default=Confidence.STRUCTURAL)
    name_only: bool = False


def load_refs_source(language_id: str) -> str:
    """``queries/<言語>/refs.scm`` の原文。``tsx`` は typescript、``jsx`` は javascript のものを使う。"""
    try:
        directory = _QUERY_DIRS[language_id]
    except KeyError:
        raise ValueError(f"no refs query for language {language_id!r}") from None
    path = resources.files("lucianke.syntax") / "queries" / directory / "refs.scm"
    return path.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=32)
def _compile(language: Language, language_id: str) -> Query:
    return Query(language, load_refs_source(language_id))


# --- 公開関数 ---------------------------------------------------------------------------


def extract_relations(
    parsed: ParsedFile,
    *,
    language_id: str,
    language: Language,
    symbols: list[SymbolRecord],
    workspace_id: str,
    rel: RelPath,
    resolve_name: Callable[[str], str | None] | None = None,
    builtins_package: str | None = None,
    resolve_import: Callable[[ImportKey, str], str | None] | None = None,
) -> list[RelationCandidate]:
    """構文木から ``structural`` の関係の候補を作る。文書順（行、列）。構文エラーがあっても拾えた分を返す。

    ``symbols`` はこのファイルの ``symbols.extract`` の結果。``resolve_name`` は import 表から作った「名前 → URN」
    （``imports.imported_name_map`` と ``resolve_module`` から、定義元ファイルの URN に直したもの。解決できない import は外部 URN）。
    ``builtins_package`` は言語の組み込み名の外部パッケージ名（Python は ``"builtins"``、TS / JS は ``"globals"``）。``None`` なら
    組み込み名を解決しない。``resolve_import`` は ``def`` / ``class`` の中の import（``ImportFact.nested``）が束縛した名前の解決
    （``(import 文の開始位置, 名前) → URN``）。``None`` なら ``resolve_name`` で引く（名前表がスコープを区別しないときの代用）。
    """
    family = _FAMILIES.get(language_id)
    if family is None:
        raise ValueError(f"relation extraction is not supported for language {language_id!r}")
    return _Extractor(
        parsed,
        language_id,
        family,
        language,
        symbols,
        workspace_id,
        rel,
        resolve_name,
        builtins_package,
        resolve_import,
    ).run()


# --- 内部 -------------------------------------------------------------------------------


@dataclass(slots=True)
class _Ref:
    node: Node
    category: str
    recv: Node | None = None


@dataclass(slots=True)
class _Import:
    node: Node
    alias: str | None
    module: bool = False  # ``import a.b.c`` の形（名前でなくモジュール）


@dataclass(slots=True)
class _Scan:
    """構文木 1 回の走査で集める、束縛と型の表。"""

    binders: set[int] = field(default_factory=set)  # 束縛位置の名前ノード（参照に数えない）
    locals: dict[int, set[str]] = field(default_factory=dict)  # 関数ノード → その関数で束縛した名前
    globals: dict[int, set[str]] = field(default_factory=dict)
    var_types: dict[int, dict[str, str]] = field(default_factory=dict)  # スコープノード → 変数名 → 型名
    attr_types: dict[int, dict[str, str]] = field(default_factory=dict)  # クラスノード → 属性名 → 型名
    # 関数・クラスノード → import で束縛した名前 → 文の位置
    imports: dict[int, dict[str, ImportKey]] = field(default_factory=dict)
    nested_imports: set[ImportKey] = field(default_factory=set)  # def / class の中の import 文の位置


class _Extractor:
    def __init__(
        self,
        parsed: ParsedFile,
        language_id: str,
        family: str,
        language: Language,
        symbols: list[SymbolRecord],
        workspace_id: str,
        rel: RelPath,
        resolve_name: Callable[[str], str | None] | None,
        builtins_package: str | None = None,
        resolve_import: Callable[[ImportKey, str], str | None] | None = None,
    ) -> None:
        self.parsed = parsed
        self.language_id = language_id
        self.family = family
        self.language = language
        self.workspace_id = workspace_id
        self.rel = rel
        self.file_urn = str(Urn(workspace_id, str(rel)))
        self._resolve_raw = resolve_name
        self._resolve_import_raw = resolve_import
        self._import_cache: dict[tuple[ImportKey, str], str | None] = {}
        self._builtins_package = builtins_package
        self._builtin_names = PYTHON_BUILTINS if family == _PYTHON else JS_GLOBALS
        self._resolve_cache: dict[str, str | None] = {}
        self.functions = _PY_FUNCTIONS if family == _PYTHON else _JS_FUNCTIONS
        self.classes = _PY_CLASSES if family == _PYTHON else _JS_CLASSES
        self.skip_ancestors = _PY_SKIP_ANCESTORS if family == _PYTHON else _JS_SKIP_ANCESTORS

        self.by_qualified: dict[str, SymbolRecord] = {}
        self.by_selection: dict[tuple[int, int], SymbolRecord] = {}
        for s in symbols:
            self.by_qualified[s.qualified_name] = (
                s  # 同じ修飾名はあとの宣言が勝つ（オーバーロードの実装、再束縛）
            )
            if s.selection is not None:
                self.by_selection[(s.selection.start.line, s.selection.start.col)] = s
        self.symbols = sorted(symbols, key=lambda s: (s.range.start, _neg_pos(s.range.end)))
        self.scan = _Scan()
        self.class_bases: dict[str, list[str]] = {}  # クラスの修飾名 → 基底クラス名（同じファイルでの解決用）

    # --- 名前解決の窓口 ---

    def _text(self, node: Node) -> str:
        """ノードの綴り。``Node.text`` は使わない（read_callback で解析した木では読み取り関数を呼び直し、
        解放済みの領域を読みうる。BUG-0020・BUG-0051）。"""
        src = self.parsed.source
        return src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def resolve(self, name: str) -> str | None:
        if self._resolve_raw is None:
            return None
        if name not in self._resolve_cache:
            self._resolve_cache[name] = self._resolve_raw(name)
        return self._resolve_cache[name]

    def resolve_scoped(self, key: ImportKey, name: str) -> str | None:
        """``def`` / ``class`` の中の import 文（開始位置 ``key``）が束ねた ``name`` の URN。"""
        if self._resolve_import_raw is None:
            return self.resolve(name)
        ck = (key, name)
        if ck not in self._import_cache:
            self._import_cache[ck] = self._resolve_import_raw(key, name)
        return self._import_cache[ck]

    def resolve_builtin(self, name: str) -> str | None:
        """言語の組み込み名なら ``lk://_ext/<builtins_package>#<name>``。"""
        if self._builtins_package is None or name not in self._builtin_names:
            return None
        try:
            return str(Urn.external(self._builtins_package, None, name))
        except ValueError:
            return None

    # --- 位置 ---

    def pos(self, node: Node) -> Position:
        return self.parsed.line_index.offset_to_position(node.start_byte).value

    # --- 実行 ---

    def run(self) -> list[RelationCandidate]:
        root = self.parsed.tree.root_node
        self._scan_tree(root)
        refs, imports = self._collect(root)

        # 参照を位置順に並べ、囲みのシンボルを掃引で決める
        items: list[tuple[Position, str, Any]] = []
        for ref in refs.values():
            items.append((self.pos(ref.node), "ref", ref))
        for imp in imports:
            items.append((self.pos(imp.node), "import", imp))
        items.sort(key=lambda t: (t[0], t[1] != "ref"))

        out: list[RelationCandidate] = []
        stack: list[SymbolRecord] = []
        pending = iter(self.symbols)
        nxt = next(pending, None)
        for position, what, item in items:
            while nxt is not None and nxt.range.start <= position:
                stack.append(nxt)
                nxt = next(pending, None)
            while stack and not (position < stack[-1].range.end):
                stack.pop()
            chain = [s for s in stack if s.range.start <= position < s.range.end]
            if what == "ref":
                cand = self._ref_candidate(item, position, chain)
            else:
                cand = self._import_candidate(item, position, chain)
            if cand is not None:
                out.append(cand)
        return out

    # --- 候補の収集 ---

    def _collect(self, root: Node) -> tuple[dict[int, _Ref], list[_Import]]:
        refs: dict[int, _Ref] = {}
        imports: dict[int, _Import] = {}

        def put(node: Node, category: str, recv: Node | None) -> None:
            cur = refs.get(node.start_byte)
            if cur is None or _PRIORITY[category] > _PRIORITY[cur.category]:
                refs[node.start_byte] = _Ref(node, category, recv)
            elif _PRIORITY[category] == _PRIORITY[cur.category] and cur.recv is None and recv is not None:
                cur.recv = recv

        for _pattern, caps in QueryCursor(_compile(self.language, self.language_id)).matches(root):
            for key, nodes in caps.items():
                if key == "type.annotation":
                    for n in nodes:
                        for name_node, recv in self._annotation_names(n):
                            put(name_node, "type", recv)
                elif key in _PRIORITY:
                    recv_nodes = caps.get(f"{key.split('.')[0]}.recv")
                    for n in nodes:
                        put(n, key, recv_nodes[0] if recv_nodes else None)
                elif key.startswith("import.") and not key.endswith(".alias"):
                    alias = caps.get("import.alias")
                    for n in nodes:
                        cur = imports.get(n.start_byte)
                        text = self._text(alias[0]) if alias else None
                        if cur is None:
                            imports[n.start_byte] = _Import(n, text, module=key == "import.module")
                        elif cur.alias is None and text is not None:
                            cur.alias = text
        return refs, list(imports.values())

    def _annotation_names(self, node: Node) -> Iterator[tuple[Node, Node | None]]:
        """型注釈の中の型名（``a.B`` は ``B`` とレシーバ ``a``）。文字列（前方参照）は読まない。"""
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                yield n, None
            elif n.type == "attribute":
                attr = n.child_by_field_name("attribute")
                if attr is not None:
                    yield attr, n.child_by_field_name("object")
            elif n.type not in ("string", "comment"):
                stack.extend(reversed(n.children))

    # --- 参照 1 件 ---

    def _ref_candidate(
        self, ref: _Ref, position: Position, chain: list[SymbolRecord]
    ) -> RelationCandidate | None:
        node = ref.node
        if (position.line, position.col) in self.by_selection:
            return None  # 定義の名前そのもの
        if node.id in self.scan.binders:
            return None
        if self._in_skipped_context(node):
            return None
        name = self._text(node)
        if not name:
            return None
        container = self._container(chain)
        source = str(container.urn) if container is not None else self.file_urn

        is_member = self._is_member_name(node)
        receiver_known = False
        if is_member:
            target, receiver_known = self._resolve_member(ref.recv, name, node, chain)
        else:
            is_local, target = self._resolve_bare(name, node, chain)
            if is_local:
                return None
        relation_kind, use_kind = _KIND_OF[ref.category]
        guard = None
        if relation_kind == "CALLS":
            call_node = self._call_node(node)
            if call_node is not None:
                guard = guard_for(call_node, self.parsed, language_id=self.language_id)
        return RelationCandidate(
            source_urn=source,
            target_name=name,
            target_urn=target,
            relation_kind=relation_kind,
            call_site_line=position.line,
            call_site_col=position.col,
            use_kind=use_kind,
            guard_condition=guard,
            name_only=is_member and target is None and not receiver_known and relation_kind == "CALLS",
        )

    def _import_candidate(
        self, imp: _Import, position: Position, chain: list[SymbolRecord]
    ) -> RelationCandidate | None:
        node = imp.node
        name = self._text(node)
        if not name:
            return None
        target: str | None = None
        key = self._import_key(node)
        resolve = self.resolve if key is None else (lambda n: self.resolve_scoped(key, n))
        if imp.alias:
            target = resolve(imp.alias)
        if target is None:
            target = resolve(name)
        if target is None and imp.module and imp.alias is None:
            target = resolve(name.split(".")[0])
        container = self._container(chain)
        return RelationCandidate(
            source_urn=str(container.urn) if container is not None else self.file_urn,
            target_name=name,
            target_urn=target,
            relation_kind="IMPORTS",
            call_site_line=position.line,
            call_site_col=position.col,
            use_kind="import",
        )

    def _import_key(self, node: Node) -> ImportKey | None:
        """名前ノードを含む import 文が ``def`` / ``class`` の中にあれば、その開始位置。"""
        stmt: Node | None = node
        while stmt is not None and stmt.type not in ("import_statement", "import_from_statement"):
            stmt = stmt.parent
        if stmt is None:
            return None
        p = self.pos(stmt)
        return (p.line, p.col) if (p.line, p.col) in self.scan.nested_imports else None

    def _container(self, chain: list[SymbolRecord]) -> SymbolRecord | None:
        for s in reversed(chain):
            if s.kind not in _NOT_CONTAINERS:
                return s
        return None

    # --- 構文上の位置の判定 ---

    def _is_member_name(self, node: Node) -> bool:
        parent = node.parent
        if parent is None or parent.type not in _ATTRIBUTE_NODES:
            return False
        field_name = "attribute" if parent.type == "attribute" else "property"
        target = parent.child_by_field_name(field_name)
        return target is not None and target.id == node.id

    def _in_skipped_context(self, node: Node) -> bool:
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "keyword_argument":
            n = parent.child_by_field_name("name")
            if n is not None and n.id == node.id:
                return True
        if parent.type in ("function_definition", "class_definition"):
            n = parent.child_by_field_name("name")
            if n is not None and n.id == node.id:
                return True
        if parent.type == "type_parameter":  # TS ``<T>``
            return True
        anc: Node | None = parent
        while anc is not None:
            if anc.type in self.skip_ancestors:
                return True
            anc = anc.parent
        return False

    def _call_node(self, name_node: Node) -> Node | None:
        """名前ノードを関数位置（``new`` の constructor）に持つ呼び出しノード。"""
        node: Node | None = name_node
        while node is not None and node.parent is not None:
            parent = node.parent
            if parent.type in _CALL_NODES:
                fn = parent.child_by_field_name(
                    "constructor" if parent.type == "new_expression" else "function"
                )
                if fn is not None and fn.id == node.id:
                    return parent
                return None
            if parent.type not in _ATTRIBUTE_NODES:
                return None
            node = parent
        return None

    # --- 素の識別子の解決 ---

    def _binding(self, name: str, node: Node) -> tuple[str, ImportKey | None]:
        """``node`` から見た ``name`` の束縛: ``("local", None)``（パラメータ・代入など）、``("import", 文の位置)``
        （``def`` / ``class`` の中の import）、``("none", None)``（関数・クラスの中では束縛されていない）。

        内側のスコープから外へ、最初に見つかったものが勝つ（Python の名前解決と同じ）。クラス直下の束縛は、使用箇所がその
        クラス直下（間に関数が無い）のときだけ見える。``global`` で宣言された名前はモジュールの名前。
        """
        innermost = True
        anc = node.parent
        while anc is not None:
            if anc.type in self.functions:
                if name in self.scan.globals.get(anc.id, ()):
                    return "none", None
                key = self.scan.imports.get(anc.id, {}).get(name)
                if key is not None:
                    return "import", key
                if name in self.scan.locals.get(anc.id, ()):
                    return "local", None
                innermost = False
            elif anc.type in self.classes:
                if innermost:
                    key = self.scan.imports.get(anc.id, {}).get(name)
                    if key is not None:
                        return "import", key
                innermost = False
            anc = anc.parent
        return "none", None

    def _resolve_bare(self, name: str, node: Node, chain: list[SymbolRecord]) -> tuple[bool, str | None]:
        """``(ローカルか, URN)``。ローカルに束縛された名前は ``(True, None)``。"""
        # 囲みの定義を内側から外へ（クラスは、参照がそのクラスの直下にあるときだけ）
        for i in range(len(chain) - 1, -1, -1):
            s = chain[i]
            if s.kind in _CLASS_KINDS and i != len(chain) - 1:
                continue
            hit = self.by_qualified.get(f"{s.qualified_name}.{name}")
            if hit is not None:
                return False, str(hit.urn)
        kind, key = self._binding(name, node)
        if kind == "local":
            return True, None
        if kind == "import" and key is not None:  # def / class の中の import が束ねた名前: その import の先
            return False, self.resolve_scoped(key, name)
        top = self.by_qualified.get(name)
        if top is not None:
            return False, str(top.urn)
        return False, self.resolve(name) or self.resolve_builtin(name)

    def _is_local(self, name: str, node: Node) -> bool:
        return self._binding(name, node)[0] == "local"

    # --- 属性アクセスの解決 ---

    def _resolve_member(
        self, recv: Node | None, name: str, node: Node, chain: list[SymbolRecord]
    ) -> tuple[str | None, bool]:
        """``(URN, 受け手の型が分かったか)``。URN が ``None`` で型も分からないものが ``name_only`` の候補（BUG-0071）。"""
        if recv is not None:
            ref = self._type_ref(recv, node, chain)
            if ref is not None:
                hit = self._member_of(ref, name)
                if hit is not None or ref[0] == "local":
                    return hit, True  # 同じファイルのクラスと分かっているのに無いメンバーは、別のクラスの同名で埋めない
            elif self._is_self_attribute(recv):
                return None, False  # ``self.x.name``: x の型が分からないとき、同名の他の定義に当てると組み込み型のメソッドを誤認する
        suffix = f".{name}"
        container = chain[-1] if chain else None
        own = recv is not None and (recv.type == "this" or self._text(recv) in _RECEIVER_SELF)
        matches = [
            s
            for q, s in self.by_qualified.items()
            if q.endswith(suffix)
            and s.kind not in _NOT_MEMBER_TARGETS
            and (own or container is None or s.urn != container.urn)
        ]
        if len(matches) == 1:
            return str(matches[0].urn), False
        got = self.resolve(name)
        return (None if got is not None and got.startswith(_EXTERNAL_PREFIX) else got), False

    def _is_self_attribute(self, recv: Node) -> bool:
        if recv.type not in _ATTRIBUTE_NODES:
            return False
        obj = recv.child_by_field_name("object")
        return obj is not None and (obj.type == "this" or self._text(obj) in _RECEIVER_SELF)

    def _member_of(self, ref: tuple[str, str], name: str) -> str | None:
        """型 ``ref`` のメンバー ``name`` の URN。

        * ``("urn", 型)``（別ファイル・外部の型）: ``<型>.name``。実在するか、基底クラスから継承したものかは索引の整え
          （``index/inherited.py``）が決める（BUG-0075）。
        * ``("local", 修飾名)``（同じファイルのクラス）: クラス自身 → 同じファイルの基底クラスを C3 の順（``core/mro.py``）に探す。
          たどる途中に別ファイルの基底・この場で解決できない基底があれば、``<このクラス>.name`` を返して整えに任せる（索引の
          ``INHERITS`` 行をファイルをまたいでたどる）。中身の分からない外部の基底（``object``・``ABC`` などの素通りするもの以外）に
          先に当たったら ``None``（その基底が同名を定義しているかもしれないので、後ろの基底の同名に結ばない）。
        """
        kind, value = ref
        if kind != "local":
            return _append_member(value, name)
        hit = self.by_qualified.get(f"{value}.{name}")
        if hit is not None:
            return str(hit.urn)
        order = c3_linearize(("local", value), self._local_bases)
        if order is None:
            return None
        if any(k == "other" for k, _ in order):
            return self._requested_member(value, name)
        for k, v in order[1:]:
            if k == "ext":
                if v in TRANSPARENT_EXTERNAL_BASES:
                    continue
                return None
            hit = self.by_qualified.get(f"{v}.{name}")
            if hit is not None:
                return str(hit.urn)
        return None

    def _local_bases(self, entry: tuple[str, str]) -> list[tuple[str, str]]:
        """``_member_of`` の C3 に渡す直接の基底: ``("local", 修飾名)`` / ``("ext", 外部 URN)`` / ``("other", 綴り)``（別ファイルの
        クラス・この場で解決できない名前・``mod.Base`` の形。整えに任せる印）。外部と ``other`` は葉（基底を持たない）として扱う。"""
        kind, value = entry
        if kind != "local":
            return []
        out: list[tuple[str, str]] = []
        for base in self.class_bases.get(value, ()):
            ref = None if "." in base else self._name_ref(base, [])
            if ref is not None and ref[0] == "local":
                out.append(("local", ref[1]))
            elif ref is not None and ref[1].startswith(_EXTERNAL_PREFIX):
                out.append(("ext", ref[1]))
            else:
                out.append(("other", base))
        return out

    def _requested_member(self, class_qualified: str, name: str) -> str | None:
        """同じファイルのクラスのメンバーとして求めた URN（``<ファイル>#<クラス>.name``。シンボルが無ければ整えが基底をたどる）。"""
        try:
            return str(Urn(self.workspace_id, str(self.rel), CodeSel(f"{class_qualified}.{name}")))
        except ValueError:
            return None

    def _type_ref(self, recv: Node, node: Node, chain: list[SymbolRecord]) -> tuple[str, str] | None:
        """レシーバ式の型を ``("local", 修飾名)`` か ``("urn", URN)`` で。分からなければ ``None``。"""
        t = recv.type
        if t in ("identifier", "this"):
            text = self._text(recv)
            if text in _RECEIVER_SELF or t == "this":
                cls = self._class_node(node)
                if cls is None:
                    return None
                return self._class_ref(cls)
            tname = self._variable_type(text, node)
            if tname is not None:
                return self._name_ref(tname, chain, node)
            if self._is_local(text, node):
                return None  # 型の分からないローカル変数（同名の import を隠す）
            return self._name_ref(
                text, chain, node
            )  # クラス名・モジュール名そのもの（``Foo.create()``、``mod.f()``）
        if t in _ATTRIBUTE_NODES:
            obj = recv.child_by_field_name("object")
            prop = recv.child_by_field_name("attribute" if t == "attribute" else "property")
            if obj is not None and prop is not None and (obj.type == "this" or self._text(obj) in _RECEIVER_SELF):
                cls = self._class_node(node)
                if cls is not None:
                    tname = self.scan.attr_types.get(cls.id, {}).get(self._text(prop))
                    if tname is not None:
                        return self._name_ref(tname, chain, node)
            elif obj is not None and prop is not None and obj.type in ("identifier", *_ATTRIBUTE_NODES):
                # 外部モジュール・クラスの属性の連鎖（``os.path``、``Decimal.ROUND_HALF_UP``）
                inner = self._type_ref(obj, node, chain)
                if inner is not None and inner[0] == "urn" and inner[1].startswith(_EXTERNAL_PREFIX):
                    joined = _append_member(inner[1], self._text(prop))
                    return ("urn", joined) if joined is not None else None
            return None
        if t in ("call", "new_expression"):
            fn = recv.child_by_field_name("constructor" if t == "new_expression" else "function")
            if fn is not None and fn.type == "identifier":
                text = self._text(fn)
                if t == "new_expression" or self._looks_like_class(text):
                    return self._name_ref(text, chain, node)
        return None

    def _looks_like_class(self, name: str) -> bool:
        local = self.by_qualified.get(name)
        if local is not None:
            return local.kind in _CLASS_KINDS
        return name[:1].isupper()

    def _name_ref(
        self, name: str, chain: list[SymbolRecord], node: Node | None = None
    ) -> tuple[str, str] | None:
        """型名・モジュール名 ``name``（``node`` の位置から見える束縛）を ``("local", 修飾名)`` か ``("urn", URN)`` に。"""
        kind, key = self._binding(name, node) if node is not None else ("none", None)
        if kind == "import" and key is not None:
            urn = self.resolve_scoped(key, name)
            return ("urn", urn) if urn is not None else None
        local = self.by_qualified.get(name)
        if local is not None and local.kind in _CLASS_KINDS | {Kind.MODULE}:
            return ("local", local.qualified_name)
        urn = self.resolve(name) or self.resolve_builtin(name)
        if urn is not None:
            return ("urn", urn)
        return None

    def _class_node(self, node: Node) -> Node | None:
        anc = node.parent
        while anc is not None:
            if anc.type in self.classes:
                return anc
            anc = anc.parent
        return None

    def _class_ref(self, cls: Node) -> tuple[str, str] | None:
        name = cls.child_by_field_name("name")
        if name is None:
            return None
        sym = self.by_selection.get((self.pos(name).line, self.pos(name).col))
        return ("local", sym.qualified_name) if sym is not None else None

    def _variable_type(self, name: str, node: Node) -> str | None:
        anc: Node | None = node.parent
        while anc is not None:
            if anc.type in self.functions or anc.type in self.classes or anc.parent is None:
                got = self.scan.var_types.get(anc.id, {}).get(name)
                if got is not None:
                    return got
                if anc.type in self.functions and (
                    name in self.scan.locals.get(anc.id, ()) or name in self.scan.imports.get(anc.id, {})
                ):
                    return None  # 型の分からないローカル（import を含む）が外側の同名変数を隠す
            anc = anc.parent
        return None

    # --- 走査: 束縛と型 ---

    def _scan_tree(self, root: Node) -> None:
        if self.family == _PYTHON:
            self._scan_python(root)
        else:
            self._scan_js(root)

    def _bind(self, node: Node, fn: Node | None) -> None:
        name = self._text(node)
        if fn is not None and name in self.scan.globals.get(fn.id, ()):
            return
        self.scan.binders.add(node.id)
        if fn is not None:
            self.scan.locals.setdefault(fn.id, set()).add(name)

    def _set_var_type(self, scope: Node, name: str, tname: str | None) -> None:
        if tname:
            self.scan.var_types.setdefault(scope.id, {}).setdefault(name, tname)

    def _set_attr_type(self, cls: Node | None, name: str, tname: str | None) -> None:
        if cls is not None and tname:
            self.scan.attr_types.setdefault(cls.id, {}).setdefault(name, tname)

    # Python

    def _py_targets(self, node: Node | None) -> Iterator[Node]:
        if node is None:
            return
        if node.type == "identifier":
            yield node
        elif node.type in (
            "pattern_list",
            "tuple_pattern",
            "list_pattern",
            "list_splat_pattern",
            "tuple",
            "list",
        ):
            for c in node.named_children:
                yield from self._py_targets(c)

    def _py_ann_name(self, node: Node | None) -> str | None:
        if node is None:
            return None
        for n in self._annotation_names(node):
            name = self._text(n[0])
            if name not in _ANNOTATION_SKIP:
                return name
        return None

    def _py_value_type(self, value: Node | None, scope: Node | None) -> str | None:
        if value is None:
            return None
        if value.type == "await":
            inner = value.named_children[0] if value.named_children else None
            return self._py_value_type(inner, scope)
        if value.type == "call":
            fn = value.child_by_field_name("function")
            if fn is not None and fn.type == "identifier" and self._looks_like_class(self._text(fn)):
                return self._text(fn)
        if value.type == "identifier" and scope is not None:
            return self.scan.var_types.get(scope.id, {}).get(self._text(value))
        return None

    def _scan_python(self, root: Node) -> None:
        # スタック要素: (ノード, 囲む関数, 囲むスコープ〈関数・クラス・ルート〉, 囲むクラス)
        stack: list[tuple[Node, Node | None, Node, Node | None]] = [(root, None, root, None)]
        while stack:
            node, fn, scope, cls = stack.pop()
            t = node.type
            child_fn, child_scope, child_cls = fn, scope, cls
            if t == "function_definition":
                name = node.child_by_field_name("name")
                if name is not None:
                    self._bind(name, fn)
                params = node.child_by_field_name("parameters")
                child_fn = child_scope = node
                if params is not None:
                    self._py_params(params, node)
            elif t == "lambda":
                child_fn = child_scope = node
                params = node.child_by_field_name("parameters")
                if params is not None:
                    self._py_params(params, node)
            elif t == "class_definition":
                name = node.child_by_field_name("name")
                if name is not None:
                    self._bind(name, fn)
                    self._record_bases(name, self._py_base_names(node))
                child_scope = child_cls = node
            elif t == "assignment":
                self._py_assignment(node, fn, scope, cls)
            elif t in ("for_statement", "for_in_clause"):
                for n in self._py_targets(node.child_by_field_name("left")):
                    self._bind(n, fn)
            elif t == "named_expression":
                n = node.child_by_field_name("name")
                if n is not None:
                    self._bind(n, fn)
            elif t == "as_pattern_target":
                for n in node.named_children:
                    if n.type == "identifier":
                        self._bind(n, fn)
            elif t in ("import_statement", "import_from_statement"):
                if scope.parent is not None:  # def / class の中（モジュール直下は名前表がそのまま使える）
                    self._py_scoped_import(node, scope)
            elif t == "global_statement" and fn is not None:
                for n in node.named_children:
                    self.scan.globals.setdefault(fn.id, set()).add(self._text(n))
            for c in reversed(node.children):
                stack.append((c, child_fn, child_scope, child_cls))

    def _py_scoped_import(self, stmt: Node, scope: Node) -> None:
        """``def`` / ``class`` の中の import 文が、そのスコープで束縛する名前を控える（ローカル変数とは別に。使用箇所は import の先を指す）。"""
        p = self.pos(stmt)
        key = (p.line, p.col)
        self.scan.nested_imports.add(key)
        table = self.scan.imports.setdefault(scope.id, {})
        for n in stmt.children_by_field_name("name"):
            bound = n.child_by_field_name("alias") if n.type == "aliased_import" else n
            if bound is None:
                continue
            first = bound if bound.type == "identifier" else (bound.named_children or [None])[0]
            if first is None:
                continue
            name = self._text(first)
            if name in self.scan.globals.get(scope.id, ()):
                continue  # ``global X`` + ``import X``: モジュールの名前を束ねる
            table[name] = key

    def _record_bases(self, name_node: Node, bases: list[str]) -> None:
        position = self.pos(name_node)
        sym = self.by_selection.get((position.line, position.col))
        if sym is not None and bases:
            self.class_bases.setdefault(sym.qualified_name, bases)

    def _py_base_names(self, cls: Node) -> list[str]:
        """基底クラスの綴り（書いた順）。``mod.Base`` は点つきのまま（``_local_bases`` が整えに任せる印にする）。"""
        out: list[str] = []
        sup = cls.child_by_field_name("superclasses")
        for c in sup.named_children if sup is not None else ():
            if c.type in ("identifier", "attribute"):
                out.append(self._text(c))
            elif c.type == "subscript":
                value = c.child_by_field_name("value")
                if value is not None and value.type in ("identifier", "attribute"):
                    out.append(self._text(value))
        return out

    def _js_base_names(self, cls: Node) -> list[str]:
        """``extends`` の基底クラス（TS の ``implements`` は入れない。実装を持たないので継承したメンバーの探し先にならない）。"""
        out: list[str] = []
        for heritage in (c for c in cls.children if c.type == "class_heritage"):
            for clause in heritage.named_children:
                if clause.type == "implements_clause":
                    continue
                value = clause.child_by_field_name("value") if clause.type == "extends_clause" else clause
                if value is not None and value.type in ("identifier", "member_expression"):
                    out.append(self._text(value))
        return out

    def _ts_interface_base_names(self, node: Node) -> list[str]:
        """TS の ``interface I extends A, ns.B<T>`` の基底（書いた順）。"""
        out: list[str] = []
        for clause in (c for c in node.named_children if c.type == "extends_type_clause"):
            for t in clause.named_children:
                if t.type == "generic_type":
                    t = t.child_by_field_name("name") or t
                if t.type in ("type_identifier", "nested_type_identifier"):
                    out.append(self._text(t))
        return out

    def _py_params(self, params: Node, fn: Node) -> None:
        for p in params.named_children:
            if p.type == "identifier":
                self._bind(p, fn)
            elif p.type == "typed_parameter":
                first = next((c for c in p.named_children if c.type == "identifier"), None)
                if first is not None:
                    self._bind(first, fn)
                    self._set_var_type(fn, self._text(first), self._py_ann_name(p.child_by_field_name("type")))
            elif p.type in ("default_parameter", "typed_default_parameter"):
                n = p.child_by_field_name("name")
                if n is not None:
                    self._bind(n, fn)
                    self._set_var_type(fn, self._text(n), self._py_ann_name(p.child_by_field_name("type")))
            elif p.type in ("list_splat_pattern", "dictionary_splat_pattern"):
                for n in p.named_children:
                    if n.type == "identifier":
                        self._bind(n, fn)

    def _py_assignment(self, node: Node, fn: Node | None, scope: Node, cls: Node | None) -> None:
        left = node.child_by_field_name("left")
        if left is None:
            return
        tname = self._py_ann_name(node.child_by_field_name("type")) or self._py_value_type(
            node.child_by_field_name("right"), fn
        )
        if left.type == "identifier":
            self._bind(left, fn)
            self._set_var_type(scope, self._text(left), tname)
            if scope is not fn and cls is not None and scope.id == cls.id:
                self._set_attr_type(
                    cls, self._text(left), tname
                )  # クラス直下の ``x: T``（dataclass のフィールドなど）
        elif left.type in ("pattern_list", "tuple_pattern", "list_pattern"):
            for n in self._py_targets(left):
                self._bind(n, fn)
        elif left.type == "attribute":
            obj = left.child_by_field_name("object")
            attr = left.child_by_field_name("attribute")
            if (
                obj is not None
                and attr is not None
                and obj.type == "identifier"
                and self._text(obj) in _RECEIVER_SELF
            ):
                self._set_attr_type(cls, self._text(attr), tname)

    # TypeScript / JavaScript

    def _js_targets(self, node: Node | None) -> Iterator[Node]:
        if node is None:
            return
        t = node.type
        if t in ("identifier", "shorthand_property_identifier_pattern"):
            yield node
        elif t == "object_pattern":
            for c in node.named_children:
                if c.type == "pair_pattern":
                    yield from self._js_targets(c.child_by_field_name("value"))
                else:
                    yield from self._js_targets(c)
        elif t == "array_pattern":
            for c in node.named_children:
                yield from self._js_targets(c)
        elif t == "rest_pattern":
            for c in node.named_children:
                yield from self._js_targets(c)
        elif t in ("assignment_pattern", "object_assignment_pattern"):  # 後者は ``{ foo = 1 }`` の ``foo``
            yield from self._js_targets(node.child_by_field_name("left"))

    def _js_ann_name(self, node: Node | None) -> str | None:
        if node is None:
            return None
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "type_identifier":
                return self._text(n)
            stack.extend(reversed(n.children))
        return None

    def _js_value_type(self, value: Node | None, fn: Node | None) -> str | None:
        if value is None:
            return None
        if value.type in ("await_expression", "parenthesized_expression", "as_expression"):
            inner = value.named_children[0] if value.named_children else None
            return self._js_value_type(inner, fn)
        if value.type == "new_expression":
            ctor = value.child_by_field_name("constructor")
            if ctor is not None and ctor.type == "identifier":
                return self._text(ctor)
        if value.type == "identifier" and fn is not None:
            return self.scan.var_types.get(fn.id, {}).get(self._text(value))
        return None

    def _scan_js(self, root: Node) -> None:
        stack: list[tuple[Node, Node | None, Node, Node | None]] = [(root, None, root, None)]
        while stack:
            node, fn, scope, cls = stack.pop()
            t = node.type
            child_fn, child_scope, child_cls = fn, scope, cls
            if t in self.functions:
                if t in ("function_declaration", "generator_function_declaration"):
                    name = node.child_by_field_name("name")
                    if name is not None:
                        self._bind(name, fn)
                child_fn = child_scope = node
                params = node.child_by_field_name("parameters")
                if params is not None:
                    self._js_params(params, node, cls)
                single = node.child_by_field_name("parameter")
                if single is not None and single.type == "identifier":
                    self._bind(single, node)
            elif t in self.classes:
                name = node.child_by_field_name("name")
                if name is not None:
                    if name.type == "identifier":
                        self._bind(name, fn)
                    self._record_bases(name, self._js_base_names(node))
                child_scope = child_cls = node
            elif t == "interface_declaration":
                name = node.child_by_field_name("name")
                if name is not None:
                    self._record_bases(name, self._ts_interface_base_names(node))
            elif t == "variable_declarator":
                name = node.child_by_field_name("name")
                if name is not None:
                    imported = (
                        js_require_bindings(node, self.parsed.source) if fn is not None else []
                    )
                    for n in self._js_targets(name):
                        if fn is not None and self._text(n) in imported:
                            self._js_scoped_require(node, n, fn)
                        else:
                            self._bind(n, fn)
                    if name.type == "identifier":
                        tname = self._js_ann_name(node.child_by_field_name("type")) or self._js_value_type(
                            node.child_by_field_name("value"), fn
                        )
                        self._set_var_type(scope, self._text(name), tname)
            elif t in ("public_field_definition", "field_definition"):
                name = node.child_by_field_name("name") or node.child_by_field_name("property")
                if name is not None and cls is not None:
                    tname = self._js_ann_name(node.child_by_field_name("type")) or self._js_value_type(
                        node.child_by_field_name("value"), fn
                    )
                    self._set_attr_type(cls, self._text(name), tname)
            elif t == "assignment_expression":
                left = node.child_by_field_name("left")
                if left is not None and left.type == "member_expression":
                    obj = left.child_by_field_name("object")
                    prop = left.child_by_field_name("property")
                    if obj is not None and prop is not None and obj.type == "this":
                        self._set_attr_type(
                            cls, self._text(prop), self._js_value_type(node.child_by_field_name("right"), fn)
                        )
            elif t == "catch_clause":
                for n in self._js_targets(node.child_by_field_name("parameter")):
                    self._bind(n, fn)
            elif t == "for_in_statement":
                for n in self._js_targets(node.child_by_field_name("left")):
                    self._bind(n, fn)
            for c in reversed(node.children):
                stack.append((c, child_fn, child_scope, child_cls))

    def _js_scoped_require(self, declarator: Node, name_node: Node, fn: Node) -> None:
        """関数の中の ``const { foo } = require("x")`` が束縛する ``foo`` を控える（ローカル変数とは別に。使用箇所は ``x`` の先を指す）。

        キーは宣言文（``const ...``）の開始位置。``extract_imports`` が ``require`` の ``ImportFact.range`` に使う文と同じ。
        """
        decl = declarator.parent or declarator
        p = self.pos(decl)
        self.scan.binders.add(name_node.id)
        self.scan.imports.setdefault(fn.id, {})[self._text(name_node)] = (p.line, p.col)

    def _js_params(self, params: Node, fn: Node, cls: Node | None) -> None:
        for p in params.named_children:
            if p.type in ("required_parameter", "optional_parameter"):
                pattern = p.child_by_field_name("pattern")
                names = list(self._js_targets(pattern))
                for n in names:
                    self._bind(n, fn)
                tname = self._js_ann_name(p.child_by_field_name("type"))
                if pattern is not None and pattern.type == "identifier":
                    self._set_var_type(fn, self._text(pattern), tname)
                    if any(
                        c.type in ("accessibility_modifier", "override_modifier") for c in p.children
                    ) or any(c.type == "readonly" for c in p.children):
                        self._set_attr_type(
                            cls, self._text(pattern), tname
                        )  # コンストラクタのパラメータプロパティ
            else:
                for n in self._js_targets(p):
                    self._bind(n, fn)


_NOT_MEMBER_TARGETS = frozenset({Kind.VARIABLE})


def _neg_pos(p: Position) -> tuple[int, int]:
    return (-p.line, -p.col)


def _append_member(urn_text: str, member: str) -> str | None:
    """``urn_text`` の修飾名に ``.member`` を足した URN（ファイル URN なら ``#member``）。"""
    try:
        urn = parse(urn_text)
    except ValueError:
        return None
    sel = urn.selector
    if sel is None:
        return str(urn.with_selector(CodeSel(member)))
    if isinstance(sel, CodeSel):
        return str(urn.with_selector(CodeSel(f"{sel.qualified}.{member}")))
    return None
