"""不変条件: ``query_project_db`` の実行エンジンは PaaD を変えない（仕様書 T-20 事後条件 1・2、§13.2、設計書 §7.6.4）。

どの書き込み文・複文・内部テーブルの参照・``load_extension`` も ``E_QUERY_REJECTED`` で返り、
**DB ファイルの SHA-256・各テーブルの行数・スキーマ・``user_version``・ディレクトリの内容が呼び出しの前後で変わらない**。

検査器が本当に変化を検出できることは陽性対照で確かめる: 同じ文を通常の書き込み接続で流すと、スナップショットが変わる。
さらに「3 重のうち 1 つを外しても書き込み文が通らない」ことを層別に確かめる（S-7）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import lucianke.index
from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.index.public import ALL_LAYERS, GrepSpec, open_public, run_query

SCHEMA = Path(lucianke.index.__file__).parent / "schema.sql"

INSERT_SYMBOL = (
    "INSERT INTO symbols(urn, name, qualified_name, kind, start_line, end_line, confidence_level)"
    " VALUES ('lk://w/x.py#evil', 'evil', 'evil', 'function', 1, 1, 'exact')"
)
WITH_INSERT = (
    "WITH c(x) AS (SELECT 1) INSERT INTO workspaces(workspace_id, root_path) SELECT 'w9', 'r9' FROM c"
)
WITH_DELETE = "WITH c(x) AS (SELECT 1) DELETE FROM symbols WHERE EXISTS (SELECT 1 FROM c)"


def _build_template(path: Path, journal_mode: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO workspaces(workspace_id, root_path) VALUES ('w1', 'S:/ws')")
    con.execute(
        "INSERT INTO files VALUES (1, 'w1', 'src/a.py', 'python', 1, 10, 100, 'x', 0, NULL, NULL, '2026-01-01', 'exact')"
    )
    for n in range(20):
        con.execute(
            "INSERT INTO symbols(urn, file_id, name, qualified_name, kind, start_line, end_line, doc_summary,"
            " confidence_level) VALUES (?, 1, ?, ?, 'function', 1, 2, 'doc', 'exact')",
            (f"lk://w1/src/a.py#f{n}", f"f{n}", f"f{n}"),
        )
    con.execute(
        "INSERT INTO relations(source_urn, target_urn, relation_kind, confidence_level)"
        " VALUES ('lk://w1/src/a.py#f1', 'lk://w1/src/a.py#f2', 'CALLS', 'exact')"
    )
    con.execute("INSERT INTO _meta(schema_version, created_at) VALUES (1, '2026-01-01')")
    con.execute("INSERT INTO _hover_cache VALUES ('lk://w1/src/a.py#f1', 'h', 'hover', '2026-01-01')")
    con.commit()
    if journal_mode == "wal":
        con.execute("PRAGMA journal_mode = WAL")
    con.close()


@pytest.fixture(scope="module", params=["delete", "wal"])
def template(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> tuple[bytes, str]:
    path = tmp_path_factory.mktemp("tpl") / "t.sqlite"
    _build_template(path, request.param)
    return path.read_bytes(), request.param


@pytest.fixture
def db(template: tuple[bytes, str], tmp_path: Path) -> Path:
    """テストごとに作り直す DB（専用ディレクトリ）。journal_mode は delete と wal の両方で回す。"""
    d = tmp_path / "db"
    d.mkdir()
    path = d / "paad.sqlite"
    path.write_bytes(template[0])
    return path


def _count(con: sqlite3.Connection, table: str) -> int | str:
    try:
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error as exc:  # 内容表を消された FTS5 など。読めなくなったこと自体が変化
        return f"error: {exc}"


def snapshot(db: Path) -> dict[str, Any]:
    """DB の外から見えるすべて: ファイルの SHA-256、各テーブルの行数、スキーマ、user_version、ディレクトリの内容。

    ``-wal`` / ``-shm`` は読み手が作るので数えない（本体のハッシュで見る）。
    """
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        names = [
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        ]
        counts = {n: _count(con, n) for n in names}
        schema = con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        user_version = con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
    listing = sorted(
        (p.name, p.stat().st_size) for p in db.parent.iterdir() if not p.name.endswith(("-wal", "-shm"))
    )
    return {
        "sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
        "counts": counts,
        "schema": schema,
        "user_version": user_version,
        "listing": listing,
    }


@dataclass(frozen=True)
class Case:
    sql: Callable[[Path], str]
    reasons: frozenset[str]
    mutates: bool  # 通常の書き込み接続で流すとスナップショットが変わる文（陽性対照の対象）


def _c(sql: str | Callable[[Path], str], *reasons: str, mutates: bool = False) -> Case:
    return Case(sql if callable(sql) else (lambda _d: sql), frozenset(reasons), mutates)


CASES: dict[str, Case] = {
    "insert": _c(INSERT_SYMBOL, "not_select", mutates=True),
    "update": _c("UPDATE symbols SET name = 'hacked'", "not_select", mutates=True),
    "delete": _c("DELETE FROM symbols", "not_select", mutates=True),
    "create": _c("CREATE TABLE evil(x)", "not_select", mutates=True),
    "create_index": _c("CREATE INDEX evil_idx ON symbols(kind)", "not_select", mutates=True),
    "drop": _c("DROP TABLE relations", "not_select", mutates=True),
    "alter": _c("ALTER TABLE symbols ADD COLUMN evil TEXT", "not_select", mutates=True),
    "attach": _c(
        lambda d: f"ATTACH DATABASE '{(d / 'evil.db').as_posix()}' AS evil", "not_select", mutates=True
    ),
    "detach": _c("DETACH DATABASE evil", "not_select"),
    "pragma_user_version": _c("PRAGMA user_version = 7", "not_select", mutates=True),
    "pragma_query_only_off": _c("PRAGMA query_only = OFF", "not_select"),
    "vacuum": _c("VACUUM", "not_select"),
    "fts_delete_all": _c(
        "INSERT INTO symbols_fts(symbols_fts) VALUES('delete-all')", "not_select", mutates=True
    ),
    "multi_delete": _c("SELECT 1; DELETE FROM symbols", "multiple_statements", mutates=True),
    "multi_drop": _c("SELECT 1; DROP TABLE symbols;", "multiple_statements", mutates=True),
    "multi_attach": _c(
        lambda d: f"SELECT 1; ATTACH DATABASE '{(d / 'evil2.db').as_posix()}' AS e",
        "multiple_statements",
        mutates=True,
    ),
    "double_semicolon": _c("SELECT 1;;", "multiple_statements"),
    # 静的検査を通り、authorizer が止めるもの
    "with_insert": _c(WITH_INSERT, "forbidden_keyword", mutates=True),
    "with_delete": _c(WITH_DELETE, "forbidden_keyword", mutates=True),
    # 内部テーブルと禁止関数
    "meta": _c("SELECT * FROM _meta", "non_public_object"),
    "meta_count": _c("SELECT count(*) FROM _meta", "non_public_object"),
    "meta_subquery": _c("SELECT (SELECT max(schema_version) FROM _meta)", "non_public_object"),
    "hover_cache": _c("SELECT * FROM _hover_cache", "non_public_object"),
    "sqlite_master": _c("SELECT sql FROM sqlite_master", "non_public_object"),
    "load_extension": _c("SELECT load_extension('nonexistent')", "forbidden_keyword"),
    "writefile": _c("SELECT writefile('x.txt', 'y')", "forbidden_keyword"),
    "readfile": _c("SELECT readfile('paad.sqlite')", "forbidden_keyword"),
}


@pytest.mark.parametrize("name", CASES)
def test_rejected_and_database_unchanged(db: Path, name: str):
    case = CASES[name]
    before = snapshot(db)
    with pytest.raises(LuciankeError) as exc:
        run_query(db, case.sql(db.parent))
    assert exc.value.code is ErrorCode.E_QUERY_REJECTED
    assert exc.value.details["reason"] in case.reasons
    assert snapshot(db) == before


@pytest.mark.parametrize("name", [n for n, c in CASES.items() if c.mutates])
def test_positive_control_same_statement_changes_database_on_a_normal_connection(db: Path, name: str):
    """陽性対照: 通常の書き込み接続なら同じ文で DB が変わる。スナップショットが本当に変化を検出できることを示す。"""
    before = snapshot(db)
    con = sqlite3.connect(db)
    try:
        con.executescript(CASES[name].sql(db.parent))  # 複文も流せる
        con.commit()
    finally:
        con.close()
    assert snapshot(db) != before, f"snapshot did not notice {name}"


def test_positive_control_internal_tables_are_readable_on_a_normal_connection(db: Path):
    """``_meta`` の拒否は authorizer の働きである（表は存在し、通常の接続では読める）。"""
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT count(*) FROM _meta").fetchone() == (1,)
    finally:
        con.close()
    with pytest.raises(LuciankeError) as exc:
        run_query(db, "SELECT count(*) FROM _meta")
    assert exc.value.details["reason"] == "non_public_object"


def test_snapshot_detects_each_kind_of_change(db: Path):
    """スナップショットの各成分が個別に効いている（行数だけ・ハッシュだけ・ファイル追加だけ）。"""
    base = snapshot(db)

    def changed(sql: str) -> dict[str, Any]:
        con = sqlite3.connect(db)
        con.execute(sql)
        con.commit()
        con.close()
        return snapshot(db)

    after_insert = changed(INSERT_SYMBOL)
    assert after_insert["counts"]["symbols"] == base["counts"]["symbols"] + 1
    after_update = changed("UPDATE files SET line_count = 99")
    assert (
        after_update["counts"] == after_insert["counts"] and after_update["sha256"] != after_insert["sha256"]
    )
    (db.parent / "stray.db").write_bytes(b"")
    assert snapshot(db)["listing"] != after_update["listing"]


# --- 正常なクエリも DB を変えない ---------------------------------------------------------------


def test_legitimate_queries_leave_database_unchanged(db: Path, tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("def f1(): pass\n", encoding="utf-8")
    before = snapshot(db)
    run_query(db, "SELECT * FROM symbols")
    run_query(db, "SELECT name FROM symbols_fts WHERE symbols_fts MATCH 'f1'")
    run_query(db, "SELECT * FROM symbols", explain=True)
    run_query(db, "SELECT * FROM temp_grep_matches", grep=GrepSpec("f1", [ws]))
    run_query(db, "SELECT json_group_array(name) FROM symbols")
    assert snapshot(db) == before


def test_database_usable_after_rejections(db: Path):
    for name in ("delete", "meta", "multi_drop", "with_delete"):
        with pytest.raises(LuciankeError):
            run_query(db, CASES[name].sql(db.parent))
    assert run_query(db, "SELECT count(*) FROM symbols").rows == [(20,)]


# --- 3 重のうち 1 つを外しても DB 本体は書き換えられない（S-7 項目 2） ----------------------------

RAW_WRITES = {
    "insert": INSERT_SYMBOL,
    "update": "UPDATE symbols SET name = 'hacked'",
    "delete": "DELETE FROM symbols",
    "create": "CREATE TABLE evil(x)",
    "create_index": "CREATE INDEX evil_idx ON symbols(kind)",
    "drop": "DROP TABLE relations",
    "alter": "ALTER TABLE symbols ADD COLUMN evil TEXT",
    "with_insert": WITH_INSERT,
    "with_delete": WITH_DELETE,
    "fts_delete_all": "INSERT INTO symbols_fts(symbols_fts) VALUES('delete-all')",
    "fts_rebuild": "INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')",
    "insert_returning": INSERT_SYMBOL + " RETURNING urn",
    "vacuum": "VACUUM",
    "pragma_user_version": "PRAGMA user_version = 7",
    "pragma_journal_delete": "PRAGMA journal_mode = DELETE",
}


NOOP_IN_DELETE_MODE = {"pragma_journal_delete"}


@pytest.mark.parametrize("sql_name", RAW_WRITES)
@pytest.mark.parametrize("missing", sorted(ALL_LAYERS))
def test_any_single_layer_removed_still_blocks_database_writes(db: Path, missing: str, sql_name: str):
    before = snapshot(db)
    con = open_public(db, layers=ALL_LAYERS - {missing})
    try:
        if sql_name in NOOP_IN_DELETE_MODE:
            # 既に delete モードの DB では何も変えずに成功してよい。変わらないことだけ見る
            try:
                con.execute(RAW_WRITES[sql_name]).fetchall()
            except sqlite3.Error:
                pass
        else:
            with pytest.raises(sqlite3.Error):
                con.execute(RAW_WRITES[sql_name]).fetchall()
    finally:
        con.close()
    assert snapshot(db) == before


@pytest.mark.parametrize("missing", ["ro", "query_only"])
def test_attach_is_blocked_when_authorizer_is_present(db: Path, missing: str):
    target = db.parent / "evil.db"
    con = open_public(db, layers=ALL_LAYERS - {missing})
    try:
        with pytest.raises(sqlite3.Error):
            con.execute(f"ATTACH DATABASE '{target.as_posix()}' AS evil")
    finally:
        con.close()
    assert not target.exists()


def test_attach_is_blocked_with_all_layers(db: Path):
    target = db.parent / "evil.db"
    con = open_public(db)
    try:
        with pytest.raises(sqlite3.Error):
            con.execute(f"ATTACH DATABASE '{target.as_posix()}' AS evil")
    finally:
        con.close()
    assert not target.exists()


def test_known_gap_attach_without_authorizer_creates_a_file(db: Path):
    """既知（S-7 項目 2）: authorizer を外すと ``mode=ro`` + ``query_only`` でも ATTACH は新しいパスにファイルを作る。

    副作用を止められるのは authorizer と静的検査の禁止語だけなので、どちらも省かない。
    """
    target = db.parent / "evil.db"
    con = open_public(db, layers=frozenset({"ro", "query_only"}))
    try:
        con.execute(f"ATTACH DATABASE '{target.as_posix()}' AS evil")
    finally:
        con.close()
    assert target.exists()
    # 同じ文は run_query では静的検査の段で止まり、ファイルは作られない
    target.unlink()
    with pytest.raises(LuciankeError):
        run_query(db, f"ATTACH DATABASE '{target.as_posix()}' AS evil")
    assert not target.exists()
