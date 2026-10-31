"""core.foreground のテスト（BUG-0112）: 前景（ツールの呼び出し）の間、背景の索引が区切りごとに譲る。"""

from __future__ import annotations

import asyncio
import threading
import time

from lucianke.core.foreground import Foreground, background


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_busy_only_within_the_window_of_a_running_call() -> None:
    clock = Clock()
    fg = Foreground(window_s=10.0, clock=clock)
    assert not fg.busy()
    with fg.active():
        assert fg.busy()
        clock.now += 9.9
        assert fg.busy()
        clock.now += 0.2  # 窓を過ぎた呼び出し（LSP を待つ T-06 など）では背景を止めない
        assert not fg.busy()
        with fg.active():  # 新しい呼び出しは再び忙しい
            assert fg.busy()
    assert not fg.busy()


def test_pause_is_a_no_op_outside_background_work() -> None:
    """背景の印の無い文脈（前景のツール自身が同じ抽出を呼んだとき）では譲らない（自分を待って止まらない）。"""
    fg = Foreground(window_s=60.0)
    with fg.active():
        t0 = time.perf_counter()
        assert fg.pause() == 0.0
        assert asyncio.run(fg.apause()) == 0.0
        assert time.perf_counter() - t0 < 0.5


def test_background_thread_waits_until_the_foreground_call_ends() -> None:
    fg = Foreground(window_s=60.0)
    resumed = threading.Event()
    slept: list[float] = []

    def worker() -> None:
        with background():
            slept.append(fg.pause())
        resumed.set()

    with fg.active():
        t = threading.Thread(target=worker)
        t.start()
        assert not resumed.wait(0.3)  # 前景の間は眠っている
    assert resumed.wait(5.0)  # 前景が終わると戻る
    t.join()
    assert slept[0] >= 0.25 and fg.paused_s >= 0.25


def test_background_task_waits_and_to_thread_inherits_the_mark() -> None:
    fg = Foreground(window_s=60.0)

    async def main() -> tuple[float, float]:
        with background():
            async def scan() -> tuple[float, float]:
                return await fg.apause(), await asyncio.to_thread(fg.pause)  # to_thread は文脈（背景の印）を写す

            with fg.active():
                task = asyncio.create_task(scan())
                await asyncio.sleep(0.2)
                assert not task.done()
            return await asyncio.wait_for(task, 5.0)

    waited, _ = asyncio.run(main())
    assert waited >= 0.15


def test_window_bounds_the_wait() -> None:
    fg = Foreground(window_s=0.2)
    with fg.active():
        with background():
            t0 = time.perf_counter()
            fg.pause()
            assert 0.1 <= time.perf_counter() - t0 < 2.0  # 窓を過ぎたら、前景が続いていても戻る


def test_background_is_not_starved_by_back_to_back_calls() -> None:
    """前景が絶え間なく続いても（``status`` を呼び続けるクライアント）、背景は上限まで譲るごとに少しずつ進む。"""
    fg = Foreground(window_s=60.0, max_yield_s=0.1, run_slice_s=0.05)
    runs = 0
    slept = 0.0
    with fg.active(), background():
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 1.0:
            got = fg.pause()
            slept += got
            if got == 0.0:
                runs += 1
                time.sleep(0.001)  # 背景の仕事の 1 区切り
    assert runs > 0  # 進んだ
    assert 0.4 < slept < 0.95  # 譲った時間が大半（上限 0.1 秒・番 0.05 秒なら約 2/3）
