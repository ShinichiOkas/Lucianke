"""グラフ形式 ``format: "dot"`` / ``"mermaid"`` のレンダラ（仕様書 §5.4.3、§6.3、§6.4）。

グラフを出すツールは、次のどちらかで入力を渡す。

1. ``ToolOutput.data_static["graph"]``（推奨。T-06・T-07 など）::

       {"nodes": [{"id": urn, "label": qualified_name, "path": str, "conf": "exact", "attrs": {...}}],
        "edges": [{"source": urn, "target": urn, "label": "CALLS", "count": 2, "conf": "exact", "attrs": {...}}]}

   ``label``・``path``・``conf``・``attrs``・``count`` は省略できる。エッジの端点がノード表に無ければ、URN から
   ノードを補う。同じ ``(source, target, label)`` のエッジは 1 本にまとめ、``count`` を足す
   （T-06 規約 6: 同じ囲み関数からの複数参照は 1 エッジに ``×N``）。
2. ``Record.data`` の ``source_urn`` / ``target_urn``（/ ``relation_kind``）列（T-20）。1 レコード = 1 エッジ。
   ``page`` があればその ``records``、無ければ ``out.records`` を使う。

どちらも無ければ ``E_INVALID_ARGUMENT`` の失敗応答（``Rendered.is_error``）にする。

表記（仕様書 §6.3）: ノード ID は URN、ラベルは ``qualified_name``、``path`` は dot の ``tooltip``。エッジのラベルは関係種別
（``count > 1`` なら `` ×N``）。降格要素（``conf`` が ``ToolOutput.max_confidence`` より低い）は、dot ではノード属性
``conf="<水準>"``（と ``style=dashed``）、mermaid ではクラス ``:::degraded``（エッジは点線）で区別する。
ヘッダ・警告・``head_kv``・続きの印はコメント（dot は ``// ``、mermaid は ``%% ``）で compact と同じ内容を出す。

エッジの ``attrs``: ``guard``（T-07 のガード条件）はエッジのラベルの 2 行目 ``when: …`` になる。dot ではさらに、
ノード・エッジの ``attrs`` のうち Graphviz の見た目の属性（``shape``・``style``・``color``・``fillcolor``・``fontcolor``・
``penwidth``・``URL``）だけをそのまま通す。mermaid は ``attrs`` を使わない。

打ち切り: ノードは 500、エッジは 2,000 まで（``max_tokens`` の予算はパッカー側で済んでいる前提の念のための歯止め）。
超えた分は落とし、末尾に ``# summary:`` のコメントで残数を書く。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence
from lucianke.mcp.envelope import collect_warnings
from lucianke.mcp.render.compact import (
    banner,
    banner_lines,
    candidate_line,
    error_header_line,
    header_line,
    kv_line,
    trailer_more,
    trailer_summary,
)
from lucianke.mcp.types import Rendered, RenderInput

__all__ = ["MAX_EDGES", "MAX_NODES", "render_dot", "render_mermaid"]

MAX_NODES = 500
MAX_EDGES = 2000

_DOT_PASSTHROUGH: frozenset[str] = frozenset(
    {"shape", "style", "color", "fillcolor", "fontcolor", "penwidth", "URL"}
)
_DOT_RESERVED: frozenset[str] = frozenset({"label", "tooltip", "conf"})
_ATTR_VALUE = re.compile(r"[A-Za-z0-9_.#\-]+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NEWLINE = re.compile(r"\r\n|\r|\n")
_MERMAID_ENTITY: dict[str, str] = {
    "#": "#35;",
    "%": "#37;",
    '"': "#quot;",
    "<": "#lt;",
    ">": "#gt;",
    "&": "#amp;",
    "`": "#96;",
    "|": "#124;",
}
_MERMAID_SPECIAL = re.compile(r"[#%\"<>&`|]")


# --- 中間表現 ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Node:
    id: str
    label: str
    path: str
    conf: str | None
    degraded: bool
    attrs: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _Edge:
    source: str
    target: str
    label: str
    count: int
    conf: str | None
    degraded: bool
    attrs: Mapping[str, Any]

    @property
    def guard(self) -> str:
        return str(self.attrs.get("guard") or "").strip()


@dataclass(slots=True)
class _Graph:
    nodes: list[_Node] = field(default_factory=list)
    edges: list[_Edge] = field(default_factory=list)
    dropped_nodes: int = 0
    dropped_edges: int = 0


def _tail(urn: str) -> str:
    """URN の末尾: セレクタ（``#`` の後ろ）があればそれ、無ければ ``lk://<ws>/`` を除いたパス。"""
    head, sep, frag = urn.partition("#")
    if sep and frag:
        return unquote(frag)
    return _path_of(head) or urn


def _path_of(urn: str) -> str:
    """URN のパス部分（``lk://<ws>/<path>``）。URN の形でなければ空文字列。"""
    head = urn.partition("#")[0]
    if not head.startswith("lk://"):
        return ""
    _ws, _sep, path = head[len("lk://") :].partition("/")
    return unquote(path)


def _is_degraded(conf: object, max_conf: Confidence) -> bool:
    if conf is None:
        return False
    try:
        return Confidence(str(conf)) < max_conf
    except ValueError:
        return False


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 1
    try:
        n = int(value)
    except ValueError:
        return 1
    return max(n, 1)


def _mapping_items(raw: object, what: str) -> list[Mapping[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"graph.{what} は配列: {type(raw).__name__}")
    items: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError(f"graph.{what} の要素はオブジェクト: {type(item).__name__}")
        items.append(item)
    return items


class _Builder:
    """ノードとエッジを集めて閉じたグラフにする（端点のノード補完・同一エッジの併合・上限）。"""

    def __init__(self, max_conf: Confidence) -> None:
        self.max_conf = max_conf
        self.nodes: dict[str, _Node] = {}
        self.edges: dict[tuple[str, str, str], _Edge] = {}

    def add_node(self, raw: Mapping[str, Any]) -> None:
        if "id" not in raw:
            raise ValueError("graph.nodes の要素に id が無い")
        nid = str(raw["id"])
        if nid in self.nodes:
            return
        conf = str(raw["conf"]) if raw.get("conf") is not None else None
        attrs = raw.get("attrs")
        self.nodes[nid] = _Node(
            id=nid,
            label=str(raw.get("label") or "") or _tail(nid),
            path=str(raw.get("path") or "") or _path_of(nid),
            conf=conf,
            degraded=_is_degraded(conf, self.max_conf),
            attrs=attrs if isinstance(attrs, Mapping) else {},
        )

    def add_edge(self, raw: Mapping[str, Any]) -> None:
        if "source" not in raw or "target" not in raw:
            raise ValueError("graph.edges の要素に source / target が無い")
        source, target = str(raw["source"]), str(raw["target"])
        label = str(raw.get("label") or "")
        count = _count(raw.get("count", 1))
        key = (source, target, label)
        prev = self.edges.get(key)
        if prev is not None:
            self.edges[key] = _Edge(
                source=prev.source,
                target=prev.target,
                label=prev.label,
                count=prev.count + count,
                conf=prev.conf,
                degraded=prev.degraded,
                attrs=prev.attrs,
            )
            return
        conf = str(raw["conf"]) if raw.get("conf") is not None else None
        attrs = raw.get("attrs")
        self.edges[key] = _Edge(
            source=source,
            target=target,
            label=label,
            count=count,
            conf=conf,
            degraded=_is_degraded(conf, self.max_conf),
            attrs=attrs if isinstance(attrs, Mapping) else {},
        )

    def build(self) -> _Graph:
        for edge in self.edges.values():  # 端点がノード表に無ければ URN から補う
            for end in (edge.source, edge.target):
                if end not in self.nodes:
                    self.add_node({"id": end})
        nodes = list(self.nodes.values())
        edges = list(self.edges.values())
        graph = _Graph()
        if len(nodes) > MAX_NODES:
            graph.dropped_nodes = len(nodes) - MAX_NODES
            nodes = nodes[:MAX_NODES]
            kept = {n.id for n in nodes}
            before = len(edges)
            edges = [e for e in edges if e.source in kept and e.target in kept]
            graph.dropped_edges += before - len(edges)
        if len(edges) > MAX_EDGES:
            graph.dropped_edges += len(edges) - MAX_EDGES
            edges = edges[:MAX_EDGES]
        graph.nodes, graph.edges = nodes, edges
        return graph


def _invalid(tool: str, reason: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT,
        f"{tool}: グラフ出力に必要な情報がありません（{reason}）。format は compact または json にしてください。",
        details={"param": "format"},
    )


def _collect(inp: RenderInput) -> _Graph:
    """入力からグラフを組み立てる。無ければ ``LuciankeError(E_INVALID_ARGUMENT)``。"""
    tool = inp.call.tool
    out = inp.out
    if out is None:
        raise _invalid(tool, "ツールの出力がない")
    builder = _Builder(out.max_confidence)
    raw = out.data_static.get("graph")
    if isinstance(raw, Mapping):
        for node in _mapping_items(raw.get("nodes"), "nodes"):
            builder.add_node(node)
        for edge in _mapping_items(raw.get("edges"), "edges"):
            builder.add_edge(edge)
        return builder.build()
    records = inp.page.records if inp.page is not None else out.records
    edge_rows = 0
    for rec in records:
        data = rec.data
        if "source_urn" not in data or "target_urn" not in data:
            continue
        edge_rows += 1
        builder.add_edge(
            {
                "source": data["source_urn"],
                "target": data["target_urn"],
                "label": data.get("relation_kind") or data.get("label") or data.get("use"),
                "count": data.get("count", 1),
                "conf": rec.confidence if _is_degraded(rec.confidence, out.max_confidence) else None,
                "attrs": {"guard": data["guard"]} if data.get("guard") else None,
            }
        )
    empty_but_typed = not records and _has_edge_columns(out.data_static.get("columns"))  # 0 行の T-20
    if edge_rows == 0 and not empty_but_typed:
        raise _invalid(tool, "data_static['graph'] も source_urn / target_urn 列も無い")
    return builder.build()


def _has_edge_columns(columns: object) -> bool:
    """T-20 の ``columns[]``（``{name, decl_type}``）に ``source_urn`` と ``target_urn`` の両方があるか。"""
    if not isinstance(columns, Sequence) or isinstance(columns, (str, bytes)):
        return False
    names = {str(c.get("name")) if isinstance(c, Mapping) else str(c) for c in columns}
    return {"source_urn", "target_urn"} <= names


# --- 文字列のエスケープ -----------------------------------------------------------------------


def _dot_str(text: object, *, multiline: bool = False) -> str:
    """dot の二重引用符つき文字列。``\\`` と ``"`` をエスケープし、改行は ``\\n``（``multiline``）か空白にする。"""
    s = _CTRL.sub(" ", str(text)).replace("\\", "\\\\").replace('"', '\\"')
    s = _NEWLINE.sub("\\\\n" if multiline else " ", s)
    return f'"{s}"'


def _dot_attr_value(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value)
    return text if _ATTR_VALUE.fullmatch(text) else None


def _mm_text(text: object) -> str:
    """mermaid の引用符つきラベルの中身。``# % " < > & ` |`` を実体参照にし、改行は空白にする。"""
    s = _NEWLINE.sub(" ", _CTRL.sub(" ", str(text)))
    return _MERMAID_SPECIAL.sub(lambda m: _MERMAID_ENTITY[m.group()], s)


def _edge_lines(edge: _Edge) -> list[str]:
    """エッジのラベルの行（1 行目: 関係種別と ``×N``、2 行目: ガード条件）。"""
    first = " ".join(p for p in (edge.label, f"×{edge.count}" if edge.count > 1 else "") if p)
    lines = [first] if first else []
    if edge.guard:
        lines.append(f"when: {edge.guard}")
    return lines


# --- コメント（ヘッダ・警告・トレーラ） --------------------------------------------------------


def _head_lines(inp: RenderInput, error: LuciankeError | None) -> list[str]:
    """compact と同じ中身の先頭行（ヘッダ、降格の警告、警告、``head_kv``）。失敗時はエラーの行。"""
    call = inp.call
    warnings = [line for w in collect_warnings(inp) for line in banner(w)]
    if error is not None:
        lines = [error_header_line(call.tool, error.code), *banner(error.message)]
        lines += [candidate_line(c) for c in error.candidates]
        return lines + warnings
    used = inp.page.used_tokens_est if inp.page is not None else 0
    lines = [
        header_line(
            call.tool,
            conf=inp.meta.confidence_level,
            lsp=inp.meta.lsp_status,
            rev=call.revision,
            ws=call.workspace_id,
            used_tokens=used,
            max_tokens=call.max_tokens,
            lsp_progress=inp.meta.lsp_progress,
        ),
        *banner_lines(inp.meta),
        *warnings,
    ]
    if inp.out is not None:
        lines += [kv_line(k, v) for k, v in inp.out.head_kv]
    return lines


def _tail_lines(inp: RenderInput, graph: _Graph | None) -> list[str]:
    lines: list[str] = []
    if graph is not None and (graph.dropped_nodes or graph.dropped_edges):
        lines.append(
            trailer_summary(
                f"graph truncated at {MAX_NODES} nodes / {MAX_EDGES} edges: "
                f"{graph.dropped_nodes} more nodes, {graph.dropped_edges} more edges omitted"
            )
        )
    page = inp.page
    if page is not None:
        if page.summary:
            lines.append(trailer_summary(page.summary))
        if page.next_cursor:
            lines.append(trailer_more(page.next_cursor, page.remaining if page.remaining > 0 else None))
    return lines


def _render(inp: RenderInput, prefix: str, body: Callable[[RenderInput, _Graph], list[str]]) -> Rendered:
    """共通の枠: 失敗ならコメントだけの失敗応答、成功なら先頭コメント + 本文 + 末尾コメント。"""
    error = inp.error
    graph: _Graph | None = None
    if error is None:
        try:
            graph = _collect(inp)
        except LuciankeError as err:
            error = err
    if error is not None:
        lines = [f"{prefix}{line}" for line in _head_lines(inp, error)]
        return Rendered(text="\n".join(lines) + "\n", is_error=True)
    assert graph is not None
    lines = [f"{prefix}{line}" for line in _head_lines(inp, None)]
    lines += body(inp, graph)
    lines += [f"{prefix}{line}" for line in _tail_lines(inp, graph)]
    return Rendered(text="\n".join(lines) + "\n")


# --- dot ---------------------------------------------------------------------------------------


def _dot_extra(attrs: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key in sorted(attrs):
        if key in _DOT_PASSTHROUGH and key not in _DOT_RESERVED:
            value = _dot_attr_value(attrs[key])
            if value is not None:
                out.append(f"{key}={value}")
    return out


def _dot_body(inp: RenderInput, graph: _Graph) -> list[str]:
    lines = [f"digraph {_dot_str(inp.call.tool)} {{", "  rankdir=LR;", "  node [shape=box];"]
    for node in graph.nodes:
        attrs = [f"label={_dot_str(node.label)}"]
        if node.path:
            attrs.append(f"tooltip={_dot_str(node.path)}")
        if node.degraded:
            attrs += [f"conf={_dot_str(node.conf)}", "style=dashed"]
        attrs += [a for a in _dot_extra(node.attrs) if not (node.degraded and a.startswith("style="))]
        lines.append(f"  {_dot_str(node.id)} [{' '.join(attrs)}];")
    for edge in graph.edges:
        attrs: list[str] = []
        text = _edge_lines(edge)
        if text:
            attrs.append(f"label={_dot_str(chr(10).join(text), multiline=True)}")
        if edge.degraded:
            attrs += [f"conf={_dot_str(edge.conf)}", "style=dashed"]
        attrs += [a for a in _dot_extra(edge.attrs) if not (edge.degraded and a.startswith("style="))]
        suffix = f" [{' '.join(attrs)}]" if attrs else ""
        lines.append(f"  {_dot_str(edge.source)} -> {_dot_str(edge.target)}{suffix};")
    lines.append("}")
    return lines


def render_dot(inp: RenderInput) -> Rendered:
    """``format: "dot"``。先頭は ``// # <tool> ok ...``、続けて ``digraph``。"""
    return _render(inp, "// ", _dot_body)


# --- mermaid -----------------------------------------------------------------------------------


def _mermaid_body(_inp: RenderInput, graph: _Graph) -> list[str]:
    alias = {node.id: f"n{i}" for i, node in enumerate(graph.nodes, start=1)}
    lines = ["flowchart LR"]
    for node in graph.nodes:
        tail = _tail(node.id)
        text = _mm_text(node.label or tail)
        if node.label and tail and tail != node.label:
            text += f"<br/>{_mm_text(tail)}"
        mark = ":::degraded" if node.degraded else ""
        lines.append(f'  {alias[node.id]}["{text}"]{mark}')
    for edge in graph.edges:
        arrow = "-.->" if edge.degraded else "-->"
        text = "<br/>".join(_mm_text(line) for line in _edge_lines(edge))
        label = f'|"{text}"|' if text else ""
        lines.append(f"  {alias[edge.source]} {arrow}{label} {alias[edge.target]}")
    if any(n.degraded for n in graph.nodes):
        lines.append("  classDef degraded stroke-dasharray:5 5,stroke:#b00020,color:#b00020")
    return lines


def render_mermaid(inp: RenderInput) -> Rendered:
    """``format: "mermaid"``。先頭は ``%% # <tool> ok ...``、続けて ``flowchart LR``。"""
    return _render(inp, "%% ", _mermaid_body)
