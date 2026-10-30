"""T-02 ``get_file_skeleton`` の契約テスト（仕様書 §11 T-02 の事後条件 1・2・4・5 ほか）。

パイプライン経由でツールを呼ぶ。ルートは ``tests/fixtures/poly``。ここは構文木のコード（Python・TypeScript）の検査で、
Notebook・データファイル（事後条件 3）は ``test_T02_data_files.py``。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest
import tree_sitter_python
from tree_sitter import Language, Parser

from lucianke.app import build_services
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import register_tools

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAY_PY = "src/shop/services/payment.py"
PAY_TS = "web/src/payment.ts"


@pytest.fixture
def pipeline() -> Pipeline:
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=build_services(lambda: [str(POLY)]))


async def call(pipeline: Pipeline, fmt: str = "compact", **args: Any):
    return await pipeline.call_tool("get_file_skeleton", {"format": fmt, **args})


def _flatten(symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in symbols:
        out.append(s)
        out.extend(_flatten(s.get("children", [])))
    return out


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


async def test_tools_list_has_t02(pipeline: Pipeline) -> None:
    names = [d.name for d in pipeline.registry.descriptors()]
    assert "get_file_skeleton" in names


async def test_compact_output_is_grammatical_and_structural(pipeline: Pipeline) -> None:
    r = await call(pipeline, paths=[PAY_PY, PAY_TS])
    assert not r.is_error
    assert g.validate(r.text) == []
    lines = r.text.splitlines()
    assert lines[0].startswith("# get_file_skeleton ok conf=structural ")
    assert f"=== {PAY_PY} (" in r.text and "sha=" in r.text
    assert "[Class] pub PaymentProcessor @L" in r.text
    assert "[Method] pub async process_charge(self, req: PaymentRequest) -> Receipt @L38-52 { ...13 lines omitted }" in r.text


async def test_postcondition_1_ranges_match_tree_sitter_nodes(pipeline: Pipeline) -> None:
    """事後条件 1: Tier 1〜2 の各 symbols[].range は、Tree-sitter の対応ノードの開始行・終了行と一致する。"""
    r = await call(pipeline, "json", paths=[PAY_PY])
    item = r.structured["data"]["items"][0]
    source = (POLY / PAY_PY).read_bytes()
    tree = Parser(Language(tree_sitter_python.language())).parse(source)
    spans: set[tuple[int, int]] = set()

    def walk(node: Any) -> None:
        if node.type in ("function_definition", "class_definition", "decorated_definition"):
            spans.add((node.start_point[0] + 1, node.end_point[0] + 1))
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    defs = [s for s in _flatten(item["symbols"]) if s["kind"] in ("Class", "Function", "Method", "Constructor")]
    assert len(defs) >= 6
    for s in defs:
        assert (s["range"]["start_line"], s["range"]["end_line"]) in spans, s["qualified_name"]


@pytest.mark.parametrize("path", [PAY_PY, PAY_TS])
async def test_postcondition_2_text_lines_come_from_source(pipeline: Pipeline, path: str) -> None:
    """事後条件 2: シグネチャ・doc: のテキストは元ファイルの内容と一致する（空白を除いて部分文字列）。"""
    r = await call(pipeline, "json", paths=[path])
    item = r.structured["data"]["items"][0]
    source = _squash((POLY / path).read_text(encoding="utf-8"))
    checked = 0
    for s in _flatten(item["symbols"]):
        if s["kind"] in ("Function", "Method", "Constructor") and s["signature"]:
            assert _squash(s["name"] + s["signature"]) in source, s["qualified_name"]
            checked += 1
        if s.get("doc_summary"):
            assert _squash(s["doc_summary"]) in source, s["qualified_name"]
            checked += 1
    assert checked >= 4
    # compact の doc: 行も同様
    c = await call(pipeline, paths=[path])
    for line in c.text.splitlines():
        m = re.match(r"^\s+doc: (.+)$", line)
        if m:
            assert _squash(m.group(1)) in source


async def test_imports_line(pipeline: Pipeline) -> None:
    r = await call(pipeline, "json", paths=[PAY_PY])
    imports = r.structured["data"]["items"][0]["imports"]
    assert "Decimal from decimal" in imports and "os" in imports


async def test_json_item_shape(pipeline: Pipeline) -> None:
    r = await call(pipeline, "json", paths=[PAY_PY])
    item = r.structured["data"]["items"][0]
    assert item["ok"] and item["path"] == PAY_PY and item["language"] == "python"
    assert len(item["file_sha256"]) == 64
    assert item["confidence_level"] == "structural"
    assert r.structured["_resolution_meta"]["confidence_level"] == "structural"
    assert r.structured["_resolution_meta"]["is_degraded"] is False  # structural が最高水準のツール


@pytest.mark.parametrize("n", [0, 21])
async def test_batch_size_is_rejected_without_items(pipeline: Pipeline, n: int) -> None:
    """PC:T-02#4 ``paths`` が 21 件のとき ``E_INVALID_ARGUMENT`` で、``items`` を持たない（0 件も同じ）。"""
    r = await call(pipeline, "json", paths=[PAY_PY] * n)
    assert r.is_error
    assert r.structured["ok"] is False
    assert r.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    assert r.structured["data"] is None


@pytest.mark.parametrize("max_tokens", [200, 300, 2000])
async def test_postcondition_5_budget_is_respected(pipeline: Pipeline, max_tokens: int) -> None:
    r = await call(pipeline, "json", paths=[PAY_PY, PAY_TS, "src/shop/models/receipt.py"], max_tokens=max_tokens)
    assert not r.is_error
    assert r.structured["budget"]["used_tokens_est"] <= max_tokens
    if r.structured["page"]["next_cursor"]:  # 収まらない分は次のページへ
        assert r.structured["page"]["returned"] < r.structured["page"]["total_estimate"]


async def test_shrink_drops_doc_first_and_stays_grammatical(pipeline: Pipeline) -> None:
    full = await call(pipeline, paths=[PAY_PY])
    # payment.py の URN はすべて lk://<ws>/<path>#<修飾名> で導けるので urn: 行は無い（§6.1 規約 2。BUG-0069）
    assert "  doc: " in full.text and "urn: " not in full.text
    used = re.search(r" ~(\d+)/\d+tok", full.text.splitlines()[0])
    assert used is not None
    small = await call(pipeline, paths=[PAY_PY], max_tokens=int(used[1]) - 1)  # 全部入りがちょうど入らない予算
    assert "  doc: " not in small.text  # doc: から先に落ちる
    assert "urn omitted" not in small.text  # 出していない urn を「落とした」と書かない
    assert g.validate(small.text) == []


async def test_compact_shows_only_urns_not_derivable_but_json_keeps_every_urn(pipeline: Pipeline) -> None:
    """compact の urn: 行は導けない URN（TS のオーバーロードの /arity・~sighash）だけ。json の SymbolRecord は全部 urn を持つ
    （§6.2 の形は変えない。BUG-0069）。"""
    r = await call(pipeline, paths=[PAY_TS])
    shown = [ln.strip().removeprefix("urn: ") for ln in r.text.splitlines() if ln.strip().startswith("urn: ")]
    assert shown and all("#PaymentClient.charge/" in u for u in shown), r.text
    assert g.validate(r.text) == []
    j = await call(pipeline, "json", paths=[PAY_TS])

    def walk(syms: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [x for s in syms for x in (s, *walk(s["children"]))]

    every = walk(j.structured["data"]["items"][0]["symbols"])
    assert every and all(s["urn"].startswith("lk://") for s in every)
    assert set(shown) < {s["urn"] for s in every}


async def test_partial_failure_keeps_ok_and_marks_item(pipeline: Pipeline) -> None:
    """§5.4.4: 1 件以上成功すれば全体は ok。失敗項目は `! <path>: <CODE> <message>`。"""
    r = await call(pipeline, paths=[PAY_PY, "src/missing.py", "../outside.py", "legacy/calc.cob"])
    assert not r.is_error
    assert g.validate(r.text) == []
    assert "! src/missing.py: E_TARGET_NOT_FOUND" in r.text
    assert "! ../outside.py: E_INVALID_ARGUMENT" in r.text
    assert "=== legacy/calc.cob" in r.text and "! legacy/calc.cob" not in r.text  # Tier 4 は失敗にせず推定の骨格（BUG-0018）
    j = await call(pipeline, "json", paths=[PAY_PY, "src/missing.py"])
    items = j.structured["data"]["items"]
    assert [i["ok"] for i in items] == [True, False]
    assert items[1]["error"]["code"] == "E_TARGET_NOT_FOUND"


async def test_all_items_failed_is_error_with_first_code(pipeline: Pipeline) -> None:
    r = await call(pipeline, "json", paths=["src/missing.py", "../outside.py"])
    assert r.is_error
    err = r.structured["error"]
    assert err["code"] == "E_TARGET_NOT_FOUND"
    assert [i["error"]["code"] for i in err["details"]["items"]] == ["E_TARGET_NOT_FOUND", "E_INVALID_ARGUMENT"]


async def test_include_options(pipeline: Pipeline) -> None:
    r = await call(
        pipeline,
        paths=[PAY_PY],
        include_private=False,
        include_urns="none",
        include_doc_summary=False,
        include_imports=False,
    )
    assert "_validate_limit" not in r.text
    assert "urn: " not in r.text and "doc: " not in r.text and "imports:" not in r.text
    assert g.validate(r.text) == []


async def test_revision_on_a_workspace_without_git_is_revision_not_found(pipeline: Pipeline) -> None:
    """poly は Git のワーキングコピーではない。過去断面の読み取りは tests/contract/test_revision_reads.py（BUG-0018）。"""
    r = await call(pipeline, "json", paths=[PAY_PY], revision="HEAD")
    assert r.is_error and r.structured["error"]["code"] == "E_REVISION_NOT_FOUND"


async def test_unknown_workspace_id(pipeline: Pipeline) -> None:
    r = await call(pipeline, "json", paths=[PAY_PY], workspace_id="nope")
    assert r.is_error and r.structured["error"]["code"] == "E_WORKSPACE_NOT_CONFIGURED"


async def test_partial_failure_counts_failed_items_in_warnings(pipeline: Pipeline) -> None:
    """§5.4.4: 失敗項目の件数を warnings[] に書く（compact は `! ` 行）。全件成功なら warnings は空。"""
    j = await call(pipeline, "json", paths=[PAY_PY, "src/missing.py", "../outside.py"])
    assert not j.is_error and j.structured["ok"] is True
    assert len(j.structured["warnings"]) == 1 and j.structured["warnings"][0].startswith("2 of 3 items failed")
    r = await call(pipeline, paths=[PAY_PY, "src/missing.py"])
    assert g.validate(r.text) == [] and any(ln.startswith("! 1 of 2 items failed") for ln in r.text.splitlines()[:3])
    assert (await call(pipeline, "json", paths=[PAY_PY])).structured["warnings"] == []


async def test_header_shows_the_resolved_workspace_id_when_omitted(pipeline: Pipeline) -> None:
    """§5.4.2・§5.2: workspace_id 省略でも、ヘッダの ws= は解決後の ID（`-` ではない）。"""
    r = await call(pipeline, paths=[PAY_PY])
    header = r.text.splitlines()[0]
    assert " ws=poly " in header and " ws=- " not in header
    explicit = await call(pipeline, paths=[PAY_PY], workspace_id="poly")
    assert " ws=poly " in explicit.text.splitlines()[0]
