"""T-13 ``apply_code_action`` の契約テスト（仕様書 §11.5 T-13 の事後条件 1〜4、処理規約 1〜7、§10.10 の EP-1〜EP-3）。

パイプライン経由でツールを呼ぶ。``tests/fixtures/poly`` を ``tmp_path`` に写してワークスペースにする（書き込むので原本は触らない）。

* **実機**: ``.venv`` の ``ruff server``（位置は utf-8 を採る）で、未使用 import の QuickFix・``source.organizeImports`` を適用する。
  診断差分は、ハブ（LSP の ``publishDiagnostics`` の受け皿）の現在の診断を ``DiagnosticRow`` にして ``EditTransaction`` に読ませる
  （DB の配線は合成ルートの仕事。ここでは差分の比較そのものと ``target_diagnostic_resolved`` を確かめる）。見つからなければ skip。
* **偽 LSP**: テスト内の Python スクリプト。失敗系（候補なし・曖昧・``command`` のみ・作成を含む編集・タイムアウト・
  ``resolve`` が要るアクション）を確かめ、どの失敗のあともツリーが不変であることを確かめる。
* 子プロセス（LSP とその孫）がテストの後に残らない（``registry.live() == []``）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.app import build_services
from lucianke.core.model import Confidence, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.core.textpos import LineIndex
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.transaction import EditTransaction
from lucianke.index.queries import DiagnosticRow
from lucianke.index.writer import IndexWriter
from lucianke.lsp import features
from lucianke.lsp.action_store import CodeActionStore
from lucianke.lsp.pool import LspHandle, LspPool
from lucianke.lsp.servers import LspServerDef, default_server, find_command
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools._services import ToolServices
from lucianke.tools.t13_apply_code_action import SPEC, _Chosen, _target_resolved  # pyright: ignore[reportPrivateUsage]
from lucianke.workspace.types import Workspace

REPO = Path(__file__).resolve().parents[2]
POLY = REPO / "tests" / "fixtures" / "poly"
FIXTURE = "src/shop/unused.py"
UNUSED_PY = "import os\nimport sys\n\n\ndef main() -> int:\n    return len(sys.argv)\n"
UNSORTED_PY = "import sys\nimport os\n\nprint(os.name, sys.argv)\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- 偽物・土台 ----------------------------------------------------------------------------


@dataclass
class FakeIndexer:
    calls: list[str] = field(default_factory=list)

    async def index_file(self, rel: RelPath) -> None:
        self.calls.append(str(rel))


@dataclass(slots=True, kw_only=True)
class T13Services(ToolServices):
    """``ToolServices`` に T-13 が ``getattr`` で求めるものを足したもの。合成ルートの結線（16a）の代わり。"""

    locks: Any = None
    writer_index: Any = None
    journal_dir: Any = None
    lsp_pool: Any = None
    code_action_store: Any = None
    hub_diagnostics: bool = False
    transactions: list[Any] = field(default_factory=list)

    def edit_transaction(self, ws: Workspace, *, lsp: LspHandle | None = None) -> EditTransaction:
        tx = EditTransaction(
            ws,
            locks=self.locks,
            writer_index=self.writer_index,
            journal_dir=self.journal_dir,
            indexer=FakeIndexer(),
            documents=lsp.documents if lsp is not None else None,
            diagnostics_hub=lsp.diagnostics if lsp is not None else None,
        )
        if self.hub_diagnostics and lsp is not None:
            tx._read_diagnostics = hub_reader(lsp, ws)  # type: ignore[method-assign]  # DB の代わりにハブを読む
        self.transactions.append(tx)
        return tx


def hub_reader(
    handle: LspHandle, ws: Workspace
) -> Callable[..., Awaitable[dict[RelPath, list[DiagnosticRow]]]]:
    """``EditTransaction._read_diagnostics`` の代わり: ハブが持つ現在の診断を、ディスクの現在の内容で ``DiagnosticRow`` にする。"""

    async def read(paths: Any) -> dict[RelPath, list[DiagnosticRow]]:
        out: dict[RelPath, list[DiagnosticRow]] = {}
        for p in paths:
            rows: list[DiagnosticRow] = []
            entry = handle.diagnostics.get(p)
            if entry is not None:
                index = LineIndex((ws.root / p).read_bytes())
                for raw in entry.diagnostics:
                    assert handle.client is not None
                    rows.append(
                        DiagnosticRow(
                            path=p,
                            severity="warning",
                            source=str(raw.get("source") or "ruff").lower(),
                            code=str(raw["code"]) if raw.get("code") is not None else None,
                            range=handle.client.from_lsp_range(index, raw["range"]),
                            message=str(raw["message"]),
                            enclosing_urn=None,
                            confidence=Confidence.EXACT,
                        )
                    )
            out[p] = rows
        return out

    return read


@pytest.fixture
async def registry() -> AsyncIterator[ProcessRegistry]:
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


@pytest.fixture
async def index_writer(tmp_path: Path) -> AsyncIterator[IndexWriter]:
    w = IndexWriter(tmp_path / "cache" / "paad.sqlite")
    try:
        yield w
    finally:
        await asyncio.to_thread(w.close)


@dataclass
class Rig:
    pipeline: Pipeline
    root: Path
    ws: Workspace
    pool: LspPool | None
    store: CodeActionStore
    svc: T13Services

    def path(self, rel: str) -> Path:
        return self.root / rel

    def write(self, rel: str, text: str) -> bytes:
        data = text.encode("utf-8")
        self.path(rel).parent.mkdir(parents=True, exist_ok=True)
        self.path(rel).write_bytes(data)
        return data

    def snapshot(self) -> dict[str, str]:
        return {
            p.relative_to(self.root).as_posix(): sha(p.read_bytes())
            for p in sorted(self.root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        }

    async def call(self, fmt: str = "json", **args: Any) -> Any:
        args.setdefault("diagnostics_wait_ms", 4000)
        return await self.pipeline.call_tool("apply_code_action", {"format": fmt, **args})

    def handle(self, role: str = "lint") -> LspHandle:
        assert self.pool is not None
        [h] = [h for h in self.pool.handles(self.ws) if h.role == role]
        return h


def make_rig(
    root: Path, tmp_path: Path, index_writer: IndexWriter, pool: LspPool | None, *, hub_diagnostics: bool
) -> Rig:
    base = build_services(lambda: [str(root)])
    store = CodeActionStore()
    svc = T13Services(
        roots=base.roots,
        trees=base.trees,
        lines=base.lines,
        locks=WorkspaceLocks(),
        writer_index=index_writer,
        journal_dir=tmp_path / "cache" / "journal",
        lsp_pool=pool,
        code_action_store=store,
        hub_diagnostics=hub_diagnostics,
    )
    registry = ToolRegistry()
    registry.register(SPEC)
    ws = svc.workspace(None)
    return Rig(Pipeline(registry, services=svc), root, ws, pool, store, svc)


def data_of(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.text
    return result.structured["data"]


def error_of(result: Any, code: str) -> dict[str, Any]:
    assert result.is_error, result.text
    err = result.structured["error"]
    assert err["code"] == code, err
    return err


# --- 実機: ruff ---------------------------------------------------------------------------

_RUFF = default_server("python", "lint")
# 実機の ruff は、テストを走らせている Python 環境（uv.lock が固定する dev グループ）のものを使う。``REPO / ".venv"`` だと、
# ``.venv`` の無い作業ツリー（git worktree）で PATH の古い ruff（isPreferred を付けない 0.11 など）に落ちて結果が変わる（BUG-0031）。
_RUFF_CMD = find_command(_RUFF, workspace_root=REPO, venv=Path(sys.prefix)) if _RUFF else None
needs_ruff = pytest.mark.skipif(_RUFF_CMD is None, reason="ruff is not installed")


@pytest.fixture
async def real(registry: ProcessRegistry, tmp_path: Path, index_writer: IndexWriter) -> AsyncIterator[Rig]:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root, ignore=shutil.ignore_patterns("__pycache__"))
    root = Path(os.path.realpath(root))
    (root / "pyproject.toml").write_bytes(b'[tool.ruff.lint]\nselect = ["F", "I"]\n')
    assert _RUFF is not None and _RUFF_CMD is not None
    pool = LspPool(
        registry,
        servers=[_RUFF],
        resolver=lambda defn, w: list(_RUFF_CMD),
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    rig = make_rig(root, tmp_path, index_writer, pool, hub_diagnostics=True)
    yield rig
    await pool.shutdown_all(300)
    assert registry.live() == []


def candidate_id(err: dict[str, Any], needle: str) -> str:
    [one] = [c["id"] for c in err["candidates"] if needle in c["title"]]
    assert one.startswith("qf_")
    return str(one)


async def ambiguous_quickfix(rig: Rig, **kw: Any) -> dict[str, Any]:
    r = await rig.call(path=FIXTURE, line=1, action_kind_or_id="quickfix", **kw)
    return error_of(r, "E_TARGET_AMBIGUOUS")


@needs_ruff
async def test_real_quickfix_by_kind_is_ambiguous_and_lists_candidate_ids(real: Rig) -> None:
    """処理規約 3: 2 件以上は E_TARGET_AMBIGUOUS。候補は {id, title, kind}（preferred が先頭）で、どのファイルも変わらない（事後条件 4）。"""
    real.write(FIXTURE, UNUSED_PY)
    before = real.snapshot()
    err = await ambiguous_quickfix(real)
    assert [c["kind"] for c in err["candidates"]] == ["quickfix", "quickfix"]
    assert "Remove unused import" in err["candidates"][0]["title"] and err["candidates"][0]["is_preferred"], (
        f"ruff={_RUFF_CMD}: {err['candidates']}"  # isPreferred を付けない版の ruff だとタイトル順（Disable が先）になる
    )
    assert "Disable for this line" in err["candidates"][1]["title"]
    assert real.snapshot() == before
    text = (await real.call("compact", path=FIXTURE, line=1, action_kind_or_id="quickfix")).text
    assert g.validate(text) == [] and text.startswith("# apply_code_action error E_TARGET_AMBIGUOUS")
    assert "! candidate: qf_" in text


MULTI_UNUSED_PY = "import os, sys\n\n\ndef main() -> int:\n    return 0\n"


# 同じ 1 行の BOM + CRLF 版（BUG-0041 の台本は両方の版を使っていた）
MULTI_UNUSED_BOM_CRLF_PY = "\ufeff" + MULTI_UNUSED_PY.replace("\n", "\r\n")


@needs_ruff
@pytest.mark.parametrize("text", [MULTI_UNUSED_PY, MULTI_UNUSED_BOM_CRLF_PY], ids=["lf", "bom_crlf"])
async def test_real_quickfix_candidates_are_unique_and_stable_when_one_line_has_two_diagnostics(
    real: Rig, text: str
) -> None:
    """BUG-0041: 1 行に同じ規則の診断が 2 つ（``import os, sys`` の F401 ×2）あっても、候補は同じ ID・タイトルを重ねず、
    メッセージの件数は列挙した候補の数と一致し、何回呼んでも同じ（診断が揃ってから数える）。"""
    real.write(FIXTURE, text)
    before = real.snapshot()
    seen: list[tuple[str, list[tuple[str, str]]]] = []
    for _ in range(4):
        err = await ambiguous_quickfix(real)
        cands = err["candidates"]
        ids = [c["id"] for c in cands]
        keys = [(c["kind"], c["title"]) for c in cands]
        assert len(set(ids)) == len(ids) and len(set(keys)) == len(keys), cands
        assert err["message"].startswith(f"{len(cands)} code actions of kind"), err["message"]
        seen.append((err["message"], [(c["id"], c["title"]) for c in cands]))
    # 呼ぶたびに ID も並びも件数も同じ（台帳は同じ内容に同じ ID を返す）
    assert all(x == seen[0] for x in seen), seen
    assert real.snapshot() == before


@needs_ruff
async def test_real_apply_quickfix_by_id_postconditions_1_2_3(real: Rig) -> None:
    """事後条件 1・2・3（と EP-1）: 未使用 import の QuickFix を ID で適用する。"""
    before_data = real.write(FIXTURE, UNUSED_PY)
    err = await ambiguous_quickfix(real)
    fix_id = candidate_id(err, "Remove unused import")
    before = real.snapshot()

    r = await real.call(
        path=FIXTURE, line=1, action_kind_or_id=fix_id, expected_file_sha256=sha(before_data)[:16]
    )
    d = data_of(r)
    after_data = real.path(FIXTURE).read_bytes()
    expected = b"import sys\n\n\ndef main() -> int:\n    return len(sys.argv)\n"
    assert after_data == expected  # 事後条件 2: LSP の WorkspaceEdit を適用した結果と等しい
    assert d["applied"] is True and d["edit_id"] and d["syntax_check"] == "ok"
    assert (
        d["action"]["id"] == fix_id and d["action"]["source"] == "ruff" and d["action"]["kind"] == "quickfix"
    )
    [change] = d["files_changed"]
    assert (change["path"], change["sha256_before"]) == (FIXTURE, sha(before_data))
    assert change["sha256_after"] == sha(after_data)  # EP-1 / 事後条件 1
    after = real.snapshot()
    assert {p for p in after if after[p] != before.get(p)} == {FIXTURE}  # files_changed 以外は不変
    assert "-import os" in d["diff"]
    nd = d["new_diagnostics"]
    assert nd["status"] == "complete"
    assert any(o["code"] == "F401" and "`os`" in o["message"] for o in nd["resolved"])
    assert d["target_diagnostic_resolved"] is True
    # 事後条件 3: 「直後の get_diagnostics」に相当するハブの診断に、その診断は無い
    entry = real.handle().diagnostics.get(RelPath(FIXTURE))
    assert entry is not None and not any("`os`" in x["message"] for x in entry.diagnostics)


@needs_ruff
async def test_real_compact_output_is_grammatical(real: Rig) -> None:
    real.write(FIXTURE, UNUSED_PY)
    fix_id = candidate_id(await ambiguous_quickfix(real), "Remove unused import")
    r = await real.call("compact", path=FIXTURE, line=1, action_kind_or_id=fix_id)
    assert not r.is_error, r.text
    assert g.validate(r.text) == [], r.text
    lines = r.text.splitlines()
    assert lines[0].startswith("# apply_code_action ok conf=exact lsp=ready rev=WORKING_TREE")
    assert any(
        ln.startswith("applied: true  edit_id=ed_") and f"action={fix_id} kind=quickfix" in ln for ln in lines
    )
    assert "files_changed: src/shop/unused.py (+0 -1)" in " ".join(ln.split("  sha256")[0] for ln in lines)
    assert "syntax_check: ok" in lines
    assert any(
        ln.startswith("new_diagnostics: complete (+0 -1) checked=1 files  target_resolved=true")
        for ln in lines
    )
    assert "resolved:" in lines
    assert any(ln.startswith("W src/shop/unused.py:1:8 [ruff:F401]") for ln in lines), lines


@needs_ruff
async def test_real_noqa_fix_resolves_its_target_and_the_id_is_bound_to_its_line(real: Rig) -> None:
    real.write(FIXTURE, UNUSED_PY)
    err = await ambiguous_quickfix(real)
    noqa_id = candidate_id(err, "Disable for this line")
    # 発行位置（1 行目）と違う行で使うと E_INVALID_ARGUMENT（処理規約: ID の発行元の位置と一致しなければ）
    before = real.snapshot()
    bad = error_of(await real.call(path=FIXTURE, line=2, action_kind_or_id=noqa_id), "E_INVALID_ARGUMENT")
    assert bad["details"]["reason"] == "id_position_mismatch" and bad["details"]["issued_line"] == 1
    assert real.snapshot() == before
    d = data_of(await real.call(path=FIXTURE, line=1, action_kind_or_id=noqa_id))
    assert real.path(FIXTURE).read_text(encoding="utf-8").startswith("import os  # noqa: F401\n")
    assert d["applied"] is True and d["target_diagnostic_resolved"] is True  # noqa で F401(os) は消える


@needs_ruff
async def test_real_organize_imports_by_kind_has_one_candidate(real: Rig) -> None:
    """種別指定で候補が 1 件ならそのまま適用する。ID 指定ではないので target_diagnostic_resolved は null。"""
    real.write(FIXTURE, UNSORTED_PY)
    r = await real.call("compact", path=FIXTURE, line=1, action_kind_or_id="source.organizeImports")
    assert not r.is_error, r.text
    assert g.validate(r.text) == [], r.text
    assert (
        real.path(FIXTURE).read_text(encoding="utf-8")
        == "import os\nimport sys\n\nprint(os.name, sys.argv)\n"
    )
    assert "applied: true  edit_id=ed_" in r.text and "files_changed: src/shop/unused.py (+1 -1)" in r.text
    assert "syntax_check: ok" in r.text and "new_diagnostics: complete" in r.text
    assert "target_resolved" not in r.text
    j = await real.call(path=FIXTURE, line=1, action_kind_or_id="source.organizeImports")
    assert "no-op" in " ".join(j.structured["warnings"])  # 2 回目は恒等編集（バイトを変えない。EP-6 と同じ）
    assert data_of(j)["edit_id"] is None and data_of(j)["target_diagnostic_resolved"] is None


@needs_ruff
async def test_real_dry_run_writes_nothing_and_predicts_the_sha(real: Rig) -> None:
    """EP-2 / 事後条件 4: dry_run は applied=false で、diff と予測の sha256_after を返し、ツリーは不変。"""
    data = real.write(FIXTURE, UNUSED_PY)
    fix_id = candidate_id(await ambiguous_quickfix(real), "Remove unused import")
    before = real.snapshot()
    d = data_of(await real.call(path=FIXTURE, line=1, action_kind_or_id=fix_id, dry_run=True))
    assert d["applied"] is False and d["edit_id"] is None and "-import os" in d["diff"]
    assert d["files_changed"][0]["sha256_after"] == sha(UNUSED_PY.replace("import os\n", "", 1).encode())
    assert d["new_diagnostics"]["status"] == "not_computed" and d["target_diagnostic_resolved"] is None
    assert real.snapshot() == before and real.path(FIXTURE).read_bytes() == data


@needs_ruff
async def test_real_expected_file_sha256_mismatch_is_refused(real: Rig) -> None:
    """EP-3: 現在値と異なる expected_file_sha256 は E_PRECONDITION_FAILED で、ファイルは変わらない。"""
    real.write(FIXTURE, UNUSED_PY)
    fix_id = candidate_id(await ambiguous_quickfix(real), "Remove unused import")
    before = real.snapshot()
    r = await real.call(path=FIXTURE, line=1, action_kind_or_id=fix_id, expected_file_sha256="0" * 64)
    error_of(r, "E_PRECONDITION_FAILED")
    assert real.snapshot() == before


@needs_ruff
async def test_real_id_expires_when_the_file_changes(real: Rig) -> None:
    """処理規約 2: ID の発行後にファイルが変われば E_PRECONDITION_FAILED（code_action_id_expired）。台帳は古いエントリを消さない。"""
    real.write(FIXTURE, UNUSED_PY)
    fix_id = candidate_id(await ambiguous_quickfix(real), "Remove unused import")
    real.write(FIXTURE, UNUSED_PY + "\nX = 1\n")
    before = real.snapshot()
    err = error_of(await real.call(path=FIXTURE, line=1, action_kind_or_id=fix_id), "E_PRECONDITION_FAILED")
    assert err["details"]["reason"] == "code_action_id_expired" and err["details"]["id"] == fix_id
    assert real.snapshot() == before


@needs_ruff
async def test_real_no_candidate_is_not_found_and_changes_nothing(real: Rig) -> None:
    """処理規約 3: 候補 0 件は E_TARGET_NOT_FOUND（事後条件 4）。"""
    real.write(FIXTURE, UNUSED_PY)
    before = real.snapshot()
    err = error_of(await real.call(path=FIXTURE, line=6, action_kind_or_id="quickfix"), "E_TARGET_NOT_FOUND")
    assert err["details"]["reason"] == "no_code_action"
    assert real.snapshot() == before


@needs_ruff
async def test_real_argument_errors(real: Rig) -> None:
    real.write(FIXTURE, UNUSED_PY)
    before = real.snapshot()
    err = error_of(await real.call(path=FIXTURE, line=1, action_kind_or_id="qf_ffff"), "E_TARGET_NOT_FOUND")
    assert err["details"]["reason"] == "unknown_code_action_id"
    error_of(await real.call(path=FIXTURE, line=99, action_kind_or_id="quickfix"), "E_INVALID_ARGUMENT")
    error_of(
        await real.call(path=FIXTURE, line=3, end_line=2, action_kind_or_id="quickfix"), "E_INVALID_ARGUMENT"
    )
    error_of(
        await real.call(path=FIXTURE, line=1, end_line=99, action_kind_or_id="quickfix"), "E_INVALID_ARGUMENT"
    )
    error_of(await real.call(path=FIXTURE, line=0, action_kind_or_id="quickfix"), "E_INVALID_ARGUMENT")
    error_of(
        await real.call(path=FIXTURE, line=1, action_kind_or_id="quickfix", revision="HEAD"),
        "E_INVALID_ARGUMENT",
    )
    error_of(
        await real.call(path="../outside.py", line=1, action_kind_or_id="quickfix"), "E_INVALID_ARGUMENT"
    )
    error_of(
        await real.call(path="src/shop/missing.py", line=1, action_kind_or_id="quickfix"),
        "E_TARGET_NOT_FOUND",
    )
    assert real.snapshot() == before


@needs_ruff
async def test_real_language_without_a_server_is_lsp_unavailable(real: Rig) -> None:
    """精度と降格: LSP の無い言語（structural 以下）は提供しない。E_LSP_UNAVAILABLE。"""
    before = real.snapshot()
    err = error_of(
        await real.call(path="legacy/calc.cob", line=1, action_kind_or_id="quickfix"), "E_LSP_UNAVAILABLE"
    )
    assert err["details"]["reason"] == "no_server"
    assert real.snapshot() == before


async def test_no_lsp_pool_is_lsp_unavailable(tmp_path: Path, index_writer: IndexWriter) -> None:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root, ignore=shutil.ignore_patterns("__pycache__"))
    rig = make_rig(Path(os.path.realpath(root)), tmp_path, index_writer, None, hub_diagnostics=False)
    r = await rig.call(path="src/shop/services/payment.py", line=1, action_kind_or_id="quickfix")
    assert error_of(r, "E_LSP_UNAVAILABLE")["details"]["reason"] == "no_pool"


# --- 診断差分の突き合わせ（事後条件 3 の真偽） -------------------------------------------------


class _Hub:
    def __init__(self, diagnostics: list[dict[str, Any]] | None) -> None:
        self._entry = None if diagnostics is None else type("E", (), {"diagnostics": tuple(diagnostics)})()

    def get(self, _path: Any) -> Any:
        return self._entry


def _result(status: str, resolved: list[dict[str, Any]], applied: bool = True) -> Any:
    from lucianke.edit.transaction import EditResult, FileChange
    from lucianke.edit.verify import DiagDelta

    return EditResult(
        applied=applied, edit_id="ed_0001", files_changed=(FileChange(RelPath("a.py"), "x", "y", 1),), diff="",
        syntax_check="ok", new_diagnostics=DiagDelta(status, (), tuple(resolved)), target_urns_after=(),
        confidence=Confidence.EXACT, warnings=(),
    )  # fmt: skip


def _chosen(hub_diags: list[dict[str, Any]] | None, raw: dict[str, Any] | None, from_id: bool = True) -> Any:
    attrs = {"diagnostics": _Hub(hub_diags), "server_name": "ruff", "language_id": "python", "role": "lint"}
    return _Chosen(type("H", (), attrs)(), {"title": "t"}, "qf_0001", raw, from_id, RelPath("a.py"))


RAW = {"code": "F401", "message": "`os` imported but unused", "source": "Ruff", "range": {}}


def test_target_resolved_follows_the_hub_after_the_edit() -> None:
    assert _target_resolved(_chosen([], RAW), _result("complete", [])) is True
    assert _target_resolved(_chosen([dict(RAW)], RAW), _result("complete", [])) is False
    other = _chosen([{"code": "F401", "message": "`sys` imported but unused", "source": "Ruff"}], RAW)
    assert _target_resolved(other, _result("complete", [])) is True


def test_target_resolved_falls_back_to_the_resolved_list_and_is_null_when_unknown() -> None:
    row = {"path": "a.py", "source": "ruff", "code": "F401", "message": "`os` imported but unused"}
    no_hub = _chosen(None, RAW)
    assert _target_resolved(no_hub, _result("complete", [row])) is True
    assert _target_resolved(no_hub, _result("complete", [])) is False
    assert _target_resolved(_chosen([], RAW), _result("pending", [])) is None  # 揃っていない
    assert _target_resolved(_chosen([], RAW), _result("complete", [], applied=False)) is None
    assert _target_resolved(_chosen([], None), _result("complete", [])) is None  # 診断が分からない
    assert _target_resolved(_chosen([], RAW, from_id=False), _result("complete", [])) is None  # 種別指定


# --- 偽 LSP -------------------------------------------------------------------------------

FAKE_SERVER = r"""
import json
import sys
import time

inp = sys.stdin.buffer
out = sys.stdout.buffer
spec = json.load(open(sys.argv[1], encoding="utf-8"))


def read():
    length = None
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.decode("ascii").partition(":")
        if key.lower() == "content-length":
            length = int(value)
    return json.loads(inp.read(length))


def send(msg):
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()


def log(msg):
    with open(spec["log"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"method": msg.get("method"), "params": msg.get("params")}, ensure_ascii=False) + "\n")


while True:
    msg = read()
    if msg is None:
        break
    method = msg.get("method")
    mid = msg.get("id")
    if method in ("textDocument/didOpen", "textDocument/didChange", "$/cancelRequest"):
        log(msg)
        if method == "textDocument/didOpen":
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": msg["params"]["textDocument"]["uri"], "diagnostics": []}})
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": spec.get("capabilities", {})}})
        continue
    if method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
        continue
    if method == "exit":
        break
    if mid is None or method is None:
        continue
    log(msg)
    rule = spec["responses"].get(method, {"result": None})
    if "sleep" in rule:
        time.sleep(rule["sleep"])
        continue
    send({"jsonrpc": "2.0", "id": mid, "result": rule.get("result")})
"""

FAKE_DEF = LspServerDef(
    language_id="python", name="fake", argv_candidates=(("fake",),), locale="en", requests_m1=frozenset()
)


def rng(sl: int, sc: int, el: int, ec: int) -> dict[str, Any]:
    return {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}}


def replace_return(value: str, path: str = "src/a.py") -> dict[str, Any]:
    """``src/a.py`` の ``return 1`` の ``1`` を ``value`` に換える WorkspaceEdit（changes 形）。"""
    return {"changes": {"{ROOT}/" + path: [{"range": rng(1, 11, 1, 12), "newText": value}]}}


class Fake:
    def __init__(self, rig: Rig, log: Path) -> None:
        self.rig, self.log = rig, log

    def requests(self, method: str) -> list[Any]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [r["params"] for r in rows if r["method"] == method]


FakeFactory = Callable[..., Awaitable[Fake]]


@pytest.fixture
async def fake(
    registry: ProcessRegistry, tmp_path: Path, index_writer: IndexWriter
) -> AsyncIterator[FakeFactory]:
    pools: list[LspPool] = []
    counter = [0]
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")

    async def make(responses: dict[str, Any]) -> Fake:
        counter[0] += 1
        n = counter[0]
        root = Path(os.path.realpath(tmp_path / f"fws{n}"))
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.py").write_bytes(b"def a():\n    return 1\n")
        (root / "src" / "b.py").write_bytes(b"B = 1\n")
        log = tmp_path / f"requests{n}.jsonl"
        spec_text = json.dumps(
            {"log": str(log), "capabilities": {"codeActionProvider": True}, "responses": responses}
        ).replace("{ROOT}", root.as_uri())
        spec_path = tmp_path / f"spec{n}.json"
        spec_path.write_text(spec_text, encoding="utf-8")
        pool = LspPool(
            registry,
            servers=[FAKE_DEF],
            resolver=lambda defn, w: [sys.executable, str(script), str(spec_path)],
            ready_fallback_s=0.2,
            grace_ms=200,
        )
        pools.append(pool)
        return Fake(make_rig(root, tmp_path, index_writer, pool, hub_diagnostics=False), log)

    yield make
    for pool in pools:
        await pool.shutdown_all(200)
    assert registry.live() == []


def action(
    title: str, edit: dict[str, Any] | None = None, kind: str = "refactor.rewrite", **extra: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {"title": title, "kind": kind, **extra}
    if edit is not None:
        out["edit"] = edit
    return out


async def call_fake(f: Fake, **kw: Any) -> Any:
    kw.setdefault("diagnostics_wait_ms", 0)
    return await f.rig.call(path="src/a.py", line=2, **kw)


async def test_fake_single_candidate_applies_and_the_diff_is_the_edit(fake: FakeFactory) -> None:
    """事後条件 1・2: 偽サーバーの WorkspaceEdit を適用した結果と diff が等しく、files_changed 以外は不変。"""
    f = await fake({"textDocument/codeAction": {"result": [action("Return two", replace_return("2"))]}})
    before = f.rig.snapshot()
    d = data_of(await call_fake(f, action_kind_or_id="refactor.rewrite"))
    assert f.rig.path("src/a.py").read_bytes() == b"def a():\n    return 2\n"
    assert d["applied"] is True and d["action"]["title"] == "Return two" and d["action"]["source"] == "fake"
    assert d["files_changed"][0]["sha256_after"] == sha(b"def a():\n    return 2\n")
    assert d["diff"].splitlines()[-2:] == ["-    return 1", "+    return 2"]
    assert d["new_diagnostics"]["status"] == "pending" and d["target_diagnostic_resolved"] is None
    after = f.rig.snapshot()
    assert {p for p in after if after[p] != before[p]} == {"src/a.py"}


async def test_fake_two_candidates_are_ambiguous_then_the_chosen_id_applies(fake: FakeFactory) -> None:
    f = await fake(
        {
            "textDocument/codeAction": {
                "result": [action("Rewrite B", replace_return("3")), action("Rewrite A", replace_return("2"))]
            }
        }
    )
    before = f.rig.snapshot()
    err = error_of(await call_fake(f, action_kind_or_id="refactor.rewrite"), "E_TARGET_AMBIGUOUS")
    assert [c["title"] for c in err["candidates"]] == ["Rewrite A", "Rewrite B"]  # タイトル順（決定的）
    assert f.rig.snapshot() == before
    again = error_of(await call_fake(f, action_kind_or_id="refactor.rewrite"), "E_TARGET_AMBIGUOUS")
    assert [c["id"] for c in again["candidates"]] == [
        c["id"] for c in err["candidates"]
    ]  # 同じ ID（台帳を膨らませない）
    data_of(await call_fake(f, action_kind_or_id=candidate_id(err, "Rewrite B")))
    assert f.rig.path("src/a.py").read_bytes() == b"def a():\n    return 3\n"


async def test_fake_no_candidate_or_wrong_kind_is_not_found(fake: FakeFactory) -> None:
    f = await fake({"textDocument/codeAction": {"result": []}})
    before = f.rig.snapshot()
    error_of(await call_fake(f, action_kind_or_id="refactor.extract"), "E_TARGET_NOT_FOUND")
    g2 = await fake(
        {"textDocument/codeAction": {"result": [action("Other", replace_return("2"), kind="source.fixAll")]}}
    )
    error_of(
        await call_fake(g2, action_kind_or_id="refactor.extract"), "E_TARGET_NOT_FOUND"
    )  # only を無視するサーバー
    assert f.rig.snapshot() == before


async def test_fake_command_only_action_is_unsupported_and_no_command_runs(fake: FakeFactory) -> None:
    """処理規約 4: command だけのアクションは E_UNSUPPORTED。サーバー側で任意コマンドを実行しない（MUST NOT）。"""
    cmd = {
        "title": "Run it",
        "kind": "refactor.rewrite",
        "command": {"title": "Run it", "command": "fake.run"},
    }
    f = await fake({"textDocument/codeAction": {"result": [cmd]}})
    before = f.rig.snapshot()
    err = error_of(await call_fake(f, action_kind_or_id="refactor.rewrite"), "E_UNSUPPORTED")
    assert err["details"]["reason"] == "command_only" and err["details"]["command"] == "fake.run"
    assert f.requests("workspace/executeCommand") == [] and f.rig.snapshot() == before
    bare = {"title": "Bare", "command": "fake.bare", "arguments": []}  # 裸の Command も同じ
    f2 = await fake({"textDocument/codeAction": {"result": [bare]}})
    err2 = error_of(await call_fake(f2, action_kind_or_id="quickfix"), "E_UNSUPPORTED")
    assert err2["details"]["command"] == "fake.bare" and f2.requests("workspace/executeCommand") == []


async def test_fake_create_file_in_the_edit_is_unsupported_and_nothing_is_written(fake: FakeFactory) -> None:
    edit = {
        "documentChanges": [
            {"kind": "create", "uri": "{ROOT}/src/new.py"},
            {
                "textDocument": {"uri": "{ROOT}/src/a.py", "version": None},
                "edits": [{"range": rng(1, 11, 1, 12), "newText": "9"}],
            },
        ]
    }
    f = await fake({"textDocument/codeAction": {"result": [action("Create", edit)]}})
    before = f.rig.snapshot()
    err = error_of(await call_fake(f, action_kind_or_id="refactor.rewrite"), "E_UNSUPPORTED")
    assert err["details"]["reason"] == "resource_operation"
    assert f.rig.snapshot() == before


async def test_fake_edit_to_another_file_changes_only_that_file(fake: FakeFactory) -> None:
    """事後条件 1: files_changed に無いファイルは不変。呼び出しの path と別のファイルだけを書く編集も扱える。"""
    edit = {"changes": {"{ROOT}/src/b.py": [{"range": rng(0, 4, 0, 5), "newText": "2"}]}}
    f = await fake({"textDocument/codeAction": {"result": [action("Bump b", edit)]}})
    before = f.rig.snapshot()
    d = data_of(await call_fake(f, action_kind_or_id="refactor.rewrite"))
    assert [c["path"] for c in d["files_changed"]] == ["src/b.py"]
    after = f.rig.snapshot()
    assert {p for p in after if after[p] != before[p]} == {"src/b.py"}
    assert f.rig.path("src/b.py").read_bytes() == b"B = 2\n"


async def test_fake_action_without_edit_is_resolved(fake: FakeFactory) -> None:
    """処理規約 4: edit が無ければ codeAction/resolve で補う（features.code_actions が行う）。"""
    lazy = {"title": "Lazy", "kind": "refactor.rewrite", "data": {"n": 1}}
    resolved = {**lazy, "edit": replace_return("5")}
    f = await fake(
        {"textDocument/codeAction": {"result": [lazy]}, "codeAction/resolve": {"result": resolved}}
    )
    data_of(await call_fake(f, action_kind_or_id="refactor.rewrite"))
    assert f.rig.path("src/a.py").read_bytes() == b"def a():\n    return 5\n"
    assert len(f.requests("codeAction/resolve")) == 1


async def test_fake_slow_code_action_is_a_timeout(fake: FakeFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    f = await fake({"textDocument/codeAction": {"sleep": 30}})
    monkeypatch.setattr(features, "LSP_TIMEOUT_CODE_ACTION_MS", 300)
    before = f.rig.snapshot()
    err = error_of(await call_fake(f, action_kind_or_id="refactor.rewrite"), "E_TIMEOUT")
    assert err["details"]["reason"] == "code_action_timeout"
    assert f.rig.snapshot() == before


async def test_fake_the_lsp_sees_the_new_content_after_the_write(fake: FakeFactory) -> None:
    """適用後の didChange: 偽サーバーは新しい内容を受け取る（段 7 の再索引）。"""
    f = await fake({"textDocument/codeAction": {"result": [action("Return two", replace_return("2"))]}})
    data_of(await call_fake(f, action_kind_or_id="refactor.rewrite"))
    texts = [c["contentChanges"][0]["text"] for c in f.requests("textDocument/didChange")]
    assert texts and texts[-1] == "def a():\n    return 2\n"
