"""BUG-0027: T-03 の検索が、ヒットが散らばるワークスペースでも問い合わせを増やさず、旧い結合と同じ並びを返す。

速さの絶対値は負荷で揺れるので、時間の閾値では確かめない。**SQL 文の数**（ファイルごとに引かない）、**行表を作らない**
（単語が入っていないファイルの grep）、**結果の同一性**（書き直した fuzzy 検索が、旧い ``LEFT JOIN hits`` と同じ行を同じ順で返す）で確かめる。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

import lucianke.index
from lucianke.index import queries
from lucianke.syntax import heuristic
from lucianke.tools import t03_search_workspace_symbols as t03

SCHEMA = Path(lucianke.index.__file__).parent / "schema.sql"
WS = "w1"
N_FILES = 40


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """40 ファイル × 3 シンボルの索引。名前 ``handler_i`` は全ファイルに散らばる。"""
    con = sqlite3.connect(tmp_path / "paad.sqlite")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO workspaces(workspace_id, root_path) VALUES (?, 'S:/ws')", (WS,))
    for fid in range(1, N_FILES + 1):
        con.execute(
            "INSERT INTO files VALUES (?, ?, ?, 'python', 1, 10, 100, 'x', ?, NULL, NULL, '2026-01-01', 'exact')",
            (fid, WS, f"src/pkg{fid}/mod.py", int(fid % 7 == 0)),
        )
        for name, kind, doc in (
            (f"handler_{fid}", "function", "handles a request"),
            ("Handler", "class", f"base handler number {fid}"),
            (f"helper_{fid}", "function", "unrelated"),
        ):
            con.execute(
                "INSERT INTO symbols(urn, file_id, parent_urn, name, qualified_name, kind, start_line, end_line,"
                " signature, doc_summary, confidence_level) VALUES (?, ?, NULL, ?, ?, ?, 1, 5, ?, ?, 'exact')",
                (f"lk://{WS}/src/pkg{fid}/mod.py#{name}", fid, name, name, kind, f"def {name}()", doc),
            )
    con.commit()
    yield con
    con.close()


def test_file_infos_are_fetched_in_one_query(conn: sqlite3.Connection) -> None:
    paths = [f"src/pkg{i}/mod.py" for i in range(1, N_FILES + 1)] + ["no/such.py"]
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    infos = queries.files_by_paths(conn, WS, paths)
    conn.set_trace_callback(None)
    assert len(statements) == 1
    assert set(infos) == set(paths) - {"no/such.py"}
    for p in ("src/pkg1/mod.py", f"src/pkg{N_FILES}/mod.py"):
        assert infos[p] == queries.file_by_path(conn, WS, p)


def test_files_by_paths_chunks_long_lists(conn: sqlite3.Connection) -> None:
    many = [f"src/pkg{i}/mod.py" for i in range(1, N_FILES + 1)] + [f"x/{i}.py" for i in range(1200)]
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    infos = queries.files_by_paths(conn, WS, many)
    conn.set_trace_callback(None)
    assert len(infos) == N_FILES
    assert len(statements) == 3  # 1,240 件 = 500 + 500 + 240（SQLite の変数の上限に当たらない）


def test_search_issues_a_bounded_number_of_statements(conn: sqlite3.Connection) -> None:
    """ヒットが 40 ファイルに散らばっても、検索の SQL はファイル数に比例しない。"""
    args = t03.SearchWorkspaceSymbolsArgs(query="handler")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    res = t03._search_index(conn, WS, args, "handler", None, None, None)
    conn.set_trace_callback(None)
    paths = {str(h.sym.path) for h in res.hits}
    assert len(paths) > 20  # 散らばっている
    issued = [
        st for st in statements if not st.startswith("--")
    ]  # FTS5 が内部で出す文（コメント行で記録される）は数えない
    assert len(issued) <= 3, issued


def _old_fuzzy(conn: sqlite3.Connection, query: str, kinds: list[str] | None, limit: int) -> list[str]:
    """書き直す前の fuzzy 検索（``LEFT JOIN hits``）。結果の同一性の基準。"""
    tokens = queries._fts_tokens(query)
    kind_sql, kind_params = queries._kind_clause(kinds)
    hits = (
        "SELECT rowid AS rid, bm25(symbols_fts, 10.0, 5.0, 2.0, 1.0) AS score FROM symbols_fts"
        " WHERE symbols_fts MATCH :fts"
        if tokens
        else "SELECT NULL AS rid, NULL AS score WHERE 0"
    )
    sql = (
        f"WITH hits AS ({hits}) "
        + queries._SYMBOL_SELECT
        + " LEFT JOIN hits h ON h.rid = s.rowid WHERE f.workspace_id = :ws"
        " AND (h.rid IS NOT NULL OR instr(lower(s.name), lower(:q)) > 0)"
        + kind_sql
        + " ORDER BY (s.name = :q) DESC, (lower(s.name) = lower(:q)) DESC, (h.score IS NULL), h.score,"
        " length(s.name), s.qualified_name, s.urn LIMIT :limit"
    )
    params: dict[str, object] = {"ws": WS, "q": query, "limit": limit, **kind_params}
    if tokens:
        params["fts"] = " AND ".join(queries._fts_phrase(t) for t in tokens)
    return [str(queries._symbol(r).urn) for r in conn.execute(sql, params).fetchall()]


@pytest.mark.parametrize(
    "query", ["handler", "Handler", "handler_7", "hand", "helper", "request", "number 3", "h", "zzz", "_"]
)
@pytest.mark.parametrize("kinds", [None, ["function"], ["class"]])
@pytest.mark.parametrize("limit", [2000, 7])
def test_fuzzy_search_matches_the_old_join(
    conn: sqlite3.Connection, query: str, kinds: list[str] | None, limit: int
) -> None:
    new = [str(s.urn) for s in queries.search_symbols_fts(conn, WS, query, "fuzzy", kinds, limit=limit)]
    assert new == _old_fuzzy(conn, query, kinds, limit)


def test_lexical_scan_skips_the_line_table_when_the_word_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[int] = []
    real = heuristic.LineIndex

    def counting(source: bytes) -> object:
        built.append(len(source))
        return real(source)

    monkeypatch.setattr(heuristic, "LineIndex", counting)
    text = ("line of prose without the word\n" * 500).encode()
    assert heuristic.lexical_scan(text, "needle").hits == []
    assert built == []  # 入っていないなら行表を作らない
    found = heuristic.lexical_scan(text + b"def needle():\n    pass\n", "needle")
    assert [h.line for h in found.hits] == [500] and found.hits[0].is_definition
    assert len(built) == 1
