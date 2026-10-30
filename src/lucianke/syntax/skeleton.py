"""ファイル骨格のレコード（設計書 §7.4.4、仕様書 §11 T-02 の処理規約 2・8・9、§6.1.1）。

``build_file_record`` は 1 ファイル = 1 レコード（``kind="file-block"``、``group=rel``）を作る。

* ``lines``: ``=== path (N lines, skeleton: M lines, sha=…) ===`` → ``imports: […]``（モジュールごと: ``m: a, b; n``）→
  入れ子の ``symbol-block``（``doc:`` ・``urn:`` ・``decorators:`` ・``raises:`` の属性行つき）。本体は ``{ ...N lines omitted }``
  に畳む。``urn:`` 行は ``lk://<ws>/<path>#<行が示す修飾名>`` で導けない URN だけ（仕様書 §6.1 規約 2。BUG-0069）。
* ``data``: T-02 の json の ``items[]`` の 1 項目（``symbols[]`` は SymbolRecord の json に ``children`` ・``fold_lines`` ・
  ``decorators`` ・``raises`` を足したもの）。
* ``shrink``: 予算に入らないときの縮退版（処理規約 8: ``doc:`` 行 → ``urn:`` 行 → 非公開メンバー → 入れ子メンバー
  〈最上位は残す〉の順に **1 件ずつ** 落とした段のうち、収まる最も浅いもの。``- truncated: N members omitted (…): <入れ物>
  <件数>, …; whole file: max_tokens>=M with this path alone`` を足す）。入らなければ ``None``。``shrink_json`` は同じ鎖を
  json の大きさで選ぶ。以前は「入れ子を全部落とす」段しか無く、予算の 1 割しか使わない応答があった（BUG-0078）。

行の組み立て関数（``file_block_header`` ・``import_line`` ・``symbol_line`` ・``attr_line`` ・``note``）は
``core/compact_lines.py`` のものを使う（``mcp/render/compact.py`` も同じものを再公開する）。
``syntax`` は ``mcp`` を import できない（``tests/unit/test_import_graph.py``。設計書 §4.2）ので、共有の部品を ``core`` に置いた。

位置: SymbolRecord は内部表現（0 始まり）。外へ出す行は ``Range.line_span_external()``（1 始まり・両端を含む）。
本体の畳み方: 畳む行数 = 範囲の行数 − デコレータ行 − シグネチャ行（定義の見出し〜本体の直前）− 出した子シンボルの範囲の行数。
``{ ...N lines omitted }`` を付けるのは関数・メソッド・コンストラクタだけ（クラスなどの入れ物は付けない。§6.1.1 の例に倣う）。
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from lucianke.core.compact_lines import (
    MODIFIERS,
    attr_line,
    file_block_header,
    import_line,
    note,
    one_line,
    symbol_line,
    urn_lines,
)
from lucianke.core.model import Confidence, Kind, SymbolRecord
from lucianke.core.records import Record
from lucianke.core.tokens import TokenEstimator
from lucianke.syntax.memo import BoundedMemo, content_key
from lucianke.syntax.support import SUPPORT
from lucianke.syntax.types import ParsedFile, SkeletonOptions

__all__ = [
    "attr_line",
    "build_file_record",
    "default_estimate",
    "extract_imports",
    "file_block_header",
    "group_imports",
    "import_line",
    "note",
    "symbol_line",
]

_CALLABLE_KINDS = frozenset({Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR})


# --- トークンの見積もり（パッカーと同じ core の推定器） ----------------------------------------------------

#: ``shrink`` の判定用の既定のトークン見積もり。MCP のパッカー（``mcp.budget.Packer``）と同じ ``core.tokens.TokenEstimator``。
default_estimate: Callable[[str], int] = TokenEstimator().count


# --- 構文木から取るもの（import ・デコレータ・raise ・シグネチャ行数） ------------------------------------


# 注意: ``Node.start_point`` / ``end_point`` の行・列は **``point[0]`` / ``point[1]``** で読む。属性 ``.row`` / ``.column`` は
# py-tree-sitter 0.26.0 で int の参照を余計に手放し（256 を超える値でヒープが壊れ）、あとの木の走査でアクセス違反になる
# （BUG-0003。``tests/unit/test_syntax_skeleton_realfiles.py`` が ``src/`` の属性読みを禁じる）。


def _text(parsed: ParsedFile, node: Any) -> str:
    return parsed.source[node.start_byte : node.end_byte].decode("utf-8", "replace")


_PY_CONTAINERS = frozenset(
    {
        "if_statement",
        "elif_clause",
        "else_clause",
        "try_statement",
        "except_clause",
        "except_group_clause",
        "finally_clause",
        "with_statement",
        "block",
        "for_statement",
        "while_statement",
        "match_statement",
        "case_clause",
    }
)


type _Import = tuple[str, str | None]
"""import 1 件: ``(module, name)``。``import module`` は ``name=None``、``from module import a as b`` は ``name="a as b"``。"""


def _py_imports(parsed: ParsedFile) -> list[_Import]:
    items: list[_Import] = []

    def name_of(node: Any) -> str:
        if node.type == "aliased_import":
            base = node.child_by_field_name("name")
            alias = node.child_by_field_name("alias")
            if base is not None and alias is not None:
                return f"{_text(parsed, base)} as {_text(parsed, alias)}"
        return _text(parsed, node)

    def visit(parent: Any) -> None:
        for c in parent.named_children:
            if c.type == "import_statement":
                items.extend((name_of(n), None) for n in c.children_by_field_name("name"))
            elif c.type == "import_from_statement":
                module = c.child_by_field_name("module_name")
                where = _text(parsed, module) if module is not None else "?"
                names = [name_of(n) for n in c.children_by_field_name("name")]
                if any(ch.type == "wildcard_import" for ch in c.children):
                    names.append("*")
                items.extend((where, n) for n in names)
            elif c.type in _PY_CONTAINERS:
                visit(c)

    visit(parsed.tree.root_node)
    return items


def _js_imports(parsed: ParsedFile) -> list[_Import]:
    items: list[_Import] = []
    for c in parsed.tree.root_node.named_children:
        if c.type != "import_statement":
            continue
        source = c.child_by_field_name("source")
        if source is None:
            continue
        where = _text(parsed, source).strip("\"'`")
        clause = next((ch for ch in c.named_children if ch.type == "import_clause"), None)
        if clause is None:
            items.append((where, None))  # import "side-effect";
            continue
        for part in clause.named_children:
            if part.type == "identifier":  # 既定 import
                items.append((where, _text(parsed, part)))
            elif part.type == "namespace_import":
                ident = next((n for n in part.named_children if n.type == "identifier"), None)
                local = _text(parsed, ident) if ident is not None else "?"
                items.append((where, f"* as {local}"))
            elif part.type == "named_imports":
                for spec in part.named_children:
                    if spec.type != "import_specifier":
                        continue
                    base = spec.child_by_field_name("name")
                    alias = spec.child_by_field_name("alias")
                    if base is None:
                        continue
                    text = _text(parsed, base)
                    if alias is not None:
                        text += f" as {_text(parsed, alias)}"
                    items.append((where, text))
    return items


def _imports_of(parsed: ParsedFile) -> list[_Import]:
    """文書順・重複なしの ``(module, name)``。対象外の言語は空。"""
    if parsed.language_id == "python":
        raw = _py_imports(parsed)
    elif parsed.language_id in ("typescript", "tsx", "javascript", "jsx"):
        raw = _js_imports(parsed)
    else:
        return []
    return list(dict.fromkeys(raw))


def _flat(items: Sequence[_Import]) -> list[str]:
    """json の ``items[].imports[]``（仕様書 §11 T-02。形は変えない）: ``module`` か ``name from module``。"""
    return list(dict.fromkeys(m if n is None else f"{n} from {m}" for m, n in items))


def group_imports(items: Sequence[_Import]) -> list[str]:
    """compact の ``import-line`` の項目（仕様書 §6.1 規約 10。BUG-0069）: モジュールごとにまとめる。

    ``import m`` → ``m``、``from m import a, b as c``（同じモジュールの文が複数あっても 1 つに）→ ``m: a, b as c``。
    並びはそのモジュールが最初に現れた順。モジュール名を名前ごとに繰り返さない（トークンを使わない）。
    """
    groups: dict[tuple[str, bool], list[str]] = {}
    for module, name in items:
        names = groups.setdefault((module, name is not None), [])
        if name is not None and name not in names:
            names.append(name)
    return [f"{m}: {', '.join(names)}" if named else m for (m, named), names in groups.items()]


def extract_imports(parsed: ParsedFile) -> list[str]:
    """``import`` の要約（json の形）: ``import x`` → ``x``、``from m import a, b`` → ``a from m``, ``b from m``、
    ``import X, { A as B } from "m"`` → ``X from m``, ``A as B from m``。重複は除く。対象外の言語は空。

    compact の ``imports:`` 行はモジュールごとにまとめた ``group_imports`` の形（``m: a, b``）。
    """
    return _flat(_imports_of(parsed))


@dataclass(slots=True)
class _Info:
    """シンボルの定義ノードから読んだもの。"""

    decorators: list[str] = field(default_factory=list)
    decorator_lines: int = 0  # 範囲の中にあるデコレータの行数
    raises: list[str] = field(default_factory=list)
    header_lines: int = 1  # シグネチャ行（定義の見出し行〜本体の直前）


_NESTED_DEFS = frozenset(
    {
        "function_definition",
        "class_definition",
        "lambda",
        "function_declaration",
        "function_expression",
        "function",
        "generator_function",
        "generator_function_declaration",
        "arrow_function",
        "method_definition",
        "class_declaration",
        "class",
    }
)
_RAISE_NODES = frozenset({"raise_statement", "throw_statement"})
_JS_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})


def _definition_node(parsed: ParsedFile, sym: SymbolRecord) -> tuple[Any, Any] | None:
    """``(定義ノード, 名前ノード)``。名前の位置から外へ辿り、``name`` フィールドがその位置を含む最初のノード。"""
    pos = sym.selection.start if sym.selection is not None else sym.range.start
    off = parsed.line_index.position_to_offset(pos).value
    root = parsed.tree.root_node
    node = root.descendant_for_byte_range(off, min(off + 1, len(parsed.source)))
    while node is not None:
        name = node.child_by_field_name("name")
        if name is not None and name.start_byte <= off < name.end_byte:
            return node, name
        node = node.parent
    return None


def _thrown_name(parsed: ParsedFile, stmt: Any) -> str | None:
    expr = next(iter(stmt.named_children), None)
    if expr is None:
        return None
    if expr.type in ("call", "call_expression"):
        expr = expr.child_by_field_name("function") or expr
    elif expr.type == "new_expression":
        expr = expr.child_by_field_name("constructor") or expr
    if expr.type not in ("identifier", "attribute", "member_expression", "type_identifier"):
        return None
    name = _text(parsed, expr)
    last = re.split(r"[.]", name)[-1]
    return name if last[:1].isupper() else None  # 変数の再 raise（``raise exc``）は拾わない


def _collect_raises(parsed: ParsedFile, body: Any) -> list[str]:
    found: list[str] = []
    stack = list(reversed(body.named_children))
    while stack:
        node = stack.pop()
        if node.type in _NESTED_DEFS:
            continue  # 入れ子の定義の raise は、その定義のもの
        if node.type in _RAISE_NODES:
            name = _thrown_name(parsed, node)
            if name:
                found.append(name)
            continue
        stack.extend(reversed(node.named_children))
    return list(dict.fromkeys(found))


def _read_info(parsed: ParsedFile, sym: SymbolRecord, span: tuple[int, int]) -> _Info:
    info = _Info()
    found = _definition_node(parsed, sym)
    if found is None:
        return info
    node, name = found
    decorator_nodes: list[Any] = []
    parent = node.parent
    if parent is not None and parent.type == "decorated_definition":
        decorator_nodes = [c for c in parent.children if c.type == "decorator"]
    else:
        decorator_nodes = [c for c in node.children if c.type == "decorator"]
        if parent is not None and parent.type == "export_statement":
            decorator_nodes = [c for c in parent.children if c.type == "decorator"] + decorator_nodes
    first, last = span[0], span[1]
    for d in decorator_nodes:
        info.decorators.append(one_line(_text(parsed, d)))
        a, b = d.start_point[0] + 1, d.end_point[0] + 1
        info.decorator_lines += max(0, min(b, last) - max(a, first) + 1)
    target = node
    if node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        if value is not None and value.type in _JS_FUNCTION_VALUES:
            target = value
    body = target.child_by_field_name("body")
    name_row = name.start_point[0]
    if body is not None:
        if parsed.language_id == "python":
            info.header_lines = max(1, body.start_point[0] - name_row)  # block は colon の次の行から
        else:
            info.header_lines = body.start_point[0] - name_row + 1  # ``{`` が見出しの最後の行にある
        if sym.kind in _CALLABLE_KINDS:
            info.raises = _collect_raises(parsed, body)
    return info


# --- 入れ子の木 -------------------------------------------------------------------------------------

_NEVER = 1 << 62  # 縮退で落とさない（最上位の公開シンボル）


@dataclass(slots=True)
class _Entry:
    sym: SymbolRecord
    span: tuple[int, int]  # 1 始まり・両端を含む
    private: bool
    info: _Info
    children: list[_Entry] = field(default_factory=list)
    hidden: bool = False  # include_private=False で最初から含めない（子も含めない）
    #: 縮退で落とす順位（``_assign_ranks``）。``_Detail.cut`` より小さいものを落とす。最上位の公開シンボルは落とさない
    rank: int = _NEVER
    doc_rank: int = _NEVER  # ``doc:`` 行を落とす順位（``_Detail.doc_cut`` より小さいものを落とす）
    urn_rank: int = (
        _NEVER  # ``urn:`` 行を落とす順位（``_Detail.urn_cut`` より小さいものを落とす。``_rank_urns``）
    )
    size: int = 1  # 自分と子孫の数（落としたときに減るメンバー数）
    #: 描いた行とその ``units``（``_Meter``）の覚え。縮退段の見積もりで同じ行を何度も作って数えないように（BUG-0078）
    memo: dict[Any, Any] = field(default_factory=dict)

    @property
    def lines(self) -> int:
        return self.span[1] - self.span[0] + 1


def _is_private(sym: SymbolRecord, *, top_level: bool, language_id: str) -> bool:
    mods = sym.modifiers
    if "priv" in mods:
        return True
    if "pub" in mods or "prot" in mods or "export" in mods:
        return False
    if language_id == "python":
        return sym.name.startswith("_") and not (sym.name.startswith("__") and sym.name.endswith("__"))
    return sym.name.startswith("#") or top_level  # TS / JS: 非 export の最上位は非公開


def _build_tree(
    parsed: ParsedFile, symbols: Sequence[SymbolRecord], *, include_private: bool
) -> list[_Entry]:
    spans = [s.range.line_span_external() for s in symbols]
    order = sorted(range(len(symbols)), key=lambda i: (spans[i][0], -spans[i][1], i))
    roots: list[_Entry] = []
    stack: list[_Entry] = []
    for i in order:
        sym, span = symbols[i], spans[i]
        # 親 = 範囲をちょうど含む最も内側のシンボル。同じ範囲は兄弟（親子にしない）
        while stack and not (
            stack[-1].span[0] <= span[0] and span[1] <= stack[-1].span[1] and stack[-1].span != span
        ):
            stack.pop()
        parent = stack[-1] if stack else None
        private = _is_private(sym, top_level=parent is None, language_id=parsed.language_id)
        hidden = (parent is not None and parent.hidden) or (not include_private and private)
        entry = _Entry(
            sym=sym,
            span=span,
            private=private,
            info=_Info() if hidden else _read_info(parsed, sym, span),
            hidden=hidden,
        )
        if not hidden:
            (parent.children if parent is not None else roots).append(entry)
        stack.append(entry)
    return roots


def _count(entries: Sequence[_Entry]) -> int:
    return sum(1 + _count(e.children) for e in entries)


def _assign_ranks(roots: Sequence[_Entry], *, with_doc: bool) -> tuple[int, int]:
    """縮退で ``doc:`` 行とメンバーを 1 件ずつ落とす順位を付け、``(落とせる doc: 行の数, 落とせるメンバーの数)`` を返す
    （処理規約 8。BUG-0078）。

    * ``doc:`` 行（``with_doc`` のときの ``doc_summary`` があるもの）: 深い順、同じ深さでは文書の後ろから。最上位の doc は最後まで残る。
    * メンバー: (1) 非公開メンバー（どの深さでも）を文書の後ろから、(2) 入れ子の公開メンバーを、足す順「浅い順 → 文書順」
      （クラスごとに上から）の逆に。最上位の公開シンボルは落とさない。親を落とすと子も見えない。
    戻すとき（予算を上げたとき）はこの逆: 最上位 → 入れ子の公開メンバーを浅い順・文書順に → 非公開メンバーを文書順に。
    """
    pre: list[tuple[_Entry, int]] = []

    def walk(entries: Sequence[_Entry], depth: int) -> int:
        total = 0
        for e in entries:
            pre.append((e, depth))
            e.size = 1 + walk(e.children, depth + 1)
            total += e.size
        return total

    walk(roots, 0)
    docs = sorted(
        ((depth, i, e) for i, (e, depth) in enumerate(pre) if with_doc and e.sym.doc_summary),
        key=lambda t: (-t[0], -t[1]),
    )
    for r, (_, _, e) in enumerate(docs):
        e.doc_rank = r
    private = [e for e, _ in pre if e.private]
    nested = sorted(
        ((depth, i, e) for i, (e, depth) in enumerate(pre) if depth > 0 and not e.private),
        key=lambda t: (t[0], t[1]),
    )
    order = [*reversed(private), *(e for _, _, e in reversed(nested))]
    for r, e in enumerate(order):
        e.rank = r
    return len(docs), len(order)


def _rank_urns(roots: Sequence[_Entry]) -> int:
    """全部入りの描画で ``urn:`` 行を出したシンボル（導けない URN。§6.1 規約 2）に、落とす順位（``doc:`` 行と同じく深い順、
    同じ深さでは文書の後ろから）を付け、その数を返す。全部入りを描いた後（``memo`` に ``urn`` の行がある）に呼ぶ。"""
    pre: list[tuple[int, int, _Entry]] = []

    def walk(entries: Sequence[_Entry], depth: int) -> None:
        for e in entries:
            hit = e.memo.get("urn")
            if hit is not None and hit[0]:
                pre.append((depth, len(pre), e))
            walk(e.children, depth + 1)

    walk(roots, 0)
    for r, (_, _, e) in enumerate(sorted(pre, key=lambda t: (-t[0], -t[1]))):
        e.urn_rank = r
    return len(pre)


# --- 描画（詳細度ごと） -----------------------------------------------------------------------------------

TOP_LEVEL = "(top level)"  # 最上位で落としたメンバーの入れ物の名前（注と ``truncated_by_container``）
OTHERS = "(others)"  # ``TRUNCATED_CONTAINERS_MAX`` を超えた入れ物をまとめた名前
#: 落としたメンバーを入れ物ごとに挙げる最大数（文書順。残りは ``OTHERS`` にまとめる）
TRUNCATED_CONTAINERS_MAX = 8
#: 丸ごと取り直すときの ``max_tokens`` の目安に足す分（応答の前置き。json の封筒は約 124）
HINT_MARGIN_TOKENS = 200
#: 縮退段の完全なレコード（json つき）を覚えておく段数（パッカーの json の二分探索が同じ段を何度も求める）
SHRUNK_KEEP = 8


@dataclass(frozen=True, slots=True)
class _Detail:
    """縮退の段: ``doc:`` 行（``doc_rank < doc_cut``）・``urn:`` 行（``urn_rank < urn_cut``）・メンバー（``rank < cut``）を
    何件落とすか。"""

    doc_cut: int
    urn_cut: int
    cut: int


def _hint_max_tokens(whole: int) -> int:
    """丸ごと取り直すときの ``max_tokens`` の目安: 丸ごとの大きさ + 前置きの分を 100 単位に切り上げる。"""
    return -(-(whole + HINT_MARGIN_TOKENS) // 100) * 100


def _by_container(omitted: dict[str, int]) -> list[tuple[str, int]]:
    """入れ物ごとの落とした件数（文書順に ``TRUNCATED_CONTAINERS_MAX`` 件。残りは ``OTHERS`` にまとめる）。"""
    items = list(omitted.items())
    if len(items) <= TRUNCATED_CONTAINERS_MAX:
        return items
    head = items[: TRUNCATED_CONTAINERS_MAX - 1]
    return [*head, (OTHERS, sum(c for _, c in items[TRUNCATED_CONTAINERS_MAX - 1 :]))]


def _urn_wanted(opts: SkeletonOptions, entry: _Entry) -> bool:
    if opts.include_urns == "all":
        return True
    return opts.include_urns == "public" and not entry.private


@dataclass(frozen=True, slots=True)
class _Meter:
    """行ごとの見積もりを足して、行の列（``"\\n".join(lines)``）のトークン数を出す（縮退段の見積もり。BUG-0078）。

    既定の推定器は ``units``（切り上げ前）を行ごとに足して最後に 1 回切り上げるので、行を連結して数えた値と一致する
    （``core.tokens.TokenEstimator.NEWLINE_UNITS``）。注入された推定器は行ごとの値の和で見積もり、縮退で選んだ段は
    実際に連結して数え直す（``shrink``）。
    """

    unit: Callable[[str], int]
    newline: int
    den: int

    def tokens(self, units: int, lines: int) -> int:
        return -(-(units + self.newline * max(0, lines - 1)) // self.den)


_DEFAULT_METER = _Meter(TokenEstimator.units, TokenEstimator.NEWLINE_UNITS, TokenEstimator.UNIT_DEN)


@dataclass(slots=True)
class _Out:
    """描画の出力: 行と、その ``units``（``_Meter.unit``）の合計。

    ``meter`` が ``None`` なら行を数えない（全部入りの描画）。``json`` なら json の大きさの ``units`` も数える
    （``_emit`` の戻り値。既定の推定器のときだけ）。
    """

    lines: list[str]
    meter: _Meter | None
    units: int = 0
    json: bool = False

    def put(self, entry: _Entry, key: Any, make: Callable[..., Sequence[str]], *args: Any) -> None:
        """``entry`` の行 ``make(*args)``（``key`` ごとに 1 回だけ作り、数えるときに 1 回だけ数える）を足す。"""
        hit = entry.memo.get(key)
        if hit is None:
            hit = entry.memo[key] = [tuple(make(*args)), None]
        if self.meter is not None:
            if hit[1] is None:
                hit[1] = sum(map(self.meter.unit, hit[0]))
            self.units += hit[1]
        self.lines.extend(hit[0])


def _dumps(value: Any) -> str:
    """json の応答と同じ書き方（``mcp/render/json_out._dumps``。``syntax`` は ``mcp`` を import できないので同じ指定を持つ）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _emit(
    entries: Sequence[_Entry],
    depth: int,
    opts: SkeletonOptions,
    d: _Detail,
    out: _Out,
    *,
    rel: str,
    parent_name: str | None = None,
    want_json: bool = True,
    omitted: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[_Entry], int, int]:
    """``(json の symbols, 出した直下の Entry, 出した総数, json の units)``。``want_json=False`` なら json は作らない
    （縮退段の見積もり用）。

    json の units（``out.json`` のときだけ。それ以外は 0）は、この階層の配列の要素の大きさの和から、要素ごとに記号の連 1 つを
    引いたもの（``TokenEstimator.SYMBOL_RUN_UNITS``）。親の ``"children":[]`` を数えた値に足すと、要素を入れた配列の値になる。

    ``parent_name`` は親の行が示す修飾名（入れ子の行は ``name`` だけを出すので、``親.name`` が行から読める修飾名）。
    ``urn:`` 行は URN がそれと ``rel`` から導けないときだけ出す（仕様書 §6.1 規約 2。BUG-0069）。
    ``rank < d.cut`` のメンバーは落とし、``omitted`` に入れ物（親の修飾名、最上位は ``TOP_LEVEL``）ごとの件数を足す（子孫も数える）。
    """
    jsons: list[dict[str, Any]] = []
    emitted: list[_Entry] = []
    total = 0
    junits = 0
    for e in entries:
        if e.rank < d.cut:
            if omitted is not None:
                where = parent_name if depth > 0 and parent_name is not None else TOP_LEVEL
                omitted[where] = omitted.get(where, 0) + e.size
            continue
        sym = e.sym
        shown = sym.qualified_name if depth == 0 or parent_name is None else f"{parent_name}.{sym.name}"
        slot = len(out.lines)
        out.lines.append("")  # 見出し行は子を出した後（畳む行数が決まってから）で埋める
        doc = sym.doc_summary if opts.include_doc_summary and e.doc_rank >= d.doc_cut else None
        if doc:
            out.put(e, "doc", _doc_lines, doc, depth)
        if e.urn_rank >= d.urn_cut and _urn_wanted(opts, e):
            out.put(e, "urn", _urn_lines, e, rel, shown, depth)
        out.put(e, "attrs", _attr_lines, e, depth)
        child_json: list[dict[str, Any]] = []
        child_entries: list[_Entry] = []
        child_total = 0
        child_junits = 0
        if e.children:
            child_json, child_entries, child_total, child_junits = _emit(
                e.children, depth + 1, opts, d, out, rel=rel, parent_name=shown, want_json=want_json,
                omitted=omitted,
            )  # fmt: skip
        fold = 0
        if sym.kind in _CALLABLE_KINDS:
            fold = (
                e.lines - e.info.decorator_lines - e.info.header_lines - sum(c.lines for c in child_entries)
            )
            fold = max(fold, 0)
        head = len(out.lines)
        out.put(e, ("symbol", fold), _symbol_lines, e, depth, fold)
        out.lines[slot] = out.lines.pop(head)  # 見出し行を予約した位置へ
        if want_json:
            jsons.append(_symbol_json(e, doc, fold, child_json))
        if out.json:
            junits += _symbol_json_units(e, doc, fold) + child_junits - TokenEstimator.SYMBOL_RUN_UNITS
        emitted.append(e)
        total += 1 + child_total
    return jsons, emitted, total, junits


def _doc_lines(doc: str, depth: int) -> tuple[str]:
    return (attr_line("doc", doc, indent=depth),)


def _urn_lines(e: _Entry, rel: str, shown: str, depth: int) -> list[str]:
    return urn_lines(e.sym.urn, path=rel, qualified=shown, indent=depth)


def _attr_lines(e: _Entry, depth: int) -> list[str]:
    lines: list[str] = []
    if e.info.decorators:
        lines.append(attr_line("decorators", ", ".join(e.info.decorators), indent=depth))
    if e.info.raises:
        lines.append(attr_line("raises", ", ".join(e.info.raises), indent=depth))
    return lines


def _symbol_lines(e: _Entry, depth: int, fold: int) -> tuple[str]:
    sym = e.sym
    line = symbol_line(
        sym.kind,
        sym.qualified_name if depth == 0 else sym.name,
        indent=depth,
        modifiers=[m for m in sym.modifiers if m in MODIFIERS],
        sig=sym.signature,
        start_line=e.span[0],
        end_line=e.span[1],
        fold_lines=fold or None,
    )
    return (line,)


def _symbol_json(e: _Entry, doc: str | None, fold: int, children: list[dict[str, Any]]) -> dict[str, Any]:
    base = e.memo.get("json")
    if base is None:
        try:
            base = e.sym.to_json()
        except ValueError:  # 複数行にまたがる selection は json に出せない
            base = dataclasses.replace(e.sym, selection=None).to_json()
        e.memo["json"] = base
    # キーは値が null・空でも省かない（BUG-0065）。仕様書 §6.2 の SymbolRecord は null のキーも出す形（例の
    # ``"scip_symbol": null``）で、§11 T-02 の ``symbols[]`` はそれに ``children[]`` ・``fold_lines`` ・``decorators[]`` ・
    # ``raises[]`` を加えたもの。BUG-0055（1e1a9aa）で null・空を省いたら、``s["children"]`` で辿る受け手が壊れた。
    return {
        **base,
        "doc_summary": doc,
        "children": children,
        "fold_lines": fold,
        "decorators": list(e.info.decorators),
        "raises": list(e.info.raises),
    }


def _symbol_json_units(e: _Entry, doc: str | None, fold: int) -> int:
    """子を空にした ``_symbol_json`` の JSON の ``units``（既定の推定器。``doc`` の有無と ``fold`` ごとに 1 回だけ数える）。"""
    key = ("json-units", doc is not None, fold)
    hit = e.memo.get(key)
    if hit is None:
        hit = e.memo[key] = TokenEstimator.units(_dumps(_symbol_json(e, doc, fold, [])))
    return hit


class _Rendered(NamedTuple):
    record: Record
    units: int  # 行の ``units`` の和（``count`` のとき。``_Meter.tokens`` でトークン数にする）
    lines: int  # 行の数（ヘッダを含む）
    json_tokens: int  # json で描いたときのトークン見積もり（``count_json`` のとき）


class _LazyData(Mapping[str, Any]):
    """縮退版の ``data``（json の 1 項目）を、初めて読まれたときに作る。compact の応答では作らない（BUG-0078）。"""

    __slots__ = ("_args", "_make", "_value")

    def __init__(self, make: Callable[..., Mapping[str, Any]], *args: Any) -> None:
        self._make = make
        self._args = args
        self._value: Mapping[str, Any] | None = None

    def _get(self) -> Mapping[str, Any]:
        if self._value is None:
            self._value = self._make(*self._args)
        return self._value

    def __getitem__(self, key: str) -> Any:
        return self._get()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._get())

    def __len__(self) -> int:
        return len(self._get())


_RECORD_MEMO = BoundedMemo(max_entries=64, max_weight=40_000)  # 直近 64 ファイル・骨格の行 4 万行まで


def build_file_record(
    parsed: ParsedFile,
    symbols: list[SymbolRecord],
    *,
    rel: str,
    opts: SkeletonOptions,
    priority: int = 0,
    estimate: Callable[[str], int] | None = None,
) -> Record:
    """1 ファイルぶんの骨格レコードを作る（設計書 §7.4.4）。

    ``symbols`` は ``symbols.extract`` の結果（順不同でよい）。入れ子は範囲の包含で決める。``opts.include_private`` が
    偽なら非公開シンボル（とその子）は最初から含めない（予算による省略ではないので ``truncated`` には数えない）。
    ``estimate`` は ``shrink`` が使うトークン見積もり（既定は ``default_estimate`` = ``core`` の ``TokenEstimator``。パッカーと同じ）。

    既定の見積もりのときは、同じ内容・同じ ``symbols``（同じオブジェクト）・同じ指定の呼び出しに直近の結果を使い回す（BUG-0027）。
    返す ``Record`` は不変で、木（``ParsedFile``）を保持しない。
    """
    memo_key: tuple[Any, ...] | None = None
    if estimate is None and not parsed.has_errors:
        # ``id`` は、覚えた Record が ``symbols`` のオブジェクトを握っている間（= 覚えている間）は他と重ならない
        memo_key = (
            content_key(parsed.source),
            parsed.language_id,
            rel,
            opts,
            priority,
            tuple(map(id, symbols)),
        )
        cached = _RECORD_MEMO.get(memo_key)
        if cached is not None:
            return cached
    record = _build_file_record(parsed, symbols, rel=rel, opts=opts, priority=priority, estimate=estimate)
    if memo_key is not None:
        _RECORD_MEMO.put(memo_key, record, weight=max(1, len(record.lines)))
    return record


def _build_file_record(
    parsed: ParsedFile,
    symbols: list[SymbolRecord],
    *,
    rel: str,
    opts: SkeletonOptions,
    priority: int,
    estimate: Callable[[str], int] | None,
) -> Record:
    est = estimate or default_estimate
    # 縮退段の見積もり: 既定の推定器は行ごとの units の和で連結と同じ値。注入された推定器は行ごとの値の和（選んだ段は数え直す）
    meter = _DEFAULT_METER if estimate is None else _Meter(estimate, 0, 1)
    entries = _build_tree(parsed, symbols, include_private=opts.include_private)
    pairs = _imports_of(parsed) if opts.include_imports else []
    imports = _flat(pairs)  # json の形（items[].imports[]）
    import_groups = group_imports(pairs)  # compact の imports: 行（モジュールごと）
    imports_text = import_line(import_groups) if import_groups else None
    imports_units = meter.unit(imports_text) if imports_text is not None else 0
    spec = SUPPORT.get(parsed.language_id)
    tier = int(getattr(spec, "tier", 1))
    line_count = parsed.line_index.content_line_count
    # 木（parsed）を握らないよう、描画に要る値だけを取り出す（覚えた Record の closure が木を保持しないように）
    language_id = parsed.language_id
    sha256 = parsed.sha256
    full = _Detail(doc_cut=0, urn_cut=0, cut=0)
    total_members = _count(entries)
    # 縮退で 1 件ずつ落とせる doc: 行とメンバーの数（落とす順は ``_assign_ranks``。urn: 行の数は全部入りを描いた後に数える）
    docs, removable = _assign_ranks(entries, with_doc=opts.include_doc_summary)
    # 丸ごとのレコードの大きさ（"compact" は行、"json" は data。取り直しの目安。遅延）
    whole: dict[str, int] = {}

    def whole_tokens(form: str) -> int:
        """丸ごとの大きさ。パッカーの数え方と同じ: compact は各行の終端の改行まで（``mcp/budget._record_text``）、json は
        data の JSON と区切りの ``,``（``mcp/render/json_out.record_text``）。全文を作って数えず、行・JSON の部品の見積もりの和で
        求める（大きいファイルで全文の JSON を数えると遅い）。"""
        if form not in whole:
            if form == "compact":
                r = render(full, False, want_json=False, count=True)
                whole[form] = meter.tokens(r.units + meter.newline, r.lines)
            else:
                whole[form] = render(full, False, want_json=False, count_json=True).json_tokens
        return whole[form]

    def render(
        d: _Detail, shrinkable: bool, *, want_json: bool = True, count: bool = False, count_json: bool = False
    ) -> _Rendered:
        """段 ``d`` を描く。``want_json=False`` は縮退段の見積もり用（``data`` は空）。

        ``count`` は行のトークン見積もり（``_Meter``）を、``count_json`` は json で描いたときの大きさ（パッカーの json の数え方。
        既定の推定器のときだけ）を求める。行も JSON の部品も覚えたものを並べて足すだけなので、縮退段を何度調べても本文を
        作り直して数えない（BUG-0078）。
        """
        out = _Out([], meter if count else None, json=count_json)
        if imports_text is not None:
            out.lines.append(imports_text)
            out.units += imports_units
        by_container: dict[str, int] = {}
        jsons, _, emitted, junits = _emit(
            entries, 0, opts, d, out, rel=rel, want_json=want_json, omitted=by_container
        )
        body = out.lines
        omitted = total_members - emitted
        containers = _by_container(by_container)
        truncated: str | None = None
        units = out.units
        if d != full:
            dropped: list[str] = []
            if d.doc_cut >= docs > 0:
                dropped.append("doc")
            elif d.doc_cut:
                dropped.append(f"{d.doc_cut} doc lines")
            # ``urn`` は、全部入りに urn: 行があったときだけ「落とした」と書く（BUG-0069）。json は urn: 行を持たない
            # （どのシンボルも ``urn`` を持つ）ので json の注には書かない（書くと json の大きさが段を深くして増える）
            urns_dropped: list[str] = []
            if d.urn_cut >= urns > 0:
                urns_dropped.append("urn")
            elif d.urn_cut:
                urns_dropped.append(f"{d.urn_cut} urn lines")
            where = ": " + ", ".join(f"{c} {n}" for c, n in containers) if containers else ""

            def sentence(attrs: list[str], need: int) -> str:
                suffix = f" ({', '.join(attrs)} omitted)" if attrs else ""
                again = f"whole file: max_tokens>={need} with this path alone"
                return f"truncated: {omitted} members omitted{suffix}{where}; {again}"

            # 落としたものはカーソルでは辿れない（カーソルは次のファイルへ進む）。このファイルだけを丸ごと取り直す目安を添える。
            # 目安は形式ごとの丸ごとの大きさから（compact の注は行、json の ``truncated_note`` は data の JSON で数える）
            body.append(
                note(
                    sentence(
                        [*dropped, *urns_dropped],
                        _hint_max_tokens(whole_tokens("compact")),
                    )
                )
            )
            if count:
                units += meter.unit(body[-1])
            if want_json or count_json:
                truncated = sentence(dropped, _hint_max_tokens(whole_tokens("json")))
        header = file_block_header(rel, lines=line_count, skeleton_lines=len(body), sha12=sha256[:12])
        if count:
            units += meter.unit(header)
        data = (
            {
                "ok": True,
                "path": rel,
                "language": language_id,
                "tier": tier,
                "line_count": line_count,
                "skeleton_line_count": len(body),
                "imports": list(imports),
                "symbols": jsons,
                "file_sha256": sha256,
                "truncated_members": omitted if d != full else 0,
                "truncated_by_container": [{"container": c, "omitted": n} for c, n in containers],
                "truncated_note": truncated,
                "confidence_level": Confidence.STRUCTURAL.value,
            }
            if want_json or count_json
            else {}
        )
        json_tokens = 0
        if count_json:
            # 記号で始まり記号で終わる要素を ``"symbols":[]`` に入れた大きさ（``_emit`` の json の units。``SYMBOL_RUN_UNITS``）
            json_tokens = -(-(TokenEstimator.units(_dumps(data) + ",") + junits) // TokenEstimator.UNIT_DEN)
            if not want_json:
                data = {}
        rec = Record(
            kind="file-block",
            key=rel,
            priority=priority,
            lines=(header, *body),
            data=data,
            confidence=Confidence.STRUCTURAL,
            group=rel,
            shrink=shrink if shrinkable else None,
            shrink_json=shrink_json if shrinkable and estimate is None else None,
        )
        return _Rendered(rec, units, len(body) + 1, json_tokens)

    # 縮退の鎖（予算に依らない）: 段 t = 1, 2, … の順に 1 つずつ落とす。``doc:`` 行を 1 件ずつ（t = 1〜docs）→ 導けない URN の
    # ``urn:`` 行を 1 件ずつ（urns 段）→ メンバーを 1 件ずつ（残り removable 段）。最後の段は最上位の公開シンボルだけ
    tokens_at: dict[int, int] = {}  # 段 t の行のトークン見積もり
    json_at: dict[int, int] = {}  # 段 t を json で描いたときのトークン見積もり
    # 選ばれた段のレコード（json の data は読まれたときに作る）。直近 ``SHRUNK_KEEP`` 段
    shrunk: OrderedDict[_Detail, Record] = OrderedDict()

    def detail_at(t: int) -> _Detail:
        doc_cut = min(t, docs)
        urn_cut = min(t - doc_cut, urns)
        return _Detail(doc_cut=doc_cut, urn_cut=urn_cut, cut=t - doc_cut - urn_cut)

    def cost_at(t: int) -> int:
        if t not in tokens_at:
            r = render(detail_at(t), False, want_json=False, count=True)
            tokens_at[t] = meter.tokens(r.units, r.lines)
        return tokens_at[t]

    def json_cost_at(t: int) -> int:
        if t not in json_at:
            json_at[t] = render(detail_at(t), False, want_json=False, count_json=True).json_tokens
        return json_at[t]

    def deepest_fit(cost: Callable[[int], int], budget: int) -> int | None:
        """``cost(t) <= budget`` の最も浅い段（二分探索。調べる位置は範囲と各位置の判定だけで決まり、判定は ``budget`` に
        ついて単調なので、結果も ``budget`` について単調）。最後の段も収まらなければ ``None``。"""
        last = docs + urns + removable
        if last == 0 or cost(last) > budget:
            return None
        lo, hi = 1, last  # cost(hi) <= budget を保つ
        while lo < hi:
            mid = (lo + hi) // 2
            if cost(mid) <= budget:
                hi = mid
            else:
                lo = mid + 1
        return hi

    def json_of(d: _Detail) -> Mapping[str, Any]:
        return render(d, False).record.data

    def level(d: _Detail) -> Record:
        rec = shrunk.pop(d, None)
        if rec is None:  # json の data は読まれたときに作る（compact の応答では作らない）
            rec = dataclasses.replace(render(d, False, want_json=False).record, data=_LazyData(json_of, d))
        shrunk[d] = rec  # 直近に使った段を後ろに
        while len(shrunk) > SHRUNK_KEEP:
            shrunk.popitem(last=False)
        return rec

    def shrink(remaining_tokens: int) -> Record | None:
        """行の本文が ``remaining_tokens`` に収まる最も浅い段（BUG-0067・BUG-0078）。

        鎖の段 ``[1, last]`` を二分探索する。調べる位置は範囲と各位置の判定（``cost_at(t) <= remaining_tokens``）だけで決まり、
        判定は ``remaining_tokens`` について単調なので、結果の段も単調になる（予算を上げても深い段＝小さい版を返さない）。
        最後の段（最上位だけ）も収まらなければ ``None``。注入された推定器では、選んだ段を連結して数え直し、収まらなければ
        より深い段へ進む（行ごとの和と連結の値が違う推定器のため。進む先も ``remaining_tokens`` について単調）。
        """
        t = deepest_fit(cost_at, remaining_tokens)
        if t is None:
            return None
        rec = level(detail_at(t))
        if estimate is not None:
            last = docs + urns + removable
            while est("\n".join(rec.lines)) > remaining_tokens:
                if t >= last:
                    return None
                t += 1
                rec = level(detail_at(t))
        return rec

    def shrink_json(remaining_tokens: int) -> Record | None:
        """``shrink`` と同じ鎖で、json で描いた大きさ（``data`` の JSON と区切りの ``,``）が ``remaining_tokens`` 以下の最も
        浅い段（``Record.shrink_json``）。見積もりは既定の推定器で数え直した値と一致する（パッカーも数え直して確かめる）。"""
        t = deepest_fit(json_cost_at, remaining_tokens)
        return None if t is None else level(detail_at(t))

    # ``shrink`` ・``whole_tokens`` ・``urns`` は、呼ばれたとき（これより後）に読む
    full_record, *_ = render(full, True)
    urns = _rank_urns(entries)  # 全部入りに出した urn: 行の数（1 件ずつ落とせる）
    return full_record
