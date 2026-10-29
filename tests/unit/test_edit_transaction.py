"""edit/transaction.py・edit/verify.py・edit/builders.py: 編集トランザクションの 8 段と診断差分。

仕様書 第 10 章、§10.10 の EP-1〜EP-9、設計書 §7.7。``tests/fixtures/poly`` を ``tmp_path`` に写してワークスペースにする。
LSP・監視・索引は偽物を注入する（索引は「呼ばれた」ことを記録するだけ）。診断の DB 読み出しは 2 つのメソッドを差し替える。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.model import Confidence, Position, Range, RelPath, TargetRef
from lucianke.edit.builders import replace_body_builder
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.transaction import EditTransaction
from lucianke.edit.writer import TMP_PREFIX
from lucianke.index.queries import DiagnosticRow
from lucianke.index.writer import IndexWriter
from lucianke.workspace.types import Workspace

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAYMENT = RelPath("src/shop/services/payment.py")
CRLF_BOM = RelPath("src/shop/models/crlf_bom.py")
COBOL = RelPath("legacy/calc.cob")
VALIDATE = TargetRef(path=str(PAYMENT), symbol_name="PaymentProcessor._validate_limit")

NEW_VALIDATE = """\
def _validate_limit(self, amount: Decimal) -> bool:
    limit = self._limits.get(self.currency)
    if limit is None:
        raise LimitExceeded(self.currency)
    return amount < limit
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- 偽物 ---------------------------------------------------------------------------------


@dataclass
class FakeIndexer:
    calls: list[str] = field(default_factory=list)

    async def index_file(self, rel: RelPath) -> None:
        self.calls.append(str(rel))


@dataclass
class FakeWatcher:
    drained: int = 0
    expected: list[tuple[str, str]] = field(default_factory=list)
    on_drain: Any = None

    async def drain(self) -> None:
        self.drained += 1
        if self.on_drain is not None:
            self.on_drain()

    def expect(self, rel: RelPath, sha256_after: str) -> None:
        self.expected.append((str(rel), sha256_after))


@dataclass
class FakeDocuments:
    versions: dict[str, int] = field(default_factory=dict)
    sent: dict[str, float] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)
    clock: float = 0.0

    def sync(self, path: RelPath, text: str, language_id: str) -> int:
        key = str(path)
        if key in self.texts and self.texts[key] == text:
            return self.versions[key]
        self.clock += 1.0
        self.versions[key] = self.versions.get(key, 0) + 1
        self.sent[key] = self.clock
        self.texts[key] = text
        return self.versions[key]

    def sent_at_of(self, path: RelPath) -> float | None:
        return self.sent.get(str(path))

    def is_open(self, path: RelPath) -> bool:
        return str(path) in self.versions


@dataclass
class FakeHub:
    """``wait_for`` が ``gate`` を待つ。``stale`` に入れたパスは揃わない（タイムアウトで偽）。"""

    gate: asyncio.Event | None = None
    stale: set[str] = field(default_factory=set)
    waits: list[tuple[str, int | None]] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait_for(
        self, path: RelPath, *, min_version: int | None, sent_at: float, timeout_ms: float, **_: Any
    ) -> bool:
        self.waits.append((str(path), min_version))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return str(path) not in self.stale


def diag(path: RelPath, message: str, *, line: int = 0, source: str = "pyright", code: str | None = "X1",
         urn: str | None = None) -> DiagnosticRow:  # fmt: skip
    return DiagnosticRow(
        path=path,
        severity="error",
        source=source,
        code=code,
        range=Range(Position(line, 0), Position(line, 5)),
        message=message,
        enclosing_urn=urn,
        confidence=Confidence.EXACT,
    )


# --- 土台 ---------------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    dst = tmp_path / "poly"
    shutil.copytree(POLY, dst)
    return Path(os.path.realpath(dst))


@pytest.fixture
def ws(root: Path) -> Workspace:
    return Workspace(workspace_id="poly", root=root)


@pytest.fixture
async def index_writer(tmp_path: Path) -> AsyncIterator[IndexWriter]:
    w = IndexWriter(tmp_path / "cache" / "paad.sqlite")
    try:
        yield w
    finally:
        await asyncio.to_thread(w.close)


@dataclass
class Rig:
    tx: EditTransaction
    indexer: FakeIndexer
    watcher: FakeWatcher
    documents: FakeDocuments | None
    hub: FakeHub | None
    locks: WorkspaceLocks
    root: Path
    journal: Path


def make_rig(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, *, lsp: bool = False, **kw: Any
) -> Rig:
    indexer, watcher = FakeIndexer(), FakeWatcher()
    documents = FakeDocuments() if lsp else None
    hub = FakeHub() if lsp else None
    locks = WorkspaceLocks()
    journal = tmp_path / "cache" / "journal"
    tx = EditTransaction(
        ws,
        locks=locks,
        writer_index=index_writer,
        journal_dir=journal,
        indexer=indexer,
        watcher=watcher,
        documents=documents,
        diagnostics_hub=hub,
        **kw,
    )
    return Rig(tx, indexer, watcher, documents, hub, locks, ws.root, journal)


@pytest.fixture
def rig(ws: Workspace, index_writer: IndexWriter, tmp_path: Path) -> Rig:
    return make_rig(ws, index_writer, tmp_path)


def read(rig: Rig, rel: RelPath) -> bytes:
    return (rig.root / rel).read_bytes()


def leftovers(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.name.startswith(TMP_PREFIX)]


async def replace_validate(rig: Rig, new_code: str = NEW_VALIDATE, **kw: Any):
    return await rig.tx.run(
        replace_body_builder(VALIDATE, new_code), target=VALIDATE, diagnostics_wait_ms=0, **kw
    )


# --- EP-1 ----------------------------------------------------------------------------------


async def test_ep1_replace_body_runs_the_eight_stages_and_writes(rig: Rig):
    before = read(rig, PAYMENT)
    result = await replace_validate(rig)

    assert result.applied is True
    assert result.edit_id == "ed_0001"
    after = read(rig, PAYMENT)
    assert after != before
    # EP-1: sha256_after はディスクと一致する
    (change,) = result.files_changed
    assert change.path == PAYMENT
    assert change.sha256_before == sha(before)
    assert change.sha256_after == sha(after)
    assert change.hunks == 1
    assert b"return amount < limit" in after and b"return amount <= limit" not in after
    assert result.syntax_check == "ok"
    assert (
        "-        return amount <= limit" in result.diff and "+        return amount < limit" in result.diff
    )
    assert result.stages == (
        "Resolved", "Planned", "Locked", "PreconditionsVerified", "SyntaxChecked",
        "Journaled", "Written", "Reindexed", "Diagnosed", "Done",
    )  # fmt: skip
    assert result.target_urns_after and result.target_urns_after[0].endswith(
        "PaymentProcessor._validate_limit"
    )
    assert result.new_range is not None
    # 再索引・監視・LSP なしの経路
    assert rig.indexer.calls == [str(PAYMENT)]
    assert rig.watcher.expected == [(str(PAYMENT), sha(after))]
    assert leftovers(rig.root) == []
