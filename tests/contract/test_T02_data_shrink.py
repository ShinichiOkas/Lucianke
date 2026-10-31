"""T-02 のデータファイル（JSON など）の縮退が 1 件ずつの段で、渡された予算をほぼ使い切る（BUG-0084。仕様書 §11 T-02 処理規約 8）。

以前は深さを 1 段ずつ浅くするだけで、package.json（丸ごと 1,679 トークン）が 1007 では 660 で返った（利用率 66%）。
``tmp_path`` に package.json 風のファイルを作り、予算を掃引して、利用率・単調性（§5.6-5）・注の形を確かめる。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.app import build_services
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import register_tools

PATH = "package.json"


def _package_json() -> dict[str, Any]:
    return {
        "name": "demo",
        "version": "1.0.0",
        "scripts": {f"script_{i}": "x" for i in range(30)},
        "dependencies": {f"dep_{i}": "^1.0.0" for i in range(40)},
        "devDependencies": {f"dev_{i}": "^2.0.0" for i in range(40)},
        "config": {f"section_{i}": {f"opt_{j}": j for j in range(6)} for i in range(12)},
        "files": ["a", "b"],
        "license": "MIT",
    }


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    (tmp_path / PATH).write_text(json.dumps(_package_json(), indent=2) + "\n", encoding="utf-8")
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=build_services(lambda: [str(tmp_path)]))


async def _call(pipeline: Pipeline, fmt: str, max_tokens: int) -> Any:
    r = await pipeline.call_tool(
        "get_file_skeleton", {"format": fmt, "paths": [PATH], "max_tokens": max_tokens}
    )
    assert not r.is_error
    return r


def _used(fmt: str, r: Any) -> int:
    if fmt == "json":
        return int(r.structured["budget"]["used_tokens_est"])
    found = re.search(r"~(\d+)/\d+tok", r.text.splitlines()[0])
    assert found is not None
    return int(found.group(1))


async def _whole(pipeline: Pipeline, fmt: str) -> int:
    return _used(fmt, await _call(pipeline, fmt, 100_000))


def _count_json(symbols: list[dict[str, Any]]) -> int:
    return sum(1 + _count_json(s["children"]) for s in symbols)


def _kept(fmt: str, r: Any) -> int:
    if fmt == "json":
        return sum(_count_json(i["symbols"]) for i in r.structured["data"]["items"])
    return sum(1 for ln in r.text.splitlines() if re.match(r"\s*\[Key\]", ln))


@pytest.mark.parametrize("fmt", ["compact", "json"])
async def test_shrunk_data_file_uses_most_of_the_budget(pipeline: Pipeline, fmt: str) -> None:
    whole = await _whole(pipeline, fmt)
    assert whole > 1500
    checked = 0
    for budget in range(whole // 3, whole, whole // 23):
        r = await _call(pipeline, fmt, budget)
        used = _used(fmt, r)
        assert used <= budget
        assert used >= budget * 0.9, (fmt, budget, used)
        checked += 1
    assert checked >= 15


@pytest.mark.parametrize("fmt", ["compact", "json"])
async def test_shrunk_data_file_is_monotone_in_budget(pipeline: Pipeline, fmt: str) -> None:
    whole = await _whole(pipeline, fmt)
    prev = 0
    for budget in range(whole // 4, whole + 60, 37):
        r = await _call(pipeline, fmt, budget)
        kept = _kept(fmt, r)
        assert kept >= prev, (budget, kept, prev)
        prev = kept


async def test_note_names_containers_and_whole_file_budget(pipeline: Pipeline) -> None:
    whole = await _whole(pipeline, "compact")
    r = await _call(pipeline, "compact", whole - 150)
    assert g.validate(r.text) == []
    notes = [ln for ln in r.text.splitlines() if ln.startswith("- truncated:")]
    assert len(notes) == 1
    assert re.fullmatch(
        r"- truncated: \d+ members omitted: .+; whole file: max_tokens>=\d+ with this path alone", notes[0]
    ), notes[0]
    found = re.search(r"max_tokens>=(\d+)", notes[0])
    assert found is not None
    again = await _call(pipeline, "compact", int(found.group(1)))
    assert "truncated:" not in again.text
    j = await _call(pipeline, "json", await _whole(pipeline, "json") - 150)
    item = j.structured["data"]["items"][0]
    assert item["truncated_members"] > 0
    assert sum(c["omitted"] for c in item["truncated_by_container"]) == item["truncated_members"]
    assert "whole file: max_tokens>=" in item["truncated_note"]


@pytest.mark.parametrize("fmt", ["compact", "json"])
async def test_top_level_only_version_still_fits_where_it_did_before(pipeline: Pipeline, fmt: str) -> None:
    """最上位だけの版の注は件数と ``whole file: max_tokens>=M`` だけに縮む。以前（深さ 1 に縮退）に返っていた最小の予算
    （最上位だけ〈max_depth=1〉の大きさ + 注の約 20 トークン）で最上位が返り、1 件も返らない帯を作らない（BUG-0084）。"""
    flat = await pipeline.call_tool(
        "get_file_skeleton", {"format": fmt, "paths": [PATH], "max_tokens": 100_000, "max_depth": 1}
    )
    budget = _used(fmt, flat) + 20
    r = await _call(pipeline, fmt, budget)
    assert _used(fmt, r) <= budget
    top = list(_package_json())
    if fmt == "json":
        item = r.structured["data"]["items"][0]
        assert [s["name"] for s in item["symbols"]] == top
        assert item["truncated_members"] > 0
    else:
        assert [ln.split()[1] for ln in r.text.splitlines() if ln.startswith("[Key]")] == top
        assert re.search(r"- truncated: \d+ members omitted; whole file: max_tokens>=\d+ ", r.text)
