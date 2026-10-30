"""``tools/list`` のスキーマの大きさの上限（23a）。ホストはセッションごとに ``tools/list`` を丸ごと読むので、固定費になる。

* 既定の一覧（M1 の 14 本。M2・M3 の入口は載せない）の合計が約 6,000 トークン（o200k_base）を超えたら落ちる。
* 1 本が約 600 トークンを超えても落ちる（1 本だけが太るのを防ぐ）。
* M2・M3 の入口は既定の一覧に出ない。

tiktoken は製品の依存に入れないので、``core/tokens.py`` の推定器で数える。推定器は JSON スキーマ（記号が多い）で
o200k_base より約 13% 少なく数えるため、実測の比 ``O200K_PER_ESTIMATE`` を掛けて o200k_base 相当にする
（2026-10-29 の実測: o200k_base 5,975 ÷ 推定器 5,173 = 1.155。``bench/schema_tokens.py`` が o200k_base の値を出す。
推定器の係数を変えたらこの比を測り直す）。落ちたら、説明文と入力スキーマの ``description`` を削る
（引数・型・必須・enum・既定値・範囲は契約テストが見ているので変えない）。
"""

from __future__ import annotations

import json

import pytest

from lucianke import tools
from lucianke.core.tokens import TokenEstimator
from lucianke.mcp.registry import ToolRegistry

O200K_PER_ESTIMATE = 1.155
LIST_BUDGET_O200K = 6000  # 既定の tools/list（M1 の全ツール）の合計
TOOL_BUDGET_O200K = 600  # 1 本あたり


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    tools.register_tools(reg)
    return reg


def _o200k_estimate(listing: object) -> int:
    return round(TokenEstimator().count(json.dumps(listing, ensure_ascii=False)) * O200K_PER_ESTIMATE)


def _entry(d) -> dict[str, object]:  # noqa: ANN001
    return {"name": d.name, "description": d.description, "inputSchema": d.input_schema}


def test_default_listing_has_only_the_current_milestone_tools(registry: ToolRegistry) -> None:
    names = {d.name for d in registry.descriptors(include_planned=False)}
    assert names == {s.name for s in registry if s.milestone == "M1"}
    assert len(names) < len(registry)  # M2・M3 の入口が（少なくとも 1 本）除かれている


def test_default_tools_list_fits_the_budget(registry: ToolRegistry) -> None:
    listing = [_entry(d) for d in registry.descriptors(include_planned=False)]
    total = _o200k_estimate(listing)
    per_tool = {e["name"]: _o200k_estimate(e) for e in listing}
    assert total <= LIST_BUDGET_O200K, (
        f"tools/list is ~{total} tokens (o200k_base eq.), budget {LIST_BUDGET_O200K}; "
        f"largest: {sorted(per_tool.items(), key=lambda kv: -kv[1])[:4]}"
    )


def test_no_single_tool_schema_is_bloated(registry: ToolRegistry) -> None:
    for d in registry.descriptors(include_planned=False):
        size = _o200k_estimate(_entry(d))
        assert size <= TOOL_BUDGET_O200K, f"{d.name}: ~{size} tokens (o200k_base eq.), budget {TOOL_BUDGET_O200K}"
