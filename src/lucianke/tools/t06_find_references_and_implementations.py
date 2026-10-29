"""T-06 ``find_references_and_implementations``（仕様書 §11.3）。参照箇所（使用種別つき）・実装・型階層を列挙する。

``structural`` 版: PaaD の ``relations``（``target_urn`` が対象）から答える。LSP による ``exact`` は別の役が足す。

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
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice, Kind, SymbolRecord, Tier
from lucianke.core.records import Record, ToolOutput
from lucianke.index import queries
from lucianke.syntax.heuristic import lexical_scan
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._nav import (
    SourceLines,
    TargetRefArg,
    db_path_of,
    enclosing_name_of,
    file_urn,
    reader,
    require_working_tree,
    resolve_target,
    use_of,
    workspace_of,
)
from lucianke.workspace.paths import to_abs
from lucianke.workspace.types import Workspace

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

ACCURACY_WARNING = (
    "LSP が利用できないため構文木のみで解決しました。参照は識別子の一致で、スコープと型を区別しないため同名の別シンボルの参照を"
    "含みえます。実装と型階層は基底クラス名の一致からの推定で、直接の基底・派生（実装は派生の推移閉包）だけです。"
)


class FindReferencesArgs(CommonArgs):
    target: TargetRefArg = Field(description="The symbol (urn, or path + symbol_name, or path + line).")
    query_type: Literal["references", "implementations", "type_hierarchy", "all"] = Field(
        default="all", description="What to list."
    )
    include_tests: bool = Field(default=True, description="Include references from test files.")
    include_declaration: bool = Field(default=False, description="Include the declaration site itself.")
    context_snippet: bool = Field(
        default=True, description="Add the code of the reference line (cut at 200 chars)."
    )
    use_kinds: list[UseName] | None = Field(
        default=None, description="Only these uses: r w rw call type import impl."
    )
    scope_paths: list[str] | None = Field(default=None, description="Only references in these paths/globs.")
    include_lexical_in_other_files: bool = Field(
        default=False,
        description="Also grep the name (word boundary) in Tier 3-4 files (config/templates/unknown) as lexical refs.",
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
            found.implementations_note = "(none; concrete class — see type_hierarchy for subclasses)"
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
) -> list[_Ref]:
    ws_id = ws.workspace_id
    in_scope: set[str] | None = None
    if args.scope_paths:
        in_scope = {f.path for f in queries.files_in_scope(conn, ws_id, _glob_list(args.scope_paths))}
    wanted = set(args.use_kinds) if args.use_kinds else None
    best: dict[tuple[str, int, int], _Ref] = {}
    for row in queries.references_to(conn, str(sym.urn), _REF_KINDS, include_tests=args.include_tests):
        if row.path is None or row.site is None:
            continue
        path = str(row.path)
        if in_scope is not None and path not in in_scope:
            continue
        use = use_of(row.use_kind, row.relation_kind)
        if wanted is not None and use not in wanted:
            continue
        key = (path, row.site.line + 1, row.site.col + 1)
        prev = best.get(key)
        if prev is not None and _USE_RANK[prev.use] >= _USE_RANK[use]:
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
            is_test=row.is_test,
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
                    confidence=sym.confidence,
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
                    source_urn=file_urn(ws.workspace_id, str(info.path)),
                    enclosing_urn=None,
                    enclosing_name=None,
                    snippet=hit.line_text.strip() if args.context_snippet else "",
                    is_test=info.is_test,
                    confidence=Confidence.LEXICAL_FALLBACK,
                    lexical=True,
                )
            )
    return out


def _work(db_path: Any, ws: Workspace, args: FindReferencesArgs) -> _Found:
    """同期の本体（``asyncio.to_thread`` で走らせる）。読み取り接続はここで開いて閉じる。"""
    with reader(db_path) as conn:
        sym = resolve_target(conn, ws, args.target)
        info = queries.file_by_path(conn, ws.workspace_id, str(sym.path))
        found = _Found(
            target=sym,
            language=info.language if info is not None else "n/a",
            tier=info.tier if info is not None else Tier.T1,
        )
        files = _Files(conn, ws.workspace_id)
        source = SourceLines(ws)
        if args.query_type in ("references", "all"):
            found.refs = _collect_refs(conn, ws, sym, args, files, source)
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
    line = L.ref_line(r.path, r.line, r.col, use=r.use, enclosing=r.enclosing_name, snippet=snippet)
    if r.lexical:
        line = line.rstrip() + "  conf=lexical_fallback"
    return Record(
        kind="ref",
        key=f"{r.path}:{r.line}:{r.col}",
        priority=priority,
        lines=(line.rstrip(),),
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


async def handler(ctx: ToolContext, args: FindReferencesArgs) -> ToolOutput:
    require_working_tree(args.revision)
    db_path = db_path_of(ctx)
    ws = workspace_of(ctx, args.workspace_id)
    L = ctx.services.lines  # type: ignore[attr-defined]
    found = await asyncio.to_thread(_work, db_path, ws, args)

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
        engine=EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL, warning=ACCURACY_WARNING),
        page_unit="site",
        head_kv=head,
        data_static=data,
        tier=found.tier,
        detected_language=found.language,
        warnings=tuple(found.warnings),
        total_estimate=None,
    )


TOOL_SPEC = ToolSpec(
    name="find_references_and_implementations",
    tid="T-06",
    args_model=FindReferencesArgs,
    handler=handler,
    default_max_tokens=2000,
    description=(
        "List every reference to one symbol (with the use: r w rw call type import impl, the enclosing function and the "
        "code of the line), plus its implementations and type hierarchy. Target by urn, or path + symbol_name, or "
        "path + line. dot/mermaid draw enclosing-function -> target edges. Syntax-tree resolution: same-name symbols "
        "are not told apart."
    ),
    allows_graph=True,
    max_confidence=Confidence.EXACT,
    milestone="M1",
    page_unit="site",
    records_key="references",
)
SPEC = TOOL_SPEC

__all__ = ["FindReferencesArgs", "SPEC", "TOOL_SPEC", "handler"]
