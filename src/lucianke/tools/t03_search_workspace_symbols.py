"""T-03 ``search_workspace_symbols``（仕様書 §11.2）。名前・種別・型シグネチャでワークスペース横断にシンボルを探す。

``structural`` 版: PaaD の ``symbols``（Tree-sitter 由来。ctags 由来の ``heuristic`` も同じ表に入る）を
``index.queries`` で引くだけで、LSP は使わない（``exact`` と ``structural`` は同じ結果を返す。仕様書 T-03「精度と降格」）。

* 一致の順位は 完全一致 > 前方一致 > 部分一致、同順位は ``pagerank`` 降順（``FileInfo`` に載ったら使う。無ければ 0）、パスの辞書順。
  FTS5 の ``bm25`` 順は使わず、広めに取った候補を Python で並べ直す。
* ``limit`` はこの 1 ページに返す最大件数。カーソルで次のページを取るときは、先頭からの ``cursor_offset + limit`` 件を
  レコードとして返し、パッカーが ``cursor_offset`` から詰める（``ToolOutput.total_estimate`` に総数）。
* 0 件で ``fallback_grep: true`` のときは、索引にある Tier 4 ファイルを単語境界 grep（``syntax.heuristic.lexical_scan``）し、
  ``lex:`` セレクタの URN で返す。エンジンは ``lexical_fallback`` になり、``! DEGRADED`` 行が付く。
* ``regex`` は索引を使わない走査で、``REGEX_SCAN_MS`` を超えたら部分結果つきの ``E_TIMEOUT``。

サービスの要求: ``ctx.services`` が ``ToolServices``（``workspace()``）で、``reader_pool``（``index.connection.ReaderPool``）または
``db_path``（``paad.sqlite`` の ``Path``。T-19 / T-20 / T-06 と同じ約束）のどちらかを持つこと。どちらも無ければ ``E_WORKSPACE_NOT_CONFIGURED``。
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import DEGRADED_GREP_MS, MAX_FILE_BYTES, REGEX_PATTERN_MAX_CHARS, REGEX_SCAN_MS
from lucianke.core.model import (
    Confidence,
    EngineChoice,
    FileInfo,
    Kind,
    Position,
    Range,
    RelPath,
    SymbolRecord,
    Tier,
)
from lucianke.core.records import Record, ToolOutput
from lucianke.core.urn import LexSel, Urn
from lucianke.engine.selector import ENGINE_LEXICAL_GREP, WarningReason, warning_text
from lucianke.index import queries
from lucianke.index.connection import open_reader
from lucianke.syntax.heuristic import lexical_scan
from lucianke.tools._base import CommonArgs, Services, ToolContext, ToolSpec
from lucianke.tools._rev import reject_unsupported_revision
from lucianke.tools._services import ToolServices, services_of
from lucianke.workspace.paths import confine
from lucianke.workspace.types import Workspace

CANDIDATE_CAP = 2000
"""索引から取る候補の上限。ここから絞り込み・並べ直しをして ``limit`` 件にする。"""

_GLOB_CHARS = frozenset("*?[")
_SCORE_BASE = {0: 1.0, 1: 0.75, 2: 0.5}


class SearchWorkspaceSymbolsArgs(CommonArgs):
    query: str = Field(min_length=1, description="Name (or regex with match=regex). Empty is an error.")
    match: Literal["fuzzy", "prefix", "exact", "regex"] = Field(default="fuzzy", description="Match mode.")
    symbol_kinds: list[str] | None = Field(
        default=None, description='Lower-case kinds, e.g. ["method", "class"]. Default: all kinds.'
    )
    type_signature_filter: str | None = Field(
        default=None,
        description='"A -> B" (parameters contain A and the return type contains B) or "A" (either contains A). '
        "Case-insensitive substring match.",
    )
    scope_paths: list[str] | None = Field(
        default=None,
        description="Workspace-relative files, directories or globs to search in. Default: everywhere.",
    )
    include_tests: bool = Field(default=True, description="Include symbols in test files.")
    limit: int = Field(default=30, ge=1, le=200, description="Maximum symbols in this page (1-200).")
    fallback_grep: bool = Field(
        default=True, description="With 0 hits, grep Tier 4 files by word boundary (degraded, lex: URNs)."
    )


# --- サービス（T-04 も使う） -----------------------------------------------------------------


@contextmanager
def index_reader(svc: Services) -> Iterator[sqlite3.Connection]:
    """読み取り接続を借りる（``asyncio.to_thread`` の中で使う）。

    ``svc.reader_pool``（``ReaderPool``）があればそれを使い、無ければ ``svc.db_path``（T-19 / T-20 / T-06 と同じ約束）から
    1 本開いて閉じる。どちらも無い・DB ファイルが開けないときは ``E_WORKSPACE_NOT_CONFIGURED``。
    """
    pool = getattr(svc, "reader_pool", None)
    conn: sqlite3.Connection
    try:
        if pool is not None:
            conn = pool.acquire()
        else:
            raw = getattr(svc, "db_path", None)
            if raw is None:
                raise LuciankeError(
                    ErrorCode.E_WORKSPACE_NOT_CONFIGURED,
                    "the project index (PaaD) is not available: the server has no reader_pool / db_path",
                    details={"reason": "index_not_available"},
                )
            conn = open_reader(raw)
    except sqlite3.OperationalError as exc:
        raise LuciankeError(
            ErrorCode.E_WORKSPACE_NOT_CONFIGURED,
            f"the project index (PaaD) cannot be opened: {exc}",
            details={"reason": "index_not_available"},
        ) from None
    try:
        yield conn
    finally:
        if pool is not None:
            pool.release(conn)
        else:
            conn.close()


# --- 引数の検査 -----------------------------------------------------------------------------


def _invalid(param: str, message: str) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, f"{param}: {message}", details={"param": param})


def _parse_kinds(values: Sequence[str] | None) -> list[Kind] | None:
    if not values:
        return None
    out: list[Kind] = []
    for v in values:
        try:
            out.append(Kind.parse(v))
        except ValueError:
            known = ", ".join(k.value.casefold() for k in Kind)
            raise _invalid("symbol_kinds", f"unknown kind {v!r} (known: {known})") from None
    return out


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
        if _GLOB_CHARS & set(text):
            if text.startswith("/") or ".." in text.split("/"):
                raise _invalid("scope_paths", f"glob must be workspace-relative: {raw!r}")
            globs.append(text.removeprefix("./"))
        else:
            prefixes.append(str(confine(ws, raw, param="scope_paths")).rstrip("/") or ".")
    return _Scope(tuple(prefixes), tuple(globs))


@dataclass(frozen=True, slots=True)
class _SigFilter:
    params: str  # 空なら条件なし
    ret: str
    either: bool  # "A"（-> なし）

    def matches(self, sym: SymbolRecord) -> bool:
        sig = (sym.signature or "").casefold()
        left, arrow, right = sig.rpartition(" -> ")
        if not arrow:
            left, right = sig, ""
        ret_text = (sym.return_type or "").casefold() or right
        if self.either:
            return self.params in sig or self.params in ret_text
        return (not self.params or self.params in left) and (not self.ret or self.ret in ret_text)


def _parse_sig_filter(text: str | None) -> _SigFilter | None:
    if text is None or not text.strip():
        return None
    if "->" in text:
        left, _, right = text.partition("->")
        return _SigFilter(left.strip().casefold(), right.strip().casefold(), either=False)
    return _SigFilter(text.strip().casefold(), "", either=True)


# --- 並べ替え -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Hit:
    sym: SymbolRecord
    tier: int  # 0 完全一致 / 1 前方一致 / 2 部分一致
    score: float
    pagerank: float
    file: FileInfo | None


def _match_tier(sym: SymbolRecord, query: str, rx: re.Pattern[str] | None) -> int:
    if rx is not None:
        for text in (sym.name, sym.qualified_name):
            m = rx.fullmatch(text)
            if m:
                return 0
        for text in (sym.name, sym.qualified_name):
            m = rx.search(text)
            if m and m.start() == 0:
                return 1
        return 2
    if sym.name == query or sym.qualified_name == query:
        return 0
    q = query.casefold()
    # 前方一致は名前で見る。限定名で探しているとき（query に "." がある）だけ限定名の前方も前方一致にする
    if sym.name.casefold().startswith(q) or ("." in q and sym.qualified_name.casefold().startswith(q)):
        return 1
    return 2


def _score(tier: int, sym: SymbolRecord, query: str) -> float:
    cover = min(1.0, len(query) / max(len(sym.name), 1))
    return round(_SCORE_BASE[tier] - (0.2 * (1.0 - cover) if tier else 0.0), 3)


def _pagerank(info: FileInfo | None) -> float:
    value = getattr(info, "pagerank", None)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _sort_key(h: _Hit) -> tuple[Any, ...]:
    return (
        h.tier,
        -h.pagerank,
        str(h.sym.path),
        h.sym.range.start.line,
        h.sym.qualified_name,
        str(h.sym.urn),
    )


# --- 索引の検索 -----------------------------------------------------------------------------


@dataclass(slots=True)
class _SearchResult:
    hits: list[_Hit]
    timed_out: bool = False
    scanned_files: int = 0
    total_files: int = 0


def _compile_regex(query: str) -> re.Pattern[str]:
    if len(query) > REGEX_PATTERN_MAX_CHARS:
        raise _invalid("query", f"regex is longer than {REGEX_PATTERN_MAX_CHARS} characters")
    try:
        return re.compile(query)
    except (re.error, RecursionError, OverflowError) as exc:
        raise _invalid("query", f"invalid regular expression: {exc}") from None


def _regex_symbols(
    conn: sqlite3.Connection,
    ws_id: str,
    rx: re.Pattern[str],
    kinds: Sequence[Kind] | None,
    scope: _Scope | None,
    include_tests: bool,
    result: _SearchResult,
) -> list[SymbolRecord]:
    """索引を使わない走査（手順 1）。``REGEX_SCAN_MS`` を超えたら打ち切って ``result.timed_out``。"""
    deadline = time.monotonic() + REGEX_SCAN_MS / 1000
    files = queries.files_in_scope(conn, ws_id, (), include_tests=include_tests)
    result.total_files = len(files)
    wanted = set(kinds) if kinds else None
    found: list[SymbolRecord] = []
    for f in files:
        if scope is not None and not scope.contains(f.path):
            continue
        if time.monotonic() > deadline:
            result.timed_out = True
            break
        result.scanned_files += 1
        for s in queries.symbols_in_file(conn, ws_id, f.path):
            if wanted is not None and s.kind not in wanted:
                continue
            if rx.search(s.name) or rx.search(s.qualified_name):
                found.append(s)
    return found


def _search_index(
    conn: sqlite3.Connection,
    ws_id: str,
    args: SearchWorkspaceSymbolsArgs,
    query: str,
    kinds: Sequence[Kind] | None,
    scope: _Scope | None,
    sig_filter: _SigFilter | None,
) -> _SearchResult:
    result = _SearchResult(hits=[])
    rx: re.Pattern[str] | None = None
    if args.match == "regex":
        rx = _compile_regex(query)
        syms = _regex_symbols(conn, ws_id, rx, kinds, scope, args.include_tests, result)
    else:
        syms = queries.search_symbols_fts(conn, ws_id, query, args.match, kinds, limit=CANDIDATE_CAP)
        if (
            args.match == "exact"
        ):  # 仕様書 T-03 手順 1・事後条件 1: exact は name の等価比較（限定名では当てない）
            syms = [s for s in syms if s.name == query]
    infos: dict[str, FileInfo | None] = {}

    def info_of(path: str) -> FileInfo | None:
        if path not in infos:
            infos[path] = queries.file_by_path(conn, ws_id, path)
        return infos[path]

    for s in syms:
        path = str(s.path)
        if scope is not None and not scope.contains(path):
            continue
        info = info_of(path)
        if not args.include_tests and info is not None and info.is_test:
            continue
        if sig_filter is not None and not sig_filter.matches(s):
            continue
        tier = _match_tier(s, query, rx)
        result.hits.append(_Hit(s, tier, _score(tier, s, query), _pagerank(info), info))
    result.hits.sort(key=_sort_key)
    return result


# --- Tier 4 の単語境界 grep（手順 4） -----------------------------------------------------------


@dataclass(slots=True)
class _Fallback:
    hits: list[_Hit]
    attempted: bool
    languages: list[str]


def _lexical_fallback(
    conn: sqlite3.Connection, ws: Workspace, query: str, scope: _Scope | None, include_tests: bool
) -> _Fallback:
    files = [
        f
        for f in queries.files_in_scope(conn, ws.workspace_id, (), include_tests=include_tests)
        if f.tier == Tier.T4 and (scope is None or scope.contains(f.path))
    ]
    out = _Fallback([], attempted=bool(files), languages=[])
    deadline = time.monotonic() + DEGRADED_GREP_MS / 1000
    for f in files:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        abs_path = ws.root / f.path
        try:
            if abs_path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = abs_path.read_bytes()
            scan = lexical_scan(data, query, context=0, budget_ms=remaining_ms)
        except (OSError, ValueError):
            continue
        for hit in scan.hits:
            line1 = hit.selector_line
            sym = SymbolRecord(
                urn=Urn(ws.workspace_id, f.path, LexSel(query, line1)),
                path=RelPath(f.path),
                name=query,
                qualified_name=query,
                kind=Kind.SECTION,
                range=Range.from_line_span_external(line1, line1),
                selection=Range(Position(hit.line, hit.col), Position(hit.line, hit.end_col)),
                signature=hit.line_text.strip() or None,
                confidence=Confidence.LEXICAL_FALLBACK,
            )
            tier = 0 if hit.is_definition else 1
            out.hits.append(_Hit(sym, tier, 0.4 if hit.is_definition else 0.2, 0.0, f))
        if scan.hits and f.language not in out.languages:
            out.languages.append(f.language)
        if scan.timed_out:
            break
    return out


# --- レコード -------------------------------------------------------------------------------


def _record(svc: ToolServices, hit: _Hit, priority: int) -> Record:
    L = svc.lines
    s = hit.sym
    start, end = s.range.line_span_external()
    rel = str(s.path)
    lines = [
        L.symbol_line(s.kind, f"{rel}#{s.qualified_name}", sig=s.signature, start_line=start, end_line=end),
        L.attr_line("urn", str(s.urn), indent=0),
    ]
    if (
        s.confidence < Confidence.STRUCTURAL
    ):  # heuristic / lexical_fallback の行は水準を明示する（仕様書 T-03）
        lines.append(L.attr_line("conf", s.confidence.value, indent=0))
    data = s.to_json()
    data["score"] = hit.score
    return Record(
        kind="symbol",
        key=str(s.urn),
        priority=priority,
        lines=tuple(lines),
        data=data,
        confidence=s.confidence,
        group=rel,
    )


def _languages(hits: Sequence[_Hit]) -> str:
    seen = list(dict.fromkeys(h.file.language for h in hits if h.file is not None))
    return ",".join(seen) if seen else "n/a"


# --- ハンドラ -------------------------------------------------------------------------------


def _build_output(
    svc: ToolServices,
    args: SearchWorkspaceSymbolsArgs,
    query: str,
    hits: list[_Hit],
    *,
    offset: int,
    engine: EngineChoice,
    fallback_used: bool,
    languages: str | None = None,
) -> ToolOutput:
    total = len(hits)
    window = hits[: offset + args.limit]  # パッカーが offset から詰める。1 ページは limit 件まで
    records = [_record(svc, h, i) for i, h in enumerate(window)]
    tier = max((h.file.tier for h in window if h.file is not None), default=Tier.T1)
    return ToolOutput(
        records=records,
        max_confidence=Confidence.STRUCTURAL,
        engine=engine,
        page_unit="symbol",
        head_kv=(("query", f'"{query}" match={args.match} hits={total}'),),
        data_static={
            "query": args.query,
            "match": args.match,
            "hits_total": total,
            "fallback_used": fallback_used,
        },
        tier=tier,
        detected_language=languages if languages is not None else _languages(window),
        total_estimate=total,
    )


async def handler(ctx: ToolContext, args: SearchWorkspaceSymbolsArgs) -> ToolOutput:
    await reject_unsupported_revision(ctx, args.revision)
    svc = services_of(ctx.services)
    ws = svc.workspace(args.workspace_id)
    query = args.query.strip()
    if not query:
        raise _invalid("query", "must not be empty")
    kinds = _parse_kinds(args.symbol_kinds)
    scope = _parse_scope(ws, args.scope_paths)
    sig_filter = _parse_sig_filter(args.type_signature_filter)
    if args.match == "regex":
        _compile_regex(query)  # 不正な正規表現は索引に触る前に E_INVALID_ARGUMENT

    def work() -> tuple[_SearchResult, _Fallback | None]:
        with index_reader(svc) as conn:
            res = _search_index(conn, ws.workspace_id, args, query, kinds, scope, sig_filter)
            fb: _Fallback | None = None
            if not res.hits and args.fallback_grep and not res.timed_out:
                fb = _lexical_fallback(conn, ws, query, scope, args.include_tests)
            return res, fb

    res, fb = await asyncio.to_thread(work)
    index_engine = EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL)
    if fb is not None and fb.attempted:
        engine = EngineChoice(
            engine=ENGINE_LEXICAL_GREP,
            confidence=Confidence.LEXICAL_FALLBACK,
            warning=warning_text(WarningReason.NO_GRAMMAR_LEXICAL),
        )
        fb.hits.sort(key=_sort_key)
        return _build_output(
            svc, args, query, fb.hits, offset=ctx.cursor_offset, engine=engine, fallback_used=True,
            languages=",".join(fb.languages) if fb.languages else "unknown",
        )  # fmt: skip
    out = _build_output(
        svc, args, query, res.hits, offset=ctx.cursor_offset, engine=index_engine, fallback_used=False
    )
    if res.timed_out:
        raise LuciankeError(
            ErrorCode.E_TIMEOUT,
            f"regex scan exceeded {REGEX_SCAN_MS} ms after {res.scanned_files} of {res.total_files} files; "
            "narrow the pattern or use scope_paths",
            details={
                "partial": len(res.hits),
                "scanned_files": res.scanned_files,
                "total_files": res.total_files,
            },
            partial=out,
        )
    return out


TOOL_SPEC = ToolSpec(
    name="search_workspace_symbols",
    tid="T-03",
    args_model=SearchWorkspaceSymbolsArgs,
    handler=handler,
    default_max_tokens=1500,
    description=(
        "Find symbols across the workspace by name (match: fuzzy | prefix | exact | regex), kind, and type signature "
        '("A -> B": parameters contain A, return type contains B). Returns URNs with signatures and line ranges, '
        "ranked exact > prefix > partial. Use it before resolve_symbols when you only know part of a name. "
        "With 0 hits it greps non-code (Tier 4) files by word boundary and marks the result degraded."
    ),
    max_confidence=Confidence.STRUCTURAL,
    milestone="M1",
    page_unit="symbol",
    records_key="symbols",
)
SPEC = TOOL_SPEC  # tools/__init__.register_tools の命名（SPEC）に合わせる別名
