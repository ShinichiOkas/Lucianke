"""唯一の書き手スレッド ``IndexWriter``（設計書 ADR-7、§7.6.1。仕様書 §9.1、§9.4）。

PaaD への書き込みはすべてここを通る。専用スレッド 1 本がジョブキューから 1 件ずつ取り、
**1 ジョブ = 1 トランザクション** で実行する::

    BEGIN IMMEDIATE → fn(conn) → (bumps_version なら data_version を +1) → COMMIT

例外は ``ROLLBACK`` してログに残し、``Future`` に載せる。半更新の状態は読み手に見えない（MUST）。
``bumps_version`` は ``files`` / ``symbols`` / ``relations`` を変えるジョブで ``True``、``diagnostics`` だけの
ジョブで ``False``（ADR-8）。``fn`` はトランザクションを自分で閉じてはならない（``COMMIT`` / ``ROLLBACK`` /
``BEGIN`` を発行しない）。

接続はこのスレッドの中で開く（``open_writer`` → ``ensure_schema``）。他のスレッドは接続に触れない。
読み手は ``ReaderPool``（``connection.py``）から借りる。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from lucianke.index.connection import open_reader, open_writer, read_data_version
from lucianke.index.migrate import SchemaAction, ensure_schema

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger("lucianke.index.writer")

# 設計書 §7.6.1 は 10000。親の指示（limits.py に無いので writer.py に置く）で 1000。
WRITE_QUEUE_MAX: Final = 1000
CHECKPOINT_EVERY_COMMITS: Final = (
    1000  # 設計書 §7.6.1-3: キューが空で、前回からこの数以上コミットしたら PASSIVE
)
IDLE_CHECKPOINT_S: Final = 2.0  # 書き込みが途切れてこの時間たったら、未チェックポイントの分を PASSIVE で流す
EXTERNAL_CHECK_S: Final = 0.025  # 別の接続（別プロセス）のコミットを見に行く間隔の下限。``data_version()`` の古さの上限（BUG-0131）
START_TIMEOUT_S: Final = 60.0  # スレッド起動（接続を開き、スキーマを確認する）の待ち上限


class WriterClosedError(RuntimeError):
    """``close()`` の後（または開始に失敗した書き手）に ``submit`` した。"""


@dataclass(frozen=True, slots=True)
class _Job:
    fn: Callable[[sqlite3.Connection], Any]
    bumps_version: bool
    label: str
    future: Future[Any]


def format_edit_id(seq: int) -> str:
    """``_meta.edit_seq`` の値から編集 ID を作る。``ed_0001``（4 桁以上のゼロ埋め）。"""
    return f"ed_{seq:04d}"


def _bump_edit_seq(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE _meta SET edit_seq = edit_seq + 1")
    return int(conn.execute("SELECT edit_seq FROM _meta").fetchone()[0])


class IndexWriter:
    """PaaD の唯一の書き手。生成した時点でスレッドが起動し、DB が使える状態になるまで待つ。

    Args:
        path: ``paad.sqlite`` のパス（``db_path_for`` で得る）。親ディレクトリが無ければ作る。
        queue_max: キュー長の上限。満杯のとき ``submit`` は空きができるまで待ち、``asubmit`` はイベント
            ループを止めずに待つ。

    Raises:
        OSError: ディレクトリに書けない、DB を開けない。
        sqlite3.DatabaseError: スキーマの確認・作成に失敗した。
    """

    def __init__(
        self, path: Path | str, *, queue_max: int = WRITE_QUEUE_MAX, external_check_s: float = EXTERNAL_CHECK_S
    ) -> None:
        self._path = Path(path)
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=queue_max)
        self._cond = threading.Condition()
        self._accepting = True
        self._pending = 0  # submit 済みで、まだ結果が決まっていないジョブ
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        # コミット済みの ``_meta.data_version``（BUG-0131: 呼び出しのたびに DB を読むと、イベントループの上で約 0.8 ms・並行する呼び出しは並ぶ）。
        # 自分のコミットは書き手スレッドがコミットの後に進める（すぐ見える）。同じ DB を開く別のプロセスのコミットは、
        # ``data_version()`` が ``external_check_s`` 以上の間をあけて ``PRAGMA data_version``（接続ごと。他の接続のコミットで変わる）で
        # 検知し、変わったときだけ DB から読み直す（古さの上限は ``external_check_s``）。
        self._data_version = 0
        self._version_lock = threading.Lock()  # ``_data_version`` と検知用の接続・印の更新
        self._external_check_s = external_check_s
        self._probe_conn: sqlite3.Connection | None = None
        self._probe_mark: int | None = None  # 最後に見た ``PRAGMA data_version``
        self._probe_at = 0.0  # 最後に見に行った時刻（monotonic）
        self.schema_action: SchemaAction | None = None
        self._thread = threading.Thread(target=self._run, name="lucianke-index-writer", daemon=True)
        self._thread.start()
        if not self._ready.wait(START_TIMEOUT_S):
            self._accepting = False
            self._queue.put_nowait(None)  # 遅れて起動したスレッドはジョブを待たずに止まる（残さない。BUG-0076）
            raise TimeoutError(f"index writer did not start within {START_TIMEOUT_S} s: {self._path}")
        if self._start_error is not None:
            self._accepting = False
            self._thread.join()
            raise self._start_error

    @property
    def path(self) -> Path:
        return self._path

    # --- 投入 -------------------------------------------------------------------------------

    def submit[T](
        self, fn: Callable[[sqlite3.Connection], T], *, bumps_version: bool, label: str
    ) -> Future[T]:
        """ジョブを積んで ``Future`` を返す（``fn`` は書き手スレッドで ``conn`` を受けて実行される）。

        キューが満杯なら空くまで呼び出しスレッドを止める。イベントループからは ``asubmit`` を使うこと。
        ``close()`` の後は ``WriterClosedError``。
        """
        return self._submit(fn, bumps_version, label, block=True)

    async def asubmit[T](
        self, fn: Callable[[sqlite3.Connection], T], *, bumps_version: bool, label: str
    ) -> T:
        """``submit`` して結果を ``await`` する。キューが満杯でもイベントループを止めない。

        待っているタスクが取り消されたとき、ジョブがまだ始まっていなければ実行しない。
        """
        try:
            future = self._submit(fn, bumps_version, label, block=False)
        except queue.Full:
            future = await asyncio.to_thread(self._submit, fn, bumps_version, label, block=True)
        return await asyncio.wrap_future(future)

    def _submit[T](
        self, fn: Callable[[sqlite3.Connection], T], bumps_version: bool, label: str, *, block: bool
    ) -> Future[T]:
        future: Future[T] = Future()
        job = _Job(fn, bumps_version, label, future)
        with self._cond:
            if not self._accepting:
                raise WriterClosedError(f"index writer is closed (job {label!r})")
            self._pending += 1
        try:
            # 書き手スレッド自身が（完了コールバックなどから）積むときは、満杯で待つと自分で詰まる
            self._queue.put(job, block=block and threading.current_thread() is not self._thread)
        except BaseException:
            self._finish_one()
            raise
        return future

    def next_edit_id(self) -> str:
        """編集 ID を 1 つ払い出す（``ed_0001`` 形式）。``_meta.edit_seq`` を +1 するので、再起動をまたいで単調増加。

        キューの順に実行されるため、キューが混んでいると待つ。イベントループからは ``anext_edit_id``。
        書き手スレッドの中（ジョブの ``fn`` や完了コールバック）から呼ぶと自分を待って止まるので ``RuntimeError``。
        """
        if threading.current_thread() is self._thread:
            raise RuntimeError("next_edit_id() must not be called from the writer thread")
        seq = self.submit(_bump_edit_seq, bumps_version=False, label="edit_seq").result()
        return format_edit_id(seq)

    async def anext_edit_id(self) -> str:
        seq = await self.asubmit(_bump_edit_seq, bumps_version=False, label="edit_seq")
        return format_edit_id(seq)

    def data_version(self) -> int:
        """現在の ``_meta.data_version``（コミット済みの値）。通常はメモリの値を返し、DB は読まない。

        自分の書き手のコミットは覚えた値に入っている。別の接続（同じ根を開いた別のプロセス）のコミットは、前回見に行ってから
        ``external_check_s`` 以上たっていれば ``PRAGMA data_version`` で調べ、変わっていたときだけ ``_meta`` を読み直す
        （呼び出しの頻度に関わらず、見に行くのは間隔に 1 回。ほかのスレッドが見に行っている最中は待たずに覚えた値）。
        カーソルとキャッシュの無効化に使う。ツールの読み取りの中で、読んだ行と同じ断面の値が要るときは、借りている接続で
        ``connection.read_data_version(conn)`` を呼ぶこと。
        """
        if not self._accepting:
            raise WriterClosedError("index writer is closed")
        if time.monotonic() - self._probe_at >= self._external_check_s:
            self._refresh_from_other_connections()
        return self._data_version

    def _refresh_from_other_connections(self) -> None:
        if not self._version_lock.acquire(blocking=False):
            return  # ほかのスレッドが見に行っている
        try:
            if not self._accepting:
                return
            self._probe_at = time.monotonic()
            try:
                if self._probe_conn is None:
                    self._probe_conn = open_reader(self._path)
                    self._probe_mark = None
                conn = self._probe_conn
                # 印を先に読む（この後のコミットは次の確認で見える）。印は自分のコミットでも変わる（別の接続なので）が、読み直すだけ
                mark = int(conn.execute("PRAGMA data_version").fetchone()[0])
                if mark != self._probe_mark:
                    self._data_version = read_data_version(conn)
                    self._probe_mark = mark
            except sqlite3.Error:
                _log.debug("data_version probe failed; keeping the remembered value", exc_info=True)
        finally:
            self._version_lock.release()

    # --- 終了 -------------------------------------------------------------------------------

    def close(self) -> None:
        """新規の投入を止め、積まれたジョブを最後まで実行してから、WAL を ``TRUNCATE`` して止まる。2 回呼んでよい。"""
        if threading.current_thread() is self._thread:
            raise RuntimeError("close() must not be called from the writer thread")
        with self._cond:
            first = self._accepting
            self._accepting = False
            while self._pending > 0 and self._thread.is_alive():
                self._cond.wait(0.1)
        with self._version_lock:  # 検知用の接続は、最後の接続を閉じる書き手より先に閉じる（-wal / -shm が消えるように）
            probe, self._probe_conn = self._probe_conn, None
        if probe is not None:
            probe.close()
        if first and self._thread.is_alive():
            self._queue.put(None)
        self._thread.join()

    def __enter__(self) -> IndexWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- 書き手スレッド -----------------------------------------------------------------------

    def _finish_one(self) -> None:
        with self._cond:
            self._pending -= 1
            self._cond.notify_all()

    def _run(self) -> None:
        try:
            conn = open_writer(self._path)
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            return
        try:
            self.schema_action = ensure_schema(conn)
        except BaseException as exc:
            self._start_error = exc
            conn.close()
            self._ready.set()
            return
        try:
            self._data_version = read_data_version(conn)
        except BaseException as exc:
            self._start_error = exc
            conn.close()
            self._ready.set()
            return
        self._ready.set()
        try:
            self._loop(conn)
        finally:
            with self._cond:
                self._accepting = False
            self._fail_remaining()
            conn.close()

    def _loop(self, conn: sqlite3.Connection) -> None:
        commits = 0  # 前回のチェックポイントからのコミット数
        while True:
            try:
                job = self._queue.get(timeout=IDLE_CHECKPOINT_S)
            except queue.Empty:
                if commits:  # 暇なとき
                    self._checkpoint(conn, "PASSIVE")
                    commits = 0
                continue
            if job is None:
                break
            if self._execute(conn, job):
                commits += 1
            self._finish_one()
            if commits >= CHECKPOINT_EVERY_COMMITS and self._queue.empty():
                self._checkpoint(conn, "PASSIVE")
                commits = 0
        self._checkpoint(conn, "TRUNCATE")

    def _execute(self, conn: sqlite3.Connection, job: _Job) -> bool:
        """1 ジョブを 1 トランザクションで実行する。コミットしたら ``True``。結果は Future に載せる。"""
        future = job.future
        if not future.set_running_or_notify_cancel():
            return False  # 始まる前に取り消された
        started = time.perf_counter()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = job.fn(conn)
            if not conn.in_transaction:
                raise RuntimeError(f"write job {job.label!r} ended the transaction itself")
            committed_version: int | None = None
            if job.bumps_version:
                conn.execute("UPDATE _meta SET data_version = data_version + 1")
                committed_version = read_data_version(conn)
            conn.execute("COMMIT")
            if committed_version is not None:
                with self._version_lock:  # 結果を載せる前に（待っている側が新しい値を見る）
                    self._data_version = committed_version
        except BaseException as exc:
            self._rollback(conn, job.label)
            _log.error("write job %r failed and was rolled back", job.label, exc_info=exc)
            future.set_exception(exc)
            return False
        _log.debug("write job %r committed in %.1f ms", job.label, (time.perf_counter() - started) * 1000)
        future.set_result(result)
        return True

    @staticmethod
    def _rollback(conn: sqlite3.Connection, label: str) -> None:
        if not conn.in_transaction:
            return
        try:
            conn.execute("ROLLBACK")
        except Exception:
            _log.exception("ROLLBACK failed after write job %r", label)

    @staticmethod
    def _checkpoint(conn: sqlite3.Connection, mode: str) -> None:
        try:
            busy, log_frames, checkpointed = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
            _log.debug(
                "wal_checkpoint(%s): busy=%s log=%s checkpointed=%s", mode, busy, log_frames, checkpointed
            )
        except Exception:
            _log.warning(
                "wal_checkpoint(%s) failed", mode, exc_info=True
            )  # 読み手が止めているだけのこともある

    def _fail_remaining(self) -> None:
        """スレッドが止まるとき、実行されなかったジョブの Future を失敗させる（待ち続けさせない）。"""
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            if job is not None and job.future.set_running_or_notify_cancel():
                job.future.set_exception(WriterClosedError(f"index writer stopped (job {job.label!r})"))
            if job is not None:
                self._finish_one()
