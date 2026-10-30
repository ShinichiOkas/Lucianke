"""``index/exact_marks.py``（内部表 ``_exact_marks``）と、スキーマ v3 → v4 の移行。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from lucianke.index.connection import open_writer
from lucianke.index.exact_marks import ExactMark, load_mark, store_marks
from lucianke.index.migrate import SchemaAction, current_schema_version, ensure_schema


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_writer(tmp_path / "paad.sqlite")
    try:
        yield c
    finally:
        c.close()


def test_migrates_v3_to_v4_by_adding_the_table_and_keeping_the_data(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    conn.execute("DROP TABLE _exact_marks")
    conn.execute("DROP TABLE _name_calls")
    conn.execute("DROP TABLE _member_refs")
    conn.execute("UPDATE _meta SET schema_version = 3")
    assert ensure_schema(conn) is SchemaAction.MIGRATED
    assert current_schema_version() >= 4  # v4 で _exact_marks、その先の移行も続けて当たる
    assert conn.execute("SELECT schema_version FROM _meta").fetchone()[0] == current_schema_version()
    assert conn.execute("SELECT count(*) FROM _exact_marks").fetchone()[0] == 0


def test_a_mark_round_trips_and_is_replaced(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    assert load_mark(conn, "ws", "refs", "urn:a") is None
    store_marks(
        conn, "ws", [ExactMark("refs", "urn:a", "tok1", "fp1"), ExactMark("calls_in", "urn:a", "tok1", "fp2")]
    )
    assert load_mark(conn, "ws", "refs", "urn:a") == ("tok1", "fp1")
    assert load_mark(conn, "ws", "calls_in", "urn:a") == ("tok1", "fp2")
    assert load_mark(conn, "other", "refs", "urn:a") is None
    store_marks(conn, "ws", [ExactMark("refs", "urn:a", "tok1", "fp9")])
    assert load_mark(conn, "ws", "refs", "urn:a") == ("tok1", "fp9")


def test_marks_with_another_files_token_are_dropped_for_that_workspace_only(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    store_marks(conn, "ws", [ExactMark("refs", "urn:old", "tok1", "fp1")])
    store_marks(conn, "other", [ExactMark("refs", "urn:old", "tok1", "fp1")])
    store_marks(conn, "ws", [ExactMark("refs", "urn:new", "tok2", "fp2")])
    assert load_mark(conn, "ws", "refs", "urn:old") is None  # 指紋が変わった目印は二度と一致しない
    assert load_mark(conn, "ws", "refs", "urn:new") == ("tok2", "fp2")
    assert load_mark(conn, "other", "refs", "urn:old") == ("tok1", "fp1")


def test_store_nothing_is_a_no_op_and_a_missing_table_reads_as_no_mark(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    assert store_marks(conn, "ws", []) == 0
    conn.execute("DROP TABLE _exact_marks")
    assert load_mark(conn, "ws", "refs", "urn:a") is None  # 移行前の DB を読み取り接続が開いても落とさない
