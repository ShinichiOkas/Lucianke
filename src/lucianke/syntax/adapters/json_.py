"""JSON / JSONL のアダプタ（仕様書 §8.6 B4・§11 T-02 処理規約 5・T-05 処理規約 3、設計書 §7.4.8）。

* 骨格: オブジェクトのキーを ``[Key]`` の階層に、配列は ``×要素数`` と推論した要素の型（``array<object{id:int,user:object}>``）にする。
  配列の中身は展開しない（型で示す）。値そのものは出さない。JSONL は先頭 100 レコードから推論し、その旨を ``- note:`` に書く。
* 部分取得: ``jsonpath:``（``jsonpath-ng`` の拡張構文。フィルタ ``[?(@.max > 5)]`` も使える）。``jq:`` は M2。
  JSONL は「全レコードの配列」に対して評価する（``$[0].id``、``$[*].id``）。

行番号は自前の位置つき再帰下降パーサで付ける（標準の ``json`` は位置を返さない）。値と構文の検証は標準の ``json`` が先に行う。
範囲はキー（配列の要素なら値）の先頭の行から値の最後の行まで。5 MB 超は深さを 2 に、型の標本を配列ごと 1000 要素に絞る。
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from typing import Any

from jsonpath_ng import jsonpath as _jp
from jsonpath_ng.ext import parse as _jsonpath_parse

from lucianke.core.records import Record
from lucianke.syntax.adapters._common import (
    SAMPLE_LARGE,
    STREAM_BYTES,
    DataAdapter,
    SelectorError,
    Shape,
    SkelNode,
    TextLines,
    build_record,
    decode_text,
    path_text,
    shape_of,
    split_selector,
)
from lucianke.syntax.types import Match, SkeletonOptions

__all__ = ["ADAPTER", "JSONL_ADAPTER", "select", "select_jsonl", "skeleton", "skeleton_jsonl"]

JSONL_SAMPLE = 100  # 仕様書 T-02 処理規約 5: JSONL は先頭 100 レコードから推論する
_MAX_NEST = 400

# --- 位置つきパーサ -----------------------------------------------------------------------------

_WS = re.compile(r"[ \t\n\r]*")
_STR = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"', re.DOTALL)
_NUM = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
_LIT = re.compile(r"true|false|null|NaN|-?Infinity")


class _JNode:
    """``kind``: ``"object"`` | ``"array"`` | ``"scalar"``。``start`` / ``end`` は文字オフセット（``end`` は含まない）。"""

    __slots__ = ("end", "items", "kind", "members", "start")

    def __init__(self, kind: str, start: int) -> None:
        self.kind = kind
        self.start = start
        self.end = start
        self.members: list[tuple[str, int, _JNode]] = []  # (キー, キーの開始オフセット, 値)
        self.items: list[_JNode] = []


def _parse_positions(text: str) -> _JNode:
    n = len(text)

    def ws(i: int) -> int:
        m = _WS.match(text, i)
        return m.end() if m else i

    def fail(i: int, what: str) -> ValueError:
        line = text.count("\n", 0, i) + 1
        return ValueError(f"invalid JSON near line {line}: {what}")

    def value(i: int, depth: int) -> tuple[_JNode, int]:
        if depth > _MAX_NEST:
            raise ValueError("JSON nesting too deep")
        i = ws(i)
        c = text[i : i + 1]
        if c == "{":
            node = _JNode("object", i)
            i = ws(i + 1)
            if text[i : i + 1] == "}":
                node.end = i + 1
                return node, i + 1
            while True:
                i = ws(i)
                m = _STR.match(text, i)
                if m is None:
                    raise fail(i, "object key expected")
                tok = m.group()
                key = json.loads(tok) if "\\" in tok else tok[1:-1]
                kstart = i
                i = ws(m.end())
                if text[i : i + 1] != ":":
                    raise fail(i, "':' expected")
                child, i = value(i + 1, depth + 1)
                node.members.append((key, kstart, child))
                i = ws(i)
                sep = text[i : i + 1]
                if sep == ",":
                    i += 1
                    continue
                if sep == "}":
                    node.end = i + 1
                    return node, i + 1
                raise fail(i, "',' or '}' expected")
        if c == "[":
            node = _JNode("array", i)
            i = ws(i + 1)
            if text[i : i + 1] == "]":
                node.end = i + 1
                return node, i + 1
            while True:
                child, i = value(i, depth + 1)
                node.items.append(child)
                i = ws(i)
                sep = text[i : i + 1]
                if sep == ",":
                    i += 1
                    continue
                if sep == "]":
                    node.end = i + 1
                    return node, i + 1
                raise fail(i, "',' or ']' expected")
        if c == '"':
            m = _STR.match(text, i)
            if m is None:
                raise fail(i, "unterminated string")
            node = _JNode("scalar", i)
            node.end = m.end()
            return node, m.end()
        m = _NUM.match(text, i) or _LIT.match(text, i)
        if m is None:
            raise fail(i, "value expected")
        node = _JNode("scalar", i)
        node.end = m.end()
        return node, m.end()

    root, end = value(0, 0)
    if ws(end) != n:
        raise fail(end, "extra data")
    return root


def _load(source: bytes) -> tuple[str, Any, _JNode]:
    text = decode_text(source)
    try:
        data = json.loads(text)
        root = _parse_positions(text)
    except RecursionError as e:
        raise ValueError("JSON nesting too deep") from e
    return text, data, root


# --- 骨格 ---------------------------------------------------------------------------------------


def _nodes(jn: _JNode, val: Any, lm: TextLines, sample: int | None) -> list[SkelNode]:
    out: list[SkelNode] = []
    for key, kstart, child in jn.members:
        v = val.get(key) if isinstance(val, dict) else None
        node = SkelNode("Key", key, lm.line_of(kstart), lm.line_of(child.end - 1))
        if child.kind == "array":
            node.count = len(child.items)
            node.type = shape_of(v, sample=sample)
        elif child.kind == "object":
            node.children = _nodes(child, v, lm, sample)
        out.append(node)
    return out


def skeleton(
    source: bytes,
    opts: SkeletonOptions,
    *,
    rel: str,
    sha256: str,
    estimate: Callable[[str], int] | None = None,
) -> Record:
    """JSON の骨格 1 ファイルブロック。``rel`` が ``.jsonl`` / ``.ndjson`` なら JSONL として扱う。"""
    if rel.lower().endswith((".jsonl", ".ndjson")):
        return skeleton_jsonl(source, opts, rel=rel, sha256=sha256, estimate=estimate)
    text, data, root = _load(source)
    lm = TextLines(text)
    notes: list[str] = []
    sample: int | None = None
    if len(source) > STREAM_BYTES:
        opts = dataclasses.replace(opts, max_depth=min(opts.max_depth, 2))
        sample = SAMPLE_LARGE
        notes.append(
            f"note: large file (> 5 MB): depth limited to 2, array types inferred from first {SAMPLE_LARGE} elements"
        )
    if root.kind == "object":
        nodes = _nodes(root, data, lm, sample)
    else:
        node = SkelNode("Key", "$", lm.line_of(root.start), lm.line_of(root.end - 1))
        if root.kind == "array":
            node.count = len(root.items)
        node.type = shape_of(data, sample=sample)
        nodes = [node]
    return build_record(
        source, nodes, rel=rel, sha256=sha256, language="json", opts=opts, notes=notes, estimate=estimate
    )


def skeleton_jsonl(
    source: bytes,
    opts: SkeletonOptions,
    *,
    rel: str,
    sha256: str,
    estimate: Callable[[str], int] | None = None,
) -> Record:
    """JSONL の骨格。先頭 100 レコードから型を推論し、全レコード数を ``×N`` に出す。"""
    lm = TextLines(decode_text(source))
    shape = Shape()
    total = 0
    first = last = 0
    for ln in range(lm.line_count):
        line = lm.line_text(ln)
        if not line.strip():
            continue
        if total == 0:
            first = ln
        last = ln
        total += 1
        if total <= JSONL_SAMPLE:
            try:
                shape.add(json.loads(line))
            except ValueError as e:
                raise ValueError(f"invalid JSON at line {ln + 1}: {e}") from e
    nodes: list[SkelNode] = []
    if total:
        outer = Shape()
        outer.arr = shape
        nodes.append(SkelNode("Key", "$", first, last, count=total, type=outer.render()))
    notes = [f"note: schema inferred from first {JSONL_SAMPLE} records"]
    return build_record(
        source, nodes, rel=rel, sha256=sha256, language="jsonl", opts=opts, notes=notes, estimate=estimate
    )


# --- 部分取得 -----------------------------------------------------------------------------------


def _compile(expr: str) -> Any:
    try:
        return _jsonpath_parse(expr)
    except Exception as e:  # jsonpath-ng はレクサ・パーサの誤りを複数の例外型で投げる
        raise SelectorError(f"invalid jsonpath {expr!r}: {e}") from e


def _find(compiled: Any, data: Any, expr: str) -> list[Any]:
    try:
        return list(compiled.find(data))
    except Exception as e:
        raise SelectorError(f"jsonpath {expr!r} could not be evaluated: {e}") from e


def _components(m: Any) -> list[str | int]:
    """一致から根までの経路（根は含まない）。"""
    comps: list[str | int] = []
    cur = m
    while cur is not None:
        p = cur.path
        if isinstance(p, _jp.Fields):
            comps.append(p.fields[0])
        elif isinstance(p, _jp.Index):
            comps.append(p.indices[0])
        cur = cur.context
    comps.reverse()
    return comps


def _locate(root: _JNode, comps: list[str | int]) -> tuple[int, int] | None:
    """経路が指す値の文字範囲。オブジェクトのメンバーはキーの先頭から値の終わりまで。"""
    node = root
    start, end = root.start, root.end
    for c in comps:
        if node.kind == "object" and isinstance(c, str):
            found = [(ks, ch) for k, ks, ch in node.members if k == c]
            if not found:
                return None
            kstart, node = found[-1]  # 重複キーは標準の json と同じく最後が勝つ
            start, end = kstart, node.end
        elif node.kind == "array" and isinstance(c, int):
            try:
                node = node.items[c]
            except IndexError:
                return None
            start, end = node.start, node.end
        else:
            return None
    return start, end


def _name_of(comps: list[str | int]) -> str | None:
    if not comps:
        return "$"
    last = comps[-1]
    return f"[{last}]" if isinstance(last, int) else last


def select(source: bytes, selector: str) -> list[Match]:
    """``jsonpath:<式>`` に一致する値の ``Match``（``kind="Key"``）を文書順に返す。範囲は元ファイルの行（0 始まり）。

    構文の誤り・評価できない式は ``SelectorError``（``ValueError``）、``jq:`` と他形式の接頭辞は ``UnsupportedSelectorError``。
    """
    expr = split_selector(selector, accepts=("jsonpath",))
    compiled = _compile(expr)
    text, data, root = _load(source)
    lm = TextLines(text)
    out: list[Match] = []
    for m in _find(compiled, data, expr):
        comps = _components(m)
        span = _locate(root, comps)
        if span is None:
            continue
        first, last = lm.line_of(span[0]), lm.line_of(span[1] - 1)
        out.append(lm.match("Key", _name_of(comps), first, last, {"path": "$" + _suffix(comps)}))
    return out


def _suffix(comps: list[str | int]) -> str:
    text = path_text(comps)
    return "" if not text else (text if text.startswith("[") else "." + text)


def select_jsonl(source: bytes, selector: str) -> list[Match]:
    """JSONL: 全レコードの配列に ``jsonpath:`` を当てる。範囲はレコードの行（1 レコード 1 行）。"""
    expr = split_selector(selector, accepts=("jsonpath",))
    compiled = _compile(expr)
    lm = TextLines(decode_text(source))
    records: list[Any] = []
    lines: list[int] = []
    for ln in range(lm.line_count):
        line = lm.line_text(ln)
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError as e:
            raise ValueError(f"invalid JSON at line {ln + 1}: {e}") from e
        lines.append(ln)
    out: list[Match] = []
    for m in _find(compiled, records, expr):
        comps = _components(m)
        if comps:
            idx = comps[0]
            if not isinstance(idx, int):
                continue
            try:
                ln = lines[idx]
            except IndexError:
                continue
            first = last = ln
        elif lines:
            first, last = lines[0], lines[-1]
        else:
            continue
        out.append(lm.match("Key", _name_of(comps), first, last, {"path": "$" + _suffix(comps)}))
    return out


ADAPTER = DataAdapter("json", (".json",), skeleton, select, ("jsonpath",))
JSONL_ADAPTER = DataAdapter("jsonl", (".jsonl", ".ndjson"), skeleton_jsonl, select_jsonl, ("jsonpath",))
