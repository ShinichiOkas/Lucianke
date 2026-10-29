"""T-06 の事後条件 1、T-07 の事後条件 2（``exact`` 水準は LSP の結果と等しい）の契約テスト（BUG-0040）。

土台（実機の basedpyright に ``tests/fixtures/poly`` の複製を読ませる ``real``）は ``test_T06_T07_lsp.py`` のものを使う。
ここは期待値を手で書かず、**テストが LSP に直接聞いた集合**とツールの答えを突き合わせる。basedpyright が無ければ skip。
"""

# ruff: noqa: F811  (フィクスチャ real を引数で受ける)

from __future__ import annotations

from pathlib import Path

import pytest
from test_T06_T07_lsp import (  # noqa: F401  (フィクスチャ real・registry)
    PAY,
    PC,
    Env,
    needs_basedpyright,
    real,
    registry,
)

from lucianke.core.model import Position, RelPath
from lucianke.lsp import features


def process_charge_position(root: Path) -> Position:
    text = (root / PAY).read_text(encoding="utf-8").splitlines()
    line = next(i for i, t in enumerate(text) if "async def process_charge" in t)
    return Position(line, text[line].index("process_charge"))


@needs_basedpyright
@pytest.mark.parametrize("include_declaration", [False, True])
async def test_t06_postcondition_1_exact_references_equal_the_lsp_result(
    real: Env, include_declaration: bool
) -> None:
    """PC:T-06#1 ``exact`` 水準のとき ``references[]`` の ``(path, line, col)`` の集合は、同じ位置への LSP の参照検索の結果
    （``include_declaration`` の設定を反映）と等しい。"""
    handle = real.pool.get("python", real.ws)
    assert handle is not None
    lsp = await features.references(
        handle, RelPath(PAY), process_charge_position(real.root), include_declaration=include_declaration
    )
    want = {(str(x.rel), x.range.start.line + 1, x.range.start.col + 1) for x in lsp}
    assert len(want) >= 3  # 空同士の一致にしない（warm_up で呼び出し 3 件が揃っている）
    r = await real.refs(PC, include_declaration=include_declaration, max_tokens=8000)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    got = {(x["path"], x["line"], x["col"]) for x in r.structured["data"]["references"]}
    assert got == want


@needs_basedpyright
async def test_t07_postcondition_2_exact_incoming_depth1_equals_the_lsp_result(real: Env) -> None:
    """PC:T-07#2 ``exact`` 水準のとき ``depth = 1`` の incoming ノードの集合は、LSP の incoming calls の結果と等しい
    （呼び元の名前・ファイル・呼び出し位置の行で比べる）。"""
    handle = real.pool.get("python", real.ws)
    assert handle is not None
    items = await features.prepare_call_hierarchy(handle, RelPath(PAY), process_charge_position(real.root))
    assert len(items) == 1
    edges = await features.incoming_calls(handle, items[0])
    want = {(e.item.name, str(e.item.rel), r.start.line + 1) for e in edges for r in e.from_ranges}
    assert len(want) >= 3
    r = await real.calls(PC, "incoming", depth=1)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    nodes = r.structured["data"]["nodes"]
    assert {n["depth"] for n in nodes} == {1}
    got = {(n["name"], n["call_site_path"], n["call_site_line"]) for n in nodes}
    assert got == want
