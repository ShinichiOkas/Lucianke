"""index の土台のテスト: 接続（connection）、スキーマ版管理（migrate）、唯一の書き手（writer）。

DB は ``tmp_path`` に作る。Windows では開いたままの接続があると tmp の後始末で ``PermissionError`` になるので、
接続・書き手・プールは必ず ``finally`` かフィクスチャで閉じる。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
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
from lucianke.index.writer import IndexWriter, WriterClosedError, format_edit_id

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
INTERNAL_TABLES = {
    "_meta",
    "_hover_cache",
    "_symbol_rank",
    "_import_probes",
    "_exact_marks",
    "_name_calls",
    "_member_refs",
    "_attr_defs",  # v9（BUG-0095）
    "_var_types",  # v9（BUG-0094）
    "_reexports",  # v10（BUG-0100）
    "_reexport_deps",  # v10（BUG-0100）
    "_type_aliases",  # v12（BUG-0108）
}


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
    assert current_schema_version() == 12
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
    steps = list(range(current_schema_version()))  # 0 → 1 → … → 最新（版を上げても数えなおさない）
    monkeypatch.setattr(migrate, "MIGRATIONS", {v: (lambda c, v=v: calls.append(v)) for v in steps})
    assert ensure_schema(conn) is SchemaAction.MIGRATED
    assert calls == steps
    assert meta(conn)[0] == current_schema_version()
    assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1  # 移行ではデータを残す


def test_ensure_schema_migrates_v1_to_v2_keeps_data(conn: sqlite3.Connection) -> None:
    """v1 の DB（``_symbol_rank`` なし）は作り直さず、表を足すだけで最新になる（データは残る）。"""
    ensure_schema(conn)
    insert_workspace(conn)
    conn.execute("DROP TABLE _symbol_rank")
    conn.execute("DROP TABLE _import_probes")
    conn.execute("DROP TABLE _exact_marks")
    conn.execute("DROP TABLE _name_calls")
    conn.execute("DROP TABLE _member_refs")
    for later in ("_attr_defs", "_var_types", "_reexports", "_reexport_deps", "_type_aliases"):  # v9・v10・v12 で足した表
        conn.execute(f"DROP TABLE {later}")
    conn.execute("UPDATE _meta SET schema_version = 1")
    assert ensure_schema(conn) is SchemaAction.MIGRATED
    assert meta(conn)[0] == current_schema_version()
    assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM _symbol_rank").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM _import_probes").fetchone()[0] == 0


def test_ensure_schema_migrates_v2_to_v3_keeps_data(conn: sqlite3.Connection) -> None:
    """v2 の DB（``_import_probes`` なし）は作り直さず、表と索引を足すだけで v3 になる（データは残る。値は各ファイルの次の取り込みで入る）。"""
    ensure_schema(conn)
    insert_workspace(conn)
    conn.execute("DROP TABLE _import_probes")
    conn.execute("DROP TABLE _exact_marks")
    conn.execute("DROP TABLE _name_calls")
    conn.execute("DROP TABLE _member_refs")
    for later in ("_attr_defs", "_var_types", "_reexports", "_reexport_deps", "_type_aliases"):  # v9・v10・v12 で足した表
        conn.execute(f"DROP TABLE {later}")
    conn.execute("UPDATE _meta SET schema_version = 2")
    assert ensure_schema(conn) is SchemaAction.MIGRATED
    assert meta(conn)[0] == current_schema_version()
    assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM _import_probes").fetchone()[0] == 0
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = '_import_probes_stem'"
    ).fetchall()


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


# --- IndexWriter -------------------------------------------------------------------------


@pytest.fixture
def writer(db_path: Path) -> Iterator[IndexWriter]:
    w = IndexWriter(db_path)
    try:
        yield w
    finally:
        w.close()


def gate_job(started: threading.Event, release: threading.Event) -> Callable[[sqlite3.Connection], None]:
    """書き手スレッドを ``release`` が立つまで止めるジョブ。止まっている間もトランザクションは開いている。"""

    def fn(c: sqlite3.Connection) -> None:
        started.set()
        assert release.wait(10), "テストが release を立てなかった"

    return fn


def test_writer_creates_schema_and_reports_action(writer: IndexWriter, db_path: Path) -> None:
    assert writer.schema_action is SchemaAction.CREATED
    assert writer.path == db_path
    assert writer.data_version() == 0
    writer.close()
    again = IndexWriter(db_path)
    try:
        assert again.schema_action is SchemaAction.UNCHANGED
    finally:
        again.close()


def writer_threads_started_since(before: set[threading.Thread]) -> list[threading.Thread]:
    """``before`` の後に起きた書き手スレッド（このテストが作ったものだけ。他のテストの残りは見ない。BUG-0076）。"""
    return [t for t in threading.enumerate() if t.name == "lucianke-index-writer" and t not in before]


def test_writer_start_failure_raises_and_leaves_no_thread(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    before = set(threading.enumerate())
    with pytest.raises(OSError):
        IndexWriter(blocker / "sub" / DB_FILENAME)  # 親がディレクトリではない
    assert [t for t in writer_threads_started_since(before) if t.is_alive()] == []


def test_writer_start_timeout_stops_the_thread_when_it_starts_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動の待ちが時間切れでも、遅れて DB を開いたスレッドはジョブを待たずに止まる（残らない。BUG-0076）。"""
    from lucianke.index import writer as writer_mod

    release = threading.Event()
    real_open = writer_mod.open_writer

    def slow_open(path: Path) -> sqlite3.Connection:
        assert release.wait(30), "テストが release を立てなかった"
        return real_open(path)

    monkeypatch.setattr(writer_mod, "open_writer", slow_open)
    monkeypatch.setattr(writer_mod, "START_TIMEOUT_S", 0.05)
    before = set(threading.enumerate())
    with pytest.raises(TimeoutError):
        IndexWriter(tmp_path / DB_FILENAME)
    [late] = writer_threads_started_since(before)
    assert late.is_alive()  # まだ DB を開いている途中
    release.set()
    late.join(30)
    assert not late.is_alive()


def test_100_concurrent_submits_run_one_at_a_time_on_the_writer_thread(writer: IndexWriter) -> None:
    inside = threading.Event()
    overlaps: list[str] = []
    threads_seen: set[str] = set()
    order: dict[int, list[int]] = {}

    def make_job(tid: int, n: int) -> Callable[[sqlite3.Connection], int]:
        def job(c: sqlite3.Connection) -> int:
            if inside.is_set():
                overlaps.append(f"{tid}-{n}")
            inside.set()
            threads_seen.add(threading.current_thread().name)
            assert c.in_transaction  # BEGIN IMMEDIATE の中
            c.execute(
                "INSERT INTO workspaces (workspace_id, root_path) VALUES (?, ?)",
                (f"w{tid}-{n}", f"/{tid}/{n}"),
            )
            time.sleep(0.0005)  # 重なる余地を作る
            order.setdefault(tid, []).append(n)
            inside.clear()
            return tid * 100 + n

        return job

    futures: list[Future[int]] = []
    futures_lock = threading.Lock()

    def submitter(tid: int) -> None:
        for n in range(10):
            f = writer.submit(make_job(tid, n), bumps_version=True, label=f"job-{tid}-{n}")
            with futures_lock:
                futures.append(f)

    threads = [threading.Thread(target=submitter, args=(tid,)) for tid in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(futures) == 100
    results = sorted(f.result(timeout=20) for f in futures)
    assert results == sorted(tid * 100 + n for tid in range(10) for n in range(10))
    assert overlaps == []  # 同時に 2 つ走らない
    assert threads_seen == {"lucianke-index-writer"}  # 全部、専用スレッドで
    assert all(seq == list(range(10)) for seq in order.values())  # 同じ投入者のジョブは投入順
    assert writer.data_version() == 100  # 1 ジョブ 1 回
    check = open_reader(writer.path)
    try:
        assert check.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 100
    finally:
        check.close()


def test_bumps_version_only_when_asked(writer: IndexWriter) -> None:
    assert writer.data_version() == 0
    writer.submit(lambda c: insert_workspace(c, "a"), bumps_version=True, label="a").result(5)
    assert writer.data_version() == 1
    writer.submit(lambda c: insert_workspace(c, "b"), bumps_version=False, label="diag").result(5)
    assert writer.data_version() == 1  # diagnostics だけの更新では進めない
    writer.submit(lambda c: None, bumps_version=True, label="noop").result(5)
    assert writer.data_version() == 2


def test_failed_job_is_rolled_back_and_later_jobs_proceed(
    writer: IndexWriter, caplog: pytest.LogCaptureFixture
) -> None:
    def bad(c: sqlite3.Connection) -> None:
        insert_workspace(c, "doomed")
        raise ValueError("boom")

    bad_future = writer.submit(bad, bumps_version=True, label="bad-job")
    good_future = writer.submit(lambda c: insert_workspace(c, "kept"), bumps_version=True, label="good-job")
    with pytest.raises(ValueError, match="boom"):
        bad_future.result(5)
    good_future.result(5)

    rows = writer.submit(
        lambda c: c.execute("SELECT workspace_id FROM workspaces").fetchall(),
        bumps_version=False,
        label="read",
    ).result(5)
    assert rows == [("kept",)]  # 失敗したジョブの書き込みは残らない
    assert writer.data_version() == 1  # 失敗したジョブは data_version を進めない
    assert any("bad-job" in r.getMessage() for r in caplog.records)  # label つきでログに残る


def test_sql_error_inside_job_is_rolled_back_whole(writer: IndexWriter) -> None:
    def half(c: sqlite3.Connection) -> None:
        insert_workspace(c, "first")
        c.execute("INSERT INTO files (workspace_id) VALUES ('first')")  # NOT NULL 違反

    with pytest.raises(sqlite3.IntegrityError):
        writer.submit(half, bumps_version=True, label="half").result(5)
    count = writer.submit(
        lambda c: c.execute("SELECT count(*) FROM workspaces").fetchone()[0], bumps_version=False, label="r"
    ).result(5)
    assert count == 0


def test_job_that_ends_its_own_transaction_is_an_error(writer: IndexWriter) -> None:
    f = writer.submit(lambda c: c.execute("COMMIT"), bumps_version=True, label="rude")
    with pytest.raises(RuntimeError, match="ended the transaction"):
        f.result(5)
    writer.submit(lambda c: None, bumps_version=False, label="after").result(5)  # 書き手は生きている


def test_next_edit_id_is_monotonic_and_survives_restart(db_path: Path) -> None:
    w = IndexWriter(db_path)
    try:
        assert [w.next_edit_id() for _ in range(3)] == ["ed_0001", "ed_0002", "ed_0003"]
        assert w.data_version() == 0  # 編集 ID の払い出しは data_version を進めない
    finally:
        w.close()
    w2 = IndexWriter(db_path)
    try:
        assert w2.next_edit_id() == "ed_0004"
        w2.submit(
            lambda c: c.execute("UPDATE _meta SET edit_seq = 9999"), bumps_version=False, label="x"
        ).result(5)
        assert w2.next_edit_id() == "ed_10000"  # 4 桁を超えたら桁が増える
    finally:
        w2.close()


def test_next_edit_id_concurrent_callers_get_unique_ids(writer: IndexWriter) -> None:
    ids: list[str] = []
    lock = threading.Lock()

    def grab() -> None:
        for _ in range(5):
            i = writer.next_edit_id()
            with lock:
                ids.append(i)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(ids) == 40
    assert sorted(ids) == [format_edit_id(n) for n in range(1, 41)]


def test_next_edit_id_from_writer_thread_is_rejected(writer: IndexWriter) -> None:
    f = writer.submit(lambda c: writer.next_edit_id(), bumps_version=False, label="self-wait")
    with pytest.raises(RuntimeError, match="writer thread"):
        f.result(5)


def test_close_drains_queue_then_rejects_new_submits(db_path: Path) -> None:
    w = IndexWriter(db_path)
    started, release = threading.Event(), threading.Event()
    blocker = w.submit(gate_job(started, release), bumps_version=False, label="blocker")
    assert started.wait(5)
    queued = [
        w.submit(lambda c, i=i: insert_workspace(c, f"q{i}"), bumps_version=True, label=f"q{i}")
        for i in range(5)
    ]
    closer = threading.Thread(target=w.close)
    closer.start()
    closer.join(0.3)
    assert closer.is_alive()  # キューが空になるまで止まらない
    with pytest.raises(WriterClosedError):
        w.submit(lambda c: None, bumps_version=False, label="late")
    release.set()
    closer.join(10)
    assert not closer.is_alive()
    assert blocker.done()
    assert all(f.done() and f.exception() is None for f in queued)  # 積まれた分は全部実行された
    with pytest.raises(WriterClosedError):
        w.submit(lambda c: None, bumps_version=False, label="after-close")
    with pytest.raises(WriterClosedError):
        w.data_version()
    w.close()  # 2 回目は何もしない
    check = open_reader(db_path)
    try:
        assert check.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 5
        assert read_data_version(check) == 5
    finally:
        check.close()


def test_close_checkpoints_wal(db_path: Path) -> None:
    w = IndexWriter(db_path)
    for i in range(20):
        w.submit(lambda c, i=i: insert_workspace(c, f"w{i}"), bumps_version=True, label=f"w{i}").result(5)
    w.close()
    wal = db_path.with_name(db_path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0  # TRUNCATE 済み（最後の接続が閉じて消えることもある）
    c = sqlite3.connect(db_path)
    try:
        assert c.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 20  # 本体に入っている
    finally:
        c.close()


def test_cancelled_job_before_start_is_skipped(writer: IndexWriter) -> None:
    started, release = threading.Event(), threading.Event()
    blocker = writer.submit(gate_job(started, release), bumps_version=False, label="blocker")
    assert started.wait(5)
    ran: list[str] = []
    victim = writer.submit(lambda c: ran.append("victim"), bumps_version=True, label="victim")
    follower = writer.submit(lambda c: ran.append("follower"), bumps_version=True, label="follower")
    assert victim.cancel()
    release.set()
    blocker.result(5)
    follower.result(5)
    assert ran == ["follower"]
    assert writer.data_version() == 1


def test_data_version_reflects_commits_visible_to_readers(writer: IndexWriter, db_path: Path) -> None:
    pool = ReaderPool(db_path, size=1)
    try:
        writer.submit(lambda c: None, bumps_version=True, label="a").result(5)
        with pool.reader() as r:
            assert read_data_version(r) == writer.data_version() == 1
    finally:
        pool.close()


async def test_asubmit_awaits_result_and_propagates_errors(writer: IndexWriter) -> None:
    assert await writer.asubmit(lambda c: 6 * 7, bumps_version=False, label="answer") == 42

    def bad(c: sqlite3.Connection) -> None:
        raise KeyError("nope")

    with pytest.raises(KeyError):
        await writer.asubmit(bad, bumps_version=True, label="bad")
    assert await writer.anext_edit_id() == "ed_0001"
    results = await asyncio.gather(
        *(writer.asubmit(lambda c, i=i: i, bumps_version=True, label=f"j{i}") for i in range(30))
    )
    assert results == list(range(30))
    assert writer.data_version() == 30


async def test_asubmit_does_not_block_event_loop_when_queue_is_full(db_path: Path) -> None:
    w = IndexWriter(db_path, queue_max=2)
    started, release = threading.Event(), threading.Event()
    try:
        blocker = w.submit(gate_job(started, release), bumps_version=False, label="blocker")
        assert await asyncio.to_thread(started.wait, 5)
        w.submit(lambda c: None, bumps_version=False, label="q1")
        w.submit(lambda c: None, bumps_version=False, label="q2")  # キュー 2 件で満杯
        task = asyncio.create_task(w.asubmit(lambda c: "done", bumps_version=False, label="waits"))
        ticks = 0
        for _ in range(5):  # 待っている間もイベントループは回る
            await asyncio.sleep(0.05)
            ticks += 1
        assert ticks == 5
        assert not task.done()
        release.set()
        assert await asyncio.wait_for(task, 10) == "done"
        blocker.result(5)
    finally:
        release.set()
        w.close()


def test_submit_blocks_when_queue_is_full_until_space(db_path: Path) -> None:
    w = IndexWriter(db_path, queue_max=1)
    started, release = threading.Event(), threading.Event()
    try:
        w.submit(gate_job(started, release), bumps_version=False, label="blocker")
        assert started.wait(5)
        w.submit(lambda c: None, bumps_version=False, label="q1")
        done: list[Future[None]] = []
        t = threading.Thread(
            target=lambda: done.append(w.submit(lambda c: None, bumps_version=False, label="q2"))
        )
        t.start()
        t.join(0.3)
        assert t.is_alive()  # 満杯なので待つ
        release.set()
        t.join(10)
        assert not t.is_alive()
        done[0].result(5)
    finally:
        release.set()
        w.close()
