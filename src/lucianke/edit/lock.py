"""ワークスペース単位の書き込みロック（設計書 §6.3、仕様書 §5.9）。

編集は ``WorkspaceLocks.acquire`` の下で 1 度に 1 トランザクションだけが走る。待ち上限（既定
``EDIT_LOCK_WAIT_MS`` = 5000 ms）を超えたら ``E_TIMEOUT``。読み取りは ``wait_idle`` で書き込みの解放を待ち、
編集確定後の状態を見る。ワークスペース ID が違えば互いに待たない。待ち順は FIFO（``asyncio.Lock`` の性質）。

ロックの順序は「書き込みロック → ``TreeCache`` → LSP 要求」で、逆順に取らない（設計書 §6.3）。
``acquire`` はリエントラントではない。同じタスクが同じワークスペースで入れ子に取ると自分自身を待つ。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import EDIT_LOCK_WAIT_MS

__all__ = ["WorkspaceLocks"]


class WorkspaceLocks:
    """ワークスペース ID ごとの ``asyncio.Lock``。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, ws_id: str) -> asyncio.Lock:
        lock = self._locks.get(ws_id)
        if lock is None:
            lock = self._locks[ws_id] = asyncio.Lock()
        return lock

    def is_locked(self, ws_id: str) -> bool:
        lock = self._locks.get(ws_id)
        return lock is not None and lock.locked()

    @asynccontextmanager
    async def acquire(self, ws_id: str, *, timeout_ms: int = EDIT_LOCK_WAIT_MS) -> AsyncIterator[None]:
        """書き込みロックを取る。``timeout_ms`` を超えて待ったら ``E_TIMEOUT``（ロックは取れていない）。"""
        lock = self._lock_for(ws_id)
        try:
            # ``await`` を挟まずに ``async with`` を抜けるので、取得と期限切れが競合して取りこぼすことはない
            async with asyncio.timeout(max(timeout_ms, 0) / 1000):
                await lock.acquire()
        except TimeoutError:
            raise LuciankeError(
                ErrorCode.E_TIMEOUT,
                f"another edit holds the workspace write lock; waited {timeout_ms} ms",
                details={"reason": "edit_lock_wait", "workspace_id": ws_id, "waited_ms": timeout_ms},
            ) from None
        try:
            yield
        finally:
            lock.release()

    async def wait_idle(self, ws_id: str) -> None:
        """書き込みロックが空くまで待つ（読み取りが編集確定後の状態を見るため）。ロックは保持しない。"""
        lock = self._locks.get(ws_id)
        if lock is None or not lock.locked():
            return
        async with lock:
            pass
