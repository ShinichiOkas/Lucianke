"""T-20 ``query_project_db``（仕様書 §11.8、設計書 §7.6.4）。PaaD に対する読み取り専用 SQL を実行して表・JSON・グラフで返す。

実行は ``lucianke.index.public.run_query``（4 段の防御）に任せ、このモジュールは引数の検査、grep の指定の組み立て、
結果の ``ToolOutput`` への写しだけを持つ。1 レコード = 1 行。``Record.data`` は列名をキーにした辞書（``mcp`` の
パッカー・json レンダラ・グラフのレンダラが ``Mapping`` を前提にするため。列の順は ``data_static["columns"]``）。

``ctx.services`` に求めるもの（duck typing。``tools`` は ``mcp`` を import できない）:

* ``db_path``: ``paad.sqlite`` の ``Path``。無い・ファイルが無い → ``E_WORKSPACE_NOT_CONFIGURED``。
* ``lines``: compact の行の組み立て関数のモジュール（``table`` を使う）。
* ``workspace(workspace_id)``: ``auto_fallback_grep_pattern`` を使うときだけ。``root`` と ``exclude_globs`` を持つ ``Workspace``。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import (
    QUERY_ROWS_DEFAULT,
    QUERY_ROWS_MAX,
    QUERY_TIMEOUT_DEFAULT_MS,
    QUERY_TIMEOUT_MAX_MS,
)
from lucianke.core.model import Confidence, EngineChoice, RelPath
from lucianke.core.records import Record, ToolOutput
from lucianke.index.connection import open_reader
from lucianke.index.public import TEMP_GREP_TABLE, GrepSpec, QueryResult, QueryTimeout, run_query
from lucianke.tools._base import GRAPH_FORMATS, CommonArgs, ToolContext, ToolSpec
from lucianke.workspace.paths import is_excluded

CELL_MAX_CHARS = 80  # 仕様書 T-20 手順 6
QUERY_TIMEOUT_MIN_MS = 50
ACCURACY_CAUTION = "結果に降格行が含まれうる。confidence_level 列を選択して確かめること"
_GRAPH_COLUMNS = ("source_urn", "target_urn")
_LEVELS = {c.value: c for c in Confidence}


class QueryProjectDbArgs(CommonArgs):
    query: str
    query_language: Literal["sql", "datalog"] = Field(
        default="sql", description='"sql" only; "datalog" is reserved.'
    )
    params: dict[str, Any] | None = Field(
        default=None,
        description="Named binds: each :name needs a key and vice versa.",
    )
    limit: int = Field(default=QUERY_ROWS_DEFAULT, ge=1, le=QUERY_ROWS_MAX)
    auto_fallback_grep_pattern: str | None = Field(
        default=None,
        description="Word-bounded regex; matches go to temp table temp_grep_matches.",
    )
    grep_scope_paths: list[str] | None = Field(default=None)
    timeout_ms: int = Field(
        default=QUERY_TIMEOUT_DEFAULT_MS,
        ge=QUERY_TIMEOUT_MIN_MS,
        le=QUERY_TIMEOUT_MAX_MS,
    )
    explain: bool = Field(default=False)


# --- 部品 -------------------------------------------------------------------------------------


def _db_path(ctx: ToolContext) -> Path:
    raw = getattr(ctx.services, "db_path", None)
    if raw is None:
        raise LuciankeError(
            ErrorCode.E_WORKSPACE_NOT_CONFIGURED, "the index database is not configured for this server"
        )
    path = Path(raw)
    if not path.is_file():
        raise LuciankeError(
            ErrorCode.E_WORKSPACE_NOT_CONFIGURED,
            "the index database does not exist yet (no workspace has been indexed)",
            details={"reason": "index_missing"},
        )
    return path


def _grep_spec(ctx: ToolContext, args: QueryProjectDbArgs) -> GrepSpec | None:
    if args.auto_fallback_grep_pattern is None:
        if args.grep_scope_paths is not None:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT,
                "grep_scope_paths needs auto_fallback_grep_pattern",
                details={"param": "grep_scope_paths"},
            )
        return None
    workspace = getattr(ctx.services, "workspace", None)
    if workspace is None:
        raise LuciankeError(
            ErrorCode.E_WORKSPACE_NOT_CONFIGURED, "no workspace is known, so there is nothing to grep"
        )
    ws = workspace(args.workspace_id)
    root = Path(ws.root).resolve()

    def exclude(path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            return True
        return is_excluded(ws, RelPath(rel))

    return GrepSpec(
        pattern=args.auto_fallback_grep_pattern,
        roots=[root],
        scope_paths=args.grep_scope_paths,
        exclude=exclude,
    )


def _unique_names(columns: Sequence[tuple[str, str | None]]) -> list[str]:
    """列名を辞書のキーにするための重複回避（``a.x, b.x`` → ``x``, ``x_2``）。順序は列の順。"""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name, _ in columns:
        n = seen.get(name, 0) + 1
        seen[name] = n
        out.append(name if n == 1 else f"{name}_{n}")
    return out


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        return "x'" + bytes(value).hex() + "'"
    return value


def _cell(value: Any) -> Any:
    """compact の 1 セル。80 文字を超えたら ``…`` で終える。NULL は ``None`` のまま（``table`` が ``NULL`` にする）。"""
    if value is None:
        return None
    text = str(_json_value(value)).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= CELL_MAX_CHARS else text[: CELL_MAX_CHARS - 1] + "…"


def _row_level(data: Mapping[str, Any]) -> Confidence:
    raw = data.get("confidence_level")
    return _LEVELS.get(str(raw), Confidence.EXACT) if raw is not None else Confidence.EXACT


def _grep_summary(result: QueryResult) -> str:
    grep = result.grep_fallback
    if grep is None:
        return "none"
    return f"{grep['pattern']} matches={grep['matches']}{' truncated' if grep['truncated'] else ''}"


def _graph(
    names: Sequence[str], rows: Sequence[Mapping[str, Any]], levels: Sequence[Confidence]
) -> dict[str, Any]:
    """``source_urn`` / ``target_urn`` 列から ``data_static["graph"]``（``mcp/render/graph.py`` の入力）を作る。"""
    edges: list[dict[str, Any]] = []
    for data, level in zip(rows, levels, strict=True):
        source, target = data.get("source_urn"), data.get("target_urn")
        if source is None or target is None:
            continue
        label = data.get("relation_kind")
        edges.append(
            {
                "source": str(source),
                "target": str(target),
                "label": None if label is None else str(label),
                "conf": level.value,
                "attrs": {"guard": data["guard_condition"]} if data.get("guard_condition") else None,
            }
        )
    return {"nodes": [], "edges": edges}


def _lower_rows_exist(db_path: Path, tables: Sequence[str]) -> bool:
    """``confidence_level`` 列を選ばなかったクエリ用: 読んだ公開テーブルに ``exact`` 以外の行があるか（手順 8）。"""
    names = [t for t in tables if t not in (TEMP_GREP_TABLE, "symbols_fts")]
    if not names:
        return False
    con = open_reader(db_path)
    try:
        return any(
            con.execute(f"SELECT 1 FROM \"{t}\" WHERE confidence_level <> 'exact' LIMIT 1").fetchone()
            is not None  # noqa: S608
            for t in names
        )
    except sqlite3.Error:
        return False
    finally:
        con.close()


def _build_output(
    ctx: ToolContext,
    args: QueryProjectDbArgs,
    result: QueryResult,
    *,
    db_path: Path,
    caution: bool,
) -> ToolOutput:
    names = _unique_names(result.columns)
    degraded_by_grep = TEMP_GREP_TABLE in result.tables_read
    has_level = any(n.lower() == "confidence_level" for n in names)
    row_dicts = [{n: _json_value(v) for n, v in zip(names, row, strict=True)} for row in result.rows]
    levels = [
        Confidence.LEXICAL_FALLBACK
        if degraded_by_grep
        else (_row_level(d) if has_level else Confidence.EXACT)
        for d in row_dicts
    ]
    table = ctx.services.lines.table  # type: ignore[attr-defined]
    records: list[Record] = []
    start = ctx.cursor_offset  # このページの先頭の行。見出し行（| col | ... |）を付ける
    for i, (row, data, level) in enumerate(zip(result.rows, row_dicts, levels, strict=True)):
        cells = [_cell(v) for v in row]
        block = ([names] if i == start else []) + [cells]
        records.append(
            Record(
                kind="row", key=f"row:{i}", priority=i, lines=tuple(table(block)), data=data, confidence=level
            )
        )

    shown = f"{result.row_count} (limit {args.limit}, {'truncated' if result.truncated else 'not truncated'})"
    head: list[tuple[str, str]] = [
        ("rows", f"{shown}  {result.elapsed_ms}ms  grep_fallback: {_grep_summary(result)}")
    ]
    if result.plan is not None:
        head += [("plan", f"{p['id']}|{p['parent']}  {p['detail']}") for p in result.plan]
    elif not records:
        head.append(("columns", ", ".join(names) if names else "-"))

    static: dict[str, Any] = {
        "columns": [{"name": n, "decl_type": t} for n, t in result.columns],
        "row_count": result.row_count,
        "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms,
        "grep_fallback": result.grep_fallback,
        "plan": result.plan if result.plan is not None else [],
        "tables_read": list(result.tables_read),
    }
    if caution:
        static["accuracy_warning"] = ACCURACY_CAUTION
    if args.format in GRAPH_FORMATS:
        static["graph"] = _graph(names, row_dicts, levels)
    floor = Confidence.LEXICAL_FALLBACK if degraded_by_grep else Confidence.EXACT
    return ToolOutput(
        records=records,
        head_kv=head,
        data_static=static,
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="sqlite", confidence=floor),
        page_unit="row",
        total_estimate=len(records),
        warnings=[ACCURACY_CAUTION] if caution else [],
    )


# --- ハンドラ ---------------------------------------------------------------------------------


async def handler(ctx: ToolContext, args: QueryProjectDbArgs) -> ToolOutput:
    if args.query_language == "datalog":
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            'query_language "datalog" is not supported yet (use "sql")',
            details={"reason": "datalog_unsupported", "param": "query_language"},
        )
    db_path = _db_path(ctx)
    grep = _grep_spec(ctx, args)
    try:
        result = await asyncio.to_thread(
            run_query,
            db_path,
            args.query,
            params=args.params,
            limit=args.limit,
            timeout_ms=args.timeout_ms,
            grep=grep,
            explain=args.explain,
        )
    except QueryTimeout as exc:
        partial = _build_output(ctx, args, exc.result, db_path=db_path, caution=False)
        raise LuciankeError(exc.code, exc.message, details=exc.details, partial=partial) from None
    if args.format in GRAPH_FORMATS and not set(_GRAPH_COLUMNS) <= {n for n, _ in result.columns}:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            f"format {args.format!r} needs both source_urn and target_urn columns in the result",
            details={"param": "format", "columns": [n for n, _ in result.columns]},
        )
    names = {n.lower() for n, _ in result.columns}
    caution = (
        "confidence_level" not in names
        and TEMP_GREP_TABLE not in result.tables_read
        and not args.explain
        and await asyncio.to_thread(_lower_rows_exist, db_path, result.tables_read)
    )
    return _build_output(ctx, args, result, db_path=db_path, caution=caution)


TOOL_SPEC = ToolSpec(
    name="query_project_db",
    tid="T-20",
    args_model=QueryProjectDbArgs,
    handler=handler,
    default_max_tokens=2000,
    description=(
        "Run one read-only SQL SELECT / WITH over the project knowledge DB (call get_project_db_schema first for tables and columns). :name binds come from params. Select confidence_level for per-row accuracy. dot/mermaid need source_urn and target_urn columns. Writes, ATTACH, PRAGMA and non-public tables are rejected (E_QUERY_REJECTED)."
    ),
    allows_graph=True,
    max_confidence=Confidence.EXACT,
    milestone="M1",
    accepts_revision=False,
    page_unit="row",
    records_key="rows",
    reads_index=True,
)
