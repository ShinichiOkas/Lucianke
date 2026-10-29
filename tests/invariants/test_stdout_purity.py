"""不変条件: ``stdout`` は JSON-RPC 専用（NFR-OPS-1、仕様書 §12.1、設計書 ADR-3）。

``python -m lucianke`` を子プロセスで起動し、生の JSON-RPC 行（SDK を通さない）でやり取りしながら、子の ``stdout`` の
全バイトを記録する。開発用 ``ping`` ツールは ``noise=true`` で ``print`` と ``os.write(1, …)`` を行う。それでも

* ``stdout`` の全行が JSON-RPC 2.0 のメッセージとして解析でき（LF 終端、CRLF なし）、``noise`` は 1 バイトも混ざらない。
* ``stdin`` を閉じると 2 秒以内に終了コード 0 で終わる。
* ログファイルの各行が JSON で、``event`` と ``request_id`` を持つ。

SDK クライアントは使わない（stdout の横取りと全行の検査をしやすくするため。S-1 の ``rawclient.py`` と同じ方式）。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REQUEST_TIMEOUT_S = 60.0  # コールドスタートは約 1 s（S-1）。負荷のある機でも落ちない余裕
EXIT_DEADLINE_S = 2.0  # stdin を閉じてから終わるまで


class RawSession:
    def __init__(self, tmp_path: Path, *, dev_tools: bool = True) -> None:
        self.cache = tmp_path / "cache"
        env = dict(os.environ)
        env["LUCIANKE_CACHE_DIR"] = str(self.cache)
        env["PYTHONUTF8"] = "1"
        env.pop("LUCIANKE_DEV_TOOLS", None)
        if dev_tools:
            env["LUCIANKE_DEV_TOOLS"] = "1"
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "lucianke"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )  # fmt: skip
        self.stdout_lines: list[bytes] = []  # 本物の stdout に出たもの全部（生バイト、改行込み）
        self.stderr = bytearray()
        self.notifications: list[dict[str, Any]] = []
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._next_id = 1
        self._threads = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(self.proc.stdout.readline, b""):
            self.stdout_lines.append(line)
            try:
                self._messages.put(json.loads(line))
            except ValueError:
                self._messages.put({"__unparsable__": line.decode("utf-8", "replace")})
        self._messages.put(None)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        while chunk := self.proc.stderr.read1(4096):  # type: ignore[attr-defined]
            self.stderr += chunk

    def send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        on_server_request: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        rid = self._next_id
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})
        deadline = time.monotonic() + REQUEST_TIMEOUT_S
        while True:
            msg = self._messages.get(timeout=max(0.1, deadline - time.monotonic()))
            assert msg is not None, f"server closed stdout before answering {method}; stderr={self.stderr.decode('utf-8', 'replace')}"
            if "method" in msg and "id" in msg:  # サーバーからの要求（roots/list）
                assert on_server_request is not None, f"unexpected server request: {msg}"
                self.send({"jsonrpc": "2.0", "id": msg["id"], "result": on_server_request(msg)})
            elif "method" in msg:
                self.notifications.append(msg)
            elif msg.get("id") == rid:
                return msg

    def wait_server_request(self, handler: Callable[[dict[str, Any]], dict[str, Any]]) -> str:
        """サーバー起点の要求が 1 つ届くまで待って応答する。要求のメソッド名を返す。"""
        deadline = time.monotonic() + REQUEST_TIMEOUT_S
        while True:
            msg = self._messages.get(timeout=max(0.1, deadline - time.monotonic()))
            assert msg is not None, f"server closed stdout; stderr={self.stderr_text}"
            if "method" in msg and "id" in msg:
                self.send({"jsonrpc": "2.0", "id": msg["id"], "result": handler(msg)})
                return str(msg["method"])
            if "method" in msg:
                self.notifications.append(msg)

    def initialize(self, capabilities: dict[str, Any] | None = None, on_server_request=None) -> dict[str, Any]:
        res = self.request(
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": capabilities or {}, "clientInfo": {"name": "raw", "version": "0"}},
            on_server_request,
        )
        self.notify("notifications/initialized")
        return res

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self) -> tuple[int, float]:
        """stdin を閉じて終了を待つ。(終了コード, 閉じてから終わるまでの秒)。"""
        assert self.proc.stdin is not None
        t0 = time.perf_counter()
        self.proc.stdin.close()
        try:
            rc = self.proc.wait(timeout=EXIT_DEADLINE_S + 3)  # 計測は dt で。ここは固まり防止の上限
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise
        dt = time.perf_counter() - t0
        for t in self._threads:
            t.join(timeout=5)
        return rc, dt

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    @property
    def stdout_bytes(self) -> bytes:
        return b"".join(self.stdout_lines)

    @property
    def stderr_text(self) -> str:
        return bytes(self.stderr).decode("utf-8", "replace")

    def log_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted((self.cache / "logs").glob("lucianke-*.log")):
            out += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return out


@pytest.fixture
def make_session(tmp_path: Path):
    sessions: list[RawSession] = []

    def factory(**kwargs: Any) -> RawSession:
        s = RawSession(tmp_path, **kwargs)
        sessions.append(s)
        return s

    yield factory
    for s in sessions:
        s.kill()


def assert_pure_jsonrpc(s: RawSession) -> None:
    assert s.stdout_lines, "server wrote nothing to stdout"
    for line in s.stdout_lines:
        assert line.endswith(b"\n") and not line.endswith(b"\r\n"), f"bad line ending: {line!r}"
        msg = json.loads(line)  # 解析できない行があればここで落ちる
        assert msg.get("jsonrpc") == "2.0", f"not a JSON-RPC message: {line!r}"
        assert "method" in msg or "result" in msg or "error" in msg, f"not a JSON-RPC message: {line!r}"


def test_stdout_stays_pure_jsonrpc_even_when_a_tool_writes_to_fd1(make_session):
    s = make_session()
    init = s.initialize()
    assert init["result"]["serverInfo"]["name"] == "lucianke"

    tools = s.request("tools/list")["result"]["tools"]
    # ツールの一覧は増えていく。不変条件の本質ではないので「ping を含む」ことだけを確かめる（決め打ちしない）
    ping = next((t for t in tools if t["name"] == "ping"), None)
    assert ping is not None, [t["name"] for t in tools]
    schema = ping["inputSchema"]
    assert schema["additionalProperties"] is False and schema["properties"]["max_tokens"]["default"] == 500

    res = s.call("ping", {"echo": "hello", "noise": True})["result"]
    assert res["isError"] is False
    text = res["content"][0]["text"]
    assert "echo: hello" in text and "noise" not in text

    bad = s.call("ping", {"bogus": 1})  # スキーマ違反は JSON-RPC エラーではなく isError の結果
    assert "error" not in bad and bad["result"]["isError"] is True
    assert "E_INVALID_ARGUMENT" in bad["result"]["content"][0]["text"]

    unknown = s.request("tools/call", {"name": "no_such_tool", "arguments": {}})  # 未知のツール名だけは JSON-RPC エラー
    assert unknown["error"]["code"] == -32602

    rc, dt = s.close()
    assert rc == 0, s.stderr_text
    assert dt < EXIT_DEADLINE_S, f"took {dt:.2f}s to exit after stdin EOF"

    # 検査の本体: 子の stdout の全行が JSON-RPC。noise は 1 バイトも混ざらない（歯があることの確認に stderr 側は見る）
    assert_pure_jsonrpc(s)
    assert len(s.stdout_lines) == 5  # initialize, tools/list, ping, ping(不正), ping(未知)。サーバー起点の要求は無い
    # （スキーマの説明文にプロパティ名 noise が載るので、バイト列全体の検索ではなく「noise だけの行」が無いことを見る）
    assert not any(line.strip() == b"noise" for line in s.stdout_lines)
    assert s.stderr_text.count("noise") >= 2  # print と os.write(1, …) の両方が stderr に流れた

    # ログファイルの各行は JSON で、event と request_id を持つ（設計書 §9.2）
    records = s.log_records()
    assert records and all("event" in r and "request_id" in r for r in records)
    calls = [r for r in records if r["event"] == "tool_call"]
    assert [r["tool"] for r in calls] == ["ping", "ping"]
    assert calls[1]["error_code"] == "E_INVALID_ARGUMENT" and all(r["request_id"] for r in calls)


def test_dev_tools_are_hidden_unless_enabled(make_session):
    s = make_session(dev_tools=False)
    s.initialize()
    names = [t["name"] for t in s.request("tools/list")["result"]["tools"]]
    assert "ping" not in names  # 開発用ツールだけが隠れる。製品のツールは並ぶので一覧が空であることは求めない
    assert s.call("ping")["error"]["code"] == -32602
    rc, dt = s.close()
    assert rc == 0 and dt < EXIT_DEADLINE_S
    assert_pure_jsonrpc(s)


def test_roots_are_requested_after_initialized_and_stdout_stays_pure(make_session, tmp_path):
    proj = tmp_path / "proj_roots_test"
    proj.mkdir()
    s = make_session()
    asked: list[str] = []

    def answer(msg: dict[str, Any]) -> dict[str, Any]:
        asked.append(msg["method"])
        return {"roots": [{"uri": proj.as_uri(), "name": "proj"}]}

    s.initialize({"roots": {}})
    # roots/list は initialized の通知の後にサーバーが要求する（別タスク。initialize の中で待つとデッドロックする）
    assert s.wait_server_request(answer) == "roots/list"
    assert asked == ["roots/list"]

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not any(r["event"] == "roots" for r in s.log_records()):
        time.sleep(0.05)
    roots_logs = [r for r in s.log_records() if r["event"] == "roots"]
    assert roots_logs and "proj_roots_test" in roots_logs[0]["msg"] and "client" in roots_logs[0]["msg"]

    rc, dt = s.close()
    assert rc == 0 and dt < EXIT_DEADLINE_S
    assert_pure_jsonrpc(s)


def test_no_roots_request_without_the_capability(make_session):
    s = make_session()
    s.initialize()  # roots 能力を宣言しない
    s.request("tools/list")
    time.sleep(0.3)
    rc, _ = s.close()
    assert rc == 0
    assert not [line for line in s.stdout_lines if b"roots/list" in line]
    assert_pure_jsonrpc(s)
