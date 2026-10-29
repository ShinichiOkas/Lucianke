"""index の土台のテスト: 接続（connection）、スキーマ版管理（migrate）、唯一の書き手（writer）。

DB は ``tmp_path`` に作る。Windows では開いたままの接続があると tmp の後始末で ``PermissionError`` になるので、
接続・書き手・プールは必ず ``finally`` かフィクスチャで閉じる。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lucianke import __version__
from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.index import migrate
from lucianke.index.connection import (
    DB_FILENAME,
    ReaderPool,
    db_path_for,
    open_reader,
    open_writer,
    read_data_version,
    workspace_hash,
)
from lucianke.index.migrate import SchemaAction, current_schema_version, ensure_schema

PUBLIC_TABLES = {
    "workspaces",
    "files",
    "symbols",
    "relations",
    "dataflows",
    "diagnostics",
    "contracts",
    "git_commits",
    "git_symbol_changes",
    "git_co_changes",
}
INTERNAL_TABLES = {"_meta", "_hover_cache"}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "ws" / DB_FILENAME


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_writer(db_path)
    try:
        yield c
    finally:
        c.close()


def table_names(c: sqlite3.Connection) -> set[str]:
    rows = c.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def meta(c: sqlite3.Connection) -> tuple[int, int, int, str, str]:
    return c.execute(
        "SELECT schema_version, data_version, edit_seq, lucianke_version, created_at FROM _meta"
    ).fetchone()


def insert_workspace(c: sqlite3.Connection, ws_id: str = "w") -> None:
    c.execute("INSERT INTO workspaces (workspace_id, root_path) VALUES (?, ?)", (ws_id, f"/{ws_id}"))


# --- connection: パスと PRAGMA --------------------------------------------------------------


def test_workspace_hash_is_16_hex_and_stable(tmp_path: Path) -> None:
    h = workspace_hash(tmp_path)
    assert len(h) == 16
    assert int(h, 16) >= 0
    assert workspace_hash(tmp_path) == h
    # 同じ実体パスの別の書き方は同じ hash
    (tmp_path / "a").mkdir()
    assert workspace_hash(tmp_path / "a" / "..") == h
    # 別のディレクトリは別の hash
    assert workspace_hash(tmp_path / "a") != h


def test_db_path_for_layout(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    cache = tmp_path / "cache"
    p = db_path_for(cache, ws)
    assert p == cache / workspace_hash(ws) / "paad.sqlite"
    assert not cache.exists()  # パスを作るだけで、ディレクトリは作らない


def test_open_writer_pragmas(conn: sqlite3.Connection, db_path: Path) -> None:
    assert db_path.parent.is_dir()  # 親ディレクトリを作る
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.isolation_level is None


def test_open_reader_pragmas_and_rejects_writes(conn: sqlite3.Connection, db_path: Path) -> None:
    ensure_schema(conn)
    r = open_reader(db_path)
    try:
        assert r.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
        assert r.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            r.execute("INSERT INTO workspaces (workspace_id, root_path) VALUES ('x', '/x')")
    finally:
        r.close()


def test_open_reader_does_not_create_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        open_reader(missing)
    assert not missing.exists()


def test_read_data_version(conn: sqlite3.Connection, db_path: Path) -> None:
    ensure_schema(conn)
    r = open_reader(db_path)
    try:
        assert read_data_version(r) == 0
        conn.execute("UPDATE _meta SET data_version = data_version + 1")
        assert read_data_version(r) == 1
    finally:
        r.close()


# --- migrate -----------------------------------------------------------------------------


def test_ensure_schema_creates_public_internal_tables_and_meta(conn: sqlite3.Connection) -> None:
    assert ensure_schema(conn) is SchemaAction.CREATED
    names = table_names(conn)
    # FTS5 の影の表（symbols_fts_data など）も sqlite_master に出るので、除いて比べる
    main_names = {n for n in names if not n.startswith("symbols_fts_")}
    assert main_names == PUBLIC_TABLES | {"symbols_fts"} | INTERNAL_TABLES
    schema_version, data_version, edit_seq, lucianke_version, created_at = meta(conn)
    assert (schema_version, data_version, edit_seq) == (current_schema_version(), 0, 0)
    assert lucianke_version == __version__
    assert created_at.endswith("+00:00")
    assert conn.execute("SELECT count(*) FROM _meta").fetchone()[0] == 1
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert triggers == {"symbols_ai", "symbols_ad", "symbols_au"}


def test_schema_version_is_read_from_schema_sql() -> None:
    assert current_schema_version() == 1
    assert migrate.parse_schema_version("-- schema_version: 7\nCREATE TABLE t (a);") == 7
    with pytest.raises(ValueError):
        migrate.parse_schema_version("CREATE TABLE t (a);")


def test_ensure_schema_twice_keeps_data(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    insert_workspace(conn)
    conn.execute("UPDATE _meta SET data_version = 5, edit_seq = 3")
    before = meta(conn)
    assert ensure_schema(conn) is SchemaAction.UNCHANGED
    assert meta(conn) == before
    assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
    assert not conn.in_transaction


def test_ensure_schema_keeps_foreign_keys_on(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.execute("UPDATE _meta SET schema_version = 0")
    ensure_schema(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("stored_version", [0, 99])
def test_ensure_schema_rebuilds_on_other_version(conn: sqlite3.Connection, stored_version: int) -> None:
    ensure_schema(conn)
    insert_workspace(conn)
    conn.execute("UPDATE _meta SET schema_version = ?, data_version = 9, edit_seq = 4", (stored_version,))
    assert ensure_schema(conn) is SchemaAction.REBUILT
    assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0  # 中身は捨てる
    schema_version, data_version, edit_seq, _, _ = meta(conn)
    assert (schema_version, data_version) == (current_schema_version(), 0)
    assert edit_seq == 4  # 編集 ID は再利用しない
    assert {n for n in table_names(conn) if not n.startswith("symbols_fts_")} == (
        PUBLIC_TABLES | {"symbols_fts"} | INTERNAL_TABLES
    )
    assert ensure_schema(conn) is SchemaAction.UNCHANGED


def test_ensure_schema_rebuilds_db_without_meta(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE leftover (a)")
    conn.execute("CREATE TABLE files (junk)")
    assert ensure_schema(conn) is SchemaAction.REBUILT
    assert "leftover" not in table_names(conn)
    assert meta(conn)[0] == current_schema_version()
    conn.execute(
        "INSERT INTO workspaces (workspace_id, root_path) VALUES ('w', '/w')"
    )  # 本物の表になっている


def test_rebuilt_schema_has_working_fts_triggers(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    conn.execute("UPDATE _meta SET schema_version = 0")
    ensure_schema(conn)
    insert_workspace(conn)
    conn.execute(
        "INSERT INTO symbols (urn, is_external, name, qualified_name, kind, start_line, end_line, confidence_level)"
        " VALUES ('lk://w/a.py#f', 0, 'frobnicate', 'frobnicate', 'function', 1, 2, 'exact')"
    )
    hits = conn.execute("SELECT name FROM symbols_fts WHERE symbols_fts MATCH 'frobnicate'").fetchall()
    assert hits == [("frobnicate",)]


def test_ensure_schema_applies_forward_migrations(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_schema(conn)
    insert_workspace(conn)
    conn.execute("UPDATE _meta SET schema_version = 0")
    calls: list[int] = []
    monkeypatch.setattr(migrate, "MIGRATIONS", {0: lambda c: calls.append(0)})
    assert ensure_schema(conn) is SchemaAction.MIGRATED
    assert calls == [0]
    assert meta(conn)[0] == current_schema_version()
    assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1  # 移行ではデータを残す


def test_ensure_schema_rolls_back_when_creation_fails(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        migrate, "schema_sql", lambda: "-- schema_version: 1\nCREATE TABLE a (x);\nCREATE TABLE a (y);\n"
    )
    with pytest.raises(sqlite3.OperationalError):
        ensure_schema(conn)
    assert not conn.in_transaction
    assert table_names(conn) == set()  # 途中までの表を残さない
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --- ReaderPool --------------------------------------------------------------------------


@pytest.fixture
def pool(conn: sqlite3.Connection, db_path: Path) -> Iterator[ReaderPool]:
    ensure_schema(conn)
    p = ReaderPool(db_path, size=2)
    try:
        yield p
    finally:
        p.close()


def test_pool_returns_borrowed_connection(pool: ReaderPool) -> None:
    with pool.reader() as c1:
        assert pool.idle_count == 0
        assert pool.created_count == 1
    assert pool.idle_count == 1
    with pool.reader() as c2:
        assert c2 is c1  # 返した接続がプールに戻り、使い回される
    assert pool.created_count == 1


def test_pool_default_size_is_reader_pool_size(db_path: Path) -> None:
    from lucianke.core.limits import READER_POOL_SIZE

    assert ReaderPool(db_path).size == READER_POOL_SIZE


def test_pool_returns_connection_even_when_body_raises(pool: ReaderPool) -> None:
    with pytest.raises(KeyError), pool.reader():
        raise KeyError("boom")
    assert pool.idle_count == 1


def test_pool_rolls_back_open_transaction_on_return(pool: ReaderPool, conn: sqlite3.Connection) -> None:
    with pool.reader() as c:
        c.execute("BEGIN")
        c.execute("SELECT count(*) FROM workspaces").fetchone()
        assert c.in_transaction
        c.row_factory = sqlite3.Row
    with pool.reader() as c:
        assert not c.in_transaction  # BEGIN を持ち越さない
        assert c.row_factory is None
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 読み手が止めない


def test_pool_blocks_when_exhausted_until_a_connection_is_returned(pool: ReaderPool) -> None:
    got: list[sqlite3.Connection] = []

    def borrower() -> None:
        with pool.reader() as c:
            got.append(c)

    with pool.reader(), pool.reader():  # size=2 を使い切る
        t = threading.Thread(target=borrower, daemon=True)
        t.start()
        t.join(0.3)
        assert t.is_alive()  # 3 本目は待つ
        assert got == []
    t.join(5)
    assert not t.is_alive()
    assert len(got) == 1
    assert pool.created_count == 2  # 大きさを超えて開かない


def test_pool_acquire_timeout_is_e_timeout(pool: ReaderPool) -> None:
    with pool.reader(), pool.reader():
        with pytest.raises(LuciankeError) as ei:
            pool.acquire(timeout=0.1)
        assert ei.value.code is ErrorCode.E_TIMEOUT
        assert pool.created_count == 2  # 失敗しても数が狂わない
    assert pool.idle_count == 2


def test_pool_connections_usable_from_other_threads(pool: ReaderPool) -> None:
    with pool.reader() as c:
        c.execute("SELECT 1").fetchone()
    results: list[int] = []

    def other() -> None:
        with pool.reader() as c2:  # 別スレッドが、別スレッドで開いた接続を借りる
            results.append(c2.execute("SELECT 41 + 1").fetchone()[0])

    t = threading.Thread(target=other)
    t.start()
    t.join(5)
    assert results == [42]


def test_pool_close_rejects_new_borrows_and_closes_on_return(db_path: Path, conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    p = ReaderPool(db_path, size=2)
    c = p.acquire()
    p.close()
    p.close()  # 2 回呼んでよい
    with pytest.raises(RuntimeError):
        p.acquire()
    p.release(c)  # 貸し出し中だった接続は返されたときに閉じる
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


def test_pool_reads_while_writer_holds_an_open_write_transaction(
    pool: ReaderPool, conn: sqlite3.Connection
) -> None:
    """WAL: 書き手が書き込み中（未コミット）でも読める。未コミットの行は見えず、コミット後に見える。"""
    conn.execute("BEGIN IMMEDIATE")
    insert_workspace(conn, "pending")
    started = time.perf_counter()
    with pool.reader() as c:
        assert c.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
    assert time.perf_counter() - started < 0.5  # busy_timeout（1 秒）まで待たされていない
    conn.execute("COMMIT")
    with pool.reader() as c:
        assert c.execute("SELECT workspace_id FROM workspaces").fetchall() == [("pending",)]
