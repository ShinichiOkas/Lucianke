"""T-13 を結線済みの ``RuntimeHub`` / ``WiredServices`` / 実物の ``LspPool`` で通す（BUG-0023 の結線の確認）。

Python のワークスペースで ``basedpyright``（types）と ``ruff``（lint）を両方 ``ready`` にし、ruff の QuickFix（未使用 import の削除）を
適用する。応答の ``new_diagnostics.resolved`` に、ruff の診断だけでなく **basedpyright の診断**（解決できない import・未使用 import）も
入ることを確かめる。``edit_transaction(ws, lsp=handle)`` が 1 サーバー分の口でなく ``DocumentRouter`` を渡し、書き込み後の ``didChange`` と
診断の待ちが両方のサーバーへ届くこと、LSP の診断が PaaD の ``diagnostics`` 表にサーバー名の行として入ること
（ruff は診断に ``"Ruff"`` と書く）が前提。

実機（``.venv`` の ``basedpyright`` と ``ruff``）が無ければ skip。待ちは条件待ち。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import LspServerDef, default_server, find_command
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t13_apply_code_action as t13
from lucianke.tools._runtime import RuntimeHub, WiredServices
from lucianke.workspace.types import Workspace

REPO = Path(__file__).resolve().parents[2]
FILE = "pkg/unused.py"
BEFORE = "import missing_mod_x\nimport sys\n\n\ndef main() -> int:\n    return len(sys.argv)\n"
AFTER = "import sys\n\n\ndef main() -> int:\n    return len(sys.argv)\n"
READY_WAIT_MS = 120_000  # 共有の作業ツリーで CPU が飽和していても足りる長さ(通常は数秒)

_TYPES = default_server("python", "types")
_LINT = default_server("python", "lint")


def _cmd(defn: LspServerDef | None) -> list[str] | None:
    return find_command(defn, workspace_root=REPO, venv=REPO / ".venv") if defn is not None else None


needs_both = pytest.mark.skipif(_cmd(_TYPES) is None or _cmd(_LINT) is None, reason="basedpyright and ruff are required")


@dataclass(slots=True)
class Rig:
    root: Path
    ws: Workspace
    hub: RuntimeHub
    pool: LspPool
    pipeline: Pipeline

    async def call(self, **args: Any) -> Any:
        args.setdefault("diagnostics_wait_ms", 10_000)
        return await self.pipeline.call_tool("apply_code_action", {"format": "json", **args})


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    root = Path(os.path.realpath(tmp_path / "proj"))
    (root / "pkg").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F"]\n', encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / FILE).write_text(BEFORE, encoding="utf-8")
    assert _TYPES is not None and _LINT is not None
    procs = ProcessRegistry()
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    pool = LspPool(
        procs,
        on_diagnostics=hub.on_diagnostics,
        servers=[_TYPES, _LINT],
        resolver=lambda defn, _ws: _cmd(defn),
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    hub.lsp_pool = pool
    try:
        (ws,) = await hub.sync_roots([str(root)])  # 書き手のスレッドを作るので try の中（BUG-0076）
        runtime = hub.runtime()
        assert runtime is not None and runtime.scan_job_id is not None
        await runtime.indexer.wait_job(runtime.scan_job_id)
        services = WiredServices(roots=lambda: [str(root)], trees=hub.trees, lines=compact, hub=hub)
        tools = ToolRegistry()
        tools.register(t13.SPEC)
        yield Rig(root, ws, hub, pool, Pipeline(tools, services=services))
    finally:
        await pool.shutdown_all(300)
        await hub.stop_watchers()
        await hub.stop_indexers()
        await hub.close_storage()
        await procs.terminate_all(200, close=True)
        assert procs.live() == []


@needs_both
async def test_a_ruff_quickfix_reports_the_basedpyright_diagnostics_it_resolved(rig: Rig) -> None:
    for role in ("types", "lint"):
        assert await rig.pool.wait_ready("python", rig.ws, READY_WAIT_MS, role=role), role  # type: ignore[arg-type]

    # 種別指定は曖昧: 候補は両方のサーバーから出る(ruff の削除と、basedpyright の `# pyright: ignore`)
    listed = await rig.call(path=FILE, line=1, action_kind_or_id="quickfix")
    assert listed.is_error and listed.structured["error"]["code"] == "E_TARGET_AMBIGUOUS", listed.text
    candidates = listed.structured["error"]["candidates"]
    assert {c["server"] for c in candidates} == {"ruff", "basedpyright"}, candidates
    [fix_id] = [c["id"] for c in candidates if c["server"] == "ruff" and "Remove unused import" in c["title"]]

    applied = await rig.call(path=FILE, line=1, action_kind_or_id=fix_id)
    assert not applied.is_error, applied.text
    data = applied.structured["data"]
    assert (rig.root / FILE).read_text(encoding="utf-8") == AFTER
    assert data["applied"] is True and data["action"]["source"] == "ruff"
    nd = data["new_diagnostics"]
    assert nd["status"] == "complete", nd
    resolved = {(d["source"], d["code"]) for d in nd["resolved"]}
    assert ("ruff", "F401") in resolved, nd
    # BUG-0023: ruff が供給した編集の結果として、basedpyright の診断の解消も同じ応答に入る
    assert ("basedpyright", "reportMissingImports") in resolved, nd
    assert ("basedpyright", "reportUnusedImport") in resolved, nd
    assert nd["added"] == [], nd
