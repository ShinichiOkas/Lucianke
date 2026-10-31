"""T-01 ``get_repo_map``（仕様書 §11.2）。ディレクトリ構造と、参照グラフ上の重要度が高い公開シンボルを予算内に詰めた俯瞰マップ。

``structural`` 版: PaaD（``files`` / ``symbols`` / ``_symbol_rank``）だけから答える。LSP は使わない（仕様書 T-01「精度と降格」:
``exact`` と ``structural`` の差は無い）。``revision`` は ``WORKING_TREE`` のみ。

* 順位付け（処理規約 2）: ``pagerank`` はファイルのスコア = 公開シンボルの PageRank の上位 ``max_symbols_per_file`` 件の和
  （``index.rank``。シンボルの値は差分索引の後に ``_symbol_rank`` へ書かれる）。``fan_in`` は同じ件数の fan_in の和。
  ``alpha`` はパス順。``_symbol_rank`` が空（索引前・参照が無い）なら ``alpha`` に降格し ``! ranking=alpha (graph not ready)``。
  同点はパスの辞書順。表示する ``rank`` は先頭のファイルを 1.00 とした相対値（``alpha`` は 0.00）。
* 詰める順序（処理規約 3）: 先頭のファイルブロックの前に概観（``tools._overview``。トップレベルのディレクトリ別のファイル数の行、
  コードのパッケージの根と 2 段目のサブパッケージのファイル数、入口のモジュール。``OVERVIEW_MAX_TOKENS`` まで。BUG-0120）、
  次にスコア降順のファイルブロック。各ブロックは公開シンボルをスコア順に上位 N 件、行の順に入れ子で出す。
  予算が尽きたら残りは ``# summary: N more files (src: 30, …)``（ディレクトリ別）と ``# more: cursor=…``。
  1 レコードは 1 ファイルブロック（途中で切らない。入らないときは縮退版: シグネチャ → シンボルの数 の順に減らす）。
* 公開シンボル: 修飾子に ``priv`` / ``prot`` を持たず、祖先（親・その親…）にも ``priv`` / ``prot`` のシンボルが無く（``_DbLock`` の
  ``hold`` や export されていない TS のクラスのメンバーは、自分が ``pub`` でも出さない。BUG-0124）、種別が ``rank.MINOR_KINDS``（``Field`` / ``Property`` / ``Column`` / ``Cell`` / ``Key`` / ``Tag``）でないもの。
  修飾子が空（ctags 由来など可視性の分からないもの）は公開として扱う（``conf: heuristic`` を付ける）。
* ``lexical_fallback`` のファイル（秘密ファイル・バイナリ・本文を解析しないもの）はファイル名と行数だけ。内容は出さない。
* ``include_tests=false``（既定）はテストファイルを順位にも要約にも入れない（``files.is_test``）。

``ctx.services`` に求めるもの（duck typing）:

* ``workspace(workspace_id)``、``lines``（compact の行の組み立て関数のモジュール）: 必須。
* ``reader_pool``（``index.connection.ReaderPool``）または ``db_path``（``paad.sqlite`` の ``Path``）のどちらか。
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice, RelPath, SymbolRecord, Tier
from lucianke.core.records import Record, ToolOutput
from lucianke.core.tokens import TokenEstimator
from lucianke.index import queries, rank
from lucianke.tools import _overview
from lucianke.tools._base import WORKING_TREE, CommonArgs, ToolContext, ToolSpec
from lucianke.tools._nav import workspace_of
from lucianke.tools.t03_search_workspace_symbols import index_reader
from lucianke.workspace.paths import confine, is_excluded
from lucianke.workspace.types import Workspace

GRAPH_NOT_READY = "ranking=alpha (graph not ready)"
TREE_LINE_MAX_DIRS = 12
TREE_JSON_TOKENS_PER_DIR = 40
"""json の ``tree[]`` に載せるディレクトリ数の上限は ``max_tokens`` をこれで割った数（予約分が予算を食い尽くさないため。ファイル数の多い順）。"""
JSON_OVERVIEW_SHARE = 10
"""json の ``packages[]`` / ``entry_modules[]`` は、compact に直して ``max_tokens`` のこの分の 1（``OVERVIEW_MAX_TOKENS`` まで）に収まる分だけ
（``tree[]`` と同じく、静的な欄が小さい予算を食い尽くさないため。compact の概観は ``max_tokens`` に依らない。BUG-0120）。"""
SUMMARY_MAX_GROUPS = 8
EXTRA_RECORDS = 3
"""予算を超えたあとにも作っておくファイルブロックの数（パッカーの ``# summary:`` が「残り」を見られるように）。"""
SCOPE_GLOB_CHARS = frozenset("*?[")

_EST = TokenEstimator()
Ranking = Literal["pagerank", "fan_in", "alpha"]


class GetRepoMapArgs(CommonArgs):
    scope_paths: list[str] | None = Field(
        default=None,
        description="Directories or globs.",
    )
    include_signatures: bool = Field(default=True)
    max_symbols_per_file: int = Field(default=5, ge=1, le=50)
    ranking: Ranking = Field(default="pagerank", description="alpha = path order.")
    include_tests: bool = Field(default=False)


# --- 範囲 -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Scope:
    prefixes: tuple[str, ...]  # ディレクトリ・ファイル（"." は全体）
    globs: tuple[str, ...]

    def contains(self, path: str) -> bool:
        for p in self.prefixes:
            if p == "." or path == p or path.startswith(p + "/"):
                return True
        return any(fnmatch.fnmatchcase(path, g) for g in self.globs)


def _parse_scope(ws: Workspace, scope_paths: Sequence[str] | None) -> _Scope | None:
    if not scope_paths:
        return None
    prefixes: list[str] = []
    globs: list[str] = []
    for raw in scope_paths:
        text = raw.replace("\\", "/")
        if SCOPE_GLOB_CHARS & set(text):
            if text.startswith("/") or ".." in text.split("/"):
                raise LuciankeError(
                    ErrorCode.E_INVALID_ARGUMENT,
                    f"scope_paths: glob must be workspace-relative: {raw!r}",
                    details={"param": "scope_paths"},
                )
            globs.append(text.removeprefix("./"))
        else:
            prefixes.append(str(confine(ws, raw, param="scope_paths")).rstrip("/") or ".")
    return _Scope(tuple(prefixes), tuple(globs))


def _top_dir(path: str) -> str:
    head, sep, _ = path.partition("/")
    return head + "/" if sep else "./"


# --- 順位 -----------------------------------------------------------------------------------


def _metric(f: rank.RepoMapFile, ranking: Ranking) -> float:
    if ranking == "pagerank":
        return f.score
    if ranking == "fan_in":
        return float(f.fan_in_top)
    return 0.0


def _order(files: list[rank.RepoMapFile], ranking: Ranking) -> list[rank.RepoMapFile]:
    if ranking == "alpha":
        return sorted(files, key=lambda f: f.path)
    if ranking == "fan_in":
        return sorted(files, key=lambda f: (-f.fan_in_top, -f.score, f.path))
    return sorted(files, key=lambda f: (-f.score, -f.fan_in, f.path))


# --- 1 ファイルのブロック -------------------------------------------------------------------


def _eligible(s: SymbolRecord) -> bool:
    mods = set(s.modifiers)
    return "priv" not in mods and "prot" not in mods and s.kind.value not in rank.MINOR_KINDS


def _hidden_by_ancestor(
    symbols: list[SymbolRecord], parents: dict[str, str | None], urns: Sequence[str] | None = None
) -> set[str]:
    """祖先に ``priv`` / ``prot`` のシンボルがある（モジュールの外から使う API ではない）シンボルの urn（BUG-0124）。

    ``_DbLock`` の ``hold``・export されていない TS のクラスのメンバーのように、自分の修飾子は ``pub`` でも親が外に出ていない。
    ``urns`` は ``symbols`` の ``str(urn)``（呼ぶ側が作ってあれば渡す）。"""
    keys = urns if urns is not None else [str(s.urn) for s in symbols]
    closed = {u for s, u in zip(symbols, keys, strict=True) if {"priv", "prot"} & set(s.modifiers)}
    hidden: set[str] = set()
    if not closed:
        return hidden
    for u in keys:
        seen: set[str] = set()
        parent = parents.get(u)
        while parent is not None and parent not in seen:
            if parent in closed:
                hidden.add(u)
                break
            seen.add(parent)
            parent = parents.get(parent)
    return hidden


@dataclass(frozen=True, slots=True)
class _Shown:
    sym: SymbolRecord
    indent: int
    name: str


def _ranked_pool(
    symbols: list[SymbolRecord],
    parents: dict[str, str | None],
    ranks: dict[str, tuple[float, int]],
    ranking: Ranking,
) -> list[tuple[SymbolRecord, str]]:
    """選ぶ候補（公開で、隠れた祖先の無いシンボルと ``str(urn)``）を指標の順に並べたもの。

    ファイルごとに 1 度だけ作り、``_select_top`` が上位 ``limit`` 件を取る（予算に合わせて件数を減らす ``reselect`` は 1 ファイルで
    何度も呼ばれる。以前は呼ぶたびに隠れた祖先・候補・並べ替えを作り直し、``str(urn)`` を 1 回の T-01 で 4,000 回以上作っていた。BUG-0128）。
    """
    urns = [str(s.urn) for s in symbols]
    hidden = _hidden_by_ancestor(symbols, parents, urns)
    pool = [(s, u) for s, u in zip(symbols, urns, strict=True) if _eligible(s) and u not in hidden]

    def key(item: tuple[SymbolRecord, str]) -> tuple[float, int, str]:
        s, u = item
        score, fan_in = ranks.get(u, (0.0, 0))
        primary = {"pagerank": -score, "fan_in": -float(fan_in), "alpha": 0.0}[ranking]
        return (primary, s.range.start.line, u)

    pool.sort(key=key)
    return pool


def _select_top(pool: list[tuple[SymbolRecord, str]], parents: dict[str, str | None], limit: int) -> list[_Shown]:
    """``_ranked_pool`` の上位 ``limit`` 件を、行の順・入れ子（選んだ祖先の下）に並べる。"""
    chosen = sorted(pool[:limit], key=lambda item: (item[0].range.start.line, -item[0].range.end.line, item[1]))
    by_urn = {u: s for s, u in chosen}
    shown: list[_Shown] = []
    depth: dict[str, int] = {}
    for s, urn in chosen:
        parent = parents.get(urn)
        anchor: str | None = None
        while parent is not None:
            if parent in by_urn:
                anchor = parent
                break
            parent = parents.get(parent)
        if anchor is None:
            depth[urn] = 0
            shown.append(_Shown(s, 0, s.qualified_name))
            continue
        depth[urn] = depth[anchor] + 1
        prefix = by_urn[anchor].qualified_name + "."
        name = s.qualified_name[len(prefix) :] if s.qualified_name.startswith(prefix) else s.qualified_name
        shown.append(_Shown(s, depth[urn], name))
    return shown


def _symbol_json(s: SymbolRecord, with_signature: bool) -> dict[str, Any]:
    full = s.to_json()
    out = {
        k: full[k]
        for k in ("urn", "name", "qualified_name", "kind", "modifiers", "range", "confidence_level")
    }
    if with_signature and s.signature is not None:
        out["signature"] = s.signature
    return out


def _block(
    L: Any,
    f: rank.RepoMapFile,
    rank_text: float,
    shown: list[_Shown],
    *,
    with_signatures: bool,
    overview: tuple[str, ...],
) -> tuple[str, ...]:
    lines: list[str] = [L.note(text) for text in overview]
    lines.append(L.file_block_header(f.path, lines=f.line_count))
    lines.append(L.note(f"rank: {rank_text:.2f} fan_in: {f.fan_in}"))
    for item in shown:
        s = item.sym
        start, end = s.range.line_span_external()
        mods = [m for m in s.modifiers if m in L.MODIFIERS]
        lines.append(
            L.symbol_line(
                s.kind,
                item.name,
                indent=item.indent,
                modifiers=mods,
                sig=s.signature if with_signatures else None,
                start_line=start,
                end_line=end,
            )
        )
        if s.confidence < Confidence.STRUCTURAL:
            lines.append(L.attr_line("conf", s.confidence.value, indent=item.indent))
    return tuple(lines)


def _confidence(f: rank.RepoMapFile, shown: list[_Shown]) -> Confidence:
    if shown:
        return min(item.sym.confidence for item in shown)
    return Confidence(f.confidence)


def _make_record(
    L: Any,
    f: rank.RepoMapFile,
    index: int,
    rank_value: float,
    shown: list[_Shown],
    *,
    with_signatures: bool,
    overview: tuple[str, ...] = (),
    reselect: Callable[[int], list[_Shown]] | None = None,
) -> Record:
    lines = _block(L, f, rank_value, shown, with_signatures=with_signatures, overview=overview)
    data = {
        "path": f.path,
        "rank": round(rank_value, 4),
        "fan_in": f.fan_in,
        "line_count": f.line_count,
        "language": f.language,
        "tier": f.tier,
        "symbols": [_symbol_json(item.sym, with_signatures) for item in shown],
    }

    def shrink(budget: int) -> Record | None:
        """残り ``budget`` トークンに収まる縮退版: シグネチャを外す → 重要でない順にシンボルを減らす → ヘッダだけ。

        先頭のレコード（概観つき）は、まず概観の行を後ろ（サブパッケージ → 入口）から外してブロックを丸ごと残し、次に
        トップレベルの行とパッケージの行を残したままブロックを縮め、最後にその 2 行も外す。段の順は固定なので、予算が大きいほど
        浅い段で返る（BUG-0067）。"""
        candidates: list[tuple[list[_Shown], bool]] = []
        if with_signatures and shown:
            candidates.append((shown, False))
        if reselect is not None:
            candidates += [(reselect(k), False) for k in range(len(shown) - 1, -1, -1)]
        keep = min(len(overview), _overview.CORE_LINES)
        ladder: list[tuple[tuple[str, ...], list[_Shown], bool]] = [
            (overview[:n], shown, with_signatures) for n in range(len(overview) - 1, keep - 1, -1)
        ]
        for n in range(keep, -1, -1):
            ladder += [(overview[:n], subset, sigs) for subset, sigs in candidates]
            if n == 0:
                ladder.append(((), [], False))
        for note, subset, sigs in ladder:
            text = "\n".join(_block(L, f, rank_value, subset, with_signatures=sigs, overview=note))
            if _EST.count(text) <= budget:
                return _make_record(
                    L, f, index, rank_value, subset, with_signatures=sigs, overview=note, reselect=None
                )
        return None

    return Record(
        kind="file-block",
        key=f.path,
        priority=index,
        lines=lines,
        data=data,
        confidence=_confidence(f, shown),
        group=_top_dir(f.path),
        shrink=shrink if shown or with_signatures or overview else None,
    )


# --- 要約 -----------------------------------------------------------------------------------


def _tree_note(files: Sequence[rank.RepoMapFile]) -> str | None:
    counts: dict[str, int] = {}
    for f in files:
        d = _top_dir(f.path)
        counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    parts = [f"{d} ({n} {'file' if n == 1 else 'files'})" for d, n in items[:TREE_LINE_MAX_DIRS]]
    if len(items) > TREE_LINE_MAX_DIRS:
        parts.append(f"+{len(items) - TREE_LINE_MAX_DIRS} more dirs")
    return ", ".join(parts)


def _tree_json(files: Sequence[rank.RepoMapFile]) -> list[dict[str, Any]]:
    agg: dict[str, list[int]] = {}
    for f in files:
        entry = agg.setdefault(_top_dir(f.path), [0, 0])
        entry[0] += 1
        entry[1] += f.line_count
    return [
        {"dir": d, "file_count": n, "line_count": lc}
        for d, (n, lc) in sorted(agg.items(), key=lambda kv: (-kv[1][0], kv[0]))
    ]


def _summarizer(ordered: Sequence[rank.RepoMapFile], total_records: int) -> Callable[[Sequence[Record]], str]:
    """``# summary:`` の本文。パッカーが渡す ``rest`` は ``records[next:]`` なので、先頭の位置は ``total - len(rest)``。"""

    def summarize(rest: Sequence[Record]) -> str:
        start = max(0, total_records - len(rest))
        remaining = ordered[start:]
        counts: dict[str, int] = {}
        for f in remaining:
            d = _top_dir(f.path).rstrip("/")
            counts[d] = counts.get(d, 0) + 1
        items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        parts = [f"{d}: {n}" for d, n in items[:SUMMARY_MAX_GROUPS]]
        others = sum(n for _, n in items[SUMMARY_MAX_GROUPS:])
        if others:
            parts.append(f"others: {others}")
        return f"{len(remaining)} more {'file' if len(remaining) == 1 else 'files'} ({', '.join(parts)})"

    return summarize


# --- ハンドラ -------------------------------------------------------------------------------


def _reject_revision(revision: str) -> None:
    if revision != WORKING_TREE:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"revision {revision!r} is not supported yet (WORKING_TREE only)",
            details={"reason": "revision_not_supported_until_M2", "revision": revision},
        )


@dataclass(slots=True)
class _Built:
    records: list[Record]
    ordered: list[rank.RepoMapFile]
    tree: list[dict[str, Any]]
    ranking_used: Ranking
    warnings: list[str]
    tier: Tier
    languages: str
    overview: dict[str, Any]


def _build(
    ctx: ToolContext, svc: Any, ws: Workspace, args: GetRepoMapArgs, scope: _Scope | None, offset: int
) -> _Built:
    L = svc.lines
    ranking: Ranking = args.ranking
    warnings: list[str] = []
    with index_reader(svc) as conn:
        files = rank.repo_map_files(
            conn, ws.workspace_id, per_file=args.max_symbols_per_file, include_tests=args.include_tests
        )
        files = [
            f
            for f in files
            if (scope is None or scope.contains(f.path)) and not is_excluded(ws, RelPath(f.path))
        ]
        ready = rank.ranked_symbol_count(conn, ws.workspace_id) > 0
        if ranking != "alpha" and not ready:
            ranking = "alpha"
            warnings.append(GRAPH_NOT_READY)
        ordered = _order(files, ranking)
        top = max((_metric(f, ranking) for f in ordered), default=0.0)

        def rank_of(f: rank.RepoMapFile) -> float:
            return _metric(f, ranking) / top if top > 0 else 0.0

        entries = _overview.entry_modules(ordered, ws.root)  # pyproject.toml を読むのは 1 度だけ（BUG-0128）
        overview = _overview.build(ordered, ws.root, _tree_note(ordered), _EST.count, entries=entries)
        budget = ctx.call.max_tokens
        json_overview = _overview.build(
            ordered, ws.root, None, _EST.count,
            max_tokens=min(_overview.OVERVIEW_MAX_TOKENS, budget // JSON_OVERVIEW_SHARE), entries=entries,
        )  # fmt: skip
        records: list[Record] = []
        spent = 0
        extra = 0
        tier = Tier.T1
        languages: list[str] = []
        for i, f in enumerate(ordered):
            if i < offset:  # 前のページ分: 位置合わせだけ（パッカーが読み飛ばす）
                records.append(
                    Record(
                        kind="file-block",
                        key=f.path,
                        priority=i,
                        lines=(),
                        data={},
                        confidence=Confidence.STRUCTURAL,
                    )
                )
                continue
            if spent > budget:
                extra += 1
                if extra > EXTRA_RECORDS:
                    break
            shown: list[_Shown] = []
            reselect: Callable[[int], list[_Shown]] | None = None
            if Confidence(f.confidence) != Confidence.LEXICAL_FALLBACK:
                symbols = queries.symbols_in_file(conn, ws.workspace_id, f.path)
                if symbols:
                    parents = queries.symbol_parents(conn, ws.workspace_id, f.path)
                    ranks = rank.file_symbol_ranks(conn, ws.workspace_id, f.path)
                    pool = _ranked_pool(symbols, parents, ranks, ranking)

                    def reselect(k: int, pool=pool, parents=parents) -> list[_Shown]:
                        return _select_top(pool, parents, k)

                    shown = reselect(args.max_symbols_per_file)
            rec = _make_record(
                L,
                f,
                i,
                rank_of(f),
                shown,
                with_signatures=args.include_signatures,
                overview=overview.lines if i == 0 else (),
                reselect=reselect,
            )
            records.append(rec)
            spent += _EST.count("\n".join(rec.lines) + "\n")
            tier = max(tier, Tier(f.tier))
            if f.language not in languages:
                languages.append(f.language)
    return _Built(
        records,
        ordered,
        _tree_json(ordered),
        ranking,
        warnings,
        tier,
        ",".join(languages) if languages else "n/a",
        json_overview.data(),
    )


async def handler(ctx: ToolContext, args: GetRepoMapArgs) -> ToolOutput:
    _reject_revision(args.revision)
    svc = ctx.services
    ws = workspace_of(ctx, args.workspace_id)
    scope = _parse_scope(ws, args.scope_paths)
    built = await asyncio.to_thread(_build, ctx, svc, ws, args, scope, ctx.cursor_offset)
    return ToolOutput(
        records=built.records,
        max_confidence=Confidence.STRUCTURAL,
        engine=EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL),
        page_unit="file",
        data_static={
            "ranking_used": built.ranking_used,
            "tree": built.tree[: max(1, ctx.call.max_tokens // TREE_JSON_TOKENS_PER_DIR)],
            **built.overview,
        },
        summarize=_summarizer(built.ordered, len(built.records)),
        tier=built.tier,
        detected_language=built.languages,
        total_estimate=len(built.ordered),
        warnings=tuple(built.warnings),
    )


SPEC = ToolSpec(
    name="get_repo_map",
    tid="T-01",
    args_model=GetRepoMapArgs,
    handler=handler,
    default_max_tokens=1500,
    description=(
        "Map an unfamiliar repository within a token budget: directory counts, code packages with their sub-packages and entry modules, then files ranked by importance (PageRank), each with its top public symbols and signatures. First call in a new codebase. Test files are hidden unless include_tests=true."
    ),
    max_confidence=Confidence.STRUCTURAL,
    milestone="M1",
    page_unit="file",
    records_key="files",
    reads_index=True,
)
