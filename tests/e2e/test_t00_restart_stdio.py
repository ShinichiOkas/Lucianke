"""T-00 の設定が再起動をまたいで戻ることの実機確認（ADR-16、設計書 §7.3.5、仕様書 §11 T-00 事後条件）。

実プロセスの ``lucianke`` を stdio の MCP クライアントでつなぎ、結線済みの ``RuntimeHub`` で確かめる:

(a) ``set_excludes`` で除外した領域が ``files`` から消える（``hub.workspace_changed`` で束が新しい規則になり、再走査が走る）。
(b) プロセスを止めて起動し直すと、最初の ``status`` から同じ ``exclude_globs`` が見える
    （``sync_roots`` が ``restore_settings`` を呼ぶ）。
(c) 起動直後の差分索引も、監視による再索引も、戻した除外を守る（除外した領域が ``files`` に入り直さない）。
    戻していなければ、(c) の走査が除外した領域を入れ直す。

F-POLY（``tests/fixtures/poly``）を tmp にコピーし、キャッシュも tmp（``LUCIANKE_CACHE_DIR``）に向ける。
待ちは固定 sleep ではなく条件待ち（タイムアウトつき）。
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
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))  # 共通の起動コマンド _server_cmd

try:
    from _server_cmd import server_argv  # noqa: E402
except ImportError:  # BUG-0030 の共通の起動コマンドがまだ無い間（同じ形: python -m lucianke）

    def server_argv(*extra: str) -> list[str]:
        return [sys.executable, "-P", "-m", "lucianke", *extra]


POLY = REPO / "tests" / "fixtures" / "poly"
WAIT_S = 90.0  # 条件待ちの上限（共有の作業ツリーで CPU が飽和していても足りる長さ。通常は数秒）
POLL_S = 0.2
EXCLUDES = ["legacy/", "web/"]
EXCLUDED_SQL = "path LIKE 'legacy/%' OR path LIKE 'web/%'"


def _params(ws: Path, cache: Path) -> StdioServerParameters:
    argv = server_argv()
    return StdioServerParameters(
        command=argv[0], args=argv[1:], env={"PYTHONUTF8": "1", "LUCIANKE_CACHE_DIR": str(cache)}, cwd=str(ws)
    )


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
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


async def _status(c: Client) -> dict[str, Any]:
    r = await c.call_tool("configure_workspace", {"action": "status", "format": "json"})
    assert not r.is_error, r.structured_content
    return dict(r.structured_content["data"])


async def _count(c: Client, where: str | None = None) -> int | None:
    sql = "SELECT count(*) AS n FROM files" + (f" WHERE {where}" if where else "")
    r = await c.call_tool("query_project_db", {"query": sql, "format": "json"})
    if r.is_error:
        return None
    rows = r.structured_content["data"]["rows"]
    return int(next(iter(rows[0].values()))) if rows else None


async def _scans_done(c: Client) -> bool:
    jobs = (await _status(c))["jobs"]
    return bool(jobs) and all(j["state"] == "done" for j in jobs)


async def test_excludes_set_by_t00_survive_a_restart(ws: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    async with Client(_params(ws, cache), mode="legacy", cache=None) as c:
        # 初回の走査が終わる。除外する前は legacy/ と web/ も索引にある
        await _until("the first scan", lambda: _scans_done(c), bool)
        total_before = await _count(c)
        excluded_before = await _count(c, EXCLUDED_SQL)
        assert total_before and excluded_before, (total_before, excluded_before)

        # (a) set_excludes: 状態に出て、再走査で files から外れる
        r = await c.call_tool("configure_workspace", {"action": "set_excludes", "exclude_globs": EXCLUDES, "format": "json"})
        assert not r.is_error, r.structured_content
        await _until("the excluded files to leave the index", lambda: _count(c, EXCLUDED_SQL), lambda n: n == 0)
        await _until("the rescan to finish", lambda: _scans_done(c), bool)
        kept = await _count(c)
        assert kept and 0 < kept < total_before
        (entry,) = (await _status(c))["workspaces"]
        assert entry["exclude_globs"] == EXCLUDES
        workspace_id = entry["workspace_id"]

    # 起動し直す（キャッシュと作業ツリーは同じ）
    async with Client(_params(ws, cache), mode="legacy", cache=None) as c:
        # (b) 最初の status から、保存した除外が見える（同じ workspace_id のまま）
        restored = await _until("the restored workspace", lambda: _status(c), lambda d: len(d["workspaces"]) == 1)
        (entry,) = restored["workspaces"]
        assert entry["exclude_globs"] == EXCLUDES, entry
        assert entry["workspace_id"] == workspace_id

        # (c) 起動直後の差分索引は除外を守る。戻していなければ、ここで legacy/ と web/ が入り直す
        await _until("the startup scan", lambda: _scans_done(c), bool)
        assert await _count(c, EXCLUDED_SQL) == 0
        assert await _count(c) == kept

        # 監視も新しい規則で落とす: 除外した領域に作ったファイルは索引に入らず、同じ時期に作った通常のファイルは入る
        (ws / "legacy" / "hidden_probe.py").write_text("def hidden_probe_fn_91c2():\n    return 1\n", encoding="utf-8")
        visible = ws / "src" / "shop" / "models" / "visible_probe.py"
        visible.write_text("def visible_probe_fn_91c2():\n    return 1\n", encoding="utf-8")
        await _until(
            "the watcher to index the visible file",
            lambda: _count(c, "path = 'src/shop/models/visible_probe.py'"),
            lambda n: n == 1,
        )
        assert await _count(c, EXCLUDED_SQL) == 0

        # 設定を空に戻すと、保存も空に戻る（もう一度起動し直しても除外が残らない）
        r = await c.call_tool("configure_workspace", {"action": "set_excludes", "exclude_globs": [], "format": "json"})
        assert not r.is_error, r.structured_content
        await _until("the excluded files to come back", lambda: _count(c, EXCLUDED_SQL), lambda n: bool(n))
        await _until("the rescan to finish", lambda: _scans_done(c), bool)

    async with Client(_params(ws, cache), mode="legacy", cache=None) as c:
        restored = await _until("the restored workspace", lambda: _status(c), lambda d: len(d["workspaces"]) == 1)
        (entry,) = restored["workspaces"]
        assert entry["exclude_globs"] == []
        await _until("the startup scan", lambda: _scans_done(c), bool)
        assert await _count(c, EXCLUDED_SQL)
