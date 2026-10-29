"""lifecycle のテスト（設計書 §6.5・§8.7・§9.5、仕様書 §12.5、NFR-OPS-2）。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.procs import ProcessRegistry
from lucianke.lifecycle import (
    DEFAULT_TIMEOUT_MS,
    Lifecycle,
    ShutdownOrder,
    install_signal_handlers,
    make_shutdown_guard,
    make_terminate_children_step,
    run_until_shutdown,
)

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def recorder(lc: Lifecycle, log: list[str], name: str, *, order: int, timeout_ms: int = 1000) -> None:
    async def step() -> None:
        log.append(name)

    lc.add_shutdown_step(name, step, order=order, timeout_ms=timeout_ms)


# --- 段の実行 -----------------------------------------------------------------------------


def test_steps_property_sorts_by_order_then_registration() -> None:
    lc = Lifecycle()
    log: list[str] = []
    recorder(lc, log, "c", order=30)
    recorder(lc, log, "a", order=10)
    recorder(lc, log, "b2", order=20)
    recorder(lc, log, "b1", order=20)
    assert [s.name for s in lc.steps] == ["a", "b2", "b1", "c"]
    assert not lc.shutting_down and not lc.closed and lc.reason is None


async def test_steps_run_in_order_and_report_ok() -> None:
    lc = Lifecycle()
    log: list[str] = []
    recorder(lc, log, "logs", order=ShutdownOrder.FLUSH_LOGS)
    recorder(lc, log, "intake", order=ShutdownOrder.STOP_INTAKE)
    recorder(lc, log, "lsp", order=ShutdownOrder.LSP)
    recorder(lc, log, "edits", order=ShutdownOrder.DRAIN_EDITS)
    recorder(lc, log, "index", order=ShutdownOrder.INDEX_CLOSE)
    recorder(lc, log, "children", order=ShutdownOrder.KILL_CHILDREN)
    recorder(lc, log, "watcher", order=ShutdownOrder.STOP_WATCHER)
    results = await lc.shutdown("test")
    assert log == ["intake", "edits", "watcher", "lsp", "children", "index", "logs"]
    assert [r.name for r in results] == log
    assert all(r.status == "ok" and r.error is None for r in results)
    assert lc.closed and lc.reason == "test"


def test_standard_orders_match_design_6_5() -> None:
    assert [o.value for o in ShutdownOrder] == [10, 20, 30, 40, 50, 60, 70]
    assert set(DEFAULT_TIMEOUT_MS) == set(ShutdownOrder)
    assert DEFAULT_TIMEOUT_MS[ShutdownOrder.DRAIN_EDITS] == 5000  # 設計書 §6.5: 編集の完了待ちは最大 5 s


async def test_step_timeout_cancels_it_and_moves_on() -> None:
    lc = Lifecycle()
    log: list[str] = []
    cancelled = asyncio.Event()

    async def stuck() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    lc.add_shutdown_step("stuck", stuck, order=10, timeout_ms=50)
    recorder(lc, log, "after", order=20)
    t0 = time.monotonic()
    results = await lc.shutdown("test")
    assert time.monotonic() - t0 < 2.0
    assert [(r.name, r.status) for r in results] == [("stuck", "timeout"), ("after", "ok")]
    assert cancelled.is_set()
    assert log == ["after"]


async def test_step_that_ignores_cancellation_does_not_block_the_sequence() -> None:
    lc = Lifecycle()
    log: list[str] = []
    finished = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.6)  # 取り消しを 1 回無視してしばらく居座る
        finished.set()

    lc.add_shutdown_step("stubborn", stubborn, order=10, timeout_ms=50)
    recorder(lc, log, "after", order=20)
    t0 = time.monotonic()
    results = await lc.shutdown("test")
    assert time.monotonic() - t0 < 0.55  # 居座りを待たずに見切りをつけた
    assert [(r.name, r.status) for r in results] == [("stubborn", "timeout"), ("after", "ok")]
    assert log == ["after"]
    await asyncio.wait_for(finished.wait(), 3)  # 後始末: 居座っていた段が終わってからループを閉じる


async def test_exception_in_a_step_is_logged_and_next_steps_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lc = Lifecycle()
    log: list[str] = []

    async def boom() -> None:
        raise RuntimeError("lsp exploded")

    def not_async() -> None:  # awaitable を返さない関数も結線ミスとして記録して次へ進む
        return None

    lc.add_shutdown_step("boom", boom, order=10, timeout_ms=1000)
    lc.add_shutdown_step("not_async", not_async, order=15, timeout_ms=1000)  # type: ignore[arg-type]
    recorder(lc, log, "children", order=20)
    with caplog.at_level(logging.ERROR, logger="lucianke.lifecycle"):
        results = await lc.shutdown("test")
    assert [(r.name, r.status) for r in results] == [
        ("boom", "error"),
        ("not_async", "error"),
        ("children", "ok"),
    ]
    assert isinstance(results[0].error, RuntimeError)
    assert log == ["children"]
    assert any("boom" in r.getMessage() for r in caplog.records)


async def test_shutdown_runs_only_once() -> None:
    lc = Lifecycle()
    calls = 0

    async def step() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)

    lc.add_shutdown_step("once", step, order=10, timeout_ms=1000)
    first, second, third = await asyncio.gather(lc.shutdown("a"), lc.shutdown("b"), lc.shutdown("c"))
    again = await lc.shutdown("d")  # 完了後の呼び出しも同じ結果を返すだけ
    assert calls == 1
    assert first == second == third == again
    assert lc.reason == "a"


async def test_cancelling_the_caller_does_not_stop_the_shutdown() -> None:
    lc = Lifecycle()
    log: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(0.1)
        log.append("slow")

    lc.add_shutdown_step("slow", slow, order=10, timeout_ms=1000)
    recorder(lc, log, "children", order=20)
    caller = asyncio.ensure_future(lc.shutdown("test"))
    await asyncio.sleep(0.02)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.wait_for(lc.wait_closed(), 3)
    assert log == ["slow", "children"]


# --- 状態と受付停止 -----------------------------------------------------------------------


async def test_shutting_down_flips_when_shutdown_starts_and_stays() -> None:
    lc = Lifecycle()
    seen: list[bool] = []

    async def step() -> None:
        seen.append(lc.shutting_down)

    lc.add_shutdown_step("probe", step, order=10, timeout_ms=1000)
    assert lc.shutting_down is False
    await lc.shutdown("test")
    assert seen == [True]
    assert lc.shutting_down is True and lc.closed is True


async def test_request_shutdown_flips_flag_without_running_steps() -> None:
    lc = Lifecycle()
    log: list[str] = []
    recorder(lc, log, "a", order=10)
    lc.request_shutdown("signal:SIGTERM")
    lc.request_shutdown("stdin_eof")  # 理由は最初のものが残る
    assert lc.shutting_down is True and lc.closed is False
    assert lc.reason == "signal:SIGTERM"
    assert log == []
    assert await asyncio.wait_for(lc.wait_requested(), 1) == "signal:SIGTERM"
    await lc.shutdown("ignored")
    assert log == ["a"] and lc.reason == "signal:SIGTERM"


def test_shutdown_guard_raises_unsupported_only_while_shutting_down() -> None:
    lc = Lifecycle()
    guard = make_shutdown_guard(lc)
    guard()  # 通常時は何も起きない
    lc.request_shutdown("stdin_eof")
    with pytest.raises(LuciankeError) as ei:
        guard()
    assert ei.value.code == ErrorCode.E_UNSUPPORTED
    assert ei.value.details == {"reason": "shutting_down"}


async def test_cannot_add_steps_after_shutdown_started_or_with_bad_arguments() -> None:
    lc = Lifecycle()
    log: list[str] = []
    recorder(lc, log, "a", order=10)
    with pytest.raises(ValueError, match="duplicate"):
        recorder(lc, log, "a", order=20)
    with pytest.raises(ValueError, match="timeout_ms"):
        recorder(lc, log, "b", order=20, timeout_ms=0)
    await lc.shutdown("test")
    with pytest.raises(RuntimeError, match="already started"):
        recorder(lc, log, "late", order=20)
    assert log == ["a"]


# --- 子プロセス（NFR-OPS-2） ---------------------------------------------------------------


async def test_children_are_gone_within_one_second_after_shutdown(tmp_path: Path) -> None:
    reg = ProcessRegistry()
    procs = [await reg.spawn(SLEEPER, cwd=tmp_path, env=None, name=f"sleeper-{i}") for i in range(2)]
    pids = [p.pid for p in procs]
    try:
        assert len(reg.live()) == 2
        lc = Lifecycle()
        order: list[str] = []
        recorder(lc, order, "edits", order=ShutdownOrder.DRAIN_EDITS)
        lc.add_shutdown_step(
            "children",
            make_terminate_children_step(reg),
            order=ShutdownOrder.KILL_CHILDREN,
            timeout_ms=DEFAULT_TIMEOUT_MS[ShutdownOrder.KILL_CHILDREN],
        )
        t0 = time.monotonic()
        results = await lc.shutdown("test")
        elapsed = time.monotonic() - t0
        assert [r.status for r in results] == ["ok", "ok"]
        assert elapsed < 1.0, f"children survived {elapsed:.2f}s"
        assert reg.live() == []
        assert not any(pid_alive(pid) for pid in pids)
        with pytest.raises(RuntimeError, match="closed"):  # 落とした後に新しい子は起こせない
            await reg.spawn(SLEEPER, cwd=tmp_path, env=None, name="late")
    finally:
        await reg.terminate_all(100, close=True)


# --- run_until_shutdown --------------------------------------------------------------------


async def test_run_until_shutdown_runs_the_steps_when_serve_returns_on_eof() -> None:
    lc = Lifecycle()
    log: list[str] = []
    recorder(lc, log, "a", order=10)

    async def serve() -> None:
        await asyncio.sleep(0.01)  # stdin の EOF で戻る

    await asyncio.wait_for(run_until_shutdown(lc, serve()), 3)
    assert log == ["a"] and lc.closed and lc.reason == "stdin_eof"


async def test_run_until_shutdown_still_runs_the_steps_when_serve_fails() -> None:
    lc = Lifecycle()
    log: list[str] = []
    recorder(lc, log, "a", order=10)

    async def serve() -> None:
        raise ValueError("transport broke")

    with pytest.raises(ValueError, match="transport broke"):
        await asyncio.wait_for(run_until_shutdown(lc, serve()), 3)
    assert log == ["a"] and lc.closed and lc.reason == "serve_error:ValueError"


async def test_run_until_shutdown_stops_serve_after_a_requested_shutdown() -> None:
    lc = Lifecycle()
    log: list[str] = []
    serving = asyncio.Event()
    serve_cancelled = asyncio.Event()

    async def serve() -> None:
        serving.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            serve_cancelled.set()
            raise

    async def step_sees_serve_alive() -> None:
        log.append(f"serve_cancelled={serve_cancelled.is_set()}")  # 終了処理の間、serve は動き続ける

    lc.add_shutdown_step("probe", step_sees_serve_alive, order=10, timeout_ms=1000)
    watch = install_signal_handlers(asyncio.get_running_loop(), lc, prefer_loop_handlers=False)
    try:
        runner = asyncio.ensure_future(run_until_shutdown(lc, serve()))
        await asyncio.wait_for(serving.wait(), 3)
        lc.request_shutdown("signal:test")  # 監視タスクが拾って shutdown を呼ぶ
        await asyncio.wait_for(runner, 3)
    finally:
        watch.close()
    assert log == ["serve_cancelled=False"]
    assert serve_cancelled.is_set() and lc.closed and lc.reason == "signal:test"


# --- シグナル -----------------------------------------------------------------------------


async def test_signal_module_fallback_requests_shutdown_and_restores_the_handler() -> None:
    """Windows 経路（``signal.signal`` + フラグ + 監視タスク）。どの OS でも ``prefer_loop_handlers=False`` で通る。"""
    lc = Lifecycle()
    log: list[str] = []
    recorder(lc, log, "a", order=10)
    previous = signal.getsignal(signal.SIGINT)
    watch = install_signal_handlers(asyncio.get_running_loop(), lc, prefer_loop_handlers=False)
    try:
        assert watch.mode["SIGINT"] == "signal"
        assert signal.getsignal(signal.SIGINT) is not previous
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(lc.wait_closed(), 3)
    finally:
        watch.close()
    assert log == ["a"] and lc.reason == "signal:SIGINT"
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.skipif(sys.platform == "win32", reason="loop.add_signal_handler は POSIX のみ")
async def test_loop_signal_handler_is_used_on_posix_and_removed_on_close() -> None:
    lc = Lifecycle()
    watch = install_signal_handlers(asyncio.get_running_loop(), lc)
    try:
        assert watch.mode == {"SIGINT": "loop", "SIGTERM": "loop"}
    finally:
        watch.close()
    assert lc.shutting_down is False


_CHILD = """
import asyncio
from lucianke.lifecycle import Lifecycle, install_signal_handlers, run_until_shutdown

async def main():
    lc = Lifecycle()
    async def step():
        print("step-ran", flush=True)
    lc.add_shutdown_step("s", step, order=10, timeout_ms=1000)
    watch = install_signal_handlers(asyncio.get_running_loop(), lc)
    print("ready", sorted(watch.mode.items()), flush=True)
    await run_until_shutdown(lc, asyncio.Event().wait())
    print("reason", lc.reason, flush=True)
    watch.close()

asyncio.run(main())
"""


async def _run_child_and_signal(tmp_path: Path, send: object) -> tuple[int | None, list[str]]:
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,  # pyright: ignore[reportArgumentType]
    )
    assert proc.stdout is not None
    try:
        ready = await asyncio.wait_for(proc.stdout.readline(), 20)
        assert ready.decode().startswith("ready"), ready
        send(proc)  # type: ignore[operator]
        out, err = await asyncio.wait_for(proc.communicate(), 10)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    lines = [*[ready.decode().strip()], *out.decode().splitlines()]
    assert proc.returncode == 0, err.decode()
    return proc.returncode, [ln.strip() for ln in lines]


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM は POSIX のみ（Windows は下の CTRL_BREAK で検査）"
)
async def test_sigterm_to_a_real_process_runs_shutdown_and_exits_zero(tmp_path: Path) -> None:
    code, lines = await _run_child_and_signal(tmp_path, lambda p: p.send_signal(signal.SIGTERM))
    assert code == 0
    assert lines[0].startswith("ready")
    assert lines[1:] == ["step-ran", "reason signal:SIGTERM"]


@pytest.mark.skipif(sys.platform != "win32", reason="CTRL_BREAK_EVENT は Windows のみ")
async def test_ctrl_break_to_a_real_process_runs_shutdown_and_exits_zero(tmp_path: Path) -> None:
    ctrl_break = signal.CTRL_BREAK_EVENT  # type: ignore[attr-defined]
    code, lines = await _run_child_and_signal(tmp_path, lambda p: p.send_signal(ctrl_break))
    assert code == 0
    assert "'SIGBREAK', 'signal'" in lines[0]
    assert lines[1:] == ["step-ran", "reason signal:SIGBREAK"]
