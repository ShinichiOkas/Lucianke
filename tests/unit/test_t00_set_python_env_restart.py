"""BUG-0035: T-00 ``set_python_env`` は、動いている Python 系の LSP を 1 回だけ起動し直す。

実物の ``RuntimeHub`` + ``LspPool`` + 偽の LSP サーバー（``test_lsp_pool.FAKE_SERVER``）で、起動の試み（standby → starting）を
数える。``hub.workspace_changed`` → ``LspPool.rebind`` が Python 系を止めて起動し直したあとで、T-00 のジョブがもう一度
止めて起動し直していた（1 回の呼び出しで 2 回）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_lsp_pool import FAKE_SERVER

from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.pool import LspHandle, LspPool, LspState
from lucianke.lsp.servers import LspServerDef
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t00_configure_workspace as t00
from lucianke.tools._runtime import RuntimeHub
from lucianke.workspace.types import Workspace

TYPES = LspServerDef(language_id="python", name="fakepy", argv_candidates=(("fakepy",),), role="types")
LINT = LspServerDef(language_id="python", name="fakeruff", argv_candidates=(("fakeruff",),), role="lint")


def starts_of(h: LspHandle) -> int:
    """このハンドルが起動を始めた回数（standby → starting の数）。プロセスを起こす前に取り消された起動も数える。"""
    return sum(1 for s in h.history if s is LspState.STARTING)


@dataclass
class Rig:
    hub: RuntimeHub
    pool: LspPool
    ws: Workspace
    root: Path
    tmp: Path

    def handles(self) -> dict[str, LspHandle]:
        return {h.role: h for h in self.pool.handles(self.ws, "python")}

    def launches(self, name: str) -> int:
        f = self.tmp / f"{name}.count"
        return int(f.read_text()) if f.exists() else 0

    async def ready(self) -> None:
        for role in ("types", "lint"):
            assert await self.pool.wait_ready("python", self.ws, 20_000, role=role)  # type: ignore[arg-type]

    def shim(self, name: str) -> Path:
        """実行できる偽のインタープリタ（自動検出された環境とは別のパス）。"""
        exe = self.tmp / (f"{name}.cmd" if sys.platform == "win32" else name)
        exe.write_text("@echo off\n" if sys.platform == "win32" else "#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
        return exe

    async def set_python_env(self, interpreter: Path) -> str:
        args = t00.ConfigureWorkspaceArgs(action="set_python_env", python_interpreter=str(interpreter))
        started: list[tuple[str, str]] = []
        await t00._set_python_env(self.hub, args, started, [])
        ((_, job_id),) = started
        deadline = time.monotonic() + 20
        while (job := t00._board(self.hub).get(job_id)) is None or job["state"] not in ("done", "failed"):
            assert time.monotonic() < deadline, f"job {job_id} did not finish: {job}"
            await asyncio.sleep(0.02)
        assert job["state"] == "done", job
        return job_id


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")

    def resolver(defn: LspServerDef, ws: Workspace) -> list[str]:
        return [sys.executable, str(script), "--progress", "0.05", "--counter", str(tmp_path / f"{defn.name}.count")]

    registry = ProcessRegistry()
    pool = LspPool(
        registry,
        servers=(TYPES, LINT),
        resolver=resolver,
        backoff_s=(0.05, 0.05, 0.05),
        ready_fallback_s=60.0,
        grace_ms=300,
    )
    hub = RuntimeHub(tmp_path / "cache", TreeCache(), lsp_pool=pool)
    (ws,) = await hub.sync_roots([str(root)])
    try:
        yield Rig(hub, pool, ws, root, tmp_path)
    finally:
        await pool.shutdown_all(300)
        await hub.stop_watchers()
        await hub.stop_indexers()
        await hub.close_storage()
        await registry.terminate_all(300, close=True)


async def test_a_running_python_lsp_restarts_once_when_the_interpreter_changes(rig: Rig) -> None:
    await rig.pool.ensure_started("python", rig.ws)
    await rig.ready()
    before = {role: starts_of(h) for role, h in rig.handles().items()}
    assert before == {"types": 1, "lint": 1}

    await rig.set_python_env(rig.shim("py-a"))  # 自動検出と違う環境: LspPool.rebind が止めて起動し直す
    await rig.ready()
    for role, h in rig.handles().items():
        assert starts_of(h) == 2, f"{role}: the Python LSP was started {starts_of(h)} times (want 1 + 1)"
        assert h.ws.python_env is not None and h.ws.python_env.interpreter.lower().endswith(("py-a.cmd", "py-a"))
        assert h.restarts == 0  # クラッシュ扱いにしない
    assert (rig.launches("fakepy"), rig.launches("fakeruff")) == (2, 2)  # プロセスも 1 回ずつ起こし直しただけ


async def test_setting_the_same_interpreter_again_still_restarts_once(rig: Rig) -> None:
    """インタープリタが同じなら rebind は再起動しない。パッケージを入れ直したあとの取り直しは T-00 が 1 回だけ起こし直す（事後条件 3）。"""
    await rig.pool.ensure_started("python", rig.ws)
    await rig.ready()
    shim = rig.shim("py-a")
    await rig.set_python_env(shim)
    await rig.ready()
    after_first = {role: starts_of(h) for role, h in rig.handles().items()}
    assert after_first == {"types": 2, "lint": 2}

    await rig.set_python_env(shim)
    await rig.ready()
    assert {role: starts_of(h) for role, h in rig.handles().items()} == {"types": 3, "lint": 3}
    assert (rig.launches("fakepy"), rig.launches("fakeruff")) == (3, 3)


async def test_a_standby_python_lsp_is_started_once(rig: Rig) -> None:
    """まだ使っていない（ハンドルが無い）Python 系は、rebind では何も起きない。T-00 のジョブが 1 回起動する。"""
    assert rig.pool.handles(rig.ws) == []
    await rig.set_python_env(rig.shim("py-a"))
    await rig.ready()
    assert {role: starts_of(h) for role, h in rig.handles().items()} == {"types": 1, "lint": 1}
    assert (rig.launches("fakepy"), rig.launches("fakeruff")) == (1, 1)


async def test_status_reports_started_at_of_the_python_daemons_after_set_python_env(rig: Rig) -> None:
    """BUG-0043: ``lsp_daemons[].started_at`` は起動した Python 系で ``null`` にならず、呼び出し時刻より後（仕様書 T-00 事後条件 3）。"""
    await rig.pool.ensure_started("python", rig.ws)
    await rig.ready()
    called_at = datetime.now(UTC)
    await asyncio.sleep(0.05)  # Windows の時計は 15 ms 刻み
    await rig.set_python_env(rig.shim("py-a"))
    await rig.ready()
    python = [d for d in t00._lsp_daemons(rig.pool, rig.ws, ["python"]) if d["language"] == "python"]
    assert {d["role"] for d in python} == {"types", "lint"}
    for d in python:
        assert d["status"] == "ready" and d["pid"] is not None
        assert d["started_at"] is not None and datetime.fromisoformat(d["started_at"]) > called_at
