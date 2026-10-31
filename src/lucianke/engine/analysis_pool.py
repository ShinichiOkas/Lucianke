"""走査のファイルの解析を子プロセスに並べる（BUG-0127。設計書 §6.1）。

構文解析と関係の抽出は GIL を握る Python のコードで、1 プロセスの中では並ばない。4,000 ファイル級のワークスペースの初回の索引では
解析だけで 2 分を超え、同じプロセスの書き手スレッド（SQLite の小さな文を何十万回も発行する。文ごとに GIL を手放して取り戻す）と
GIL を取り合って、重ねたほうが順に流すより遅くなっていた（convoy。core/foreground.py）。

* ``AnalysisPool``: ``spawn`` の子プロセスの組（``concurrent.futures.ProcessPoolExecutor``）。走査ごとに起こして走査の終わりに畳む
  （起こすのは解析の要るファイルが ``POOL_MIN_FILES`` に達してから。差分の小さな走査では子プロセスを起こさない）。
  起こせない・子が落ちた（``BrokenProcessPool``）ときは ``broken`` になり、呼び手は残りを同じプロセスで解析する（結果は同じ）。
* 子プロセスの数（``default_workers``）: 環境変数 ``LUCIANKE_INDEX_WORKERS``（0 で使わない）、無ければ論理 CPU の半分（最大
  ``MAX_WORKERS``）。残りの CPU は LSP（別プロセス）とツールの呼び出しに残す。
* 子の標準出力: ``bootstrap_io`` が fd 1 を stderr に重ねた後に起こすので、子が書いても stderr に行く（stdout は JSON-RPC 専用。
  設計書 ADR-3）。exe（PyInstaller）は ``__main__`` の ``freeze_support`` が子の起動を受ける。

このモジュールは解析の中身を知らない（子で走る関数と初期化は呼び手が渡す。``engine/indexer.py``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import os
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Final

__all__ = [
    "MAX_WORKERS",
    "POOL_MIN_FILES",
    "WORKERS_ENV",
    "AnalysisPool",
    "default_workers",
]

_log = logging.getLogger(__name__)

WORKERS_ENV: Final = "LUCIANKE_INDEX_WORKERS"
MAX_WORKERS: Final = 8
POOL_MIN_FILES: Final = 64
"""走査で解析の要るファイルがこの数に達したら子プロセスを起こす（起こす費用は Windows で 1〜2 秒。小さな差分の走査では割に合わない）。"""
IN_FLIGHT_PER_WORKER: Final = 8
"""子 1 つあたり、結果を待たずに先に渡しておくファイルの数（子を遊ばせない。結果は渡した順に受け取る）。"""


def default_workers(env: dict[str, str] | None = None, cpu_count: int | None = None) -> int:
    """走査の解析に使う子プロセスの数。``LUCIANKE_INDEX_WORKERS`` があればそれ（0 は使わない）、無ければ論理 CPU の半分（最大 ``MAX_WORKERS``）。

    論理 CPU が 2 以下なら 0（子に分けても速くならない）。
    """
    raw = (os.environ if env is None else env).get(WORKERS_ENV, "").strip()
    if raw:
        try:
            return max(0, min(MAX_WORKERS * 4, int(raw)))
        except ValueError:
            _log.warning("ignoring %s=%r (not an integer)", WORKERS_ENV, raw)
    cpus = os.cpu_count() if cpu_count is None else cpu_count
    if not cpus or cpus <= 2:
        return 0
    return min(MAX_WORKERS, cpus // 2)


class AnalysisPool:
    """走査 1 回分の解析の子プロセス。``start`` は最初の ``submit`` で呼ばれる（呼び手が明示してもよい）。

    Args:
        workers: 子プロセスの数（1 以上）。
        initializer: 子の起動時に 1 回呼ぶ関数（モジュールの最上位の関数。``spawn`` で引けるもの）。
        initargs: ``initializer`` の引数（pickle できるもの）。
    """

    def __init__(self, workers: int, initializer: Callable[..., None], initargs: tuple[Any, ...]) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self.workers = workers
        self._initializer = initializer
        self._initargs = initargs
        self._executor: ProcessPoolExecutor | None = None
        self.broken = False
        self.submitted = 0

    @property
    def window(self) -> int:
        """結果を待たずに渡しておくファイルの数。"""
        return self.workers * IN_FLIGHT_PER_WORKER

    def start(self) -> bool:
        """子プロセスの組を作る（子は最初の仕事で起きる）。作れなければ ``broken`` にして ``False``。"""
        if self._executor is not None:
            return True
        if self.broken:
            return False
        try:
            # fork はスレッドのあるプロセスでは安全でない（書き手・イベントループのスレッドがある）。どの OS でも spawn
            self._executor = ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=self._initializer,
                initargs=self._initargs,
            )
        except Exception:  # noqa: BLE001  起こせなければ同じプロセスで解析する
            _log.warning("could not start the analysis worker processes", exc_info=True)
            self.broken = True
            return False
        _log.info("analysis worker processes: %d", self.workers, extra={"event": "analysis_pool_start"})
        return True

    def submit(self, fn: Callable[..., Any], *args: Any) -> asyncio.Future[Any] | None:
        """子で ``fn(*args)`` を走らせる。``None`` は渡せなかった（``broken``。呼び手が同じプロセスで解析する）。"""
        if not self.start():
            return None
        assert self._executor is not None
        try:
            future: Future[Any] = self._executor.submit(fn, *args)
        except (BrokenProcessPool, RuntimeError):
            self.mark_broken()
            return None
        self.submitted += 1
        return asyncio.wrap_future(future)

    def mark_broken(self) -> None:
        """子が落ちた（``BrokenProcessPool``）。以降は渡さない。"""
        if not self.broken:
            _log.warning("analysis worker processes broke; analysing the rest in-process")
        self.broken = True

    def failed(self, exc: BaseException) -> None:
        """渡した仕事が例外で返った。子が落ちたのなら（``BrokenProcessPool``）以降は渡さない。"""
        if isinstance(exc, BrokenProcessPool):
            self.mark_broken()

    async def aclose(self) -> None:
        """子プロセスを畳む（待っている仕事は取り消す）。取り消されても畳み終えるまで待ち、その後で ``CancelledError`` を送り直す。"""
        executor, self._executor = self._executor, None
        if executor is None:
            return
        task = asyncio.ensure_future(asyncio.to_thread(executor.shutdown, True, cancel_futures=True))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True  # 畳み終えてから取り消しを伝える（子プロセスを残さない）
        with contextlib.suppress(Exception):
            task.result()
        if cancelled:
            raise asyncio.CancelledError
