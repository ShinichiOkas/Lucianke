"""T-07 ``query_call_hierarchy``（仕様書 §11.3）。関数・メソッドを起点に、呼び出し元（incoming）・呼び出し先（outgoing）を木で返す。

``structural`` 版: PaaD の ``relations`` の ``CALLS`` エッジを深さ ``depth`` まで辿る。LSP の呼び出し階層（``exact``）は別の役が足す。

* 木は ``(親, 子のシンボル)`` ごとに 1 ノード（同じ子が同じ親の中で何度も呼ぶときは最初の呼び出し箇所を表示し、残りは json の
  ``call_sites`` に入れる）。子は ``(呼び出し箇所のパス, 行)`` の順で、1 ノードあたり ``max_children`` まで。超過分は
  ``- children: N more`` の note と ``children_truncated`` に要約する。
* 表示する ``@path:line`` は **呼び出し箇所**（incoming は呼び出し元の中、outgoing は親の中）。``when:`` は
  ``relations.guard_condition``（その呼び出し箇所を直接囲む条件）。
* 循環: 根（起点）からそのノードまでの経路に既に現れた URN は ``(cycle)`` を付けて 1 回だけ表示し、展開しない。
* 外部（``lk://_ext/``）は葉として ``(external)`` を付けて表示する。
* 1 レコード = 1 ノード（1 tree-line）。レコードは深さ優先の前順（インデントが親子と合う順）で並べる。予算が尽きたら前順の
  途中で止め、残りを ``# summary:`` に要約して ``next_cursor`` で続きを返す。
* ``Class`` / ``Struct`` を渡したときはコンストラクタ（``__init__`` / ``constructor``）の呼び出しを起点にする。
* ``dot`` / ``mermaid``: ``data_static["graph"]``。エッジは呼び出し元 → 呼び出し先（ラベル ``CALLS``、``attrs.guard`` に条件）。

``ctx.services`` に求めるもの（duck typing）: ``db_path``、``workspace(workspace_id)``、``lines``（compact の行の関数）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice, Kind, SymbolRecord, Tier
from lucianke.core.records import Record, ToolOutput
from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, SCHEME
from lucianke.index import queries
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._nav import (
    TargetRefArg,
    db_path_of,
    enclosing_name_of,
    reader,
    require_working_tree,
    resolve_target,
    workspace_of,
)

DEPTH_MAX = 5
NODE_CAP = 2000  # 木全体のノード数の歯止め（深さ 5 × 子 20 の爆発を防ぐ）
CALLABLE_KINDS = frozenset({Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR})
CONSTRUCTIBLE_KINDS = frozenset({Kind.CLASS, Kind.STRUCT})
CONSTRUCTOR_NAMES = ("__init__", "constructor", "new")
_EXTERNAL_PREFIX = f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"
_ARROW = {"incoming": "<-", "outgoing": "->"}

ACCURACY_WARNING = (
    "LSP が利用できないため構文木のみで解決しました。relations の名前解決済みエッジを使うため、動的ディスパッチ（実装の取り違え・"
    "見落とし）と同名メソッドの区別が落ちます。型・オーバーロード・再エクスポート越しの呼び出しは区別されていません。"
)


class QueryCallHierarchyArgs(CommonArgs):
    target: TargetRefArg = Field(
        description="The function/method/class (urn, or path + symbol_name, or path + line)."
    )
    direction: Literal["incoming", "outgoing", "both"] = Field(
        description="incoming = callers, outgoing = callees."
    )
    depth: int = Field(default=2, ge=1, le=DEPTH_MAX, description="1-5.")
    include_guard_conditions: bool = Field(
        default=True, description="Add 'when:' (the condition around each call)."
    )
    exclude_external: bool = Field(
        default=True, description="Show stdlib/package functions as leaves '(external)'."
    )
    include_tests: bool = Field(default=True, description="Include callers/callees in test files.")
    max_children: int = Field(
        default=20, ge=1, le=500, description="Children per node; the rest is summarised."
    )


# --- 木 -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Site:
    path: str
    line: int  # 1 始まり
    guard: str | None
    confidence: Confidence


@dataclass(slots=True)
class _Node:
    urn: str
    name: str
    def_path: str  # このシンボルの定義ファイル
    direction: str
    depth: int
    parent_urn: str | None
    sites: list[_Site]
    is_test: bool
    external: bool
    cycle: bool = False
    children_truncated: int = 0
    children: list[_Node] = field(default_factory=list)

    @property
    def site(self) -> _Site:
        return self.sites[0]


@dataclass(slots=True)
class _Tree:
    root: SymbolRecord
    language: str
    tier: Tier
    roots: dict[str, list[_Node]] = field(default_factory=dict)  # direction → 第 1 階層のノード
    root_truncated: dict[str, int] = field(
        default_factory=dict
    )  # direction → 根の子のうち max_children で省いた数
    count: int = 0
    capped: bool = False


class _Symbols:
    """シンボルと ``is_test`` の問い合わせ（URN・パスごとに 1 度）。"""

    def __init__(self, conn: sqlite3.Connection, workspace_id: str) -> None:
        self.conn, self.ws = conn, workspace_id
        self._sym: dict[str, SymbolRecord | None] = {}
        self._test: dict[str, bool] = {}

    def symbol(self, urn: str) -> SymbolRecord | None:
        if urn not in self._sym:
            self._sym[urn] = queries.symbol_by_urn(self.conn, urn)
        return self._sym[urn]

    def is_test(self, path: str) -> bool:
        if path not in self._test:
            info = queries.file_by_path(self.conn, self.ws, path)
            self._test[path] = bool(info and info.is_test)
        return self._test[path]


def _start_urns(conn: sqlite3.Connection, ws_id: str, sym: SymbolRecord, direction: str) -> list[str]:
    """起点の URN。``Class`` はコンストラクタ呼び出し（クラスそのものへの CALLS と、コンストラクタ）。"""
    if sym.kind in CALLABLE_KINDS:
        return [str(sym.urn)]
    if sym.kind in CONSTRUCTIBLE_KINDS:
        urns = [str(sym.urn)] if direction == "incoming" else []
        for name in CONSTRUCTOR_NAMES:
            want = f"{sym.qualified_name}.{name}"
            for m in queries.symbols_by_qualified_suffix(conn, ws_id, str(sym.path), want):
                if m.qualified_name == want:
                    urns.append(str(m.urn))
        return urns
    raise LuciankeError(
        ErrorCode.E_UNSUPPORTED,
        f"{sym.qualified_name} is a {sym.kind.value}, which cannot be called "
        f"(use a Function, Method, Constructor or Class)",
        details={"reason": "not_callable", "kind": sym.kind.value},
    )


def _call_edges(
    syms: _Symbols, urns: Sequence[str], direction: str, include_tests: bool
) -> dict[str, list[_Site]]:
    """``urns`` の CALLS エッジの相手（子）の URN → 呼び出し箇所。呼び出し箇所の順（パス、行）。"""
    grouped: dict[str, dict[tuple[str, int, int], _Site]] = {}
    for urn in urns:
        if direction == "incoming":
            rows = queries.references_to(syms.conn, urn, ("CALLS",), include_tests=include_tests)
        else:
            rows = queries.references_from(syms.conn, urn, ("CALLS",))
        for r in rows:
            if r.path is None or r.site is None:
                continue
            child = r.source_urn if direction == "incoming" else r.target_urn
            key = (str(r.path), r.site.line + 1, r.site.col + 1)
            grouped.setdefault(child, {}).setdefault(
                key, _Site(path=key[0], line=key[1], guard=r.guard_condition, confidence=r.confidence)
            )
    return {c: sorted(s.values(), key=lambda x: (x.path, x.line)) for c, s in grouped.items()}


def _make_node(
    syms: _Symbols, urn: str, sites: list[_Site], direction: str, depth: int, parent: str
) -> _Node:
    external = urn.startswith(_EXTERNAL_PREFIX)
    sym = syms.symbol(urn)
    if sym is not None:
        name, def_path = sym.qualified_name, str(sym.path)
    else:
        qualified = enclosing_name_of(urn)
        name, def_path = (
            (qualified or urn.partition("#")[0].removeprefix(SCHEME).partition("/")[2] or urn),
            sites[0].path,
        )
        if qualified is None and not external:
            name += " (module)"
    is_test = False if external else syms.is_test(def_path)
    return _Node(
        urn=urn,
        name=name,
        def_path=def_path,
        direction=direction,
        depth=depth,
        parent_urn=parent,
        sites=sites,
        is_test=is_test,
        external=external,
    )


def _expand(
    syms: _Symbols,
    tree: _Tree,
    urns: Sequence[str],
    direction: str,
    depth: int,
    ancestors: frozenset[str],
    args: QueryCallHierarchyArgs,
    parent_urn: str,
) -> tuple[list[_Node], int]:
    """``urns`` の子を展開する。``(残したノード, max_children で省いた数)``。"""
    edges = _call_edges(syms, urns, direction, args.include_tests)
    nodes: list[_Node] = []
    for child_urn, sites in edges.items():
        node = _make_node(syms, child_urn, sites, direction, depth, parent_urn)
        if not args.include_tests and node.is_test:
            continue
        nodes.append(node)
    nodes.sort(key=lambda n: (n.site.path, n.site.line, n.name))
    kept = nodes[: args.max_children]
    for node in kept:
        tree.count += 1
        if node.urn in ancestors:  # 経路上に既出: 1 回だけ表示して展開しない
            node.cycle = True
        elif node.external or depth >= args.depth:
            continue
        elif tree.count >= NODE_CAP:
            tree.capped = True
        else:
            node.children, node.children_truncated = _expand(
                syms, tree, [node.urn], direction, depth + 1, ancestors | {node.urn}, args, node.urn
            )
    return kept, len(nodes) - len(kept)


def _work(db_path: Any, ws: Any, args: QueryCallHierarchyArgs) -> _Tree:
    with reader(db_path) as conn:
        sym = resolve_target(conn, ws, args.target)
        syms = _Symbols(conn, ws.workspace_id)
        info = queries.file_by_path(conn, ws.workspace_id, str(sym.path))
        tree = _Tree(
            root=sym,
            language=info.language if info is not None else "n/a",
            tier=info.tier if info is not None else Tier.T1,
        )
        directions = ("incoming", "outgoing") if args.direction == "both" else (args.direction,)
        root_urn = str(sym.urn)
        for direction in directions:
            urns = _start_urns(conn, ws.workspace_id, sym, direction)
            if urns:
                tree.roots[direction], skipped = _expand(
                    syms, tree, urns, direction, 1, frozenset({root_urn, *urns}), args, root_urn
                )
                if skipped:
                    tree.root_truncated[direction] = skipped
            else:
                tree.roots[direction] = []
        return tree


# --- 出力 ---------------------------------------------------------------------------------------


def _preorder(nodes: Sequence[_Node]) -> list[_Node]:
    out: list[_Node] = []
    for n in nodes:
        out.append(n)
        out += _preorder(n.children)
    return out


def _node_json(n: _Node, include_guard: bool) -> dict[str, Any]:
    first = n.site
    return {
        "urn": n.urn,
        "name": n.name,
        "path": n.def_path,
        "call_site_path": first.path,
        "call_site_line": first.line,
        "call_sites": [
            {"path": s.path, "line": s.line, "when": s.guard if include_guard else None} for s in n.sites
        ],
        "direction": n.direction,
        "depth": n.depth,
        "parent_urn": n.parent_urn,
        "when": first.guard if include_guard else None,
        "is_test": n.is_test,
        "is_external": n.external,
        "cycle": n.cycle,
        "children_truncated": n.children_truncated,
        "confidence_level": first.confidence.value,
    }


def _edges(root_urn: str, all_nodes: Sequence[_Node], include_guard: bool) -> list[dict[str, Any]]:
    """木のノードごとの呼び出しエッジ（呼び出し元 → 呼び出し先）。``both`` で同じエッジが 2 度出ないよう、箇所で重複を除く。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int]] = set()
    for n in all_nodes:
        parent = n.parent_urn or root_urn
        src, dst = (n.urn, parent) if n.direction == "incoming" else (parent, n.urn)
        for s in n.sites:
            key = (src, dst, s.path, s.line)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "source_urn": src,
                    "target_urn": dst,
                    "call_site_line": s.line,
                    "call_site_path": s.path,
                    "guard": s.guard if include_guard else None,
                }
            )
    return out


def _graph(root: SymbolRecord, all_nodes: Sequence[_Node], edges: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {
        str(root.urn): {"id": str(root.urn), "label": root.qualified_name, "path": str(root.path)}
    }
    for n in all_nodes:
        nodes.setdefault(n.urn, {"id": n.urn, "label": n.name, "path": n.def_path})
    return {
        "nodes": list(nodes.values()),
        "edges": [
            {
                "source": e["source_urn"],
                "target": e["target_urn"],
                "label": "CALLS",
                "count": 1,
                "attrs": {"guard": e["guard"]} if e["guard"] else {},
            }
            for e in edges
        ],
    }


def _summarize(rest: Sequence[Record]) -> str:
    trees = [r for r in rest if r.kind == "tree"]
    if not trees:
        return ""
    depths = [int(r.data["depth"]) for r in trees]
    files = {str(r.data["call_site_path"]) for r in trees}
    incoming = sum(1 for r in trees if r.data["direction"] == "incoming")
    what = "callers" if incoming * 2 >= len(trees) else "callees"
    span = f"depth {min(depths)}" if min(depths) == max(depths) else f"depth {min(depths)}-{max(depths)}"
    return f"{span}: {len(trees)} more {what} in {len(files)} files"


async def handler(ctx: ToolContext, args: QueryCallHierarchyArgs) -> ToolOutput:
    require_working_tree(args.revision)
    db_path = db_path_of(ctx)
    ws = workspace_of(ctx, args.workspace_id)
    L = ctx.services.lines  # type: ignore[attr-defined]
    tree = await asyncio.to_thread(_work, db_path, ws, args)
    root_urn = str(tree.root.urn)

    records: list[Record] = []
    all_nodes: list[_Node] = []
    for direction, first_level in tree.roots.items():
        for n in _preorder(first_level):
            all_nodes.append(n)
            name = f"{n.name} (external)" if n.external else n.name
            when = n.site.guard if args.include_guard_conditions else None
            lines = [
                L.tree_line(
                    _ARROW[direction],
                    name,
                    n.site.path,
                    n.site.line,
                    indent=n.depth - 1,
                    when=when,
                    cycle=n.cycle,
                )
            ]
            if n.children_truncated:
                lines.append(L.note(f"children: {n.children_truncated} more"))
            records.append(
                Record(
                    kind="tree",
                    key=f"{direction}#{len(records)}",
                    priority=len(records),
                    lines=tuple(lines),
                    data=_node_json(n, args.include_guard_conditions),
                    confidence=n.site.confidence,
                    group=n.site.path,
                )
            )

    edges = _edges(root_urn, all_nodes, args.include_guard_conditions)
    data: dict[str, Any] = {
        "root": tree.root.to_json(),
        "direction": args.direction,
        "depth": args.depth,
        "edges": edges,
    }
    if args.format in ("dot", "mermaid"):
        data["graph"] = _graph(tree.root, all_nodes, edges)
    head = [("root", f"[{tree.root.kind.value}] {root_urn} direction={args.direction} depth={args.depth}")]
    if tree.root_truncated:
        head.append(("children", "; ".join(f"{d} {n} more" for d, n in tree.root_truncated.items())))
        data["root_children_truncated"] = dict(tree.root_truncated)
    warnings = ["the tree was cut at the node cap; narrow depth or max_children"] if tree.capped else []
    return ToolOutput(
        records=records,
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL, warning=ACCURACY_WARNING),
        page_unit="tree-line",
        head_kv=head,
        data_static=data,
        summarize=_summarize,
        tier=tree.tier,
        detected_language=tree.language,
        warnings=tuple(warnings),
    )


TOOL_SPEC = ToolSpec(
    name="query_call_hierarchy",
    tid="T-07",
    args_model=QueryCallHierarchyArgs,
    handler=handler,
    default_max_tokens=2000,
    description=(
        "Show who calls a function/method (incoming) or what it calls (outgoing) as a tree up to depth 1-5, with the "
        "condition around each call ('when:'). Cycles are shown once as '(cycle)'. Target by urn, or path + "
        "symbol_name, or path + line. dot/mermaid draw the call graph. Syntax-tree resolution: dynamic dispatch and "
        "same-name methods are not told apart."
    ),
    allows_graph=True,
    max_confidence=Confidence.EXACT,
    milestone="M1",
    page_unit="tree-line",
    records_key="nodes",
)
SPEC = TOOL_SPEC

__all__ = ["QueryCallHierarchyArgs", "SPEC", "TOOL_SPEC", "handler"]
