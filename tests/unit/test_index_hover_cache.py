"""``_hover_cache`` の読み書き（``ingest.store_hover`` と ``queries.cached_hover``）。

T-04 の LSP 経路（``tools/_t04_exact.py``）はここを通す。SQL は index 層に閉じる。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from lucianke.index import ingest, queries
from lucianke.index.connection import open_writer
from lucianke.index.migrate import ensure_schema

URN = "lk://w1/src/a.py#f1"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_writer(tmp_path / "paad.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


def _store(conn: sqlite3.Connection, urn: str, sha: str, text: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    ingest.store_hover(conn, urn, sha, text)
    conn.execute("COMMIT")


def test_miss_then_hit(conn: sqlite3.Connection) -> None:
    assert queries.cached_hover(conn, URN, "h1") is None
    _store(conn, URN, "h1", "def f() -> int")
    assert queries.cached_hover(conn, URN, "h1") == "def f() -> int"


def test_other_sha_is_a_miss_and_new_sha_replaces_the_old_row(conn: sqlite3.Connection) -> None:
    _store(conn, URN, "h1", "old")
    assert queries.cached_hover(conn, URN, "h2") is None
    _store(conn, URN, "h2", "new")
    assert queries.cached_hover(conn, URN, "h2") == "new"
    assert queries.cached_hover(conn, URN, "h1") is None  # 古い版の行は消える
    assert conn.execute("SELECT COUNT(*) FROM _hover_cache WHERE urn = ?", (URN,)).fetchone()[0] == 1


def test_same_key_is_overwritten_and_other_urns_are_kept(conn: sqlite3.Connection) -> None:
    _store(conn, URN, "h1", "first")
    _store(conn, "lk://w1/src/b.py#g1", "h9", "other")
    _store(conn, URN, "h1", "second")
    assert queries.cached_hover(conn, URN, "h1") == "second"
    assert queries.cached_hover(conn, "lk://w1/src/b.py#g1", "h9") == "other"


def test_store_outside_a_transaction_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        ingest.store_hover(conn, URN, "h1", "x")
