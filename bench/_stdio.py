"""実プロセスの MCP サーバー（``uv run lucianke``）を stdio でつなぐ最小クライアントと、測定中の監視（仕様書 §14.2「共通の前提」）。

SDK のクライアントを使わない理由: ``stdout`` の純度（§12.1: 各行が JSON として解析でき、JSON-RPC 2.0 のメッセージであること）を
**バイト単位で** 検査し、往復時間を ``perf_counter`` で最小のオーバーヘッドで測るため。プロトコルは ``initialize`` →
``notifications/initialized`` → ``tools/list`` / ``tools/call`` と、サーバーからの ``roots/list`` への応答だけ。

監視するもの:

* ``stdout``: 空行・JSON でない行・JSON-RPC でない行・CRLF・改行で終わらない末尾を数える（1 件でも違反ならその実行全体が不合格）。
* 子プロセス: 起動した ``uv`` の子孫を 1 秒間隔で数える（参考値。LSP は正当な子）。終了時の合否は NFR-OPS-2:
  ``stdin`` を閉じて親が終わった後 1 秒以内に、観測した子孫がすべて消えること。

標準ライブラリだけ（``ctypes`` で Windows のプロセス表を引く。``psutil`` は使わない）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
SAMPLE_INTERVAL_S = 1.0
ORPHAN_DEADLINE_S = 1.0  # NFR-OPS-2: 終了後 1 秒以内に子プロセス 0


# --- プロセス表 -----------------------------------------------------------------------------


def snapshot() -> dict[int, tuple[int, str]]:
    """全プロセスの ``pid -> (ppid, 実行ファイル名)``。"""
    if sys.platform == "win32":
        return _snapshot_windows()
    try:
        cp = subprocess.run(  # noqa: S603  固定の引数列
            ["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=10, check=False
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[int, tuple[int, str]] = {}
    for line in cp.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = (int(parts[1]), Path(parts[2]).name)
    return out


def _snapshot_windows() -> dict[int, tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    class _Entry(ctypes.Structure):
        _fields_ = [  # noqa: RUF012  ctypes の慣例
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]  # fmt: skip

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if handle in (None, wintypes.HANDLE(-1).value):
        return {}
    out: dict[int, tuple[int, str]] = {}
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        ok = k32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            ok = k32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        k32.CloseHandle(handle)
    return out


def descendants(root_pid: int, snap: dict[int, tuple[int, str]]) -> dict[int, str]:
    """``root_pid`` の子孫（自身を除く）の ``pid -> 実行ファイル名``。"""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _exe) in snap.items():
        children.setdefault(ppid, []).append(pid)
    found: dict[int, str] = {}
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in found or pid == root_pid:
            continue
        found[pid] = snap[pid][1]
        stack.extend(children.get(pid, []))
    return found


def kill_pids(pids: list[int]) -> None:
    """渡された pid を終了させる（呼び出し側が「自分が起こした木」だけを渡す）。"""
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


# --- クライアント ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolResult:
    rtt_ms: float
    is_error: bool
    text: str
    structured: dict[str, Any] | None
    rpc_error: dict[str, Any] | None = None

    @property
    def envelope(self) -> dict[str, Any] | None:
        """json 形式の応答のエンベロープ（``structuredContent``、無ければ ``text`` を JSON として読む）。"""
        if self.structured is not None:
            return self.structured
        try:
            obj = json.loads(self.text)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None


@dataclass(slots=True)
class Monitor:
    stdout_lines: int = 0
    stdout_bytes: int = 0
    violations: list[dict[str, Any]] = field(default_factory=list)
    violation_count: int = 0
    notifications: int = 0
    seen: dict[int, str] = field(default_factory=dict)  # 観測した子孫（pid -> exe）
    child_series: list[tuple[float, int]] = field(default_factory=list)  # (起動からの秒, 子孫数) 変化点だけ
    max_descendants: int = 0
    samples: int = 0

    def violate(self, lineno: int, reason: str, raw: bytes) -> None:
        self.violation_count += 1
        if len(self.violations) < 20:
            head = raw[:120].decode("utf-8", errors="replace")
            self.violations.append({"line": lineno, "reason": reason, "head": head})


class McpStdioClient:
    """サブプロセスのサーバーに stdio で話す。``async with`` で使う。"""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        roots: list[Path] | None = None,
        stderr_path: Path | None = None,
        max_children: int = 16,
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.roots = roots
        self.stderr_path = stderr_path
        self.max_children = max_children
        self.monitor = Monitor()
        self.proc: asyncio.subprocess.Process | None = None
        self.init_ms = 0.0
        self.server_info: dict[str, Any] = {}
        self._pending: dict[int, asyncio.Future[tuple[dict[str, Any], float]]] = {}
        self._next_id = 0
        self._reader: asyncio.Task[None] | None = None
        self._stderr_fh: Any = None
        self._roots_answered = asyncio.Event()
        self._sampler: threading.Thread | None = None
        self._stop_sampler = threading.Event()
        self._t_start = 0.0
        self.closed_report: dict[str, Any] | None = None

    # --- ライフサイクル ---

    async def __aenter__(self) -> McpStdioClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.closed_report is None:
            await self.close()

    async def start(self, timeout: float = 60.0) -> None:
        stderr: Any = asyncio.subprocess.DEVNULL
        if self.stderr_path is not None:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_fh = self.stderr_path.open("wb")
            stderr = self._stderr_fh
        self._t_start = time.perf_counter()
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
            cwd=str(self.cwd), env=self.env, limit=1 << 26,
        )  # fmt: skip
        self._reader = asyncio.create_task(self._read_loop())
        self._sampler = threading.Thread(target=self._sample_loop, name="bench-proc-sampler", daemon=True)
        self._sampler.start()
        caps: dict[str, Any] = {"roots": {}} if self.roots else {}
        msg, _ = await self.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": caps,
             "clientInfo": {"name": "lucianke-bench", "version": "1"}},
            timeout=timeout,
        )  # fmt: skip
        self.init_ms = (time.perf_counter() - self._t_start) * 1000
        if "error" in msg:
            raise RuntimeError(f"initialize failed: {msg['error']}")
        self.server_info = msg.get("result", {}).get("serverInfo", {})
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if self.roots:  # サーバーは initialized の後に roots/list を要求する（server.py）
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._roots_answered.wait(), 8.0)

    async def close(self) -> dict[str, Any]:
        """``stdin`` を閉じて終了を待ち、子プロセスが 1 秒以内に消えるかを検査する（NFR-OPS-2）。"""
        report: dict[str, Any] = {"exit_code": None, "exit_ms": None, "exit_timeout": False,
                                  "orphans_after_1s": [], "children_gone_ms": None}  # fmt: skip
        proc = self.proc
        if proc is None:
            self.closed_report = report
            return report
        self._stop_sampler.set()
        if self._sampler is not None:
            await asyncio.to_thread(self._sampler.join, 5.0)
        # 直前の観測で子孫の表を更新する
        self._observe(time.perf_counter() - self._t_start)
        t_close = time.perf_counter()
        with contextlib.suppress(OSError, RuntimeError):
            proc.stdin.close() if proc.stdin else None
        try:
            await asyncio.wait_for(proc.wait(), 10.0)
        except TimeoutError:
            report["exit_timeout"] = True
            kill_pids([proc.pid, *descendants(proc.pid, snapshot())])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 5.0)
        t_exit = time.perf_counter()
        report["exit_code"] = proc.returncode
        report["exit_ms"] = round((t_exit - t_close) * 1000, 1)
        # 終了後 1 秒以内に観測した子孫が 0 になるか
        alive: dict[int, str] = {}
        while True:
            snap = snapshot()
            alive = {pid: exe for pid, exe in self.monitor.seen.items() if pid in snap and snap[pid][1] == exe}
            elapsed = time.perf_counter() - t_exit
            if not alive:
                report["children_gone_ms"] = round(elapsed * 1000, 1)
                break
            if elapsed >= ORPHAN_DEADLINE_S:
                report["orphans_after_1s"] = [{"pid": p, "exe": e} for p, e in alive.items()]
                kill_pids(list(alive))  # 片付け（自分が起こした木の残りだけ）
                break
            await asyncio.sleep(0.05)
        if self._reader is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._reader, 5.0)
        if self._stderr_fh is not None:
            self._stderr_fh.close()
        self.closed_report = report
        return report

    def abort(self) -> None:
        """例外時の後始末（同期）。自分が起こした木を落とす。"""
        self._stop_sampler.set()
        if self.proc is not None and self.proc.returncode is None:
            kill_pids([self.proc.pid, *descendants(self.proc.pid, snapshot())])
        kill_pids([p for p, e in self.monitor.seen.items() if snapshot().get(p, (0, ""))[1] == e])

    # --- 送受信 ---

    async def _send(self, obj: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any], *, timeout: float = 60.0) -> tuple[dict[str, Any], float]:
        """JSON-RPC の要求を送り、応答メッセージと往復時間（ms）を返す。"""
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[tuple[dict[str, Any], float]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        data = (json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                           ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")  # fmt: skip
        assert self.proc is not None and self.proc.stdin is not None
        t0 = time.perf_counter()
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()
        try:
            msg, t1 = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
        return msg, (t1 - t0) * 1000

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        while True:
            msg, _ = await self.request("tools/list", params)
            res = msg.get("result", {})
            tools.extend(res.get("tools", []))
            cursor = res.get("nextCursor")
            if not cursor:
                return tools
            params = {"cursor": cursor}

    async def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: float = 60.0) -> ToolResult:
        msg, rtt = await self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if "error" in msg:
            return ToolResult(rtt, True, "", None, rpc_error=msg["error"])
        res = msg.get("result", {})
        content = res.get("content") or []
        text = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
        return ToolResult(rtt, bool(res.get("isError")), text, res.get("structuredContent"))

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        mon = self.monitor
        lineno = 0
        while True:
            try:
                line = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):  # 1 行が上限超え: 純度の違反として数え、読み捨てる
                mon.violate(lineno + 1, "line_too_long", b"")
                continue
            t_recv = time.perf_counter()
            if not line:
                break
            lineno += 1
            mon.stdout_lines += 1
            mon.stdout_bytes += len(line)
            msg = self._check_line(lineno, line)
            if msg is None:
                continue
            if "method" in msg and "id" in msg:  # サーバーからの要求
                await self._answer_server_request(msg)
            elif "method" in msg:
                mon.notifications += 1
            elif "id" in msg:
                fut = self._pending.get(msg["id"]) if isinstance(msg["id"], int) else None
                if fut is not None and not fut.done():
                    fut.set_result((msg, t_recv))
        for fut in self._pending.values():  # EOF: 待っている要求を失敗させる
            if not fut.done():
                fut.set_exception(ConnectionError("server closed stdout"))

    def _check_line(self, lineno: int, line: bytes) -> dict[str, Any] | None:
        mon = self.monitor
        if not line.endswith(b"\n"):
            mon.violate(lineno, "unterminated_line", line)
        body = line.rstrip(b"\n")
        if body.endswith(b"\r"):
            mon.violate(lineno, "crlf", line)
            body = body.rstrip(b"\r")
        if not body.strip():
            mon.violate(lineno, "blank_line", line)
            return None
        try:
            msg = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            mon.violate(lineno, "not_json", line)
            return None
        is_rpc = (
            isinstance(msg, dict)
            and msg.get("jsonrpc") == "2.0"
            and ("method" in msg or ("id" in msg and ("result" in msg or "error" in msg)))
        )
        if not is_rpc:
            mon.violate(lineno, "not_jsonrpc", line)
            return None
        return msg

    async def _answer_server_request(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "roots/list" and self.roots:
            roots = [{"uri": p.resolve().as_uri(), "name": p.name} for p in self.roots]
            await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"roots": roots}})
            self._roots_answered.set()
        elif method == "ping":
            await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        else:
            await self._send({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32601, "message": f"unsupported: {method}"}})  # fmt: skip

    # --- 子プロセスの観測 ---

    def _observe(self, t_rel: float) -> None:
        if self.proc is None:
            return
        desc = descendants(self.proc.pid, snapshot())
        mon = self.monitor
        mon.samples += 1
        mon.seen.update(desc)
        mon.max_descendants = max(mon.max_descendants, len(desc))
        if not mon.child_series or mon.child_series[-1][1] != len(desc):
            mon.child_series.append((round(t_rel, 2), len(desc)))

    def _sample_loop(self) -> None:
        while not self._stop_sampler.is_set():
            self._observe(time.perf_counter() - self._t_start)
            self._stop_sampler.wait(SAMPLE_INTERVAL_S)

    # --- 報告 ---

    def watch_report(self) -> dict[str, Any]:
        """``stdout`` 純度と子プロセスの監視結果。``ok`` が False ならその実行全体を不合格にする。"""
        mon = self.monitor
        closed = self.closed_report or {}
        stdout_ok = mon.violation_count == 0
        orphans = closed.get("orphans_after_1s", [])
        children_ok = not orphans and not closed.get("exit_timeout") and mon.max_descendants <= self.max_children
        return {
            "ok": stdout_ok and children_ok,
            "stdout": {"ok": stdout_ok, "lines": mon.stdout_lines, "bytes": mon.stdout_bytes,
                       "violation_count": mon.violation_count, "violations": mon.violations},  # fmt: skip
            "children": {
                "ok": children_ok,
                "samples": mon.samples,
                "max_descendants": mon.max_descendants,
                "limit": self.max_children,
                "series": mon.child_series[:60],
                "images": sorted(set(mon.seen.values())),
                **closed,
            },
        }


def uv_argv(repo: Path) -> list[str]:
    """``.mcp.json`` と同じ起動（``uv run ... lucianke``）。作業ディレクトリは呼び出し側の ``cwd`` にするので ``--project`` を使う。"""
    uv = shutil.which("uv") or "uv"
    return [uv, "run", "--quiet", "--project", str(repo), "lucianke"]


def server_env(cache_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """サーバーの環境変数。索引キャッシュを実行ごとの空ディレクトリにして、並行する他の役と干渉させない。"""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONUTF8"] = "1"
    env["LUCIANKE_CACHE_DIR"] = str(cache_dir)
    env.update(extra or {})
    return env
