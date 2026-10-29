"""BUG-0027: 同じファイルへの T-02 の 2 回目以降は、再解析・再抽出・骨格の作り直しをしない。

速さの絶対値は負荷で揺れるので、時間の閾値では確かめない。**呼び出しの回数**（Tree-sitter の解析・シンボル抽出・骨格の構築）
と、**出力が変わらないこと**（覚えた結果を使っても、毎回作り直した結果と同じ）で確かめる。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from lucianke.app import build_services
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.syntax import skeleton, symbols
from lucianke.syntax.memo import BoundedMemo
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import register_tools

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAY_PY = "src/shop/services/payment.py"
RECEIPT_PY = "src/shop/models/receipt.py"


class Counters:
    def __init__(self) -> None:
        self.extract = 0
        self.build = 0


@pytest.fixture
def counters(monkeypatch: pytest.MonkeyPatch) -> Counters:
    """シンボル抽出の本体と骨格の構築の本体が呼ばれた回数を数える。覚えたものは空にしてから始める。"""
    symbols._EXTRACT_MEMO.clear()
    skeleton._RECORD_MEMO.clear()
    c = Counters()
    real_extract = symbols._extract_uncached
    real_build = skeleton._build_file_record

    def extract(*a: Any, **k: Any) -> Any:
        c.extract += 1
        return real_extract(*a, **k)

    def build(*a: Any, **k: Any) -> Any:
        c.build += 1
        return real_build(*a, **k)

    monkeypatch.setattr(symbols, "_extract_uncached", extract)
    monkeypatch.setattr(skeleton, "_build_file_record", build)
    return c


def make_pipeline(root: Path) -> tuple[Pipeline, TreeCache]:
    registry = ToolRegistry()
    register_tools(registry)
    trees = TreeCache()
    return Pipeline(registry, services=build_services(lambda: [str(root)], trees=trees)), trees


async def skeleton_of(pipeline: Pipeline, fmt: str = "json", **args: Any) -> Any:
    return await pipeline.call_tool("get_file_skeleton", {"format": fmt, "paths": [PAY_PY], "max_tokens": 20000, **args})  # json は data の全項目を数える（BUG-0017）。既定の 2000 では 1 件も入らない


def comparable(r: Any) -> Any:
    """呼び出しごとに変わる値（サーバー内時間）を除いた中身。"""
    if r.structured is None:
        return r.text
    out = dict(r.structured)
    out.pop("timing_ms", None)
    return out


async def test_repeated_skeleton_parses_extracts_and_builds_once(counters: Counters) -> None:
    pipeline, trees = make_pipeline(POLY)
    first = await skeleton_of(pipeline)
    again = [await skeleton_of(pipeline) for _ in range(4)]
    assert not first.is_error
    assert trees.stats().parses == 1  # 構文木は 1 回だけ作る
    assert counters.extract == 1  # シンボル抽出も 1 回だけ
    assert counters.build == 1  # 骨格の構築も 1 回だけ
    assert all(comparable(r) == comparable(first) for r in again)


@pytest.mark.parametrize("fmt", ["compact", "json"])
async def test_reused_result_equals_a_fresh_one(counters: Counters, fmt: str) -> None:
    """覚えた結果は、毎回作り直した結果と同じ（予算に入らず縮退する小さな ``max_tokens`` でも）。"""
    pipeline, _ = make_pipeline(POLY)
    for max_tokens in (2000, 400, 250):
        symbols._EXTRACT_MEMO.clear()
        skeleton._RECORD_MEMO.clear()
        fresh = await skeleton_of(pipeline, fmt, max_tokens=max_tokens)
        reused = await skeleton_of(pipeline, fmt, max_tokens=max_tokens)
        reused2 = await skeleton_of(pipeline, fmt, max_tokens=max_tokens)
        assert not fresh.is_error
        assert comparable(reused) == comparable(fresh) == comparable(reused2)
    assert (
        counters.build == 3
    )  # 予算ごとに 1 回（覚えたものは空にしてから呼んでいる）。2 回目以降は作り直さない


async def test_changed_file_is_reanalyzed(counters: Counters, tmp_path: Path) -> None:
    """内容が変われば（内容の指紋が変わるので）覚えた結果は使わない。"""
    root = tmp_path / "ws"
    shutil.copytree(POLY, root, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    pipeline, trees = make_pipeline(root)
    before = await skeleton_of(pipeline)
    assert "brand_new_function" not in str(before.structured)
    target = root / PAY_PY
    added = "\n\ndef brand_new_function(x: int) -> int:\n    return x\n"
    target.write_text(target.read_text(encoding="utf-8") + added, encoding="utf-8")
    after = await skeleton_of(pipeline)
    assert "brand_new_function" in str(after.structured)
    assert trees.stats().parses == 2
    assert counters.extract == 2
    assert counters.build == 2


async def test_files_with_syntax_errors_are_not_remembered(counters: Counters, tmp_path: Path) -> None:
    """構文エラーのある木（期限で打ち切った不完全な木も同じ）の結果は覚えない。"""
    (tmp_path / "broken.py").write_text(
        "def ok():\n    return 1\n\ndef broken(:\n    pass\n", encoding="utf-8"
    )
    pipeline, _ = make_pipeline(tmp_path)
    for _ in range(2):
        r = await pipeline.call_tool("get_file_skeleton", {"format": "json", "paths": ["broken.py"]})
        assert not r.is_error
    assert counters.extract == 2
    assert counters.build == 2


async def test_second_path_in_a_later_call_is_the_only_new_work(counters: Counters) -> None:
    pipeline, _ = make_pipeline(POLY)
    one = await skeleton_of(pipeline)
    both = await pipeline.call_tool("get_file_skeleton", {"format": "json", "paths": [PAY_PY, RECEIPT_PY], "max_tokens": 20000})
    assert not one.is_error and not both.is_error
    assert counters.extract == 2  # 2 ファイル目だけが新しい
    assert counters.build == 2
    items = both.structured["data"]["items"]
    assert items[0]["path"] == PAY_PY and items[1]["path"] == RECEIPT_PY


def test_memo_is_bounded_by_entries_and_weight() -> None:
    m = BoundedMemo(max_entries=3, max_weight=10)
    for i in range(5):
        m.put(i, f"v{i}")
    assert len(m) == 3 and m.get(0) is None and m.get(4) == "v4"
    m.clear()
    m.put("a", 1, weight=6)
    m.put("b", 2, weight=6)  # 重みの合計が上限を超えるので古い方が落ちる
    assert m.get("a") is None and m.get("b") == 2
    m.put("huge", 3, weight=11)  # 1 件で上限を超えるものは覚えない
    assert m.get("huge") is None and m.get("b") == 2
