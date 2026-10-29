"""TOML のアダプタ（仕様書 §8.6 B4・§11 T-02 処理規約 5・T-05 処理規約 3、設計書 §7.4.8）。

* 骨格: 値は標準の ``tomllib`` で読み（構文の検証もここ）、行の範囲は自前の軽い走査で付ける。表（``[a.b]`` / ``[[a]]``）とキー（ドット付きキー、
  複数行の配列・文字列・インライン表）の文の開始行と終了行を数える。表の範囲は子の表とキーを含む。
  暗黙の表（宣言が無く子が 1 つだけ）は ``tool.ruff`` のように名前を連ねて 1 つの ``[Key]`` にする。
  配列は ``×要素数`` と推論した要素の型（表の配列 ``[[a]]`` も同じ）。
* 部分取得: ``toml:`` はキー経路（``project.dependencies``、``servers[1].name``、``"a.b".c``、``*``）。一致が無ければ空のリスト。
  配列の中身（インラインの要素）は、その配列を書いた文の行を指す。

``tree-sitter-toml`` は使わない（文法の読み込みに依存せず、検証済みの文書だけを走査するため）。
"""

from __future__ import annotations

import dataclasses
import json
import re
import tomllib
from collections.abc import Callable
from typing import Any

from lucianke.core.records import Record
from lucianke.syntax.adapters._common import (
    SAMPLE_LARGE,
    STREAM_BYTES,
    WILDCARD,
    DataAdapter,
    SkelNode,
    TextLines,
    build_record,
    decode_text,
    parse_key_path,
    path_text,
    shape_of,
    split_selector,
)
from lucianke.syntax.types import Match, SkeletonOptions

__all__ = ["ADAPTER", "select", "skeleton"]

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_Path = tuple[str | int, ...]


class _Layout:
    """``span[path]`` = そのパス以下（子の表・キーを含む）の行の範囲 ``[開始, 終了]``（0 始まり）。"""

    __slots__ = ("aot", "declared", "span")

    def __init__(self) -> None:
        self.span: dict[_Path, list[int]] = {}
        self.declared: set[_Path] = set()  # ``[a.b]`` / ``[[a]]`` で宣言した表のパス
        self.aot: dict[_Path, int] = {}  # 表の配列のパス → 要素数

    def extend(self, path: _Path, first: int, last: int) -> None:
        """``path`` とそのすべての接頭辞の範囲を広げる。"""
        for k in range(1, len(path) + 1):
            q = path[:k]
            cur = self.span.get(q)
            if cur is None:
                self.span[q] = [first, last]
            else:
                cur[0] = min(cur[0], first)
                cur[1] = max(cur[1], last)


def _unescape(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw


def _scan(text: str, lm: TextLines) -> _Layout:  # noqa: C901 - 字句の走査 1 本
    """検証済み（``tomllib`` が通した）文書の表・キーの行を数える。"""
    n = len(text)
    lay = _Layout()
    cur: _Path = ()

    def skip_ws(i: int) -> int:
        while i < n and text[i] in " \t":
            i += 1
        return i

    def eol(i: int) -> int:
        while i < n and text[i] not in "\r\n":
            i += 1
        return i

    def parse_key(i: int) -> tuple[tuple[str, ...], int]:
        parts: list[str] = []
        while True:
            i = skip_ws(i)
            c = text[i]
            if c == '"':
                j = i + 1
                while text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
                parts.append(_unescape(text[i + 1 : j]))
                i = j + 1
            elif c == "'":
                j = text.index("'", i + 1)
                parts.append(text[i + 1 : j])
                i = j + 1
            else:
                m = _BARE_KEY.match(text, i)
                if m is None:
                    raise ValueError(f"invalid TOML key near line {lm.line_of(i) + 1}")
                parts.append(m.group())
                i = m.end()
            i = skip_ws(i)
            if i < n and text[i] == ".":
                i += 1
                continue
            return tuple(parts), i

    def skip_string(i: int) -> int:
        """``i`` は文字列の開始の引用符。閉じた直後のオフセットを返す。"""
        q = text[i]
        if text.startswith(q * 3, i):
            j = i + 3
            while j < n:
                if q == '"' and text[j] == "\\":
                    j += 2
                    continue
                if text.startswith(q * 3, j):
                    j += 3
                    extra = 0
                    while (
                        j < n and text[j] == q and extra < 2
                    ):  # """"a"""" のように末尾に 2 つまで引用符が付く
                        j += 1
                        extra += 1
                    return j
                j += 1
            return n
        j = i + 1
        while j < n and text[j] != q:
            j += 2 if (q == '"' and text[j] == "\\") else 1
        return j + 1

    def skip_value(i: int) -> tuple[int, int]:
        """``(文の終わり〈改行の位置〉, 最後の有意な文字の位置)``。括弧の中の改行は続ける。"""
        depth = 0
        last = i
        while i < n:
            c = text[i]
            if c in "\r\n":
                if depth <= 0:
                    break
                i += 1
            elif c in " \t":
                i += 1
            elif c == "#":
                i = eol(i)
            elif c in "\"'":
                i = skip_string(i)
                last = i - 1
            else:
                if c in "[{":
                    depth += 1
                elif c in "]}":
                    depth -= 1
                last = i
                i += 1
        return i, last

    def resolve_header(parts: tuple[str, ...], is_array: bool) -> _Path:
        path: _Path = ()
        for k, p in enumerate(parts):
            path = (*path, p)
            if k == len(parts) - 1 and is_array:
                idx = lay.aot.get(path, 0)
                lay.aot[path] = idx + 1
                path = (*path, idx)
            elif path in lay.aot:  # 表の配列の途中のキーは、最後に宣言された要素の下を指す
                path = (*path, lay.aot[path] - 1)
        return path

    i = 0
    while i < n:
        i = skip_ws(i)
        if i >= n:
            break
        c = text[i]
        if c in "\r\n":
            i += 1
            continue
        if c == "#":
            i = eol(i)
            continue
        start = i
        if c == "[":
            is_array = text.startswith("[[", i)
            parts, j = parse_key(i + (2 if is_array else 1))
            j = text.index("]", j) + (2 if is_array else 1)
            cur = resolve_header(parts, is_array)
            line = lm.line_of(start)
            lay.declared.add(cur)
            lay.extend(cur, line, line)
            i = eol(j)
            continue
        parts, j = parse_key(i)
        j = skip_ws(j)
        if j >= n or text[j] != "=":
            raise ValueError(f"invalid TOML near line {lm.line_of(j) + 1}: '=' expected")
        end, last = skip_value(j + 1)
        lay.extend((*cur, *parts), lm.line_of(start), lm.line_of(last))
        i = end
    return lay


def _load(source: bytes) -> tuple[str, dict[str, Any], TextLines, _Layout]:
    text = decode_text(source)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"invalid TOML: {e}") from e
    lm = TextLines(text)
    return text, data, lm, _scan(text, lm)


def _range(lay: _Layout, path: _Path) -> tuple[int, int]:
    """``path`` の範囲。記録が無ければ（インライン配列の要素など）いちばん近い祖先の範囲。"""
    for k in range(len(path), 0, -1):
        r = lay.span.get(path[:k])
        if r is not None:
            return r[0], r[1]
    return 0, 0


def _node(name: str, v: Any, path: _Path, lay: _Layout, sample: int | None) -> SkelNode:
    s, e = _range(lay, path)
    node = SkelNode("Key", name, s, e)
    if isinstance(v, dict):
        node.children = [_node(k, x, (*path, k), lay, sample) for k, x in v.items()]
        if path not in lay.declared and len(node.children) == 1:  # 暗黙の表: tool + ruff → tool.ruff
            c = node.children[0]
            node.name = f"{name}.{c.name}"
            node.count, node.type, node.children, node.omitted = c.count, c.type, c.children, c.omitted
    elif isinstance(v, list):
        node.count = len(v)
        node.type = shape_of(v, sample=sample)
    return node


def skeleton(
    source: bytes,
    opts: SkeletonOptions,
    *,
    rel: str,
    sha256: str,
    estimate: Callable[[str], int] | None = None,
) -> Record:
    """TOML の骨格 1 ファイルブロック。"""
    _, data, _, lay = _load(source)
    notes: list[str] = []
    sample: int | None = None
    if len(source) > STREAM_BYTES:
        opts = dataclasses.replace(opts, max_depth=min(opts.max_depth, 2))
        sample = SAMPLE_LARGE
        notes.append(
            f"note: large file (> 5 MB): depth limited to 2, array types inferred from first {SAMPLE_LARGE} elements"
        )
    nodes = [_node(k, v, (k,), lay, sample) for k, v in data.items()]
    return build_record(
        source, nodes, rel=rel, sha256=sha256, language="toml", opts=opts, notes=notes, estimate=estimate
    )


def select(source: bytes, selector: str) -> list[Match]:
    """``toml:<キー経路>`` に一致する値の ``Match``（``kind="Key"``）。一致が無ければ空のリスト。"""
    expr = split_selector(selector, accepts=("toml",))
    comps = parse_key_path(expr)
    _, data, lm, lay = _load(source)
    cur: list[tuple[_Path, Any]] = [((), data)]
    for c in comps:
        nxt: list[tuple[_Path, Any]] = []
        for path, v in cur:
            if isinstance(v, dict):
                for k, x in v.items():
                    if c is WILDCARD or k == str(c):
                        nxt.append(((*path, k), x))
            elif isinstance(v, list) and (c is WILDCARD or isinstance(c, int)):
                idxs = range(len(v)) if c is WILDCARD else [int(c) + (len(v) if int(c) < 0 else 0)]  # type: ignore[arg-type]
                for i in idxs:
                    if 0 <= i < len(v):
                        nxt.append(((*path, i), v[i]))
        cur = nxt
    out: list[Match] = []
    for path, _v in cur:
        s, e = _range(lay, path)
        last = path[-1]
        name = f"[{last}]" if isinstance(last, int) else last
        out.append(lm.match("Key", name, s, e, {"path": path_text(path)}))
    return out


ADAPTER = DataAdapter("toml", (".toml",), skeleton, select, ("toml",))
