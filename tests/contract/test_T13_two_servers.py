"""T-13 を結線済みの ``RuntimeHub`` / ``WiredServices`` / 実物の ``LspPool`` で通す（BUG-0023 の結線の確認）。

Python のワークスペースで ``basedpyright``（types）と ``ruff``（lint）を両方 ``ready`` にし、ruff の QuickFix（未使用 import の削除）を
適用する。応答の ``new_diagnostics.resolved`` に、ruff の診断だけでなく **basedpyright の診断**（解決できない import・未使用 import）も
入ることを確かめる。``edit_transaction(ws, lsp=handle)`` が 1 サーバー分の口でなく ``DocumentRouter`` を渡し、書き込み後の ``didChange`` と
診断の待ちが両方のサーバーへ届くこと、LSP の診断が PaaD の ``diagnostics`` 表にサーバー名の行として入ること
（ruff は診断に ``"Ruff"`` と書く）が前提。

実機（``.venv`` の ``basedpyright`` と ``ruff``）が無ければ skip。待ちは条件待ち。

前提（BUG-0097）: 編集前の診断の写し（設計書 §7.7.6-1）は ``diagnostics`` 表から取るので、**編集の前に両方のサーバーがその
ファイルの診断を出し終え、表に入っていること** を、試験が合図（各サーバーの ``publishDiagnostics`` の版と中身、表への書き込み）で
待つ。ツールの中の待ち（QuickFix の文脈の診断 ``CONTEXT_DIAGNOSTICS_WAIT_MS`` = 2 秒）に頼ると、負荷で basedpyright の最初の
解析がそれを超えたとき、写しに basedpyright の行が無く ``resolved`` に入らない（票の赤）。製品の期限は変えない。
製品が降格した答え（``new_diagnostics`` が ``pending``・``E_TIMEOUT``）は、新しい作業ツリーとサーバーで最初からやり直して
聞き直す（最大 ``RETRIES`` 回。やり直したことは警告に書く。BUG-0080 と同じ考え）。検査そのものは緩めない。

BUG-0098（編集の前に basedpyright がいまの内容の診断を出していないと、前からある診断を ``added`` と答えた）は、試験の側で
basedpyright の発行をハブに渡す前に止めて／遅らせて（``_lsp_settle.HeldPublishes``。負荷で最初の解析が遅れたときの代わり）確かめる:
待ちの間に届けば通常の差分（``complete``）、届かなければ ``partial`` で ``present_after`` に入れ、``added`` と言わない。
"""

from __future__ import annotations

import os
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _lsp_settle import HeldPublishes, diagnostics_published

from lucianke.core.model import TargetRef
from lucianke.core.procs import ProcessRegistry
from lucianke.edit.builders import replace_body_builder
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
ANALYSIS_WAIT_S = 240.0  # 編集前の診断を待つ上限（条件待ち。届けば即戻る。BUG-0097）
RETRIES = 2  # 製品が降格したときの聞き直し（最初からのやり直し）の回数
# 編集前に各役のサーバーが出しているはずの診断（BEFORE の 1 行目の、未使用で解決できない import）
BEFORE_CODES = {"types": {"reportMissingImports", "reportUnusedImport"}, "lint": {"F401"}}

_TYPES = default_server("python", "types")
_LINT = default_server("python", "lint")


def _cmd(defn: LspServerDef | None) -> list[str] | None:
    return find_command(defn, workspace_root=REPO, venv=REPO / ".venv") if defn is not None else None


needs_both = pytest.mark.skipif(
    _cmd(_TYPES) is None or _cmd(_LINT) is None, reason="basedpyright and ruff are required"
)


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


@asynccontextmanager
async def open_rig(base: Path, text: str = BEFORE) -> AsyncIterator[Rig]:
    root = Path(os.path.realpath(base / "proj"))
    (root / "pkg").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F"]\n', encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / FILE).write_text(text, encoding="utf-8")
    assert _TYPES is not None and _LINT is not None
    procs = ProcessRegistry()
    hub = RuntimeHub(base / "cache", TreeCache())
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


async def _before_published(rig: Rig) -> None:
    """両方のサーバーが ready で、編集前の診断（``BEFORE_CODES``）を出し終え、表に入っている。"""
    for role in ("types", "lint"):
        assert await rig.pool.wait_ready("python", rig.ws, READY_WAIT_MS, role=role), role  # type: ignore[arg-type]
    handles = {h.role: h for h in rig.pool.handles(rig.ws, "python")}
    for role, codes in BEFORE_CODES.items():
        await diagnostics_published(handles[role], FILE, codes, timeout_s=ANALYSIS_WAIT_S)


def _degraded(result: Any) -> str | None:
    """製品が降格した答えなら、その理由（聞き直す）。そうでなければ ``None``（そのまま検査する）。"""
    if result.is_error:
        code = result.structured["error"]["code"]
        return f"{code}: {result.text}" if code == "E_TIMEOUT" else None
    nd = result.structured["data"]["new_diagnostics"]
    return f"new_diagnostics={nd['status']} ({nd})" if nd["status"] == "pending" else None


async def _scenario(rig: Rig) -> tuple[Any, str | None]:
    """編集前の診断を待ち、ruff の QuickFix を選んで適用する。``(適用の応答, 降格の理由)``。"""
    await _before_published(rig)

    # 種別指定は曖昧: 候補は両方のサーバーから出る(ruff の削除と、basedpyright の `# pyright: ignore`)
    listed = await rig.call(path=FILE, line=1, action_kind_or_id="quickfix")
    if (why := _degraded(listed)) is not None:
        return listed, f"listing: {why}"
    assert listed.is_error and listed.structured["error"]["code"] == "E_TARGET_AMBIGUOUS", listed.text
    candidates = listed.structured["error"]["candidates"]
    assert {c["server"] for c in candidates} == {"ruff", "basedpyright"}, candidates
    [fix_id] = [c["id"] for c in candidates if c["server"] == "ruff" and "Remove unused import" in c["title"]]

    applied = await rig.call(path=FILE, line=1, action_kind_or_id=fix_id)
    why = _degraded(applied)
    return applied, None if why is None else f"applying: {why}"


@needs_both
async def test_a_ruff_quickfix_reports_the_basedpyright_diagnostics_it_resolved(tmp_path: Path) -> None:
    tries: list[str] = []
    for attempt in range(1 + RETRIES):
        async with open_rig(tmp_path / f"try{attempt}") as rig:
            applied, why = await _scenario(rig)
            after_text = (rig.root / FILE).read_text(encoding="utf-8")
        if why is None:
            break
        tries.append(f"#{attempt + 1} {why}")
    else:
        pytest.fail(f"the product degraded on every try: {'; '.join(tries)}", pytrace=False)
    if tries:  # 製品の答えが降格したことは隠さない
        warnings.warn(f"T-13 two servers: answered only after a re-ask; {'; '.join(tries)}", stacklevel=1)

    assert not applied.is_error, applied.text
    data = applied.structured["data"]
    assert after_text == AFTER
    assert data["applied"] is True and data["action"]["source"] == "ruff"
    nd = data["new_diagnostics"]
    assert nd["status"] == "complete", nd
    resolved = {(d["source"], d["code"]) for d in nd["resolved"]}
    assert ("ruff", "F401") in resolved, nd
    # BUG-0023: ruff が供給した編集の結果として、basedpyright の診断の解消も同じ応答に入る
    assert ("basedpyright", "reportMissingImports") in resolved, nd
    assert ("basedpyright", "reportUnusedImport") in resolved, nd
    assert nd["added"] == [], nd


# --- BUG-0098: 編集の前の基準 -----------------------------------------------------------------------

# 2 行目の ``import os`` を ruff の QuickFix で消す。1 行目の ``import missing_mod_x`` の basedpyright の 2 件は編集の前からある
KEEP = "import missing_mod_x\nimport os\n\n\ndef main() -> int:\n    return 1\n"
KEEP_AFTER = "import missing_mod_x\n\n\ndef main() -> int:\n    return 1\n"
DELAY_S = 3.0  # 票の再現（basedpyright の配達を 3 秒遅らせる）。QuickFix の文脈の診断の待ち（2 秒）より長い


def _handles(rig: Rig) -> dict[str, Any]:
    return {h.role: h for h in rig.pool.handles(rig.ws, "python")}


async def _ruff_remove_os(rig: Rig) -> str:
    """両方 ready・ruff がいまの内容の F401 を出し終えてから、2 行目の QuickFix の候補から ruff の削除の ID を取る。"""
    for role in ("types", "lint"):
        assert await rig.pool.wait_ready("python", rig.ws, READY_WAIT_MS, role=role), role  # type: ignore[arg-type]
    await diagnostics_published(_handles(rig)["lint"], FILE, {"F401"}, timeout_s=ANALYSIS_WAIT_S)
    listed = await rig.call(path=FILE, line=2, action_kind_or_id="quickfix")
    assert listed.is_error and listed.structured["error"]["code"] == "E_TARGET_AMBIGUOUS", listed.text
    candidates = listed.structured["error"]["candidates"]
    [fix_id] = [
        c["id"]
        for c in candidates
        if c["server"] == "ruff" and "Remove unused import" in c["title"] and "os" in c["title"]
    ]
    return str(fix_id)


@needs_both
async def test_bug0098_a_baseline_that_never_arrived_is_not_reported_as_added(tmp_path: Path) -> None:
    """basedpyright の発行を編集の ``didChange``（版 2）まで止める: 編集の前の基準の待ち（``diagnostics_wait_ms``）は必ず切れる。
    前からある basedpyright の 2 件は ``added`` ではなく ``present_after``、``status`` は ``partial``、警告で basedpyright を名指し。"""
    tries: list[str] = []
    for attempt in range(1 + RETRIES):
        async with open_rig(tmp_path / f"held{attempt}", KEEP) as rig:
            for role in ("types", "lint"):
                assert await rig.pool.wait_ready("python", rig.ws, READY_WAIT_MS, role=role), role  # type: ignore[arg-type]
            with HeldPublishes(_handles(rig)["types"], FILE, until_version=2) as held:
                fix_id = await _ruff_remove_os(rig)
                # 編集の前の待ちは必ずこの長さで切れる。書き込み後は版 2 の解析を同じ長さまで待つ
                applied = await rig.call(
                    path=FILE, line=2, action_kind_or_id=fix_id, diagnostics_wait_ms=4000
                )
                after_text = (rig.root / FILE).read_text(encoding="utf-8")
                received = list(held.received)
        why = _degraded(applied)  # 版 2 の解析が待ちを超えた（負荷）なら pending: 新しい組み立てで聞き直す
        if why is None:
            break
        tries.append(f"#{attempt + 1} {why} (received {received})")
    else:
        pytest.fail(f"the product degraded on every try: {'; '.join(tries)}", pytrace=False)
    if tries:
        warnings.warn(f"T-13 BUG-0098 held: answered only after a re-ask; {'; '.join(tries)}", stacklevel=1)

    assert not applied.is_error, applied.text
    assert after_text == KEEP_AFTER
    nd = applied.structured["data"]["new_diagnostics"]
    assert nd["status"] == "partial", (nd, received)
    # 直す前: basedpyright の reportMissingImports・reportUnusedImport（missing_mod_x）が added に入った
    assert nd["added"] == [], nd
    assert {(d["source"], d["code"]) for d in nd["resolved"]} == {("ruff", "F401")}, nd
    present = {(d["source"], d["code"]) for d in nd["present_after"]}
    assert ("basedpyright", "reportMissingImports") in present, nd
    assert all(
        d["source"] == "basedpyright" and "missing_mod_x" in d["message"] for d in nd["present_after"]
    ), nd
    assert nd["checked_files"] == [{"path": FILE, "fresh": True, "unknown_baseline": ["basedpyright"]}], nd
    warnings_ = applied.structured.get("warnings") or []
    assert any("basedpyright" in w and "present_after" in w for w in warnings_), warnings_


@needs_both
async def test_bug0098_a_baseline_delayed_within_the_wait_gives_the_normal_delta(tmp_path: Path) -> None:
    """票の再現: basedpyright の配達を 3 秒遅らせる（順序は保つ）。編集の前の基準の待ち（上限 10 秒）の間に届くので、
    ``complete`` で ``added`` は空、``resolved`` に ruff の F401 と basedpyright の reportUnusedImport（``os``）。"""
    tries: list[str] = []
    for attempt in range(1 + RETRIES):
        async with open_rig(tmp_path / f"delay{attempt}", KEEP) as rig:
            for role in ("types", "lint"):
                assert await rig.pool.wait_ready("python", rig.ws, READY_WAIT_MS, role=role), role  # type: ignore[arg-type]
            with HeldPublishes(_handles(rig)["types"], FILE, delay_s=DELAY_S) as held:
                fix_id = await _ruff_remove_os(rig)
                applied = await rig.call(
                    path=FILE, line=2, action_kind_or_id=fix_id, diagnostics_wait_ms=10_000
                )
                after_text = (rig.root / FILE).read_text(encoding="utf-8")
                received = list(held.received)
        why = _degraded(applied)
        if why is None:
            break
        tries.append(f"#{attempt + 1} {why} (received {received})")
    else:
        pytest.fail(f"the product degraded on every try: {'; '.join(tries)}", pytrace=False)
    if tries:
        warnings.warn(f"T-13 BUG-0098 delay: answered only after a re-ask; {'; '.join(tries)}", stacklevel=1)

    assert not applied.is_error, applied.text
    assert after_text == KEEP_AFTER
    nd = applied.structured["data"]["new_diagnostics"]
    assert nd["status"] == "complete", (nd, received)
    assert nd["added"] == [] and nd["present_after"] == [], nd  # 直す前（票）: basedpyright の 2 件が added
    resolved = {(d["source"], d["code"]) for d in nd["resolved"]}
    assert resolved == {("ruff", "F401"), ("basedpyright", "reportUnusedImport")}, nd
    assert all("missing_mod_x" not in d["message"] for d in nd["resolved"]), nd


REF_A = "def f() -> int:\n    return 1\n"
REF_B = "from pkg.a import f\n\n\ndef g() -> int:\n    return f()\n"


@needs_both
async def test_bug0098_a_referrer_opened_for_the_baseline_is_rechecked_after_the_edit(tmp_path: Path) -> None:
    """編集の前の基準の待ちは、まだ開いていない直接の参照元（``pkg/b.py``）もその場で LSP に開く。開いた参照元を basedpyright が
    編集の後に診断し直すこと（書き込み後の待ちは「編集の didChange より後の発行」）を、編集トランザクション（T-10 の
    replace_body と同じ口）で確かめる: ``complete``・``b.py`` は fresh・``added`` に ``b.py`` の reportReturnType。"""
    async with open_rig(tmp_path / "ref") as rig:
        (rig.root / "pkg" / "a.py").write_text(REF_A, encoding="utf-8")
        (rig.root / "pkg" / "b.py").write_text(REF_B, encoding="utf-8")
        runtime = rig.hub.runtime()
        assert runtime is not None
        for rel in ("pkg/a.py", "pkg/b.py"):
            await runtime.indexer.index_file(rel)
        for role in ("types", "lint"):
            assert await rig.pool.wait_ready("python", rig.ws, READY_WAIT_MS, role=role), role  # type: ignore[arg-type]
        assert not any(h.documents.is_open("pkg/b.py") for h in rig.pool.handles(rig.ws, "python"))  # type: ignore[arg-type]
        tx = rig.pipeline.services.edit_transaction(rig.ws)  # type: ignore[attr-defined]
        target = TargetRef(path="pkg/a.py", symbol_name="f")
        result = await tx.run(
            replace_body_builder(target, 'def f() -> str:\n    return "x"\n'),
            target=target,
            diagnostics_wait_ms=10_000,
        )
    nd = result.new_diagnostics
    assert nd.status == "complete", nd
    assert [dict(c) for c in nd.checked_files] == [
        {"path": "pkg/a.py", "fresh": True},
        {"path": "pkg/b.py", "fresh": True},
    ], nd
    assert {(o["path"], o["source"], o["code"]) for o in nd.added} == {
        ("pkg/b.py", "basedpyright", "reportReturnType")
    }
    assert nd.present_after == () and nd.resolved == (), nd
