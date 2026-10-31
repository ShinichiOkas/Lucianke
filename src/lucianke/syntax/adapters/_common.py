"""Tier 3 データファイル（JSON / JSONL / YAML / TOML / XML）のアダプタの共通部品（仕様書 §8.6 B4・§11 T-02 処理規約 4・5、設計書 §7.4.8）。

* ``SkelNode`` とその描画（``build_record``）: 骨格の 1 ファイルブロック（``kind="file-block"``）を作る。
  行は ``[Key] name ×N @Ls-e`` / ``[Tag] name ×N @Ls-e``（``@L`` は 1 始まり・両端を含む）、属性行は ``type:``。
  ``max_depth`` を超える階層は ``{ ...N nodes omitted }`` に畳む。予算に入らないときの ``shrink``（深さを 1 段ずつ浅くする）も付ける。
* ``Shape``: 値から推論した型（``array<object{id:int,user:object}>``）。値そのものは骨格に出さない。
* ``TextLines``: 文字オフセット → 行と、``Match`` の組み立て。``Match.range`` は 0 始まりで、行単位（列は先頭から行末まで）。
* セレクタの接頭辞の分解（``split_selector``）とキー経路の構文解析（``parse_key_path``）。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import re
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from lucianke.core.compact_lines import file_block_header, note
from lucianke.core.model import Confidence, Kind, Position, Range, SymbolRecord, Tier
from lucianke.core.records import Record
from lucianke.core.textpos import LineIndex
from lucianke.syntax.adapters._symbols import MAX_SYMBOL_SOURCE_BYTES, data_symbols, items_symbols
from lucianke.syntax.types import Match, SkeletonOptions

STREAM_BYTES = (
    5 * 1024 * 1024
)  # 仕様書 §8.6 B4: これを超えるファイルは深さと型推論の標本を絞る（XML は構文木を作らない）
LINE_LIMIT = 200  # compact の 1 行の上限（§6.1 規約 6）。型の行はこれを超えたら末尾を … にする
MAX_FIELDS = 12  # object{...} に並べるキーの上限
SAMPLE_LARGE = 1000  # STREAM_BYTES 超のとき、配列 1 つあたり型を推論する要素数

__all__ = [
    "LINE_LIMIT",
    "SAMPLE_LARGE",
    "STREAM_BYTES",
    "WILDCARD",
    "DataAdapter",
    "SelectorError",
    "Shape",
    "SkelNode",
    "TextLines",
    "Typed",
    "UnsupportedSelectorError",
    "build_record",
    "decode_text",
    "parse_key_path",
    "path_text",
    "shape_of",
    "split_selector",
]


# --- エラー -----------------------------------------------------------------------------------


class SelectorError(ValueError):
    """セレクタの構文誤り・評価できない式（呼び出し側が ``E_INVALID_ARGUMENT`` にする）。"""


class UnsupportedSelectorError(SelectorError):
    """このファイル種別に合わない接頭辞・未対応のセレクタ（呼び出し側が ``E_UNSUPPORTED`` にする）。"""


# --- セレクタ -----------------------------------------------------------------------------------

_SCHEMES = ("xpath", "jsonpath", "jq", "toml", "yaml")


def split_selector(selector: str, *, accepts: Sequence[str]) -> str:
    """``"jsonpath:$.a"`` → ``"$.a"``。接頭辞が ``accepts`` に無ければ ``UnsupportedSelectorError``、式が空なら ``SelectorError``。"""
    scheme, sep, expr = selector.partition(":")
    scheme = scheme.strip().lower()
    if not sep or scheme not in _SCHEMES:
        raise SelectorError(
            f"selector must start with one of {', '.join(s + ':' for s in _SCHEMES)}: {selector!r}"
        )
    if scheme == "jq":
        raise UnsupportedSelectorError("jq: selectors are not supported yet (M2); use jsonpath:")
    if scheme not in accepts:
        raise UnsupportedSelectorError(f"{scheme}: selectors do not apply to this file type")
    if not expr.strip():
        raise SelectorError(f"empty selector expression: {selector!r}")
    return expr


class _Wildcard:
    __slots__ = ()

    def __repr__(self) -> str:
        return "*"


WILDCARD = _Wildcard()


def parse_key_path(expr: str) -> list[str | int | _Wildcard]:
    """``a.b[0]."c.d".*`` → ``["a", "b", 0, "c.d", WILDCARD]``。TOML・YAML のキー経路。

    区切りは ``.``。名前に ``.`` を含むときは ``"..."`` か ``'...'`` で囲む。``[n]`` は配列の添字（負も可）、``[*]`` と ``*`` は全要素。
    空の式・先頭や末尾や連続の ``.``・閉じない ``[`` ``"``・整数でない添字は ``SelectorError``。
    """
    expr = expr.strip()
    if not expr:
        raise SelectorError("empty key path")
    out: list[str | int | _Wildcard] = []
    i, n = 0, len(expr)
    need_segment = True  # 次に名前が必要（先頭と ``.`` の直後）
    while i < n:
        c = expr[i]
        if c == ".":
            if need_segment:
                raise SelectorError(f"unexpected '.' at {i} in key path {expr!r}")
            need_segment = True
            i += 1
            continue
        if c == "[":
            j = expr.find("]", i)
            if j < 0:
                raise SelectorError(f"unbalanced '[' in key path {expr!r}")
            tok = expr[i + 1 : j].strip()
            if tok == "*":
                out.append(WILDCARD)
            elif re.fullmatch(r"-?\d+", tok):
                out.append(int(tok))
            else:
                raise SelectorError(f"index must be an integer or '*': [{tok}] in {expr!r}")
            need_segment = False
            i = j + 1
            continue
        if not need_segment:
            raise SelectorError(f"expected '.' or '[' at {i} in key path {expr!r}")
        if c in "\"'":
            j = expr.find(c, i + 1)
            if j < 0:
                raise SelectorError(f"unterminated quote in key path {expr!r}")
            out.append(expr[i + 1 : j])
            i = j + 1
        else:
            j = i
            while j < n and expr[j] not in ".[":
                j += 1
            name = expr[i:j].strip()
            if not name:
                raise SelectorError(f"empty key in key path {expr!r}")
            out.append(WILDCARD if name == "*" else name)
            i = j
        need_segment = False
    if need_segment:
        raise SelectorError(f"key path ends with '.': {expr!r}")
    return out


_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")


def path_text(parts: Sequence[str | int]) -> str:
    """経路の部品を ``limits[0].currency`` の形に直す（``Match.data["path"]``）。"""
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            seg = p if _BARE.fullmatch(p) else json.dumps(p, ensure_ascii=False)
            out += f".{seg}" if out else seg
    return out


# --- テキストと行 -------------------------------------------------------------------------------

_NL = re.compile(r"\r\n|\n|\r")


def decode_text(source: bytes) -> str:
    """UTF-8（BOM は捨てる）。不正なバイトは 1 バイト = 1 文字（``LineIndex`` と同じ数え方）。"""
    text = source.decode("utf-8", "surrogateescape")
    return text[1:] if text.startswith("﻿") else text


class TextLines:
    """文字列の行表。区切りは ``\\r\\n`` / ``\\n`` / ``\\r``（``LineIndex`` と同じ）。行は 0 始まり。"""

    __slots__ = ("ends", "starts", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        starts = [0]
        ends: list[int] = []
        for m in _NL.finditer(text):
            ends.append(m.start())
            starts.append(m.end())
        ends.append(len(text))
        self.starts = starts
        self.ends = ends

    @property
    def line_count(self) -> int:
        return len(self.starts)

    def line_of(self, offset: int) -> int:
        """文字オフセットが属する行。範囲外は端に丸める。"""
        offset = max(0, min(offset, len(self.text)))
        return bisect_right(self.starts, offset) - 1

    def line_text(self, line: int) -> str:
        return self.text[self.starts[line] : self.ends[line]]

    def last_content_line(self, first: int, last: int) -> int:
        """``last`` から遡り、空行とコメントだけの行（``#`` 始まり）を除いた最後の行（``first`` を下限とする）。"""
        while last > first:
            s = self.line_text(last).strip()
            if s and not s.startswith("#"):
                break
            last -= 1
        return last

    def span(self, first: int, last: int) -> Range:
        """``first``〜``last`` 行（0 始まり・両端を含む）。列は ``first`` 行の先頭から ``last`` 行の行末まで。"""
        last = max(first, min(last, self.line_count - 1))
        return Range(Position(first, 0), Position(last, self.ends[last] - self.starts[last]))

    def slice(self, first: int, last: int) -> str:
        last = max(first, min(last, self.line_count - 1))
        return self.text[self.starts[first] : self.ends[last]]

    def match(
        self,
        kind: str,
        name: str | None,
        first: int,
        last: int,
        data: dict[str, Any] | None = None,
    ) -> Match:
        return Match(
            range=self.span(first, last),
            text=self.slice(first, last),
            kind=kind,
            name=name,
            data=data or {},
        )


# --- 型の推論 -----------------------------------------------------------------------------------

_MAX_SHAPE_DEPTH = 6


class Shape:
    """値の集まりから推論した型。スカラー名・オブジェクトのキーごとの型・配列の要素の型を持つ（共用体）。"""

    __slots__ = ("arr", "obj", "obj_n", "scalars", "seen")

    def __init__(self) -> None:
        self.scalars: list[str] = []
        self.obj: dict[str, Shape] | None = None
        self.obj_n = 0
        self.seen: dict[str, int] = {}
        self.arr: Shape | None = None

    def add(self, value: Any, depth: int = 0, *, sample: int | None = None) -> None:
        if isinstance(value, dict):
            if self.obj is None:
                self.obj = {}
            self.obj_n += 1
            if depth >= _MAX_SHAPE_DEPTH:
                return
            for k, v in value.items():
                key = str(k)
                sub = self.obj.get(key)
                if sub is None:
                    sub = self.obj[key] = Shape()
                self.seen[key] = self.seen.get(key, 0) + 1
                sub.add(v, depth + 1, sample=sample)
        elif isinstance(value, (list, tuple)):
            if self.arr is None:
                self.arr = Shape()
            if depth >= _MAX_SHAPE_DEPTH:
                return
            items = value if sample is None else value[:sample]
            for v in items:
                self.arr.add(v, depth + 1, sample=sample)
        else:
            name = _scalar_name(value)
            if name not in self.scalars:
                self.scalars.append(name)

    @property
    def empty(self) -> bool:
        return not self.scalars and self.obj is None and self.arr is None

    def render(self, level: int = 0) -> str:
        """``level`` 0: オブジェクトはキーと型を展開する。1 以上: ``object``（入れ子は畳む。例: ``user:object``）。"""
        parts = list(self.scalars)
        if self.obj is not None:
            if level == 0:
                items = list(self.obj.items())
                fields = ",".join(
                    f"{_field_name(k)}{'?' if self.seen.get(k, 0) < self.obj_n else ''}:{v.render(1)}"
                    for k, v in items[:MAX_FIELDS]
                )
                if len(items) > MAX_FIELDS:
                    fields += ",…"
                parts.append(f"object{{{fields}}}")
            else:
                parts.append("object")
        if self.arr is not None:
            inner = "unknown" if self.arr.empty else self.arr.render(level)
            parts.append(f"array<{inner}>")
        return "|".join(parts) if parts else "unknown"


def _field_name(name: str) -> str:
    return (
        name
        if name and not re.search(r"[\s,:{}<>|?]|[\x00-\x1f]", name)
        else json.dumps(name, ensure_ascii=False)
    )


class Typed:
    """型名だけを持つスカラーの代役（YAML のノードから値を作らずに型だけ渡す）。"""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def _scalar_name(v: Any) -> str:
    if isinstance(v, Typed):
        return v.name
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, _dt.datetime):
        return "datetime"
    if isinstance(v, _dt.date):
        return "date"
    if isinstance(v, _dt.time):
        return "time"
    if isinstance(v, (bytes, bytearray)):
        return "binary"
    return type(v).__name__


def shape_of(value: Any, *, sample: int | None = None) -> str:
    """値 1 つの型の文字列（配列なら ``array<...>``）。"""
    s = Shape()
    s.add(value, sample=sample)
    return s.render()


def clip(text: str, limit: int = LINE_LIMIT - 20) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- 骨格のノードと描画 --------------------------------------------------------------------------


@dataclass(slots=True)
class SkelNode:
    """骨格の 1 ノード。``start`` / ``end`` は 0 始まりの行（両端を含む）。

    ``count`` は ``×N`` に出す数（配列の要素数、XML の同名兄弟の数）。``type`` は配列の推論型、``attrs`` は XML の属性名。
    ``omitted`` は ``max_depth`` で畳んだ子孫ノードの数。``parts`` はノードの名前の素性（TOML だけ。``None`` なら ``[name]``）:
    名前を作ったキーの部品で、暗黙の表の連なり ``tool.ruff`` は ``("tool", "ruff")``、引用符つきキー ``"a.b"`` は ``("a.b",)``。
    骨格の表示（``name``）は両方 ``a.b`` だが、セレクタの式は部品から作らないと一方が解けない。
    """

    kind: str
    name: str
    start: int
    end: int
    count: int | None = None
    type: str | None = None
    attrs: tuple[str, ...] = ()
    children: list[SkelNode] = field(default_factory=list)
    omitted: int = 0
    parts: tuple[str, ...] | None = None


def symbol_items(nodes: Sequence[SkelNode]) -> list[dict[str, Any]]:
    """索引用のシンボルの項目（``items_symbols`` の入力）。T-02 の出力（``build_record`` の ``data["symbols"]``）と違い、``parts`` を持つ。"""
    return [
        {
            "name": n.name,
            "start_line": n.start + 1,
            "end_line": max(n.start, n.end) + 1,
            "parts": n.parts,
            "children": symbol_items(n.children),
        }
        for n in nodes
    ]


def count_nodes(nodes: Sequence[SkelNode]) -> int:
    return sum(1 + n.omitted + count_nodes(n.children) for n in nodes)


def limit_depth(nodes: Sequence[SkelNode], depth: int, level: int = 1) -> list[SkelNode]:
    """``depth`` を超える階層を畳んだ複製を返す（``level`` 1 が最上位）。畳んだ子孫の数は ``omitted`` に入る。"""
    out: list[SkelNode] = []
    for n in nodes:
        if level >= depth and n.children:
            out.append(dataclasses.replace(n, children=[], omitted=n.omitted + count_nodes(n.children)))
        else:
            out.append(dataclasses.replace(n, children=limit_depth(n.children, depth, level + 1)))
    return out


def _display_name(name: str) -> str:
    if not name or re.search(r"[\x00-\x1f\x7f]", name):
        name = json.dumps(name, ensure_ascii=False)
    return name if len(name) <= 120 else name[:119] + "…"


def _emit(nodes: Sequence[SkelNode], indent: int, lines: list[str]) -> list[dict[str, Any]]:
    pad = "  " * indent
    out: list[dict[str, Any]] = []
    for n in nodes:
        head = f"{pad}[{n.kind}] {_display_name(n.name)}"
        if n.count is not None:
            head += f" ×{n.count}"
        head += f" @L{n.start + 1}" if n.end <= n.start else f" @L{n.start + 1}-{n.end + 1}"
        if n.omitted:
            head += f" {{ ...{n.omitted} nodes omitted }}"
        lines.append(head)
        if n.type:
            lines.append(f"{pad}  type: {clip(n.type)}")
        if n.attrs:
            lines.append(f"{pad}  type: {clip('attrs=[' + ','.join(n.attrs) + ']')}")
        sym: dict[str, Any] = {
            "kind": n.kind,
            "name": n.name,
            "start_line": n.start + 1,
            "end_line": max(n.start, n.end) + 1,
        }
        if n.count is not None:
            sym["count"] = n.count
        if n.type:
            sym["inferred_type"] = n.type
        if n.attrs:
            sym["attrs"] = list(n.attrs)
        if n.omitted:
            sym["omitted_nodes"] = n.omitted
        sym["children"] = _emit(n.children, indent + 1, lines)
        out.append(sym)
    return out


def _bfs_rank(nodes: Sequence[SkelNode]) -> dict[int, int]:
    """表示するノード（``id``）→「浅い順 → 文書順」の連番。親は必ず子より小さい（先頭から取れば親を含む木になる）。"""
    rank: dict[int, int] = {}
    level: Sequence[SkelNode] = nodes
    while level:
        nxt: list[SkelNode] = []
        for n in level:
            rank[id(n)] = len(rank)
            nxt.extend(n.children)
        level = nxt
    return rank


def _prune(
    nodes: Sequence[SkelNode],
    keep: int,
    rank: dict[int, int],
    prefix: str | None,
    dropped: dict[str, int],
) -> list[SkelNode]:
    """``rank < keep`` のノードだけ残した複製（最上位は全部残る前提）。落とした子（と子孫）の数は親の ``omitted`` に足し、
    落とした表示中のノードの数は入れ物（親の修飾名）ごとに文書順で ``dropped`` に足す。"""
    out: list[SkelNode] = []
    for n in nodes:
        name = n.name if prefix is None else f"{prefix}.{n.name}"
        gone = 0
        kept: list[SkelNode] = []
        for c in n.children:
            if rank[id(c)] >= keep:
                gone += count_nodes([c])
                dropped[name] = dropped.get(name, 0) + _visible([c])
            else:
                kept.append(c)
        out.append(
            dataclasses.replace(n, children=_prune(kept, keep, rank, name, dropped), omitted=n.omitted + gone)
        )
    return out


def _dumps(value: Any) -> str:
    """json の応答と同じ書き方（``mcp/render/json_out._dumps``。``syntax`` は ``mcp`` を import できない）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def build_record(
    source: bytes,
    nodes: Sequence[SkelNode],
    *,
    rel: str,
    sha256: str,
    language: str,
    opts: SkeletonOptions,
    notes: Sequence[str] = (),
    estimate: Callable[[str], int] | None = None,
) -> Record:
    """骨格 1 ファイルぶんの ``Record``（``kind="file-block"``）。

    ``data`` は T-02 の ``items[]`` の 1 項目の形（``symbols[]`` の行は 1 始まり）。``shrink`` は予算に入らないときの縮退版:
    表示しているノードを「浅い順 → 文書順」の逆（深いもの・後ろのものから）に **1 件ずつ** 落とした段のうち、収まる最も浅いもの
    （最上位は残す。段の鎖は予算に依らないので結果は予算に単調。BUG-0084。コードの骨格と同じ ``- truncated:`` の注を付ける）。
    ``shrink_json`` は同じ鎖を json の大きさで選ぶ（既定の推定器のときだけ）。
    """
    # 遅延 import（skeleton が adapters を使っても循環しない）
    from lucianke.syntax.skeleton import (
        HINT_MARGIN_TOKENS,
        OTHERS,
        TOP_LEVEL,
        TRUNCATED_CONTAINERS_MAX,
        default_estimate,
    )

    est = estimate or default_estimate
    line_count = LineIndex(source).content_line_count
    visible = limit_depth(nodes, max(1, opts.max_depth))
    rank = _bfs_rank(visible)
    steps = len(rank) - len(visible)  # 1 件ずつ落とせるノードの数（最上位は残す）
    wholes: dict[str, int] = {}  # 丸ごとの大きさ（形式ごと。取り直しの目安）

    def hint(form: str) -> int:
        if form not in wholes:
            full = render(0, False)
            text = _dumps(full.data) + "," if form == "json" else "\n".join(full.lines) + "\n"
            wholes[form] = est(text)
        return -(-(wholes[form] + HINT_MARGIN_TOKENS) // 100) * 100

    def sentence(omitted: int, containers: dict[str, int], form: str) -> str:
        items = [(TOP_LEVEL if k == "" else k, v) for k, v in containers.items()]
        if len(items) > TRUNCATED_CONTAINERS_MAX:
            rest = sum(c for _, c in items[TRUNCATED_CONTAINERS_MAX - 1 :])
            items = [*items[: TRUNCATED_CONTAINERS_MAX - 1], (OTHERS, rest)]
        where = ": " + ", ".join(f"{c} {n}" for c, n in items) if items else ""
        again = f"whole file: max_tokens>={hint(form)} with this path alone"
        return f"truncated: {omitted} members omitted{where}; {again}"

    def render(t: int, with_json_note: bool = True) -> Record:
        """``t`` 件落とした段（0 は丸ごと）。``with_json_note=False`` は行の大きさを見積もるとき（json の注の目安を作らない）。"""
        dropped: dict[str, int] = {}
        shown = visible if t == 0 else _prune(visible, len(rank) - t, rank, None, dropped)
        body = [note(n) for n in notes]
        symbols = _emit(shown, 0, body)
        omitted = sum(dropped.values())
        json_note: str | None = None
        if t:
            body.append(note(sentence(omitted, dropped, "compact")))
            if with_json_note:
                json_note = sentence(omitted, dropped, "json")
        header = file_block_header(rel, lines=line_count, skeleton_lines=len(body), sha12=sha256)
        data = {
            "ok": True,
            "path": rel,
            "language": language,
            "tier": 3,
            "line_count": line_count,
            "skeleton_line_count": len(body),
            "file_sha256": sha256,
            "imports": [],
            "symbols": symbols,
            "truncated_members": omitted,
            "truncated_by_container": [
                {"container": TOP_LEVEL if k == "" else k, "omitted": v} for k, v in dropped.items()
            ],
            "truncated_note": json_note,
            "confidence_level": Confidence.STRUCTURAL.value,
        }
        return Record(
            kind="file-block",
            key=rel,
            priority=0,
            lines=(header, *body),
            data=data,
            confidence=Confidence.STRUCTURAL,
            group=rel,
            shrink=shrink if (t == 0 and steps > 0) else None,
            shrink_json=shrink_json if (t == 0 and steps > 0 and estimate is None) else None,
        )

    costs: dict[int, int] = {}
    json_costs: dict[int, int] = {}

    def cost(t: int) -> int:
        if t not in costs:
            costs[t] = est("\n".join(render(t, False).lines))
        return costs[t]

    def json_cost(t: int) -> int:
        if t not in json_costs:
            json_costs[t] = est(_dumps(render(t).data) + ",")
        return json_costs[t]

    def shallowest(size: Callable[[int], int], budget: int) -> int | None:
        """``size(t) <= budget`` の最も浅い段 ``t``（二分探索。調べる位置は範囲と各位置の判定だけで決まり、判定は ``budget`` に
        ついて単調なので、結果の段も ``budget`` について単調）。最も深い段も収まらなければ ``None``。"""
        if size(steps) > budget:
            return None
        lo, hi = 1, steps  # size(hi) <= budget を保つ
        while lo < hi:
            mid = (lo + hi) // 2
            if size(mid) <= budget:
                hi = mid
            else:
                lo = mid + 1
        return hi

    def shrink(remaining_tokens: int) -> Record | None:
        t = shallowest(cost, remaining_tokens)
        return None if t is None else render(t)

    def shrink_json(remaining_tokens: int) -> Record | None:
        t = shallowest(json_cost, remaining_tokens)
        return None if t is None else render(t)

    return render(0)


def _visible(nodes: Sequence[SkelNode]) -> int:
    return sum(1 + _visible(n.children) for n in nodes)


class DataAdapter:
    """``FormatAdapter``（``syntax.support``）の実装。``language_id`` / ``extensions`` と 2 つの関数を束ねる。

    ``skeleton`` は ``rel`` / ``sha256`` の受け口が Protocol に無いため、``rel`` は空・``sha256`` は ``source`` から計算する。
    呼び出し側（T-02）は ``skeleton_record`` に ``rel`` / ``sha256`` を渡して呼ぶこと。置換は M2。

    ``symbols`` は索引（``engine.indexer``）が拾うシンボル（骨格の最上位の節。``_symbols`` の docstring を参照）。
    ``symbol_kind`` が ``None`` のアダプタ（JSONL）はシンボルを持たない。``symbol_depth`` は拾う階層（XML だけ 2）。
    ``symbol_nodes_fn`` は骨格のノード（``SkelNode``。``parts`` つき）を直接返す関数で、渡すとシンボルはそこから作る（T-02 の出力は変えない）。
    """

    tier = Tier.T3

    def __init__(
        self,
        language_id: str,
        extensions: tuple[str, ...],
        skeleton_fn: Callable[..., Record],
        select_fn: Callable[[bytes, str], list[Match]],
        selectors: tuple[str, ...],
        *,
        symbol_kind: Kind | None = None,
        symbol_depth: int = 1,
        symbol_nodes_fn: Callable[[bytes], list[SkelNode]] | None = None,
    ) -> None:
        self.language_id = language_id
        self.extensions = extensions
        self.selectors = selectors  # 受け付けるセレクタの接頭辞（``"jsonpath"`` など。``jq`` は M2）
        self.symbol_kind = symbol_kind
        self.symbol_depth = symbol_depth
        self._skeleton = skeleton_fn
        self._select = select_fn
        self._symbol_nodes = symbol_nodes_fn  # 名前の素性（``SkelNode.parts``）が要る形式（TOML）だけ。骨格の Record を経由しない

    def skeleton(self, source: bytes, opts: SkeletonOptions) -> list[Record]:
        return [self._skeleton(source, opts, rel="", sha256=hashlib.sha256(source).hexdigest())]

    def skeleton_record(
        self,
        source: bytes,
        opts: SkeletonOptions,
        *,
        rel: str,
        sha256: str,
        estimate: Callable[[str], int] | None = None,
        python_symbols: Callable[[bytes], Sequence[Any]] | None = None,  # noqa: ARG002 - ipynb だけが使う（受け口をそろえる）
    ) -> Record:
        """T-02 の 1 ファイルぶん。``estimate`` は予算に入らないときの縮退（``shrink``）の見積もり。壊れたファイルは ``ValueError``。"""
        return self._skeleton(source, opts, rel=rel, sha256=sha256, estimate=estimate)

    def symbols(self, source: bytes, *, rel: str, workspace_id: str) -> list[SymbolRecord]:
        """索引用のシンボル（最上位の節）。壊れたファイル・大きすぎるファイル（``MAX_SYMBOL_SOURCE_BYTES`` 超）・シンボルを持たない形式は空（呼び手は ``files`` 行だけにする）。"""
        if self.symbol_kind is None or len(source) > MAX_SYMBOL_SOURCE_BYTES:
            return []
        try:
            if self._symbol_nodes is not None:
                items = symbol_items(limit_depth(self._symbol_nodes(source), self.symbol_depth))
                return items_symbols(
                    items,
                    scheme=self.selectors[0],
                    kind=self.symbol_kind,
                    rel=rel,
                    workspace_id=workspace_id,
                    depth=self.symbol_depth,
                )
            record = self._skeleton(
                source,
                SkeletonOptions(max_depth=self.symbol_depth),
                rel=rel,
                sha256=hashlib.sha256(source).hexdigest(),
            )
        except (ValueError, RecursionError):  # 各アダプタは構文の誤りを ValueError にそろえる
            return []
        return data_symbols(
            record,
            scheme=self.selectors[0],
            kind=self.symbol_kind,
            rel=rel,
            workspace_id=workspace_id,
            depth=self.symbol_depth,
        )

    def select(self, source: bytes, selector: str) -> list[Match]:
        return self._select(source, selector)

    def replace(self, source: bytes, selector: str, new: bytes) -> bytes:
        raise NotImplementedError("editing structured data by selector is M2")

    def __repr__(self) -> str:
        return f"DataAdapter({self.language_id!r})"
