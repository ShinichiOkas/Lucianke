"""T-06 ``find_references_and_implementations``（仕様書 §11.3）。参照箇所（使用種別つき）・実装・型階層を列挙する。

``structural`` 版: PaaD の ``relations``（``target_urn`` が対象）から答える。
``exact`` 版（仕様書 T-06 処理規約 2）: LSP が ``ready`` なら ``references`` で集め（``_lsp_exact`` の経路）、``relations`` に ``exact`` で書き戻す。
2 回目以降は、目印が新しければ LSP を呼ばず PaaD の ``exact`` 行だけで答える（LSP が止まっていても ``exact``。設計書 R-11）。
LSP が ``ready`` でない・時間切れ・失敗なら ``structural``（今までどおり）に降格する。実装・型階層は LSP の実装検索が無いので
``exact`` の応答でも索引の継承関係からの推定のまま（応答の ``warnings`` に書く）。

* 参照: ``relations`` のうち ``target_urn`` が対象の行。1 参照箇所 = 1 レコード（``records_key="references"``）。同じ
  ``(path, line, col)`` に複数の行があれば 1 つにまとめる（使用種別は ``impl > type > call > rw > w > r > import`` で選ぶ）。
  並びは ``(path, line, col)``。``context_snippet`` の行はファイルから読む。
* 実装・型階層: ``INHERITS`` / ``IMPLEMENTS`` の行から推定する（直接の基底・派生と、派生の推移閉包）。ページネーションせず、
  ``head_kv`` と ``data_static`` に載せる（50 件を超えたら件数と先頭 50 件）。
* ``dot`` / ``mermaid``: ``data_static["graph"]``。参照元の囲み関数（モジュール直下ならファイル）→ 対象のエッジを ``use`` ラベルで。
  同じ囲みからの複数参照は ``mcp/render/graph.py`` が 1 本にまとめて ``×N`` を付ける。
* ``include_lexical_in_other_files``: 対象が Tier 1〜2 でも、Tier 3〜4 のファイルを単語境界 grep して ``lex:`` の参照を足す
  （その行は ``conf=lexical_fallback``。応答は ``mixed``）。

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
from lucianke.core.model import (
    Capability,
    Confidence,
    EngineChoice,
    FileInfo,
    Kind,
    SymbolRecord,
    Tier,
)
from lucianke.core.records import Record, ToolOutput
from lucianke.core.urn import LexSel, Urn
from lucianke.engine.selector import ENGINE_LSP_CACHE
from lucianke.index import queries
from lucianke.lsp import features
from lucianke.syntax.heuristic import lexical_scan
from lucianke.tools import _lsp_exact as lx
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._nav import (
    SourceLines,
    TargetRefArg,
    db_path_of,
    enclosing_name_of,
    file_urn,
    reader,
    resolve_target,
    use_of,
    workspace_of,
)
from lucianke.tools._rev import reject_unsupported_revision
from lucianke.workspace.paths import to_abs
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.tools.t06")

UseName = Literal["r", "w", "rw", "call", "type", "import", "impl"]

LIST_MAX = 50  # implementations / type_hierarchy の件数上限（仕様書 T-06 処理規約 5）
COMPACT_INLINE_MAX = 8  # compact の見出し行に並べる名前の数（残りは件数。json には先頭 50 件が全部入る）
TRANSITIVE_MAX = 500  # 派生の推移閉包を辿る数の歯止め
CLASS_KINDS = frozenset({Kind.CLASS, Kind.INTERFACE, Kind.STRUCT, Kind.TRAIT, Kind.ENUM})
_REF_KINDS = ("CALLS", "REFERENCES", "TYPE_USES", "IMPORTS", "INHERITS", "IMPLEMENTS")
_HIERARCHY_KINDS = ("INHERITS", "IMPLEMENTS")
_USE_RANK = {
    "impl": 6,
    "type": 5,
    "call": 4,
    "rw": 3,
    "w": 2,
    "r": 1,
    "import": 0,
}  # 同じ位置の複数の行から選ぶ

HIERARCHY_NOTE = (
    "実装と型階層は索引の継承関係（基底クラス名の一致）からの推定で、直接の基底・派生（実装は派生の推移閉包）だけです"
    "（LSP の実装検索は使っていません）。"
)

ACCURACY_WARNING = (
    "LSP が利用できないため構文木のみで解決しました。参照は識別子の一致で、スコープと型を区別しないため同名の別シンボルの参照を"
    "含みえます。実装と型階層は基底クラス名の一致からの推定で、直接の基底・派生（実装は派生の推移閉包）だけです。"
)


class FindReferencesArgs(CommonArgs):
    target: TargetRefArg
    query_type: Literal["references", "implementations", "type_hierarchy", "all"] = Field(
        default="all"
    )
    include_tests: bool = Field(default=True)
    include_declaration: bool = Field(default=False)
    context_snippet: bool = Field(
        default=True, description="Add the code of the reference line."
    )
    use_kinds: list[UseName] | None = Field(
        default=None
    )
    scope_paths: list[str] | None = Field(default=None)
    include_lexical_in_other_files: bool = Field(
        default=False,
        description="Also grep the name in non-code files as lexical refs.",
    )


# --- 結果の中間表現 -----------------------------------------------------------------------------


@dataclass(slots=True)
class _Ref:
    path: str
    line: int  # 1 始まり
    col: int  # 1 始まり
    use: str
    source_urn: str
    enclosing_urn: str | None
    enclosing_name: str | None
    snippet: str
    is_test: bool
    confidence: Confidence = Confidence.STRUCTURAL
    lexical: bool = False


@dataclass(slots=True)
class _Found:
    target: SymbolRecord
    language: str
    tier: Tier
    refs: list[_Ref] = field(default_factory=list)
    implementations: list[SymbolRecord] = field(default_factory=list)
    implementations_total: int = 0
    implementations_note: str = ""
    supertypes: list[SymbolRecord] = field(default_factory=list)
    subtypes: list[SymbolRecord] = field(default_factory=list)
    hierarchy_applicable: bool = False
    warnings: list[str] = field(default_factory=list)


def _glob_list(scope_paths: list[str]) -> list[str]:
    globs: list[str] = []
    for raw in scope_paths:
        g = raw.replace("\\", "/")
        parts = [p for p in g.split("/") if p]
        if not parts or g.startswith("/") or ".." in parts or (len(g) > 1 and g[1] == ":"):
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT,
                f"scope_paths: must be a workspace-relative path or glob: {raw!r}",
                details={"param": "scope_paths"},
            )
        g = "/".join(parts)
        globs += [g, g + "/**"]  # ファイル・glob、またはディレクトリ
    return globs


class _Files:
    """``is_test`` の判定（パスごとに 1 度）。"""

    def __init__(self, conn: sqlite3.Connection, workspace_id: str) -> None:
        self._conn, self._ws = conn, workspace_id
        self._cache: dict[str, bool] = {}

    def is_test(self, path: str) -> bool:
        if path not in self._cache:
            info = queries.file_by_path(self._conn, self._ws, path)
            self._cache[path] = bool(info and info.is_test)
        return self._cache[path]


# --- 型階層・実装 -------------------------------------------------------------------------------


def _symbols(conn: sqlite3.Connection, urns: list[str]) -> list[SymbolRecord]:
    seen: set[str] = set()
    out: list[SymbolRecord] = []
    for urn in urns:
        if urn in seen:
            continue
        seen.add(urn)
        sym = queries.symbol_by_urn(conn, urn)
        if sym is not None:
            out.append(sym)
    return out


def _by_location(symbols: list[SymbolRecord]) -> list[SymbolRecord]:
    return sorted(symbols, key=lambda s: (str(s.path), s.range.start.line, s.qualified_name))


def _direct_subtypes(
    conn: sqlite3.Connection, urn: str, files: _Files, include_tests: bool
) -> list[SymbolRecord]:
    rows = queries.references_to(conn, urn, _HIERARCHY_KINDS)
    syms = _symbols(conn, [r.source_urn for r in rows])
    return [s for s in syms if include_tests or not files.is_test(str(s.path))]


def _supertypes(conn: sqlite3.Connection, urn: str) -> list[SymbolRecord]:
    rows = queries.references_from(conn, urn, _HIERARCHY_KINDS)
    return _symbols(conn, [r.target_urn for r in rows])


def _transitive_subtypes(
    conn: sqlite3.Connection, urn: str, files: _Files, include_tests: bool
) -> list[SymbolRecord]:
    out: list[SymbolRecord] = []
    seen = {urn}
    queue = [urn]
    while queue and len(out) < TRANSITIVE_MAX:
        cur = queue.pop(0)
        for sub in _direct_subtypes(conn, cur, files, include_tests):
            key = str(sub.urn)
            if key in seen:
                continue
            seen.add(key)
            out.append(sub)
            queue.append(key)
    return out


def _owner_class(conn: sqlite3.Connection, ws_id: str, sym: SymbolRecord) -> SymbolRecord | None:
    owner_q = sym.qualified_name.rpartition(".")[0]
    if not owner_q:
        return None
    for cand in queries.symbols_by_qualified_suffix(conn, ws_id, str(sym.path), owner_q):
        if cand.qualified_name == owner_q and cand.kind in CLASS_KINDS:
            return cand
    return None


def _implementations(
    conn: sqlite3.Connection, ws_id: str, sym: SymbolRecord, files: _Files, include_tests: bool, found: _Found
) -> None:
    abstract = "abstract" in sym.modifiers
    if sym.kind in CLASS_KINDS:
        subs = _transitive_subtypes(conn, str(sym.urn), files, include_tests)
        if sym.kind in (Kind.INTERFACE, Kind.TRAIT) or abstract:
            impls = subs
            found.implementations_note = "(none)"
        else:
            impls = []
            found.implementations_note = "(none; concrete class, see type_hierarchy for subclasses)"
    elif sym.kind in (Kind.METHOD, Kind.FUNCTION, Kind.CONSTRUCTOR, Kind.PROPERTY):
        owner = _owner_class(conn, ws_id, sym)
        impls = []
        if owner is not None:
            for sub in _transitive_subtypes(conn, str(owner.urn), files, include_tests):
                want = f"{sub.qualified_name}.{sym.name}"
                impls += [
                    m
                    for m in queries.symbols_by_qualified_suffix(conn, ws_id, str(sub.path), want)
                    if m.qualified_name == want
                ]
        found.implementations_note = "(none)" if abstract else "(none; concrete method)"
    else:
        impls = []
        found.implementations_note = "(none; not an interface, class or method)"
    impls = _by_location(impls)
    found.implementations_total = len(impls)
    found.implementations = impls[:LIST_MAX]


# --- 参照 ---------------------------------------------------------------------------------------


def _collect_refs(
    conn: sqlite3.Connection,
    ws: Workspace,
    sym: SymbolRecord,
    args: FindReferencesArgs,
    files: _Files,
    source: SourceLines,
    exact_rows: Sequence[queries.ReferenceRow] | None = None,
) -> list[_Ref]:
    """``exact_rows`` があればそれだけを参照の元にする（LSP の答え、または PaaD の ``exact`` 行）。無ければ ``relations`` の全行。"""
    ws_id = ws.workspace_id
    in_scope: set[str] | None = None
    if args.scope_paths:
        in_scope = {f.path for f in queries.files_in_scope(conn, ws_id, _glob_list(args.scope_paths))}
    wanted = set(args.use_kinds) if args.use_kinds else None
    best: dict[tuple[str, int, int], _Ref] = {}
    if exact_rows is None:
        rows: Sequence[queries.ReferenceRow] = queries.references_to(
            conn, str(sym.urn), _REF_KINDS, include_tests=args.include_tests
        )
    else:
        rows = exact_rows
    for row in rows:
        if row.path is None or row.site is None:
            continue
        path = str(row.path)
        if in_scope is not None and path not in in_scope:
            continue
        is_test = files.is_test(path) if exact_rows is not None else row.is_test
        if not args.include_tests and is_test:
            continue
        use = use_of(row.use_kind, row.relation_kind)
        if wanted is not None and use not in wanted:
            continue
        key = (path, row.site.line + 1, row.site.col + 1)
        prev = best.get(key)
        if prev is not None and (
            _USE_RANK[prev.use] > _USE_RANK[use]
            or (
                _USE_RANK[prev.use] == _USE_RANK[use]
                and not (row.confidence is Confidence.EXACT and prev.confidence is not Confidence.EXACT)
            )
        ):
            continue
        name = enclosing_name_of(row.source_urn)
        best[key] = _Ref(
            path=path,
            line=key[1],
            col=key[2],
            use=use,
            source_urn=row.source_urn,
            enclosing_urn=row.source_urn if name is not None else None,
            enclosing_name=name,
            snippet=source.text(path, row.site.line) if args.context_snippet else "",
            is_test=is_test,
            confidence=row.confidence,
        )
    if args.include_declaration:
        sel = sym.selection or sym.range
        path = str(sym.path)
        parent = sym.qualified_name.rpartition(".")[0] or None
        key = (path, sel.start.line + 1, sel.start.col + 1)
        if (in_scope is None or path in in_scope) and (wanted is None or "r" in wanted) and key not in best:
            if args.include_tests or not files.is_test(path):
                best[key] = _Ref(
                    path=path,
                    line=key[1],
                    col=key[2],
                    use="r",
                    source_urn=str(sym.urn),
                    enclosing_urn=None,
                    enclosing_name=parent,
                    snippet=source.text(path, sel.start.line) if args.context_snippet else "",
                    is_test=files.is_test(path),
                    confidence=Confidence.EXACT if exact_rows is not None else sym.confidence,
                )
    return [best[k] for k in sorted(best)]


def _lexical_refs(
    conn: sqlite3.Connection,
    ws: Workspace,
    sym: SymbolRecord,
    args: FindReferencesArgs,
    known: set[tuple[str, int, int]],
    found: _Found,
) -> list[_Ref]:
    """Tier 3〜4 のファイルを単語境界 grep して ``lex:`` の参照にする（仕様書 T-06 の ``include_lexical_in_other_files``）。"""
    globs = _glob_list(args.scope_paths) if args.scope_paths else ()
    out: list[_Ref] = []
    for info in queries.files_in_scope(conn, ws.workspace_id, globs, include_tests=args.include_tests):
        if int(info.tier) < int(Tier.T3):
            continue
        try:
            data = to_abs(ws, info.path).read_bytes()
        except (OSError, ValueError):
            continue
        try:
            scan = lexical_scan(data, sym.name, context=0)
        except ValueError:
            return out
        if scan.timed_out or scan.truncated:
            found.warnings.append(f"lexical search of {info.path} was cut short")
        for hit in scan.hits:
            key = (str(info.path), hit.line + 1, hit.col + 1)
            if key in known:
                continue
            out.append(
                _Ref(
                    path=str(info.path),
                    line=key[1],
                    col=key[2],
                    use="w" if hit.is_definition else "r",
                    source_urn=str(Urn(ws.workspace_id, str(info.path), LexSel(sym.name, key[1]))),
                    enclosing_urn=None,
                    enclosing_name=None,
                    snippet=hit.line_text.strip() if args.context_snippet else "",
                    is_test=info.is_test,
                    confidence=Confidence.LEXICAL_FALLBACK,
                    lexical=True,
                )
            )
    return out


@dataclass(slots=True)
class _Pre:
    """対象の解決と、2 回目以降（PaaD の ``exact`` 行で答えてよいか）の判定。"""

    sym: SymbolRecord
    info: FileInfo | None
    token: str  # この時点の files_fingerprint（目印の鮮度。LSP に聞く前に取る）
    cached: list[queries.ReferenceRow] | None  # 目印が新しいときの PaaD の exact 行
    structural_hits: int = 0  # relations にある参照の数（LSP が 0 件と答えたときの突き合わせ用）
    # exact 経路で足す警告（LSP に開かせるファイルの上限など）
    warnings: list[str] = field(default_factory=list)


def _refs_wanted(args: FindReferencesArgs) -> bool:
    return args.query_type in ("references", "all")


def _load_refs(urn: str) -> Any:
    return lambda conn: queries.references_to(conn, urn, _REF_KINDS)


def _pre(db_path: Any, ws: Workspace, args: FindReferencesArgs) -> _Pre:
    with reader(db_path) as conn:
        sym = resolve_target(conn, ws, args.target)
        info = queries.file_by_path(conn, ws.workspace_id, str(sym.path))
        token = queries.files_fingerprint(conn, ws.workspace_id)
        cached = (
            lx.cached_rows(
                conn, db_path, ws.workspace_id, "refs", str(sym.urn), _load_refs(str(sym.urn)), token=token
            )
            if _refs_wanted(args)
            else None
        )
        hits = queries.count_references_to(conn, str(sym.urn), _REF_KINDS) if cached is None else 0
        return _Pre(sym, info, token, cached, hits)


def _work(
    db_path: Any,
    ws: Workspace,
    args: FindReferencesArgs,
    pre: _Pre,
    exact_rows: Sequence[queries.ReferenceRow] | None = None,
) -> _Found:
    """同期の本体（``asyncio.to_thread`` で走らせる）。読み取り接続はここで開いて閉じる。"""
    with reader(db_path) as conn:
        sym, info = pre.sym, pre.info
        found = _Found(
            target=sym,
            language=info.language if info is not None else "n/a",
            tier=info.tier if info is not None else Tier.T1,
        )
        files = _Files(conn, ws.workspace_id)
        source = SourceLines(ws)
        if args.query_type in ("references", "all"):
            found.refs = _collect_refs(conn, ws, sym, args, files, source, exact_rows)
            if args.include_lexical_in_other_files:
                known = {(r.path, r.line, r.col) for r in found.refs}
                found.refs += _lexical_refs(conn, ws, sym, args, known, found)
                found.refs.sort(key=lambda r: (r.path, r.line, r.col))
        if args.query_type in ("implementations", "all"):
            _implementations(conn, ws.workspace_id, sym, files, args.include_tests, found)
        if args.query_type in ("type_hierarchy", "all") and sym.kind in CLASS_KINDS:
            found.hierarchy_applicable = True
            found.supertypes = _by_location(_supertypes(conn, str(sym.urn)))[:LIST_MAX]
            found.subtypes = _by_location(_direct_subtypes(conn, str(sym.urn), files, args.include_tests))[
                :LIST_MAX
            ]
        if info is None and found.refs:  # 外部シンボルは自分のファイルが無い。参照元の言語で代える
            first = queries.file_by_path(conn, ws.workspace_id, found.refs[0].path)
            if first is not None:
                found.language, found.tier = first.language, first.tier
        return found


# --- 出力 ---------------------------------------------------------------------------------------


def _sym_label(s: SymbolRecord) -> str:
    return f"{s.qualified_name} @{s.path}:{s.range.start.line + 1}"


def _sym_list(symbols: list[SymbolRecord], total: int) -> str:
    shown = ", ".join(_sym_label(s) for s in symbols[:COMPACT_INLINE_MAX])
    more = total - min(len(symbols), COMPACT_INLINE_MAX)
    return f"{total}: {shown}" + (f", … {more} more (format=json lists up to {LIST_MAX})" if more > 0 else "")


def _ref_record(L: Any, r: _Ref, priority: int, target_urn: str, show_snippet: bool) -> Record:
    snippet = r.snippet if show_snippet else ""
    suffix = "  conf=lexical_fallback" if r.lexical else ""
    if suffix and snippet:  # 末尾の印を足しても 200 文字に収める（規約 6）
        head = L.ref_line(r.path, r.line, r.col, use=r.use, enclosing=r.enclosing_name, snippet="x")[:-1]
        room = L.LINE_LIMIT - len(head) - len(suffix)
        if len(snippet) > room:
            snippet = snippet[: max(room - 1, 0)].rstrip() + "…"
    line = (
        L.ref_line(r.path, r.line, r.col, use=r.use, enclosing=r.enclosing_name, snippet=snippet).rstrip()
        + suffix
    )
    return Record(
        kind="ref",
        key=f"{r.path}:{r.line}:{r.col}",
        priority=priority,
        lines=(line,),
        data={
            "path": r.path,
            "line": r.line,
            "col": r.col,
            "use": r.use,
            "enclosing_urn": r.enclosing_urn,
            "enclosing_name": r.enclosing_name,
            "snippet": r.snippet,
            "is_test": r.is_test,
            "confidence_level": r.confidence.value,
            "source_urn": r.source_urn,
            "target_urn": target_urn,
        },
        confidence=r.confidence,
        group=r.path,
    )


def _graph(found: _Found, ws_id: str) -> dict[str, Any]:
    target_urn = str(found.target.urn)
    nodes: dict[str, dict[str, Any]] = {
        target_urn: {"id": target_urn, "label": found.target.qualified_name, "path": str(found.target.path)}
    }
    edges: list[dict[str, Any]] = []
    for r in found.refs:
        src = r.enclosing_urn or file_urn(ws_id, r.path)
        if src not in nodes:
            nodes[src] = {"id": src, "label": r.enclosing_name or r.path, "path": r.path}
            if r.lexical:
                nodes[src]["conf"] = r.confidence.value
        edges.append(
            {
                "source": src,
                "target": target_urn,
                "label": r.use,
                "count": 1,
                "conf": r.confidence.value if r.lexical else None,
            }
        )
    return {"nodes": list(nodes.values()), "edges": edges}


def _confirm(db_path: Any, ws_id: str, urn: str, rows: Sequence[queries.ReferenceRow], token: str) -> bool:
    with reader(db_path) as conn:
        return lx.confirm_marker(conn, db_path, ws_id, "refs", urn, _load_refs(urn), rows, token=token)


def _prime_set(db_path: Any, ws: Workspace, handle: Any, sym: SymbolRecord) -> lx.PrimeSet:
    with reader(db_path) as conn:
        return lx.prime_candidates(
            conn, ws, name=sym.name, exclude=str(sym.path), is_open=handle.documents.is_open
        )


async def _exact_references(
    ctx: ToolContext, ws: Workspace, db_path: Any, route: lx.LspRoute, pre: _Pre
) -> list[queries.ReferenceRow]:
    """LSP の ``references`` で集め、``relations`` に ``exact`` で書き戻して、その行を返す（失敗は ``lx.LSP_FAILURES``）。"""
    sym = pre.sym
    assert route.handle is not None
    if lx.needs_priming(route.file.language):
        # tsserver は tsconfig の include に入らないファイル（test/ など）を、開かれるまで探さない（BUG-0047）。
        # 対象の名前を含むファイルを先に開いてから聞くので、結果は「LSP が見たものそのまま」のまま（exact）。
        prime = await asyncio.to_thread(_prime_set, db_path, ws, route.handle, sym)
        await lx.prime_documents(route.handle, prime)
        note = prime.warning(sym.name)
        if note:
            pre.warnings.append(note)
    sel = sym.selection or sym.range
    locs = await features.references(route.handle, sym.path, sel.start, include_declaration=False)
    target_urn = str(sym.urn)
    sites = lx.SiteIndex(ctx.services, ws)
    facts = []
    rows: list[queries.ReferenceRow] = []
    for loc in locs:
        rel = str(loc.rel)
        start = (loc.range.start.line, loc.range.start.col)
        if loc.rel == sym.path and (sel.start.line, sel.start.col) <= start < (sel.end.line, sel.end.col):
            continue  # 宣言そのもの
        fact = lx.reference_fact(
            target_urn, rel, loc.range, (await sites.of(rel)).get(start), ws.workspace_id
        )
        if fact is not None:
            facts.append(fact)
            rows.append(lx.row_of(fact, rel))
    if not rows and pre.structural_hits:
        # 起動直後の LSP は ``ready`` でも解析が済まず 0 件を返すことがある（basedpyright の実測）。索引に参照があるのに
        # 0 件なら信用せず、LSP は索引中として構文木の答えに降格する（呼び直すと exact になる）。
        raise LuciankeError(
            ErrorCode.E_LSP_UNAVAILABLE,
            "the LSP found no references although the index has some; it may not have finished analysing",
            details={"reason": "indexing"},
        )
    wrote = await lx.write_facts(ctx.services, facts, label=f"lsp:references:{sym.qualified_name}")
    if wrote:  # 0 件の答えは覚えない（未完了の LSP の空振りを固定しないため）
        await asyncio.to_thread(_confirm, db_path, ws.workspace_id, target_urn, rows, pre.token)
    return rows


async def handler(ctx: ToolContext, args: FindReferencesArgs) -> ToolOutput:
    await reject_unsupported_revision(ctx, args.revision)
    db_path = db_path_of(ctx)
    ws = workspace_of(ctx, args.workspace_id)
    L = ctx.services.lines  # type: ignore[attr-defined]
    pre = await asyncio.to_thread(_pre, db_path, ws, args)

    # 経路: PaaD の exact 行（2 回目以降）→ LSP（1 回目）→ structural（降格）
    engine = EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL, warning=ACCURACY_WARNING)
    exact_rows: Sequence[queries.ReferenceRow] | None = None
    if _refs_wanted(args):
        if pre.cached is not None:
            exact_rows = pre.cached
            engine = EngineChoice(
                engine=ENGINE_LSP_CACHE,
                confidence=Confidence.EXACT,
                lsp_status=lx.lsp_status_of(ctx.services, ws, pre.info),
            )
        elif pre.info is not None:
            route = await lx.route_for(ctx.services, ws, pre.info, Capability.REFERENCES)
            if route is not None:
                engine = route.choice
                if route.exact:
                    try:
                        exact_rows = await _exact_references(ctx, ws, db_path, route, pre)
                    except lx.LSP_FAILURES as exc:
                        if not lx.is_lsp_failure(exc):
                            raise
                        _log.info("LSP references failed; answering from the index: %r", exc)
                        engine = lx.degraded_choice(route, exc)
                if exact_rows is None:
                    engine = replace(engine, warning=(engine.warning or "") + HIERARCHY_NOTE)
    found = await asyncio.to_thread(_work, db_path, ws, args, pre, exact_rows)

    target_urn = str(found.target.urn)
    records = [_ref_record(L, r, i, target_urn, args.context_snippet) for i, r in enumerate(found.refs)]
    files = {r.path for r in found.refs}
    tests = sum(1 for r in found.refs if r.is_test)
    totals = {"files": len(files), "sites": len(found.refs), "tests": tests}

    head: list[tuple[str, str]] = [("target", f"[{found.target.kind.value}] {target_urn}")]
    if args.query_type in ("implementations", "all"):
        head.append(
            (
                "implementations",
                _sym_list(found.implementations, found.implementations_total)
                if found.implementations_total
                else found.implementations_note,
            )
        )
    if args.query_type in ("type_hierarchy", "all"):
        if found.hierarchy_applicable:
            head.append(
                (
                    "type_hierarchy",
                    f"supertypes {_sym_list(found.supertypes, len(found.supertypes)) if found.supertypes else '(none)'}"
                    f"; subtypes {_sym_list(found.subtypes, len(found.subtypes)) if found.subtypes else '(none)'}",
                )
            )
        else:
            head.append(("type_hierarchy", "(n/a)"))
    if args.query_type in ("references", "all"):
        head.append(("references", f"{totals['files']} files, {totals['sites']} sites (tests: {tests})"))

    hierarchy_note = (
        [HIERARCHY_NOTE]
        if exact_rows is not None and args.query_type in ("implementations", "type_hierarchy", "all")
        else []
    )
    data: dict[str, Any] = {
        "target": found.target.to_json(),
        "implementations": [s.to_json() for s in found.implementations],
        "implementations_total": found.implementations_total,
        "type_hierarchy": (
            {
                "supertypes": [s.to_json() for s in found.supertypes],
                "subtypes": [s.to_json() for s in found.subtypes],
            }
            if found.hierarchy_applicable
            else None
        ),
        "totals": totals,
    }
    if args.format in ("dot", "mermaid"):
        data["graph"] = _graph(found, ws.workspace_id)
    return ToolOutput(
        records=records,
        max_confidence=Confidence.EXACT,
        engine=engine,
        page_unit="site",
        head_kv=head,
        data_static=data,
        tier=found.tier,
        detected_language=found.language,
        warnings=tuple(found.warnings)
        + tuple(pre.warnings if exact_rows is not None else ())
        + tuple(hierarchy_note),
        total_estimate=None,
    )


TOOL_SPEC = ToolSpec(
    name="find_references_and_implementations",
    tid="T-06",
    args_model=FindReferencesArgs,
    handler=handler,
    default_max_tokens=2000,
    description=(
        "All references to one symbol (use, enclosing function, code of the line), plus implementations and type hierarchy. Syntax-tree resolution: same-name symbols are not told apart."
    ),
    allows_graph=True,
    max_confidence=Confidence.EXACT,
    milestone="M1",
    page_unit="site",
    records_key="references",
)
SPEC = TOOL_SPEC

__all__ = ["FindReferencesArgs", "SPEC", "TOOL_SPEC", "handler"]
