"""XML のアダプタ（仕様書 §8.6 B4・§11 T-02 処理規約 4・T-05 処理規約 3、設計書 §7.4.8）。

* 骨格: タグの階層を ``[Tag]`` で、同名の兄弟は ``×N`` に集約する（集約した兄弟の子・属性名は和集合）。属性は名前だけ
  ``type: attrs=[name,tests,...]`` に出す。テキストと属性の値は出さない。``max_depth`` を超える階層は ``{ ...N nodes omitted }``。
  構文木は作らず、標準の ``xml.parsers.expat`` を 1 回流して集約表だけを持つ（メモリは別々のタグ経路の数に比例。5 MB 超でも同じ）。
* 部分取得: ``xpath:``。``lxml`` は使わず ``xml.etree.ElementTree`` が評価できる範囲（``.//tag[@attr]``、``tag[child]``、``[@a='v']``、
  ``[child='text']``、``[n]``）に限る。先頭の ``//`` は ``.//`` に、``/`` は文書ノードからの相対に読み替える。
  評価できない式は ``SelectorError``。行は ``expat`` が数える開始タグの行と、閉じタグ（空要素は開始タグ）の ``>`` の行。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from xml.parsers import expat

from lucianke.core.model import Kind
from lucianke.core.records import Record
from lucianke.syntax.adapters._common import (
    STREAM_BYTES,
    DataAdapter,
    SelectorError,
    SkelNode,
    TextLines,
    build_record,
    decode_text,
    split_selector,
)
from lucianke.syntax.types import Match, SkeletonOptions

__all__ = ["ADAPTER", "select", "skeleton"]

_MAX_AGG_DEPTH = 64  # これより深い要素は個別に集約せず、64 段目のノードの ``omitted`` に数える
_TAG_END = re.compile(rb"""(?:[^>"']|"[^"]*"|'[^']*')*>""")
_NL_B = re.compile(rb"\r\n|\n|\r")


def _parser() -> expat.XMLParserType:
    p = expat.ParserCreate()
    p.ordered_attributes = True

    def no_entities(*_: object) -> None:
        raise ValueError("XML entity declarations are not supported")

    p.EntityDeclHandler = no_entities
    return p


def _close_line(source: bytes, parser: expat.XMLParserType) -> int:
    """終了イベントが指す要素の最後の ``>`` の行（0 始まり）。

        空要素（``<e/>``）の終了イベントはタグの直後を指し、行番号がそのまま ``/>`` の行になる。通常の閉じタグ（``</e>``）は
        閉じタグの先頭を指すので、``</e
    >`` のように ``>`` が次の行にある場合のために ``>`` までの改行を数える。
    """
    line0 = parser.CurrentLineNumber - 1
    idx = parser.CurrentByteIndex
    if source[idx : idx + 2] != b"</":
        return line0
    m = _TAG_END.match(source, idx)
    if m is None:  # UTF-16 など ASCII 互換でない入力
        return line0
    return line0 + len(_NL_B.findall(source, idx, m.end()))


# --- 骨格 ---------------------------------------------------------------------------------------


class _Agg:
    """同名の兄弟を集約した 1 ノード。"""

    __slots__ = ("attrs", "children", "count", "end", "omitted", "start", "tag")

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.count = 0
        self.start = 1 << 60
        self.end = -1
        self.attrs: dict[str, None] = {}  # 出現順の集合
        self.children: dict[str, _Agg] = {}
        self.omitted = 0


def _aggregate(source: bytes) -> _Agg:
    p = _parser()
    root = _Agg("")
    # (集約ノード, 開始行, 深さ, 実体か)。深さが上限を超えた要素は実体ではなく、直近の集約ノードの omitted に数える
    stack: list[tuple[_Agg, int, int, bool]] = [(root, 0, 0, True)]

    def start(name: str, attrs: list[str]) -> None:
        parent, _, depth, _ = stack[-1]
        line = p.CurrentLineNumber - 1
        if depth >= _MAX_AGG_DEPTH:
            parent.omitted += 1
            stack.append((parent, line, depth, False))
            return
        agg = parent.children.get(name)
        if agg is None:
            agg = parent.children[name] = _Agg(name)
        agg.count += 1
        agg.start = min(agg.start, line)
        for a in attrs[0::2]:
            agg.attrs.setdefault(a, None)
        stack.append((agg, line, depth + 1, True))

    def end(_name: str) -> None:
        agg, line, _, real = stack.pop()
        if real:
            agg.end = max(agg.end, _close_line(source, p), line)

    p.StartElementHandler = start
    p.EndElementHandler = end
    try:
        p.Parse(source, True)
    except expat.ExpatError as e:
        raise ValueError(f"invalid XML: {e}") from e
    return root


def _to_nodes(agg: _Agg) -> list[SkelNode]:
    return [
        SkelNode(
            "Tag",
            c.tag,
            c.start,
            c.end,
            count=c.count if c.count > 1 else None,
            attrs=tuple(c.attrs),
            children=_to_nodes(c),
            omitted=c.omitted,
        )
        for c in agg.children.values()
    ]


def skeleton(
    source: bytes,
    opts: SkeletonOptions,
    *,
    rel: str,
    sha256: str,
    estimate: Callable[[str], int] | None = None,
) -> Record:
    """XML の骨格 1 ファイルブロック。"""
    nodes = _to_nodes(_aggregate(source))
    notes = []
    if len(source) > STREAM_BYTES:
        notes.append("note: large file (> 5 MB): streamed without building a tree")
    return build_record(
        source, nodes, rel=rel, sha256=sha256, language="xml", opts=opts, notes=notes, estimate=estimate
    )


# --- 部分取得 -----------------------------------------------------------------------------------


def _build_tree(source: bytes) -> tuple[ET.Element, dict[ET.Element, tuple[int, int]]]:
    """``(文書ノード, 要素 → (開始行, 終了行))``。文書ノードは根要素を子に持つ仮の要素。"""
    p = _parser()
    p.buffer_text = True
    doc = ET.Element("#document")
    stack: list[ET.Element] = [doc]
    spans: dict[ET.Element, tuple[int, int]] = {}
    starts: list[int] = []
    last_closed: list[ET.Element | None] = [None]

    def start(name: str, attrs: list[str]) -> None:
        el = ET.SubElement(stack[-1], name, dict(zip(attrs[0::2], attrs[1::2], strict=True)))
        stack.append(el)
        starts.append(p.CurrentLineNumber - 1)
        last_closed[0] = None

    def end(_name: str) -> None:
        el = stack.pop()
        line = starts.pop()
        spans[el] = (line, max(line, _close_line(source, p)))
        last_closed[0] = el

    def chars(data: str) -> None:
        target = last_closed[0]
        if target is not None:
            target.tail = (target.tail or "") + data
        else:
            cur = stack[-1]
            cur.text = (cur.text or "") + data

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.CharacterDataHandler = chars
    try:
        p.Parse(source, True)
    except expat.ExpatError as e:
        raise ValueError(f"invalid XML: {e}") from e
    return doc, spans


def _normalize(expr: str) -> str:
    e = expr.strip()
    return "." + e if e.startswith("/") else e


def select(source: bytes, selector: str) -> list[Match]:
    """``xpath:<式>`` に一致する要素の ``Match``（``kind="Tag"``）を文書順に返す。範囲は元ファイルの行（0 始まり）。"""
    expr = split_selector(selector, accepts=("xpath",))
    doc, spans = _build_tree(source)
    path = _normalize(expr)
    try:
        found = doc.findall(path)
    except (SyntaxError, KeyError, ValueError, TypeError, AttributeError) as e:
        raise SelectorError(
            f"xpath {expr!r} is not supported (ElementTree subset: .//tag[@attr], tag[child], [@a='v'], [n]): {e}"
        ) from e
    lm = TextLines(decode_text(source))
    out: list[Match] = []
    for el in found:
        span = spans.get(el)
        if span is None:
            continue
        out.append(lm.match("Tag", el.tag, span[0], span[1], {"attrs": dict(el.attrib)}))
    return out


ADAPTER = DataAdapter(
    "xml",
    (".xml", ".xsd", ".xsl", ".xslt", ".wsdl", ".svg"),
    skeleton,
    select,
    ("xpath",),
    symbol_kind=Kind.TAG,
    symbol_depth=2,  # 根要素だけでは名前検索の手がかりにならないので、直下の子要素まで
)
