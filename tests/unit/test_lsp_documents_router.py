"""lsp/documents.py の ``DocumentRouter``: 1 言語に複数の LSP サーバー（Python の basedpyright と ruff）があるときの振り分け（BUG-0023）。

大半は、本物の ``DocumentSync`` / ``DiagnosticsHub`` を持つ偽のハンドル（プロセスなし）で、サーバーごとの版・診断の受信を
手で動かして確かめる。最後に ``LspPool.router`` で本物のプロセス（偽の LSP）を 2 本立てた結線を 1 本だけ通す。
待ちは条件で行い（固定 sleep は使わない）、「届かない」ことの確認だけ短いタイムアウトを使う。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lucianke.core.model import RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.diagnostics import DiagnosticsHub
from lucianke.lsp.documents import DocumentRouter, DocumentSync
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import LspServerDef
from lucianke.workspace.paths import to_file_uri
from lucianke.workspace.types import Workspace

A = RelPath("a.fake")
B = RelPath("b.fake")
IS_WINDOWS = os.name == "nt"


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = Path(os.path.realpath(tmp_path / "proj"))
    root.mkdir()
    return Workspace(workspace_id="w", root=root, case_insensitive=IS_WINDOWS)


class StubHandle:
    """``RoutedHandle`` の形をした、プロセスを持たないハンドル。"""

    def __init__(
        self, ws: Workspace, role: str, *, language_id: str = "fake", lsp_language_id: str | None = None
    ) -> None:
        self.ws = ws
        self.role = role
        self.language_id = language_id
        self.ready = True
        self.down = False
        self.sent: list[tuple[str, Any]] = []
        self.defn = SimpleNamespace(document_language_id=lsp_language_id) if lsp_language_id else None
        self.documents = DocumentSync(ws, self._notify)
        self.diagnostics = DiagnosticsHub(ws, source=role)
        self.documents.on_close = self.diagnostics.forget

    @property
    def is_ready(self) -> bool:
        return self.ready

    def _notify(self, method: str, params: Any) -> None:
        if self.down:
            raise ConnectionError("down")
        self.sent.append((method, params))

    def methods(self) -> list[str]:
        return [m for m, _ in self.sent]

    def publish(self, path: RelPath, version: int | None) -> None:
        """このサーバーが ``path`` の診断を（空でない内容で）返したことにする。"""
        self.diagnostics.on_publish(
            {
                "uri": to_file_uri(self.ws, path),
                "version": version,
                "diagnostics": [
                    {
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                        "message": f"{self.role} v{version}",
                        "severity": 1,
                    }
                ],
            }
        )


class StubPool:
    def __init__(self, *handles: StubHandle) -> None:
        self._handles = list(handles)

    def handles(self, ws: Workspace | None = None, language_id: str | None = None) -> Sequence[StubHandle]:
        return [
            h
            for h in self._handles
            if (ws is None or h.ws.workspace_id == ws.workspace_id)
            and (language_id is None or h.language_id == language_id)
        ]


def make(ws: Workspace, *, lint_first: bool = False) -> tuple[DocumentRouter, StubHandle, StubHandle]:
    types, lint = StubHandle(ws, "types", lsp_language_id="types-lang"), StubHandle(ws, "lint")
    pool = StubPool(lint, types) if lint_first else StubPool(types, lint)
    return DocumentRouter(pool, ws), types, lint


async def spin(n: int = 20) -> None:
    """待ち手が走る機会を与える（時間を使わない）。"""
    for _ in range(n):
        await asyncio.sleep(0)


# --- sync -----------------------------------------------------------------------------------


def test_sync_reaches_every_ready_server_of_the_language(ws: Workspace):
    router, types, lint = make(ws)
    assert router.sync(A, "x = 1\n", "fake") == 1
    assert types.methods() == ["textDocument/didOpen"] and lint.methods() == ["textDocument/didOpen"]
    assert router.sync(A, "x = 2\n", "fake") == 2
    assert types.methods()[-1] == "textDocument/didChange" and lint.methods()[-1] == "textDocument/didChange"
    assert router.sync(A, "x = 2\n", "fake") == 2  # 同じ内容なら何も送らない
    assert len(types.sent) == 2 and len(lint.sent) == 2
    assert router.is_open(A) and not router.is_open(B)


def test_sync_sends_each_servers_own_language_id(ws: Workspace):
    router, types, lint = make(ws)
    router.sync(A, "x\n", "fake")
    assert types.sent[0][1]["textDocument"]["languageId"] == "types-lang"  # 定義の document_language_id
    assert lint.sent[0][1]["textDocument"]["languageId"] == "fake"  # 定義が無ければ呼び手の言語 ID


def test_sync_returns_the_types_servers_version_whatever_the_order(ws: Workspace):
    router, types, lint = make(ws, lint_first=True)
    lint.documents.did_open(A, "a\n", "x")
    lint.documents.did_change(A, "a2\n")
    lint.documents.did_change(A, "a3\n")  # lint は版 3
    assert router.sync(A, "a4\n", "fake") == 1  # types は初めて開く: 版 1。lint は 4
    assert lint.documents.version_of(A) == 4


def test_sync_skips_servers_that_are_not_ready(ws: Workspace):
    router, types, lint = make(ws)
    lint.ready = False
    router.sync(A, "x\n", "fake")
    assert types.methods() == ["textDocument/didOpen"] and lint.sent == []
    assert [h for h in router.handles_for("fake")] == [types]


def test_sync_keeps_going_when_one_server_is_down(ws: Workspace):
    router, types, lint = make(ws)
    types.down = True
    assert router.sync(A, "x\n", "fake") == 1  # lint の版
    assert lint.methods() == ["textDocument/didOpen"] and not types.documents.is_open(A)


def test_sync_raises_when_no_server_took_the_document(ws: Workspace):
    router, types, lint = make(ws)
    with pytest.raises(ConnectionError):
        router.sync(A, "x\n", "other-language")  # その言語のサーバーが無い
    types.down = lint.down = True
    with pytest.raises(ConnectionError):
        router.sync(A, "x\n", "fake")
    lint.ready = types.ready = False
    with pytest.raises(ConnectionError):
        router.sync(A, "x\n", "fake")


def test_sent_at_of_is_the_earliest_send_and_none_when_closed(ws: Workspace):
    router, types, lint = make(ws)
    assert router.sent_at_of(A) is None
    router.sync(A, "x\n", "fake")
    assert router.sent_at_of(A) == min(
        types.documents.sent_at_of(A) or 0.0, lint.documents.sent_at_of(A) or 0.0
    )
    types.documents.did_close(A)
    assert router.sent_at_of(A) == lint.documents.sent_at_of(A)  # 開いているサーバーだけ
    lint.documents.did_close(A)
    assert router.sent_at_of(A) is None and not router.is_open(A)


# --- wait_for -------------------------------------------------------------------------------


async def test_wait_for_needs_every_server_at_its_own_version(ws: Workspace):
    router, types, lint = make(ws)
    # types は版 3、lint は版 1 まで進んだ状態から始める（サーバーごとに版の数え方がずれる）
    types.documents.did_open(A, "v1\n", "x")
    types.documents.did_change(A, "v2\n")
    types.documents.did_change(A, "v3\n")
    lint.documents.did_open(A, "v1\n", "x")
    version = router.sync(A, "v4\n", "fake")
    assert version == 4 and lint.documents.version_of(A) == 2
    task = asyncio.create_task(
        router.diagnostics_hub.wait_for(
            A, min_version=version, sent_at=router.sent_at_of(A) or 0.0, timeout_ms=30_000
        )
    )
    await spin()
    assert not task.done()
    types.publish(A, 4)
    await spin()
    assert not task.done()  # lint がまだ
    lint.publish(A, 1)  # lint の旧い版は数えない
    await spin()
    assert not task.done()
    lint.publish(A, 2)  # 代表の版（4）に届いていなくても、lint の送った版（2）で足りる
    assert await asyncio.wait_for(task, 10) is True


async def test_wait_for_is_false_when_one_server_stays_silent(ws: Workspace):
    router, types, lint = make(ws)
    version = router.sync(A, "x\n", "fake")
    types.publish(A, version)
    start = time.perf_counter()
    ok = await router.diagnostics_hub.wait_for(
        A, min_version=version, sent_at=router.sent_at_of(A) or 0.0, timeout_ms=150
    )
    assert ok is False
    assert time.perf_counter() - start < 5


async def test_wait_for_waits_on_all_servers_at_once_with_the_same_timeout(ws: Workspace):
    """直列に待つとデッドロックする仕掛け: 両方の ``wait_for`` が走り出すまで、どちらも戻らない。"""
    router, types, lint = make(ws)
    version = router.sync(A, "x\n", "fake")
    started: list[dict[str, Any]] = []
    both = asyncio.Event()

    async def stub(_path: RelPath, **kw: Any) -> bool:
        started.append(kw)
        if len(started) == 2:
            both.set()
        await both.wait()
        return True

    types.diagnostics.wait_for = stub  # type: ignore[method-assign]
    lint.diagnostics.wait_for = stub  # type: ignore[method-assign]
    ok = await asyncio.wait_for(
        router.diagnostics_hub.wait_for(A, min_version=version, sent_at=0.0, timeout_ms=1000, settle_ms=7),
        10,
    )
    assert ok is True
    # 時間はサーバーの数で足し算にならない（同じ timeout_ms を共有する）
    assert [(kw["timeout_ms"], kw["settle_ms"]) for kw in started] == [(1000, 7), (1000, 7)]


async def test_wait_sources_reports_each_server_by_its_name(ws: Workspace):
    """BUG-0098: ``wait_sources`` は ``wait_for`` と同じ待ちを、サーバー名（``diagnostics`` 表の ``source``）ごとの結果で返す。"""
    router, types, lint = make(ws)
    types.server_name, lint.server_name = "basedpyright", "ruff"  # type: ignore[attr-defined]
    version = router.sync(A, "x\n", "fake")
    lint.publish(A, version)
    start = time.perf_counter()
    got = await router.diagnostics_hub.wait_sources(
        A, min_version=version, sent_at=router.sent_at_of(A) or 0.0, timeout_ms=150
    )
    assert got == {"basedpyright": False, "ruff": True}
    assert time.perf_counter() - start < 5
    types.publish(A, version)
    assert await router.diagnostics_hub.wait_sources(A, min_version=version, sent_at=0.0, timeout_ms=150) == {
        "basedpyright": True,
        "ruff": True,
    }
    # 依存元（版なし）は lint を待たない（``wait_for`` と同じ）。開いているサーバーが無ければ空
    assert await router.diagnostics_hub.wait_sources(A, min_version=None, sent_at=0.0, timeout_ms=150) == {
        "basedpyright": True
    }
    assert await router.diagnostics_hub.wait_sources(B, min_version=1, sent_at=0.0, timeout_ms=150) == {}


async def test_wait_for_a_dependent_ignores_the_lint_server(ws: Workspace):
    """依存元（``min_version=None``）は ruff が再発行しない: types だけ待つ。"""
    router, types, lint = make(ws)
    router.sync(A, "x\n", "fake")
    sent_at = router.sent_at_of(A) or 0.0
    task = asyncio.create_task(
        router.diagnostics_hub.wait_for(A, min_version=None, sent_at=sent_at, timeout_ms=30_000)
    )
    await spin()
    assert not task.done()
    types.publish(A, 1)
    assert await asyncio.wait_for(task, 10) is True  # lint は沈黙のまま


async def test_wait_for_a_dependent_waits_on_lint_when_it_is_the_only_one_open(ws: Workspace):
    router, types, lint = make(ws)
    lint.documents.did_open(A, "x\n", "x")
    task = asyncio.create_task(
        router.diagnostics_hub.wait_for(
            A, min_version=None, sent_at=lint.documents.sent_at_of(A) or 0.0, timeout_ms=30_000
        )
    )
    await spin()
    assert not task.done()
    lint.publish(A, 1)
    assert await asyncio.wait_for(task, 10) is True


async def test_wait_for_a_document_nobody_has_open_is_false_at_once(ws: Workspace):
    router, _types, _lint = make(ws)
    start = time.perf_counter()
    assert not await router.diagnostics_hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=30_000)
    assert time.perf_counter() - start < 5


async def test_wait_for_ignores_servers_that_stopped(ws: Workspace):
    router, types, lint = make(ws)
    version = router.sync(A, "x\n", "fake")
    lint.ready = False  # 落ちた（再起動中など）: もう待たない
    types.publish(A, version)
    assert await router.diagnostics_hub.wait_for(
        A, min_version=version, sent_at=router.sent_at_of(A) or 0.0, timeout_ms=10_000
    )


async def test_wait_for_follows_a_reopened_document_back_to_version_1(ws: Workspace):
    """LRU で閉じて開き直したサーバーは版が 1 に戻る: 記録の版（5）ではなく現在の版で待つ。"""
    router, types, lint = make(ws)
    for i in range(5):
        router.sync(A, f"v{i}\n", "fake")
    version = router.sync(A, "v5\n", "fake")
    assert version == 6
    types.documents.did_close(A)
    types.documents.did_open(A, "v5\n", "x")  # 版 1
    task = asyncio.create_task(
        router.diagnostics_hub.wait_for(A, min_version=version, sent_at=0.0, timeout_ms=30_000)
    )
    await spin()
    types.publish(A, 1)
    lint.publish(A, 6)
    assert await asyncio.wait_for(task, 10) is True


async def test_wait_for_a_server_synced_outside_the_router_is_waited_by_time(ws: Workspace):
    router, types, lint = make(ws)
    router.sync(A, "x\n", "fake")
    stranger = StubHandle(ws, "lint2")
    router._pool._handles.append(stranger)  # type: ignore[attr-defined]
    stranger.documents.did_open(A, "x\n", "x")  # ルーターを通さずに開いた
    types.publish(A, 1)
    lint.publish(A, 1)
    task = asyncio.create_task(
        router.diagnostics_hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=30_000)
    )
    await spin()
    assert not task.done()  # 記録の無いサーバーは版なし（時刻）で待つが、まだ受信していない
    stranger.publish(A, None)
    assert await asyncio.wait_for(task, 10) is True


# --- 結線（本物のプロセス） -------------------------------------------------------------------

FAKE_SERVER = r"""
import json, sys

log_path, silent = sys.argv[1], sys.argv[2] == "silent"
inp, out = sys.stdin.buffer, sys.stdout.buffer


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
    out.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    out.flush()


while True:
    msg = read()
    if msg is None:
        break
    m, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {"textDocumentSync": 1}}})
    elif m == "shutdown":
        send({"jsonrpc": "2.0", "id": rid, "result": None})
    elif m == "exit":
        sys.exit(0)
    elif m in ("textDocument/didOpen", "textDocument/didChange"):
        td = params["textDocument"]
        with open(log_path, "a") as f:
            f.write(json.dumps({"m": m, "version": td.get("version"), "lang": td.get("languageId")}) + "\n")
        if not silent:
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {
                "uri": td["uri"], "version": td.get("version"),
                "diagnostics": [{"range": {"start": {"line": 0, "character": 0},
                                          "end": {"line": 0, "character": 1}}, "message": "m", "severity": 1}]}})
    elif m is not None and rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": None})
"""


async def wait_until(cond: Callable[[], Any], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.02)


def read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(300, close=True)


async def test_pool_router_drives_two_real_servers(tmp_path: Path, ws: Workspace, registry: ProcessRegistry):
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    logs = {"types": tmp_path / "types.log", "lint": tmp_path / "lint.log"}
    defs = (
        LspServerDef(language_id="fake", name="fakets", argv_candidates=(("x",),), role="types"),
        LspServerDef(
            language_id="fake",
            name="fakelint",
            argv_candidates=(("x",),),
            role="lint",
            lsp_language_id="lintlang",
        ),
    )

    def resolver(defn: LspServerDef, _ws: Workspace) -> list[str]:
        return [sys.executable, str(script), str(logs[defn.role]), "plain"]

    pool = LspPool(registry, servers=defs, resolver=resolver, ready_fallback_s=0.2, grace_ms=300)
    try:
        await pool.ensure_started("fake", ws)
        assert await pool.wait_ready("fake", ws, 20_000, role="types")
        assert await pool.wait_ready("fake", ws, 20_000, role="lint")
        router = pool.router(ws)
        assert [h.role for h in router.handles_for("fake")] == ["types", "lint"]
        version = router.sync(A, "x = 1\n", "fake")
        await wait_until(lambda: read_log(logs["types"]) and read_log(logs["lint"]))
        assert (
            read_log(logs["types"])[0]["lang"] == "fake" and read_log(logs["lint"])[0]["lang"] == "lintlang"
        )
        assert await router.diagnostics_hub.wait_for(
            A, min_version=version, sent_at=router.sent_at_of(A) or 0.0, timeout_ms=20_000
        )
        version = router.sync(A, "x = 2\n", "fake")
        assert version == 2
        assert await router.diagnostics_hub.wait_for(
            A, min_version=version, sent_at=router.sent_at_of(A) or 0.0, timeout_ms=20_000
        )
        for role in ("types", "lint"):
            assert [e["m"] for e in read_log(logs[role])] == [
                "textDocument/didOpen",
                "textDocument/didChange",
            ]
            entry = pool.handles(ws, "fake")[0 if role == "types" else 1].diagnostics.get(A)
            assert entry is not None and entry.version == 2
    finally:
        await pool.shutdown_all(300)
