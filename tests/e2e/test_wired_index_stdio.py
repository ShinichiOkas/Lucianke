"""結線の実機確認（設計書 §6.5、ADR-16）: 実プロセスの ``lucianke`` を stdio の MCP クライアントでつなぎ、索引が効いていることを確かめる。

F-POLY（``tests/fixtures/poly``）を tmp にコピーし、キャッシュも tmp（``LUCIANKE_CACHE_DIR``）に向ける。

(a) 初回の走査が終わると ``query_project_db`` の ``SELECT count(*) FROM symbols`` が 0 より大きい。
(b) ファイルを書き換えると、監視→再索引で結果が変わる。
(c) プロセスを止めて起動し直すと、走査を待たずに同じ結果が出る（再起動耐性）。
(d) ``stdin`` の EOF で終了コード 0（ADR-16）、終了処理の全段が ``ok``。

待ちは固定 sleep ではなく条件待ち（タイムアウトつき）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

REPO = Path(__file__).resolve().parents[2]
POLY = REPO / "tests" / "fixtures" / "poly"
WAIT_S = 60.0  # 条件待ちの上限（初回は uv の起動と SDK の import を含む）
POLL_S = 0.2
PROBE = "wired_probe_fn_7f3a"


def _env(cache: Path) -> dict[str, str]:
    return {"PYTHONUTF8": "1", "LUCIANKE_CACHE_DIR": str(cache)}


def _argv() -> list[str]:
    return ["run", "--project", str(REPO), "lucianke"]  # cwd は変えない（ワークスペースは cwd で決まる）


def _params(ws: Path, cache: Path) -> StdioServerParameters:
    return StdioServerParameters(command="uv", args=_argv(), env=_env(cache), cwd=str(ws))


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(POLY, root)
    return root


async def _rows(c: Client, sql: str) -> list[dict[str, Any]] | None:
    """読み取り専用 SQL の行。索引がまだ無い（``E_WORKSPACE_NOT_CONFIGURED``）ときは ``None``。"""
    r = await c.call_tool("query_project_db", {"query": sql, "format": "json"})
    if r.is_error:
        return None
    return list(r.structured_content["data"]["rows"])


async def _count(c: Client, sql: str) -> int | None:
    rows = await _rows(c, sql)
    return None if not rows else int(next(iter(rows[0].values())))


async def _until(what: str, probe: Callable[[], Awaitable[Any]], ok: Callable[[Any], bool], timeout: float = WAIT_S) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await probe()
        if ok(last):
            return last
        await asyncio.sleep(POLL_S)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}; last = {last!r}")


async def test_index_is_wired_through_real_stdio(ws: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    sym_count = "SELECT count(*) AS n FROM symbols"
    probe_count = f"SELECT count(*) AS n FROM symbols WHERE name = '{PROBE}'"

    async with Client(_params(ws, cache), mode="legacy", cache=None) as c:
        # (a) 初回の走査が終わると symbols が増える
        first = await _until("the first scan", lambda: _count(c, sym_count), lambda n: n is not None and n > 0)
        files = await _count(c, "SELECT count(*) AS n FROM files")
        assert files and files > 5
        assert await _count(c, probe_count) == 0

        # (b) 外部からの書き換え → 監視 → 再索引
        target = ws / "src" / "shop" / "models" / "receipt.py"
        with target.open("a", encoding="utf-8", newline="") as f:
            f.write(f"\n\ndef {PROBE}() -> int:\n    return 1\n")
        await _until("the watcher to reindex the edited file", lambda: _count(c, probe_count), lambda n: n == 1)
        after_edit = await _count(c, sym_count)
        assert after_edit is not None and after_edit > first

        # 初回の走査と再索引が落ち着くまで待つ（同じ値が続く）
        await _until("the symbol count to settle", lambda: _count(c, sym_count), lambda n: n == after_edit)

    # (c) 止めて起動し直す → 走査を待たずに同じ結果（永続状態はディスク。ADR-16）
    async with Client(_params(ws, cache), mode="legacy", cache=None) as c:
        again = await _until("the persisted index to be readable", lambda: _count(c, probe_count), lambda n: n is not None)
        assert again == 1
        assert await _count(c, sym_count) == after_edit
        # 2 回目の起動の差分索引は何も変えない（変更の無いワークスペース。§7.6.5）
        await asyncio.sleep(1.0)
        assert await _count(c, sym_count) == after_edit


async def test_stdin_eof_exits_zero_after_orderly_shutdown(ws: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    env = {**os.environ, **_env(cache)}
    proc = await asyncio.create_subprocess_exec(
        "uv", *_argv(), cwd=str(ws), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    assert proc.stdin and proc.stdout and proc.stderr
    stderr_lines: list[str] = []

    async def pump_stderr() -> None:
        async for line in proc.stderr:  # type: ignore[union-attr]
            stderr_lines.append(line.decode("utf-8", "replace"))

    pump = asyncio.create_task(pump_stderr())
    try:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}}}  # fmt: skip
        proc.stdin.write((json.dumps(init) + "\n").encode())
        await proc.stdin.drain()
        reply = json.loads(await asyncio.wait_for(proc.stdout.readline(), WAIT_S))
        assert reply["id"] == 1 and "result" in reply
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
        await proc.stdin.drain()

        async def started() -> bool:
            return any("scan job" in ln for ln in stderr_lines)

        await _until("the index to start", started, bool)
        proc.stdin.close()  # EOF
        code = await asyncio.wait_for(proc.wait(), 20)
        await asyncio.wait_for(pump, 5)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        pump.cancel()
    log = "".join(stderr_lines)
    assert code == 0, log
    assert "shutdown done" in log and "steps ok" in log and "failed:" not in log, log
    assert "Traceback" not in log, log
