"""edit/transaction.py・edit/verify.py・edit/builders.py: 編集トランザクションの 8 段と診断差分。

仕様書 第 10 章、§10.10 の EP-1〜EP-9、設計書 §7.7。``tests/fixtures/poly`` を ``tmp_path`` に写してワークスペースにする。
LSP・監視・索引は偽物を注入する（索引は「呼ばれた」ことを記録するだけ）。診断の DB 読み出しは 2 つのメソッドを差し替える。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import lucianke.workspace.watcher as watcher_mod
from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import WATCH_EXPECT_TTL_S
from lucianke.core.model import Confidence, Position, Range, RelPath, TargetRef
from lucianke.edit.builders import replace_body_builder
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.plan import Hunk, Plan
from lucianke.edit.transaction import EditTransaction, PlanContext
from lucianke.edit.verify import DiagDelta, Snapshot, compute_delta, structural_delta
from lucianke.edit.writer import TMP_PREFIX
from lucianke.index.queries import DiagnosticRow
from lucianke.index.writer import IndexWriter
from lucianke.lsp.diagnostics import DiagnosticsHub
from lucianke.lsp.documents import DocumentRouter, DocumentSync
from lucianke.syntax.parsers import parse_source
from lucianke.workspace.paths import to_file_uri
from lucianke.workspace.types import Workspace
from lucianke.workspace.watcher import Watcher

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
    forgotten: list[str] = field(default_factory=list)
    on_drain: Any = None

    async def drain(self) -> None:
        self.drained += 1
        if self.on_drain is not None:
            self.on_drain()

    def expect(self, rel: RelPath, sha256_after: str) -> None:
        self.expected.append((str(rel), sha256_after))

    def forget(self, rel: RelPath) -> None:
        self.forgotten.append(str(rel))


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
    """``wait_for`` が ``gate`` を待つ（待ち始めたら ``blocked``）。``stale`` に入れたパスは揃わない（タイムアウトで偽）。
    ``stale_first`` に入れたパスは最初の 1 回（編集前の基準の待ち）だけ揃わない（BUG-0098）。"""

    gate: asyncio.Event | None = None
    stale: set[str] = field(default_factory=set)
    stale_first: set[str] = field(default_factory=set)
    waits: list[tuple[str, int | None]] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    blocked: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait_for(
        self, path: RelPath, *, min_version: int | None, sent_at: float, timeout_ms: float, **_: Any
    ) -> bool:
        first = all(p != str(path) for p, _v in self.waits)
        self.waits.append((str(path), min_version))
        self.entered.set()
        if self.gate is not None:
            self.blocked.set()
            await self.gate.wait()
        return str(path) not in self.stale and not (first and str(path) in self.stale_first)


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
    assert rig.watcher.forgotten == []
    assert leftovers(rig.root) == []


# --- EP-2: applied: false のあとバイト不変 -------------------------------------------------------


async def test_ep2_dry_run_changes_nothing_and_predicts_the_write(rig: Rig):
    before = read(rig, PAYMENT)
    dry = await replace_validate(rig, dry_run=True)
    assert dry.applied is False and dry.edit_id is None
    assert read(rig, PAYMENT) == before
    assert dry.diff and dry.files_changed[0].sha256_after != sha(before)
    assert dry.new_diagnostics.status == "not_computed"
    assert rig.indexer.calls == [] and rig.watcher.expected == []
    assert not rig.journal.exists() or list(rig.journal.glob("*")) == []

    real = await replace_validate(rig)
    assert real.files_changed[0].sha256_after == dry.files_changed[0].sha256_after  # 予測値のとおり
    assert real.diff == dry.diff


@pytest.mark.parametrize(
    "error_code, runner",
    [
        (ErrorCode.E_TARGET_NOT_FOUND, "missing_symbol"),
        (ErrorCode.E_SYNTAX_INVALID, "body_only"),
        (ErrorCode.E_INVALID_ARGUMENT, "empty_code"),
    ],
)
async def test_ep2_errors_leave_every_byte_alone(rig: Rig, error_code: ErrorCode, runner: str):
    before = read(rig, PAYMENT)
    target = (
        TargetRef(path=str(PAYMENT), symbol_name="PaymentProcessor.nope")
        if runner == "missing_symbol"
        else VALIDATE
    )
    code = {"body_only": "return amount < 1\n", "empty_code": "  \n"}.get(runner, NEW_VALIDATE)
    with pytest.raises(LuciankeError) as exc:
        await rig.tx.run(replace_body_builder(target, code), target=target, diagnostics_wait_ms=0)
    assert exc.value.code is error_code
    assert read(rig, PAYMENT) == before
    assert leftovers(rig.root) == []
    assert rig.indexer.calls == []


async def test_body_only_new_code_is_rejected_with_the_hint(rig: Rig):
    with pytest.raises(LuciankeError) as exc:
        await replace_validate(rig, "return amount < 1\n")
    assert exc.value.code is ErrorCode.E_SYNTAX_INVALID
    assert "シグネチャを含む定義全体" in exc.value.details["hint"]


async def test_non_working_tree_revision_is_rejected_before_anything(rig: Rig):
    with pytest.raises(LuciankeError) as exc:
        await rig.tx.run(replace_body_builder(VALIDATE, NEW_VALIDATE), target=VALIDATE, revision="HEAD")
    assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT and exc.value.details["param"] == "revision"


async def test_ambiguous_target_lists_candidates_and_changes_nothing(rig: Rig):
    dup = rig.root / "src/shop/dup.py"
    dup.write_text(
        "class A:\n    def go(self):\n        return 1\n\n\nclass B:\n    def go(self):\n        return 2\n",
        encoding="utf-8",
        newline="\n",
    )
    target = TargetRef(path="src/shop/dup.py", symbol_name="go")
    before = dup.read_bytes()
    with pytest.raises(LuciankeError) as exc:
        await rig.tx.run(replace_body_builder(target, "def go(self):\n    return 3\n"), target=target)
    assert exc.value.code is ErrorCode.E_TARGET_AMBIGUOUS
    assert {c["path"] for c in exc.value.candidates} == {"src/shop/dup.py"} and len(exc.value.candidates) == 2
    assert dup.read_bytes() == before


# --- EP-3: expected_file_sha256 -----------------------------------------------------------------


async def test_ep3_wrong_expected_sha_fails_and_changes_nothing(rig: Rig):
    before = read(rig, PAYMENT)
    with pytest.raises(LuciankeError) as exc:
        await replace_validate(rig, expected_shas={str(PAYMENT): "0" * 64})
    assert exc.value.code is ErrorCode.E_PRECONDITION_FAILED
    assert exc.value.details["path"] == str(PAYMENT)
    assert exc.value.details["actual"] == sha(before)
    assert read(rig, PAYMENT) == before


async def test_ep3_prefix_of_twelve_digits_or_more_is_accepted(rig: Rig):
    before = read(rig, PAYMENT)
    result = await replace_validate(rig, expected_shas={str(PAYMENT): sha(before)[:12]})
    assert result.applied is True


async def test_ep3_short_or_non_hex_prefix_is_an_invalid_argument(rig: Rig):
    for bad in (sha(b"x")[:11], "z" * 12, ""):
        with pytest.raises(LuciankeError) as exc:
            await replace_validate(rig, expected_shas={str(PAYMENT): bad})
        assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT


# --- EP-4: 解決と書き込みの間の外部書き込み -------------------------------------------------------


EXTERNAL = b'"""rewritten by someone else."""\n'


@pytest.mark.parametrize(
    "stage", ["Planned", "Locked", "PreconditionsVerified", "SyntaxChecked", "Journaled"]
)
async def test_ep4_external_write_between_resolve_and_write_is_detected_and_kept(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, stage: str
):
    rig = make_rig(ws, index_writer, tmp_path)

    def hook(name: str) -> None:
        if name == stage:
            (rig.root / PAYMENT).write_bytes(EXTERNAL)

    rig.tx.stage_hook = hook
    with pytest.raises(LuciankeError) as exc:
        await replace_validate(rig)
    assert exc.value.code is ErrorCode.E_PRECONDITION_FAILED
    assert read(rig, PAYMENT) == EXTERNAL  # 外部の変更が保たれる
    assert leftovers(rig.root) == []
    assert rig.indexer.calls == []


async def test_ep4_event_delivered_late_is_caught_by_the_drain_recheck(rig: Rig):
    rig.watcher.on_drain = lambda: (rig.root / PAYMENT).write_bytes(EXTERNAL)
    with pytest.raises(LuciankeError) as exc:
        await replace_validate(rig)
    assert exc.value.code is ErrorCode.E_PRECONDITION_FAILED
    assert rig.watcher.drained == 1
    assert read(rig, PAYMENT) == EXTERNAL


# --- EP-5: 構文 ---------------------------------------------------------------------------------


def error_count(data: bytes) -> int:
    parsed, _ = parse_source(data, "python", sha(data))
    return len(parsed.error_ranges)


def breaking_builder(old: str, new: str):
    async def build(ctx: PlanContext) -> Plan:
        text = ctx.source.decode("utf-8")
        i = text.index(old)
        assert ctx.parsed is not None
        start = ctx.parsed.line_index.offset_to_position(len(text[:i].encode("utf-8"))).value
        end = Position(start.line, start.col + len(old))
        return Plan(
            hunks=(Hunk(ctx.path, Range(start, end), new, "test"),), resolved_shas={ctx.path: ctx.sha256}
        )

    return build


async def test_ep5_an_edit_that_adds_an_error_node_is_refused(rig: Rig):
    before = read(rig, PAYMENT)
    builder = breaking_builder("return amount <= limit", "return amount <=")
    with pytest.raises(LuciankeError) as exc:
        await rig.tx.run(builder, target=VALIDATE, diagnostics_wait_ms=0)
    assert exc.value.code is ErrorCode.E_SYNTAX_INVALID
    assert exc.value.details["errors"] and exc.value.details["errors"][0]["line"] >= 1
    assert read(rig, PAYMENT) == before


async def test_ep5_applied_file_has_no_more_error_nodes_than_before(rig: Rig):
    before = read(rig, PAYMENT)
    await replace_validate(rig)
    assert error_count(read(rig, PAYMENT)) <= error_count(before) == 0


async def test_ep5_a_file_that_was_already_broken_may_be_edited_elsewhere(rig: Rig):
    broken = rig.root / "src/shop/broken.py"
    broken.write_bytes(b"def fine(x):\n    return x\n\n\ndef oops(:\n    pass\n")
    target = TargetRef(path="src/shop/broken.py", symbol_name="fine")
    before_errors = error_count(broken.read_bytes())
    assert before_errors > 0
    result = await rig.tx.run(
        replace_body_builder(target, "def fine(x):\n    return x + 1\n"), target=target, diagnostics_wait_ms=0
    )
    assert result.applied is True
    assert error_count(broken.read_bytes()) <= before_errors


# --- EP-6: 恒等編集 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel, name, code",
    [
        (
            CRLF_BOM,
            "crlf_function",
            'def crlf_function(x: int) -> int:\n    """Doubles x."""\n    return x * 2\n',
        ),
        (
            RelPath("src/shop/models/no_final_newline.py"),
            "no_newline_at_end",
            "def no_newline_at_end(value: int) -> int:\n    return value + 1\n",
        ),
    ],
)
async def test_ep6_identity_replace_body_keeps_every_byte(rig: Rig, rel: RelPath, name: str, code: str):
    before = read(rig, rel)
    mtime = (rig.root / rel).stat().st_mtime_ns
    target = TargetRef(path=str(rel), symbol_name=name)
    result = await rig.tx.run(replace_body_builder(target, code), target=target, diagnostics_wait_ms=0)
    assert result.applied is True and result.edit_id is None
    assert result.diff == "" and result.files_changed[0].hunks == 0
    assert result.files_changed[0].sha256_after == sha(before)
    assert any("no-op" in w for w in result.warnings)
    assert read(rig, rel) == before
    assert (rig.root / rel).stat().st_mtime_ns == mtime  # 更新時刻も変えない
    assert rig.indexer.calls == [] and rig.watcher.expected == []
    assert "Journaled" not in result.stages


async def test_edit_keeps_crlf_bom_and_final_newline_state(rig: Rig):
    before = read(rig, CRLF_BOM)
    target = TargetRef(path=str(CRLF_BOM), symbol_name="crlf_function")
    code = 'def crlf_function(x: int) -> int:\n    """Triples x."""\n    return x * 3\n'
    result = await rig.tx.run(replace_body_builder(target, code), target=target, diagnostics_wait_ms=0)
    after = read(rig, CRLF_BOM)
    assert result.applied is True and after.startswith(b"\xef\xbb\xbf")
    assert b"return x * 3\r\n" in after and after.count(b"\r\n") == before.count(b"\r\n")
    assert b"\n" not in after.replace(b"\r\n", b"")


# --- EP-8: 降格インターロック ---------------------------------------------------------------------


def cobol_builder():
    async def build(ctx: PlanContext) -> Plan:
        lines = ctx.source.decode("utf-8").splitlines()
        idx = next(i for i, line in enumerate(lines) if "0.10" in line)
        rng = Range(Position(idx, 0), Position(idx, len(lines[idx])))
        return Plan(
            hunks=(Hunk(ctx.path, rng, "           COMPUTE WS-TAX = WS-AMOUNT * 0.20.", "test"),),
            resolved_shas={ctx.path: ctx.sha256},
            expects_syntax=False,
        )

    return build


async def test_ep8_degraded_file_is_not_written_without_the_flag(rig: Rig):
    target = TargetRef(path=str(COBOL))
    before = read(rig, COBOL)
    result = await rig.tx.run(cobol_builder(), target=target)
    assert result.applied is False and result.edit_id is None and result.forced_dry_run is True
    assert result.confidence < Confidence.STRUCTURAL
    assert "allow_heuristic_mutation" in " ".join(result.warnings)
    assert "0.20" in result.diff and result.syntax_check == "skipped"
    assert read(rig, COBOL) == before  # EP-8 / EP-2
    assert rig.indexer.calls == []


async def test_ep8_flag_without_dry_run_writes_a_degraded_file(rig: Rig):
    target = TargetRef(path=str(COBOL))
    result = await rig.tx.run(
        cobol_builder(), target=target, allow_heuristic_mutation=True, diagnostics_wait_ms=0
    )
    assert result.applied is True and result.edit_id is not None
    assert b"0.20" in read(rig, COBOL) and result.confidence < Confidence.STRUCTURAL
    assert result.warnings  # 推定境界の注意


async def test_ep8_flag_with_explicit_dry_run_still_does_not_write(rig: Rig):
    target = TargetRef(path=str(COBOL))
    before = read(rig, COBOL)
    result = await rig.tx.run(cobol_builder(), target=target, allow_heuristic_mutation=True, dry_run=True)
    assert result.applied is False and result.forced_dry_run is False
    assert read(rig, COBOL) == before


# --- EP-9: 複数ファイル・改名段階の失敗 ---------------------------------------------------------------

LIMITS = RelPath("src/shop/services/limits.py")


def two_file_builder():
    async def build(ctx: PlanContext) -> Plan:
        other_data = (ctx.workspace.root / LIMITS).read_bytes()
        hunks = (
            Hunk(ctx.path, Range(Position(0, 0), Position(0, 0)), "# edited a\n", "test"),
            Hunk(LIMITS, Range(Position(0, 0), Position(0, 0)), "# edited b\n", "test"),
        )
        return Plan(hunks=hunks, resolved_shas={ctx.path: ctx.sha256, LIMITS: sha(other_data)})

    return build


async def test_two_file_plan_writes_both(rig: Rig):
    result = await rig.tx.run(two_file_builder(), target=VALIDATE, diagnostics_wait_ms=0)
    assert [str(c.path) for c in result.files_changed] == [str(PAYMENT), str(LIMITS)]
    assert read(rig, PAYMENT).startswith(b"# edited a") and read(rig, LIMITS).startswith(b"# edited b")
    assert rig.indexer.calls == [str(PAYMENT), str(LIMITS)]


async def test_ep9_rename_failure_on_the_second_file_restores_every_file(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
):
    before = {PAYMENT: read(rig, PAYMENT), LIMITS: read(rig, LIMITS)}
    real_replace = os.replace
    target_abs = rig.root / LIMITS

    def fake(src, dst, *a, **k):
        if Path(dst) == target_abs and Path(src).name.startswith(TMP_PREFIX):
            raise OSError(5, "injected rename failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", fake)
    with pytest.raises(LuciankeError) as exc:
        await rig.tx.run(two_file_builder(), target=VALIDATE, diagnostics_wait_ms=0)
    monkeypatch.setattr(os, "replace", real_replace)
    assert exc.value.code is ErrorCode.E_INTERNAL
    assert exc.value.details["rolled_back"] is True and exc.value.details["log_ref"]
    assert {p: read(rig, p) for p in before} == before  # 全ファイルが書き込み前に戻っている
    assert leftovers(rig.root) == []
    assert rig.indexer.calls == [] and rig.watcher.expected == []


# --- 再索引と監視のこだま（BUG-0087） -----------------------------------------------------------------
# 本物の ``Watcher`` に、索引へ取り込む sink（``tools/_runtime.IndexerSink`` と同じく ``changed`` → ``index_file``）を付け、
# こだま（自分の書き込みのイベント）の窓を ``_consume`` に直接流す（``awatch`` を使わないので窓の割れ方も時刻も決まっている）。


@dataclass
class FlakyIndexer:
    """最初の ``fail`` 回の ``index_file`` は例外。``calls`` は取り込めた呼び出し、``attempts`` はすべての呼び出し。"""

    fail: int = 0
    calls: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)

    async def index_file(self, rel: RelPath) -> None:
        self.attempts.append(str(rel))
        if len(self.attempts) <= self.fail:
            raise RuntimeError(f"injected reindex failure for {rel}")
        self.calls.append(str(rel))


@dataclass
class IndexingSink:
    indexer: FlakyIndexer

    async def changed(self, rel: RelPath) -> None:
        await self.indexer.index_file(rel)

    async def removed(self, rel: RelPath) -> None:
        raise AssertionError(f"unexpected removal: {rel}")

    async def rescan(self) -> None:
        raise AssertionError("unexpected rescan")

    def known_paths_under(self, prefix: str) -> list[RelPath]:
        return []


class Clock:
    """監視モジュールの ``time`` の差し替え。``monotonic`` は手で進める（期待の失効を決定的に試す）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    @staticmethod
    def perf_counter() -> float:
        return time.perf_counter()


@dataclass
class EchoRig:
    tx: EditTransaction
    indexer: FlakyIndexer
    watcher: Watcher
    clock: Clock
    root: Path

    async def echo(self, *windows: set[str]) -> None:
        """監視に窓を順に流して処理し切る。"""
        q: asyncio.Queue[watcher_mod._Item] = asyncio.Queue()
        for window in windows:
            q.put_nowait(window)
        q.put_nowait(None)
        await asyncio.wait_for(self.watcher._consume(q, asyncio.Event()), 60)


def make_echo_rig(
    ws: Workspace,
    index_writer: IndexWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: int = 0,
) -> EchoRig:
    clock = Clock()
    monkeypatch.setattr(watcher_mod, "time", clock)
    indexer = FlakyIndexer(fail=fail)
    watcher = Watcher(ws, IndexingSink(indexer))
    tx = EditTransaction(
        ws,
        locks=WorkspaceLocks(),
        writer_index=index_writer,
        journal_dir=tmp_path / "cache" / "journal",
        indexer=indexer,
        watcher=watcher,
    )
    return EchoRig(tx, indexer, watcher, clock, ws.root)


async def run_validate(tx: EditTransaction, **kw: Any):
    return await tx.run(
        replace_body_builder(VALIDATE, NEW_VALIDATE), target=VALIDATE, diagnostics_wait_ms=0, **kw
    )


async def test_reindex_failure_does_not_drop_the_echo_so_the_watcher_reindexes(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = make_echo_rig(ws, index_writer, tmp_path, monkeypatch, fail=1)
    result = await run_validate(rig.tx)
    assert result.applied is True
    assert f"reindex failed for {PAYMENT}: RuntimeError" in result.warnings
    assert rig.indexer.attempts == [str(PAYMENT)] and rig.indexer.calls == []  # 編集の中では取り込めていない
    await rig.echo({str(PAYMENT)})  # 自分の書き込みのこだま
    assert rig.indexer.calls == [str(PAYMENT)]  # 監視の通常の道で取り込み直した（捨てていない）


async def test_reindex_failure_also_clears_a_stale_expectation_for_the_same_content(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """前の編集の期待が残っていて、書いた内容と ``sha256`` が一致しても（同じ内容に戻す編集）、こだまは捨てられない。"""
    rig = make_echo_rig(ws, index_writer, tmp_path, monkeypatch, fail=1)
    dry = await run_validate(rig.tx, dry_run=True)
    rig.watcher.expect(PAYMENT, dry.files_changed[0].sha256_after)  # 失効していない古い期待（同じ内容）
    result = await run_validate(rig.tx)
    assert (
        result.applied is True and result.files_changed[0].sha256_after == dry.files_changed[0].sha256_after
    )
    assert rig.indexer.calls == []
    await rig.echo({str(PAYMENT)})
    assert rig.indexer.calls == [str(PAYMENT)]


async def test_reindexed_edit_drops_its_echo_even_when_split_into_windows(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = make_echo_rig(ws, index_writer, tmp_path, monkeypatch)
    result = await run_validate(rig.tx)
    assert result.applied is True and not any("reindex failed" in w for w in result.warnings)
    assert rig.indexer.calls == [str(PAYMENT)]
    await rig.echo({str(PAYMENT)}, {str(PAYMENT)})  # os.replace の deleted と added が別々の窓に割れて届いた
    assert rig.indexer.calls == [str(PAYMENT)]  # 二重取り込みしない


async def test_after_the_ttl_an_external_write_of_the_same_content_is_ingested(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = make_echo_rig(ws, index_writer, tmp_path, monkeypatch)
    await run_validate(rig.tx)
    after = (rig.root / PAYMENT).read_bytes()
    await rig.echo({str(PAYMENT)})
    assert rig.indexer.calls == [str(PAYMENT)]
    rig.clock.now += WATCH_EXPECT_TTL_S + 0.001
    (rig.root / PAYMENT).write_bytes(after)  # 外部のプロセスが同じ内容を書いた（保存し直し）
    await rig.echo({str(PAYMENT)})
    assert rig.indexer.calls == [str(PAYMENT), str(PAYMENT)]  # 失効した期待は捨てない


async def test_without_an_indexer_the_echo_is_left_to_the_watcher(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path
):
    watcher = FakeWatcher()
    tx = EditTransaction(
        ws,
        locks=WorkspaceLocks(),
        writer_index=index_writer,
        journal_dir=tmp_path / "cache" / "journal",
        watcher=watcher,
    )
    result = await run_validate(tx)
    assert result.applied is True
    assert watcher.expected == [] and watcher.forgotten == [str(PAYMENT)]


# --- ロック ---------------------------------------------------------------------------------------


async def test_lock_is_held_for_stages_3_to_7_and_released_after_reindex(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path
):
    rig = make_rig(ws, index_writer, tmp_path)
    seen: dict[str, bool] = {}
    rig.tx.stage_hook = lambda name: seen.__setitem__(name, rig.locks.is_locked("poly"))
    await replace_validate(rig)
    assert seen["Resolved"] is False and seen["Planned"] is False
    for held in ("Locked", "PreconditionsVerified", "SyntaxChecked", "Journaled", "Written", "Reindexed"):
        assert seen[held] is True, held
    assert seen["Diagnosed"] is False and seen["Done"] is False


async def test_lock_is_free_while_waiting_for_diagnostics_so_another_edit_proceeds(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path
):
    rig = make_rig(ws, index_writer, tmp_path, lsp=True)
    assert rig.hub is not None
    hub, gate = rig.hub, asyncio.Event()

    def gate_after_write(name: str) -> None:
        # 編集前の基準の待ち（BUG-0098）は即座に揃えて、書き込み後の診断の待ちだけを止める
        if name == "Written" and hub.gate is None:
            hub.gate = gate

    rig.tx.stage_hook = gate_after_write
    first = asyncio.create_task(
        rig.tx.run(replace_body_builder(VALIDATE, NEW_VALIDATE), target=VALIDATE, diagnostics_wait_ms=2000)
    )
    await asyncio.wait_for(hub.blocked.wait(), 5)
    assert rig.locks.is_locked("poly") is False  # 診断待ちはロックの外

    other = TargetRef(path=str(PAYMENT), symbol_name="PaymentProcessor.apply_discount")
    code = "def apply_discount(self, price: Decimal, rate: int) -> Decimal:\n    return price\n"
    second = await asyncio.wait_for(
        rig.tx.run(replace_body_builder(other, code), target=other, diagnostics_wait_ms=0), 5
    )
    assert second.applied is True and "Written" in second.stages
    assert not first.done()

    gate.set()
    done = await asyncio.wait_for(first, 5)
    assert done.applied is True and done.new_diagnostics.status == "complete"
    assert b"return price\n" in read(rig, PAYMENT) and b"return amount < limit" in read(rig, PAYMENT)


async def test_lock_wait_beyond_the_limit_is_a_timeout_and_nothing_changes(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path
):
    rig = make_rig(ws, index_writer, tmp_path, lock_wait_ms=30)
    before = read(rig, PAYMENT)
    async with rig.locks.acquire("poly"):
        with pytest.raises(LuciankeError) as exc:
            await replace_validate(rig)
    assert exc.value.code is ErrorCode.E_TIMEOUT
    assert read(rig, PAYMENT) == before


async def test_edit_ids_increase_and_reindex_failure_is_only_a_warning(rig: Rig):
    class Boom:
        async def index_file(self, rel: RelPath) -> None:
            raise RuntimeError("index down")

    rig.tx._indexer = Boom()  # type: ignore[assignment]
    first = await replace_validate(rig)
    second = await replace_validate(rig, NEW_VALIDATE.replace("amount < limit", "amount <= limit"))
    assert (first.edit_id, second.edit_id) == ("ed_0001", "ed_0002")
    assert first.applied and any("reindex failed" in w for w in first.warnings)


# --- EP-7 / 診断差分 -------------------------------------------------------------------------------


ROUTES = RelPath("src/shop/api/routes.py")


def patch_diagnostics(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    before: dict[RelPath, list[DiagnosticRow]],
    after: dict[RelPath, list[DiagnosticRow]],
) -> list[list[RelPath]]:
    calls: list[list[RelPath]] = []

    async def read_diagnostics(paths):
        calls.append(list(paths))
        source = before if len(calls) == 1 else after
        return {p: list(source.get(p, [])) for p in paths}

    async def referrers(urns, exclude):
        return (ROUTES,), 0

    monkeypatch.setattr(rig.tx, "_read_diagnostics", read_diagnostics)
    monkeypatch.setattr(rig.tx, "_direct_referrers", referrers)
    return calls


def key_of(o: dict) -> tuple:
    return (o["path"], o["source"], o["code"], o["message"], o["enclosing_urn"])


async def test_ep7_complete_delta_matches_the_diagnostics_table(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = make_rig(ws, index_writer, tmp_path, lsp=True)
    a_before, a_after = diag(PAYMENT, "unused variable", line=3), diag(PAYMENT, "unused variable", line=5)
    b = diag(PAYMENT, "possibly unbound", line=10)
    d = diag(PAYMENT, "reportOperatorIssue", line=12, code="Y2")
    c = diag(ROUTES, "import cycle", line=1)
    before = {PAYMENT: [a_before, b], ROUTES: [c]}
    after = {PAYMENT: [a_after, d], ROUTES: []}
    calls = patch_diagnostics(rig, monkeypatch, before, after)

    result = await rig.tx.run(
        replace_body_builder(VALIDATE, NEW_VALIDATE), target=VALIDATE, diagnostics_wait_ms=500
    )
    delta = result.new_diagnostics
    assert delta.status == "complete"
    # 行がずれただけの a は差分にならない
    assert {key_of(o) for o in delta.added} == {(str(PAYMENT), "pyright", "Y2", "reportOperatorIssue", None)}
    assert {key_of(o) for o in delta.resolved} == {
        (str(PAYMENT), "pyright", "X1", "possibly unbound", None),
        (str(ROUTES), "pyright", "X1", "import cycle", None),
    }
    assert [(f["path"], f["fresh"]) for f in delta.checked_files] == [
        (str(PAYMENT), True),
        (str(ROUTES), True),
    ]
    # EP-7: added はすべて現在の診断に含まれ、resolved はいずれも含まれない
    current = {
        (str(r.path), r.source, r.code, r.message, r.enclosing_urn) for rows in after.values() for r in rows
    }
    assert {key_of(o) for o in delta.added} <= current
    assert not ({key_of(o) for o in delta.resolved} & current)
    assert calls[0] == [PAYMENT, ROUTES] and sorted(calls[1]) == sorted([PAYMENT, ROUTES])
    # LSP には、編集の前にいまの本文（版 1。基準の待ち。BUG-0098）、編集の後に新しい版（版 2）と参照元の本文が渡る
    assert rig.documents is not None and rig.documents.versions[str(PAYMENT)] == 2
    assert str(ROUTES) in rig.documents.versions
    assert rig.hub is not None and rig.hub.waits == [
        (str(PAYMENT), 1),
        (str(ROUTES), 1),
        (str(PAYMENT), 2),
        (str(ROUTES), None),
    ]
    assert delta.added[0]["start_line"] == 13 and delta.added[0]["severity"] == "error"


async def test_ep7_delta_waits_for_the_diagnostics_write_without_a_barrier(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """本物の ``DiagnosticsHub`` は、書き込みジョブがコミットしてから待ち手を起こす（BUG-0007）: トランザクションは追加の待ちをしない。"""
    rig = make_rig(ws, index_writer, tmp_path, lsp=True)
    assert rig.documents is not None
    gate = threading.Event()
    old, new = diag(PAYMENT, "old"), diag(PAYMENT, "brand new", line=4)
    db: dict[RelPath, list[DiagnosticRow]] = {PAYMENT: [old]}

    def write_job(_conn: Any) -> None:
        assert gate.wait(10)
        db[PAYMENT] = [new]  # 書き込みのコミット（のつもり）

    armed = False
    hub = DiagnosticsHub(
        ws,
        on_diagnostics=lambda *_: (
            index_writer.submit(write_job, bumps_version=False, label="diag") if armed else None
        ),
    )
    rig.tx._hub = hub

    sync = rig.documents.sync

    def sync_and_publish(path: RelPath, text: str, language_id: str) -> int:
        old = rig.documents.versions.get(str(path)) if rig.documents is not None else None
        version = sync(path, text, language_id)
        if version != old:  # 送ったとき（新しい版）だけ LSP が発行する
            hub.on_publish(
                {"uri": to_file_uri(ws, path), "version": version, "diagnostics": [{"message": "x"}]}
            )
        return version

    monkeypatch.setattr(rig.documents, "sync", sync_and_publish)
    # 編集の前に LSP はいまの内容（版 1）の診断を出し終えている（表は ``old``）。基準の待ち（BUG-0098）は即座に揃う
    sync_and_publish(PAYMENT, read(rig, PAYMENT).decode("utf-8"), "python")
    armed = True

    async def read_diagnostics(paths):
        return {p: list(db.get(p, [])) for p in paths}

    async def no_referrers(urns, exclude):
        return (), 0

    monkeypatch.setattr(rig.tx, "_read_diagnostics", read_diagnostics)
    monkeypatch.setattr(rig.tx, "_direct_referrers", no_referrers)

    waiting = asyncio.Event()
    real_wait_for = hub.wait_for

    async def wait_for(*args: Any, **kwargs: Any) -> bool:
        if kwargs.get("min_version") != 1:  # 書き込み後の待ち（版 2）。編集前の基準の待ちでは放さない
            waiting.set()
        return await real_wait_for(*args, **kwargs)

    monkeypatch.setattr(hub, "wait_for", wait_for)

    async def release() -> None:
        await waiting.wait()  # トランザクションが診断を待ち始めた（書き込みジョブはゲートで止まっている）
        for _ in range(50):  # 待ち手が書き込み前に起きていれば、その間に DB を読んでしまう
            await asyncio.sleep(0)
        gate.set()

    releaser = asyncio.create_task(release())
    try:
        result = await rig.tx.run(
            replace_body_builder(VALIDATE, NEW_VALIDATE), target=VALIDATE, diagnostics_wait_ms=10_000
        )
    finally:
        gate.set()
        await releaser
    delta = result.new_diagnostics
    assert delta.status == "complete"
    assert {key_of(o) for o in delta.added} == {(str(PAYMENT), "pyright", "X1", "brand new", None)}
    assert {key_of(o) for o in delta.resolved} == {(str(PAYMENT), "pyright", "X1", "old", None)}


async def test_delta_is_pending_for_files_the_lsp_has_not_caught_up_on(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = make_rig(ws, index_writer, tmp_path, lsp=True)
    assert rig.hub is not None
    rig.hub.stale = {str(ROUTES)}
    patch_diagnostics(
        rig,
        monkeypatch,
        {PAYMENT: [diag(PAYMENT, "old")], ROUTES: [diag(ROUTES, "stale")]},
        {PAYMENT: [], ROUTES: [diag(ROUTES, "stale")]},
    )
    result = await rig.tx.run(
        replace_body_builder(VALIDATE, NEW_VALIDATE), target=VALIDATE, diagnostics_wait_ms=100
    )
    delta = result.new_diagnostics
    assert delta.status == "pending"
    assert [(f["path"], f["fresh"]) for f in delta.checked_files] == [
        (str(PAYMENT), True),
        (str(ROUTES), False),
    ]
    assert {key_of(o) for o in delta.resolved} == {
        (str(PAYMENT), "pyright", "X1", "old", None)
    }  # 揃った分だけ


async def test_zero_wait_answers_pending_without_waiting(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = make_rig(ws, index_writer, tmp_path, lsp=True)
    patch_diagnostics(rig, monkeypatch, {}, {})
    result = await rig.tx.run(
        replace_body_builder(VALIDATE, NEW_VALIDATE), target=VALIDATE, diagnostics_wait_ms=0
    )
    assert result.new_diagnostics.status == "pending"
    assert rig.hub is not None and rig.hub.waits == []


async def test_without_an_lsp_the_delta_is_the_tree_sitter_error_difference(rig: Rig):
    broken = rig.root / "src/shop/broken.py"
    broken.write_bytes(b"def fine(x):\n    return x\n\n\ndef oops(:\n    pass\n")
    target = TargetRef(path="src/shop/broken.py", symbol_name="fine")
    result = await rig.tx.run(replace_body_builder(target, "def fine(x):\n    return x + 1\n"), target=target)
    delta = result.new_diagnostics
    assert delta.status == "complete" and delta.confidence is Confidence.STRUCTURAL
    assert delta.added == () and delta.resolved == ()  # 壊れ方は変わらない
    assert [f["path"] for f in delta.checked_files] == ["src/shop/broken.py"]


# --- verify.py の単体 ------------------------------------------------------------------------------


def test_verify_identity_ignores_line_numbers_and_diffs_by_key():
    a1, a2 = diag(PAYMENT, "m", line=1), diag(PAYMENT, "m", line=40)
    other = diag(PAYMENT, "n", line=2)
    delta = compute_delta(Snapshot({PAYMENT: (a1, other)}), {PAYMENT: [a2]}, {PAYMENT: True})
    assert (
        delta.status == "complete" and delta.added == () and [r["message"] for r in delta.resolved] == ["n"]
    )


def test_verify_enclosing_urn_is_part_of_the_identity():
    x1, x2 = diag(PAYMENT, "m", urn="lk://poly/a#f"), diag(PAYMENT, "m", urn="lk://poly/a#g")
    delta = compute_delta(Snapshot({PAYMENT: (x1,)}), {PAYMENT: [x2]}, {PAYMENT: True})
    assert len(delta.added) == 1 and len(delta.resolved) == 1


def test_verify_omitted_referrers_become_a_warning():
    delta = compute_delta(Snapshot({PAYMENT: ()}, (), 7), {PAYMENT: []}, {PAYMENT: True})
    assert any("7" in w for w in delta.warnings)


def test_verify_structural_delta_counts_error_nodes():
    good, _ = parse_source(b"def f():\n    return 1\n", "python", "a")
    bad, _ = parse_source(b"def f(:\n    return 1\n", "python", "b")
    broke = structural_delta({PAYMENT: (good, bad)})
    fixed = structural_delta({PAYMENT: (bad, good)})
    assert broke.status == "complete" and len(broke.added) == 1 and broke.resolved == ()
    assert len(fixed.resolved) == 1 and fixed.added == ()
    assert isinstance(broke, DiagDelta)


# --- 編集前の基準（BUG-0098） ------------------------------------------------------------------------
#
# 2 つの「サーバー」（プロセスを持たないハンドル）を本物の ``DocumentRouter`` / ``DocumentSync`` / ``DiagnosticsHub`` で
# 結ぶ。サーバーは ``didOpen`` / ``didChange`` の本文から診断を作って ``publishDiagnostics`` を返し、表（``db``）に
# サーバー名の行として書く。``hold_until`` の版になるまでは発行を溜める（負荷で最初の解析が遅れ、編集の前に診断が届かない
# ときの代わり。順序は保つ）。時間に頼らない（溜めた発行は編集の ``didChange`` で出る）。

UNUSED = RelPath("src/shop/unused_imports.py")
UNUSED_TEXT = "import missing_mod_x\nimport os\n\n\ndef main() -> int:\n    return 1\n"


def pyright_rules(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        if line == "import missing_mod_x":
            out += [
                ("reportMissingImports", 'Import "missing_mod_x" could not be resolved'),
                ("reportUnusedImport", 'Import "missing_mod_x" is not accessed'),
            ]
        elif line == "import os":
            out.append(("reportUnusedImport", 'Import "os" is not accessed'))
    return out


def ruff_rules(text: str) -> list[tuple[str, str]]:
    return [
        ("F401", f"`{line.split()[1]}` imported but unused")
        for line in text.splitlines()
        if line.startswith("import ")
    ]


class StubServer:
    """``DocumentRouter`` が見るハンドルの形をした LSP（プロセスなし）。本文から ``rules`` で診断を作って発行する。"""

    def __init__(
        self, ws: Workspace, name: str, role: str, rules: Any, db: dict[Any, list[DiagnosticRow]],
        *, hold_until: int | None = None,
    ) -> None:  # fmt: skip
        self.ws, self.server_name, self.role, self.language_id, self.defn = ws, name, role, "python", None
        self.rules, self.db, self.hold_until = rules, db, hold_until
        self.held: list[dict[str, Any]] = []
        self.published: list[int] = []
        self.documents = DocumentSync(ws, self._notify)
        self.diagnostics = DiagnosticsHub(ws, source=name, on_diagnostics=self._write)
        self.documents.on_close = self.diagnostics.forget

    @property
    def is_ready(self) -> bool:
        return True

    def _notify(self, method: str, params: Any) -> None:
        if method not in ("textDocument/didOpen", "textDocument/didChange"):
            return
        doc = params["textDocument"]
        text = doc["text"] if method == "textDocument/didOpen" else params["contentChanges"][0]["text"]
        line = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}
        diags = [{"range": line, "code": c, "message": m, "severity": 2} for c, m in self.rules(text)]
        self.held.append({"uri": doc["uri"], "version": doc["version"], "diagnostics": diags})
        if self.hold_until is None or doc["version"] >= self.hold_until:
            self.flush()

    def flush(self) -> None:
        held, self.held = self.held, []
        for params in held:
            self.published.append(params["version"])
            self.diagnostics.on_publish(params)

    def _write(self, path: RelPath, diags: list[dict[str, Any]], version: int | None) -> None:
        self.db[(path, self.server_name)] = [
            diag(path, d["message"], source=self.server_name, code=d["code"]) for d in diags
        ]


class StubPool:
    def __init__(self, *handles: StubServer) -> None:
        self._handles = list(handles)

    def handles(self, ws: Workspace | None = None, language_id: str | None = None) -> list[StubServer]:
        return [h for h in self._handles if language_id is None or h.language_id == language_id]


@dataclass
class TwoServers:
    tx: EditTransaction
    types: StubServer
    lint: StubServer
    db: dict[Any, list[DiagnosticRow]]


def two_servers(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, *, types_hold_until: int | None = None
):
    (ws.root / UNUSED).write_bytes(UNUSED_TEXT.encode("utf-8"))
    db: dict[Any, list[DiagnosticRow]] = {}
    types = StubServer(ws, "basedpyright", "types", pyright_rules, db, hold_until=types_hold_until)
    lint = StubServer(ws, "ruff", "lint", ruff_rules, db)
    router = DocumentRouter(StubPool(types, lint), ws)
    tx = EditTransaction(
        ws,
        locks=WorkspaceLocks(),
        writer_index=index_writer,
        journal_dir=tmp_path / "cache" / "journal",
        indexer=FakeIndexer(),
        watcher=FakeWatcher(),
        documents=router,
        diagnostics_hub=router.diagnostics_hub,
    )

    async def read_db(
        paths: Any,
    ) -> dict[RelPath, list[DiagnosticRow]]:  # 表の現在の行（サーバー名の source）
        return {p: [r for (q, _src), rows in db.items() if q == p for r in rows] for p in paths}

    tx._read_diagnostics = read_db  # type: ignore[method-assign]
    return TwoServers(tx, types, lint, db)


async def remove_import_os(ctx: PlanContext) -> Plan:
    """2 行目の ``import os`` を消す（1 行目の ``import missing_mod_x`` は残す。票の再現と同じ編集）。"""
    return Plan(
        hunks=(Hunk(ctx.path, Range(Position(1, 0), Position(2, 0)), ""),),
        resolved_shas={ctx.path: ctx.sha256},
    )


def keys(objs: Any) -> set[tuple[str, str | None]]:
    return {(o["source"], o["code"]) for o in objs}


async def test_baseline_that_never_arrived_is_not_reported_as_added(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path
):
    """BUG-0098: basedpyright が編集の前にいまの内容の診断を出していない（待っても届かない）と、編集後の basedpyright の診断
    （前からある 1 行目の 2 件）を ``added`` と言わない。``present_after`` に入れ、``partial`` にし、警告でサーバーを名指しする。"""
    rig = two_servers(
        ws, index_writer, tmp_path, types_hold_until=2
    )  # 版 1 の発行は編集の didChange（版 2）まで溜まる
    result = await rig.tx.run(remove_import_os, target=TargetRef(path=str(UNUSED)), diagnostics_wait_ms=300)

    assert (ws.root / UNUSED).read_text(encoding="utf-8").startswith("import missing_mod_x\n\n")
    delta = result.new_diagnostics
    assert delta.status == "partial", delta
    assert delta.added == (), (
        delta.added
    )  # 直す前: basedpyright の reportMissingImports・reportUnusedImport が added
    assert keys(delta.resolved) == {("ruff", "F401")} and "`os`" in delta.resolved[0]["message"]
    assert keys(delta.present_after) == {
        ("basedpyright", "reportMissingImports"),
        ("basedpyright", "reportUnusedImport"),
    }
    assert all("missing_mod_x" in o["message"] for o in delta.present_after)
    assert [dict(c) for c in delta.checked_files] == [
        {"path": str(UNUSED), "fresh": True, "unknown_baseline": ["basedpyright"]}
    ]
    [note] = [w for w in delta.warnings if "basedpyright" in w]
    assert "present_after" in note and "get_diagnostics" in note and str(UNUSED) in note
    assert rig.types.published == [1, 2]  # 版 1 は編集の後に届いた（その間、基準の待ちは時間切れ）


async def test_baseline_that_arrives_within_the_wait_gives_the_normal_delta(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path
):
    """BUG-0098: 編集の前の基準の待ちの間に basedpyright の診断が届けば、通常どおり ``added`` / ``resolved``（``complete``）。"""
    rig = two_servers(ws, index_writer, tmp_path, types_hold_until=1_000)  # 自分では出さない（試験が出す）
    real_sync = rig.types.documents.sync

    def sync_then_publish_soon(path: RelPath, text: str, language_id: str) -> int:
        version = real_sync(path, text, language_id)
        asyncio.get_running_loop().call_later(0.05, rig.types.flush)  # 少し遅れて届く（待ちの上限 10 秒の内）
        return version

    rig.types.documents.sync = sync_then_publish_soon  # type: ignore[method-assign]
    result = await rig.tx.run(
        remove_import_os, target=TargetRef(path=str(UNUSED)), diagnostics_wait_ms=10_000
    )

    delta = result.new_diagnostics
    assert delta.status == "complete", delta
    assert delta.added == () and delta.present_after == ()
    assert keys(delta.resolved) == {("ruff", "F401"), ("basedpyright", "reportUnusedImport")}
    assert all("os" in o["message"] and "missing" not in o["message"] for o in delta.resolved)
    assert [dict(c) for c in delta.checked_files] == [{"path": str(UNUSED), "fresh": True}]
    assert not [w for w in delta.warnings if "present_after" in w]


async def test_baseline_is_not_waited_for_without_a_write(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path
):
    """書かない呼び出し（``dry_run``）と ``diagnostics_wait_ms == 0`` は、編集前の基準を待たない（LSP にも見せない）。"""
    rig = two_servers(ws, index_writer, tmp_path, types_hold_until=1_000)
    started = time.perf_counter()
    dry = await rig.tx.run(
        remove_import_os, target=TargetRef(path=str(UNUSED)), dry_run=True, diagnostics_wait_ms=10_000
    )
    assert dry.new_diagnostics.status == "not_computed" and time.perf_counter() - started < 5
    assert not rig.types.documents.is_open(UNUSED) and not rig.lint.documents.is_open(UNUSED)
    quick = await rig.tx.run(remove_import_os, target=TargetRef(path=str(UNUSED)), diagnostics_wait_ms=0)
    assert quick.applied and quick.new_diagnostics.status == "pending" and time.perf_counter() - started < 5


async def test_a_hub_without_per_server_waits_marks_every_lsp_source_unknown(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``wait_sources`` を持たない口（1 サーバー分のハブ・偽物）: 基準の待ちが揃わなければ、そのファイルの LSP の行はすべて
    基準が不明（``"*"``）。索引の行（``tree-sitter``）は前後で必ず揃うので、ふつうに比べる。"""
    rig = make_rig(ws, index_writer, tmp_path, lsp=True)
    assert rig.hub is not None
    rig.hub.stale_first = {str(PAYMENT)}
    ts_old = diag(PAYMENT, "syntax error near 'x'", source="tree-sitter", code=None)
    patch_diagnostics(
        rig,
        monkeypatch,
        {PAYMENT: [diag(PAYMENT, "old"), ts_old], ROUTES: [diag(ROUTES, "kept")]},
        {PAYMENT: [diag(PAYMENT, "still here"), diag(PAYMENT, "new one")], ROUTES: [diag(ROUTES, "kept")]},
    )
    result = await rig.tx.run(
        replace_body_builder(VALIDATE, NEW_VALIDATE), target=VALIDATE, diagnostics_wait_ms=100
    )
    delta = result.new_diagnostics
    assert delta.status == "partial"
    assert delta.added == () and {o["message"] for o in delta.present_after} == {"still here", "new one"}
    assert [(o["source"], o["message"]) for o in delta.resolved] == [("tree-sitter", "syntax error near 'x'")]
    assert [dict(c) for c in delta.checked_files] == [
        {"path": str(PAYMENT), "fresh": True, "unknown_baseline": ["*"]},
        {"path": str(ROUTES), "fresh": True},
    ]
    assert any(w.startswith(f"{PAYMENT}: LSP は編集前") for w in delta.warnings)


async def test_a_failed_snapshot_reports_nothing_as_added(
    ws: Workspace, index_writer: IndexWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """編集前の診断を取れなかった（表が読めない）ときも、編集後の診断を ``added`` と言わない（基準が不明）。"""
    rig = make_rig(ws, index_writer, tmp_path, lsp=True)
    now = {PAYMENT: [diag(PAYMENT, "was it there?")]}
    calls = patch_diagnostics(rig, monkeypatch, now, now)  # 写しが取れないので、最初の読み出しが書き込み後のもの

    async def broken_referrers(urns: Any, exclude: Any) -> Any:
        raise RuntimeError("db is busy")

    monkeypatch.setattr(rig.tx, "_direct_referrers", broken_referrers)
    result = await rig.tx.run(
        replace_body_builder(VALIDATE, NEW_VALIDATE), target=VALIDATE, diagnostics_wait_ms=100
    )
    delta = result.new_diagnostics
    assert calls == [[PAYMENT]]  # 写しは取れず、書き込み後の 1 回だけ
    assert delta.status == "partial" and delta.added == () and len(delta.present_after) == 1


def test_verify_unknown_baseline_moves_rows_to_present_after_and_skips_resolved():
    tsx = diag(PAYMENT, "syntax", source="tree-sitter", code=None)
    before = Snapshot(
        {PAYMENT: (diag(PAYMENT, "stale", source="basedpyright"), diag(PAYMENT, "gone", source="ruff"), tsx)}
    )
    after = {PAYMENT: [diag(PAYMENT, "now", source="basedpyright")]}
    delta = compute_delta(before, after, {PAYMENT: True}, unknown_baseline={PAYMENT: {"basedpyright"}})
    assert delta.status == "partial"
    assert [o["message"] for o in delta.present_after] == ["now"] and delta.added == ()
    assert sorted(o["message"] for o in delta.resolved) == [
        "gone",
        "syntax",
    ]  # 古い basedpyright の行は「解消」と言わない
    assert delta.checked_files[0]["unknown_baseline"] == ["basedpyright"]
    star = compute_delta(before, after, {PAYMENT: True}, unknown_baseline={PAYMENT: {"*", "ruff"}})
    assert star.checked_files[0]["unknown_baseline"] == ["*"]
    assert [o["message"] for o in star.resolved] == ["syntax"]  # 索引の行だけ比べる


def test_verify_pending_wins_over_partial_and_unknown_is_ignored_for_files_not_caught_up():
    rows = {PAYMENT: [diag(PAYMENT, "a")], ROUTES: [diag(ROUTES, "b")]}
    delta = compute_delta(
        Snapshot(), rows, {PAYMENT: True, ROUTES: False}, unknown_baseline={PAYMENT: {"*"}, ROUTES: {"*"}}
    )
    assert delta.status == "pending"
    assert [dict(c) for c in delta.checked_files] == [
        {"path": str(PAYMENT), "fresh": True, "unknown_baseline": ["*"]},
        {"path": str(ROUTES), "fresh": False},
    ]
    assert [o["message"] for o in delta.present_after] == ["a"]
