"""T-05 ``get_symbol_bodies`` の契約テスト（仕様書 §11 T-05 の事後条件 1・2・5 ほか）。

パイプライン経由でツールを呼ぶ。ルートは ``tests/fixtures/poly``。構文木だけ（``structural``）の縦切りなので、
セレクタ（``xpath:`` など）・``cell:<n>``（事後条件 3）・``lexical_fallback`` の窓（事後条件 4）は対象外。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.app import build_services
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import register_tools

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAY_PY = "src/shop/services/payment.py"
PAY_TS = "web/src/payment.ts"
WS = "poly"


@pytest.fixture
def pipeline() -> Pipeline:
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=build_services(lambda: [str(POLY)]))


async def call(pipeline: Pipeline, fmt: str = "compact", **args: Any):
    return await pipeline.call_tool("get_symbol_bodies", {"format": fmt, **args})


def source_lines(path: str) -> list[str]:
    return (POLY / path).read_text(encoding="utf-8").splitlines()


async def test_tools_list_has_t05(pipeline: Pipeline) -> None:
    assert "get_symbol_bodies" in [d.name for d in pipeline.registry.descriptors()]


@pytest.mark.parametrize(
    "ref",
    [
        {"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"},
        {"path": PAY_PY, "symbol_name": "process_charge"},  # 末尾一致
        {"path": PAY_PY, "line": 45},  # その行を含む最も内側のシンボル
        {"urn": f"lk://{WS}/{PAY_PY}#PaymentProcessor.process_charge"},
    ],
)
async def test_target_forms_resolve_to_the_same_method(pipeline: Pipeline, ref: dict[str, Any]) -> None:
    r = await call(pipeline, "json", targets=[ref])
    assert not r.is_error, r.text
    item = r.structured["data"]["items"][0]
    assert item["ok"] and item["symbol"]["qualified_name"] == "PaymentProcessor.process_charge"
    assert item["symbol"]["kind"] == "Method"


async def test_postcondition_1_code_lines_equal_file_lines(pipeline: Pipeline) -> None:
    """事後条件 1: code の各行は、range_returned.start_line〜end_line の対象ファイルの各行と順に一致する。"""
    targets = [
        {"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"},
        {"path": PAY_PY, "symbol_name": "PaymentRequest"},
        {"path": PAY_TS, "symbol_name": "PaymentClient"},
    ]
    r = await call(pipeline, "json", targets=targets)
    assert not r.is_error
    for t, item in zip(targets, r.structured["data"]["items"], strict=True):
        rr = item["range_returned"]
        expected = source_lines(t["path"])[rr["start_line"] - 1 : rr["end_line"]]
        assert item["code"].split("\n") == expected, t
        assert len(item["file_sha256"]) == 64 and item["confidence_level"] == "structural"


async def test_postcondition_2_line_numbers_are_consecutive(pipeline: Pipeline) -> None:
    """事後条件 2: with_line_numbers: true のとき、各行の @L<n> は range_returned 内の連番。"""
    r = await call(pipeline, targets=[{"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"}])
    assert g.validate(r.text) == []
    numbers = [int(m.group(1)) for m in re.finditer(r"^@L(\d+): ", r.text, re.M)]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers))) and len(numbers) >= 10
    assert "  sha: " in r.text
    j = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"}])
    rr = j.structured["data"]["items"][0]["range_returned"]
    assert (numbers[0], numbers[-1]) == (rr["start_line"], rr["end_line"])


async def test_without_line_numbers(pipeline: Pipeline) -> None:
    r = await call(pipeline, targets=[{"path": PAY_PY, "symbol_name": "PaymentRequest"}], with_line_numbers=False)
    assert "@L" in r.text.splitlines()[1] and not re.search(r"^@L\d+: ", r.text, re.M)
    assert g.validate(r.text) == []


async def test_decorators_and_context_lines(pipeline: Pipeline) -> None:
    """範囲は宣言ノード + デコレータ・直前のコメント + context_lines。context_lines=0 なら前置きなし。"""
    base = {"path": PAY_PY, "symbol_name": "PaymentRequest"}
    r0 = await call(pipeline, "json", targets=[base], context_lines=0)
    r2 = await call(pipeline, "json", targets=[base], context_lines=2)
    i0, i2 = (x.structured["data"]["items"][0] for x in (r0, r2))
    assert i0["context_before_lines"] == 0
    assert i2["range_returned"]["end_line"] == i0["range_returned"]["end_line"]
    assert i2["range_returned"]["start_line"] == i0["range_returned"]["start_line"] - i2["context_before_lines"]
    assert i0["code"].lstrip().startswith("@dataclass")  # デコレータを含む


async def test_line_range_is_clamped_to_the_body(pipeline: Pipeline) -> None:
    ref = {"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"}
    full = (await call(pipeline, "json", targets=[ref], context_lines=0)).structured["data"]["items"][0]
    start = full["range_returned"]["start_line"]
    part = await call(pipeline, "json", targets=[ref], line_range={"start": 1, "end": start + 2})
    item = part.structured["data"]["items"][0]
    assert item["range_returned"] == {"start_line": start, "end_line": start + 2}
    assert len(item["code"].split("\n")) == 3


async def test_ambiguous_symbol_name_lists_candidates(pipeline: Pipeline) -> None:
    """§5.3: 末尾一致の候補が 2 件以上なら E_TARGET_AMBIGUOUS（候補つき）。TS の charge は 3 つのオーバーロード。"""
    r = await call(pipeline, "json", targets=[{"path": PAY_TS, "symbol_name": "charge"}])
    assert r.is_error
    err = r.structured["error"]
    assert err["code"] == "E_TARGET_AMBIGUOUS"
    assert len(err["candidates"]) == 3 and all(c["urn"].startswith("lk://") for c in err["candidates"])


async def test_partial_failure(pipeline: Pipeline) -> None:
    """§5.4.4: 1 件以上成功すれば ok。失敗項目は `! <target>: <CODE> <message>`。"""
    r = await call(
        pipeline,
        targets=[
            {"path": PAY_PY, "symbol_name": "PaymentRequest"},
            {"path": PAY_PY, "symbol_name": "NoSuchThing"},
            {"path": "missing.py", "symbol_name": "x"},
            {"path": PAY_PY, "symbol_name": "xpath:/a/b"},
            {"path": PAY_PY},
        ],
    )
    assert not r.is_error and g.validate(r.text) == []
    assert f"! {PAY_PY}#NoSuchThing: E_TARGET_NOT_FOUND" in r.text
    assert "! missing.py#x: E_TARGET_NOT_FOUND" in r.text
    assert f"! {PAY_PY}#xpath:/a/b: E_UNSUPPORTED" in r.text
    assert "E_INVALID_ARGUMENT" in r.text


async def test_all_failed_is_an_error_with_the_first_item(pipeline: Pipeline) -> None:
    r = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "Nope"}, {"path": "m.py", "line": 1}])
    assert r.is_error
    err = r.structured["error"]
    assert err["code"] == "E_TARGET_NOT_FOUND"
    assert [i["error"]["code"] for i in err["details"]["items"]] == ["E_TARGET_NOT_FOUND"] * 2


@pytest.mark.parametrize("n", [0, 21])
async def test_target_count_bounds(pipeline: Pipeline, n: int) -> None:
    r = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "PaymentRequest"}] * n)
    assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


@pytest.mark.parametrize("bad", [{"context_lines": 21}, {"line_range": {"start": 5, "end": 2}}])
async def test_invalid_arguments(pipeline: Pipeline, bad: dict[str, Any]) -> None:
    r = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "PaymentRequest"}], **bad)
    assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


@pytest.mark.parametrize("max_tokens", [200, 300, 3000])
async def test_postcondition_5_budget_is_respected(pipeline: Pipeline, max_tokens: int) -> None:
    targets = [
        {"path": PAY_PY, "symbol_name": "PaymentProcessor"},
        {"path": PAY_TS, "symbol_name": "PaymentClient"},
        {"path": PAY_PY, "symbol_name": "PaymentRequest"},
    ]
    r = await call(pipeline, "json", targets=targets, max_tokens=max_tokens)
    assert not r.is_error
    assert r.structured["budget"]["used_tokens_est"] <= max_tokens
    items = r.structured["data"]["items"]
    for item in items:
        if item.get("truncated"):  # 切ったときは続きの位置を記録する
            assert item["truncated_at_line"] > item["range_returned"]["end_line"]


async def test_truncated_body_is_a_prefix_and_tells_where(pipeline: Pipeline) -> None:
    ref = {"path": PAY_PY, "symbol_name": "PaymentProcessor"}
    full = (await call(pipeline, "json", targets=[ref])).structured["data"]["items"][0]
    small = await call(pipeline, "json", targets=[ref], max_tokens=300)
    items = small.structured["data"]["items"]
    assert items, small.text
    item = items[0]
    assert item["truncated"] is True
    assert full["code"].startswith(item["code"])
    assert item["truncated_at_line"] == item["range_returned"]["end_line"] + 1
    text = (await call(pipeline, targets=[ref], max_tokens=300)).text
    assert g.validate(text) == []
    assert "truncated at line" in text


async def test_non_working_tree_revision_is_unsupported(pipeline: Pipeline) -> None:
    r = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "PaymentRequest"}], revision="HEAD")
    assert r.is_error and r.structured["error"]["code"] == "E_UNSUPPORTED"
