"""lsp/pool.py・documents.py・diagnostics.py: 状態機械・文書同期・診断の版待ち（設計書 §7.5.2・§7.5.4・§7.5.5、S-04）。

偽の LSP サーバー（下の ``FAKE_SERVER``。``sys.executable`` で起動する Python スクリプト）を使う。引数で挙動を切り替える:
``--progress`` ``--no-version`` ``--silent`` ``--crash-first N`` ``--die-before-init`` ``--init-delay`` ``--counter`` ``--log``。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.limits import LSP_RESTART_BACKOFF_S
from lucianke.core.model import LspStatus, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.index.writer import IndexWriter
from lucianke.lsp.diagnostics import PROVISIONAL_QUIET_MS, DiagnosticsHub
from lucianke.lsp.documents import DocumentSync
from lucianke.lsp.pool import FileChangeType, LspHandle, LspPool, LspState
from lucianke.lsp.servers import LspServerDef
from lucianke.workspace.paths import to_file_uri
from lucianke.workspace.types import Workspace

FAKE_SERVER = r"""
import argparse, json, os, sys, threading, time

ap = argparse.ArgumentParser()
ap.add_argument("--no-version", action="store_true")
ap.add_argument("--silent", action="store_true")
ap.add_argument("--progress", type=float, default=None)
ap.add_argument("--crash-first", type=int, default=0)
ap.add_argument("--die-before-init", action="store_true")
ap.add_argument("--init-delay", type=float, default=0.0)
ap.add_argument("--counter")
ap.add_argument("--log")
args = ap.parse_args()

launch = 1
if args.counter:
    try:
        with open(args.counter) as f:
            launch = int(f.read() or 0) + 1
    except FileNotFoundError:
        launch = 1
    with open(args.counter, "w") as f:
        f.write(str(launch))

inp, out = sys.stdin.buffer, sys.stdout.buffer
lock = threading.Lock()


def read():
    headers = {}
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        k, _, v = line.partition(b":")
        headers[k.strip().lower()] = v.strip()
    return json.loads(inp.read(int(headers[b"content-length"])))


def send(obj):
    data = json.dumps(obj).encode()
    with lock:
        out.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        out.flush()


def log(entry):
    if args.log:
        with open(args.log, "a") as f:
            f.write(json.dumps(entry) + "\n")


def run_progress():
    send({"jsonrpc": "2.0", "id": "p1", "method": "window/workDoneProgress/create", "params": {"token": "t"}})
    send({"jsonrpc": "2.0", "method": "$/progress",
          "params": {"token": "t", "value": {"kind": "begin", "title": "indexing", "percentage": 0}}})
    time.sleep(args.progress)
    send({"jsonrpc": "2.0", "method": "$/progress",
          "params": {"token": "t", "value": {"kind": "report", "percentage": 50}}})
    time.sleep(args.progress)
    send({"jsonrpc": "2.0", "method": "$/progress", "params": {"token": "t", "value": {"kind": "end"}}})


while True:
    msg = read()
    if msg is None:
        break
    m, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        if args.die_before_init:
            os._exit(2)
        time.sleep(args.init_delay)
        send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {"textDocumentSync": 1}}})
    elif m == "initialized":
        log({"m": m, "launch": launch})
        if launch <= args.crash_first:
            os._exit(3)
        if args.progress is not None:
            threading.Thread(target=run_progress, daemon=True).start()
    elif m == "shutdown":
        send({"jsonrpc": "2.0", "id": rid, "result": None})
    elif m == "exit":
        sys.exit(0)
    elif m in ("textDocument/didOpen", "textDocument/didChange"):
        td = params["textDocument"]
        log({"m": m, "uri": td["uri"], "version": td.get("version")})
        if not args.silent:
            p = {"uri": td["uri"], "diagnostics": [
                {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                 "message": "v%s" % td.get("version"), "severity": 1}]}
            if not args.no_version:
                p["version"] = td.get("version")
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": p})
    elif m in ("textDocument/didClose", "workspace/didChangeWatchedFiles"):
        log({"m": m, "params": params})
    elif m is not None and rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": None})
"""

IS_WINDOWS = os.name == "nt"
FAKE = LspServerDef(language_id="fake", name="fakels", argv_candidates=(("fakels",),))
FAKE2 = LspServerDef(language_id="fake2", name="fakels2", argv_candidates=(("fakels2",),))
A = RelPath("a.fake")
B = RelPath("b.fake")
C = RelPath("c.fake")


async def wait_until(cond: Callable[[], Any], timeout: float = 15.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


def read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = Path(os.path.realpath(tmp_path / "proj"))
    root.mkdir()
    return Workspace(workspace_id="w", root=root, case_insensitive=IS_WINDOWS)


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(300, close=True)


class Env:
    def __init__(self, tmp_path: Path, registry: ProcessRegistry) -> None:
        self.tmp = tmp_path
        self.registry = registry
        self.script = tmp_path / "fake_lsp.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.log = tmp_path / "server.log"
        self.counter = tmp_path / "counter.txt"
        self.pools: list[LspPool] = []

    @property
    def launches(self) -> int:
        return int(self.counter.read_text()) if self.counter.exists() else 0

    def pool(self, *args: str, servers=(FAKE,), resolver=None, **kw: Any) -> LspPool:
        argv = [
            sys.executable,
            str(self.script),
            "--counter",
            str(self.counter),
            "--log",
            str(self.log),
            *args,
        ]
        kw.setdefault("backoff_s", (0.05, 0.05, 0.05))
        kw.setdefault("ready_fallback_s", 60.0)
        kw.setdefault("grace_ms", 300)
        pool = LspPool(self.registry, servers=servers, resolver=resolver or (lambda d, w: list(argv)), **kw)
        self.pools.append(pool)
        return pool


@pytest.fixture
async def env(tmp_path: Path, registry: ProcessRegistry):
    e = Env(tmp_path, registry)
    yield e
    for pool in e.pools:
        await pool.shutdown_all(300)


async def ready_handle(pool: LspPool, ws: Workspace, language_id: str = "fake") -> LspHandle:
    assert await pool.wait_ready(language_id, ws, 20_000)
    return pool.handles(ws, language_id)[0]


# --- 状態遷移 -----------------------------------------------------------------------------


async def test_status_without_handle(env: Env, ws: Workspace):
    pool = env.pool()
    assert pool.status("fake", ws) == (LspStatus.STANDBY, None, "fakels", None, None)
    assert pool.status("no-such-language", ws).status is LspStatus.NOT_APPLICABLE
    assert env.registry.live() == []


async def test_standby_to_ready_via_progress(env: Env, ws: Workspace):
    pool = env.pool("--progress", "0.5")
    await pool.ensure_started("fake", ws)  # 待たずに戻る
    assert pool.status("fake", ws).status is LspStatus.INDEXING  # starting は indexing として見える
    await wait_until(lambda: pool.status("fake", ws).progress == 50)
    assert pool.status("fake", ws).status is LspStatus.INDEXING  # end が来るまで ready にしない
    assert await pool.wait_ready("fake", ws, 20_000)
    status = pool.status("fake", ws)
    assert status.status is LspStatus.READY
    assert status.progress is None
    assert status.server_name == "fakels"
    assert status.pid is not None and status.reason is None
    h = pool.handles(ws)[0]
    assert list(h.history) == [LspState.STANDBY, LspState.STARTING, LspState.INDEXING, LspState.READY]


async def test_first_publish_diagnostics_makes_ready(env: Env, ws: Workspace):
    pool = env.pool()
    await pool.ensure_started("fake", ws)
    await wait_until(lambda: pool.handles(ws) and pool.handles(ws)[0].state is LspState.INDEXING)
    assert not await pool.wait_ready("fake", ws, 300)  # 信号が無い間は indexing のまま
    h = pool.handles(ws)[0]
    h.documents.did_open(A, "x = 1\n", "fake")
    assert await pool.wait_ready("fake", ws, 20_000)
    assert pool.get("fake", ws) is h


async def test_ready_fallback_when_no_signal(env: Env, ws: Workspace):
    pool = env.pool("--silent", ready_fallback_s=0.3)
    assert await pool.wait_ready("fake", ws, 20_000)  # wait_ready は standby なら起動する


async def test_ensure_started_twice_spawns_one_process(env: Env, ws: Workspace):
    pool = env.pool("--silent", ready_fallback_s=0.3)
    await asyncio.gather(*(pool.ensure_started("fake", ws) for _ in range(3)))
    await pool.ensure_started("fake", ws)
    await ready_handle(pool, ws)
    await pool.ensure_started("fake", ws)
    assert len(env.registry.live()) == 1
    assert env.launches == 1


async def test_get_starts_standby_and_returns_none_until_ready(env: Env, ws: Workspace):
    pool = env.pool("--silent", ready_fallback_s=0.3)
    assert pool.get("fake", ws) is None  # ブロックせず、起動を投げる
    assert pool.get("fake", ws, start_if_needed=False) is None
    assert pool.status("fake", ws).status is LspStatus.INDEXING
    h = await ready_handle(pool, ws)
    assert pool.get("fake", ws) is h


async def test_restart_after_crash_with_backoff(env: Env, ws: Workspace):
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        await asyncio.sleep(0)

    # ready の合図は ``$/progress`` の end だけにする（3 回目の起動だけが出す。落ちる起動は ``initialized`` で死ぬ）。
    # ``--silent`` と ``ready_fallback_s=0.3`` の組では、負荷で偽サーバーが ``initialized`` を処理する前の 0.3 秒が
    # 過ぎると、これから落ちる起動が時間切れで ready になり、起動回数・再起動回数の表明が先に走って落ちた（BUG-0022）。
    pool = env.pool("--progress", "0.05", "--crash-first", "2", sleep=fake_sleep)
    h = await ready_handle(pool, ws)
    assert env.launches == 3
    assert h.restarts == 2
    assert delays == [0.05, 0.05]
    assert len(env.registry.live()) == 1


async def test_unavailable_after_three_restarts(env: Env, ws: Workspace):
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        await asyncio.sleep(0)

    pool = env.pool("--crash-first", "99", backoff_s=LSP_RESTART_BACKOFF_S, sleep=fake_sleep)
    assert not await pool.wait_ready("fake", ws, 30_000)
    status = pool.status("fake", ws)
    assert status.status is LspStatus.UNAVAILABLE
    assert status.reason == "crashed_3x"
    assert status.pid is None
    assert delays == [1, 2, 4]  # 1 / 2 / 4 秒のバックオフで 3 回
    assert env.launches == 4  # 最初の起動 + 再起動 3 回。4 回目の落ちで諦める
    assert env.registry.live() == []
    await pool.ensure_started("fake", ws)  # unavailable は reset までそのまま
    assert pool.status("fake", ws).status is LspStatus.UNAVAILABLE
    assert env.launches == 4

    await pool.reset("fake", ws)
    assert pool.status("fake", ws) == (LspStatus.STANDBY, None, "fakels", None, None)
    assert pool.handles(ws)[0].restarts == 0


async def test_init_failed_after_retries(env: Env, ws: Workspace):
    pool = env.pool("--die-before-init")
    assert not await pool.wait_ready("fake", ws, 30_000)
    status = pool.status("fake", ws)
    assert status.status is LspStatus.UNAVAILABLE and status.reason == "init_failed"
    assert env.launches == 4
    assert env.registry.live() == []


async def test_not_found_is_unavailable_without_process(env: Env, ws: Workspace):
    pool = env.pool(resolver=lambda d, w: None)
    assert not await pool.wait_ready("fake", ws, 10_000)
    assert pool.status("fake", ws)[0] is LspStatus.UNAVAILABLE
    assert pool.status("fake", ws).reason == "not_found"
    assert env.registry.live() == [] and env.launches == 0


async def test_spawn_failure_is_init_failed(env: Env, ws: Workspace):
    pool = env.pool(resolver=lambda d, w: [str(ws.root / "no-such-binary-xyz")])
    assert not await pool.wait_ready("fake", ws, 10_000)
    assert pool.status("fake", ws).reason == "init_failed"
    assert env.registry.live() == []


async def test_three_consecutive_timeouts_make_it_unavailable(env: Env, ws: Workspace):
    pool = env.pool("--silent", ready_fallback_s=0.2)
    h = await ready_handle(pool, ws)
    assert not h.record_timeout() and not h.record_timeout()
    h.record_ok()  # 連続でなくなる
    assert not h.record_timeout() and not h.record_timeout()
    assert h.record_timeout()  # 連続 3 回
    await wait_until(lambda: h.state is LspState.UNAVAILABLE)
    assert pool.status("fake", ws).reason == "timeout_3x"
    await wait_until(lambda: env.registry.live() == [])
    assert not h.record_timeout()  # すでに unavailable


async def test_shutdown_all_leaves_no_children(env: Env, ws: Workspace):
    pool = env.pool("--silent", servers=(FAKE, FAKE2), ready_fallback_s=0.2)
    await ready_handle(pool, ws, "fake")
    await ready_handle(pool, ws, "fake2")
    procs = [mp.process for mp in env.registry.live()]
    assert len(procs) == 2
    await pool.shutdown_all()
    assert env.registry.live() == []
    assert all(p.returncode is not None for p in procs)
    assert pool.status("fake", ws).status is LspStatus.STANDBY
    await pool.ensure_started("fake", ws)  # 終了後は起動しない
    assert pool.status("fake", ws).status is LspStatus.STANDBY
    assert env.registry.live() == []


async def test_shutdown_all_during_initialize(env: Env, ws: Workspace):
    pool = env.pool("--init-delay", "20")
    await pool.ensure_started("fake", ws)
    await wait_until(lambda: len(env.registry.live()) == 1)
    started = time.monotonic()
    await pool.shutdown_all()
    assert time.monotonic() - started < 10
    assert env.registry.live() == []


async def test_notify_watched_reaches_running_servers(env: Env, ws: Workspace):
    pool = env.pool("--silent", ready_fallback_s=0.2)
    await ready_handle(pool, ws)
    before = time.perf_counter()
    sent_at = await pool.notify_watched(ws, [(A, FileChangeType.CHANGED), (B, 3)])
    assert sent_at >= before
    await wait_until(lambda: any(e["m"] == "workspace/didChangeWatchedFiles" for e in read_log(env.log)))
    params = next(e["params"] for e in read_log(env.log) if e["m"] == "workspace/didChangeWatchedFiles")
    assert params["changes"] == [
        {"uri": to_file_uri(ws, A), "type": 2},
        {"uri": to_file_uri(ws, B), "type": 3},
    ]


async def test_notify_watched_skips_standby(env: Env, ws: Workspace):
    pool = env.pool("--silent")
    await pool.notify_watched(ws, [(A, 2)])  # 何も起動しない
    assert env.registry.live() == []


async def test_sweep_idle_returns_to_standby(env: Env, ws: Workspace):
    pool = env.pool("--silent", ready_fallback_s=0.2)
    h = await ready_handle(pool, ws)
    assert await pool.sweep_idle(now=h.last_used + 10) == []
    assert await pool.sweep_idle(now=h.last_used + 3600) == [h]
    assert h.state is LspState.STANDBY and env.registry.live() == []
    assert await pool.wait_ready("fake", ws, 20_000)  # 次の要求でまた起動する
    assert env.launches == 2


# --- 文書同期（偽サーバー経由） ---------------------------------------------------------------


async def test_document_versions_reach_the_server(env: Env, ws: Workspace):
    pool = env.pool("--silent", ready_fallback_s=0.2)
    h = await ready_handle(pool, ws)
    assert h.documents.did_open(A, "a = 1\n", "fake") == 1
    assert h.documents.did_change(A, "a = 2\n") == 2
    assert h.documents.did_change(A, "a = 3\n") == 3
    assert h.documents.version_of(A) == 3
    await wait_until(lambda: len(read_log(env.log)) >= 4)
    events = [(e["m"], e["version"]) for e in read_log(env.log) if "version" in e]
    assert events == [
        ("textDocument/didOpen", 1),
        ("textDocument/didChange", 2),
        ("textDocument/didChange", 3),
    ]


async def test_lru_eviction_sends_did_close(env: Env, ws: Workspace):
    pool = env.pool("--silent", ready_fallback_s=0.2, open_docs_max=2)
    h = await ready_handle(pool, ws)
    h.documents.did_open(A, "a", "fake")
    h.documents.did_open(B, "b", "fake")
    h.documents.touch(A)  # B が最も古くなる
    h.documents.did_open(C, "c", "fake")
    assert h.documents.open_paths() == [A, C]
    await wait_until(lambda: any(e["m"] == "textDocument/didClose" for e in read_log(env.log)))
    closes = [
        e["params"]["textDocument"]["uri"] for e in read_log(env.log) if e["m"] == "textDocument/didClose"
    ]
    assert closes == [to_file_uri(ws, B)]


async def test_diagnostics_wait_with_version(env: Env, ws: Workspace):
    seen: list[tuple[RelPath, list[dict[str, Any]], int | None, str, Workspace]] = []
    pool = env.pool(on_diagnostics=lambda *a: seen.append(a))
    await pool.ensure_started("fake", ws)
    await wait_until(lambda: pool.handles(ws) and pool.handles(ws)[0].state is LspState.INDEXING)
    h = pool.handles(ws)[0]
    v = h.documents.did_open(A, "a = 1\n", "fake")
    assert await h.diagnostics.wait_for(
        A, min_version=v, sent_at=h.documents.sent_at_of(A) or 0.0, timeout_ms=10_000
    )
    v = h.documents.did_change(A, "a = 2\n")
    assert await h.diagnostics.wait_for(
        A, min_version=v, sent_at=h.documents.sent_at_of(A) or 0.0, timeout_ms=10_000
    )
    entry = h.diagnostics.get(A)
    assert entry is not None and entry.version == v and entry.diagnostics[0]["message"] == f"v{v}"
    assert [(s[0], s[2], s[3]) for s in seen][-1] == (A, v, "fakels")
    assert all(s[4] is ws for s in seen)  # 5 引数目: そのハンドルのワークスペース


async def test_diagnostics_wait_without_version_uses_sent_at(env: Env, ws: Workspace):
    pool = env.pool("--no-version")
    await pool.ensure_started("fake", ws)
    await wait_until(lambda: pool.handles(ws) and pool.handles(ws)[0].state is LspState.INDEXING)
    h = pool.handles(ws)[0]
    v = h.documents.did_open(A, "a = 1\n", "fake")
    assert await h.diagnostics.wait_for(
        A, min_version=v, sent_at=h.documents.sent_at_of(A) or 0.0, timeout_ms=10_000
    )
    entry = h.diagnostics.get(A)
    assert entry is not None and entry.version is None
    v = h.documents.did_change(A, "a = 2\n")
    sent_at = h.documents.sent_at_of(A) or 0.0
    assert await h.diagnostics.wait_for(A, min_version=v, sent_at=sent_at, timeout_ms=10_000)
    entry = h.diagnostics.get(A)
    assert entry is not None and entry.received_at >= sent_at and entry.diagnostics[0]["message"] == "v2"


async def test_restart_resets_documents_and_wakes_waiters(env: Env, ws: Workspace):
    pool = env.pool("--progress", "0.05", "--crash-first", "1")  # ready は 2 回目の起動の progress end だけ（BUG-0022）
    h = await ready_handle(pool, ws)
    assert h.restarts == 1 and h.documents.open_paths() == []  # 再起動で台帳は空


# --- DocumentSync（単体） --------------------------------------------------------------------


def make_sync(ws: Workspace, max_open: int = 3):
    sent: list[tuple[str, Any]] = []
    sync = DocumentSync(ws, lambda m, p: sent.append((m, p)), max_open=max_open)
    return sync, sent


def methods(sent: list[tuple[str, Any]]) -> list[str]:
    return [m.removeprefix("textDocument/") for m, _ in sent]


def test_documents_open_change_close(ws: Workspace):
    sync, sent = make_sync(ws)
    assert sync.version_of(A) is None
    assert sync.did_open(A, "x", "python") == 1
    assert sync.did_open(A, "ignored", "python") == 1  # 開いていれば何も送らない
    assert sync.did_change(A, "y") == 2
    assert sync.version_of(A) == 2 and sync.sent_at_of(A) is not None
    assert methods(sent) == ["didOpen", "didChange"]
    assert sent[0][1]["textDocument"] == {
        "uri": to_file_uri(ws, A),
        "languageId": "python",
        "version": 1,
        "text": "x",
    }
    assert sent[1][1] == {
        "textDocument": {"uri": to_file_uri(ws, A), "version": 2},
        "contentChanges": [{"text": "y"}],
    }
    assert sync.did_close(A) and not sync.did_close(A)
    assert sync.version_of(A) is None and methods(sent)[-1] == "didClose"
    with pytest.raises(ValueError):
        sync.did_change(A, "z")
    assert sync.did_open(A, "x", "python") == 1  # 閉じて開き直すと版 1


def test_documents_sync_and_external_change_skip_identical_text(ws: Workspace):
    sync, sent = make_sync(ws)
    assert sync.sync(A, "x", "python") == 1
    assert sync.sync(A, "x", "python") == 1
    assert sync.sync(A, "y", "python") == 2
    assert methods(sent) == ["didOpen", "didChange"]
    assert sync.on_external_change(A, "y") is None  # 変わっていない
    assert sync.on_external_change(A, "z") == 3
    assert sync.on_external_change(B, "z") is None  # 開いていない
    assert methods(sent) == ["didOpen", "didChange", "didChange"]


def test_documents_lru_closes_oldest_and_never_the_one_being_opened(ws: Workspace):
    sync, sent = make_sync(ws, max_open=2)
    closed: list[RelPath] = []
    sync.on_close = closed.append
    sync.did_open(A, "a", "p")
    sync.did_open(B, "b", "p")
    sync.did_change(A, "a2")  # A が新しい側へ
    sync.did_open(C, "c", "p")
    assert sync.open_paths() == [A, C] and closed == [B]
    assert methods(sent)[-1] == "didClose" and sent[-1][1]["textDocument"]["uri"] == to_file_uri(ws, B)
    assert len(sync) == 2 and A in sync and B not in sync


def test_documents_default_limit_is_200(ws: Workspace):
    sync, sent = make_sync(ws, max_open=200)
    for i in range(205):
        sync.did_open(RelPath(f"f{i}.py"), "", "python")
    assert len(sync) == 200
    assert methods(sent).count("didClose") == 5
    assert sync.version_of(RelPath("f0.py")) is None and sync.version_of(RelPath("f204.py")) == 1


def test_documents_send_failure_keeps_state(ws: Workspace):
    def boom(method: str, params: Any) -> None:
        raise ConnectionError("down")

    sync = DocumentSync(ws, boom)
    with pytest.raises(ConnectionError):
        sync.did_open(A, "x", "python")
    assert sync.version_of(A) is None


# --- DiagnosticsHub（単体。偽の publishDiagnostics を直接入れる） --------------------------------------


def publish(hub: DiagnosticsHub, ws: Workspace, path: RelPath, version: int | None, n: int = 1) -> None:
    params: dict[str, Any] = {
        "uri": to_file_uri(ws, path),
        "diagnostics": [{"message": f"d{i}"} for i in range(n)],
    }
    if version is not None:
        params["version"] = version
    hub.on_publish(params)


async def test_hub_waits_for_min_version(ws: Workspace):
    hub = DiagnosticsHub(ws)
    publish(hub, ws, A, 1, n=1)
    sent_at = time.perf_counter()
    task = asyncio.create_task(hub.wait_for(A, min_version=3, sent_at=sent_at, timeout_ms=5000))
    await asyncio.sleep(0.05)
    publish(hub, ws, A, 2)
    await asyncio.sleep(0.05)
    assert not task.done()  # 版 2 では足りない
    publish(hub, ws, A, 4)  # basedpyright はバーストを最後の版に畳む: >= で足りる
    assert await task is True
    entry = hub.get(A)
    assert entry is not None and entry.version == 4 and entry.seq == 3


async def test_hub_ignores_stale_version_even_if_received_after_sent_at(ws: Workspace):
    hub = DiagnosticsHub(ws)
    sent_at = time.perf_counter()
    publish(hub, ws, A, 1)  # 依存元の旧い版の再送
    assert await hub.wait_for(A, min_version=2, sent_at=sent_at, timeout_ms=150) is False


async def test_hub_without_version_waits_for_publish_after_sent_at(ws: Workspace):
    hub = DiagnosticsHub(ws)
    publish(hub, ws, A, None)  # 送信より前に届いた古い診断
    await asyncio.sleep(0.01)
    sent_at = time.perf_counter()
    assert await hub.wait_for(A, min_version=5, sent_at=sent_at, timeout_ms=100) is False
    task = asyncio.create_task(hub.wait_for(A, min_version=5, sent_at=sent_at, timeout_ms=5000))
    await asyncio.sleep(0.05)
    assert not task.done()
    publish(hub, ws, A, None)
    assert await task is True


async def test_hub_min_publishes_for_versionless_servers(ws: Workspace):
    hub = DiagnosticsHub(ws)
    sent_at = time.perf_counter()
    publish(hub, ws, A, None)
    publish(hub, ws, A, None)
    assert await hub.wait_for(A, min_version=None, sent_at=sent_at, timeout_ms=100, min_publishes=1) is True
    assert await hub.wait_for(A, min_version=None, sent_at=sent_at, timeout_ms=100, min_publishes=3) is False
    publish(hub, ws, A, None)
    assert await hub.wait_for(A, min_version=None, sent_at=sent_at, timeout_ms=100, min_publishes=3) is True


async def test_hub_timeout_returns_false_promptly(ws: Workspace):
    hub = DiagnosticsHub(ws)
    started = time.monotonic()
    assert await hub.wait_for(A, min_version=1, sent_at=time.perf_counter(), timeout_ms=200) is False
    assert 0.15 <= time.monotonic() - started < 2.0


async def test_hub_empty_first_publish_is_provisional(ws: Workspace):
    hub = DiagnosticsHub(ws)
    publish(hub, ws, A, 1, n=0)  # 複数文書を続けて開いた直後の空の診断
    assert hub.is_provisional(A)
    started = time.monotonic()
    assert await hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=5000) is True
    assert time.monotonic() - started >= PROVISIONAL_QUIET_MS / 1000 - 0.06  # 静止まで「揃った」としない
    assert not hub.is_provisional(A)

    publish(hub, ws, B, 1, n=0)
    hub.settle_all()  # $/progress end
    assert not hub.is_provisional(B)
    started = time.monotonic()
    assert await hub.wait_for(B, min_version=1, sent_at=0.0, timeout_ms=5000) is True
    assert time.monotonic() - started < 0.2

    publish(hub, ws, C, 1, n=2)  # 本物が最初から来た
    assert not hub.is_provisional(C)
    assert await hub.wait_for(C, min_version=1, sent_at=0.0, timeout_ms=50) is True


async def test_hub_provisional_is_replaced_by_the_real_diagnostics(ws: Workspace):
    hub = DiagnosticsHub(ws)
    publish(hub, ws, A, 1, n=0)
    task = asyncio.create_task(hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=5000))
    await asyncio.sleep(0.05)
    publish(hub, ws, A, 1, n=2)
    assert await task is True
    entry = hub.get(A)
    assert entry is not None and len(entry.diagnostics) == 2


async def test_hub_settle_ms_waits_for_quiet(ws: Workspace):
    hub = DiagnosticsHub(ws)
    publish(hub, ws, A, 2)
    task = asyncio.create_task(hub.wait_for(A, min_version=2, sent_at=0.0, timeout_ms=5000, settle_ms=200))
    await asyncio.sleep(0.1)
    publish(hub, ws, A, 3)  # 静止が破られる
    started = time.monotonic()
    assert await task is True
    assert time.monotonic() - started >= 0.15


async def test_hub_reset_wakes_waiters(ws: Workspace):
    hub = DiagnosticsHub(ws)
    task = asyncio.create_task(hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=30_000))
    await asyncio.sleep(0.05)
    hub.reset()
    started = time.monotonic()
    assert await asyncio.wait_for(task, 2) is False
    assert time.monotonic() - started < 1
    publish(hub, ws, A, 1, n=1)  # reset のあとも使える
    assert await hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=1000) is True


async def test_hub_callback_and_foreign_uris(ws: Workspace, tmp_path: Path):
    calls: list[tuple[RelPath, list[dict[str, Any]], int | None]] = []
    awaited: list[RelPath] = []

    async def acb(path: RelPath, diags: list[dict[str, Any]], version: int | None) -> None:
        awaited.append(path)

    hub = DiagnosticsHub(ws, on_diagnostics=lambda p, d, v: calls.append((p, d, v)))
    publish(hub, ws, A, 7, n=2)
    assert calls == [(A, [{"message": "d0"}, {"message": "d1"}], 7)]
    outside = Path(os.path.realpath(tmp_path)) / "elsewhere" / "x.fake"
    hub.on_publish({"uri": outside.as_uri(), "version": 1, "diagnostics": []})
    hub.on_publish({"uri": "untitled:Untitled-1", "diagnostics": []})
    hub.on_publish("garbage")
    assert len(calls) == 1 and hub.paths() == [A] and hub.publish_count == 3

    hub2 = DiagnosticsHub(ws, on_diagnostics=acb)
    publish(hub2, ws, B, None)
    await asyncio.sleep(0.05)
    assert awaited == [B]

    def boom(*_: Any) -> None:
        raise RuntimeError("callback failed")

    hub3 = DiagnosticsHub(ws, on_diagnostics=boom)
    publish(hub3, ws, A, 1)  # 例外は握りつぶしてログへ。保持は残る
    assert hub3.get(A) is not None


# --- 待ち手は DB への書き込みが済んでから起こす（BUG-0007。設計書 §7.7.6-4、EP-7） -------------------


async def spin(n: int = 10) -> None:
    """イベントループを n 回回す（固定の sleep ではなく、待ち手が進めるだけ進んだ状態を作る）。"""
    for _ in range(n):
        await asyncio.sleep(0)


async def test_hub_wakes_waiters_only_after_the_async_write_finished(ws: Workspace):
    gate = asyncio.Event()
    written: list[tuple[RelPath, int | None]] = []

    async def write(path: RelPath, diags: list[dict[str, Any]], version: int | None) -> None:
        await gate.wait()
        written.append((path, version))

    hub = DiagnosticsHub(ws, on_diagnostics=write)
    early = asyncio.create_task(hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=5000))
    await spin()
    publish(hub, ws, A, 1, n=2)
    # 受信のあとに来た待ち手も、書き込みが済むまで返らない
    late = asyncio.create_task(hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=5000))
    await spin()
    assert not early.done() and not late.done()
    assert written == []
    entry = hub.get(A)
    assert entry is not None and len(entry.diagnostics) == 2  # 受信した診断そのものはすぐ見える
    gate.set()
    assert await asyncio.wait_for(asyncio.gather(early, late), 5) == [True, True]
    assert written == [(A, 1)]


async def test_hub_waits_for_every_overlapping_write(ws: Workspace):
    gates = {1: asyncio.Event(), 2: asyncio.Event()}

    def write(path: RelPath, diags: list[dict[str, Any]], version: int | None) -> Any:
        assert version is not None
        return gates[version].wait()

    hub = DiagnosticsHub(ws, on_diagnostics=write)
    publish(hub, ws, A, 1)
    publish(hub, ws, A, 2)
    task = asyncio.create_task(hub.wait_for(A, min_version=2, sent_at=0.0, timeout_ms=5000))
    gates[2].set()  # 新しい方の書き込みだけが先に終わった
    await spin()
    assert not task.done()
    gates[1].set()
    assert await asyncio.wait_for(task, 5) is True


async def test_hub_waits_for_the_index_writer_job_returned_as_a_future(ws: Workspace, tmp_path: Path):
    """``IndexWriter.submit``（``concurrent.futures.Future``）を返す呼び手でも、コミットのあとに起こす。"""
    writer = IndexWriter(tmp_path / "cache" / "paad.sqlite")
    gate = threading.Event()

    def job(conn: sqlite3.Connection) -> None:
        assert gate.wait(10)
        conn.execute("UPDATE _meta SET edit_seq = edit_seq + 1")

    def on_diagnostics(path: RelPath, diags: list[dict[str, Any]], version: int | None) -> Any:
        return writer.submit(job, bumps_version=False, label="diag")

    def edit_seq() -> int:
        conn = sqlite3.connect(writer.path)
        try:
            return int(conn.execute("SELECT edit_seq FROM _meta").fetchone()[0])
        finally:
            conn.close()

    try:
        hub = DiagnosticsHub(ws, on_diagnostics=on_diagnostics)
        publish(hub, ws, A, 1)
        task = asyncio.create_task(hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=10_000))
        await spin()
        assert not task.done()
        assert edit_seq() == 0
        gate.set()
        assert await asyncio.wait_for(task, 10) is True
        assert edit_seq() == 1  # 起こされた時点で、書き込みはコミット済み
    finally:
        gate.set()
        await asyncio.to_thread(writer.close)


async def test_hub_settle_ms_also_waits_for_the_write(ws: Workspace):
    gate = asyncio.Event()
    now = [0.0]

    def write(path: RelPath, diags: list[dict[str, Any]], version: int | None) -> Any:
        return gate.wait() if version == 2 else None

    hub = DiagnosticsHub(ws, on_diagnostics=write, clock=lambda: now[0])
    publish(hub, ws, A, 1)
    task = asyncio.create_task(hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=5000, settle_ms=50))
    await spin()
    publish(hub, ws, A, 2)  # 静止の最中に届いた診断（書き込みは未了）
    now[0] = 10.0  # 静止時間はもう十分に過ぎている
    await spin()
    assert not task.done()  # 静止だけでは足りない: 書き込みを待つ
    gate.set()
    assert await asyncio.wait_for(task, 5) is True


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_hub_does_not_report_fresh_when_the_write_failed(ws: Workspace, kind: str):
    attempts = 0

    def write(path: RelPath, diags: list[dict[str, Any]], version: int | None) -> Any:
        nonlocal attempts
        attempts += 1
        failing = attempts == 1
        if kind == "sync":
            if failing:
                raise RuntimeError("disk full")
            return None

        async def run() -> None:
            if failing:
                raise RuntimeError("disk full")

        return run()

    hub = DiagnosticsHub(ws, on_diagnostics=write)
    publish(hub, ws, A, 1)
    # DB に載らなかった診断を「揃った」とは言わない（pending になる）
    assert await hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=150) is False
    entry = hub.get(A)
    assert entry is not None and entry.version == 1  # 受信そのものは残る
    publish(hub, ws, A, 2)  # 次の受信は書き込めた
    assert await hub.wait_for(A, min_version=2, sent_at=0.0, timeout_ms=2000) is True
    assert attempts == 2


async def test_tsserver_less_workspace_skips_tsls_for_vtsls(env: Env, ws: Workspace):
    """tsls は ``tsserver.js`` が無いと ``initialize`` に失敗する（S-5）: 無いワークスペースでは代替の vtsls を起動する。"""
    tsls = LspServerDef(
        language_id="fake",
        name="tsls",
        argv_candidates=(("tsls",),),
        tsserver_via="initialization_options",
        needs_workspace_tsserver=True,
    )
    vtsls = LspServerDef(
        language_id="fake", name="vtsls", argv_candidates=(("vtsls",),), tsserver_via="settings"
    )
    tried: list[str] = []
    argv = [sys.executable, str(env.script), "--silent"]

    def resolver(defn: LspServerDef, w: Workspace) -> list[str] | None:
        tried.append(defn.name)
        return list(argv)

    pool = env.pool("--silent", servers=(tsls, vtsls), resolver=resolver, ready_fallback_s=0.2)
    await ready_handle(pool, ws)
    assert pool.status("fake", ws).server_name == "vtsls" and tried == ["vtsls"]

    ts_lib = ws.root / "node_modules" / "typescript" / "lib"
    ts_lib.mkdir(parents=True)
    (ts_lib / "tsserver.js").write_text("", encoding="utf-8")
    await pool.reset("fake", ws)
    tried.clear()
    await ready_handle(pool, ws)
    assert pool.status("fake", ws).server_name == "tsls" and tried == ["tsls"]
