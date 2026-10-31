"""T-19 の ``COUNT(*)`` の上限の見張り（BUG-0128）。

以前は ``COUNT(*)`` のたびに ``threading.Timer`` を作って ``join`` していた（1 回の呼び出しで 10 本）。CPU が混むとスレッドの
起動・終わりの待ちのたびに順番待ちになり、16 ms の呼び出しが 120 ms〜1 秒に延びた。見張りはプロセスで 1 本を共有する。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from lucianke.tools import t19_get_project_db_schema as t19


class _Blocking:
    """``count`` の文が ``interrupt()`` されるまで（最大 ``hold_s`` 秒）止まる接続の代わり。"""

    def __init__(self, hold_s: float) -> None:
        self.hold_s = hold_s
        self.interrupted = threading.Event()

    def interrupt(self) -> None:
        self.interrupted.set()

    def execute(self, sql: str):
        class Cur:
            def __init__(self, row: tuple[int]) -> None:
                self.row = row

            def fetchone(self) -> tuple[int]:
                return self.row

        if sql.startswith("SELECT count"):
            if self.interrupted.wait(self.hold_s):
                raise sqlite3.OperationalError("interrupted")
            return Cur((7,))
        return Cur((99,))


def test_a_count_past_its_budget_is_interrupted_and_falls_back_to_max_rowid() -> None:
    con = _Blocking(hold_s=5.0)
    t0 = time.monotonic()
    assert t19._count_rows(con, "symbols", budget_s=0.05) == (99, False)  # type: ignore[arg-type]
    assert time.monotonic() - t0 < 2.0  # 5 秒待たずに止まった


def test_a_count_within_its_budget_is_exact_and_no_late_interrupt_arrives() -> None:
    con = _Blocking(hold_s=0.0)  # すぐ終わる
    assert t19._count_rows(con, "symbols", budget_s=0.05) == (7, True)  # type: ignore[arg-type]
    time.sleep(0.2)  # 期限を過ぎても、終わった後の interrupt() は届かない
    assert not con.interrupted.is_set()


def test_counts_do_not_start_a_thread_per_call(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    with sqlite3.connect(db) as w:
        w.execute("CREATE TABLE t (a INTEGER)")
        w.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    con = sqlite3.connect(db, check_same_thread=False)
    try:
        assert t19._count_rows(con, "t") == (100, True)  # 見張りが無ければここで 1 本起きる
        started: list[str] = []
        original = threading.Thread.start

        def spy(self: threading.Thread) -> None:
            started.append(self.name)
            original(self)

        threading.Thread.start = spy  # type: ignore[method-assign]
        try:
            for _ in range(30):
                assert t19._count_rows(con, "t") == (100, True)
        finally:
            threading.Thread.start = original  # type: ignore[method-assign]
        assert started == []
    finally:
        con.close()
