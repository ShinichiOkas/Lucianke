"""T-07 ``query_call_hierarchy``（仕様書 §11.3）。関数・メソッドを起点に、呼び出し元（incoming）・呼び出し先（outgoing）を木で返す。

``structural`` 版: PaaD の ``relations`` の ``CALLS`` エッジを深さ ``depth`` まで辿る。
``exact`` 版（仕様書 T-07 処理規約 2）: LSP が ``ready`` なら呼び出し階層（prepare -> incoming / outgoing）で同じ木を作り、
辺を ``relations`` に ``exact`` で書き戻す（``_lsp_exact.CallEdgeSource``）。2 回目以降は、ノードごとの目印が新しければ LSP を
呼ばず PaaD の ``exact`` 行だけで答える（LSP が止まっていても ``exact``）。LSP が ``ready`` でない・時間切れ・失敗なら
木ごと ``structural``（今までどおり）に降格する。外部（標準ライブラリ等）の呼び先は、LSP が返したファイルのパスから
推定した ``lk://_ext/`` の URN で葉にする（最善努力）。

* 木は ``(親, 子のシンボル)`` ごとに 1 ノード（同じ子が同じ親の中で何度も呼ぶときは最初の呼び出し箇所を表示し、残りは json の
  ``call_sites`` に入れる）。子は ``(呼び出し箇所のパス, 行)`` の順で、1 ノードあたり ``max_children`` まで。超過分は
  ``- children: N more`` の note と ``children_truncated`` に要約する。
* 表示する ``@path:line`` は **呼び出し箇所**（incoming は呼び出し元の中、outgoing は親の中）。``when:`` は
  ``relations.guard_condition``（その呼び出し箇所を直接囲む条件）。
* 循環: 根（起点）からそのノードまでの経路に既に現れた URN は ``(cycle)`` を付けて 1 回だけ表示し、展開しない。
* 外部（``lk://_ext/``）は葉として ``(external)`` を付けて表示する。ただし言語の組み込み名（``lk://_ext/builtins#print``・
  ``lk://_ext/globals#Error`` など。``syntax/builtin_names.py``）は ``exclude_external=true``（既定）では出さない
  （``print``・``ValueError`` の葉が outgoing を埋めるため。``false`` なら出す）。標準ライブラリ・外部パッケージの呼び先は葉のまま。
* 1 レコード = 1 ノード（1 tree-line）。レコードは深さ優先の前順（インデントが親子と合う順）で並べる。予算が尽きたら前順の
  途中で止め、残りを ``# summary:`` に要約して ``next_cursor`` で続きを返す。
* ``Class`` / ``Struct`` を渡したときはコンストラクタ（``__init__`` / ``constructor``）の呼び出しを起点にする。
* ``dot`` / ``mermaid``: ``data_static["graph"]``。エッジは呼び出し元 → 呼び出し先（ラベル ``CALLS``、``attrs.guard`` に条件）。
* 型の分からない受け手の呼び出し（``x.pack(...)`` で ``x`` が ``Any`` など。BUG-0071、``_name_calls``）: ``structural`` の木では、
  incoming はノードが ``Method`` でその名前のメソッドがワークスペースに 1 つだけ（組み込み型と同名でない）なら、その名前の型不明の
  呼び出しを呼び出し元として足す。outgoing はノードの中の型不明の呼び出しのうち、名前のメソッドが 1 つだけのものをそのメソッドへの
  呼び出しとして足す。どちらも ``conf=heuristic``・json の ``match: "name"``。結べない（同名が複数・組み込み型と同名）ものは件数と
  先頭の位置を ``warnings`` と ``data.unattributed_calls`` に書く（outgoing は組み込み型と同名のものを数えない）。``exact`` の木には
  足さず、根の incoming で結べるのに LSP の答えに無い位置の件数と先頭を ``warnings`` に書くだけにする。

``ctx.services`` に求めるもの（duck typing）: ``db_path``、``workspace(workspace_id)``、``lines``（compact の行の関数）。
``exact`` 経路には ``lsp_pool`` と書き手（``runtime().writer``）、``trees`` も使う。詳細は ``_lsp_exact`` の docstring。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Capability, Confidence, EngineChoice, FileInfo, Kind, SymbolRecord, Tier
from lucianke.core.records import Record, ToolOutput
from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, SCHEME
from lucianke.engine.selector import ENGINE_LSP_CACHE
from lucianke.index import queries
from lucianke.syntax.builtin_names import BUILTINS_PACKAGE, GLOBALS_PACKAGE
from lucianke.tools import _lsp_exact as lx
from lucianke.tools import _name_calls as nc
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._building import ensure_target_indexed
from lucianke.tools._nav import (
    TargetRefArg,
    db_path_of,
    enclosing_name_of,
    reader,
    resolve_target,
    workspace_of,
)
from lucianke.tools._rev import reject_unsupported_revision

_log = logging.getLogger("lucianke.tools.t07")

DEPTH_MAX = 5
NODE_CAP = 2000  # 木全体のノード数の歯止め（深さ 5 × 子 20 の爆発を防ぐ）
CALLABLE_KINDS = frozenset({Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR})
CONSTRUCTIBLE_KINDS = frozenset({Kind.CLASS, Kind.STRUCT})
CONSTRUCTOR_NAMES = ("__init__", "constructor", "new")
_EXTERNAL_PREFIX = f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"
_BUILTIN_PREFIXES = tuple(f"{_EXTERNAL_PREFIX}{pkg}#" for pkg in (BUILTINS_PACKAGE, GLOBALS_PACKAGE))
_ARROW = {"incoming": "<-", "outgoing": "->"}

ACCURACY_NOTE = "relations の名前解決済みエッジを使うため、動的ディスパッチ（実装の取り違え・見落とし）と同名メソッドの区別が落ちます。"

ACCURACY_WARNING = (
    "LSP が利用できないため構文木のみで解決しました。relations の名前解決済みエッジを使うため、動的ディスパッチ（実装の取り違え・"
    "見落とし）と同名メソッドの区別が落ちます。型・オーバーロード・再エクスポート越しの呼び出しは区別されていません。"
)


class QueryCallHierarchyArgs(CommonArgs):
    target: TargetRefArg
    direction: Literal["incoming", "outgoing", "both"] = Field()
    depth: int = Field(default=2, ge=1, le=DEPTH_MAX)
    include_guard_conditions: bool = Field(default=True)
    exclude_external: bool = Field(
        default=True,
        description="Hide language builtins (print, ValueError...); other stdlib/package callees stay '(external)' leaves.",
    )
    include_tests: bool = Field(default=True)
    max_children: int = Field(default=20, ge=1, le=500)


# --- 木 -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Site:
    path: str
    line: int  # 1 始まり
    guard: str | None
    confidence: Confidence
    match: str | None = None  # 名前だけで結んだ呼び出しは ``"name"``（BUG-0071）


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

    @property
    def confidence(self) -> Confidence:
        """呼び出し箇所のうち最も高い水準（1 箇所でも名前解決済みなら、名前だけの箇所があってもその水準）。"""
        return max(s.confidence for s in self.sites)

    @property
    def match(self) -> str | None:
        """すべての呼び出し箇所が名前だけで結んだものなら ``"name"``。"""
        return nc.MATCH_NAME if all(s.match for s in self.sites) else None


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
    warnings: list[str] = field(default_factory=list)  # 名前で結んだ・結ばなかった呼び出しの説明（BUG-0071）
    unattributed: list[dict[str, Any]] = field(default_factory=list)  # data.unattributed_calls


class _Symbols:
    """シンボルと ``is_test`` の問い合わせ（URN・パスごとに 1 度）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        source: lx.CallEdgeSource | None = None,
        names: nc.NameIndex | None = None,
    ) -> None:
        self.conn, self.ws = conn, workspace_id
        self.source = source  # exact 経路の辺の供給（無ければ relations の全行）
        self.names = names  # structural の木で、型の分からない受け手の呼び出しを名前で足す（BUG-0071）
        self._sym: dict[str, SymbolRecord | None] = {}
        self._test: dict[str, bool] = {}
        # 名前で足した呼び出し箇所（向き, パス, 行, 列）と、結ばなかったもの（向き, 名前, 言語族）→ 集まり
        self.by_name: set[tuple[str, str, int, int]] = set()
        self.unattributed: dict[tuple[str, str, str], nc.Unattributed] = {}

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


def _is_builtin(urn: str) -> bool:
    """言語の組み込み名の外部シンボル（``lk://_ext/builtins#print``・``lk://_ext/globals#Error``）。"""
    return urn.startswith(_BUILTIN_PREFIXES)


def _call_edges(
    syms: _Symbols, urns: Sequence[str], direction: str, include_tests: bool
) -> dict[str, list[_Site]]:
    """``urns`` の CALLS エッジの相手（子）の URN → 呼び出し箇所。呼び出し箇所の順（パス、行）。"""
    grouped: dict[str, dict[tuple[str, int, int], _Site]] = {}
    for urn in urns:
        if syms.source is not None:
            rows = syms.source.rows(urn, direction, include_tests)
        elif direction == "incoming":
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
    if syms.names is not None:
        for urn in urns:
            for child, key, site in _name_edges(syms, syms.names, urn, direction, include_tests):
                if key not in grouped.get(child, {}):
                    grouped.setdefault(child, {})[key] = site
                    syms.by_name.add((direction, *key))
    return {c: sorted(s.values(), key=lambda x: (x.path, x.line)) for c, s in grouped.items()}


def _name_site(r: queries.NameCallRow) -> tuple[tuple[str, int, int], _Site]:
    key = nc.site_key(r)
    return key, _Site(
        path=key[0], line=key[1], guard=r.guard_condition, confidence=Confidence.HEURISTIC, match=nc.MATCH_NAME
    )


def _name_edges(
    syms: _Symbols, names: nc.NameIndex, urn: str, direction: str, include_tests: bool
) -> list[tuple[str, tuple[str, int, int], _Site]]:
    """型の分からない受け手の呼び出しのうち、名前で ``urn`` と結べるもの ``(子の URN, 箇所のキー, 箇所)``（BUG-0071）。

    結べないもの（同名が複数・組み込み型と同名）は ``syms.unattributed`` に集める（outgoing は組み込み型と同名のものを数えない）。
    """
    out: list[tuple[str, tuple[str, int, int], _Site]] = []
    if direction == "incoming":
        sym = syms.symbol(urn)
        verdict = names.target_verdict(sym) if sym is not None else None
        if sym is None or verdict is None:
            return out
        rows = names.sites_named(verdict, include_tests=include_tests)
        if not rows:
            return out
        if not verdict.owned_by(sym):
            syms.unattributed.setdefault(
                (direction, verdict.name, verdict.family), nc.Unattributed(verdict)
            ).add(rows)
            return out
        for r in rows:
            key, site = _name_site(r)
            out.append((r.source_urn, key, site))
        return out
    for r in names.calls_from(urn):
        family = nc.family_of(r.language)
        verdict = names.verdict(r.name, family) if family is not None else None
        if verdict is None:
            continue  # ワークスペースにその名前のメソッドが無い（外部・組み込みの呼び出し）
        owner = verdict.owner
        if owner is None:
            if len(verdict.candidates) > 1 and not verdict.builtin:
                syms.unattributed.setdefault(
                    (direction, verdict.name, verdict.family), nc.Unattributed(verdict)
                ).add([r])
            continue
        key, site = _name_site(r)
        out.append((str(owner.urn), key, site))
    return out


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
        if args.exclude_external and _is_builtin(child_urn):
            continue  # print・ValueError などの組み込み名は既定で出さない（exclude_external=False で出す）
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


@dataclass(slots=True)
class _Pre:
    """対象の解決（``exact`` 経路の判定に言語・Tier が要る）。"""

    sym: SymbolRecord
    info: FileInfo | None


def _pre(db_path: Any, ws: Any, args: QueryCallHierarchyArgs) -> _Pre:
    with reader(db_path) as conn:
        sym = resolve_target(conn, ws, args.target)
        return _Pre(sym, queries.file_by_path(conn, ws.workspace_id, str(sym.path)))


@dataclass(slots=True)
class _ExactCtx:
    """``exact`` 経路でスレッドの中の ``CallEdgeSource`` を作るための材料。"""

    loop: asyncio.AbstractEventLoop
    services: Any
    route: lx.LspRoute


def _roots_of(conn: sqlite3.Connection, ws_id: str, sym: SymbolRecord) -> list[str]:
    if sym.kind not in CALLABLE_KINDS | CONSTRUCTIBLE_KINDS:
        return []
    return [str(sym.urn)] + [u for d in ("incoming", "outgoing") for u in _start_urns(conn, ws_id, sym, d)]


def _work(
    db_path: Any, ws: Any, args: QueryCallHierarchyArgs, pre: _Pre, exact: _ExactCtx | None = None
) -> tuple[_Tree, lx.CallEdgeSource | None]:
    with reader(db_path) as conn:
        sym, info = pre.sym, pre.info
        source = None
        if exact is not None:
            source = lx.CallEdgeSource(
                loop=exact.loop,
                services=exact.services,
                ws=ws,
                db_path=db_path,
                conn=conn,
                route=exact.route,
                token=queries.files_fingerprint(conn, ws.workspace_id),
                roots=_roots_of(conn, ws.workspace_id, sym),
            )
        names = nc.NameIndex(conn, ws.workspace_id)
        syms = _Symbols(conn, ws.workspace_id, source, names=None if exact is not None else names)
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
        if exact is None:
            _name_notes(syms, tree)
        elif "incoming" in directions and sym.kind is Kind.METHOD:
            _exact_name_note(names, sym, tree, args.include_tests)
        return tree, source


def _name_notes(syms: _Symbols, tree: _Tree) -> None:
    """structural の木の、名前で結んだ・結ばなかった呼び出しの説明（BUG-0071）。"""
    if syms.by_name:
        tree.warnings.append(
            f"{len(syms.by_name)} call site(s) marked conf=heuristic are matched by method name only: the receiver's"
            " type is unknown and the workspace has exactly one method of that name"
        )
    for (direction, _name, _family), group in sorted(syms.unattributed.items()):
        rows = group.sorted_rows()
        tree.warnings.append(
            f"{direction}: {len(rows)} call site(s) of .{group.verdict.name}() with a receiver of unknown type were"
            f" not attributed: {group.verdict.reason()}; e.g. {nc.sample_text(rows)} (data.unattributed_calls)"
        )
        tree.unattributed.append({"direction": direction, **group.to_json(enclosing_name_of)})


def _exact_name_note(names: nc.NameIndex, sym: SymbolRecord, tree: _Tree, include_tests: bool) -> None:
    """exact の木には足さない。根の incoming で、名前で結べるのに LSP の答えに無い呼び出しの件数と先頭を書く（BUG-0071）。"""
    verdict = names.target_verdict(sym)
    if verdict is None or not verdict.owned_by(sym):
        return
    known = {(s.path, s.line) for n in tree.roots.get("incoming", []) for s in n.sites}
    rows = [r for r in names.sites_named(verdict, include_tests=include_tests) if nc.site_key(r)[:2] not in known]
    if not rows:
        return
    tree.warnings.append(
        f"incoming: {len(rows)} call site(s) of .{verdict.name}() whose receiver type is unknown to the syntax tree"
        f" are not in this exact tree (the LSP could not type the receiver, e.g. Any); {verdict.only_text()}, so"
        f" they may call it: {nc.sample_text(rows)} (data.unattributed_calls)"
    )
    group = nc.Unattributed(verdict)
    group.add(rows)
    tree.unattributed.append({"direction": "incoming", **group.to_json(enclosing_name_of, "not_in_exact_answer")})


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
            {
                "path": s.path,
                "line": s.line,
                "when": s.guard if include_guard else None,
                **({"match": s.match} if s.match else {}),
            }
            for s in n.sites
        ],
        "direction": n.direction,
        "depth": n.depth,
        "parent_urn": n.parent_urn,
        "when": first.guard if include_guard else None,
        "is_test": n.is_test,
        "is_external": n.external,
        "cycle": n.cycle,
        "children_truncated": n.children_truncated,
        "confidence_level": n.confidence.value,
        **({"match": n.match} if n.match else {}),
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
                    **({"match": s.match} if s.match else {}),
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
    await reject_unsupported_revision(ctx, args.revision)
    db_path = db_path_of(ctx)
    ws = workspace_of(ctx, args.workspace_id)
    L = ctx.services.lines  # type: ignore[attr-defined]
    await ensure_target_indexed(ctx, ws, args.target)  # 走査がまだ届いていないファイルは先取りする（BUG-0070）
    pre = await asyncio.to_thread(_pre, db_path, ws, args)

    # 経路: LSP の呼び出し階層（PaaD の exact 行が新しければそれ）→ structural（降格）
    engine = EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL, warning=ACCURACY_WARNING)
    route = (
        await lx.route_for(ctx.services, ws, pre.info, Capability.CALL_HIERARCHY)
        if pre.info is not None
        else None
    )
    tree: _Tree | None = None
    source: lx.CallEdgeSource | None = None
    if route is not None:
        engine = replace(route.choice, warning=(route.choice.warning or "") + ACCURACY_NOTE)
        try:
            tree, source = await asyncio.to_thread(
                _work, db_path, ws, args, pre, _ExactCtx(asyncio.get_running_loop(), ctx.services, route)
            )
        except lx.LSP_FAILURES as exc:
            if not lx.is_lsp_failure(exc):
                raise
            _log.info("LSP call hierarchy failed; answering from the index: %r", exc)
            if route.exact:
                engine = lx.degraded_choice(route, exc)
                engine = replace(engine, warning=(engine.warning or "") + ACCURACY_NOTE)
        else:
            if source is not None and source.from_lsp:
                await source.flush()
                engine = route.choice
            else:
                engine = EngineChoice(
                    engine=ENGINE_LSP_CACHE,
                    confidence=Confidence.EXACT,
                    lsp_status=lx.lsp_status_of(ctx.services, ws, pre.info),
                )
    if tree is None:
        tree, source = await asyncio.to_thread(_work, db_path, ws, args, pre, None)
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
                    conf=n.confidence if n.match else None,
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
                    confidence=n.confidence,
                    group=n.site.path,
                )
            )

    edges = _edges(root_urn, all_nodes, args.include_guard_conditions)
    data: dict[str, Any] = {
        "root": tree.root.to_json(),  # 仕様書 §6.2 の SymbolRecord そのまま（null・空のキーも出す。BUG-0066）
        "direction": args.direction,
        "depth": args.depth,
        "edges": edges,
    }
    if tree.unattributed:
        data["unattributed_calls"] = tree.unattributed
    if args.format in ("dot", "mermaid"):
        data["graph"] = _graph(tree.root, all_nodes, edges)
    head = [("root", f"[{tree.root.kind.value}] {root_urn} direction={args.direction} depth={args.depth}")]
    if tree.root_truncated:
        head.append(("children", "; ".join(f"{d} {n} more" for d, n in tree.root_truncated.items())))
        data["root_children_truncated"] = dict(tree.root_truncated)
    warnings = ["the tree was cut at the node cap; narrow depth or max_children"] if tree.capped else []
    if source is not None and source.skipped:
        warnings.append(f"{source.skipped} call edge(s) were left out: the caller/callee is not in the index")
    if source is not None:
        warnings.extend(source.warnings)  # exact で答えたときだけ溜まる（LSP に開かせるファイルの上限など）
    warnings.extend(tree.warnings)
    return ToolOutput(
        records=records,
        max_confidence=Confidence.EXACT,
        engine=engine,
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
        "Who calls a function/method (incoming) or what it calls (outgoing), as a tree up to depth 5, with the condition around each call ('when:'). dot/mermaid draw the call graph. Syntax-tree resolution: dynamic dispatch and same-name methods are not told apart."
    ),
    allows_graph=True,
    max_confidence=Confidence.EXACT,
    milestone="M1",
    page_unit="tree-line",
    records_key="nodes",
    reads_index=True,
)
SPEC = TOOL_SPEC

__all__ = ["QueryCallHierarchyArgs", "SPEC", "TOOL_SPEC", "handler"]
