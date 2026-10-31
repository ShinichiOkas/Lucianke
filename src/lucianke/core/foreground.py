"""前景（ツールの呼び出し）を背景の索引より先に通す（BUG-0112。設計書 §6.1）。

索引の取り込み（構文解析・関係の抽出・関係の整え・順位の計算）は CPU を使う Python のコードで、GIL を握る。同じプロセスの
ツールの呼び出しが小さな I/O（``stat``・ファイルの読み込み・SQLite の 1 文）を重ねると、I/O のたびに GIL を手放し、取り戻すのに
切り替え間隔（5 ms）ずつ待たされる（convoy）。大きなワークスペースの初回の索引の間、``configure_workspace status``（Git の状態を
調べる）が 1〜4 秒から 2 分以上に延びていた。

* ``Foreground.active()``: パイプラインがツールの呼び出しを囲む。囲みの中の呼び出しが 1 つでも始まってから ``window_s`` 秒以内なら
  「前景が忙しい」（``busy``）。窓を過ぎた呼び出し（LSP を待つ T-06 など）では背景を止めない（索引が止まり続けないように）。
* 1 回に譲るのは ``MAX_YIELD_S`` まで。前景が続いていれば、背景は ``RUN_SLICE_S`` だけ進んでからまた譲る（呼ぶ側が ``status`` を
  絶え間なく呼び続けても、索引は止まらずに 1/20 以上の速さで進む）。
* ``pause()`` / ``apause()``: 背景の索引が区切りごとに呼ぶ。前景が忙しい間、短く眠って GIL を譲る（スレッドは ``time.sleep``、
  イベントループは ``asyncio.sleep``）。背景の印（``background()``）の無い文脈では何もしない（ツールが同じ抽出の関数を前景で呼んでも、
  自分自身に譲って止まらない）。``asyncio.to_thread`` は文脈を写すので、走査のタスクで印をつければ、解析のスレッドにも印が付く。
* 書き手スレッド（``IndexWriter``）のジョブの中では譲らない: 前景の書き込み（先取り・編集）がそのジョブの後ろで待っているかもしれない。
  背景の書き込みは短いジョブに割り、ジョブの間（イベントループ側）で譲る。
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Final

__all__ = [
    "FOREGROUND",
    "MAX_YIELD_S",
    "PRIORITY_WINDOW_S",
    "RUN_SLICE_S",
    "YIELD_SLICE_S",
    "Foreground",
    "background",
    "cooperate",
]

PRIORITY_WINDOW_S: Final = 10.0
"""呼び出しが始まってからこの秒数のあいだ、背景の索引は区切りごとに手を止めて譲る。"""

YIELD_SLICE_S: Final = 0.005
"""譲るときに 1 回に眠る長さ（前景が終わったら、この程度の遅れで背景が戻る）。"""

MAX_YIELD_S: Final = 2.0
"""背景が 1 回に譲る長さの上限（たいていの呼び出しはこの間に終わり、その間は背景に一度も邪魔されない）。"""

RUN_SLICE_S: Final = 0.1
"""上限まで譲っても前景が続くとき、背景が次に譲るまでに進む長さ（背景が飢えないように）。"""

_BACKGROUND: contextvars.ContextVar[bool] = contextvars.ContextVar("lucianke_background", default=False)


@contextmanager
def background() -> Iterator[None]:
    """この文脈（とそこから ``asyncio.to_thread`` で起こしたスレッド）の仕事を背景にする（``pause`` が譲るようになる）。"""
    token = _BACKGROUND.set(True)
    try:
        yield
    finally:
        _BACKGROUND.reset(token)


class Foreground:
    """前景の呼び出しの登録と、背景が譲るための待ち。スレッド安全。"""

    def __init__(
        self,
        *,
        window_s: float = PRIORITY_WINDOW_S,
        max_yield_s: float = MAX_YIELD_S,
        run_slice_s: float = RUN_SLICE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_s
        self._max_yield = max_yield_s
        self._run_slice = run_slice_s
        self._run_until = 0.0  # これより前は背景が譲らずに進む（上限まで譲った後の番）
        self._clock = clock
        self._lock = threading.Lock()
        self._starts: dict[int, float] = {}
        self._seq = itertools.count()
        self.paused_s = 0.0  # 背景が譲って眠った合計（観測用）

    @contextmanager
    def active(self) -> Iterator[None]:
        """前景の呼び出し 1 つを囲む。"""
        token = next(self._seq)
        with self._lock:
            self._starts[token] = self._clock()
        try:
            yield
        finally:
            with self._lock:
                self._starts.pop(token, None)

    def busy(self) -> bool:
        """始まってから ``window_s`` 秒以内の前景の呼び出しがあるか。"""
        if not self._starts:
            return False
        now = self._clock()
        with self._lock:
            return any(now - t < self._window for t in self._starts.values())

    def _should_wait(self) -> bool:
        return bool(self._starts) and _BACKGROUND.get() and self._clock() >= self._run_until and self.busy()

    def _still_yielding(self, t0: float) -> bool:
        """譲り続けるか。上限まで譲ったら背景の番（``RUN_SLICE_S``）にして ``False``。"""
        if not self.busy():
            return False
        now = self._clock()
        if now - t0 >= self._max_yield:
            self._run_until = now + self._run_slice
            return False
        return True

    def pause(self) -> float:
        """（背景のスレッドから）前景が忙しい間、短く眠って譲る（最長 ``MAX_YIELD_S``）。眠った秒数。背景の印が無ければすぐ戻る。"""
        if not self._should_wait():
            return 0.0
        t0 = self._clock()
        while self._still_yielding(t0):
            time.sleep(YIELD_SLICE_S)
        slept = self._clock() - t0
        self.paused_s += slept
        return slept

    async def apause(self) -> float:
        """（背景のタスクから）前景が忙しい間、イベントループを止めずに待つ（最長 ``MAX_YIELD_S``）。待った秒数。"""
        if not self._should_wait():
            return 0.0
        t0 = self._clock()
        while self._still_yielding(t0):
            await asyncio.sleep(YIELD_SLICE_S)
        slept = self._clock() - t0
        self.paused_s += slept
        return slept


FOREGROUND: Final = Foreground()
"""プロセスに 1 つの前景の登録（パイプラインが登録し、索引が譲る）。"""


def cooperate() -> None:
    """背景の CPU を使う繰り返しの 1 回ごとに呼ぶ（前景が無ければほぼ費用なし。あれば ``FOREGROUND.pause``）。"""
    if FOREGROUND._starts:  # noqa: SLF001  毎回の呼び出しを安く保つ（ロックも文脈も見ない）
        FOREGROUND.pause()
