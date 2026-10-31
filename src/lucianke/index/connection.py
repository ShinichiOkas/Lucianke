"""PaaD（SQLite）の接続の開き方と読み取りプール（設計書 ADR-7、§6.2。仕様書 §4.5、§9.1）。

接続は 3 種ある。このモジュールが持つのは最初の 2 つ。

* 書き込み接続: ``open_writer``。``IndexWriter``（``index/writer.py``）の専用スレッドだけが使う。
* 読み取りプール: ``ReaderPool``。ツール・エンジン・編集の読みが借りて返す。
* 公開クエリ用の隔離接続（``query_project_db``）は ``engine/`` が自分で開く。

読み取り接続はスパイク S-07 の実測に従い、``mode=ro`` の URI で開く（WAL の DB を別接続で読める。書き手が
閉じていて -wal/-shm が無くても読み手が作る）。設計書 §6.2 は「通常モード」だが、書き込み文を誤って発行した
ときに黙って書き手と競合しないよう、読み取り専用で開き ``PRAGMA query_only=ON`` も掛ける（より厳しい側）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.hashing import WORKSPACE_HASH_LEN as WORKSPACE_HASH_LEN  # 再公開
from lucianke.core.hashing import workspace_hash
from lucianke.core.limits import READ_REQUEST_TIMEOUT_MS, READER_POOL_SIZE

if TYPE_CHECKING:
    from collections.abc import Iterator

_log = logging.getLogger("lucianke.index.connection")

DB_FILENAME: Final = "paad.sqlite"  # 仕様書 §4.5: <cache_root>/<workspace_hash>/paad.sqlite
WRITER_BUSY_TIMEOUT_MS: Final = 5000  # 設計書 §6.2
READER_BUSY_TIMEOUT_MS: Final = 1000  # 設計書 §6.2
WRITER_CACHE_KIB: Final = 131072
"""書き手のページキャッシュの上限（KiB。``PRAGMA cache_size = -N``）。既定の 2 MiB では、長い URN の索引（``symbols`` の ``urn``・``name``、
``relations`` の ``target_urn``）へばらばらの位置に差し込む初回の索引でページの読み直しが続き、4,000 ファイル級で書き込みが倍かかった
（BUG-0127）。上限なので、小さな DB では使った分しか取らない。"""
DEFAULT_ACQUIRE_TIMEOUT_S: Final = (
    READ_REQUEST_TIMEOUT_MS / 1000
)  # プールの空きを待つ上限（1 要求の読み取り上限と同じ）


# --- パス -------------------------------------------------------------------------------


def db_path_for(cache_root: Path | str, workspace_root: Path | str) -> Path:
    """``<cache_root>/<workspace_hash>/paad.sqlite``（仕様書 §4.5）。ディレクトリは作らない。"""
    return Path(cache_root) / workspace_hash(workspace_root) / DB_FILENAME


def ensure_dir_writable(directory: Path) -> None:
    """``directory`` を作り（無ければ）、書き込めることを確かめる。書けなければ ``OSError``。

    WAL では読み手も -shm を作るため、ディレクトリに書けないと読み手が DB を開けない（S-07 項目 1）。
    起動時に 1 度だけ確かめて、実行中に分かりにくい ``unable to open database file`` になるのを避ける。
    """
    directory.mkdir(parents=True, exist_ok=True)
    fd, probe = tempfile.mkstemp(dir=directory, prefix=".lucianke-probe-")
    os.close(fd)
    os.unlink(probe)


# --- 書き込み接続 -----------------------------------------------------------------------


def open_writer(path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """書き込み接続を開く。``IndexWriter`` の専用スレッドの中で呼んで、そのスレッドだけで使うこと。

    ``journal_mode=WAL``、``synchronous=NORMAL``、``foreign_keys=ON``、``busy_timeout=5000``、``cache_size``（``WRITER_CACHE_KIB``）。
    ``isolation_level=None``（自動コミット）にして、トランザクションは呼び出し側が ``BEGIN IMMEDIATE`` で
    明示する（Python の暗黙の ``BEGIN`` と衝突させない）。親ディレクトリが無ければ作る。

    Args:
        path: ``paad.sqlite`` のパス。
        check_same_thread: 既定の ``True`` は別スレッドからの利用を例外にする。
    """
    db_file = Path(path)
    ensure_dir_writable(db_file.parent)
    conn = sqlite3.connect(
        db_file,
        timeout=WRITER_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=check_same_thread,
    )
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":  # ネットワークドライブなど。読み手が書き手を待たせうる
            _log.warning("journal_mode=WAL を設定できませんでした（%s）: %s", mode, db_file)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={WRITER_BUSY_TIMEOUT_MS}")
        conn.execute(f"PRAGMA cache_size=-{WRITER_CACHE_KIB}")
    except BaseException:
        conn.close()
        raise
    return conn


# --- 読み取り接続 -----------------------------------------------------------------------


def open_reader(path: Path | str) -> sqlite3.Connection:
    """読み取り接続を 1 本開く（``mode=ro``、``query_only=ON``、``busy_timeout=1000``）。

    DB ファイルが無ければ ``sqlite3.OperationalError``（空のファイルを作らない）。プールの接続は
    別スレッドが順に借りるので ``check_same_thread=False``。同時に 2 スレッドが使ってはならない。
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=READER_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={READER_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only=ON")
    except BaseException:
        conn.close()
        raise
    return conn


def read_data_version(conn: sqlite3.Connection) -> int:
    """``_meta.data_version``（設計書 §6.2、ADR-8）。カーソルとキャッシュの無効化に使う。

    読み取り接続で読むと、同じ接続で読んだ行と同じ断面の値になる。
    """
    row = conn.execute("SELECT data_version FROM _meta LIMIT 1").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("_meta has no row (schema not initialized)")
    return int(row[0])


class ReaderPool:
    """読み取り接続の小さなプール（既定 ``READER_POOL_SIZE`` = 8）。接続は使うときに開く。

    ``with pool.reader() as conn:`` で借りて返す。借りた接続はそのブロックの中だけで使い、スレッドを越えて
    保持しない（返すと別のスレッドが借りる）。返すときに開いたままのトランザクションは巻き戻す
    （読み手が ``BEGIN`` を持ち越すと書き手の ``wal_checkpoint(TRUNCATE)`` が完了しない。S-07）。

    プールは DB ファイルがあるときに使う（``ensure_schema`` の後）。無ければ借りる時点で
    ``sqlite3.OperationalError``。
    """

    def __init__(self, path: Path | str, size: int = READER_POOL_SIZE) -> None:
        if size < 1:
            raise ValueError(f"size must be >= 1: {size}")
        self._path = Path(path)
        self._size = size
        self._cond = threading.Condition()
        self._idle: deque[sqlite3.Connection] = deque()
        self._created = 0  # 空き + 貸し出し中
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def idle_count(self) -> int:
        with self._cond:
            return len(self._idle)

    @property
    def created_count(self) -> int:
        with self._cond:
            return self._created

    def acquire(self, timeout: float | None = DEFAULT_ACQUIRE_TIMEOUT_S) -> sqlite3.Connection:
        """接続を 1 本借りる。全部貸し出し中なら返るまで待つ。``timeout`` 秒で ``E_TIMEOUT``（``None`` は無期限）。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                if self._closed:
                    raise RuntimeError("ReaderPool is closed")
                if self._idle:
                    return self._idle.pop()
                if self._created < self._size:
                    self._created += 1
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise LuciankeError(
                        ErrorCode.E_TIMEOUT,
                        f"no free read connection within {timeout} s (pool size {self._size})",
                    )
                self._cond.wait(remaining)
        try:
            return open_reader(self._path)
        except BaseException:
            with self._cond:
                self._created -= 1
                self._cond.notify()
            raise

    def release(self, conn: sqlite3.Connection) -> None:
        """接続を返す。開いたままのトランザクションは巻き戻す。壊れていれば捨てて、次の借り手が開き直す。"""
        healthy = True
        try:
            if conn.in_transaction:
                conn.rollback()
            conn.row_factory = None
        except sqlite3.Error:
            healthy = False
        with self._cond:
            keep = healthy and not self._closed
            if keep:
                self._idle.append(conn)
            else:
                self._created -= 1
            self._cond.notify()
        if not keep:
            conn.close()

    @contextmanager
    def reader(self, *, timeout: float | None = DEFAULT_ACQUIRE_TIMEOUT_S) -> Iterator[sqlite3.Connection]:
        """``with pool.reader() as conn:`` で借りて返す。"""
        conn = self.acquire(timeout)
        try:
            yield conn
        finally:
            self.release(conn)

    def close(self) -> None:
        """空きの接続を閉じる。貸し出し中の接続は返されたときに閉じる。2 回呼んでよい。"""
        with self._cond:
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
            self._created -= len(idle)
            self._cond.notify_all()
        for conn in idle:
            conn.close()

    def __enter__(self) -> ReaderPool:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
