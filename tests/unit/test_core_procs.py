"""core.procs のテスト（設計書 ADR-12、仕様書 §12.5・§13.3）。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lucianke.core.procs import ProcessRegistry, minimal_env

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]
GRACE_MS = 300


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


def hard_kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


async def test_terminate_all_leaves_no_live_processes(tmp_path: Path) -> None:
    reg = ProcessRegistry()
    procs = [await reg.spawn(SLEEPER, cwd=tmp_path, env=None, name=f"sleeper-{i}") for i in range(2)]
    try:
        assert len(reg.live()) == 2
        assert [m.name for m in reg.live()] == ["sleeper-0", "sleeper-1"]
        assert all(p.returncode is None for p in procs)
        t0 = time.monotonic()
        await reg.terminate_all(GRACE_MS)
        elapsed = time.monotonic() - t0
        assert reg.live() == []
        assert all(p.returncode is not None for p in procs)
        assert elapsed < 10
        assert all(not pid_alive(p.pid) for p in procs)
    finally:
        for p in procs:
            if p.returncode is None:
                hard_kill(p.pid)


async def test_terminate_all_is_idempotent_and_safe_when_empty() -> None:
    reg = ProcessRegistry()
    await reg.terminate_all(GRACE_MS)
    assert reg.live() == []
    await reg.terminate_all(GRACE_MS, close=True)


async def test_terminate_single_process_leaves_the_others(tmp_path: Path) -> None:
    reg = ProcessRegistry()
    a = await reg.spawn(SLEEPER, cwd=tmp_path, name="a")
    b = await reg.spawn(SLEEPER, cwd=tmp_path, name="b")
    try:
        await reg.terminate(a, GRACE_MS)
        assert a.returncode is not None
        assert b.returncode is None
        assert [m.name for m in reg.live()] == ["b"]
        await reg.terminate(a, GRACE_MS)  # 終了済みへの再実行は何もしない
    finally:
        await reg.terminate_all(GRACE_MS)
    assert b.returncode is not None


async def test_exited_process_is_dropped_from_live(tmp_path: Path) -> None:
    reg = ProcessRegistry()
    proc = await reg.spawn([sys.executable, "-c", "pass"], cwd=tmp_path, name="short")
    await proc.wait()
    assert reg.live() == []


async def test_pipes_cwd_and_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUCIANKE_TEST_SECRET", "do-not-leak")
    code = (
        "import os, sys\n"
        "print(os.getcwd())\n"
        "print(os.environ.get('LUCIANKE_TEST_SECRET'))\n"
        "print(os.environ.get('LUCIANKE_TEST_EXTRA'))\n"
        "print(sys.stdin.readline().strip())\n"
    )
    reg = ProcessRegistry()
    proc = await reg.spawn(
        [sys.executable, "-c", code], cwd=tmp_path, env={"LUCIANKE_TEST_EXTRA": "yes"}, name="echo"
    )
    out, err = await asyncio.wait_for(proc.communicate(b"from-stdin\n"), timeout=30)
    assert proc.returncode == 0, err.decode(errors="replace")
    lines = out.decode().splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1] == "None"  # 親の環境変数は引き継がない
    assert lines[2] == "yes"
    assert lines[3] == "from-stdin"  # stdin はパイプ
    assert reg.live() == []


async def test_spawn_failure_is_not_registered(tmp_path: Path) -> None:
    reg = ProcessRegistry()
    with pytest.raises(FileNotFoundError):
        await reg.spawn(["this-command-does-not-exist-lucianke"], cwd=tmp_path, name="missing")
    assert reg.live() == []
    with pytest.raises(ValueError):
        await reg.spawn([], cwd=tmp_path, name="empty")


async def test_spawn_after_close_is_refused(tmp_path: Path) -> None:
    reg = ProcessRegistry()
    await reg.terminate_all(GRACE_MS, close=True)
    with pytest.raises(RuntimeError):
        await reg.spawn(SLEEPER, cwd=tmp_path, name="late")
    assert reg.live() == []


async def test_terminate_kills_descendants(tmp_path: Path) -> None:
    parent_code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    reg = ProcessRegistry()
    proc = await reg.spawn([sys.executable, "-c", parent_code], cwd=tmp_path, name="parent")
    grandchild = 0
    try:
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        grandchild = int(line.decode().strip())
        assert pid_alive(grandchild)
        await reg.terminate_all(GRACE_MS)
        assert proc.returncode is not None
        deadline = time.monotonic() + 10
        while pid_alive(grandchild) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert not pid_alive(grandchild), "descendant process survived terminate_all"
    finally:
        if proc.returncode is None:
            hard_kill(proc.pid)
        if grandchild and pid_alive(grandchild):
            hard_kill(grandchild)


async def test_forced_kill_after_grace(tmp_path: Path) -> None:
    # 穏やかな合図を無視する子（SIGTERM / CTRL_BREAK を握りつぶす）は、猶予後の強制終了で落ちる
    code = (
        "import signal, sys, time\n"
        "for name in ('SIGTERM', 'SIGBREAK', 'SIGINT'):\n"
        "    if hasattr(signal, name):\n"
        "        signal.signal(getattr(signal, name), signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    reg = ProcessRegistry()
    proc = await reg.spawn([sys.executable, "-c", code], cwd=tmp_path, name="stubborn")
    try:
        assert proc.stdout is not None
        assert (await asyncio.wait_for(proc.stdout.readline(), timeout=30)).strip() == b"ready"
        t0 = time.monotonic()
        await reg.terminate_all(GRACE_MS)
        assert proc.returncode is not None
        assert time.monotonic() - t0 >= GRACE_MS / 1000 * 0.8  # 猶予の間は待った
        assert reg.live() == []
    finally:
        if proc.returncode is None:
            hard_kill(proc.pid)


# --- minimal_env -------------------------------------------------------------------------


def test_minimal_env_contains_path_and_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("LUCIANKE_SECRET_TOKEN", "x")
    env = minimal_env()
    keys = {k.upper() for k in env}
    assert "PATH" in keys
    assert env["LANG"] == "ja_JP.UTF-8"
    assert env["LC_ALL"] == "C.UTF-8"
    assert "LUCIANKE_SECRET_TOKEN" not in env
    if sys.platform == "win32":
        assert "SYSTEMROOT" in keys


def test_minimal_env_passes_appdata_for_user_site_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pip install --user`` の Python 製コンソールスクリプト（pyright など）は ``%APPDATA%`` が無いとユーザーの
    site-packages を見つけられず ``ModuleNotFoundError`` で落ちる（BUG-0037）。"""
    monkeypatch.setenv("APPDATA", "/home/u/AppData/Roaming")
    monkeypatch.setenv("LOCALAPPDATA", "/home/u/AppData/Local")
    monkeypatch.setenv("LUCIANKE_SECRET_TOKEN", "x")
    env = {k.upper(): v for k, v in minimal_env().items()}
    assert env["APPDATA"] == "/home/u/AppData/Roaming"
    assert env["LOCALAPPDATA"] == "/home/u/AppData/Local"
    assert "LUCIANKE_SECRET_TOKEN" not in env


def test_minimal_env_extra_wins_and_adds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "C")
    env = minimal_env({"LANG": "en_US.UTF-8", "VIRTUAL_ENV": "/venv"})
    assert env["LANG"] == "en_US.UTF-8"
    assert env["VIRTUAL_ENV"] == "/venv"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows は変数名の大文字小文字を区別しない")
def test_minimal_env_extra_overrides_case_insensitively_on_windows() -> None:
    env = minimal_env({"Path": "C:\\only"})
    assert [k for k in env if k.upper() == "PATH"] == ["Path"]
    assert env["Path"] == "C:\\only"


def test_minimal_env_does_not_mutate_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "C")
    before = dict(os.environ)
    minimal_env({"X_EXTRA": "1"})
    assert dict(os.environ) == before
