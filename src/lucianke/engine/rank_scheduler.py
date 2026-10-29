"""PageRank の再計算のデバウンス（監視による 1 ファイルの更新の後に、まとめて 1 回だけ計算し直す）。

``Indexer.scan`` は全体の取り込みの後に PageRank を 1 回書く。一方、監視や編集ツールによる ``index_file`` / ``remove`` は
1 ファイルずつ来るので、そのたびに全体を計算し直すと重い。そこで「最後の更新から ``delay`` 秒のあいだ、次の更新が来なければ
1 回だけ再計算する」デバウンスを挟む（連続 10 回の更新で再計算は 1 回）。

* ``touch()``: 更新があったことを知らせる。待ちの途中なら起算し直す。再計算の最中なら、終わった後にもう一度
  （もう一度 ``delay`` 待ってから）計算し直す（計算の途中の取り消しはしない。書き込みジョブを中途半端に止めないため）。
* ``aclose()``: 待ちも再計算も取り消して、終わるまで待つ。閉じた後の ``touch()`` は何もしない。
* 待ちは ``sleep`` を差し替えられる（テストは固定の ``sleep`` に頼らず、待ちを自分で解く）。
* 計算そのもの（``refresh``）は呼び出し側が渡す。``Indexer`` は読み取り接続のスレッドで計算し、書き込みは短いジョブ 1 つで、
  ``data_version`` は進めない（``Indexer.refresh_ranks``）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

__all__ = ["RANK_DEBOUNCE_S", "RankScheduler"]

_log = logging.getLogger(__name__)

RANK_DEBOUNCE_S: Final = 1.0
"""最後の更新から再計算までの待ち（秒）。"""


class RankScheduler:
    """デバウンス付きの再計算の予約。イベントループの中で使う（``touch`` はループが回っているスレッドから呼ぶ）。"""

    def __init__(
        self,
        refresh: Callable[[], Awaitable[object]],
        *,
        delay: float = RANK_DEBOUNCE_S,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._refresh = refresh
        self._delay = delay
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._refreshing = False
        self._dirty = False
        self._closed = False
        self.runs = 0  # ``refresh`` を呼んだ回数（テストと計測用）

    @property
    def pending(self) -> bool:
        """待ちの途中か、再計算の最中。"""
        return self._task is not None and not self._task.done()

    def touch(self) -> None:
        """更新があった。最後の ``touch`` から ``delay`` 秒後に 1 回だけ再計算する。"""
        if self._closed:
            return
        if self._refreshing:
            self._dirty = True  # 計算の最中。終わった後にもう一度
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # ループの外から呼ばれた（想定外）。順位は付加情報なので何もしない
            _log.debug("rank refresh not scheduled: no running event loop")
            return
        if self._task is not None and not self._task.done():
            self._task.cancel()  # 待ちの途中だけがここに来る（再計算の最中は上で返している）。起算し直す
        self._task = loop.create_task(self._run(), name="lucianke-rank-refresh")

    async def idle(self) -> None:
        """待ちも再計算も無くなるまで待つ（テストと終了処理用。待ちは ``sleep`` が返るまで続く）。"""
        while (task := self._task) is not None and not task.done():
            await asyncio.wait({task})

    async def aclose(self) -> None:
        """予約を取り消し、走っていれば止めて、終わるまで待つ。"""
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    async def _run(self) -> None:
        await self._sleep(self._delay)
        while True:
            # ここから ``await self._refresh()`` の終わりまで ``touch`` は取り消さない（``_refreshing``）。
            # ``sleep`` から戻ってここまでに ``await`` は無いので、``touch`` が割り込む隙間は無い
            self._refreshing = True
            self._dirty = False
            self.runs += 1
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("pagerank refresh failed", exc_info=True)
            finally:
                self._refreshing = False
            if not self._dirty or self._closed:
                return
            await self._sleep(self._delay)  # 計算の最中に更新が来ていた。もう一度まとめて待ってから
