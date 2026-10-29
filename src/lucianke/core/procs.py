"""子プロセスの台帳 ``ProcessRegistry``（設計書 ADR-12、仕様書 §12.1・§12.5・§13.3）。

Lucianke が起動する子プロセス（LSP、ctags、ソルバなど）は **すべてここを通す**。

* シェルを介さない（argv の配列。``shell=True`` は使わない）。
* 標準入出力はすべてパイプで捕捉する（親の ``stdout`` を継承しない。NFR-OPS-1）。
* 環境変数は ``minimal_env`` が作る最小限（仕様書 §13.3）。作業ディレクトリは呼び出し側がワークスペースルートを渡す。
* 台帳に載せ、終了手順（``terminate_all``）が全部を落として回収する。NFR-OPS-2 の検査は ``live()`` が空であることの確認。

子孫プロセス（``npx`` や ``.cmd`` の中継、venv のランチャーが挟む孫）も残さないために、OS ごとに次の仕組みを使う。

* Windows: ``CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED`` で起動し、**Job Object**
  （``KILL_ON_JOB_CLOSE``）に入れてから再開する。子孫はすべて同じジョブに入るので、終了時にジョブごと落とせば
  1 つも残らない。Lucianke が異常終了してジョブのハンドルが閉じたときも OS が全部落とす（仕様書 §12.5）。
  穏やかな終了の合図は ``CTRL_BREAK_EVENT``（コンソールが無い・届かない環境では黙って失敗する）。
  ジョブに入れられなかった場合（権限など）は、リーダーだけを ``kill()`` する縮退になる。
* POSIX: ``start_new_session=True`` で起動し（自分がグループの先頭）、合図は ``killpg``
  （穏やかな終了は ``SIGTERM``、強制終了は ``SIGKILL``）。

台帳は **イベントループのスレッドからだけ** 使う（スレッド安全ではない）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_log = logging.getLogger("lucianke.procs")

# minimal_env が親の環境から引き継ぐ変数（仕様書 §13.3: PATH、ロケール、…）。
# SYSTEMROOT 系は Windows の子プロセス（Python、node）が起動に必要とする。
# HOME / USERPROFILE / TEMP / TMP は LSP サーバーが設定・キャッシュの置き場を決めるのに使う。
_ENV_BASELINE = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LANGUAGE",
    }
)
_IS_WINDOWS = sys.platform == "win32"
_REAP_TIMEOUT_S = 5.0


def minimal_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """子プロセスに渡す環境変数: ``extra`` + 親の ``PATH``・``SYSTEMROOT`` 系・ロケール系（``LC_*``）・一時/ホーム。

    親の環境のそれ以外の変数（トークン、API キーなど）は渡さない。``extra`` は引き継ぎ分より優先する。
    Windows は変数名の大文字小文字を区別しないので、``extra`` の ``Path`` は引き継いだ ``PATH`` を置き換える。
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        name = key.upper() if _IS_WINDOWS else key
        if name in _ENV_BASELINE or name.startswith("LC_"):
            env[key] = value
    if extra:
        if _IS_WINDOWS:
            overridden = {k.upper() for k in extra}
            env = {k: v for k, v in env.items() if k.upper() not in overridden}
        env.update(extra)
    return env


# --- Windows: Job Object -----------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_SUSPEND_RESUME = 0x0800

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    def _win_create_job() -> int | None:
        """``KILL_ON_JOB_CLOSE`` つきの Job Object を作る。作れなければ ``None``。"""
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _k32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            _k32.CloseHandle(job)
            return None
        return int(job)

    def _win_assign_and_resume(job: int | None, pid: int) -> bool:
        """停止状態で起動したプロセスをジョブに入れて再開する。ジョブに入れられたかを返す。

        再開（``NtResumeProcess``）は必ず試みる。再開に失敗したら ``OSError``（停止したままにしない）。
        """
        handle = _k32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_SUSPEND_RESUME, False, pid
        )
        if not handle:
            raise OSError(ctypes.get_last_error(), f"OpenProcess failed for pid {pid}")
        try:
            assigned = bool(job) and bool(_k32.AssignProcessToJobObject(job, handle))
            status = _ntdll.NtResumeProcess(handle)
        finally:
            _k32.CloseHandle(handle)
        if status != 0:
            raise OSError(0, f"NtResumeProcess failed for pid {pid}: NTSTATUS {status & 0xFFFFFFFF:#x}")
        return assigned

    def _win_close_job(job: int, *, terminate: bool) -> None:
        """ジョブを閉じる。``terminate`` ならジョブ内の全プロセス（子孫を含む）を終了させる。"""
        if terminate:
            _k32.TerminateJobObject(job, 1)
        _k32.CloseHandle(job)


# --- 台帳 --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManagedProcess:
    """台帳の 1 行。``name`` はログ用（``"lsp:python"`` など）。``job`` は Windows の Job Object ハンドル。"""

    name: str
    argv: tuple[str, ...]
    process: asyncio.subprocess.Process
    started_at: float
    job: int | None = None

    @property
    def pid(self) -> int:
        return self.process.pid


class ProcessRegistry:
    """子プロセスの起動と一括終了。"""

    def __init__(self) -> None:
        self._procs: dict[int, ManagedProcess] = {}
        self._closed = False

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None,
        env: Mapping[str, str] | None = None,
        name: str,
    ) -> asyncio.subprocess.Process:
        """``argv`` をシェルを介さずに起動する。stdin / stdout / stderr はすべて PIPE。

        ``env`` は ``minimal_env(env)`` として渡る（親の環境の全体は渡らない）。``argv[0]`` は実行ファイルの名前または
        パスで、探索は呼び出し側（``shutil.which`` など）が済ませておく。起動に失敗した場合の例外
        （``FileNotFoundError`` など）はそのまま送出し、台帳には載せない。
        """
        if self._closed:
            raise RuntimeError("ProcessRegistry is closed; no new processes may be spawned")
        if not argv:
            raise ValueError("argv is empty")
        kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=minimal_env(env),
            **kwargs,  # pyright: ignore[reportArgumentType]
        )
        job: int | None = None
        if sys.platform == "win32":
            try:
                job = _win_create_job()
                if not _win_assign_and_resume(job, proc.pid):
                    if job is not None:
                        _win_close_job(job, terminate=False)
                    job = None
                    _log.warning(
                        "could not put pid %s into a job object; descendants may outlive it", proc.pid
                    )
            except BaseException:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                if job is not None:
                    _win_close_job(job, terminate=True)
                raise
        self._procs[proc.pid] = ManagedProcess(name, tuple(argv), proc, time.monotonic(), job)
        return proc

    def live(self) -> list[ManagedProcess]:
        """まだ終了していない子プロセス（起動順）。終了済みは台帳から外す（Windows は残った子孫も落とす）。"""
        for mp in list(self._procs.values()):
            if mp.process.returncode is not None:
                self._forget(mp, swept=False)
        return list(self._procs.values())

    async def terminate(self, proc: asyncio.subprocess.Process, grace_ms: int) -> None:
        """1 つの子プロセスを落とす: 穏やかな終了の合図 → ``grace_ms`` 待つ → 強制終了 → 回収。

        子孫プロセスも落とす（Windows: ジョブごと、POSIX: プロセスグループごと）。台帳に載っていないプロセスにも使える
        （その場合はリーダーだけ）。回収（``wait``）が終わるまで戻らない。
        """
        mp = self._procs.get(proc.pid)
        was_alive = proc.returncode is None
        try:
            if was_alive:
                _signal_graceful(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=max(grace_ms, 0) / 1000)
                except TimeoutError:
                    _kill_now(proc, mp)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_S)
                    except TimeoutError:
                        _log.error("process did not exit after kill: pid=%s", proc.pid)
        finally:
            if proc.returncode is not None:
                if mp is not None:
                    self._forget(mp, swept=was_alive)
                elif was_alive:
                    _sweep_group(proc.pid)
                _close_transport(proc)

    async def terminate_all(self, grace_ms: int, *, close: bool = False) -> None:
        """台帳の全プロセスを並行して落とす（全部に合図 → ``grace_ms`` 待つ → 残りに強制終了 → 回収）。

        ``close=True`` のときは以後の ``spawn`` を ``RuntimeError`` にする（終了手順で、落とした後に再起動された
        LSP が残らないようにする）。1 つの失敗が他の回収を止めないよう、例外は記録して続ける。
        """
        if close:
            self._closed = True
        procs = [mp.process for mp in self._procs.values()]
        results = await asyncio.gather(*(self.terminate(p, grace_ms) for p in procs), return_exceptions=True)
        for proc, result in zip(procs, results, strict=True):
            if isinstance(result, BaseException):
                _log.error("failed to terminate pid=%s: %r", proc.pid, result)

    def _forget(self, mp: ManagedProcess, *, swept: bool) -> None:
        """台帳から外し、残っている子孫を片付ける。

        ``swept`` は「リーダーが生きている間に終了手順を踏んだ」の意味。POSIX のグループへの ``SIGKILL`` は、
        リーダーの終了から時間が経つと PID が別のグループに再利用されている恐れがあるので、``swept=True`` のときだけ送る。
        Windows のジョブはハンドルで特定できるので常に安全。
        """
        self._procs.pop(mp.pid, None)
        if sys.platform == "win32":
            if mp.job is not None:
                _win_close_job(mp.job, terminate=True)
        elif swept:
            _sweep_group(mp.pid)


def _signal_graceful(proc: asyncio.subprocess.Process) -> None:
    """穏やかな終了の合図（届かなくてもエラーにしない）。"""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError, ValueError):
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)


def _kill_now(proc: asyncio.subprocess.Process, mp: ManagedProcess | None) -> None:
    """強制終了。可能なら子孫プロセスごと。"""
    if sys.platform == "win32":
        if mp is not None and mp.job is not None:
            _k32.TerminateJobObject(mp.job, 1)
    else:
        _sweep_group(proc.pid)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            proc.kill()


def _close_transport(proc: asyncio.subprocess.Process) -> None:
    """回収した子のパイプ（stdin / stdout / stderr）を閉じる。asyncio に公開 API が無いので ``_transport`` を使う。

    閉じないと、イベントループが終わるまで ``unclosed transport`` の ``ResourceWarning`` が残る。
    パイプの読み手のタスクには EOF として伝わる。
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()


def _sweep_group(pid: int) -> None:
    """POSIX: ``pid`` を先頭とするプロセスグループに残っているものを ``SIGKILL`` する。Windows では何もしない。"""
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
