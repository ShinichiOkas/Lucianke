"""F-PY（httpx）・F-TS（ky）をワークスペースにして、結線済みのサーバーをプロセス内に組む土台（BUG-0036、M1-1）。

* フィクスチャの原本は ``bench/.cache/<name>/``（``bench/fetch_fixtures.py`` が SHA 固定で取得する）。**原本には触れない**:
  試験は ``tmp_path`` に複製（``.git`` と ``node_modules`` を除く）した作業ツリーに対して走る。編集系も複製に対して行う。
* 結線は本番の合成ルート（``app.run_stdio``）と同じ: ``RuntimeHub`` + ``LspPool(on_diagnostics=hub.on_diagnostics)`` +
  ``WiredServices`` + ``Pipeline``（全ツールを ``register_tools`` で登録）。索引（差分索引の背景ジョブ）の完了を待ってから渡す。
* LSP は ``.venv`` の basedpyright と ruff（Python）。TypeScript の LSP（typescript-language-server / vtsls）は、見つかれば使い、
  無ければ構文層（Tree-sitter）の降格応答で事後条件を確かめる（``Rig.lsp_ready``）。
* 取得されていない環境（CI など）は ``FixtureMissing``。試験側が ``skip`` の理由に使う。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _t11_harness import snapshot  # tests/contract の共通部品（conftest が sys.path に足す）

from lucianke.core.model import RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import LspServerDef, find_command, servers_for
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import register_tools
from lucianke.tools._runtime import RuntimeHub, WiredServices
from lucianke.workspace.types import Workspace

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "bench"
CACHE = BENCH / ".cache"
FIXTURES_TOML = BENCH / "fixtures.toml"

# 索引・LSP の準備を待つ上限。条件待ち（完了したら即戻る）なので、共有の作業ツリーで CPU が飽和していても足りる長さ。
INDEX_WAIT_S = 300.0
READY_WAIT_MS = 120_000
FIRST_ANALYSIS_TIMEOUT_MS = 120_000

COPY_IGNORE = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache")


class FixtureMissing(Exception):
    """取得済みのフィクスチャが無い（``bench/fetch_fixtures.py`` を走らせていない）。"""


def fixture_spec(key: str) -> dict[str, Any]:
    data = tomllib.loads(FIXTURES_TOML.read_text(encoding="utf-8"))
    return data[key]


def fixture_dir(key: str) -> Path:
    """取得済みの原本の場所。無い・SHA が違うときは ``FixtureMissing``（理由つき）。"""
    spec = fixture_spec(key)
    d = CACHE / str(spec["name"])
    if not (d / ".git").exists():
        raise FixtureMissing(
            f"{key} ({spec['name']}) is not fetched: run `uv run --no-project python bench/fetch_fixtures.py`"
        )
    try:
        head = subprocess.run(  # noqa: S603  固定の引数列
            ["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True, check=False, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise FixtureMissing(f"{key}: cannot read the fixture HEAD ({exc})") from exc
    if head != spec["sha"]:
        raise FixtureMissing(
            f"{key} ({spec['name']}) is at {head[:12]}, fixtures.toml pins {str(spec['sha'])[:12]}"
        )
    return d


def missing_reason(key: str) -> str | None:
    """``None`` なら取得済み。そうでなければ skip の理由。"""
    try:
        fixture_dir(key)
    except FixtureMissing as exc:
        return str(exc)
    return None


def _lsp_command(defn: LspServerDef, root: Path) -> list[str] | None:
    return find_command(defn, workspace_root=root, venv=Path(sys.prefix))


def available_servers(language_id: str, root: Path) -> list[LspServerDef]:
    """``language_id`` の定義のうち、この環境で起動コマンドが見つかるもの（役ごとに先頭 1 つ）。"""
    out: list[LspServerDef] = []
    seen_roles: set[str] = set()
    for defn in servers_for(language_id):
        if defn.role in seen_roles:
            continue
        if _lsp_command(defn, root) is not None:
            out.append(defn)
            seen_roles.add(defn.role)
    return out


@dataclass(slots=True)
class Rig:
    key: str  # "F-PY" | "F-TS"
    language: str  # "python" | "typescript"
    root: Path  # 複製した作業ツリー（実パス）
    ws: Workspace
    hub: RuntimeHub
    pool: LspPool
    pipeline: Pipeline
    procs: ProcessRegistry
    types_ready: bool = False
    lint_ready: bool = False
    servers: list[str] = field(default_factory=list)

    @property
    def lsp_ready(self) -> bool:
        """型解決の LSP が ready（exact の応答を期待できる）。"""
        return self.types_ready

    async def call(self, tool: str, fmt: str = "json", **args: Any) -> Any:
        return await self.pipeline.call_tool(tool, {"format": fmt, **args})

    async def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        """成功を期待して呼び、``structured`` を返す（失敗は本文つきで assert）。"""
        r = await self.call(tool, **args)
        assert not r.is_error, f"{tool} {args}: {r.text[:800]}"
        return r.structured

    def read(self, rel: str) -> str:
        return (self.root / rel).read_bytes().decode("utf-8")

    def snapshot(self) -> dict[str, str]:
        return snapshot(self.root)


def copy_fixture(key: str, dst_parent: Path) -> Path:
    src = fixture_dir(key)
    dst = dst_parent / str(fixture_spec(key)["name"])
    shutil.copytree(src, dst, ignore=COPY_IGNORE)
    return Path(os.path.realpath(dst))


async def open_rig(key: str, tmp_path: Path) -> Rig:
    """``key`` の複製を ``tmp_path`` に作り、結線済みのサーバーを組んで、索引の完了と LSP の ready まで待つ。"""
    spec = fixture_spec(key)
    root = copy_fixture(key, tmp_path)
    language = str(spec["language"])
    procs = ProcessRegistry()
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    servers = available_servers(language, root)
    pool = LspPool(
        procs,
        on_diagnostics=hub.on_diagnostics,
        servers=servers,
        resolver=lambda defn, _ws: _lsp_command(defn, root),
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    hub.lsp_pool = pool
    (ws,) = await hub.sync_roots([str(root)])
    runtime = hub.runtime()
    assert runtime is not None and runtime.scan_job_id is not None, hub.failed
    await asyncio.wait_for(runtime.indexer.wait_job(runtime.scan_job_id), INDEX_WAIT_S)
    registry = ToolRegistry()
    register_tools(registry)
    services = WiredServices(roots=lambda: [str(root)], trees=hub.trees, lines=compact, hub=hub)
    rig = Rig(
        key, language, root, ws, hub, pool, Pipeline(registry, services=services), procs,
        servers=[s.name for s in servers],
    )  # fmt: skip
    for defn in servers:
        ok = False
        try:
            await pool.ensure_started(defn.language_id, ws)
            ok = await pool.wait_ready(defn.language_id, ws, READY_WAIT_MS, role=defn.role)
        except Exception:  # noqa: BLE001  起動できないサーバーは「LSP なし」として降格応答で確かめる
            ok = False
        if defn.role == "types":
            rig.types_ready = ok
        else:
            rig.lint_ready = ok
    return rig


async def close_rig(rig: Rig) -> None:
    await rig.pool.shutdown_all(300)
    await rig.hub.stop_watchers()
    await rig.hub.stop_indexers()
    await rig.hub.close_storage()
    await rig.procs.terminate_all(200, close=True)


async def warm_up(rig: Rig, rels: list[str]) -> None:
    """型解決の LSP に文書を開かせ、最初の診断（= 解析の完了の合図）が届くまで待つ（BUG-0012 と同じ。固定 sleep なし）。"""
    handle = rig.pool.get(rig.language, rig.ws)
    if handle is None or not rig.types_ready:
        return
    for rel in rels:
        handle.documents.sync(RelPath(rel), rig.read(rel), rig.language)
    for rel in rels:
        sent_at = handle.documents.sent_at_of(RelPath(rel))
        if sent_at is not None:
            await handle.diagnostics.wait_for(
                RelPath(rel), min_version=1, sent_at=sent_at, timeout_ms=FIRST_ANALYSIS_TIMEOUT_MS
            )
