"""lsp.client（JSON-RPC クライアントと位置変換）と lsp.servers（サーバー定義と起動コマンドの探索）のテスト。

クライアントは **偽の LSP サーバー**（下の ``FAKE_SERVER``。``tmp_path`` に書き出して ``sys.executable`` で起動する Python
スクリプト）と実際にパイプで話す。偽サーバーは枠を読み、``initialize`` に答え、サーバーからの要求（``ask``）や通知（``emit``）を
クライアントへ送り、壊れた枠（``break``）を返せる。子は必ず ``ProcessRegistry`` 経由で起動し、``registry.live()`` が空であることを確かめる。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Position, Range
from lucianke.core.procs import ProcessRegistry
from lucianke.core.textpos import LineIndex, Unit
from lucianke.lsp.client import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    LspClient,
    LspError,
    from_lsp_position,
    to_lsp_position,
    to_lsp_range,
)
from lucianke.lsp.servers import (
    SERVERS,
    LspServerDef,
    build_initialization_options,
    build_settings,
    default_server,
    env_var_name,
    find_command,
    find_tsserver,
    server_env_var_name,
    servers_for,
    split_command,
)

REPO = Path(__file__).resolve().parents[2]

FAKE_SERVER = r'''
import collections
import json
import os
import sys
import time

inp = sys.stdin.buffer
out = sys.stdout.buffer
ENC = "utf-16"
STUBBORN = False
for a in sys.argv[1:]:
    if a == "--stubborn":
        STUBBORN = True
    else:
        ENC = a

backlog = collections.deque()
state = {"cancelled": [], "slow_ids": [], "init_params": None, "initialized": False}
srv_seq = 0


def send_raw(data):
    out.write(data)
    out.flush()


def send(msg):
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    send_raw(b"Content-Length: %d\r\n\r\n" % len(body) + body)


def reply(rid, result=None):
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def read_frame():
    length = None
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.partition(b":")
        if key.strip().lower() == b"content-length":
            length = int(value)
    if length is None:
        return None
    data = inp.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode("utf-8"))


def ask(method, params):
    """サーバーからクライアントへ要求を送り、その応答（メッセージ全体）を返す。途中で届いた別のメッセージは後回し。"""
    global srv_seq
    srv_seq += 1
    sid = "srv-%d" % srv_seq
    send({"jsonrpc": "2.0", "id": sid, "method": method, "params": params})
    while True:
        msg = read_frame()
        if msg is None:
            sys.exit(1)
        if "method" not in msg and msg.get("id") == sid:
            return msg
        backlog.append(msg)


def next_message():
    return backlog.popleft() if backlog else read_frame()


sys.stderr.write("fake-server started\n")
sys.stderr.flush()

while True:
    msg = next_message()
    if msg is None:
        break
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params")
    if method == "initialize":
        caps = {"textDocumentSync": 2}
        if ENC != "absent":
            caps["positionEncoding"] = ENC
        state["init_params"] = params
        reply(rid, {"capabilities": caps, "serverInfo": {"name": "fake", "version": "1"}})
    elif method == "initialized":
        state["initialized"] = True
    elif method == "echo":
        reply(rid, params)
    elif method == "slow":
        state["slow_ids"].append(rid)
    elif method == "$/cancelRequest":
        state["cancelled"].append(params["id"])
        send({"jsonrpc": "2.0", "method": "test/cancelled", "params": params})
    elif method == "fail":
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "bad params", "data": {"x": 1}}})
    elif method == "ask":
        reply(rid, ask(params["method"], params.get("params")))
    elif method == "emit":
        for i in range(params["n"]):
            send({"jsonrpc": "2.0", "method": "test/event", "params": {"i": i}})
        send({"jsonrpc": "2.0", "method": "window/logMessage", "params": {"type": 2, "message": "hello from fake"}})
        reply(rid, "emitted")
    elif method == "split":
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": "x" * 5000 + "日本語"}, ensure_ascii=False).encode("utf-8")
        frame = b"Content-Length: %d\r\n\r\n" % len(body) + body
        for i in range(0, len(frame), 777):
            send_raw(frame[i : i + 777])
            time.sleep(0.01)
    elif method == "big":
        reply(rid, "あ😀" * params["n"])
    elif method == "state":
        reply(rid, state)
    elif method == "die":
        os._exit(3)
    elif method == "break":
        kind = params["kind"]
        if kind == "no-length":
            send_raw(b"Foo: bar\r\n\r\n")
        elif kind == "no-colon":
            send_raw(b"garbage\r\n\r\n")
        elif kind == "bad-length":
            send_raw(b"Content-Length: abc\r\n\r\n")
        elif kind == "huge":
            send_raw(b"Content-Length: 99999999999\r\n\r\n")
        elif kind == "bad-json":
            send_raw(b"Content-Length: 3\r\n\r\n{{{")
        elif kind == "not-object":
            send_raw(b"Content-Length: 2\r\n\r\n[]")
        elif kind == "bad-utf8":
            send_raw(b"Content-Length: 4\r\n\r\n\xff\xfe\xfd\xfc")
        elif kind == "truncated":
            send_raw(b"Content-Length: 100\r\n\r\nabc")
            os._exit(0)
    elif method == "shutdown":
        if not STUBBORN:
            reply(rid, None)
    elif method == "exit":
        if not STUBBORN:
            sys.exit(0)
'''


@pytest.fixture
def fake_script(tmp_path: Path) -> Path:
    path = tmp_path / "fake_lsp_server.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return path


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


Spawn = Callable[..., Awaitable[LspClient]]


@pytest.fixture
async def spawn(registry: ProcessRegistry, fake_script: Path, tmp_path: Path):
    """偽サーバーを起動して ``initialize`` まで済ませたクライアントを返す。テストの後で全部 ``shutdown`` する。"""
    clients: list[LspClient] = []

    async def _spawn(*args: str, initialize: bool = True) -> LspClient:
        client = LspClient("fake")
        clients.append(client)
        await client.start([sys.executable, str(fake_script), *args], cwd=tmp_path, registry=registry)
        if initialize:
            await client.initialize(root_uri=tmp_path.as_uri(), root_path=tmp_path, timeout_ms=10_000)
        return client

    yield _spawn
    for client in clients:
        await client.shutdown(200)


async def eventually(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# --- 往復・通知・サーバーからの要求 ---------------------------------------------------------


async def test_roundtrip_echo_with_non_ascii(spawn: Spawn):
    client = await spawn()
    params = {"text": "全角 😀 e\u0301", "n": [1, 2, {"k": None}]}
    assert await client.request("echo", params, timeout_ms=5000) == params
    # 本文の長さはバイト数（UTF-8）で数える: 大きい非 ASCII の応答も欠けずに届く
    big = await client.request("big", {"n": 50_000}, timeout_ms=10_000)
    assert big == "あ😀" * 50_000
    # 分割して届く枠もつなげて読める
    assert await client.request("split", None, timeout_ms=5000) == "x" * 5000 + "日本語"


async def test_concurrent_requests_are_matched_by_id(spawn: Spawn):
    client = await spawn()
    results = await asyncio.gather(*(client.request("echo", {"i": i}, timeout_ms=5000) for i in range(20)))
    assert results == [{"i": i} for i in range(20)]


async def test_initialize_sends_utf8_and_utf16_and_keeps_the_server_info(spawn: Spawn, tmp_path: Path):
    client = await spawn()
    state = await client.request("state", None, timeout_ms=5000)
    assert state["initialized"] is True
    params = state["init_params"]
    assert params["capabilities"]["general"]["positionEncodings"] == ["utf-8", "utf-16"]
    assert params["rootUri"] == tmp_path.as_uri()
    assert params["workspaceFolders"] == [{"uri": tmp_path.as_uri(), "name": tmp_path.name}]
    assert client.server_info == {"name": "fake", "version": "1"}
    assert client.server_capabilities["textDocumentSync"] == 2


async def test_initialize_passes_locale_and_initialization_options(spawn: Spawn, tmp_path: Path):
    client = await spawn(initialize=False)
    await client.initialize(
        root_uri=tmp_path.as_uri(),
        root_path=tmp_path,
        initialization_options={"tsserver": {"path": "/x/tsserver.js"}},
        locale="en",
        timeout_ms=10_000,
    )
    params = (await client.request("state", None, timeout_ms=5000))["init_params"]
    assert params["locale"] == "en"
    assert params["initializationOptions"] == {"tsserver": {"path": "/x/tsserver.js"}}
    # 省いたときはキーごと送らない
    other = await spawn(initialize=False)
    await other.initialize(root_uri=tmp_path.as_uri(), timeout_ms=10_000)
    params = (await other.request("state", None, timeout_ms=5000))["init_params"]
    assert "locale" not in params
    assert "initializationOptions" not in params


@pytest.mark.parametrize(
    ("server_arg", "expected"),
    [
        ("utf-16", Unit.UTF16),
        ("utf-8", Unit.UTF8),
        ("utf-32", Unit.CODEPOINT),
        ("absent", Unit.UTF16),  # basedpyright / vtsls は返さない（S-4・S-5）
        ("utf-7", Unit.UTF16),  # 未知の値は UTF-16 にして警告
    ],
)
async def test_negotiated_encoding(spawn: Spawn, server_arg: str, expected: Unit):
    client = await spawn(server_arg)
    assert client.negotiated_encoding is expected


async def test_error_response_raises_lsp_error(spawn: Spawn):
    client = await spawn()
    with pytest.raises(LspError) as info:
        await client.request("fail", None, timeout_ms=5000)
    assert (info.value.code, info.value.message, info.value.data) == (-32602, "bad params", {"x": 1})
    assert await client.request("echo", 1, timeout_ms=5000) == 1  # 接続は生きている


async def test_timeout_sends_cancel_request_then_raises(spawn: Spawn):
    client = await spawn()
    cancelled = asyncio.Event()
    seen: list[Any] = []

    def on_cancelled(params: Any) -> None:
        seen.append(params["id"])
        cancelled.set()

    client.on_notification("test/cancelled", on_cancelled)
    with pytest.raises(TimeoutError):
        await client.request("slow", None, timeout_ms=150)
    await asyncio.wait_for(cancelled.wait(), 5)
    state = await client.request("state", None, timeout_ms=5000)
    assert state["slow_ids"] == seen == state["cancelled"]
    assert len(seen) == 1
    assert await client.request("echo", "still alive", timeout_ms=5000) == "still alive"


async def test_cancelling_the_waiting_task_sends_cancel_request(spawn: Spawn):
    client = await spawn()
    cancelled = asyncio.Event()
    client.on_notification("test/cancelled", lambda _p: cancelled.set())
    task = asyncio.create_task(client.request("slow", None, timeout_ms=60_000))
    await eventually(lambda: bool(client._pending))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), 5)


async def test_server_requests_get_default_answers(spawn: Spawn, tmp_path: Path):
    client = await spawn()
    client.set_settings(
        {
            "python": {"analysis": {"typeCheckingMode": "standard"}, "pythonPath": "/py"},
            "basedpyright": {"a": 1},
        }
    )

    async def ask(method: str, params: Any = None) -> dict[str, Any]:
        return await client.request("ask", {"method": method, "params": params}, timeout_ms=5000)

    resp = await ask(
        "workspace/configuration",
        {
            "items": [
                {"section": "python.analysis"},
                {"section": "python"},
                {"section": "nope"},
                {"section": ""},
                {},
            ]
        },
    )
    assert resp["id"].startswith("srv-")  # サーバーが付けた（文字列の）id をそのまま返している
    analysis = {"typeCheckingMode": "standard"}
    python = {"analysis": analysis, "pythonPath": "/py"}
    assert resp["result"] == [
        analysis,
        python,
        {},
        {"python": python, "basedpyright": {"a": 1}},
        {"python": python, "basedpyright": {"a": 1}},
    ]
    for method in (
        "client/registerCapability",
        "client/unregisterCapability",
        "window/workDoneProgress/create",
    ):
        resp = await ask(method, {"registrations": [], "token": "t"})
        assert resp["result"] is None
        assert "error" not in resp
    folders = await ask("workspace/workspaceFolders")
    assert folders["result"] == [{"uri": tmp_path.as_uri(), "name": tmp_path.name}]
    unknown = await ask("workspace/somethingNew", {})
    assert unknown["error"]["code"] == METHOD_NOT_FOUND
    assert "result" not in unknown


async def test_configuration_is_empty_without_settings(spawn: Spawn):
    client = await spawn()
    resp = await client.request(
        "ask",
        {"method": "workspace/configuration", "params": {"items": [{"section": "python"}, {"section": ""}]}},
        timeout_ms=5000,
    )
    assert resp["result"] == [{}, {}]


async def test_custom_request_handlers(spawn: Spawn):
    client = await spawn()
    calls: list[Any] = []

    def sync_handler(params: Any) -> Any:
        calls.append(params)
        return {"sync": True}

    async def async_handler(params: Any) -> Any:
        await asyncio.sleep(0.05)
        return {"async": params}

    def broken(_params: Any) -> Any:
        raise RuntimeError("secret detail")

    def lsp_error(_params: Any) -> Any:
        raise LspError(-32800, "cancelled by handler", {"why": "test"})

    client.on_request("t/sync", sync_handler)
    client.on_request("t/async", async_handler)
    client.on_request("t/broken", broken)
    client.on_request("t/lsp-error", lsp_error)

    async def ask(method: str) -> dict[str, Any]:
        return await client.request("ask", {"method": method, "params": {"k": method}}, timeout_ms=5000)

    assert (await ask("t/sync"))["result"] == {"sync": True}
    assert calls == [{"k": "t/sync"}]
    assert (await ask("t/async"))["result"] == {"async": {"k": "t/async"}}
    error = (await ask("t/broken"))["error"]
    assert error["code"] == INTERNAL_ERROR
    assert "secret detail" not in error["message"]
    error = (await ask("t/lsp-error"))["error"]
    assert (error["code"], error["message"], error["data"]) == (
        -32800,
        "cancelled by handler",
        {"why": "test"},
    )
    assert (
        await client.request("echo", "alive", timeout_ms=5000) == "alive"
    )  # ハンドラの失敗で読み取りは止まらない


async def test_notification_handlers_run_in_order(spawn: Spawn):
    client = await spawn()
    order: list[str] = []
    async_done = asyncio.Event()

    def first(params: Any) -> None:
        order.append(f"first{params['i']}")

    def second(params: Any) -> None:
        order.append(f"second{params['i']}")

    def failing(_params: Any) -> None:
        raise RuntimeError("handler bug")

    async def slow_async(params: Any) -> None:
        await asyncio.sleep(0.05)
        order.append(f"async{params['i']}")
        if params["i"] == 2:
            async_done.set()

    client.on_notification("test/event", failing)
    client.on_notification("test/event", first)
    client.on_notification("test/event", second)
    client.on_notification("test/event", slow_async)
    assert await client.request("emit", {"n": 3}, timeout_ms=5000) == "emitted"
    # 応答より前に届いた通知の同期ハンドラは、応答が返った時点で済んでいる（受信順・登録順）
    assert [o for o in order if not o.startswith("async")] == [
        f"{h}{i}" for i in range(3) for h in ("first", "second")
    ]
    await asyncio.wait_for(async_done.wait(), 5)
    assert [o for o in order if o.startswith("async")] == ["async0", "async1", "async2"]


async def test_log_message_and_stderr_go_to_the_log(spawn: Spawn, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG, logger="lucianke.lsp.client")
    client = await spawn()
    await client.request("emit", {"n": 0}, timeout_ms=5000)
    await eventually(lambda: "fake-server started" in client.stderr_tail)
    records = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "lucianke.lsp.client"]
    assert (logging.WARNING, "[fake] hello from fake") in records  # MessageType 2 = Warning
    assert (logging.DEBUG, "[fake] stderr: fake-server started") in records


# --- 終了・切断 -----------------------------------------------------------------------------


async def test_shutdown_leaves_no_child_process(spawn: Spawn, registry: ProcessRegistry):
    client = await spawn()
    assert len(registry.live()) == 1
    proc_pid = client.pid
    assert proc_pid is not None
    await client.shutdown(2000)
    assert registry.live() == []
    assert client.returncode == 0  # shutdown → exit で自分から終わった（強制終了ではない）
    assert client.closed_reason in ("server closed stdout", "shutdown")
    assert not client.is_connected
    await client.shutdown(200)  # 2 回目は何もしない
    with pytest.raises(ConnectionError):
        await client.request("echo", 1, timeout_ms=1000)
    with pytest.raises(ConnectionError):
        client.notify("x")


async def test_shutdown_terminates_a_server_that_ignores_exit(spawn: Spawn, registry: ProcessRegistry):
    client = await spawn("--stubborn")
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await client.shutdown(300)
    assert loop.time() - t0 < 10
    assert registry.live() == []
    assert client.returncode is not None


async def test_wait_closed_and_pending_requests_fail_when_the_server_dies(
    spawn: Spawn, registry: ProcessRegistry
):
    client = await spawn()
    with pytest.raises(ConnectionError):
        await client.request("die", None, timeout_ms=5000)
    await asyncio.wait_for(client.wait_closed(), 5)
    assert client.closed_reason == "server closed stdout"
    with pytest.raises(ConnectionError):
        await client.request("echo", 1, timeout_ms=1000)
    await client.shutdown(200)
    assert registry.live() == []


@pytest.mark.parametrize(
    "kind",
    ["no-length", "no-colon", "bad-length", "huge", "bad-json", "not-object", "bad-utf8", "truncated"],
)
async def test_bad_frame_disconnects(
    spawn: Spawn, registry: ProcessRegistry, caplog: pytest.LogCaptureFixture, kind: str
):
    caplog.set_level(logging.ERROR, logger="lucianke.lsp.client")
    client = await spawn()
    with pytest.raises(ConnectionError):  # 待っていた要求が切断で失敗する
        await client.request("break", {"kind": kind}, timeout_ms=5000)
    await asyncio.wait_for(client.wait_closed(), 5)
    assert client.closed_reason is not None
    assert client.closed_reason.startswith("bad frame")
    assert any("bad frame" in r.getMessage() for r in caplog.records)
    assert not client.is_connected
    with pytest.raises(ConnectionError):
        await client.request("echo", 1, timeout_ms=1000)
    with pytest.raises(ConnectionError):
        client.notify("textDocument/didClose", {})
    # 子はまだ生きている（truncated 以外）。切断済みなので shutdown 要求は省いて回収だけで残さない
    await client.shutdown(200)
    assert registry.live() == []


async def test_requests_before_start_raise_connection_error():
    client = LspClient("never-started")
    with pytest.raises(ConnectionError):
        await client.request("echo", 1, timeout_ms=1000)
    with pytest.raises(ConnectionError):
        client.notify("x")
    await client.shutdown(100)  # 起動していなければ何もしない


async def test_start_failure_leaves_no_registry_entry(registry: ProcessRegistry, tmp_path: Path):
    client = LspClient("missing")
    with pytest.raises(FileNotFoundError):
        await client.start([str(tmp_path / "no-such-lsp-server")], cwd=tmp_path, registry=registry)
    assert registry.live() == []
    assert not client.is_connected


async def test_child_gets_the_minimal_environment(registry: ProcessRegistry, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LUCIANKE_TEST_SECRET", "hunter2")
    script = tmp_path / "env_dump.py"
    script.write_text(
        "import json, os, sys\n"
        "body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'secret': os.environ.get('LUCIANKE_TEST_SECRET'),"
        " 'extra': os.environ.get('EXTRA_FLAG')}}).encode()\n"
        "sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(b'Content-Length: %d\\r\\n\\r\\n' % len(body) + body); sys.stdout.buffer.flush()\n"
        "sys.stdin.buffer.read()\n",
        encoding="utf-8",
    )
    client = LspClient("env")
    await client.start(
        [sys.executable, str(script)], cwd=tmp_path, env={"EXTRA_FLAG": "1"}, registry=registry
    )
    try:
        # 1 行目（ヘッダ）を読んだら応答するスクリプト。id=1 が最初の要求の id
        result = await client.request("initialize-ish", None, timeout_ms=10_000)
    finally:
        await client.shutdown(200)
    assert result == {"secret": None, "extra": "1"}
    assert registry.live() == []


# --- 位置変換（ADR-14） ---------------------------------------------------------------------

TEXT = 'ascii line\ns = "全角😀x"\ne\u0301 and 𝒳 tail\r\n\r\nlast😀'
LINES = ["ascii line", 's = "全角😀x"', "e\u0301 and 𝒳 tail", "", "last😀"]


def line_index() -> LineIndex:
    return LineIndex(TEXT.encode("utf-8"))


def test_position_conversion_expected_values():
    li = line_index()
    # 行 1: s(0) ' '(1) =(2) ' '(3) "(4) 全(5) 角(6) 😀(7) x(8) "(9)
    assert to_lsp_position(li, Position(1, 7), Unit.UTF16) == {"line": 1, "character": 7}
    assert to_lsp_position(li, Position(1, 8), Unit.UTF16) == {"line": 1, "character": 9}  # 😀 は 2 単位
    assert to_lsp_position(li, Position(1, 7), Unit.UTF8) == {
        "line": 1,
        "character": 11,
    }  # 全角 3 バイト × 2 + 5
    assert to_lsp_position(li, Position(1, 8), Unit.UTF8) == {"line": 1, "character": 15}
    assert to_lsp_position(li, Position(1, 8), Unit.CODEPOINT) == {"line": 1, "character": 8}
    assert from_lsp_position(li, {"line": 1, "character": 9}, Unit.UTF16) == Position(1, 8)
    assert from_lsp_position(li, {"line": 1, "character": 15}, Unit.UTF8) == Position(1, 8)
    # 結合文字は 2 コードポイント（行 2）、CRLF の行（行 2 の終わり）でも行頭が合う
    assert to_lsp_position(li, Position(2, 2), Unit.UTF16) == {"line": 2, "character": 2}
    assert to_lsp_position(li, Position(4, 5), Unit.UTF16) == {"line": 4, "character": 6}
    assert from_lsp_position(li, {"line": 4, "character": 6}, Unit.UTF16) == Position(4, 5)


@pytest.mark.parametrize("encoding", [Unit.UTF16, Unit.UTF8, Unit.CODEPOINT])
def test_position_roundtrip_every_column(encoding: Unit):
    li = line_index()
    for line, text in enumerate(LINES):
        for col in range(len(text) + 1):
            pos = Position(line, col)
            lsp = to_lsp_position(li, pos, encoding)
            assert lsp["line"] == line
            assert from_lsp_position(li, lsp, encoding) == pos


def test_utf16_utf8_codepoint_agree_via_the_codepoint_hub():
    """utf-16 → コードポイント → utf-8 → コードポイント → utf-16 の往復が全列で閉じる。"""
    li = line_index()
    for line, text in enumerate(LINES):
        for col in range(len(text) + 1):
            u16 = to_lsp_position(li, Position(line, col), Unit.UTF16)
            cp = from_lsp_position(li, u16, Unit.UTF16)
            u8 = to_lsp_position(li, cp, Unit.UTF8)
            assert from_lsp_position(li, u8, Unit.UTF8) == Position(line, col)


def test_from_lsp_position_clamps_like_the_lsp_spec():
    li = line_index()
    assert from_lsp_position(li, {"line": 0, "character": 2_147_483_647}) == Position(0, 10)  # 行末
    assert from_lsp_position(li, {"line": 1, "character": 2_147_483_647}) == Position(1, 10)
    assert from_lsp_position(li, {"line": 99, "character": 0}) == Position(4, 5)  # 文書末
    assert from_lsp_position(li, {"line": 1, "character": 8}, Unit.UTF16) == Position(
        1, 7
    )  # 😀 の途中は直前の境界
    with pytest.raises(ValueError):
        from_lsp_position(li, {"line": -1, "character": 0})
    with pytest.raises(ValueError):
        to_lsp_position(li, Position(99, 0))


EMOJI_KANA = "a😀かな"  # 4 コードポイント: UTF-16 では 5 単位、UTF-8 では 1+4+3+3=11 バイト


def emoji_kana_index() -> LineIndex:
    return LineIndex((EMOJI_KANA + "\nsecond\n").encode("utf-8"))


@pytest.mark.parametrize("encoding", [Unit.UTF16, Unit.UTF8, Unit.CODEPOINT])
def test_to_lsp_position_refuses_a_clamped_column_instead_of_returning_the_rounded_one(encoding: Unit):
    """BUG-0010: 送る列は文字境界だけ（設計書 §7.5.3 手順 3、S-11）。行末を越える列は丸めた値を返さず降格の例外にする。"""
    li = emoji_kana_index()
    with pytest.raises(LuciankeError) as e:
        to_lsp_position(li, Position(0, 99), encoding)
    assert e.value.code is ErrorCode.E_LSP_UNAVAILABLE
    assert e.value.details == {"reason": "position_clamped"}
    assert "1:100" in e.value.message and "not sent" in e.value.message
    # 行末を 1 つ越えただけでも丸めになる（境界の隣は通り、その次は通らない）
    to_lsp_position(li, Position(0, len(EMOJI_KANA)), encoding)
    with pytest.raises(LuciankeError):
        to_lsp_position(li, Position(0, len(EMOJI_KANA) + 1), encoding)


@pytest.mark.parametrize("encoding", [Unit.UTF16, Unit.UTF8, Unit.CODEPOINT])
def test_to_lsp_position_passes_every_boundary_column(encoding: Unit):
    li = emoji_kana_index()
    expected = {
        Unit.UTF16: [0, 1, 3, 4, 5],
        Unit.UTF8: [0, 1, 5, 8, 11],
        Unit.CODEPOINT: [0, 1, 2, 3, 4],
    }[encoding]
    got = [to_lsp_position(li, Position(0, col), encoding) for col in range(len(EMOJI_KANA) + 1)]
    assert got == [{"line": 0, "character": c} for c in expected]
    # 次の行は丸めに巻き込まれない
    assert to_lsp_position(li, Position(1, 6), encoding) == {"line": 1, "character": 6}


def test_to_lsp_position_details_are_merged_and_reason_wins():
    li = emoji_kana_index()
    with pytest.raises(LuciankeError) as e:
        to_lsp_position(
            li, Position(0, 99), details={"server": "pyright", "language": "python", "reason": "x"}
        )
    assert e.value.details == {"reason": "position_clamped", "server": "pyright", "language": "python"}


def test_to_lsp_position_keeps_value_error_for_a_line_outside_the_file():
    with pytest.raises(ValueError):
        to_lsp_position(emoji_kana_index(), Position(99, 0))


@pytest.mark.parametrize("encoding", [Unit.UTF16, Unit.UTF8])
def test_to_lsp_range_refuses_when_either_end_is_clamped(encoding: Unit):
    li = emoji_kana_index()
    ok = Range(Position(0, 1), Position(0, 3))
    assert to_lsp_range(li, ok, encoding) == {
        "start": to_lsp_position(li, ok.start, encoding),
        "end": to_lsp_position(li, ok.end, encoding),
    }
    for bad in (Range(Position(0, 99), Position(1, 2)), Range(Position(0, 1), Position(0, 99))):
        with pytest.raises(LuciankeError) as e:
            to_lsp_range(li, bad, encoding, details={"server": "s"})
        assert e.value.code is ErrorCode.E_LSP_UNAVAILABLE
        assert e.value.details == {"reason": "position_clamped", "server": "s"}


def test_client_methods_refuse_a_clamped_position_in_both_encodings():
    li = emoji_kana_index()
    client = LspClient("unstarted")
    for encoding in (Unit.UTF16, Unit.UTF8):
        client.negotiated_encoding = encoding
        with pytest.raises(LuciankeError) as e:
            client.to_lsp_position(li, Position(0, 99), details={"server": "unstarted"})
        assert e.value.details == {"reason": "position_clamped", "server": "unstarted"}
        with pytest.raises(LuciankeError):
            client.to_lsp_range(li, Range(Position(0, 0), Position(0, 99)))
        # 受信側（LSP → 内部）の丸めは許す（設計書どおり）
        assert client.from_lsp_position(li, {"line": 0, "character": 2_147_483_647}) == Position(
            0, len(EMOJI_KANA)
        )


def test_client_methods_use_the_negotiated_encoding_without_spawning():
    li = line_index()
    client = LspClient("unstarted")
    assert client.negotiated_encoding is Unit.UTF16
    assert client.to_lsp_position(li, Position(1, 8)) == {"line": 1, "character": 9}
    client.negotiated_encoding = Unit.UTF8
    assert client.to_lsp_position(li, Position(1, 8)) == {"line": 1, "character": 15}
    assert client.from_lsp_position(li, {"line": 1, "character": 15}) == Position(1, 8)
    rng = Range(Position(1, 5), Position(1, 8))
    lsp = client.to_lsp_range(li, rng)
    assert lsp == {"start": {"line": 1, "character": 5}, "end": {"line": 1, "character": 15}}
    assert client.from_lsp_range(li, lsp) == rng


# --- servers --------------------------------------------------------------------------------


def exe(name: str) -> str:
    """プラットフォームの実行ファイル名（Windows は ``.exe``）。"""
    return name + ".exe" if sys.platform == "win32" else name


def shim(name: str) -> str:
    """node_modules/.bin の実行ファイル名（Windows は ``.cmd``）。"""
    return name + ".cmd" if sys.platform == "win32" else name


def make_exe(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def venv_bin(root: Path) -> Path:
    return root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")


def test_env_var_names():
    assert env_var_name("python") == "LUCIANKE_LSP_PYTHON_CMD"
    assert env_var_name("python", "lint") == "LUCIANKE_LSP_PYTHON_LINT_CMD"
    assert env_var_name("c_sharp") == "LUCIANKE_LSP_C_SHARP_CMD"
    assert env_var_name("tsx") == "LUCIANKE_LSP_TSX_CMD"
    assert server_env_var_name("ruff") == "LUCIANKE_LSP_RUFF_CMD"
    assert server_env_var_name("typescript-language-server") == "LUCIANKE_LSP_TYPESCRIPT_LANGUAGE_SERVER_CMD"
    assert default_server("python").server_env_var == "LUCIANKE_LSP_BASEDPYRIGHT_CMD"  # type: ignore[union-attr]


def test_env_var_overrides_everything(tmp_path: Path):
    make_exe(venv_bin(tmp_path), exe("basedpyright-langserver"))
    based = default_server("python")
    assert based is not None
    argv = find_command(
        based, workspace_root=tmp_path, environ={"LUCIANKE_LSP_PYTHON_CMD": "my-lsp --stdio -v"}
    )
    assert argv == ["my-lsp", "--stdio", "-v"]
    # 空・空白だけの値は「未指定」
    found = find_command(
        based, workspace_root=tmp_path, environ={"LUCIANKE_LSP_PYTHON_CMD": "  ", "PATH": ""}
    )
    assert found is not None
    assert Path(found[0]).parent == venv_bin(tmp_path)


def test_env_var_override_applies_per_role(tmp_path: Path):
    env = {"LUCIANKE_LSP_PYTHON_CMD": "my-types --stdio", "PATH": ""}
    assert find_command(servers_for("python", "types")[0], workspace_root=tmp_path, environ=env) == [
        "my-types",
        "--stdio",
    ]
    assert find_command(servers_for("python", "types")[1], workspace_root=tmp_path, environ=env) == [
        "my-types",
        "--stdio",
    ]
    assert (
        find_command(servers_for("python", "lint")[0], workspace_root=tmp_path, environ=env) is None
    )  # ruff には効かない
    env = {"LUCIANKE_LSP_PYTHON_LINT_CMD": "my-lint server", "PATH": ""}
    assert find_command(servers_for("python", "lint")[0], workspace_root=tmp_path, environ=env) == [
        "my-lint",
        "server",
    ]


def test_server_name_env_var_overrides_only_that_server(tmp_path: Path):
    based, pyright = servers_for("python", "types")
    ruff = default_server("python", "lint")
    assert ruff is not None
    env = {
        "LUCIANKE_LSP_BASEDPYRIGHT_CMD": "my-based --stdio",
        "LUCIANKE_LSP_RUFF_CMD": "my-ruff server",
        "PATH": "",
    }
    assert find_command(based, workspace_root=tmp_path, environ=env) == ["my-based", "--stdio"]
    assert find_command(ruff, workspace_root=tmp_path, environ=env) == ["my-ruff", "server"]
    assert find_command(pyright, workspace_root=tmp_path, environ=env) is None  # 代替には効かない
    # サーバー名 → 言語の順。サーバー名の値が不正なら言語単位に落ちる
    env = {"LUCIANKE_LSP_BASEDPYRIGHT_CMD": "by-name", "LUCIANKE_LSP_PYTHON_CMD": "by-language", "PATH": ""}
    assert find_command(based, workspace_root=tmp_path, environ=env) == ["by-name"]
    assert find_command(pyright, workspace_root=tmp_path, environ=env) == ["by-language"]
    env = {"LUCIANKE_LSP_BASEDPYRIGHT_CMD": '"broken', "LUCIANKE_LSP_PYTHON_CMD": "by-language", "PATH": ""}
    assert find_command(based, workspace_root=tmp_path, environ=env) == ["by-language"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のパスの扱い")
def test_split_command_keeps_windows_backslashes():
    assert split_command(r"C:\tools\ls.exe --stdio") == [r"C:\tools\ls.exe", "--stdio"]
    assert split_command(r'"C:\Program Files\ls\ls.exe" --stdio') == [
        r"C:\Program Files\ls\ls.exe",
        "--stdio",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のシェル風の分割")
def test_split_command_posix():
    assert split_command("'/opt/my tools/ls' --stdio") == ["/opt/my tools/ls", "--stdio"]


def test_invalid_env_value_is_ignored_with_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    make_exe(venv_bin(tmp_path), exe("ruff"))
    ruff = default_server("python", "lint")
    assert ruff is not None
    caplog.set_level(logging.WARNING, logger="lucianke.lsp.servers")
    argv = find_command(
        ruff, workspace_root=tmp_path, environ={"LUCIANKE_LSP_PYTHON_LINT_CMD": '"unclosed', "PATH": ""}
    )
    assert argv is not None
    assert argv[1:] == ["server"]
    assert any("LUCIANKE_LSP_PYTHON_LINT_CMD" in r.getMessage() for r in caplog.records)


def test_search_order_venv_then_node_modules_then_path(tmp_path: Path):
    ruff = default_server("python", "lint")
    assert ruff is not None
    path_dir = tmp_path / "pathdir"
    explicit_venv = tmp_path / "other-venv"
    in_explicit = make_exe(venv_bin_of(explicit_venv), exe("ruff"))
    in_venv = make_exe(venv_bin(tmp_path), exe("ruff"))
    in_modules = make_exe(tmp_path / "node_modules" / ".bin", shim("ruff"))
    in_path = make_exe(path_dir, exe("ruff"))
    env = {"PATH": str(path_dir)}

    def find(**kw: Any) -> list[str] | None:
        return find_command(ruff, workspace_root=tmp_path, environ=env, **kw)

    assert find(venv=explicit_venv) == [str(in_explicit), "server"]
    assert find() == [str(in_venv), "server"]
    in_venv.unlink()
    assert find() == [str(in_modules), "server"]
    in_modules.unlink()
    assert find() == [str(in_path), "server"]
    in_path.unlink()
    assert find() is None
    assert find(venv=explicit_venv) == [str(in_explicit), "server"]


def venv_bin_of(venv: Path) -> Path:
    return venv / ("Scripts" if sys.platform == "win32" else "bin")


def test_typescript_servers_are_found_in_node_modules_bin(tmp_path: Path):
    tsls, vtsls = servers_for("typescript")
    assert (tsls.name, vtsls.name) == ("typescript-language-server", "vtsls")
    env = {"PATH": ""}
    assert find_command(tsls, workspace_root=tmp_path, environ=env) is None
    tsls_shim = make_exe(tmp_path / "node_modules" / ".bin", shim("typescript-language-server"))
    vtsls_shim = make_exe(tmp_path / "node_modules" / ".bin", shim("vtsls"))
    assert find_command(tsls, workspace_root=tmp_path, environ=env) == [str(tsls_shim), "--stdio"]
    assert find_command(vtsls, workspace_root=tmp_path, environ=env) == [str(vtsls_shim), "--stdio"]


def test_only_absolute_path_entries_are_searched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """カレントや相対の PATH 項目にある実行ファイルは掴まない（ワークスペース直下に置かれた偽物を避ける）。"""
    make_exe(tmp_path, exe("ruff"))
    monkeypatch.chdir(tmp_path)
    ruff = default_server("python", "lint")
    assert ruff is not None
    assert (
        find_command(ruff, workspace_root=tmp_path / "elsewhere", environ={"PATH": f".{os.pathsep}"}) is None
    )


def test_candidates_are_tried_in_order(tmp_path: Path):
    defn = LspServerDef(
        language_id="python",
        name="two-candidates",
        argv_candidates=(("first-choice", "--a"), ("second-choice", "--b")),
    )
    bindir = tmp_path / "bin"
    second = make_exe(bindir, exe("second-choice"))
    env = {"PATH": str(bindir)}
    assert find_command(defn, workspace_root=tmp_path, environ=env) == [str(second), "--b"]
    first = make_exe(bindir, exe("first-choice"))
    assert find_command(defn, workspace_root=tmp_path, environ=env) == [str(first), "--a"]


def test_repo_venv_has_basedpyright_and_ruff():
    """このリポジトリの .venv（dev グループ）から見つかる。PATH を空にして、.venv の探索だけで見つけたことを確かめる。"""
    if not (REPO / ".venv").is_dir():
        pytest.skip("no .venv in this checkout")
    env = {"PATH": ""}  # LUCIANKE_LSP_* も渡さない
    based = default_server("python")
    ruff = default_server("python", "lint")
    assert based is not None
    assert ruff is not None
    argv = find_command(based, workspace_root=REPO, environ=env)
    assert argv is not None
    assert Path(argv[0]).parent == venv_bin(REPO)
    assert Path(argv[0]).stem == "basedpyright-langserver"
    assert argv[1:] == ["--stdio"]
    argv = find_command(ruff, workspace_root=REPO, environ=env)
    assert argv is not None
    assert Path(argv[0]).stem == "ruff"
    assert argv[1:] == ["server"]


def test_missing_server_returns_none(tmp_path: Path):
    rust = default_server("rust")
    assert rust is not None
    assert rust.best_effort
    assert find_command(rust, workspace_root=tmp_path, environ={"PATH": str(tmp_path / "empty")}) is None


def test_server_table_shape():
    assert [d.name for d in servers_for("python", "types")] == ["basedpyright", "pyright"]
    assert [d.name for d in servers_for("python", "lint")] == ["ruff"]
    assert [d.name for d in servers_for("python")] == ["basedpyright", "pyright", "ruff"]
    for lang, lsp_lang in (
        ("typescript", "typescript"),
        ("tsx", "typescriptreact"),
        ("javascript", "javascript"),
    ):
        defs = servers_for(lang)
        assert [d.name for d in defs] == [
            "typescript-language-server",
            "vtsls",
        ]  # S-5: 既定は tsls、代替が vtsls
        assert all(d.document_language_id == lsp_lang for d in defs)
    assert default_server("c_sharp") is not None
    assert default_server("c_sharp").document_language_id == "csharp"  # type: ignore[union-attr]
    assert default_server("no-such-language") is None
    # 同じ言語・役の中で名前は重ならない。起動コマンドの候補は空でない
    seen = {(d.language_id, d.role, d.name) for d in SERVERS}
    assert len(seen) == len(SERVERS)
    assert all(d.argv_candidates and all(c for c in d.argv_candidates) for d in SERVERS)


def test_build_settings_adds_the_interpreter_without_mutating_the_table():
    based = default_server("python")
    assert based is not None
    before = repr(based.settings)
    built = build_settings(based, python_path="/venv/bin/python")
    assert built["python"]["pythonPath"] == "/venv/bin/python"
    assert built["python"]["analysis"]["typeCheckingMode"] == "standard"
    assert "pythonPath" not in based.settings["python"]
    assert repr(based.settings) == before
    ruff = default_server("python", "lint")
    assert ruff is not None
    assert build_settings(ruff, python_path="/x") == {}
    assert "pythonPath" not in build_settings(based)["python"]


def test_find_tsserver(tmp_path: Path):
    assert find_tsserver(tmp_path) is None
    lib = tmp_path / "node_modules" / "typescript" / "lib"
    lib.mkdir(parents=True)
    (lib / "typescript.js").write_text("", encoding="utf-8")  # TypeScript 7 のように tsserver.js が無い
    assert find_tsserver(tmp_path) is None
    tsserver = lib / "tsserver.js"
    tsserver.write_text("", encoding="utf-8")
    assert find_tsserver(tmp_path) == tsserver
    explicit = tmp_path / "elsewhere" / "tsserver.js"
    assert find_tsserver(tmp_path, explicit=explicit) == tsserver  # 実在しない明示指定は無視して次へ
    explicit.parent.mkdir()
    explicit.write_text("", encoding="utf-8")
    assert find_tsserver(tmp_path, explicit=explicit) == explicit


def test_tsserver_location_is_passed_the_way_each_server_wants(tmp_path: Path):
    tsls, vtsls = servers_for("typescript")
    assert tsls.needs_workspace_tsserver
    assert not vtsls.needs_workspace_tsserver
    tsserver = tmp_path / "node_modules" / "typescript" / "lib" / "tsserver.js"
    # tsls: initializationOptions.tsserver.path（settings には載せない）
    assert build_initialization_options(tsls) is None
    assert build_initialization_options(tsls, tsserver_path=tsserver) == {"tsserver": {"path": str(tsserver)}}
    assert build_settings(tsls, tsserver_path=tsserver) == {}
    # vtsls: workspace/configuration の応答（autoUseWorkspaceTsdk と tsdk = lib）。渡さなければ同梱の TypeScript
    assert build_initialization_options(vtsls, tsserver_path=tsserver) is None
    assert build_settings(vtsls) == {}
    assert build_settings(vtsls, tsserver_path=tsserver) == {
        "vtsls": {"autoUseWorkspaceTsdk": True},
        "typescript": {"tsdk": str(tsserver.parent)},
    }


def test_locale_and_requests_per_server():
    based, pyright = servers_for("python", "types")
    ruff = default_server("python", "lint")
    assert ruff is not None
    # S-4: basedpyright の診断は OS のロケールで日本語になる。initialize の locale を "en" に固定する
    assert (based.locale, pyright.locale, ruff.locale) == ("en", "en", None)
    # Python の codeAction（quickfix・organizeImports・fixAll）の供給元は ruff だけ。resolve は ruff のためにある
    assert ruff.supports("textDocument/codeAction")
    assert ruff.supports("codeAction/resolve")
    assert not ruff.supports("textDocument/definition")
    for defn in (based, pyright):
        assert not defn.supports("textDocument/codeAction")
        assert defn.supports("textDocument/definition")
        assert defn.supports("textDocument/rename")
        assert defn.supports("callHierarchy/incomingCalls")
    for defn in servers_for("typescript"):
        assert defn.supports("textDocument/codeAction")
        assert defn.supports("codeAction/resolve")  # vtsls の organizeImports は resolve で edit が付く
