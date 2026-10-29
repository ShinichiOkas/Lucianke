"""index/public.py（公開クエリエンジン。仕様書 T-20・§13.2、設計書 §7.6.4）の単体テスト。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

import lucianke.index
from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import (
    GREP_MATCHES_MAX,
    QUERY_LENGTH_MAX,
    REGEX_PATTERN_MAX_CHARS,
    SNIPPET_MAX_CHARS,
)
from lucianke.index.public import (
    PUBLIC_TABLES,
    GrepSpec,
    QueryTimeout,
    check_statement,
    compile_grep_pattern,
    open_public,
    run_query,
    scan_grep,
)

SCHEMA = Path(lucianke.index.__file__).parent / "schema.sql"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """schema.sql から作った一時 DB。数行入っている。"""
    path = tmp_path / "paad.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO workspaces(workspace_id, root_path) VALUES ('w1', 'S:/ws')")
    for fid, p in ((1, "src/a.py"), (2, "src/b.py")):
        con.execute(
            "INSERT INTO files VALUES (?, 'w1', ?, 'python', 1, 10, 100, 'x', 0, NULL, NULL, '2026-01-01', 'exact')",
            (fid, p),
        )
    symbols = [
        ("lk://w1/src/a.py#Root", 1, None, "Root", "Root", "class", "exact"),
        ("lk://w1/src/a.py#Root.child", 1, "lk://w1/src/a.py#Root", "child", "Root.child", "method", "exact"),
        ("lk://w1/src/a.py#Root.child.leaf", 1, "lk://w1/src/a.py#Root.child", "leaf", "Root.child.leaf", "function", "heuristic"),
        ("lk://w1/src/b.py#helper", 2, None, "helper", "helper", "function", "lexical_fallback"),
    ]  # fmt: skip
    for urn, fid, parent, name, qname, kind, conf in symbols:
        con.execute(
            "INSERT INTO symbols(urn, file_id, parent_urn, name, qualified_name, kind, start_line, end_line,"
            " signature, doc_summary, confidence_level) VALUES (?, ?, ?, ?, ?, ?, 1, 5, ?, ?, ?)",
            (urn, fid, parent, name, qname, kind, f"def {name}()", f"does {name} work", conf),
        )
    for src, dst, kind, conf in (
        ("lk://w1/src/a.py#Root.child", "lk://w1/src/b.py#helper", "CALLS", "exact"),
        ("lk://w1/src/a.py#Root", "lk://w1/src/b.py#helper", "REFERENCES", "heuristic"),
        ("lk://w1/src/a.py#Root", "lk://w1/src/a.py#Root.child", "CALLS", "heuristic"),
    ):
        con.execute(
            "INSERT INTO relations(source_urn, target_urn, relation_kind, confidence_level) VALUES (?, ?, ?, ?)",
            (src, dst, kind, conf),
        )
    con.execute("INSERT INTO _meta(schema_version, created_at) VALUES (1, '2026-01-01')")
    con.commit()
    con.close()
    return path


def reason(exc: pytest.ExceptionInfo[LuciankeError]) -> str:
    return exc.value.details["reason"]


# --- 正常系 --------------------------------------------------------------------------------


def test_select_returns_columns_rows_and_declared_types(db: Path):
    r = run_query(db, "SELECT urn, name, start_line AS first, upper(name) AS shout FROM symbols ORDER BY urn")
    assert [c[0] for c in r.columns] == ["urn", "name", "first", "shout"]
    assert [c[1] for c in r.columns] == ["TEXT", "TEXT", None, None]  # 別名の列は名前が合わず None（近似）
    assert r.row_count == 4 == len(r.rows) and not r.truncated
    assert r.rows[0][0] == "lk://w1/src/a.py#Root"
    assert r.tables_read == ("symbols",)
    assert r.grep_fallback is None and r.plan is None


def test_with_and_recursive_cte(db: Path):
    r = run_query(
        db,
        """
        WITH RECURSIVE anc(urn, depth) AS (
          SELECT urn, 0 FROM symbols WHERE name = 'leaf'
          UNION ALL
          SELECT s.parent_urn, anc.depth + 1 FROM symbols s JOIN anc ON s.urn = anc.urn WHERE s.parent_urn IS NOT NULL
        )
        SELECT depth, urn FROM anc ORDER BY depth
        """,
    )
    assert [row[0] for row in r.rows] == [0, 1, 2]
    assert run_query(db, "WITH c AS (SELECT 1 AS x) SELECT count(*) FROM c").rows == [(1,)]


def test_count_star_and_trailing_semicolon_and_comments(db: Path):
    assert run_query(db, "/* c */ SELECT count(*) FROM symbols; -- end").rows == [(4,)]
    assert run_query(db, "-- lead\nSELECT 1").rows == [(1,)]


def test_fts5_match_and_json_functions(db: Path):
    r = run_query(
        db, "SELECT name FROM symbols_fts WHERE symbols_fts MATCH :q ORDER BY name", params={"q": "helper"}
    )
    assert r.rows == [("helper",)]
    r = run_query(
        db, "SELECT json_extract('{\"a\": [1, 2]}', '$.a[1]'), (SELECT count(*) FROM json_each('[1,2,3]'))"
    )
    assert r.rows == [(2, 3)]


def test_quoted_forbidden_words_are_data_not_keywords(db: Path):
    assert run_query(
        db, "SELECT count(*) FROM symbols WHERE name = 'PRAGMA' OR doc_summary LIKE '%attach%'"
    ).rows == [(0,)]


def test_join_across_public_tables(db: Path):
    r = run_query(
        db,
        "SELECT f.path, count(*) FROM symbols s JOIN files f ON f.file_id = s.file_id GROUP BY f.path ORDER BY f.path",
    )
    assert r.rows == [("src/a.py", 3), ("src/b.py", 1)]
    assert set(r.tables_read) == {"symbols", "files"}


# --- limit / truncated / params / explain ---------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "count", "truncated"), [(1, 1, True), (3, 3, True), (4, 4, False), (10, 4, False)]
)
def test_limit_and_truncated(db: Path, limit: int, count: int, truncated: bool):
    r = run_query(db, "SELECT urn FROM symbols ORDER BY urn", limit=limit)
    assert r.row_count == count == len(r.rows)
    assert r.truncated is truncated


@pytest.mark.parametrize("limit", [0, -1, 2001])
def test_limit_out_of_range(db: Path, limit: int):
    with pytest.raises(LuciankeError) as exc:
        run_query(db, "SELECT 1", limit=limit)
    assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT


def test_timeout_ms_out_of_range(db: Path):
    for bad in (0, 5001):
        with pytest.raises(LuciankeError) as exc:
            run_query(db, "SELECT 1", timeout_ms=bad)
        assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT


def test_params_bind_by_name(db: Path):
    r = run_query(
        db, "SELECT name FROM symbols WHERE kind = :k AND name != :k2", params={"k": "function", "k2": "leaf"}
    )
    assert r.rows == [("helper",)]
    assert run_query(db, "SELECT :n IS NULL", params={"n": None}).rows == [(1,)]


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("SELECT :a", {"a": 1, "extra": 2}),  # params に余分なキー
        ("SELECT :a, :b", {"a": 1}),  # params に無いバインド名
        ("SELECT :a", None),
        ("SELECT ?", {"a": 1}),  # 位置指定は使えない
        ("SELECT :a", {"a": [1, 2]}),  # 束縛できない型
        ("SELECT :a", {"a": 2**70}),
    ],
)
def test_params_mismatch_is_invalid_argument(db: Path, sql: str, params):
    with pytest.raises(LuciankeError) as exc:
        run_query(db, sql, params=params)
    assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT


def test_binding_names_inside_strings_and_comments_are_ignored(db: Path):
    assert run_query(db, "SELECT ':nope', 1 /* :nope */", params={}).rows == [(":nope", 1)]


def test_explain_returns_plan_and_no_rows(db: Path):
    r = run_query(db, "SELECT * FROM symbols WHERE name = :n", params={"n": "leaf"}, explain=True)
    assert r.rows == [] and r.row_count == 0 and not r.truncated
    assert r.plan and all({"id", "parent", "detail"} <= set(p) for p in r.plan)
    assert any("idx_symbols_name" in p["detail"] for p in r.plan)


def test_explain_still_rejects_non_public(db: Path):
    with pytest.raises(LuciankeError) as exc:
        run_query(db, "SELECT * FROM _meta", explain=True)
    assert exc.value.code is ErrorCode.E_QUERY_REJECTED


def test_confidence_breakdown(db: Path):
    r = run_query(db, "SELECT urn, confidence_level FROM symbols")
    assert r.confidence_breakdown == {"exact": 2, "heuristic": 1, "lexical_fallback": 1}
    assert list(r.confidence_breakdown) == ["exact", "heuristic", "lexical_fallback"]  # 水準の高い順
    assert run_query(db, "SELECT urn FROM symbols").confidence_breakdown is None
    # limit で切った行は数えない（返した行の分だけ）
    assert run_query(
        db, "SELECT confidence_level FROM symbols ORDER BY urn", limit=1
    ).confidence_breakdown == {"exact": 1}


# --- 静的検査と authorizer ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1; SELECT 2", "multiple_statements"),
        ("SELECT 1;;", "multiple_statements"),
        ("SELECT 1; -- x\n DROP TABLE symbols", "multiple_statements"),
        ("INSERT INTO symbols(urn) VALUES ('x')", "not_select"),
        ("DELETE FROM symbols", "not_select"),
        ("EXPLAIN SELECT 1", "not_select"),
        ("PRAGMA query_only = OFF", "not_select"),
        ("", "not_select"),
        ("-- only a comment", "not_select"),
        ("SELECT 1 FROM symbols WHERE 1; ATTACH DATABASE 'x' AS y", "multiple_statements"),
        ("SELECT load_extension('x')", "forbidden_keyword"),
        ("SELECT readfile('/etc/passwd')", "forbidden_keyword"),
        ("SELECT writefile('/tmp/x', 'y')", "forbidden_keyword"),
        ("WITH c AS (SELECT 1) SELECT * FROM c, (SELECT 1 AS pragma)", "forbidden_keyword"),
        ("SELECT * FROM _meta", "non_public_object"),
        ("SELECT count(*) FROM _meta", "non_public_object"),
        ("SELECT * FROM _hover_cache", "non_public_object"),
        ("SELECT sql FROM sqlite_master", "non_public_object"),
        ("SELECT * FROM pragma_table_info('symbols')", "non_public_object"),
        ("SELECT count(*) FROM pragma_table_info('symbols')", "non_public_object"),
        ("SELECT * FROM no_such_table", "non_public_object"),
        ("SELECT * FROM temp_grep_matches", "non_public_object"),  # grep 指定が無ければ存在しない
        ("WITH c(x) AS (SELECT 1) INSERT INTO workspaces(workspace_id, root_path) SELECT 'z', 'z' FROM c", "forbidden_keyword"),
        ("SELECT FROM", "syntax_error"),
        ("SELECT nope FROM symbols", "syntax_error"),
        ("SELECT nope_fn(1)", "syntax_error"),
    ],
)  # fmt: skip
def test_rejections(db: Path, sql: str, expected: str):
    with pytest.raises(LuciankeError) as exc:
        run_query(db, sql)
    assert exc.value.code is ErrorCode.E_QUERY_REJECTED
    assert reason(exc) == expected


def test_syntax_error_carries_sqlite_message(db: Path):
    with pytest.raises(LuciankeError) as exc:
        run_query(db, "SELECT FROM")
    assert "syntax error" in exc.value.details["sqlite_message"]


def test_query_too_long_is_invalid_argument(db: Path):
    with pytest.raises(LuciankeError) as exc:
        run_query(db, "SELECT '" + "x" * QUERY_LENGTH_MAX + "'")
    assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT


def test_check_statement_accepts_and_rejects():
    check_statement("select 1")
    check_statement("  WITH x AS (SELECT 1) SELECT * FROM x ;  ")
    with pytest.raises(LuciankeError):
        check_statement("update symbols set name = 'x'")


def test_public_tables_cover_schema_public_tables():
    expected = {
        "workspaces", "files", "symbols", "relations", "dataflows", "diagnostics", "contracts",
        "git_commits", "git_symbol_changes", "git_co_changes", "symbols_fts", "temp_grep_matches",
    }  # fmt: skip
    assert expected == PUBLIC_TABLES
    assert len(PUBLIC_TABLES) == 12


def test_connection_is_read_only_at_every_layer(db: Path):
    con = open_public(db)
    try:
        assert con.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.DatabaseError):
            con.execute("PRAGMA query_only = OFF")  # authorizer が値の設定を拒否する
        assert con.guard.denied is not None
        with pytest.raises(sqlite3.DatabaseError):
            con.execute("DELETE FROM symbols")
    finally:
        con.close()


def test_missing_database_is_internal_error(tmp_path: Path):
    with pytest.raises(LuciankeError) as exc:
        run_query(tmp_path / "nope.sqlite", "SELECT 1")
    assert exc.value.code is ErrorCode.E_INTERNAL


def test_path_with_spaces_and_japanese(db: Path, tmp_path: Path):
    odd = tmp_path / "空白 と #記号"
    odd.mkdir()
    target = odd / "索引 1.sqlite"
    target.write_bytes(db.read_bytes())
    assert run_query(target, "SELECT count(*) FROM symbols").rows == [(4,)]


# --- タイムアウト ---------------------------------------------------------------------------

INFINITE = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT count(*) FROM c"


def test_infinite_recursive_cte_times_out_within_a_second(db: Path):
    t0 = time.perf_counter()
    with pytest.raises(QueryTimeout) as exc:
        run_query(db, INFINITE, timeout_ms=300)
    assert time.perf_counter() - t0 < 1.0
    assert exc.value.code is ErrorCode.E_TIMEOUT
    assert exc.value.details["partial"] is True
    assert exc.value.result.rows == [] and not exc.value.result.truncated


def test_timeout_keeps_rows_fetched_so_far(db: Path):
    # 行が出てから、いつまでも次の行が出ない
    sql = (
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) "
        "SELECT n FROM c WHERE n IN (1, 2) OR n < 0"
    )
    with pytest.raises(QueryTimeout) as exc:
        run_query(db, sql, timeout_ms=200, limit=5)
    # Python の sqlite3 は 1 行先読みするので、中断した fetch が返すはずだった最後の 1 行は失われうる
    got = exc.value.result.rows
    assert got and got == [(1,), (2,)][: len(got)]
    assert exc.value.result.row_count == len(got)


def test_connection_is_reusable_after_interrupt(db: Path):
    con = open_public(db)
    try:
        con.guard.deadline = time.perf_counter() + 0.1
        with pytest.raises(sqlite3.OperationalError):
            con.execute(INFINITE).fetchall()
        assert con.guard.timed_out
        con.guard.deadline = float("inf")
        assert con.execute("SELECT count(*) FROM symbols").fetchone() == (4,)
    finally:
        con.close()


def test_elapsed_ms_never_exceeds_timeout_ms(db: Path):
    r = run_query(db, "SELECT count(*) FROM symbols", timeout_ms=50)
    assert r.elapsed_ms <= 50


def test_huge_value_is_rejected_fast(db: Path):
    t0 = time.perf_counter()
    with pytest.raises(LuciankeError) as exc:
        run_query(db, "SELECT length(zeroblob(2000000000))")
    assert exc.value.code is ErrorCode.E_QUERY_REJECTED
    assert time.perf_counter() - t0 < 1.0


# --- grep の一時テーブル --------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "src" / "a.py").write_text(
        "def helper():\n    return 1\n\nx = helper()\nhelper_two = 3\n", encoding="utf-8"
    )
    (root / "src" / "pkg" / "b.py").write_text("# call helper here\r\nprint('no')\r\n", encoding="utf-8")
    (root / "src" / "bin.dat").write_bytes(b"helper\x00helper")
    (root / "node_modules" / "dep" / "c.js").write_text("helper()\n", encoding="utf-8")
    (root / ".git" / "config").write_text("helper\n", encoding="utf-8")
    return root


def test_grep_temp_table_joins_with_public_tables(db: Path, ws: Path):
    r = run_query(
        db,
        """
        SELECT g.path, g.line, g.snippet, g.confidence_level, s.urn
        FROM temp_grep_matches g
        LEFT JOIN files f ON f.path = g.path
        LEFT JOIN symbols s ON s.file_id = f.file_id AND s.name = 'Root'
        ORDER BY g.path, g.line
        """,
        grep=GrepSpec("helper", [ws], None, lambda p: p.name == "node_modules"),
    )
    assert [(row[0], row[1]) for row in r.rows] == [("src/a.py", 1), ("src/a.py", 4), ("src/pkg/b.py", 1)]
    assert r.rows[0][2] == "def helper():" and r.rows[2][2] == "# call helper here"
    assert {row[3] for row in r.rows} == {"lexical_fallback"}
    assert r.rows[0][4] == "lk://w1/src/a.py#Root"  # src/a.py は files にあり、JOIN できる
    assert r.grep_fallback == {"pattern": "helper", "matches": 3, "truncated": False, "scope_paths": None}
    assert r.confidence_breakdown == {"lexical_fallback": 3}
    assert "temp_grep_matches" in r.tables_read
    assert dict(r.columns)["line"] == "INTEGER" and dict(r.columns)["path"] == "TEXT"


def test_grep_table_exists_even_with_zero_matches_and_is_read_only(db: Path, ws: Path):
    spec = GrepSpec("zzz_nothing", [ws])
    r = run_query(db, "SELECT count(*) FROM temp_grep_matches", grep=spec)
    assert r.rows == [(0,)]
    assert r.grep_fallback is not None and r.grep_fallback["matches"] == 0
    for sql in (
        "DELETE FROM temp_grep_matches",
        "INSERT INTO temp_grep_matches VALUES ('a', 1, 'b', 'exact')",
    ):
        with pytest.raises(LuciankeError) as exc:
            run_query(db, sql, grep=spec)
        assert exc.value.code is ErrorCode.E_QUERY_REJECTED
    # 一時テーブルは接続ローカル: 次の呼び出しには残らない
    with pytest.raises(LuciankeError):
        run_query(db, "SELECT count(*) FROM temp_grep_matches")


def test_grep_scope_paths_and_word_boundary(ws: Path):
    scan = scan_grep(GrepSpec("helper", [ws], ["src/pkg"]))
    assert scan.rows == [("src/pkg/b.py", 1, "# call helper here")]
    assert scan.scope_paths == ["src/pkg"]
    scan = scan_grep(GrepSpec("helper", [ws], ["src/a.py"]))
    assert [r[1] for r in scan.rows] == [1, 4]  # helper_two は単語境界で外れる
    assert scan_grep(GrepSpec("helper", [ws], ["does/not/exist"])).rows == []


@pytest.mark.parametrize(
    "scope", ["../outside", "src/../../outside", "..", "/definitely/not/here", "C:/Windows"]
)
def test_grep_scope_outside_roots_is_invalid_argument(ws: Path, scope: str):
    with pytest.raises(LuciankeError) as exc:
        scan_grep(GrepSpec("helper", [ws], [scope]))
    assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT


def test_grep_scope_absolute_inside_root_is_accepted(ws: Path):
    scan = scan_grep(GrepSpec("helper", [ws], [str(ws / "src" / "pkg")]))
    assert [r[0] for r in scan.rows] == ["src/pkg/b.py"]


def test_grep_skips_binary_git_and_symlinks(ws: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("helper\n", encoding="utf-8")
    link = ws / "src" / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted")
    paths = {r[0] for r in scan_grep(GrepSpec("helper", [ws])).rows}
    assert paths == {
        "src/a.py",
        "src/pkg/b.py",
        "node_modules/dep/c.js",
    }  # bin.dat・.git・link.txt は読まない


def test_grep_snippet_is_capped_and_centered_on_match(ws: Path):
    (ws / "long.txt").write_text("a" * 500 + " needle " + "b" * 500 + "\n", encoding="utf-8")
    (ws / "short.txt").write_text("   padded needle   \n", encoding="utf-8")
    rows = {r[0]: r for r in scan_grep(GrepSpec("needle", [ws])).rows}
    assert len(rows["long.txt"][2]) <= SNIPPET_MAX_CHARS
    assert (
        "needle" in rows["long.txt"][2]
        and rows["long.txt"][2].startswith("…")
        and rows["long.txt"][2].endswith("…")
    )
    assert rows["short.txt"][2] == "padded needle"


def test_grep_matches_capped_with_truncated_flag(ws: Path):
    (ws / "many.txt").write_text("hit\n" * (GREP_MATCHES_MAX + 5), encoding="utf-8")
    scan = scan_grep(GrepSpec("hit", [ws]))
    assert len(scan.rows) == GREP_MATCHES_MAX and scan.truncated
    (ws / "many.txt").write_text("hit\n" * GREP_MATCHES_MAX, encoding="utf-8")
    scan = scan_grep(GrepSpec("hit", [ws]))
    assert len(scan.rows) == GREP_MATCHES_MAX and not scan.truncated  # ちょうど上限は切っていない


def test_grep_results_are_deterministic(ws: Path):
    first = scan_grep(GrepSpec("helper", [ws])).rows
    assert first == scan_grep(GrepSpec("helper", [ws])).rows
    assert [r[0] for r in first] == sorted(r[0] for r in first)


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "x" * (REGEX_PATTERN_MAX_CHARS + 1),
        "(unclosed",
        "(a+)+$",
        "(a*)*b",
        "(\\w+\\s*)+",
        "(?:a|b+)*c",
        "(a{1,5})+",
        "[z-a]",
    ],
)
def test_grep_bad_patterns_are_invalid_argument(pattern: str):
    with pytest.raises(LuciankeError) as exc:
        compile_grep_pattern(pattern)
    assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT


@pytest.mark.parametrize(
    "pattern", ["foo|bar", r"\w+_id", r"(?:get|set)_\w+", r"(\d{1,3}\.){3}\d{1,3}", "(?i)FOO", "a{2,5}"]
)
def test_grep_reasonable_patterns_compile(pattern: str):
    compile_grep_pattern(pattern)


def test_grep_pattern_limits_apply_through_run_query(db: Path, ws: Path):
    with pytest.raises(LuciankeError) as exc:
        run_query(db, "SELECT 1", grep=GrepSpec("(a+)+$", [ws]))
    assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT


def test_grep_inline_flag_and_word_boundary_semantics():
    assert compile_grep_pattern("(?i)foo").search("a FOO b")
    assert not compile_grep_pattern("foo").search("foobar")
    assert compile_grep_pattern("foo").search("x.foo(")
    assert compile_grep_pattern("->").search("a ->  b")


def test_grep_exclude_receives_absolute_paths_for_dirs_and_files(ws: Path):
    seen: list[Path] = []

    def exclude(p: Path) -> bool:
        seen.append(p)
        return p.name == "pkg"

    scan = scan_grep(GrepSpec("helper", [ws], None, exclude))
    assert all(p.is_absolute() for p in seen)
    assert any(p.name == "pkg" for p in seen)
    assert "src/pkg/b.py" not in {r[0] for r in scan.rows}
