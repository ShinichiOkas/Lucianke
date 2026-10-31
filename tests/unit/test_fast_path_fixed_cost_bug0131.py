"""呼び出しごとの固定の上乗せをなくす（BUG-0131）。

- ``IndexWriter.data_version`` は DB を読まず、書き手がコミットした後に覚えた値を返す（イベントループの上で SQLite を読まない）。
  値は DB の ``_meta.data_version`` と常に同じ（コミットされたジョブの分だけ進み、失敗・取り消しの分は進まない）。
- ``LspPool.state_of`` は起動コマンドを探さない（ファイルを stat しない）が、``status(...).status`` と同じ値を返す。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from lucianke.core.model import LspStatus
from lucianke.core.procs import ProcessRegistry
from lucianke.index import writer as writer_module
from lucianke.index.connection import DB_FILENAME, open_reader, read_data_version
from lucianke.index.writer import IndexWriter, WriterClosedError
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import servers_for
from lucianke.workspace.types import Workspace


def db_version(path: Path) -> int:
    conn = open_reader(path)
    try:
        return read_data_version(conn)
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ws" / DB_FILENAME


def test_data_version_is_served_from_memory(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with IndexWriter(db_path) as w:
        w.submit(lambda c: None, bumps_version=True, label="a").result(5)

        def no_read(*_a: object, **_k: object) -> int:
            raise AssertionError("data_version read the database")

        monkeypatch.setattr(writer_module, "read_data_version", no_read)
        assert w.data_version() == 1
        assert w.data_version() == 1


def test_data_version_follows_the_database_across_commits_failures_and_restarts(db_path: Path) -> None:
    def boom(_c: sqlite3.Connection) -> None:
        raise KeyError("nope")

    with IndexWriter(db_path) as w:
        assert w.data_version() == db_version(db_path) == 0
        for expected in (1, 2, 3):
            w.submit(lambda c: None, bumps_version=True, label="bump").result(5)
            assert w.data_version() == db_version(db_path) == expected  # 結果を受け取った時点で新しい値
        w.submit(lambda c: None, bumps_version=False, label="quiet").result(5)
        assert w.data_version() == 3  # 診断だけのジョブは進めない
        with pytest.raises(KeyError):
            w.submit(boom, bumps_version=True, label="bad").result(5)
        assert w.data_version() == db_version(db_path) == 3  # 失敗（ROLLBACK）は進めない
    with IndexWriter(db_path) as again:
        assert again.data_version() == 3  # 再起動したら DB の値から始まる
        again.submit(lambda c: None, bumps_version=True, label="more").result(5)
        assert again.data_version() == db_version(db_path) == 4


def test_a_cancelled_job_does_not_bump_the_version(db_path: Path) -> None:
    started, release = threading.Event(), threading.Event()
    with IndexWriter(db_path) as w:
        blocker = w.submit(
            lambda c: (started.set(), release.wait(5)) and None, bumps_version=False, label="blocker"
        )
        assert started.wait(5)
        queued = w.submit(lambda c: None, bumps_version=True, label="queued")
        assert queued.cancel()
        release.set()
        blocker.result(5)
        assert w.data_version() == db_version(db_path) == 0


def test_data_version_after_close_raises(db_path: Path) -> None:
    w = IndexWriter(db_path)
    w.close()
    with pytest.raises(WriterClosedError):
        w.data_version()


def test_concurrent_readers_see_a_monotonic_version(db_path: Path) -> None:
    seen: list[int] = []
    stop = threading.Event()
    with IndexWriter(db_path) as w:

        def poll() -> None:
            while not stop.is_set():
                seen.append(w.data_version())

        t = threading.Thread(target=poll)
        t.start()
        try:
            futures = [w.submit(lambda c: None, bumps_version=True, label=f"j{i}") for i in range(50)]
            for f in futures:
                f.result(10)
        finally:
            stop.set()
            t.join(5)
        assert seen == sorted(seen)
        assert w.data_version() == db_version(db_path) == 50


# --- LspPool.state_of -----------------------------------------------------------------------


def test_state_of_matches_status_without_looking_for_the_command(tmp_path: Path) -> None:
    ws = Workspace(workspace_id="w", root=tmp_path, case_insensitive=False)

    def resolver(_defn: object, _ws: object) -> list[str] | None:
        raise AssertionError("state_of looked for the server command")

    pool = LspPool(ProcessRegistry(), resolver=resolver)  # pyright: ignore[reportArgumentType]
    assert servers_for("python", "types")
    assert pool.state_of("python", ws) is LspStatus.STANDBY
    assert pool.state_of("markdown", ws) is LspStatus.NOT_APPLICABLE
    assert pool.state_of("nosuchlang", ws) is LspStatus.NOT_APPLICABLE
