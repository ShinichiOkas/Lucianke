"""T-11 ``rename_symbol`` の契約テスト、実機の側（仕様書 §11.5 T-11 の事後条件 1〜4、§10.10 の EP-2・EP-3）。

* 実機: ``.venv`` の basedpyright に ``tests/fixtures/poly``（``tmp_path`` に複製）を読ませ、``Pipeline`` 経由でツールを呼ぶ。
  見つからなければ skip。位置の単位は UTF-16（絵文字・日本語の行で確かめる）。
* 冷間の対策は BUG-0012 と同じ（``_t11_harness.warm_up``）: ``ready`` のあとで対象の文書を開き、最初の診断が届くのを待つ。
  新しく作るファイルも、開いて最初の診断を待ってから rename する（basedpyright は ``didOpen`` か
  ``workspace/didChangeWatchedFiles`` を受けるまで、作業ツリーの新規ファイルを rename に含めない）。
* 事後条件 3 は「LSP の参照件数 + 1」で確かめる（仕様書の「T-06 の参照件数 + 1」は、basedpyright が
  ``tests/test_payment.py`` の呼び出しを参照にも rename にも含めない分だけ合わない。T-06 と T-11 は同じ LSP の答えを使う）。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest
from _t11_harness import (
    BASEDPYRIGHT,
    BASEDPYRIGHT_CMD,
    PAY,
    POLY,
    PROCESS_CHARGE,
    PY_FILES,
    ROUTES,
    RST,
    TESTS,
    WS,
    Rig,
    U,
    build_rig,
    copy_poly,
    index_writer_fixture,  # noqa: F401  (フィクスチャ index_writer)
    needs_basedpyright,
    procs_fixture,  # noqa: F401  (フィクスチャ procs)
    snapshot,
    warm_up,
)

from lucianke.core.model import RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.index.writer import IndexWriter
from lucianke.lsp import features
from lucianke.lsp.pool import LspPool
from lucianke.tools._services import analyze_file
from lucianke.workspace.types import PythonEnv, Workspace


async def start_real(
    tmp_path: Path, procs: ProcessRegistry, index_writer: IndexWriter, *, pyright_config: str | None = None
) -> AsyncIterator[Rig]:
    """複製した ``poly`` に basedpyright を起こし、``PY_FILES`` の最初の診断が届いてから ``Rig`` を渡す。"""
    root = copy_poly(tmp_path)
    if pyright_config is not None:
        (root / "pyrightconfig.json").write_text(pyright_config, encoding="utf-8")
    ws = Workspace(workspace_id=WS, root=root, python_env=PythonEnv(interpreter=sys.executable))
    pool = LspPool(
        procs,
        servers=[BASEDPYRIGHT],
        resolver=lambda defn, w: list(BASEDPYRIGHT_CMD),
        init_timeout_ms=60_000,
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    try:
        await pool.ensure_started("python", ws)
        assert await pool.wait_ready("python", ws, 60_000), pool.status("python", ws)
        handle = pool.get("python", ws)
        assert handle is not None
        await warm_up(handle, ws, PY_FILES)
        yield build_rig(tmp_path, pool, handle, ws, index_writer)
    finally:
        await pool.shutdown_all(300)
    assert procs.live() == []


@pytest.fixture
async def real(tmp_path: Path, procs: ProcessRegistry, index_writer: IndexWriter) -> AsyncIterator[Rig]:
    async for rig in start_real(tmp_path, procs, index_writer):
        yield rig


@pytest.fixture
async def real_src_root(
    tmp_path: Path, procs: ProcessRegistry, index_writer: IndexWriter
) -> AsyncIterator[Rig]:
    """``src`` を import の根に足した設定つき。``tests/test_payment.py`` から ``shop.*`` が解決される。"""
    async for rig in start_real(tmp_path, procs, index_writer, pyright_config='{"extraPaths": ["src"]}'):
        yield rig


# --- 実機: basedpyright -----------------------------------------------------------------------


async def symbol_urns(rig: Rig, paths: list[str]) -> set[str]:
    out: set[str] = set()
    for p in paths:
        out |= {str(s.urn) for s in (await analyze_file(rig.svc, rig.ws, p)).symbols}
    return out


@needs_basedpyright
async def test_real_rename_method_postconditions(real: Rig):
    before = snapshot(real.root)
    urns_before = await symbol_urns(real, PY_FILES)
    uses = await features.references(
        real.handle, RelPath(ROUTES), real.pos(ROUTES, "process_charge"), include_declaration=False
    )

    r = await real.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert not r.is_error, r.text
    data = r.structured["data"]
    assert (
        data["applied"] is True
        and data["old_name"] == "process_charge"
        and data["new_name"] == "charge_order"
    )

    after = snapshot(real.root)
    changed = {c["path"] for c in data["files_changed"]}
    assert changed and PAY in changed and ROUTES in changed
    # 事後条件 1: files_changed の sha256_after はディスクと一致し、それ以外のファイルは変わらない
    for c in data["files_changed"]:
        assert after[c["path"]] == c["sha256_after"] and before[c["path"]] == c["sha256_before"]
    assert {p: s for p, s in after.items() if p not in changed} == {
        p: s for p, s in before.items() if p not in changed
    }
    # 事後条件 2: urn_map のキーは呼び出し前にあって後に無く、値は後にある
    urns_after = await symbol_urns(real, PY_FILES)
    assert data["urn_map"] == {U(PAY, PROCESS_CHARGE): U(PAY, "PaymentProcessor.charge_order")}
    for old, new in data["urn_map"].items():
        assert old in urns_before and old not in urns_after and new in urns_after
    # 事後条件 3: edits_count は LSP の参照件数 + 定義 1
    assert data["edits_count"] == len(uses) + 1 == len(data["edits"])
    # 同名の別物（ほかの語・ほかのスコープ）を巻き込まず、旧名はコードから消えた
    for p in PY_FILES:
        assert "process_charge" not in real.read(p) or p == TESTS
    assert "await processor.charge_order(req)" in real.read(ROUTES)
    assert real.read(RST) == (POLY / RST).read_text(encoding="utf-8")  # 文書は触らない
    first = data["edits"][0]
    assert (first["path"], first["use"]) == (PAY, "def") and first["snippet_after"].startswith(
        "async def charge_order("
    )
    assert {e["use"] for e in data["edits"][1:]} == {"call"}
    assert all(e["enclosing_urn"] for e in data["edits"][1:] if e["path"] == ROUTES)
    assert data["syntax_check"] == "ok"


@needs_basedpyright
async def test_real_rename_class_maps_its_members(real: Rig):
    target = {"urn": U(PAY, "PaymentProcessor")}
    dry = await real.rename(target, "Processor", dry_run=True)
    assert not dry.is_error, dry.text
    urn_map = dry.structured["data"]["urn_map"]
    # 改名したクラスのメンバ（メソッド）も旧 URN → 新 URN に入る（処理規約 5）
    assert urn_map[U(PAY, "PaymentProcessor")] == U(PAY, "Processor")
    assert urn_map[U(PAY, PROCESS_CHARGE)] == U(PAY, "Processor.process_charge")
    assert len(urn_map) >= 3 and all(v.startswith(f"lk://{WS}/{PAY}#Processor") for v in urn_map.values())

    r = await real.rename(target, "Processor", fmt="compact")
    assert not r.is_error, r.text
    assert g.validate(r.text) == [], g.validate(r.text)
    assert (
        "applied: true" in r.text
        and f"urn_map: {U(PAY, 'PaymentProcessor')} -> {U(PAY, 'Processor')}" in r.text
    )
    assert "class Processor:" in real.read(PAY) and "Processor(LimitTable())" in real.read(ROUTES)
    after = await symbol_urns(real, PY_FILES)
    assert all(v in after for v in urn_map.values()) and not set(urn_map) & after


@needs_basedpyright
async def test_real_rename_on_a_line_with_non_ascii_text_uses_utf16_columns(real: Rig):
    """絵文字（UTF-16 で 2 単位）と日本語の識別子・文字列の後ろにある参照も正しい列で書き換える。"""
    await real.write(
        "src/shop/jp.py",
        "from shop.services.payment import PaymentProcessor, PaymentRequest\n"
        "\n"
        "\n"
        "async def go(req: PaymentRequest):\n"
        "    p = PaymentProcessor(None)\n"
        '    s = "😀日本語😀"; r = await p.process_charge(req)  # process_charge\n'
        "    データ = await p.process_charge(req)\n"
        "    return s, r, データ\n",
    )
    r = await real.rename({"urn": U(PAY, PROCESS_CHARGE)}, "請求する")
    assert not r.is_error, r.text
    text = real.read("src/shop/jp.py")
    assert '    s = "😀日本語😀"; r = await p.請求する(req)  # process_charge\n' in text  # コメントは触らない
    assert "    データ = await p.請求する(req)\n" in text
    sites = [e for e in r.structured["data"]["edits"] if e["path"] == "src/shop/jp.py"]
    assert [(e["line"], e["character"]) for e in sites] == [
        (6, text.split("\n")[5].index("請求する") + 1),
        (7, text.split("\n")[6].index("請求する") + 1),
    ]
    assert real.read(PAY).count("請求する") == 1 and "process_charge" not in real.read(PAY)


@needs_basedpyright
async def test_real_rename_a_local_by_position_leaves_the_same_name_in_other_scopes(real: Rig):
    await real.write(
        "src/shop/scopes.py",
        "def first(items):\n"
        "    total = 0\n"
        "    for i in items:\n"
        "        total += i  # total of first\n"
        '    return total, "total"\n'
        "\n"
        "\n"
        "def second(items):\n"
        "    total = 1\n"
        "    return total + len(items)\n",
    )
    r = await real.rename({"path": "src/shop/scopes.py", "line": 2, "character": 5}, "subtotal")
    assert not r.is_error, r.text
    assert real.read("src/shop/scopes.py") == (
        "def first(items):\n"
        "    subtotal = 0\n"
        "    for i in items:\n"
        "        subtotal += i  # total of first\n"
        '    return subtotal, "total"\n'
        "\n"
        "\n"
        "def second(items):\n"
        "    total = 1\n"
        "    return total + len(items)\n"
    )
    assert r.structured["data"]["edits_count"] == 3


@needs_basedpyright
async def test_real_dry_run_and_stale_expected_sha_write_nothing(real: Rig):
    before = snapshot(real.root)
    target = {"urn": U(PAY, PROCESS_CHARGE)}
    dry = await real.rename(target, "charge_order", dry_run=True)
    assert not dry.is_error, dry.text
    d = dry.structured["data"]
    assert d["applied"] is False and d["edit_id"] is None and d["edits_count"] == 3 and d["urn_map"]
    assert "charge_order" in d["diff"] and snapshot(real.root) == before  # EP-2

    stale = await real.rename(target, "charge_order", expected_file_sha256s={ROUTES: "0" * 64})
    assert stale.is_error and stale.structured["error"]["code"] == "E_PRECONDITION_FAILED"  # EP-3
    assert snapshot(real.root) == before

    ok = await real.rename(
        target, "charge_order", expected_file_sha256s={ROUTES: before[ROUTES][:12], PAY: before[PAY]}
    )
    assert not ok.is_error and ok.structured["data"]["applied"] is True


@needs_basedpyright
async def test_real_rename_argument_errors(real: Rig):
    before = snapshot(real.root)
    target = {"urn": U(PAY, PROCESS_CHARGE)}

    async def code(new_name: str, tgt: dict[str, Any] = target, **kw: Any) -> str:
        r = await real.rename(tgt, new_name, **kw)
        assert r.is_error, r.text
        return r.structured["error"]["code"]

    assert await code("process_charge") == "E_INVALID_ARGUMENT"  # 同じ名前
    assert await code("class") == "E_INVALID_ARGUMENT"  # 予約語
    assert await code("None") == "E_INVALID_ARGUMENT"
    assert await code("1abc") == "E_INVALID_ARGUMENT"
    assert await code("a b") == "E_INVALID_ARGUMENT"
    assert await code("") == "E_INVALID_ARGUMENT"
    assert await code("x", revision="HEAD") == "E_INVALID_ARGUMENT"
    assert await code("x", {"path": PAY, "symbol_name": "NoSuchThing"}) == "E_TARGET_NOT_FOUND"
    assert await code("x", {"path": PAY}) == "E_INVALID_ARGUMENT"  # パスだけではシンボルが決まらない
    assert await code("x", {"path": RST, "symbol_name": "process_charge"}) == "E_UNSUPPORTED"  # Tier 3
    assert await code("x", {"path": "src/shop/nothing.py", "symbol_name": "a"}) == "E_TARGET_NOT_FOUND"
    assert snapshot(real.root) == before


@needs_basedpyright
async def test_real_rename_reaches_the_test_module_when_its_imports_resolve(real_src_root: Rig):
    """``tests/test_payment.py`` から ``shop.*`` が解決される設定なら、LSP の参照にも rename にも 3 ファイル目が入る。

    ``poly`` を設定なしで開くと、``tests/`` からは ``shop`` が解決されず（``p`` が Unknown）呼び出しは参照に含まれない。
    これは LSP の答えで、T-06 と T-11 は同じ答えを使う。事後条件 3 は「LSP の参照件数 + 1」。
    """
    rig = real_src_root
    uses = await features.references(
        rig.handle, RelPath(ROUTES), rig.pos(ROUTES, "process_charge"), include_declaration=False
    )
    assert {str(u.rel) for u in uses} == {ROUTES, TESTS}
    r = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert not r.is_error, r.text
    data = r.structured["data"]
    assert data["applied"] is True and data["edits_count"] == len(uses) + 1 == 4
    assert {c["path"] for c in data["files_changed"]} == {PAY, ROUTES, TESTS}
    assert "await p.charge_order(PaymentRequest(" in rig.read(TESTS) and "process_charge" not in rig.read(
        TESTS
    )
    assert {e["path"] for e in data["edits"]} == {PAY, ROUTES, TESTS}
