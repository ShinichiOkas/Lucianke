"""roots を渡せないクライアントでも、最初の呼び出しが待たされない（BUG-0042）。実プロセスの ``lucianke`` を stdio の MCP クライアントでつなぐ。

SDK の ``Client`` の既定（``mode="auto"``）は 2026-07-28 版を話す: ``initialize`` も ``notifications/initialized`` も無く、サーバー起点の
``roots/list`` もできない。以前は roots の決定が来ないまま退避の 8 秒を待ち、最初の ``set_roots`` / ``status`` が 8〜10 秒ブロックされ、
頼んでいない cwd（作業ツリー全体）の索引が始まった。

* 2026-07-28 版: 最初の ``set_roots`` は待たず、cwd の索引を始めない。最初の ``status`` は待たずに cwd を既定にする。
* ``set_roots`` で決めた根は、退避の期限が過ぎても cwd に戻らない（短くした期限〈``LUCIANKE_ROOTS_FALLBACK_S``〉で確かめる）。
* ``initialize`` の版で roots 能力を宣言しないクライアント: 従来どおり cwd が既定。
* ``initialize`` の版で roots 能力を宣言するクライアント: 従来どおり ``roots/list`` の結果が根になる（待ち、cwd を先に決めない）。

待ちは条件待ち（タイムアウトつき）。「待たされない」の上限は、退避の期限（8 s）の半分で、通常は 1 秒に満たない
（共有の作業ツリーで CPU が飽和していても、8 秒の退避には届かない）。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters, types
from mcp.shared.exceptions import MCPDeprecationWarning

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))  # 共通の起動コマンド _server_cmd

from _server_cmd import server_argv  # noqa: E402

POLY = REPO / "tests" / "fixtures" / "poly"
WAIT_S = 90.0  # 条件待ちの上限（共有の作業ツリーで CPU が飽和していても足りる長さ。通常は数秒）
POLL_S = 0.2
NOT_BLOCKED_S = 4.0  # 退避の既定（8 s）の半分。ブロックされていれば 8 s 以上かかる
SHORT_FALLBACK_S = 2.0  # 退避の期限を短くして、「期限が過ぎても set_roots の根が残る」を短い時間で確かめる


def _params(ws: Path, cache: Path, **env: str) -> StdioServerParameters:
    argv = server_argv()
    return StdioServerParameters(
        command=argv[0],
        args=argv[1:],
        env={"PYTHONUTF8": "1", "LUCIANKE_CACHE_DIR": str(cache), **env},
        cwd=str(ws),
    )


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(POLY, root)
    return root


@pytest.fixture
def other(tmp_path: Path) -> Path:
    root = tmp_path / "other"
    shutil.copytree(POLY, root)
    return root


async def _until(what: str, probe: Callable[[], Awaitable[Any]], ok: Callable[[Any], bool], timeout: float = WAIT_S) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await probe()
        if ok(last):
            return last
        await asyncio.sleep(POLL_S)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}; last = {last!r}")


async def _timed(c: Client, args: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    t0 = time.monotonic()
    r = await c.call_tool("configure_workspace", {"format": "json", **args})
    elapsed = time.monotonic() - t0
    assert not r.is_error, r.structured_content
    return elapsed, dict(r.structured_content["data"])


async def _status(c: Client) -> dict[str, Any]:
    return (await _timed(c, {"action": "status"}))[1]


def _roots_of(status: dict[str, Any]) -> list[Path]:
    return [Path(w["root_path"]).resolve() for w in status["workspaces"]]


async def _scans_done(c: Client) -> bool:
    jobs = (await _status(c))["jobs"]
    return bool(jobs) and all(j["state"] == "done" for j in jobs)


async def test_set_roots_as_the_first_call_is_not_blocked_and_the_cwd_is_not_indexed(ws: Path, other: Path, tmp_path: Path) -> None:
    async with Client(_params(ws, tmp_path / "cache"), cache=None) as c:  # mode="auto": 2026-07-28 版（roots/list も initialized も無い）
        elapsed, data = await _timed(c, {"action": "set_roots", "root_paths": [str(other)]})
        assert elapsed < NOT_BLOCKED_S, f"set_roots was blocked for {elapsed:.1f} s"
        assert _roots_of(data) == [other.resolve()]  # 頼んでいない cwd は登録されない

        status = await _status(c)
        assert _roots_of(status) == [other.resolve()]
        await _until("the scan of the requested root", lambda: _scans_done(c), bool)
        assert _roots_of(await _status(c)) == [other.resolve()]


async def test_status_as_the_first_call_decides_the_cwd_at_once_and_set_roots_replaces_it(ws: Path, other: Path, tmp_path: Path) -> None:
    async with Client(_params(ws, tmp_path / "cache"), cache=None) as c:
        elapsed, data = await _timed(c, {"action": "status"})
        assert elapsed < NOT_BLOCKED_S, f"status was blocked for {elapsed:.1f} s"
        assert _roots_of(data) == [ws.resolve()]  # roots を渡せない相手の既定は cwd

        elapsed, data = await _timed(c, {"action": "set_roots", "root_paths": [str(other)]})
        assert elapsed < NOT_BLOCKED_S
        assert _roots_of(data) == [other.resolve()]
        await _until("the scan of the requested root", lambda: _scans_done(c), bool)


async def test_a_root_set_by_set_roots_survives_the_fallback_deadline(ws: Path, other: Path, tmp_path: Path) -> None:
    """退避（initialized が来ないホスト向け）は、すでに決まった根を cwd で上書きしない。"""
    params = _params(ws, tmp_path / "cache", LUCIANKE_ROOTS_FALLBACK_S=str(SHORT_FALLBACK_S))
    started = time.monotonic()
    async with Client(params, cache=None) as c:
        _, data = await _timed(c, {"action": "set_roots", "root_paths": [str(other)]})
        assert _roots_of(data) == [other.resolve()]
        # 期限（サーバー起動から SHORT_FALLBACK_S）を十分に過ぎるまで、状態が cwd に戻らないことを見続ける
        while time.monotonic() - started < SHORT_FALLBACK_S + 3.0:
            assert _roots_of(await _status(c)) == [other.resolve()]
            await asyncio.sleep(0.3)
        assert _roots_of(await _status(c)) == [other.resolve()]


async def test_a_handshake_client_without_roots_still_gets_the_cwd_at_once(ws: Path, tmp_path: Path) -> None:
    async with Client(_params(ws, tmp_path / "cache"), mode="legacy", cache=None) as c:
        elapsed, data = await _timed(c, {"action": "status"})
        assert elapsed < NOT_BLOCKED_S, f"status was blocked for {elapsed:.1f} s"
        assert _roots_of(data) == [ws.resolve()]


async def test_a_handshake_client_that_declares_roots_gets_them_as_before(ws: Path, other: Path, tmp_path: Path) -> None:
    asked: list[int] = []

    async def list_roots(context: Any) -> types.ListRootsResult:
        asked.append(1)
        return types.ListRootsResult(roots=[types.Root(uri=other.as_uri())])

    async with Client(
        _params(ws, tmp_path / "cache"), mode="legacy", list_roots_callback=list_roots, cache=None
    ) as c:
        # 最初の status は roots/list の結果が根になるのを待つ。cwd を先に決めてはならない
        elapsed, data = await _timed(c, {"action": "status"})
        assert elapsed < NOT_BLOCKED_S
        assert asked, "the server never asked for roots/list"
        assert _roots_of(data) == [other.resolve()]
        await _until("the scan of the client's root", lambda: _scans_done(c), bool)
        assert _roots_of(await _status(c)) == [other.resolve()]
