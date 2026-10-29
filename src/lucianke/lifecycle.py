"""終了処理の登録表 ``Lifecycle`` とシグナルの結線（設計書 §6.5・§8.7・§9.5・ADR-12・ADR-16、仕様書 §12.5）。

終了の契機は ``stdin`` の EOF、``SIGTERM`` / ``SIGINT``、Windows の ``SIGBREAK``。どれでも同じ手順になる:

1. 受付停止: ``shutting_down`` が真になり、パイプラインのガード（``make_shutdown_guard``）が新規 ``tools/call`` を
   ``E_UNSUPPORTED``（``details.reason="shutting_down"``）で拒否する。
2. 登録された段を ``order`` の昇順に 1 つずつ実行する。各段にはタイムアウトがあり、超えたら取り消して次へ進む。
   例外も記録して次へ進む（1 つの失敗が後ろの段、とくに子プロセスの回収を止めない）。同じ ``order`` は登録順。
3. 全部終わったら ``closed`` が真になる。

標準の段の番号は ``ShutdownOrder``: 10 受付停止 / 20 編集の完了待ち / 30 監視停止 / 40 LSP の ``shutdown_all`` /
50 ``ProcessRegistry`` の残りを kill / 60 ``IndexWriter`` の close（キューを空にしてチェックポイント）/ 70 ログの flush。
段を足すのは合成ルート（``app``）の仕事で、この module は何も import せずに登録だけを受ける。

組み込み方（``app.py`` から）::

    lifecycle = Lifecycle()
    lifecycle.add_shutdown_step("lsp", pool.shutdown_all, order=ShutdownOrder.LSP, timeout_ms=...)
    lifecycle.add_shutdown_step("children", make_terminate_children_step(registry), order=ShutdownOrder.KILL_CHILDREN, ...)
    pipeline = Pipeline(..., shutdown_guard=make_shutdown_guard(lifecycle))   # 真なら LuciankeError を投げる関数
    watch = install_signal_handlers(asyncio.get_running_loop(), lifecycle)
    try:
        await run_until_shutdown(lifecycle, serve())    # serve() が EOF で戻っても、シグナルでも、最後は shutdown が完了している
    finally:
        watch.close()

Windows では ``loop.add_signal_handler`` が使えない（Proactor ループ）ので、``signal.signal`` で合図を受けて
``request_shutdown`` を呼ぶだけにし（ハンドラの中では何も待たない）、ループ側の監視タスクが拾って ``shutdown`` を呼ぶ。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Literal

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import SHUTDOWN_GRACE_MS

if TYPE_CHECKING:
    from types import FrameType

    from lucianke.core.procs import ProcessRegistry

_log = logging.getLogger("lucianke.lifecycle")

StepFn = Callable[[], Awaitable[object]]
StepStatus = Literal["ok", "timeout", "error"]

# タイムアウトで取り消した段が取り消しを無視して居座っても、これだけ待って見切りをつけ、次の段へ進む。
_CANCEL_GRACE_S = 0.2
# 監視タスクが、起こされなくてもフラグを見に来る間隔（signal.signal のハンドラはループを確実には起こせない環境がある）。
_POLL_INTERVAL_S = 0.1


class ShutdownOrder(IntEnum):
    """標準の段の番号（設計書 §6.5 の終了順）。``order`` には ``int`` を渡せるので、間に挟む段は 25 などでよい。"""

    STOP_INTAKE = 10
    DRAIN_EDITS = 20
    STOP_WATCHER = 30
    LSP = 40
    KILL_CHILDREN = 50
    INDEX_CLOSE = 60
    FLUSH_LOGS = 70


# 段ごとの既定のタイムアウト（ms）。編集の完了待ちは設計書 §6.5 の「最大 5 s」。LSP と子プロセスは猶予 500 ms の後の kill と回収。
DEFAULT_TIMEOUT_MS: dict[ShutdownOrder, int] = {
    ShutdownOrder.STOP_INTAKE: 1000,
    ShutdownOrder.DRAIN_EDITS: 5000,
    ShutdownOrder.STOP_WATCHER: 1000,
    ShutdownOrder.LSP: 3000,
    ShutdownOrder.KILL_CHILDREN: 3000,
    ShutdownOrder.INDEX_CLOSE: 3000,
    ShutdownOrder.FLUSH_LOGS: 1000,
}


@dataclass(frozen=True, slots=True)
class ShutdownStep:
    """登録された 1 段。"""

    name: str
    fn: StepFn
    order: int
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class StepResult:
    """1 段の結果。``error`` は ``status="error"`` のときの例外（``timeout`` では ``None``）。"""

    name: str
    order: int
    status: StepStatus
    elapsed_ms: float
    error: BaseException | None = None


class Lifecycle:
    """終了処理の登録表。スレッド安全ではない（``request_shutdown`` だけはシグナルハンドラから呼べる）。"""

    def __init__(self) -> None:
        self._steps: list[ShutdownStep] = []
        self._shutting_down = False
        self._task: asyncio.Task[tuple[StepResult, ...]] | None = None
        self._closed = asyncio.Event()
        self._requested = asyncio.Event()
        self._reason: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- 状態 ---

    @property
    def shutting_down(self) -> bool:
        """終了が始まった（または要求された）か。真なら新規 ``tools/call`` を拒否する。一度真になったら戻らない。"""
        return self._shutting_down

    @property
    def closed(self) -> bool:
        """全部の段を実行し終えたか。"""
        return self._closed.is_set()

    @property
    def reason(self) -> str | None:
        """終了の理由（最初に要求された理由。``"stdin_eof"``、``"signal:SIGTERM"`` など）。まだなら ``None``。"""
        return self._reason

    @property
    def steps(self) -> tuple[ShutdownStep, ...]:
        """実行順（``order`` の昇順、同じなら登録順）に並べた登録表。"""
        return tuple(sorted(self._steps, key=lambda s: s.order))

    # --- 登録 ---

    def add_shutdown_step(self, name: str, fn: StepFn, *, order: int, timeout_ms: int) -> None:
        """終了処理の段を足す。``fn`` は引数なしで awaitable を返す関数（``async def`` など）。

        ``timeout_ms`` を超えたら ``fn`` を取り消して次の段へ進む。終了が始まった後の登録は ``RuntimeError``
        （実行されない段を黙って受け取らない）。同じ ``name`` の二重登録は ``ValueError``（結線の取り違え）。
        """
        if self._task is not None:
            raise RuntimeError(f"shutdown already started; cannot add step {name!r}")
        if any(s.name == name for s in self._steps):
            raise ValueError(f"duplicate shutdown step name: {name!r}")
        if timeout_ms <= 0:
            raise ValueError(f"timeout_ms must be positive: {name!r} {timeout_ms}")
        self._steps.append(ShutdownStep(name, fn, int(order), int(timeout_ms)))

    # --- 要求と実行 ---

    def request_shutdown(self, reason: str) -> None:
        """終了を要求する: ``shutting_down`` を真にして理由を記録し、ループを起こす。**同期で、何も待たない**。

        シグナルハンドラ（``signal.signal``）から呼んでよい（ループへの合図は ``call_soon_threadsafe``）。
        段の実行は ``shutdown`` の仕事で、これは走らせない。2 回目以降は理由を上書きしない。
        """
        if self._reason is None:
            self._reason = reason
        self._shutting_down = True
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):  # ループが閉じた後
                loop.call_soon_threadsafe(self._requested.set)

    async def wait_requested(self) -> str:
        """``request_shutdown`` が呼ばれるまで待ち、その理由を返す。"""
        self._bind_loop()
        while not self._shutting_down:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._requested.wait(), _POLL_INTERVAL_S)
        return self._reason or "unknown"

    async def wait_closed(self) -> None:
        """全部の段を実行し終えるまで待つ。"""
        await self._closed.wait()

    async def shutdown(self, reason: str) -> tuple[StepResult, ...]:
        """段を順に実行する。**一度だけ**走る: 2 回目以降（実行中を含む）は走らせず、1 回目の完了を待って同じ結果を返す。

        呼び出し側が取り消されても段の実行は続く（途中で止めると子プロセスが残る）。
        """
        self._bind_loop()
        if self._task is None:
            if self._reason is None:
                self._reason = reason
            self._shutting_down = True
            self._task = asyncio.get_running_loop().create_task(self._run(), name="lucianke-shutdown")
        return await asyncio.shield(self._task)

    # --- 内部 ---

    def _bind_loop(self) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """``request_shutdown`` がループを起こせるようにする（``install_signal_handlers`` が呼ぶ）。"""
        self._loop = loop

    async def _run(self) -> tuple[StepResult, ...]:
        steps = self.steps
        _log.info("shutdown begin: reason=%s steps=%d", self._reason, len(steps))
        t0 = time.monotonic()
        results: list[StepResult] = []
        try:
            for step in steps:
                results.append(await self._run_step(step))
        finally:
            self._closed.set()
        bad = [r for r in results if r.status != "ok"]
        _log.info(
            "shutdown done: %.0f ms, %d/%d steps ok%s",
            (time.monotonic() - t0) * 1000,
            len(results) - len(bad),
            len(results),
            "" if not bad else " (failed: " + ", ".join(f"{r.name}={r.status}" for r in bad) + ")",
        )
        return tuple(results)

    async def _run_step(self, step: ShutdownStep) -> StepResult:
        t0 = time.monotonic()

        def elapsed() -> float:
            return (time.monotonic() - t0) * 1000

        try:
            work = asyncio.ensure_future(step.fn())
        except Exception as exc:  # fn() 自体が同期で失敗した
            _log.error("shutdown step %r failed to start: %r", step.name, exc)
            return StepResult(step.name, step.order, "error", elapsed(), exc)
        try:
            done, _pending = await asyncio.wait({work}, timeout=step.timeout_ms / 1000)
        except asyncio.CancelledError:
            work.cancel()
            raise
        if not done:
            work.cancel()
            await asyncio.wait({work}, timeout=_CANCEL_GRACE_S)
            if not work.done():
                # 取り消しを無視して居座る。見切りをつける（後で終わっても例外を拾って黙らせる）
                work.add_done_callback(_consume_exception)
            _log.warning("shutdown step %r timed out after %d ms", step.name, step.timeout_ms)
            return StepResult(step.name, step.order, "timeout", elapsed())
        if work.cancelled():
            _log.warning("shutdown step %r was cancelled", step.name)
            return StepResult(step.name, step.order, "error", elapsed(), asyncio.CancelledError())
        exc = work.exception()
        if exc is not None:
            _log.error("shutdown step %r failed: %r", step.name, exc, exc_info=exc)
            return StepResult(step.name, step.order, "error", elapsed(), exc)
        ms = elapsed()
        _log.info("shutdown step %r ok (%.0f ms)", step.name, ms)
        return StepResult(step.name, step.order, "ok", ms)


def _consume_exception(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()


# --- パイプラインへの接続 ---------------------------------------------------------------------


def make_shutdown_guard(lifecycle: Lifecycle) -> Callable[[], None]:
    """``tools/call`` の入口で呼ぶ関数を作る。終了が始まっていれば ``E_UNSUPPORTED``（``details.reason="shutting_down"``）を投げる。"""

    def guard() -> None:
        if lifecycle.shutting_down:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                "Lucianke is shutting down; no new requests are accepted.",
                details={"reason": "shutting_down"},
            )

    return guard


def make_terminate_children_step(registry: ProcessRegistry, grace_ms: int = SHUTDOWN_GRACE_MS) -> StepFn:
    """``ShutdownOrder.KILL_CHILDREN`` の段: 台帳に残った子プロセスを落とし、以後の ``spawn`` を拒否する（NFR-OPS-2）。"""

    async def terminate_children() -> None:
        await registry.terminate_all(grace_ms, close=True)

    return terminate_children


async def run_until_shutdown(
    lifecycle: Lifecycle, serve: Awaitable[object], *, eof_reason: str = "stdin_eof"
) -> None:
    """``serve``（ホストが ``stdin`` を閉じると戻る MCP サーバーの実行）を走らせ、終了の契機が来たら終了処理を完了させる。

    * ``serve`` が先に戻った（EOF）→ ``shutdown(eof_reason)`` を走らせて完了を待つ。``serve`` が例外で落ちたときも
      終了処理を済ませてから、その例外を再送出する。
    * シグナルで ``shutdown`` が先に完了した → ``serve`` を取り消して戻る（終了処理の間、``serve`` は動き続けるので、
      実行中の要求は終われる。新規 ``tools/call`` はガードが拒否する）。
    """
    serve_task = asyncio.ensure_future(serve)
    closed_task = asyncio.ensure_future(lifecycle.wait_closed())
    error: BaseException | None = None
    try:
        await asyncio.wait({serve_task, closed_task}, return_when=asyncio.FIRST_COMPLETED)
        if serve_task.done() and not serve_task.cancelled():
            error = serve_task.exception()
        await lifecycle.shutdown(eof_reason if error is None else f"serve_error:{type(error).__name__}")
    finally:
        pending = [t for t in (serve_task, closed_task) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    if error is not None:
        raise error


# --- シグナル ---------------------------------------------------------------------------------


def _signals() -> tuple[signal.Signals, ...]:
    if sys.platform == "win32":
        return (signal.SIGINT, signal.SIGBREAK)  # SIGTERM は Windows では誰も送れない
    return (signal.SIGINT, signal.SIGTERM)


class SignalWatch:
    """``install_signal_handlers`` の戻り値。``close()`` でハンドラを元に戻し、監視タスクを止める。

    ``mode`` は、シグナル名ごとにどの方式で登録したか（``"loop"`` = ``add_signal_handler``、``"signal"`` = ``signal.signal``）。
    登録できなかったシグナルは載らない。
    """

    def __init__(self, task: asyncio.Task[None]) -> None:
        self._task = task
        self._undo: list[Callable[[], object]] = []
        self.mode: dict[str, Literal["loop", "signal"]] = {}

    def add_undo(self, undo: Callable[[], object]) -> None:
        self._undo.append(undo)

    def close(self) -> None:
        self._task.cancel()
        for undo in reversed(self._undo):
            with contextlib.suppress(Exception):
                undo()
        self._undo.clear()


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop, lifecycle: Lifecycle, *, prefer_loop_handlers: bool = True
) -> SignalWatch:
    """終了のシグナルを受けて ``lifecycle`` を止めるようにする。

    * POSIX（``SIGINT``・``SIGTERM``）: ``loop.add_signal_handler``。
    * Windows（``SIGINT``・``SIGBREAK``）、および ``add_signal_handler`` が使えない環境: ``signal.signal``。
      ハンドラは ``request_shutdown`` を呼ぶだけ。メインスレッド以外では登録できない（``ValueError``）ので、そのシグナルは
      諦めてログに残す（``stdin`` の EOF が残る）。

    どちらの場合も、ループ側の監視タスクが ``shutdown`` を呼ぶ。``prefer_loop_handlers=False`` は ``signal.signal`` を強制する（テスト用）。
    戻り値の ``close()`` を終了時に呼ぶ（ハンドラを元に戻す）。
    """
    lifecycle.bind_loop(loop)
    watch = SignalWatch(loop.create_task(_monitor(lifecycle), name="lucianke-signal-monitor"))
    for sig in _signals():
        label = f"signal:{sig.name}"
        if prefer_loop_handlers:
            try:
                loop.add_signal_handler(sig, lifecycle.request_shutdown, label)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows のループ、メインスレッド以外 → signal.signal へ
            else:
                watch.add_undo(functools.partial(loop.remove_signal_handler, sig))
                watch.mode[sig.name] = "loop"
                continue
        try:
            previous = signal.signal(sig, _make_handler(lifecycle, label))
        except (ValueError, OSError) as exc:
            _log.warning("cannot install handler for %s: %r", sig.name, exc)
            continue
        watch.add_undo(
            functools.partial(signal.signal, sig, previous if previous is not None else signal.SIG_DFL)
        )
        watch.mode[sig.name] = "signal"
    return watch


def _make_handler(lifecycle: Lifecycle, label: str) -> Callable[[int, FrameType | None], None]:
    def handler(_signum: int, _frame: FrameType | None) -> None:
        lifecycle.request_shutdown(label)

    return handler


async def _monitor(lifecycle: Lifecycle) -> None:
    reason = await lifecycle.wait_requested()
    _log.info("shutdown requested: %s", reason)
    await lifecycle.shutdown(reason)
