"""T-19 ``get_project_db_schema`` と T-20 ``query_project_db`` の契約テスト（仕様書 §11.8 の事後条件）。

``tmp_path`` に ``ensure_schema`` で PaaD を作って数行入れ、偽のレジストリに ``TOOL_SPEC`` を登録した ``Pipeline`` で呼ぶ
（``tools/__init__.py`` と ``app.py`` の結線には依存しない）。サービスは ``db_path``・``lines``・``workspace()`` だけを持つ偽物。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import compact_grammar as g
import pytest

from lucianke.index.migrate import ensure_schema, schema_sql
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.tools import t19_get_project_db_schema as t19
from lucianke.tools import t20_query_project_db as t20
from lucianke.tools._base import Services
from lucianke.workspace.types import Workspace

NOW = "2026-10-29T11:20:04+09:00"
PUBLIC = t19.PUBLIC_VIEWS


@dataclass(slots=True)
class FakeServices(Services):
    db_path: Path | None
    lines: ModuleType
    root: Path | None = None

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        assert self.root is not None
        return Workspace(workspace_id="app", root=self.root)


def _sym(urn: str, name: str, level: str, kind: str = "Function") -> tuple[Any, ...]:
    return (
        urn,
        1,
        0,
        None,
        name,
        name,
        kind,
        "[]",
        1,
        2,
        None,
        None,
        None,
        None,
        None,
        None,
        "source",
        None,
        None,
        None,
        level,
    )


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO workspaces(workspace_id, root_path, indexed_at) VALUES ('app', '/app', ?)", (NOW,)
    )
    conn.execute(
        "INSERT INTO files(file_id, workspace_id, path, language, tier, line_count, byte_size, sha256, is_test, indexed_at, "
        "confidence_level) VALUES (1, 'app', 'src/a.py', 'python', 1, 10, 100, 'x', 0, ?, 'exact')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO files(file_id, workspace_id, path, language, tier, line_count, byte_size, sha256, is_test, indexed_at, "
        "confidence_level) VALUES (2, 'app', 'legacy/x.cob', 'cobol', 4, 5, 50, 'y', 0, ?, 'lexical_fallback')",
        (NOW,),
    )
    cols = (
        "urn, file_id, is_external, parent_urn, name, qualified_name, kind, modifiers_json, start_line, end_line, "
        "sel_line, sel_start_col, sel_end_col, signature, return_type, doc_summary, declared_in, complexity, scip_symbol, "
        "body_sha256, confidence_level"
    )
    marks = ", ".join("?" * 21)
    for i, level in enumerate(["exact", "exact", "exact", "heuristic", "exact"], start=1):
        conn.execute(
            f"INSERT INTO symbols({cols}) VALUES ({marks})", _sym(f"lk://app/src/a.py#f{i}", f"f{i}", level)
        )
    for i, level in enumerate(["exact", "exact", "structural", "lexical_fallback"]):
        conn.execute(
            "INSERT INTO relations(source_urn, target_urn, relation_kind, source_file_id, confidence_level) "
            "VALUES (?, ?, 'CALLS', 1, ?)",
            (f"lk://app/src/a.py#f{i + 1}", f"lk://app/src/a.py#f{(i + 1) % 5 + 1}", level),
        )
    conn.close()


def make_pipeline(
    db: Path | None, *, root: Path | None = None, specs: tuple[Any, ...] | None = None
) -> Pipeline:
    registry = ToolRegistry()
    for spec in specs or (t19.TOOL_SPEC, t20.TOOL_SPEC):
        registry.register(spec)
    return Pipeline(registry, services=FakeServices(db_path=db, lines=compact, root=root))


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "paad.sqlite"
    make_db(path)
    return path


@pytest.fixture
def pipeline(db: Path, tmp_path: Path) -> Pipeline:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("def foo():\n    return needle_word\n", encoding="utf-8")
    (root / "skip").mkdir()
    (root / "skip" / "b.py").write_text("needle_word = 1\n", encoding="utf-8")
    return make_pipeline(db, root=root)


async def schema(pipeline: Pipeline, **args: Any):
    # json は全テーブルの全列を数える（BUG-0017）。既定の予算では 10 テーブルが入らないので、全部入る予算にする
    return await pipeline.call_tool("get_project_db_schema", {"format": "json", "max_tokens": 20000, **args})


async def query(pipeline: Pipeline, sql: str, fmt: str = "json", **args: Any):
    return await pipeline.call_tool("query_project_db", {"format": fmt, "query": sql, **args})


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count_all(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in PUBLIC}  # noqa: S608
    finally:
        conn.close()


# --- T-19 -------------------------------------------------------------------------------------


async def test_t19_post1_default_tables_are_the_ten_public_views(pipeline: Pipeline) -> None:
    r = await schema(pipeline)
    assert r.structured is not None and r.structured["ok"], r.text
    names = [t["name"] for t in r.structured["data"]["tables"]]
    assert set(names) == set(PUBLIC) and len(names) == 10
    assert "symbols_fts" not in names
    assert not any(n.startswith("_") for n in names)  # 内部テーブルは列挙しない
    assert r.structured["_resolution_meta"]["confidence_level"] == "exact"
    assert r.structured["_resolution_meta"]["is_degraded"] is False


async def test_t19_post2_column_names_match_the_ddl(pipeline: Pipeline) -> None:
    ddl = sqlite3.connect(":memory:")
    ddl.executescript(schema_sql())
    expected = {t: {row[1] for row in ddl.execute(f"PRAGMA table_info({t})")} for t in PUBLIC}
    r = await schema(pipeline)
    got = {t["name"]: {c["name"] for c in t["columns"]} for t in r.structured["data"]["tables"]}
    for table in PUBLIC:
        assert got[table] == expected[table], table
        assert expected[table] - got[table] == set()


async def test_t19_columns_carry_type_nullability_keys_and_ddl_comments(pipeline: Pipeline) -> None:
    r = await schema(pipeline)
    tables = {t["name"]: t for t in r.structured["data"]["tables"]}
    cols = {c["name"]: c for c in tables["symbols"]["columns"]}
    assert (
        cols["urn"]["pk"] is True and cols["urn"]["nullable"] is False and cols["urn"]["decl_type"] == "TEXT"
    )
    assert cols["file_id"]["fk"] == "files.file_id" and cols["file_id"]["nullable"] is True
    assert cols["parent_urn"]["fk"] == "symbols.urn"
    assert (
        cols["qualified_name"]["description"] == "'PaymentProcessor.process_charge'"
    )  # DDL の列コメントと同一
    assert cols["confidence_level"]["decl_type"] == "TEXT" and cols["confidence_level"]["description"]
    assert tables["symbols"]["description"]
    assert all(t["description"] for t in tables.values())


async def test_t19_post3_sample_queries_run_in_t20(pipeline: Pipeline) -> None:
    r = await schema(pipeline)
    samples = r.structured["data"]["sample_queries"]
    assert 1 <= len(samples) <= 5
    for q in samples:
        assert set(q) >= {"id", "title", "sql", "params_example", "appendix_ref"}
        got = await query(pipeline, q["sql"], params=q["params_example"])
        assert got.structured is not None and got.structured["ok"], (q["id"], got.text)


async def test_t19_post4_exact_row_counts_match_count_star(pipeline: Pipeline, db: Path) -> None:
    r = await schema(pipeline)
    expected = count_all(db)
    assert expected["symbols"] == 5 and expected["relations"] == 4
    for t in r.structured["data"]["tables"]:
        assert t["row_count_exact"] is True
        assert t["row_count"] == expected[t["name"]], t["name"]


async def test_t19_post5_coverage_sums_to_one(pipeline: Pipeline, tmp_path: Path) -> None:
    r = await schema(pipeline)
    cov = r.structured["data"]["coverage"]
    assert set(cov) == {"exact", "structural", "heuristic", "lexical_fallback"}
    assert round(sum(cov.values()), 3) == 1.0
    assert cov["exact"] == pytest.approx(6 / 9, abs=0.002) and cov["lexical_fallback"] == pytest.approx(
        1 / 9, abs=0.002
    )
    assert any("lexical_fallback" in w for w in r.structured["warnings"])  # Tier 4 相当の行があれば注意
    empty = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(empty, isolation_level=None)
    ensure_schema(conn)
    conn.close()
    r2 = await schema(make_pipeline(empty))
    assert r2.structured["data"]["coverage"] == {
        "exact": 1.0,
        "structural": 0.0,
        "heuristic": 0.0,
        "lexical_fallback": 0.0,
    }


def test_t19_coverage_rounding_never_breaks_the_sum() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_sql())
    conn.execute("INSERT INTO workspaces(workspace_id, root_path) VALUES ('w', '/w')")
    for i, level in enumerate(["exact"] * 1 + ["structural"] * 1 + ["heuristic"] * 1):
        conn.execute(
            "INSERT INTO relations(source_urn, target_urn, relation_kind, confidence_level) VALUES (?, 'x', 'CALLS', ?)",
            (f"s{i}", level),
        )
    assert (
        round(sum(t19.coverage_of(conn).values()), 3) == 1.0
    )  # 1/3 ずつ: 0.333 * 3 = 0.999 を 1.0 にそろえる


async def test_t19_tables_filter_and_unknown_name(pipeline: Pipeline) -> None:
    r = await schema(pipeline, tables=["relations", "symbols"], include_sample_queries=False)
    assert [t["name"] for t in r.structured["data"]["tables"]] == ["relations", "symbols"]
    assert r.structured["data"]["sample_queries"] == []
    bad = await schema(pipeline, tables=["symbols", "_meta"])
    assert bad.is_error and bad.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    assert "symbols" in bad.structured["error"]["details"]["known_tables"]
    fts = await schema(pipeline, tables=["symbols_fts"])
    cols = [c["name"] for c in fts.structured["data"]["tables"][0]["columns"]]
    assert cols == ["name", "qualified_name", "signature", "doc_summary"]


async def test_t19_indexes_and_stats_switches(pipeline: Pipeline) -> None:
    on = await schema(pipeline, tables=["symbols"], include_indexes=True)
    names = {i["name"] for i in on.structured["data"]["tables"][0]["indexes"]}
    assert {"idx_symbols_file", "idx_symbols_name"} <= names
    off = await schema(pipeline, tables=["symbols"], include_stats=False)
    entry = off.structured["data"]["tables"][0]
    assert entry["row_count"] is None and entry["indexes"] == []
    assert off.structured["data"]["coverage"] is None


async def test_t19_compact_is_grammatical_and_names_every_table(pipeline: Pipeline) -> None:
    r = await pipeline.call_tool("get_project_db_schema", {"format": "compact", "max_tokens": 6000})
    assert not r.is_error, r.text
    assert g.validate(r.text) == [], g.validate(r.text)
    first = r.text.splitlines()[0]
    assert first.startswith("# get_project_db_schema ok conf=exact lsp=n/a")
    for table in PUBLIC:
        assert f"table: {table} (" in r.text
    assert "db: sqlite  views=10" in r.text and "coverage: exact" in r.text
    assert "sample_queries:" in r.text and "- A-4 " in r.text


def test_t19_count_falls_back_to_max_rowid_when_count_is_interrupted() -> None:
    class Slow:
        def interrupt(self) -> None: ...

        def execute(self, sql: str):
            class Cur:
                def fetchone(self_inner):  # noqa: N805
                    return (42,)

            if sql.startswith("SELECT count"):
                raise sqlite3.OperationalError("interrupted")
            return Cur()

    assert t19._count_rows(Slow(), "symbols") == (42, False)  # type: ignore[arg-type]


async def test_t19_without_db_is_workspace_not_configured(tmp_path: Path) -> None:
    for db in (None, tmp_path / "missing.sqlite"):
        r = await schema(make_pipeline(db))
        assert r.is_error and r.structured["error"]["code"] == "E_WORKSPACE_NOT_CONFIGURED"


# --- T-20 -------------------------------------------------------------------------------------

WRITES = [
    "INSERT INTO files(file_id) VALUES (99)",
    "UPDATE symbols SET name = 'x'",
    "DELETE FROM symbols",
    "CREATE TABLE t(a)",
    "DROP TABLE symbols",
    "ALTER TABLE symbols ADD COLUMN z",
    "ATTACH DATABASE ':memory:' AS m",
    "PRAGMA journal_mode = DELETE",
    "SELECT 1; DELETE FROM symbols",
    "WITH x AS (SELECT 1) DELETE FROM symbols",
    "SELECT * FROM _meta",
]


async def test_t20_post1_and_2_writes_are_rejected_and_the_db_is_untouched(
    pipeline: Pipeline, db: Path
) -> None:
    before_hash, before_counts = sha256_of(db), count_all(db)
    for sql in WRITES:
        r = await query(pipeline, sql)
        assert r.is_error, sql
        assert r.structured["error"]["code"] == "E_QUERY_REJECTED", (sql, r.text)
        assert r.structured["error"]["details"]["reason"] in {
            "multiple_statements", "not_select", "forbidden_keyword", "non_public_object", "syntax_error",
        }  # fmt: skip
    assert sha256_of(db) == before_hash and count_all(db) == before_counts
    ok = await query(pipeline, "SELECT count(*) AS n FROM symbols")
    assert ok.structured["ok"] and sha256_of(db) == before_hash and count_all(db) == before_counts


async def test_t20_rejection_reasons(pipeline: Pipeline) -> None:
    assert (await query(pipeline, "DELETE FROM symbols")).structured["error"]["details"][
        "reason"
    ] == "not_select"
    assert (await query(pipeline, "SELECT 1; SELECT 2")).structured["error"]["details"][
        "reason"
    ] == "multiple_statements"
    assert (await query(pipeline, "SELECT * FROM _meta")).structured["error"]["details"][
        "reason"
    ] == "non_public_object"
    bad = await query(pipeline, "SELECT FROM WHERE")
    assert bad.structured["error"]["details"]["reason"] == "syntax_error"
    assert bad.structured["error"]["details"]["sqlite_message"]


async def test_t20_post3_row_count_and_truncated(pipeline: Pipeline) -> None:
    for limit, truncated in [(1, True), (4, True), (5, False), (6, False), (2000, False)]:
        r = await query(pipeline, "SELECT urn FROM symbols ORDER BY urn", limit=limit)
        data = r.structured["data"]
        assert data["row_count"] == min(limit, 5) <= limit
        assert data["truncated"] is truncated, limit
        assert len(data["rows"]) == data["row_count"]
    for bad in (0, 2001):
        r = await query(pipeline, "SELECT 1", limit=bad)
        assert r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


async def test_t20_post4_elapsed_within_timeout(pipeline: Pipeline) -> None:
    r = await query(pipeline, "SELECT count(*) AS n FROM relations", timeout_ms=500)
    assert r.structured["ok"] and r.structured["data"]["elapsed_ms"] <= 500
    runaway = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r"
    t = await query(pipeline, runaway, timeout_ms=50)
    assert t.is_error and t.structured["error"]["code"] == "E_TIMEOUT"
    assert t.structured["error"]["details"]["partial"] is True
    assert t.structured["data"]["elapsed_ms"] <= 1000  # 部分結果が data に載る
    for bad in (49, 5001):
        r = await query(pipeline, "SELECT 1", timeout_ms=bad)
        assert r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


async def test_t20_post5_params_and_named_binds_correspond(pipeline: Pipeline) -> None:
    sql = "SELECT name FROM symbols WHERE kind = :kind AND name <> :skip ORDER BY name"
    ok = await query(pipeline, sql, params={"kind": "Function", "skip": "f1"})
    assert ok.structured["ok"] and [row["name"] for row in ok.structured["data"]["rows"]] == [
        "f2",
        "f3",
        "f4",
        "f5",
    ]
    extra = await query(pipeline, sql, params={"kind": "Function", "skip": "f1", "unused": 1})
    assert extra.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    missing = await query(pipeline, sql, params={"kind": "Function"})
    assert missing.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    none = await query(pipeline, sql)
    assert none.structured["error"]["code"] == "E_INVALID_ARGUMENT"


async def test_t20_datalog_is_unsupported(pipeline: Pipeline) -> None:
    r = await query(pipeline, "?- symbol(X).", query_language="datalog")
    assert r.is_error and r.structured["error"]["code"] == "E_UNSUPPORTED"


async def test_t20_json_shape(pipeline: Pipeline) -> None:
    r = await query(pipeline, "SELECT urn, name AS n, NULL AS nul FROM symbols ORDER BY urn LIMIT 2")
    data = r.structured["data"]
    assert [c["name"] for c in data["columns"]] == ["urn", "n", "nul"]
    assert data["columns"][0]["decl_type"] == "TEXT" and data["columns"][2]["decl_type"] is None
    assert data["rows"][0] == {"urn": "lk://app/src/a.py#f1", "n": "f1", "nul": None}
    assert data["grep_fallback"] is None and data["plan"] == []
    assert set(data) >= {"columns", "rows", "row_count", "truncated", "elapsed_ms", "grep_fallback", "plan"}
    assert r.structured["_resolution_meta"]["confidence_level"] == "exact"


async def test_t20_compact_table(pipeline: Pipeline) -> None:
    long = "x" * 200
    r = await query(
        pipeline,
        "SELECT name, NULL AS missing, :long AS wide, 'a|b' AS pipe FROM symbols ORDER BY name LIMIT 2",
        fmt="compact",
        params={"long": long},
    )
    assert not r.is_error, r.text
    assert g.validate(r.text) == [], g.validate(r.text)
    lines = r.text.splitlines()
    assert lines[0].startswith("# query_project_db ok conf=exact lsp=n/a")
    i = next(n for n, line in enumerate(lines) if line.startswith("rows:"))
    assert lines[i].startswith("rows: 2 (limit 200, not truncated)") and "grep_fallback: none" in lines[i]
    assert lines[i + 1] == "| name | missing | wide | pipe |"
    assert lines[i + 2] == rf"| f1 | NULL | {'x' * 79}… | a\|b |"
    assert len(lines[i + 2].split(" | ")[2]) == 80
    assert sum(1 for line in lines if line == "| name | missing | wide | pipe |") == 1


async def test_t20_compact_zero_rows_names_the_columns(pipeline: Pipeline) -> None:
    r = await query(pipeline, "SELECT urn, name FROM symbols WHERE 0", fmt="compact")
    assert g.validate(r.text) == []
    assert "rows: 0 (limit 200, not truncated)" in r.text and "columns: urn, name" in r.text


async def test_t20_second_page_repeats_the_table_header(pipeline: Pipeline) -> None:
    sql = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 80) "
        "SELECT i, 'row number ' || i AS label FROM n"
    )
    first = await pipeline.call_tool(
        "query_project_db", {"format": "compact", "query": sql, "max_tokens": 200}
    )
    cursor = next(
        (ln.split("cursor=")[1].split()[0] for ln in first.text.splitlines() if ln.startswith("# more:")),
        None,
    )
    assert cursor is not None, first.text
    assert g.validate(first.text) == []
    second = await pipeline.call_tool(
        "query_project_db", {"format": "compact", "query": sql, "max_tokens": 200, "cursor": cursor}
    )
    assert not second.is_error, second.text
    assert g.validate(second.text) == []
    body = [ln for ln in second.text.splitlines() if ln.startswith("| ")]
    assert body[0] == "| i | label |"
    assert "| 1 | row number 1 |" not in second.text


async def test_t20_accuracy_comes_from_the_confidence_level_column(pipeline: Pipeline) -> None:
    r = await query(pipeline, "SELECT urn, confidence_level FROM symbols ORDER BY urn")
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "heuristic" and meta["mixed"] is True and meta["is_degraded"] is True
    assert meta["confidence_breakdown"] == {"exact": 4, "heuristic": 1}
    only_exact = await query(
        pipeline, "SELECT urn, confidence_level FROM symbols WHERE confidence_level = 'exact'"
    )
    assert only_exact.structured["_resolution_meta"]["confidence_level"] == "exact"
    assert only_exact.structured["_resolution_meta"]["mixed"] is False


async def test_t20_without_confidence_column_warns_when_the_tables_have_lower_rows(
    pipeline: Pipeline,
) -> None:
    r = await query(pipeline, "SELECT count(*) AS n FROM symbols")
    assert any("confidence_level" in w for w in r.structured["warnings"])
    clean = await query(pipeline, "SELECT 1 AS one")
    assert clean.structured["warnings"] == []
    exact_only = await query(pipeline, "SELECT count(*) AS n FROM workspaces")
    assert exact_only.structured["warnings"] == []


async def test_t20_explain_returns_the_plan_and_no_rows(pipeline: Pipeline) -> None:
    r = await query(pipeline, "SELECT * FROM symbols WHERE name = :n", params={"n": "f1"}, explain=True)
    data = r.structured["data"]
    assert data["rows"] == [] and data["row_count"] == 0
    assert data["plan"] and all({"id", "parent", "detail"} <= set(p) for p in data["plan"])
    c = await query(pipeline, "SELECT * FROM symbols", fmt="compact", explain=True)
    assert g.validate(c.text) == [] and "plan: " in c.text


@pytest.mark.parametrize("fmt", ["dot", "mermaid"])
async def test_t20_graph_needs_source_and_target_columns(pipeline: Pipeline, fmt: str) -> None:
    ok = await query(
        pipeline, "SELECT source_urn, target_urn, relation_kind, confidence_level FROM relations", fmt=fmt
    )
    assert not ok.is_error, ok.text
    assert "CALLS" in ok.text and ("digraph" in ok.text or "flowchart" in ok.text)
    bad = await query(pipeline, "SELECT source_urn FROM relations", fmt=fmt)
    assert bad.is_error and "E_INVALID_ARGUMENT" in bad.text


async def test_t20_graph_format_error_code_is_invalid_argument_in_json_path(pipeline: Pipeline) -> None:
    # dot / mermaid は json に出せないので、ハンドラの検査はエラーの描画（compact 経由）で確かめる
    bad = await query(pipeline, "SELECT name FROM symbols", fmt="mermaid")
    assert bad.is_error and "E_INVALID_ARGUMENT" in bad.text


async def test_t20_grep_fallback_makes_a_degraded_temp_table(pipeline: Pipeline) -> None:
    r = await query(
        pipeline,
        "SELECT path, line, snippet, confidence_level FROM temp_grep_matches ORDER BY path, line",
        auto_fallback_grep_pattern="needle_word",
    )
    assert r.structured["ok"], r.text
    data = r.structured["data"]
    assert [(row["path"], row["line"]) for row in data["rows"]] == [("a.py", 2), ("skip/b.py", 1)]
    assert data["grep_fallback"]["pattern"] == "needle_word" and data["grep_fallback"]["matches"] == 2
    assert data["grep_fallback"]["truncated"] is False
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "lexical_fallback" and meta["is_degraded"] is True
    scoped = await query(
        pipeline,
        "SELECT path FROM temp_grep_matches",
        auto_fallback_grep_pattern="needle_word",
        grep_scope_paths=["skip"],
    )
    assert [row["path"] for row in scoped.structured["data"]["rows"]] == ["skip/b.py"]
    outside = await query(
        pipeline,
        "SELECT path FROM temp_grep_matches",
        auto_fallback_grep_pattern="needle_word",
        grep_scope_paths=["../etc"],
    )
    assert outside.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    no_table = await query(pipeline, "SELECT path FROM temp_grep_matches")
    assert no_table.structured["error"]["code"] == "E_QUERY_REJECTED"  # 指定が無ければ一時テーブルは無い


async def test_t20_grep_scope_without_pattern_is_invalid(pipeline: Pipeline) -> None:
    r = await query(pipeline, "SELECT 1", grep_scope_paths=["a.py"])
    assert r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


async def test_t20_without_db_is_workspace_not_configured(tmp_path: Path) -> None:
    for db in (None, tmp_path / "missing.sqlite"):
        r = await query(make_pipeline(db), "SELECT 1")
        assert r.is_error and r.structured["error"]["code"] == "E_WORKSPACE_NOT_CONFIGURED"
