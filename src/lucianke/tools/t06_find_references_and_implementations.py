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
* 型の分からない受け手の呼び出し（``x.pack(...)`` で ``x`` が ``Any`` など。BUG-0071 / BUG-0077、``_name_calls``）: 対象が ``Method`` で、
  呼び出し箇所ごとに近い段（同じファイル → 箇所を含むテストフィクスチャ → ワークスペース。テストダブルは同じファイルからだけ）の
  同名のメソッドが対象 1 つだけ（組み込み型と同名でない）なら、``structural`` の答えにその位置を ``use=call`` ・
  ``conf=heuristic`` ・json の ``match: "name"`` で足す。同じ段に同名が複数・組み込み型と同名なら足さず、件数と先頭の位置を
  ``warnings`` と ``data.unattributed_calls`` に書く。``exact`` の答えの ``references`` には足さず、結べるのに LSP の答えに無い位置
  （``Any`` の受け手は LSP も型を決められない）を **補足の欄** に並べる（BUG-0107）: compact は本体の後ろに
  ``- not in exact answer (matched by name; receiver type unknown to the LSP): N sites`` の見出しと ``conf=heuristic`` の行、
  json は ``data.name_matched``（``references`` と同じ形。``Record.section`` なので予算・ページネーションは本体と同じ、精度の
  集計には数えない）。見出しの ``references:`` に ``+ N by name, not confirmed by the LSP``、``totals.name_matched``。
* 型の分からない受け手の属性の読み書き（``mon.count += 1``・``x.count``。BUG-0095、``_name_calls`` の ``use_kind`` が ``r`` / ``w`` /
  ``rw``）: 対象がクラスの ``Field`` / ``Property`` / ``Constant`` か ``Method``（メソッドの参照）なら、同じ段の規則で同名のメンバーと
  インスタンス属性（``self.x = ...``）が対象 1 つだけ（組み込み型の属性・メソッドとも、型の分かる受け手が使った外部の型の
  メンバーとも同名でない）のとき、その位置を ``use=r/w/rw``・
  ``conf=heuristic``・``match: "name"`` で足す。足せない位置は件数・候補・先頭の位置を ``warnings`` と ``data.unattributed_refs`` に書く。
* 結ばなかった ``super()``・継承メンバーの参照（BUG-0081、``_blocked_refs``）: 受け手のクラスの線形化で中身の分からない外部の基底などに
  先に当たって結ばなかった参照のうち、対象のクラスがその基底の後ろにあるものを、``structural`` の答えで件数・理由・先頭の位置として
  ``warnings`` と ``data.unattributed_calls``（``reason: external_base | unknown_base``）に書く。

``ctx.services`` に求めるもの（duck typing）: ``db_path``、``workspace(workspace_id)``、``lines``（compact の行の関数）。
``exact`` 経路には ``lsp_pool`` と書き手（``runtime().writer``）、``trees`` も使う。詳細は ``_lsp_exact`` の docstring。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
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
from lucianke.tools import _blocked_refs as br
from lucianke.tools import _lsp_exact as lx
from lucianke.tools import _name_calls as nc
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._building import ensure_target_indexed
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
    query_type: Literal["references", "implementations", "type_hierarchy", "all"] = Field(default="all")
    include_tests: bool = Field(default=True)
    include_declaration: bool = Field(default=False)
    context_snippet: bool = Field(default=True, description="Add the code of the reference line.")
    use_kinds: list[UseName] | None = Field(default=None)
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
    match: str | None = None  # 名前だけで結んだ参照は ``"name"``（BUG-0071）


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
    by_name: int = 0  # 名前だけで結んで足した参照の数
    # exact の答えに無い、名前で結べる位置（補足の欄 ``data.name_matched``。references には混ぜない。BUG-0107）
    name_matched: list[_Ref] = field(default_factory=list)
    unattributed: list[dict[str, Any]] = field(default_factory=list)  # data.unattributed_calls
    # data.unattributed_refs（属性の読み書き。BUG-0095）
    unattributed_refs: list[dict[str, Any]] = field(default_factory=list)


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


def _scope_set(conn: sqlite3.Connection, ws_id: str, args: FindReferencesArgs) -> set[str] | None:
    if not args.scope_paths:
        return None
    return {f.path for f in queries.files_in_scope(conn, ws_id, _glob_list(args.scope_paths))}


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
    in_scope = _scope_set(conn, ws.workspace_id, args)
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
    if not args.include_declaration and nc.is_instance_attr(sym):
        # インスタンス属性の宣言（最初の ``self.x = ...``）は書き込みの参照の行でもある。宣言を含めないなら外す（BUG-0122）
        sel = sym.selection or sym.range
        best.pop((str(sym.path), sel.start.line + 1, sel.start.col + 1), None)
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


def _name_ref(r: queries.NameCallRow, args: FindReferencesArgs, source: SourceLines) -> _Ref:
    """名前で結んだ型不明の呼び出し・属性の読み書きの 1 箇所（``conf=heuristic``・``match: "name"``）。"""
    key = nc.site_key(r)
    ename = enclosing_name_of(r.source_urn)
    return _Ref(
        path=key[0],
        line=key[1],
        col=key[2],
        use=use_of(r.use_kind, "CALLS" if r.use_kind == "call" else "REFERENCES"),
        source_urn=r.source_urn,
        enclosing_urn=r.source_urn if ename is not None else None,
        enclosing_name=ename,
        snippet=source.text(key[0], r.site.line) if args.context_snippet else "",
        is_test=r.is_test,
        confidence=Confidence.HEURISTIC,
        match=nc.MATCH_NAME,
    )


def _name_refs(
    conn: sqlite3.Connection,
    ws: Workspace,
    sym: SymbolRecord,
    args: FindReferencesArgs,
    source: SourceLines,
    found: _Found,
    *,
    exact: bool,
) -> None:
    """型の分からない受け手の呼び出し ``x.<name>(...)`` と属性の読み書き ``x.<name>``（BUG-0095）を名前で足す・要約する
    （BUG-0071 / BUG-0077。``_name_calls`` の docstring）。

    判定は箇所ごと（同じファイル → 箇所を含むテストフィクスチャ → ワークスペース）。対象より近い同名の定義がある箇所は
    足しも書きもしない。結べないものは、呼び出しは ``data.unattributed_calls``、属性の読み書きは ``data.unattributed_refs`` に書く。
    ``exact`` の答え（``exact=True``）では ``found.refs`` に足さず、結べる位置を ``found.name_matched``（補足の欄）に置く（BUG-0107）。
    """
    names = nc.NameIndex(conn, ws.workspace_id)
    target = names.target_member(sym)
    if target is None:
        return
    name, family, uses = target
    if args.use_kinds:
        uses = tuple(u for u in uses if u in args.use_kinds)
    if not uses:
        return
    in_scope = _scope_set(conn, ws.workspace_id, args)
    known = {(r.path, r.line, r.col) for r in found.refs}
    sites = names.for_target(
        sym,
        (
            r
            for r in names.sites_named(name, family, include_tests=args.include_tests, uses=uses)
            if nc.site_key(r) not in known and (in_scope is None or str(r.path) in in_scope)
        ),
    )
    owned_calls = [(r, v) for r, v in sites.owned if r.use_kind == "call"]
    owned_refs = [(r, v) for r, v in sites.owned if r.use_kind != "call"]
    if exact:
        # references は LSP の答えのまま。結べる（その箇所の候補が対象 1 つだけ）のに LSP の答えに無い位置は、別の欄
        # （data.name_matched・compact の「not in exact answer」の後ろ）に印つきで並べる（BUG-0107。件数だけだと位置が
        # 見えず、構文木の答えより情報が減っていた）。結べない（同名が同じ近さに複数）なら LSP の答えが正。足しも書きもしない
        for owned, what in (
            (owned_calls, f"call site(s) of .{name}()"),
            (owned_refs, f"read/write reference(s) of .{name}"),
        ):
            if not owned:
                continue
            found.warnings.append(
                f"{len(owned)} {what} are not in this exact answer because the LSP could not type the receiver"
                f" (e.g. Any); {nc.owned_text(owned)}, so they are listed after the exact references under"
                f" 'not in exact answer', marked conf=heuristic (data.name_matched)"
            )
        found.name_matched = sorted(
            (_name_ref(r, args, source) for r, _ in sites.owned), key=lambda x: (x.path, x.line, x.col)
        )
        return
    found.refs += [_name_ref(r, args, source) for r, _ in sites.owned]
    if sites.owned:
        found.refs.sort(key=lambda x: (x.path, x.line, x.col))
        found.by_name = len(sites.owned)
    if owned_calls:
        found.warnings.append(
            f"{len(owned_calls)} call site(s) marked conf=heuristic are matched by name only: the receiver's type is"
            f" unknown and {nc.owned_text(owned_calls)}"
        )
    if owned_refs:
        found.warnings.append(
            f"{len(owned_refs)} read/write reference(s) marked conf=heuristic are matched by name only: the receiver's"
            f" type is unknown and {nc.owned_text(owned_refs)}"
        )
    for _key, u in sorted(sites.unattributed.items()):
        left = u.sorted_rows()
        if u.verdict.pool == "call":
            found.warnings.append(
                f"{len(left)} call site(s) of .{name}() with a receiver of unknown type are not listed:"
                f" {u.verdict.reason()}; e.g. {nc.sample_text(left)} (data.unattributed_calls)"
            )
            found.unattributed.append(u.to_json(enclosing_name_of))
        else:
            found.warnings.append(
                f"{len(left)} read/write reference(s) of .{name} through a receiver of unknown type are not listed:"
                f" {u.verdict.reason()}; e.g. {nc.sample_text(left)} (data.unattributed_refs)"
            )
            found.unattributed_refs.append(u.to_json(enclosing_name_of))


def _blocked_refs(
    conn: sqlite3.Connection, ws: Workspace, sym: SymbolRecord, args: FindReferencesArgs, found: _Found
) -> None:
    """結ばなかった ``super()``・継承メンバーの参照のうち、対象に届くかもしれないものを理由つきで書く（BUG-0081。処理規約 9）。"""
    notes = br.blocked_notes(
        conn,
        ws.workspace_id,
        sym,
        enclosing_name_of,
        include_tests=args.include_tests,
        use_kinds=args.use_kinds or None,
        in_scope=_scope_set(conn, ws.workspace_id, args),
        known={(r.path, r.line, r.col) for r in found.refs},
    )
    found.warnings += notes.warnings
    found.unattributed += notes.unattributed


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
    # relations で参照している（LSP の答えに入りそうな）ファイル。references を待つ間に参照箇所の分類を始める（BUG-0134）
    likely_files: list[str] = field(default_factory=list)


def _refs_wanted(args: FindReferencesArgs) -> bool:
    return args.query_type in ("references", "all")


def _load_refs(urn: str) -> Any:
    return lambda conn: queries.references_to(conn, urn, _REF_KINDS)


def _pre(db_path: Any, ws: Workspace, args: FindReferencesArgs) -> _Pre:
    with reader(db_path) as conn:
        sym = resolve_target(conn, ws, args.target, instance_attrs=True)  # インスタンス属性も対象（BUG-0122）
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
        likely = queries.referencing_paths(conn, str(sym.urn), _REF_KINDS) if hits else []
        return _Pre(sym, info, token, cached, hits, likely_files=likely)


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
            _name_refs(conn, ws, sym, args, source, found, exact=exact_rows is not None)
            if exact_rows is None:
                _blocked_refs(conn, ws, sym, args, found)
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


NAME_MATCHED_SECTION = "name_matched"  # exact の答えの補足の欄（json の data のキー。BUG-0107）


def name_matched_header(n: int) -> str:
    """compact の補足の欄の見出し（``note``）。欄の最初のレコードの先頭の行。"""
    return f"not in exact answer (matched by name; receiver type unknown to the LSP): {n} sites"


def _name_matched_records(
    L: Any, found: _Found, start: int, target_urn: str, show_snippet: bool
) -> list[Record]:
    """``found.name_matched`` のレコード（本体の後ろ。``Record.section`` で json の ``data.name_matched`` に置く）。

    見出しは欄の最初のレコードの行に含める（ページの末尾に見出しだけが残らない）。続きのページでは見出しは出ないが、
    各行の ``conf=heuristic`` と見出しの kv 行（``references: … + N by name``）で本体と区別できる。
    """
    out: list[Record] = []
    for i, r in enumerate(found.name_matched):
        rec = _ref_record(L, r, start + i, target_urn, show_snippet, section=NAME_MATCHED_SECTION)
        if i == 0:
            rec = replace(rec, lines=(L.note(name_matched_header(len(found.name_matched))), *rec.lines))
        out.append(rec)
    return out


def _ref_record(
    L: Any, r: _Ref, priority: int, target_urn: str, show_snippet: bool, *, section: str | None = None
) -> Record:
    snippet = r.snippet if show_snippet else ""
    suffix = "  conf=lexical_fallback" if r.lexical else ("  conf=heuristic" if r.match else "")
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
            **({"match": r.match} if r.match else {}),
        },
        confidence=r.confidence,
        group=r.path,
        section=section,
    )


def _graph(found: _Found, ws_id: str) -> dict[str, Any]:
    target_urn = str(found.target.urn)
    nodes: dict[str, dict[str, Any]] = {
        target_urn: {"id": target_urn, "label": found.target.qualified_name, "path": str(found.target.path)}
    }
    edges: list[dict[str, Any]] = []
    for r in [*found.refs, *found.name_matched]:  # 補足の欄も印（conf=heuristic）つきで描く（BUG-0107）
        src = r.enclosing_urn or file_urn(ws_id, r.path)
        if src not in nodes:
            nodes[src] = {"id": src, "label": r.enclosing_name or r.path, "path": r.path}
            if r.lexical or r.match:
                nodes[src]["conf"] = r.confidence.value
        edges.append(
            {
                "source": src,
                "target": target_urn,
                "label": r.use,
                "count": 1,
                "conf": r.confidence.value if r.lexical or r.match else None,
            }
        )
    return {"nodes": list(nodes.values()), "edges": edges}


def _confirm(db_path: Any, ws_id: str, urn: str, rows: Sequence[queries.ReferenceRow], token: str) -> bool:
    with reader(db_path) as conn:
        return lx.confirm_marker(conn, db_path, ws_id, "refs", urn, _load_refs(urn), rows, token=token)


def _prime_set(db_path: Any, ws: Workspace, handle: Any, sym: SymbolRecord, language: str) -> lx.PrimeSet:
    with reader(db_path) as conn:
        return lx.prime_set(
            conn,
            ws,
            language,
            name=sym.name,
            exclude=str(sym.path),
            is_open=handle.documents.is_open,
            scope=getattr(handle, "priming_scope", None),
        )


async def _exact_references(
    ctx: ToolContext,
    ws: Workspace,
    db_path: Any,
    route: lx.LspRoute,
    pre: _Pre,
    budget: lx.ExactBudget,
) -> list[queries.ReferenceRow]:
    """LSP の ``references`` で集め、``relations`` に ``exact`` で書き戻して、その行を返す（失敗は ``lx.LSP_FAILURES``）。

    最初の診断の待ちと references の要求は 1 問い合わせの予算（``budget``）の内。references は要求 1 つの期限ではなく予算の
    残りまで待ち、時間切れを連続時間切れに数えるのは要求 1 つの期限以上待ったときだけ（``ExactBudget.request``。BUG-0091）。
    """
    sym = pre.sym
    assert route.handle is not None
    # BUG-0058: 各段の所要（秒）。合計が閾値を超えた呼び出しだけ内訳をログに落とす（T-07 の ``_ask`` と同じ閾値ガード付き）
    spans: dict[str, float] = {}
    t = time.monotonic()
    if lx.needs_priming(route.file.language):
        # tsserver は tsconfig の include に入らないファイル（test/ など）を、basedpyright は解析の対象（include / exclude）の
        # 外のファイルを、開かれるまで探さない（BUG-0047・BUG-0132）。対象の名前を含むそのファイルを先に開いてから聞くので、
        # 結果は「LSP が見たものそのまま」のまま（exact）。
        prime = await asyncio.to_thread(_prime_set, db_path, ws, route.handle, sym, route.file.language)
        await lx.prime_documents(route.handle, prime)
        note = prime.warning(sym.name)
        if note:
            pre.warnings.append(note)
    # 起動直後の LSP は ``ready`` でも、最初の診断が来るまで解析が済んでいない（BUG-0012）。LSP が起動後にまだどの文書の診断も
    # 出していなければ、文書を開いて最初の診断を待ってから聞く（出していれば待たない。BUG-0058）。待てなかった（上限）ときも
    # exact で答えるが、解析前の部分的な答えかもしれないので目印は置かない。
    spans["prime"], t = time.monotonic() - t, time.monotonic()
    settled = True
    if lx.needs_first_diagnostic(route.file.language):
        settled = await lx.await_first_diagnostic(route.handle, sym.path, timeout_s=budget.left_s())
    spans["settle"], t = time.monotonic() - t, time.monotonic()
    sel = sym.selection or sym.range
    sites = lx.SiteIndex(ctx.services, ws)
    # 索引で対象を参照しているファイル（LSP の答えに入りそうなもの）の参照箇所の分類を、references を送ってから答えを待つ間に
    # 進める（BUG-0134。大きなテストファイルの初回の分類は 1 ファイル 150〜300 ms。分類は Python で GIL を持つので、開かせる
    # ファイルを探す prime の前に始めると prime が遅れて要求を送るのが遅れる。答えに入らなかったファイルも内容ごとに覚えて使う）
    for rel in pre.likely_files:
        sites.start(rel)
    locs = await features.references(
        route.handle,
        sym.path,
        sel.start,
        include_declaration=False,
        **budget.request(features.LSP_TIMEOUT_REFERENCES_MS),
    )
    spans["references"], t = time.monotonic() - t, time.monotonic()
    target_urn = str(sym.urn)
    # 参照元のファイルの分類は並行に（構文木はスレッド。内容が同じなら前の問い合わせの分類を使い回す。BUG-0058）
    rels = sorted({str(loc.rel) for loc in locs})
    infos = dict(zip(rels, await asyncio.gather(*(sites.of(r) for r in rels)), strict=True))
    facts = []
    rows: list[queries.ReferenceRow] = []
    for loc in locs:
        rel = str(loc.rel)
        start = (loc.range.start.line, loc.range.start.col)
        if loc.rel == sym.path and (sel.start.line, sel.start.col) <= start < (sel.end.line, sel.end.col):
            continue  # 宣言そのもの
        fact = lx.reference_fact(target_urn, rel, loc.range, infos[rel].get(start), ws.workspace_id)
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
    spans["sites"], t = time.monotonic() - t, time.monotonic()
    if facts:
        wrote = await lx.write_facts(ctx.services, facts, label=f"lsp:references:{sym.qualified_name}")
    else:
        # 0 件の答え（索引にも参照が無い。あれば上で降格している）も、解析の後の答えなら覚える（BUG-0058）。書く行は無いが、
        # 読み戻した exact 行が 0 件のときだけ目印が付く（以前の答えの exact 行が残っていれば一致せず、覚えない）。以前は
        # 覚えなかったので、参照の無いシンボル（テスト関数など）は同じ問い合わせのたびに LSP に聞き直していた
        wrote = lx.writer_of(ctx.services) is not None
    spans["write"], t = time.monotonic() - t, time.monotonic()
    # 最初の診断の前の答えは覚えない（未完了の LSP の部分的な答えを固定しないため）
    if (
        wrote
        and settled
        and await asyncio.to_thread(_confirm, db_path, ws.workspace_id, target_urn, rows, pre.token)
    ):
        spans["confirm"], t = time.monotonic() - t, time.monotonic()
        await lx.persist_marks(
            ctx.services, db_path, ws.workspace_id, [("refs", target_urn)], label="lsp:references:marks"
        )
        spans["marks"] = time.monotonic() - t
    lx.log_slow_spans("references", str(sym.qualified_name), spans, rows=len(rows), files=len(rels))
    return rows


async def handler(ctx: ToolContext, args: FindReferencesArgs) -> ToolOutput:
    await reject_unsupported_revision(ctx, args.revision)
    db_path = db_path_of(ctx)
    ws = workspace_of(ctx, args.workspace_id)
    L = ctx.services.lines  # type: ignore[attr-defined]
    await ensure_target_indexed(
        ctx, ws, args.target
    )  # 走査がまだ届いていないファイルは先取りする（BUG-0070）
    pre = await asyncio.to_thread(_pre, db_path, ws, args)

    # 経路: PaaD の exact 行（2 回目以降）→ LSP（1 回目）→ structural（降格）
    engine = EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL, warning=ACCURACY_WARNING)
    exact_rows: Sequence[queries.ReferenceRow] | None = None
    budget_note: str | None = None  # LSP が予算の内に答えなかった（warnings[] の 1 行。BUG-0091）
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
                    budget = lx.ExactBudget()
                    try:
                        exact_rows = await _exact_references(ctx, ws, db_path, route, pre, budget)
                    except lx.LSP_FAILURES as exc:
                        if not lx.is_lsp_failure(exc):
                            raise
                        _log.info("LSP references failed; answering from the index: %r", exc)
                        engine = lx.degraded_choice(route, exc)
                        if isinstance(
                            exc, TimeoutError
                        ):  # 要求の期限は予算の残り: 切れたのは予算（BUG-0091）
                            engine = replace(
                                engine, warning=lx.budget_timeout_warning(budget, exc, what="参照")
                            )
                            budget_note = lx.budget_timeout_note(budget, exc, what="references")
                if exact_rows is None:
                    engine = replace(engine, warning=(engine.warning or "") + HIERARCHY_NOTE)
    found = await asyncio.to_thread(_work, db_path, ws, args, pre, exact_rows)

    target_urn = str(found.target.urn)
    records = [_ref_record(L, r, i, target_urn, args.context_snippet) for i, r in enumerate(found.refs)]
    records += _name_matched_records(L, found, len(records), target_urn, args.context_snippet)
    files = {r.path for r in found.refs}
    tests = sum(1 for r in found.refs if r.is_test)
    totals = {"files": len(files), "sites": len(found.refs), "tests": tests}
    if found.by_name:
        totals["by_name"] = found.by_name
    if found.name_matched:
        totals["name_matched"] = len(found.name_matched)

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
        by_name = f"; by name: {found.by_name}" if found.by_name else ""
        text = f"{totals['files']} files, {totals['sites']} sites (tests: {tests}{by_name})"
        if found.name_matched:  # exact の答えの外の補足（BUG-0107）。「0 sites」だけに見せない
            nm_tests = sum(1 for r in found.name_matched if r.is_test)
            text += f" + {len(found.name_matched)} by name, not confirmed by the LSP (tests: {nm_tests})"
        head.append(("references", text))

    hierarchy_note = (
        [HIERARCHY_NOTE]
        if exact_rows is not None and args.query_type in ("implementations", "type_hierarchy", "all")
        else []
    )
    # target・implementations[] は仕様書 §6.2 の SymbolRecord そのまま（null・空のキーも出す。BUG-0066）。
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
    if found.unattributed:
        data["unattributed_calls"] = found.unattributed
    if found.unattributed_refs:
        data["unattributed_refs"] = found.unattributed_refs
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
        + ((budget_note,) if budget_note else ())
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
    reads_index=True,
)
SPEC = TOOL_SPEC

__all__ = ["FindReferencesArgs", "SPEC", "TOOL_SPEC", "handler"]
