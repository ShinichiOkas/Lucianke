"""グラフ出力（``format: "dot"`` / ``"mermaid"``）のパイプライン配線の単体テスト（仕様書 §5.2、§5.4.3、§6.3、§6.4）。

偽のツールを登録し、既定の本実装（パッカー・カーソル・レンダラ）を通す。失敗は要求された形式のコメント形式
（dot は ``// ``、mermaid は ``%% ``）で返ること、グラフ非対応のツールへの指定は ``E_INVALID_ARGUMENT`` で、
その応答も要求の形式のコメント形式であること。
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput
from lucianke.mcp.pipeline import INTERNAL_MESSAGE, Pipeline, default_renderers
from lucianke.mcp.registry import CommonArgs, ToolRegistry, ToolSpec

A = "lk://app/src/a.py#f"
B = "lk://app/src/b.py#B.g"
C = "lk://app/src/c.py#h"
PREFIX = {"dot": "// ", "mermaid": "%% "}


class GraphArgs(CommonArgs):
    pass


def graph_output() -> ToolOutput:
    rec = Record(kind="edge", key="e0", priority=0, lines=("f -> g",), data={}, confidence=Confidence.EXACT)
    return ToolOutput(
        records=[rec],
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="none", confidence=Confidence.EXACT),
        page_unit="edge",
        data_static={
            "graph": {
                "nodes": [{"id": A, "label": "f", "path": "src/a.py"}, {"id": B, "label": "B.g", "path": "src/b.py"}],
                "edges": [
                    {"source": A, "target": B, "label": "CALLS"},
                    {"source": B, "target": C, "label": "CALLS", "conf": "heuristic"},
                ],
            }
        },
    )


def pipeline(*, allows_graph: bool = True, behavior: Any = None) -> Pipeline:
    async def handler(ctx: Any, args: Any) -> ToolOutput:
        if behavior is not None:
            return behavior()
        return graph_output()

    spec = ToolSpec(name="graph_tool", tid="T-XX", args_model=GraphArgs, handler=handler, default_max_tokens=2000,
                    allows_graph=allows_graph)  # fmt: skip
    return Pipeline(ToolRegistry([spec]))  # 既定のレンダラ・パッカー・カーソルコーデック


def raising(err: Exception) -> Any:
    def behavior() -> ToolOutput:
        raise err

    return behavior


def test_default_renderers_include_graph_formats():
    assert set(default_renderers()) == {"compact", "json", "dot", "mermaid"}


async def test_dot_success_through_pipeline():
    rendered = await pipeline().call_tool("graph_tool", {"format": "dot"})
    assert rendered.is_error is False
    lines = rendered.text.splitlines()
    assert lines[0].startswith("// # graph_tool ok ")
    assert 'digraph "graph_tool" {' in rendered.text
    assert f'"{A}" -> "{B}" [label="CALLS"];' in rendered.text
    assert 'conf="heuristic"' in rendered.text  # 降格エッジ（仕様書 §6.3）
    assert rendered.structured is None


async def test_mermaid_success_through_pipeline():
    rendered = await pipeline().call_tool("graph_tool", {"format": "mermaid"})
    assert rendered.is_error is False
    lines = rendered.text.splitlines()
    assert lines[0].startswith("%% # graph_tool ok ")
    assert "flowchart LR" in rendered.text
    assert re.search(r"n1 -->\|\"CALLS\"\| n2", rendered.text)
    assert "-.->" in rendered.text  # 降格エッジは点線


@pytest.mark.parametrize("fmt", ["dot", "mermaid"])
async def test_graph_format_on_non_graph_tool_is_invalid_argument_in_comment_form(fmt):
    rendered = await pipeline(allows_graph=False).call_tool("graph_tool", {"format": fmt})
    assert rendered.is_error is True
    prefix = PREFIX[fmt]
    lines = rendered.text.splitlines()
    assert lines[0] == f"{prefix}# graph_tool error E_INVALID_ARGUMENT"
    assert lines[1].startswith(f"{prefix}! format: ")
    assert all(line.startswith(prefix) for line in lines)  # 全行がコメント（dot / mermaid として読み込める）
    assert "digraph" not in rendered.text and "flowchart" not in rendered.text


@pytest.mark.parametrize("fmt", ["dot", "mermaid"])
async def test_tool_error_is_comment_form_header_in_requested_format(fmt):
    err = LuciankeError(ErrorCode.E_TARGET_NOT_FOUND, "no such symbol: f")
    rendered = await pipeline(behavior=raising(err)).call_tool("graph_tool", {"format": fmt})
    assert rendered.is_error is True
    prefix = PREFIX[fmt]
    lines = rendered.text.splitlines()
    assert lines[0] == f"{prefix}# graph_tool error E_TARGET_NOT_FOUND"
    assert lines[1] == f"{prefix}! no such symbol: f"
    assert all(line.startswith(prefix) for line in lines)


async def test_ambiguous_target_candidates_are_listed_as_comments():
    err = LuciankeError(ErrorCode.E_TARGET_AMBIGUOUS, "2 candidates",
                        candidates=[{"urn": A, "signature": "def f()"}, {"urn": B, "signature": "def g()"}])  # fmt: skip
    rendered = await pipeline(behavior=raising(err)).call_tool("graph_tool", {"format": "dot"})
    assert rendered.is_error is True
    lines = rendered.text.splitlines()
    assert lines[0] == "// # graph_tool error E_TARGET_AMBIGUOUS"
    assert all(line.startswith("// ") for line in lines)
    assert any(A in line for line in lines) and any(B in line for line in lines)


async def test_unexpected_exception_is_internal_error_in_comment_form():
    rendered = await pipeline(behavior=raising(RuntimeError("SECRET-DETAIL"))).call_tool("graph_tool", {"format": "mermaid"})
    assert rendered.is_error is True
    lines = rendered.text.splitlines()
    assert lines[0] == "%% # graph_tool error E_INTERNAL"
    assert lines[1] == f"%% ! {INTERNAL_MESSAGE}"
    assert "SECRET-DETAIL" not in rendered.text


async def test_invalid_argument_with_graph_format_stays_in_requested_format():
    # 検証前の失敗（スキーマ違反）でも、要求が dot ならコメント形式で返す
    rendered = await pipeline().call_tool("graph_tool", {"format": "dot", "bogus": 1})
    assert rendered.is_error is True
    assert rendered.text.splitlines()[0] == "// # graph_tool error E_INVALID_ARGUMENT"


async def test_graph_output_without_graph_data_is_invalid_argument_comment():
    # グラフ対応ツールでも、グラフの材料（data_static["graph"] / source_urn・target_urn 列）が無ければ失敗応答（コメント形式）
    out = graph_output()
    out.data_static.pop("graph")
    rendered = await pipeline(behavior=lambda: out).call_tool("graph_tool", {"format": "mermaid"})
    assert rendered.is_error is True
    assert rendered.text.splitlines()[0] == "%% # graph_tool error E_INVALID_ARGUMENT"


async def test_compact_and_json_failures_are_unchanged():
    err = LuciankeError(ErrorCode.E_TARGET_NOT_FOUND, "no such symbol: f")
    p = pipeline(behavior=raising(err))
    compact = await p.call_tool("graph_tool", {})
    assert compact.is_error and compact.text.splitlines()[0] == "# graph_tool error E_TARGET_NOT_FOUND"
    j = await p.call_tool("graph_tool", {"format": "json"})
    assert j.is_error and j.structured is not None and j.structured["error"]["code"] == "E_TARGET_NOT_FOUND"
    assert json.loads(j.text)["tool"] == "graph_tool"
