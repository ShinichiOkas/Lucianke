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
from lucianke.syntax.adapters._symbols import MAX_SYMBOL_SOURCE_BYTES, data_symbols
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
    ``omitted`` は ``max_depth`` で畳んだ子孫ノードの数。
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

    ``data`` は T-02 の ``items[]`` の 1 項目の形（``symbols[]`` の行は 1 始まり）。``shrink`` は深さを 1 段ずつ浅くした版を返す。
    """
    line_count = LineIndex(source).content_line_count
    depth = max(1, opts.max_depth)
    total = count_nodes(limit_depth(nodes, 10_000))  # 畳まない全ノード数（truncated_members の基準）

    def render(d: int, *, shrunk: bool) -> Record:
        shown = limit_depth(nodes, d)
        body = [note(n) for n in notes]
        symbols = _emit(shown, 0, body)
        truncated = 0
        if shrunk:
            truncated = max(0, total - _visible(shown))
            body.append(note(f"truncated: {truncated} members omitted (depth limited to {d})"))
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
            "truncated_members": truncated,
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
            shrink=shrink if (not shrunk and depth > 1) else None,
        )

    def shrink(remaining_tokens: int) -> Record | None:
        if estimate is None:
            # 遅延 import（skeleton が adapters を使っても循環しない）
            from lucianke.syntax.skeleton import default_estimate

            est = default_estimate
        else:
            est = estimate
        for d in range(depth - 1, 0, -1):
            rec = render(d, shrunk=True)
            if est("\n".join(rec.lines)) <= remaining_tokens:
                return rec
        return None

    return render(depth, shrunk=False)


def _visible(nodes: Sequence[SkelNode]) -> int:
    return sum(1 + _visible(n.children) for n in nodes)


class DataAdapter:
    """``FormatAdapter``（``syntax.support``）の実装。``language_id`` / ``extensions`` と 2 つの関数を束ねる。

    ``skeleton`` は ``rel`` / ``sha256`` の受け口が Protocol に無いため、``rel`` は空・``sha256`` は ``source`` から計算する。
    呼び出し側（T-02）は ``skeleton_record`` に ``rel`` / ``sha256`` を渡して呼ぶこと。置換は M2。

    ``symbols`` は索引（``engine.indexer``）が拾うシンボル（骨格の最上位の節。``_symbols`` の docstring を参照）。
    ``symbol_kind`` が ``None`` のアダプタ（JSONL）はシンボルを持たない。``symbol_depth`` は拾う階層（XML だけ 2）。
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
    ) -> None:
        self.language_id = language_id
        self.extensions = extensions
        self.selectors = selectors  # 受け付けるセレクタの接頭辞（``"jsonpath"`` など。``jq`` は M2）
        self.symbol_kind = symbol_kind
        self.symbol_depth = symbol_depth
        self._skeleton = skeleton_fn
        self._select = select_fn

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
