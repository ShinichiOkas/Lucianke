"""SDK を通さず JSON-RPC の行を直接やり取りする最小クライアント（probe_*.py / bench.py の共通部品）。
使い方: from rawclient import RawServer; s = RawServer(["--divert=manual"]); s.initialize(); s.call("noop"); s.close()
stdout は 1 行ずつ生のバイト列で保存する（外から stdout を記録して検査するため）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PY = str(REPO / ".venv" / "Scripts" / "python.exe") if sys.platform == "win32" else str(REPO / ".venv" / "bin" / "python")
SERVER = str(HERE / "server.py")


class RawServer:
    def __init__(self, args: list[str] | None = None, python_args: list[str] | None = None,
                 on_server_request: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                 watchdog: float = 60.0, server: str = SERVER):  # fmt: skip
        self.t_spawn = time.perf_counter()
        self.p = subprocess.Popen(
            [PY, *(python_args or []), server, *(args or [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )  # fmt: skip
        self.stdout_lines: list[bytes] = []  # 本物の stdout に出た行（生バイト、改行込み）
        self.incoming: list[dict[str, Any]] = []  # 応答以外（通知・サーバーからの要求）
        self.stderr_buf = bytearray()
        self._next_id = 1
        self.on_server_request = on_server_request
        self._out = self.p.stdout
        assert self._out is not None and self.p.stderr is not None
        self._out = open(self.p.stdout.fileno(), "rb", buffering=65536, closefd=False)  # noqa: SIM115
        threading.Thread(target=self._drain_err, daemon=True).start()
        timer = threading.Timer(watchdog, self.p.kill)  # 応答が返らない構成（naive など）で固まらないように
        timer.daemon = True
        timer.start()
        self._timer = timer

    def _drain_err(self) -> None:
        assert self.p.stderr is not None
        while chunk := self.p.stderr.read(4096):
            self.stderr_buf += chunk

    @property
    def stderr(self) -> str:
        return bytes(self.stderr_buf).decode("utf-8", "replace")

    def send(self, obj: dict[str, Any]) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")

    def _read_msg(self) -> dict[str, Any]:
        line = self._out.readline()
        if not line:
            raise EOFError("server closed stdout")
        self.stdout_lines.append(line)
        return json.loads(line)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        rid = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        while True:
            m = self._read_msg()
            if "method" not in m and m.get("id") == rid:
                return m
            self.incoming.append(m)
            if "method" in m and "id" in m and self.on_server_request is not None:  # サーバーからの要求に応答
                res = self.on_server_request(m)
                self.send({"jsonrpc": "2.0", "id": m["id"], "result": res or {}})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def initialize(self, caps: dict[str, Any] | None = None, version: str = "2025-11-25") -> dict[str, Any]:
        res = self.request("initialize", {"protocolVersion": version, "capabilities": caps or {},
                                          "clientInfo": {"name": "raw", "version": "0"}})  # fmt: skip
        self.notify("notifications/initialized")
        return res

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self, timeout: float = 10.0) -> tuple[int | None, float]:
        """stdin を閉じて終了を待つ。(終了コード, 閉じてから終わるまでの秒) を返す。"""
        t0 = time.perf_counter()
        assert self.p.stdin is not None
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            rc = self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            rc = None
        dt = time.perf_counter() - t0
        self._timer.cancel()
        time.sleep(0.05)
        return rc, dt
