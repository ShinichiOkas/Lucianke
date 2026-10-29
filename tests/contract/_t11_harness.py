"""T-11 ``rename_symbol`` の契約テストが共有する土台（偽 LSP の ``test_T11_rename.py`` と実機の ``test_T11_rename_real.py``）。

* ``Rig``: ``Pipeline`` 越しにツールを呼び、作業ツリーを読み書きする。
* ``build_rig``: ``lsp_pool`` と ``edit_transaction(ws, **overrides)`` を足した ``ToolServices`` を組む。
* ``warm_up``: 実 LSP の冷間の対策（BUG-0012）。文書を開き、サーバーが最初の診断（= 解析の完了）を返すまで待つ。
  要求の期限を延ばしたり再試行したりしない。待つのは条件（診断の到着）だけで、固定の ``sleep`` は使わない。
* フィクスチャ ``procs`` / ``index_writer``（``index_writer_fixture`` / ``procs_fixture`` を取り込む。引数名と同じ名前を
  import すると F811 になるので、``name=`` で別名にしてある）。
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lucianke.app import build_services
from lucianke.core.model import Position, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.core.urn import CodeSel, Urn
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.transaction import EditTransaction
from lucianke.index.writer import IndexWriter
from lucianke.lsp.servers import default_server, find_command
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import t11_rename_symbol as t11
from lucianke.tools._services import ToolServices
from lucianke.workspace.types import Workspace

REPO = Path(__file__).resolve().parents[2]
POLY = REPO / "tests" / "fixtures" / "poly"
WS = "poly"
PAY = "src/shop/services/payment.py"
ROUTES = "src/shop/api/routes.py"
TESTS = "tests/test_payment.py"
RST = "docs/api.rst"
PROCESS_CHARGE = "PaymentProcessor.process_charge"
PY_FILES = [PAY, ROUTES, TESTS]

# 最初の診断を待つ上限。解析の完了そのものを待つので、負荷の下でも通るように長くとる（要求の期限ではない）。
FIRST_ANALYSIS_TIMEOUT_MS = 90_000

BASEDPYRIGHT = default_server("python", "types")
BASEDPYRIGHT_CMD = (
    find_command(BASEDPYRIGHT, workspace_root=REPO, venv=Path(sys.prefix)) if BASEDPYRIGHT else None
)
needs_basedpyright = pytest.mark.skipif(BASEDPYRIGHT_CMD is None, reason="basedpyright is not installed")


def U(path: str, qualified: str) -> str:
    return str(Urn(WS, path, CodeSel(qualified)))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    """ワークスペースの全ファイルの SHA-256（相対パス → sha）。"""
    return {
        p.relative_to(root).as_posix(): sha(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def copy_poly(tmp_path: Path) -> Path:
    dst = tmp_path / "poly"
    shutil.copytree(POLY, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst.resolve()


# --- 土台 -----------------------------------------------------------------------------------


@dataclass(slots=True)
class Services(ToolServices):
    """``ToolServices`` に、T-11 が求める ``lsp_pool`` と ``edit_transaction(ws, **overrides)`` を足したもの。"""

    ws: Workspace | None = None
    lsp_pool: Any = None
    handle: Any = None
    tx_kwargs: dict[str, Any] = field(default_factory=dict)

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        assert self.ws is not None
        return self.ws

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        return WS

    def edit_transaction(self, ws: Workspace, **overrides: Any) -> EditTransaction:
        """本番のファクトリ（``WiredServices.edit_transaction``）の役: 文書の同期と診断の待ちはファクトリの仕事で、
        ツールは ``lsp_status`` しか渡さない（BUG-0023）。ここでは 1 サーバーなので LSP ハンドル自身の口を使う。"""
        wiring: dict[str, Any] = {}
        if self.handle is not None:
            wiring = {"documents": self.handle.documents, "diagnostics_hub": self.handle.diagnostics}
        return EditTransaction(ws, **{**self.tx_kwargs, **wiring, **overrides})


@dataclass
class Rig:
    pipeline: Pipeline
    svc: Services
    ws: Workspace
    pool: Any
    handle: Any
    root: Path

    async def rename(self, target: dict[str, Any], new_name: str, fmt: str = "json", **args: Any):
        args.setdefault("diagnostics_wait_ms", 0)
        return await self.pipeline.call_tool(
            "rename_symbol", {"target": target, "new_name": new_name, "format": fmt, **args}
        )

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    async def write(self, rel: str, text: str) -> None:
        """新しいファイルを作り、LSP に開かせて最初の診断を待つ（LSP は作業ツリーの新規ファイルを自分では見つけない。
        本番では監視が ``workspace/didChangeWatchedFiles`` で知らせる）。"""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        if self.handle is not None:
            await warm_up(self.handle, self.ws, [RelPath(rel)])

    def pos(self, rel: str, needle: str, nth_line: int = 0) -> Position:
        """``needle`` を含む行（``nth_line`` 番目）の、その位置（0 始まり・コードポイント）。"""
        hits = [(i, t) for i, t in enumerate(self.read(rel).split("\n")) if needle in t]
        line, text = hits[nth_line]
        return Position(line, text.index(needle))


def build_rig(tmp_path: Path, pool: Any, handle: Any, ws: Workspace, writer: IndexWriter) -> Rig:
    registry = ToolRegistry()
    registry.register(t11.TOOL_SPEC)
    b = build_services(lambda: [str(ws.root)])
    svc = Services(
        roots=b.roots,
        trees=b.trees,
        lines=b.lines,
        ws=ws,
        lsp_pool=pool,
        handle=handle,
        tx_kwargs={
            "locks": WorkspaceLocks(),
            "writer_index": writer,
            "journal_dir": tmp_path / "cache" / "journal",
        },
    )
    return Rig(Pipeline(registry, services=svc), svc, ws, pool, handle, ws.root)


async def warm_up(handle: Any, ws: Workspace, rels: Sequence[str | RelPath]) -> None:
    """文書を開き、サーバーが最初の診断（= その文書の解析の完了）を返すまで待つ（BUG-0012）。

    ``ready`` の合図（診断・``$/progress end``・猶予時間）は basedpyright の解析の完了を意味しない。最初の要求が解析
    そのものを払うので、負荷の下では rename・references の期限を超えうる。要求の期限は延ばさず、再試行もせず、
    解析を終えたという合図（診断の到着）を待ってから本番の要求に入る。
    """
    paths = [RelPath(str(r)) for r in rels]
    for rel in paths:
        text = (ws.root / rel).read_bytes().decode("utf-8")
        handle.documents.sync(rel, text, "python")
    for rel in paths:
        sent_at = handle.documents.sent_at_of(rel)
        assert sent_at is not None
        assert await handle.diagnostics.wait_for(
            rel, min_version=1, sent_at=sent_at, timeout_ms=FIRST_ANALYSIS_TIMEOUT_MS
        ), f"basedpyright did not finish analysing {rel}"


@pytest.fixture(name="index_writer")
async def index_writer_fixture(tmp_path: Path) -> AsyncIterator[IndexWriter]:
    w = IndexWriter(tmp_path / "cache" / "paad.sqlite")
    try:
        yield w
    finally:
        await asyncio.to_thread(w.close)


@pytest.fixture(name="procs")
async def procs_fixture():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)
