"""YAML のアダプタ（仕様書 §8.6 B4・§11 T-02 処理規約 5・T-05 処理規約 3、設計書 §7.4.8）。

* 骨格: ``yaml.compose`` 系（PyYAML の ``SafeLoader``。値を作らない）のノードの位置で ``[Key]`` の階層を作る。
  配列は ``×要素数`` と推論した要素の型。スカラーの型は PyYAML の暗黙のタグ（``int`` / ``float`` / ``bool`` / ``null`` / ``string`` /
  ``datetime``）から取る。複数文書は先頭の文書だけを扱い、その旨を ``- note:`` に書く。
* 部分取得: ``yaml:`` はキー経路（``retry.backoff_ms``、``items[0].name``、``"a.b".c``、``*``）。一致が無ければ空のリスト。

終了行の決め方: ブロックの入れ子は最後の子の終了行、フロー形式（``{}`` / ``[]``）は ``end_mark`` の行、スカラーは ``end_mark`` の行
（ブロックスカラー ``|`` ``>`` は次のトークンの手前。末尾の空行・コメント行は含めない）。エイリアス（``*a``）の値は 1 行（キーの行）とする。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from lucianke.core.records import Record
from lucianke.syntax.adapters._common import (
    SAMPLE_LARGE,
    STREAM_BYTES,
    WILDCARD,
    DataAdapter,
    Shape,
    SkelNode,
    TextLines,
    Typed,
    build_record,
    decode_text,
    parse_key_path,
    path_text,
    split_selector,
)
from lucianke.syntax.types import Match, SkeletonOptions

__all__ = ["ADAPTER", "select", "skeleton"]

_TAG_NAMES = {
    "str": "string",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "null": "null",
    "timestamp": "datetime",
    "binary": "binary",
    "set": "object",
    "omap": "object",
    "pairs": "array",
}
_PREFIX = "tag:yaml.org,2002:"
_MAX_CONVERT_DEPTH = 8


def _compose(text: str, large: bool) -> tuple[Node | None, bool]:
    """``(先頭の文書のルート, 2 つ目以降の文書があるか)``。値は作らない（独自タグがあっても落ちない）。"""
    cls: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader) if large else yaml.SafeLoader
    try:
        loader = cls(text)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}") from e
    try:
        node = loader.get_node() if loader.check_node() else None  # check_node が StreamStart を読み飛ばす
        more = loader.check_node() if node is not None else False
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}") from e
    finally:
        loader.dispose()
    return node, bool(more)


def _scalar_type(node: ScalarNode) -> str:
    tag = node.tag or ""
    if tag.startswith(_PREFIX):
        return _TAG_NAMES.get(tag[len(_PREFIX) :], tag[len(_PREFIX) :])
    return tag.lstrip("!") or "string"


def _key_name(k: Node) -> str:
    return k.value if isinstance(k, ScalarNode) else "?"  # 複合キーは "?"


def _py(node: Node, stack: tuple[int, ...] = (), depth: int = 0) -> Any:
    """型の推論用に、ノードを ``dict`` / ``list`` / ``Typed`` に写す（独自タグを解釈しない）。"""
    if isinstance(node, ScalarNode):
        return Typed(_scalar_type(node))
    if id(node) in stack or depth > _MAX_CONVERT_DEPTH:
        return {} if isinstance(node, MappingNode) else []
    nxt = (*stack, id(node))
    if isinstance(node, MappingNode):
        return {_key_name(k): _py(v, nxt, depth + 1) for k, v in node.value}
    if isinstance(node, SequenceNode):
        return [_py(v, nxt, depth + 1) for v in node.value]
    return Typed("unknown")


def _type_of(node: Node, sample: int | None) -> str:
    s = Shape()
    s.add(_py(node), sample=sample)
    return s.render()


def _is_alias_of_earlier(key: Node, value: Node) -> bool:
    return value.start_mark.line < key.start_mark.line


def _end_line(node: Node, lm: TextLines) -> int:
    s = node.start_mark.line
    if isinstance(node, ScalarNode):
        e = node.end_mark
        line = e.line - 1 if (e.column == 0 and e.line > s) else e.line
        return lm.last_content_line(s, max(s, line))
    if node.flow_style or not node.value:
        return max(s, node.end_mark.line)
    if isinstance(node, MappingNode):
        k, v = node.value[-1]
        return max(s, _pair_end(k, v, lm))
    return max(s, _end_line(node.value[-1], lm))


def _pair_end(key: Node, value: Node, lm: TextLines) -> int:
    if _is_alias_of_earlier(key, value):
        return key.start_mark.line
    return max(key.start_mark.line, _end_line(value, lm))


def _nodes(node: MappingNode, lm: TextLines, sample: int | None, stack: tuple[int, ...]) -> list[SkelNode]:
    out: list[SkelNode] = []
    nxt = (*stack, id(node))
    for k, v in node.value:
        n = SkelNode("Key", _key_name(k), k.start_mark.line, _pair_end(k, v, lm))
        if isinstance(v, SequenceNode):
            n.count = len(v.value)
            n.type = _type_of(v, sample)
        elif isinstance(v, MappingNode) and not _is_alias_of_earlier(k, v) and id(v) not in nxt:
            n.children = _nodes(v, lm, sample, nxt)
        out.append(n)
    return out


def skeleton(
    source: bytes,
    opts: SkeletonOptions,
    *,
    rel: str,
    sha256: str,
    estimate: Callable[[str], int] | None = None,
) -> Record:
    """YAML の骨格 1 ファイルブロック。"""
    text = decode_text(source)
    large = len(source) > STREAM_BYTES
    root, more = _compose(text, large)
    lm = TextLines(text)
    notes: list[str] = []
    sample: int | None = None
    if large:
        opts = dataclasses.replace(opts, max_depth=min(opts.max_depth, 2))
        sample = SAMPLE_LARGE
        notes.append(
            f"note: large file (> 5 MB): depth limited to 2, array types inferred from first {SAMPLE_LARGE} elements"
        )
    if more:
        notes.append("note: multi-document YAML: only the first document is shown")
    nodes: list[SkelNode] = []
    if isinstance(root, MappingNode):
        nodes = _nodes(root, lm, sample, ())
    elif root is not None:
        top = SkelNode("Key", "$", root.start_mark.line, _end_line(root, lm))
        if isinstance(root, SequenceNode):
            top.count = len(root.value)
        top.type = _type_of(root, sample)
        nodes = [top]
    return build_record(
        source, nodes, rel=rel, sha256=sha256, language="yaml", opts=opts, notes=notes, estimate=estimate
    )


def _normalize_index(i: int, n: int) -> int | None:
    j = i + n if i < 0 else i
    return j if 0 <= j < n else None


def select(source: bytes, selector: str) -> list[Match]:
    """``yaml:<キー経路>`` に一致するノードの ``Match``（``kind="Key"``）。一致が無ければ空のリスト。"""
    expr = split_selector(selector, accepts=("yaml",))
    comps = parse_key_path(expr)
    text = decode_text(source)
    root, _ = _compose(text, len(source) > STREAM_BYTES)
    if root is None:
        return []
    lm = TextLines(text)
    cur: list[tuple[list[str | int], Node, int, int]] = [
        ([], root, root.start_mark.line, _end_line(root, lm))
    ]
    for c in comps:
        nxt: list[tuple[list[str | int], Node, int, int]] = []
        for path, node, _s, _e in cur:
            if isinstance(node, MappingNode):
                for k, v in node.value:
                    name = _key_name(k)
                    if c is WILDCARD or name == str(c):
                        nxt.append(([*path, name], v, k.start_mark.line, _pair_end(k, v, lm)))
            elif isinstance(node, SequenceNode) and (c is WILDCARD or isinstance(c, int)):
                n = len(node.value)
                if c is WILDCARD:
                    idxs: list[int] = list(range(n))
                else:
                    j = _normalize_index(int(c), n)  # type: ignore[arg-type]
                    idxs = [] if j is None else [j]
                for i in idxs:
                    child = node.value[i]
                    nxt.append(([*path, i], child, child.start_mark.line, _end_line(child, lm)))
        cur = nxt
    out: list[Match] = []
    for path, _node, s, e in cur:
        last = path[-1] if path else None
        name = f"[{last}]" if isinstance(last, int) else last
        out.append(lm.match("Key", name, s, max(s, e), {"path": path_text(path)}))
    return out


ADAPTER = DataAdapter("yaml", (".yaml", ".yml"), skeleton, select, ("yaml",))
