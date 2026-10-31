"""T-19 ``get_project_db_schema``（仕様書 §11.8）。PaaD の公開ビューの定義・行数・精度の構成比・サンプル SQL を返す。

エージェントが T-20 のクエリを書くための地図。1 レコード = 1 テーブル。列名・型・NULL 可否・主キー・外部キーは実際の DB から
``PRAGMA table_xinfo`` / ``foreign_key_list`` で読み（DDL とのずれを作らない）、1 行の説明は ``schema.sql`` の列コメントから取る。
既定で返すのは公開 10 テーブル。``symbols_fts`` は ``tables`` で名指ししたときだけ返す（事後条件 1 は 10 個の集合）。

``ctx.services`` に求めるもの（duck typing。``tools`` は ``mcp`` を import できない）:

* ``db_path``: ``paad.sqlite`` の ``Path``。無い・ファイルが無い → ``E_WORKSPACE_NOT_CONFIGURED``。
* ``lines``: compact の行の組み立て関数のモジュール（``kv_line``・``attr_line``・``note``）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput
from lucianke.index.connection import open_reader
from lucianke.index.migrate import schema_sql
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec

#: 公開ビュー 10 個（仕様書 §9.1）。この順で返す。
PUBLIC_VIEWS: tuple[str, ...] = (
    "workspaces", "files", "symbols", "relations", "dataflows", "diagnostics",
    "contracts", "git_commits", "git_symbol_changes", "git_co_changes",
)  # fmt: skip
FTS_TABLE = "symbols_fts"
KNOWN_TABLES: tuple[str, ...] = (*PUBLIC_VIEWS, FTS_TABLE)
COUNT_BUDGET_S = 0.1  # 仕様書 T-19 手順 3: COUNT(*) を待つ上限
_LEVELS = ("exact", "structural", "heuristic", "lexical_fallback")

_TABLE_DOC: dict[str, str] = {
    "workspaces": "ワークスペース 1 件 = 1 行。ルート、アクティブブランチ、LSP の状態、最後に索引した時刻",
    "files": "索引済みファイル 1 件 = 1 行。言語、Tier（1〜4）、行数、sha256、テストか否か、PageRank",
    "symbols": "クラス・関数・変数・スキーマ要素・未知言語タグ。1 行 1 シンボル",
    "relations": "シンボル間の有向エッジ（CALLS, IMPLEMENTS, INHERITS, REFERENCES, IMPORTS, TYPE_USES, READS_TABLE, WRITES_TABLE, RENDERS_TEMPLATE, EXPOSES_ENDPOINT, BINDS_NATIVE）",
    "dataflows": "データフロー（ソースからシンクまでの経路）。1 経路 = 1 行。path_json に中継点",
    "diagnostics": "LSP と構文木の診断。1 件 = 1 行。enclosing_urn で囲むシンボルに結べる",
    "contracts": "抽出した契約（PRE / POST / INVARIANT / THROWS）。型・ガード・Docstring・OpenAPI・Gherkin 由来",
    "git_commits": "Git のコミット。1 件 = 1 行（メッセージは 1 行目だけ）",
    "git_symbol_changes": "コミットごとのシンボルの変更（ADDED, MODIFIED_BODY, MODIFIED_SIG, DELETED, RENAMED）",
    "git_co_changes": "共変更結合。entity_a が変わったとき entity_b も変わる割合（coupling_ratio）",
    FTS_TABLE: "symbols の全文検索（FTS5）。symbols_fts MATCH 'text' で検索し、rowid で symbols と結合する",
}
_CONFIDENCE_DOC = "精度水準: exact | structural | heuristic | lexical_fallback（仕様書 §8.3）"

#: 付録 A のサンプル SQL。T-20 にそのまま渡せる形にする（定数は ``:name`` にして ``params_example`` を添える）。
SAMPLE_QUERIES: tuple[dict[str, Any], ...] = (
    {
        "id": "A-1",
        "title": "直近 N コミットで変わった関数のうち、3 か所以上から呼ばれ、テストから参照が無く、エラー診断を持つもの（上位 5）",
        "sql": """WITH recent AS (
  SELECT commit_sha FROM git_commits ORDER BY committed_at DESC LIMIT :n
)
SELECT s.urn, f.path, s.signature,
       COUNT(DISTINCT r_in.source_urn) AS caller_count,
       MAX(d.message)                  AS current_error
FROM symbols s
JOIN files f               ON f.file_id = s.file_id
JOIN git_symbol_changes g  ON g.urn = s.urn AND g.commit_sha IN (SELECT commit_sha FROM recent)
LEFT JOIN relations r_in   ON r_in.target_urn = s.urn AND r_in.relation_kind = 'CALLS'
LEFT JOIN diagnostics d    ON d.enclosing_urn = s.urn AND d.severity = 'error'
WHERE s.kind IN ('Function', 'Method')
  AND NOT EXISTS (
    SELECT 1 FROM relations rt JOIN files ft ON ft.file_id = rt.source_file_id
    WHERE rt.target_urn = s.urn AND ft.is_test = 1
  )
GROUP BY s.urn
HAVING caller_count >= 3 OR current_error IS NOT NULL
ORDER BY caller_count DESC
LIMIT 5""",
        "params_example": {"n": 10},
        "appendix_ref": "A-1",
    },
    {
        "id": "A-2",
        "title": "公開メソッドのうち Doc 要約が無いもの（ファイル別件数）",
        "sql": """SELECT f.path, COUNT(*) AS undocumented
FROM symbols s JOIN files f ON f.file_id = s.file_id
WHERE s.kind = 'Method'
  AND s.modifiers_json LIKE '%"pub"%'
  AND (s.doc_summary IS NULL OR s.doc_summary = '')
GROUP BY f.path
ORDER BY undocumented DESC
LIMIT 20""",
        "params_example": {},
        "appendix_ref": "A-2",
    },
    {
        "id": "A-3",
        "title": "あるファイルと一緒に変わりやすいファイル（共変更結合が min_ratio 以上）",
        "sql": """SELECT entity_b AS path_urn, coupling_ratio, co_change_count, last_co_changed_sha
FROM git_co_changes
WHERE entity_a = :entity_a
  AND entity_kind = 'file'
  AND coupling_ratio >= :min_ratio
ORDER BY coupling_ratio DESC""",
        "params_example": {"entity_a": "lk://app/src/services/payment.py", "min_ratio": 0.5},
        "appendix_ref": "A-3",
    },
    {
        "id": "A-4",
        "title": "推移的な呼び出し元（深さ 3 まで）を再帰 CTE で辿る",
        "sql": """WITH RECURSIVE callers(urn, depth) AS (
  SELECT :target, 0
  UNION
  SELECT r.source_urn, c.depth + 1
  FROM relations r JOIN callers c ON r.target_urn = c.urn
  WHERE r.relation_kind = 'CALLS' AND c.depth < 3
)
SELECT c.urn, c.depth, s.signature, s.confidence_level
FROM callers c JOIN symbols s ON s.urn = c.urn
WHERE c.depth > 0
ORDER BY c.depth, c.urn""",
        "params_example": {"target": "lk://app/src/pricing.py#apply_discount"},
        "appendix_ref": "A-4",
    },
    {
        "id": "A-5",
        "title": "降格（exact 以外）で索引されているファイルの一覧（言語・Tier・水準別）",
        "sql": """SELECT language, tier, confidence_level, COUNT(*) AS files
FROM files
WHERE confidence_level <> 'exact'
GROUP BY language, tier, confidence_level
ORDER BY files DESC""",
        "params_example": {},
        "appendix_ref": "A-5",
    },
)


class GetProjectDbSchemaArgs(CommonArgs):
    tables: list[str] | None = Field(
        default=None,
        description="Default: the 10 public tables.",
    )
    include_stats: bool = Field(default=True)
    include_sample_queries: bool = Field(default=True)
    include_indexes: bool = Field(default=False)


# --- DDL のコメント ---------------------------------------------------------------------------

_CREATE_RE = re.compile(r"^CREATE TABLE (\w+)\s*\(")
_COLUMN_RE = re.compile(r"^\s*(\w+)\s+(?:TEXT|INTEGER|REAL|BLOB|NUMERIC)\b")


@cache
def column_comments() -> dict[tuple[str, str], str]:
    """``schema.sql`` の ``CREATE TABLE`` の列行にある ``-- コメント``。(テーブル, 列) → 文字列。"""
    out: dict[tuple[str, str], str] = {}
    table: str | None = None
    for line in schema_sql().splitlines():
        if (m := _CREATE_RE.match(line)) is not None:
            table = m.group(1)
            continue
        if table is None:
            continue
        if line.startswith(");"):
            table = None
            continue
        code, sep, comment = line.partition("--")
        col = _COLUMN_RE.match(code)
        if col is not None and sep and comment.strip():
            out[(table, col.group(1))] = comment.strip()
    return out


# --- DB の読み取り ----------------------------------------------------------------------------


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


def _columns(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    fks: dict[str, str] = {}
    for row in con.execute(f'PRAGMA foreign_key_list("{table}")'):  # id, seq, table, from, to, ...
        target = row[4]
        if target is None:  # 参照先の主キーを暗黙に指す書き方
            pk = [r[1] for r in con.execute(f'PRAGMA table_info("{row[2]}")') if r[5]]
            target = pk[0] if pk else "rowid"
        fks[row[3]] = f"{row[2]}.{target}"
    comments = column_comments()
    cols: list[dict[str, Any]] = []
    for _cid, name, decl, notnull, _default, pk, hidden in con.execute(f'PRAGMA table_xinfo("{table}")'):
        if hidden:  # FTS5 の内部列（テーブル名と同名の列、rank）
            continue
        description = comments.get((table, name))
        if description is None and name == "confidence_level":
            description = _CONFIDENCE_DOC
        cols.append(
            {
                "name": name,
                "decl_type": decl or "TEXT",
                "nullable": not notnull and not pk,
                "pk": bool(pk),
                "fk": fks.get(name),
                "description": description,
            }
        )
    return cols


def _indexes(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in con.execute(f'PRAGMA index_list("{table}")'):  # seq, name, unique, origin, partial
        cols = [r[2] for r in con.execute(f'PRAGMA index_info("{row[1]}")') if r[2] is not None]
        out.append({"name": row[1], "columns": cols, "unique": bool(row[2])})
    return out


class _Interrupter:
    """期限を過ぎた接続に ``interrupt()`` を送る見張り。プロセスで 1 本のスレッドを共有する（BUG-0128）。

    以前は ``COUNT(*)`` のたびに ``threading.Timer`` を作って ``join`` していた（1 回の呼び出しで 10 本）。CPU が混んでいると
    スレッドの起動と終わりの待ちのたびに順番待ちになり、16 ms の呼び出しが 120 ms〜1 秒に延びた（14 論理 CPU を 16 本の
    計算で埋めた実測: p50 14→122 ms・p95 1028 ms。Timer なしなら 8→16 ms）。

    * ``arm`` は期限を登録するだけ。見張りを起こすのは、見張りがいま眠っている期限より早い期限を登録したとき
      （待つものが無く無期限に眠っているときを含む）だけで、続けて登録する同じ長さの期限では起こさない。
    * 送るのは錠の中で、まだ登録されている期限だけ。``disarm`` が返った後に、その期限の ``interrupt()`` は届かない
      （遅れて届いた ``interrupt()`` が次の文を巻き込まない）。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._armed: dict[int, tuple[float, sqlite3.Connection]] = {}
        self._seq = itertools.count()
        self._wake_at = math.inf  # 見張りが次に目を覚ます時刻（無期限に眠っているときは inf）
        self._thread: threading.Thread | None = None

    def arm(self, con: sqlite3.Connection, budget_s: float) -> int:
        deadline = time.monotonic() + budget_s
        with self._cond:
            token = next(self._seq)
            self._armed[token] = (deadline, con)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="lucianke-count-interrupter", daemon=True)
                self._wake_at = deadline
                self._thread.start()
            elif deadline < self._wake_at:
                self._wake_at = deadline
                self._cond.notify()
        return token

    def disarm(self, token: int) -> None:
        with self._cond:
            self._armed.pop(token, None)

    def _run(self) -> None:
        with self._cond:
            while True:
                now = time.monotonic()
                for token, (deadline, con) in list(self._armed.items()):
                    if deadline <= now:
                        del self._armed[token]
                        con.interrupt()
                self._wake_at = min((d for d, _ in self._armed.values()), default=math.inf)
                self._cond.wait(None if self._wake_at == math.inf else self._wake_at - now)


_INTERRUPTER = _Interrupter()


def _count_rows(
    con: sqlite3.Connection, table: str, budget_s: float = COUNT_BUDGET_S
) -> tuple[int | None, bool]:
    """``COUNT(*)`` が ``budget_s`` 以内に終わればその値と True。超えたら ``MAX(rowid)``（上限の目安）と False。

    ``count(*)`` は 1 命令で B 木を歩くので progress handler では止まらない。共有の見張り（``_Interrupter``）の
    ``interrupt()`` で止める。
    """
    token = _INTERRUPTER.arm(con, budget_s)
    try:
        return int(con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]), True
    except sqlite3.OperationalError:
        pass  # interrupted
    finally:
        _INTERRUPTER.disarm(token)
    try:
        row = con.execute(f'SELECT max(rowid) FROM "{table}"').fetchone()
        return (int(row[0]) if row and row[0] is not None else 0), False
    except sqlite3.Error:
        return None, False


def coverage_of(con: sqlite3.Connection) -> dict[str, float]:
    """``symbols`` と ``relations`` の ``confidence_level`` 別の行数割合。小数第 3 位で丸めて和を 1.0 にそろえる。"""
    counts = dict.fromkeys(_LEVELS, 0)
    for table in ("symbols", "relations"):
        for level, n in con.execute(
            f"SELECT confidence_level, count(*) FROM {table} GROUP BY confidence_level"
        ):  # noqa: S608
            if level in counts:
                counts[level] += int(n)
    total = sum(counts.values())
    if total == 0:
        return {"exact": 1.0, "structural": 0.0, "heuristic": 0.0, "lexical_fallback": 0.0}
    shares = {k: round(v / total, 3) for k, v in counts.items()}
    diff = round(1.0 - sum(shares.values()), 3)
    if diff:
        biggest = max(shares, key=lambda k: counts[k])
        shares[biggest] = round(shares[biggest] + diff, 3)
    return shares


def _read_schema(db_path: Path, names: list[str], args: GetProjectDbSchemaArgs) -> dict[str, Any]:
    con = open_reader(db_path)
    try:
        tables: list[dict[str, Any]] = []
        for name in names:
            entry: dict[str, Any] = {"name": name, "row_count": None, "row_count_exact": None}
            if args.include_stats:
                entry["row_count"], entry["row_count_exact"] = _count_rows(con, name)
            entry["description"] = _TABLE_DOC[name]
            entry["columns"] = _columns(con, name)
            entry["indexes"] = _indexes(con, name) if args.include_indexes else []
            tables.append(entry)
        info: dict[str, Any] = {"tables": tables}
        row = con.execute("SELECT max(indexed_at) FROM workspaces").fetchone()
        info["indexed_at"] = row[0] if row else None
        if args.include_stats:
            info["coverage"] = coverage_of(con)
        return info
    finally:
        con.close()


# --- compact の行 -----------------------------------------------------------------------------


def _sig(columns: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for c in columns:
        text = f"{c['name']} {c['decl_type']}{'?' if c['nullable'] else ''}"
        if c["pk"]:
            text += " PK"
        if c["fk"]:
            text += f" FK {c['fk']}"
        parts.append(text)
    return ", ".join(parts)


def _count_text(entry: Mapping[str, Any], include_stats: bool) -> str:
    if not include_stats:
        return ""
    n = entry["row_count"]
    if n is None:
        return " (rows ?)"
    return f" ({n} rows)" if entry["row_count_exact"] else f" (~{n} rows)"


def _percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def _table_lines(lines: Any, entry: Mapping[str, Any], include_stats: bool) -> list[str]:
    out = [
        lines.kv_line("table", f"{entry['name']}{_count_text(entry, include_stats)}"),
        lines.attr_line("sig", _sig(entry["columns"]), indent=0),
        lines.attr_line("doc", entry["description"], indent=0),
    ]
    if entry["indexes"]:
        out.append(
            lines.note(
                "indexes: " + "; ".join(f"{i['name']}({', '.join(i['columns'])})" for i in entry["indexes"])
            )
        )
    return out


def _sample_lines(lines: Any) -> list[str]:
    out = [lines.kv_line("sample_queries", "pass sql and params to query_project_db")]
    for q in SAMPLE_QUERIES:
        out.append(
            lines.note(
                f"{q['id']} {q['title']}: {q['sql']} (params: {json.dumps(q['params_example'], ensure_ascii=False)})"
            )
        )
    return out


# --- ハンドラ ---------------------------------------------------------------------------------


def _wanted(args: GetProjectDbSchemaArgs) -> list[str]:
    if not args.tables:
        return list(PUBLIC_VIEWS)
    unknown = [t for t in args.tables if t not in KNOWN_TABLES]
    if unknown:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            f"unknown table(s): {', '.join(unknown)} (public tables: {', '.join(KNOWN_TABLES)})",
            details={"param": "tables", "unknown": unknown, "known_tables": list(KNOWN_TABLES)},
        )
    return list(dict.fromkeys(args.tables))


async def handler(ctx: ToolContext, args: GetProjectDbSchemaArgs) -> ToolOutput:
    names = _wanted(args)
    db_path = _db_path(ctx)
    info = await asyncio.to_thread(_read_schema, db_path, names, args)
    lines = ctx.services.lines  # type: ignore[attr-defined]
    entries: list[dict[str, Any]] = info["tables"]
    records: list[Record] = []
    for i, entry in enumerate(entries):
        block = _table_lines(lines, entry, args.include_stats)
        if args.include_sample_queries and i == len(entries) - 1:
            block += _sample_lines(lines)  # 予算の勘定に載せるため、最後の表のレコードの行として運ぶ
        records.append(
            Record(
                kind="kv",
                key=f"table:{entry['name']}",
                priority=i,
                lines=tuple(block),
                data=entry,
                confidence=Confidence.EXACT,
            )
        )

    db_parts = [f"sqlite  views={len([n for n in names if n != FTS_TABLE])}"]
    if info["indexed_at"]:
        db_parts.append(f"indexed_at={info['indexed_at']}")
    static: dict[str, Any] = {
        "engine": {"name": "sqlite", "version": sqlite3.sqlite_version},
        "indexed_at": info["indexed_at"],
        "coverage": info.get("coverage"),
        "sample_queries": [dict(q) for q in SAMPLE_QUERIES] if args.include_sample_queries else [],
    }
    head: list[tuple[str, str]] = []
    coverage = info.get("coverage")
    if coverage is not None:
        db_parts.append("coverage: " + " ".join(f"{k} {_percent(v)}" for k, v in coverage.items()))
    head.append(("db", "  ".join(db_parts)))
    warnings: list[str] = []
    if coverage is not None and coverage["lexical_fallback"] > 0:
        warnings.append(
            f"lexical_fallback の行が {_percent(coverage['lexical_fallback'])} 含まれる。クエリでは confidence_level 列で絞ること"
        )
    return ToolOutput(
        records=records,
        head_kv=head,
        data_static=static,
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="sqlite", confidence=Confidence.EXACT),
        page_unit="table",
        total_estimate=len(records),
        warnings=warnings,
    )


TOOL_SPEC = ToolSpec(
    name="get_project_db_schema",
    tid="T-19",
    args_model=GetProjectDbSchemaArgs,
    handler=handler,
    default_max_tokens=6000,
    description=(
        "Describe the project knowledge DB used by query_project_db: the 10 public tables (columns, types, PK/FK, meanings), row counts, confidence_level composition, and up to 5 ready-to-run sample SQL statements. Call it once before writing SQL."
    ),
    max_confidence=Confidence.EXACT,
    milestone="M1",
    accepts_revision=False,
    page_unit="table",
    records_key="tables",
)
