"""``engine.rank_scheduler.RankScheduler``: PageRank の再計算のデバウンス。

固定の ``sleep`` に頼らない。待ちは ``GatedSleep``（テストが解くまで返らない）に差し替え、再計算は偽物で数える。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from lucianke.engine.rank_scheduler import RANK_DEBOUNCE_S, RankScheduler


class GatedSleep:
    """``asyncio.sleep`` の代わり。``release()`` が呼ばれるまで返らない（呼ばれた時点で待っている全員を解く）。"""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._gates: list[asyncio.Event] = []

    async def __call__(self, delay: float) -> None:
        gate = asyncio.Event()
        self.delays.append(delay)
        self._gates.append(gate)
        await gate.wait()

    def release(self) -> None:
        for gate in self._gates:
            gate.set()
        self._gates.clear()


class Refresher:
    """再計算の偽物。回数を数え、``block`` を立てると ``unblock()`` まで終わらない。"""

    def __init__(self, *, block: bool = False) -> None:
        self.calls = 0
        self.finished = 0
        self.cancelled = 0
        self.started = asyncio.Event()
        self._gate = asyncio.Event()
        if not block:
            self._gate.set()

    async def __call__(self) -> None:
        self.calls += 1
        self.started.set()
        try:
            await self._gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.finished += 1

    def unblock(self) -> None:
        self._gate.set()


async def test_ten_touches_make_one_refresh() -> None:
    sleep = GatedSleep()
    refresh = Refresher()
    sched = RankScheduler(refresh, delay=1.0, sleep=sleep)
    for _ in range(10):
        sched.touch()
        await asyncio.sleep(0)  # 待ちに入らせる（時間は経たない）
    assert sched.pending and refresh.calls == 0
    assert sleep.delays == [1.0] * 10  # 起算し直しのたびに待ち直す（前の待ちは取り消される）
    sleep.release()
    await sched.idle()
    assert refresh.calls == 1 and refresh.finished == 1 and sched.runs == 1
    assert not sched.pending
    await sched.aclose()


async def test_touch_after_a_refresh_starts_a_new_cycle() -> None:
    sleep = GatedSleep()
    refresh = Refresher()
    sched = RankScheduler(refresh, sleep=sleep)
    sched.touch()
    await asyncio.sleep(0)
    sleep.release()
    await sched.idle()
    assert refresh.calls == 1
    sched.touch()
    await asyncio.sleep(0)
    assert refresh.calls == 1  # 待ち直し
    sleep.release()
    await sched.idle()
    assert refresh.calls == 2
    await sched.aclose()


async def test_touch_during_a_refresh_runs_once_more_after_another_delay() -> None:
    sleep = GatedSleep()
    refresh = Refresher(block=True)
    sched = RankScheduler(refresh, sleep=sleep)
    sched.touch()
    await asyncio.sleep(0)
    sleep.release()
    await refresh.started.wait()  # 再計算の最中
    for _ in range(5):
        sched.touch()  # 取り消さない・何本も起こさない
    assert refresh.calls == 1 and refresh.cancelled == 0
    refresh.unblock()
    for _ in range(5):  # 終わった後、もう一度待ちに入る
        await asyncio.sleep(0)
    assert refresh.finished == 1 and refresh.calls == 1 and sched.pending
    assert len(sleep.delays) == 2
    sleep.release()
    await sched.idle()
    assert refresh.calls == 2 and refresh.finished == 2 and sched.runs == 2
    await sched.aclose()


async def test_aclose_cancels_a_pending_wait() -> None:
    sleep = GatedSleep()
    refresh = Refresher()
    sched = RankScheduler(refresh, sleep=sleep)
    sched.touch()
    await asyncio.sleep(0)
    assert sched.pending
    await sched.aclose()
    assert not sched.pending
    sleep.release()
    await asyncio.sleep(0)
    assert refresh.calls == 0
    sched.touch()  # 閉じた後は何もしない
    await asyncio.sleep(0)
    assert not sched.pending and sleep.delays == [RANK_DEBOUNCE_S]


async def test_aclose_cancels_a_running_refresh() -> None:
    sleep = GatedSleep()
    refresh = Refresher(block=True)
    sched = RankScheduler(refresh, sleep=sleep)
    sched.touch()
    await asyncio.sleep(0)
    sleep.release()
    await refresh.started.wait()
    await sched.aclose()
    assert refresh.cancelled == 1 and refresh.finished == 0
    assert not sched.pending


async def test_a_failing_refresh_is_logged_and_the_next_touch_still_works(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleep = GatedSleep()
    calls: list[int] = []

    async def refresh() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    sched = RankScheduler(refresh, sleep=sleep)
    with caplog.at_level(logging.WARNING, logger="lucianke.engine.rank_scheduler"):
        sched.touch()
        await asyncio.sleep(0)
        sleep.release()
        await sched.idle()
    assert calls == [1] and any("pagerank refresh failed" in r.message for r in caplog.records)
    sched.touch()
    await asyncio.sleep(0)
    sleep.release()
    await sched.idle()
    assert calls == [1, 1]
    await sched.aclose()


async def test_touch_without_a_running_loop_is_harmless() -> None:
    sched = RankScheduler(Refresher(), sleep=GatedSleep())
    await asyncio.to_thread(sched.touch)  # ループの無いスレッドから
    assert not sched.pending
    await sched.aclose()


async def test_real_sleep_default_runs_after_the_delay() -> None:
    """既定の ``asyncio.sleep`` でも動く（待ちを実際に取る唯一のテスト。0.01 秒）。"""
    refresh = Refresher()
    sched = RankScheduler(refresh, delay=0.01)
    sched.touch()
    await asyncio.wait_for(sched.idle(), timeout=5)
    assert refresh.calls == 1
    await sched.aclose()
