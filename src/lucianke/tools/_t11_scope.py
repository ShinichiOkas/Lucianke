"""T-11 の ``structural`` 降格: Python の関数内のローカル変数・引数を、同一ファイル内だけで改名する（仕様書 T-11 精度と降格、§10.5）。

LSP が使えないとき、名前の解決は Tree-sitter の構文木だけでできる範囲に限る。参照が **1 つの関数（とその中の入れ子）に閉じる**
名前だけが対象で、それ以外（モジュール直下・クラス直下・import・``global`` / ``nonlocal``・メンバー）は ``None`` を返し、
呼び手が元の ``E_LSP_UNAVAILABLE`` を返す。誤って改名するより、断るほうを選ぶ。

規則（Python のスコープ）:

* 名前を束縛する最も内側の関数 ``F``（引数・代入・拡張代入・``for`` / ``with`` / ``except`` の ``as``・``:=``・入れ子の ``def`` / ``class`` の名前）が
  改名のスコープ。``F`` の引数の既定値・注釈・戻り値注釈・デコレータは ``F`` の外で評価されるので、``F`` の束縛の外とする。
* ``F`` の中の同じ綴りの ``identifier`` を改名する。ただし属性アクセスの属性名（``obj.name``）、キーワード引数の名前（``f(name=1)``）、
  import 文の中、同名を自分で束縛する入れ子の関数・ラムダ・内包表記の中は除く（入れ子の関数の既定値・注釈・デコレータは外側の評価なので含める）。
* 次のときは対象にしない（``None``）: ``F`` が ``global`` / ``nonlocal`` を宣言している、import で束縛している、入れ子のクラスが同名をクラス直下で
  束縛している、``new_name`` が ``F`` の中（または ``F`` の外側の関数）で既に使われている（``E_MUTATION_BLOCKED``）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Position, Range
from lucianke.syntax.types import ParsedFile

__all__ = ["rename_local"]

_FUNCTIONS = frozenset({"function_definition", "lambda"})
_COMPREHENSIONS = frozenset(
    {"list_comprehension", "set_comprehension", "dictionary_comprehension", "generator_expression"}
)
_IMPORTS = frozenset({"import_statement", "import_from_statement", "future_import_statement"})
_PARAM_NAME_PARENTS = frozenset(
    {
        "parameters",
        "lambda_parameters",
        "default_parameter",
        "typed_parameter",
        "typed_default_parameter",
        "list_splat_pattern",
        "dictionary_splat_pattern",
    }
)


class _Unsupported(Exception):
    """この名前は構文木だけでは安全に改名できない。"""


def _same(a: Any, b: Any | None) -> bool:
    """Tree-sitter のノードは取り出すたびに別のオブジェクトなので、``id`` で同一か見る。"""
    return b is not None and a.id == b.id


def _text(node: Any) -> str:
    return node.text.decode("utf-8", "surrogateescape")


def _children(node: Any) -> Iterator[Any]:
    yield from node.children


def _is_param_name(node: Any) -> bool:
    """``node``（identifier）が関数の引数の **名前** か（既定値・注釈ではない）。"""
    parent = node.parent
    if parent is None or parent.type not in _PARAM_NAME_PARENTS:
        return False
    if parent.type in ("default_parameter", "typed_default_parameter"):
        name = parent.child_by_field_name("name")
        return name is not None and name.id == node.id
    if parent.type == "typed_parameter":
        return parent.children[0].id == node.id
    return True  # parameters / lambda_parameters の直下、*args / **kwargs


def _param_names(fn: Any) -> list[Any]:
    out: list[Any] = []
    params = fn.child_by_field_name("parameters")
    if params is None:
        return out

    def visit(n: Any) -> None:
        for c in n.children:
            if c.type == "identifier":
                if _is_param_name(c):
                    out.append(c)
            elif c.type in ("typed_parameter", "list_splat_pattern", "dictionary_splat_pattern"):
                visit(c)
            elif c.type in ("default_parameter", "typed_default_parameter"):
                name = c.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    out.append(name)

    visit(params)
    return out


def _target_names(node: Any | None) -> Iterator[Any]:
    """代入・``for`` の左辺から、束縛される identifier（属性・添字は束縛しない）。"""
    if node is None:
        return
    if node.type == "identifier":
        yield node
    elif node.type in (
        "pattern_list",
        "tuple_pattern",
        "list_pattern",
        "list_splat_pattern",
        "parenthesized_expression",
    ):
        for c in node.named_children:
            yield from _target_names(c)


class _Bindings:
    """関数 1 つの **自分のスコープ** の束縛（入れ子の関数・ラムダ・クラスの中身は含まない）。"""

    __slots__ = ("bound", "globals_", "imported", "nonlocals")

    def __init__(self, fn: Any) -> None:
        self.bound: set[str] = set()
        self.globals_: set[str] = set()
        self.nonlocals: set[str] = set()
        self.imported: set[str] = set()
        for n in _param_names(fn):
            self.bound.add(_text(n))
        body = fn.child_by_field_name("body")
        if body is not None:
            self._scan(body)

    def _scan(self, node: Any) -> None:
        t = node.type
        if t in _FUNCTIONS:
            if t == "function_definition":
                name = node.child_by_field_name("name")
                if name is not None:
                    self.bound.add(_text(name))
            return  # 入れ子の関数の中身は別のスコープ
        if t == "class_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                self.bound.add(_text(name))
            return
        if t in _COMPREHENSIONS:
            for n in _walrus_targets(node):
                self.bound.add(_text(n))
            return
        if t in ("global_statement", "nonlocal_statement"):
            names = {_text(c) for c in node.children if c.type == "identifier"}
            (self.globals_ if t == "global_statement" else self.nonlocals).update(names)
            return
        if t in _IMPORTS:
            self.imported.update(self._import_names(node))
            return
        if t == "assignment":
            for n in _target_names(node.child_by_field_name("left")):
                self.bound.add(_text(n))
        elif t == "augmented_assignment":
            for n in _target_names(node.child_by_field_name("left")):
                self.bound.add(_text(n))
        elif t == "for_statement":
            for n in _target_names(node.child_by_field_name("left")):
                self.bound.add(_text(n))
        elif t == "named_expression":
            name = node.child_by_field_name("name")
            if name is not None:
                self.bound.add(_text(name))
        elif t == "except_clause":
            seen_as = False
            for c in node.children:
                if seen_as and c.type == "identifier":
                    self.bound.add(_text(c))
                seen_as = c.type == "as"
        elif t == "as_pattern":
            alias = node.child_by_field_name("alias") or (node.children[-1] if node.children else None)
            if alias is not None:
                for n in _walk_identifiers(alias):
                    self.bound.add(_text(n))
        for c in node.children:
            self._scan(c)

    @property
    def declared(self) -> set[str]:
        return self.globals_ | self.nonlocals

    @staticmethod
    def _import_names(node: Any) -> Iterator[str]:
        for c in node.children:
            if c.type == "aliased_import":
                alias = c.child_by_field_name("alias")
                if alias is not None:
                    yield _text(alias)
            elif c.type == "dotted_name":
                if node.type == "import_statement" and c.children:
                    yield _text(c.children[0])
                elif node.type == "import_from_statement" and not _same(
                    c, node.child_by_field_name("module_name")
                ):
                    yield _text(c)
            elif c.type == "wildcard_import":
                continue


def _walk_identifiers(node: Any) -> Iterator[Any]:
    if node.type == "identifier":
        yield node
        return
    for c in node.children:
        yield from _walk_identifiers(c)


def _walrus_targets(node: Any) -> Iterator[Any]:
    if node.type == "named_expression":
        name = node.child_by_field_name("name")
        if name is not None:
            yield name
    for c in node.children:
        if c.type not in _FUNCTIONS:
            yield from _walrus_targets(c)


# --- 改名するスコープの決定 -----------------------------------------------------------------------


def _scope_function(node: Any, name: str, cache: dict[int, _Bindings]) -> Any | None:
    """``node`` を含む関数のうち、``name`` を束縛する最も内側のもの。無ければ（または ``global`` / ``nonlocal`` なら）``None``。"""
    child = node
    cur = node.parent
    while cur is not None:
        if cur.type in _FUNCTIONS:
            body = cur.child_by_field_name("body")
            in_scope = (body is not None and child.id == body.id) or (
                _is_param_name(node) and _owning_function(node).id == cur.id
            )
            if in_scope:
                b = cache.get(cur.id)
                if b is None:
                    b = cache[cur.id] = _Bindings(cur)
                if name in b.declared:
                    return None
                if name in b.bound:
                    return cur
        child, cur = cur, cur.parent
    return None


def _owning_function(param_name: Any) -> Any | None:
    cur = param_name.parent
    while cur is not None and cur.type not in _FUNCTIONS:
        cur = cur.parent
    return cur


def _is_member_or_keyword(node: Any) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "attribute":
        attr = parent.child_by_field_name("attribute")
        return attr is not None and attr.id == node.id
    if parent.type == "keyword_argument":
        name = parent.child_by_field_name("name")
        return name is not None and name.id == node.id
    return False


class _Collector:
    def __init__(self, scope_fn: Any, name: str) -> None:
        self.scope_fn = scope_fn
        self.name = name
        self.found: list[Any] = []
        self._cache: dict[int, _Bindings] = {}

    def _bindings(self, fn: Any) -> _Bindings:
        b = self._cache.get(fn.id)
        if b is None:
            b = self._cache[fn.id] = _Bindings(fn)
        return b

    def run(self) -> list[Any]:
        fn = self.scope_fn
        for n in _param_names(fn):
            if _text(n) == self.name:
                self.found.append(n)
        body = fn.child_by_field_name("body")
        if body is not None:
            self.walk(body)
        return self.found

    def _shadows(self, fn: Any) -> bool:
        """入れ子の関数が ``name`` を自分で束縛する（外側の ``name`` とは別の変数）か。``nonlocal`` なら外側と同じ変数。"""
        b = self._bindings(fn)
        if self.name in b.nonlocals:
            return False
        return self.name in b.bound or self.name in b.globals_

    def _outer_parts(self, fn: Any) -> None:
        """入れ子の関数の、外側のスコープで評価される部分（既定値・注釈・戻り値注釈）。"""
        params = fn.child_by_field_name("parameters")
        if params is not None:
            for p in params.children:
                if p.type in ("default_parameter", "typed_default_parameter"):
                    for field in ("value", "type"):
                        sub = p.child_by_field_name(field)
                        if sub is not None:
                            self.walk(sub)
                elif p.type == "typed_parameter":
                    sub = p.child_by_field_name("type")
                    if sub is not None:
                        self.walk(sub)
        ret = fn.child_by_field_name("return_type")
        if ret is not None:
            self.walk(ret)

    def walk(self, node: Any) -> None:
        t = node.type
        if t == "identifier":
            if _text(node) == self.name and not _is_member_or_keyword(node):
                self.found.append(node)
            return
        if t in _IMPORTS:
            return
        if t in _FUNCTIONS:
            if t == "function_definition":
                nm = node.child_by_field_name("name")
                if nm is not None and _text(nm) == self.name:
                    self.found.append(nm)  # def name(): はこのスコープの束縛
            if self._shadows(node):
                self._outer_parts(node)
                return
            for c in node.children:
                if t == "function_definition" and _same(c, node.child_by_field_name("name")):
                    continue
                self.walk(c)
            return
        if t == "class_definition":
            nm = node.child_by_field_name("name")
            if nm is not None and _text(nm) == self.name:
                self.found.append(nm)
            body = node.child_by_field_name("body")
            if body is not None and _class_binds(body, self.name):
                raise _Unsupported("class body binds the name")
            for c in node.children:
                if nm is not None and c.id == nm.id:
                    continue
                self.walk(c)
            return
        if t in _COMPREHENSIONS:
            clauses = [c for c in node.children if c.type == "for_in_clause"]
            binds = any(
                _text(n) == self.name for c in clauses for n in _target_names(c.child_by_field_name("left"))
            )
            if binds:
                first = clauses[0].child_by_field_name("right") if clauses else None
                if first is not None:
                    self.walk(first)  # 最初の反復対象だけは外側のスコープで評価される
                return
        for c in node.children:
            self.walk(c)


def _descendants(node: Any) -> Iterator[Any]:
    for c in node.children:
        yield c
        if c.type not in _FUNCTIONS:
            yield from _descendants(c)


def _class_binds(body: Any, name: str) -> bool:
    """クラス直下（メソッドの中身は除く）で ``name`` を束縛するか。"""
    for n in body.children:
        t = n.type
        if t == "expression_statement":
            for sub in n.children:
                if sub.type in ("assignment", "augmented_assignment") and any(
                    _text(x) == name for x in _target_names(sub.child_by_field_name("left"))
                ):
                    return True
        elif t in ("function_definition", "class_definition"):
            nm = n.child_by_field_name("name")
            if nm is not None and _text(nm) == name:
                return True
        elif t == "decorated_definition":
            d = n.child_by_field_name("definition")
            nm = d.child_by_field_name("name") if d is not None else None
            if nm is not None and _text(nm) == name:
                return True
    return False


def _used_names_outside(scope_fn: Any, name: str) -> bool:
    """``name`` を、``scope_fn`` を含む最も外側の関数の中で使っているか（``new_name`` の衝突検査。入れ子の別関数も数える安全側）。"""
    outer = scope_fn
    while (nxt := _enclosing_function(outer)) is not None:
        outer = nxt
    return any(_text(n) == name and not _is_member_or_keyword(n) for n in _walk_identifiers_deep(outer))


def _enclosing_function(node: Any) -> Any | None:
    cur = node.parent
    while cur is not None and cur.type not in _FUNCTIONS:
        cur = cur.parent
    return cur


def _walk_identifiers_deep(node: Any) -> Iterator[Any]:
    if node.type == "identifier":
        yield node
        return
    for c in node.children:
        yield from _walk_identifiers_deep(c)


# --- 入口 ---------------------------------------------------------------------------------------


def rename_local(
    parsed: ParsedFile, source: bytes, language_id: str, line: int, character: int, new_name: str
) -> tuple[str, Position, list[Range]] | None:
    """``(line, character)``（0 始まり・コードポイント）の識別子が関数内のローカル変数・引数なら、
    ``(旧名, 先頭位置, 改名する範囲の列)`` を返す。対象にならなければ ``None``。``new_name`` が衝突すれば ``E_MUTATION_BLOCKED``。
    """
    if language_id != "python":
        return None
    index = parsed.line_index
    if line >= index.line_count:
        return None
    text = index.line_text(line)
    start = min(character, len(text))
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    end = start
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    if end == start:
        return None
    lo = index.position_to_offset(Position(line, start)).value
    hi = index.position_to_offset(Position(line, end)).value
    node = parsed.tree.root_node.descendant_for_byte_range(lo, hi)
    if node is None or node.type != "identifier" or node.start_byte != lo or node.end_byte != hi:
        return None
    if _is_member_or_keyword(node):
        return None
    name = _text(node)
    try:
        scope_fn = _scope_function(node, name, {})
        if scope_fn is None:
            return None
        b = _Bindings(scope_fn)
        if name in b.declared or name in b.imported:
            return None
        found = _Collector(scope_fn, name).run()
    except _Unsupported:
        return None
    if not found:
        return None
    if name == new_name:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            "new_name is the same as the current name",
            details={"param": "new_name", "reason": "same_name"},
        )
    if _used_names_outside(scope_fn, new_name):
        raise LuciankeError(
            ErrorCode.E_MUTATION_BLOCKED,
            f"new_name {new_name!r} is already used in the function that binds {name!r} (or in a function around it)",
            details={"reason": "new_name_already_in_scope", "new_name": new_name, "old_name": name},
        )
    ranges: list[Range] = []
    for n in sorted(found, key=lambda x: x.start_byte):
        a = index.offset_to_position(n.start_byte).value
        z = index.offset_to_position(n.end_byte).value
        ranges.append(Range(a, z))
    return name, Position(line, start), ranges
