"""T-06 / T-07: 基底クラスが別ファイルにある継承メンバーへの呼び出し（``self.helper()``・``s: Sub`` の ``s.helper()``）を
``structural`` の答えに入れる（BUG-0075。``index/inherited.py``）。

フィクスチャ ``tests/fixtures/inherited_calls``（README に各呼び出しの意図）を ``Indexer`` で取り込んだ実索引で確かめる。
名前で結ぶ（BUG-0071 の ``conf=heuristic`` / ``match: "name"``）のではなく、受け手の型と ``INHERITS`` 行から決まる ``structural`` の行。
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.app import build_services
from lucianke.core.urn import CodeSel, Urn
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t06_find_references_and_implementations as t06
from lucianke.tools import t07_query_call_hierarchy as t07
from lucianke.tools._services import ToolServices
from lucianke.workspace.types import Workspace

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "inherited_calls"
WS = "inherited_calls"
BASE = "pkg/base.py"
MID = "pkg/mid.py"
SUB = "pkg/sub.py"
USE = "pkg/use.py"
MIXED = "pkg/mixed.py"


def U(path: str, qualified: str | None = None) -> str:
    return str(Urn(WS, path, CodeSel(qualified) if qualified else None))


def lines_of(path: str, needle: str) -> list[int]:
    lines = (FIXTURE / path).read_bytes().decode("utf-8").splitlines()
    return [i + 1 for i, text in enumerate(lines) if needle in text]


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("inherited")
    root = base / WS
    shutil.copytree(FIXTURE, root)
    path = base / "db" / "paad.sqlite"

    async def build() -> None:
        writer = IndexWriter(path)
        readers = ReaderPool(path, size=2)
        indexer = Indexer(Workspace(workspace_id=WS, root=root.resolve()), writer, readers, TreeCache())
        try:
            report = await indexer.scan()
            assert not report.errors, report.errors
        finally:
            await indexer.aclose()
            readers.close()
            writer.close()

    asyncio.run(build())
    return path


@dataclass(slots=True)
class _Services(ToolServices):
    db_path: Path | None = None


@pytest.fixture
def pipeline(db: Path) -> Pipeline:
    base = build_services(lambda: [str(db.parent.parent / WS)])
    services = _Services(roots=base.roots, trees=base.trees, lines=base.lines, db_path=db)
    registry = ToolRegistry()
    registry.register(t06.TOOL_SPEC)
    registry.register(t07.TOOL_SPEC)
    return Pipeline(registry, services=services)


async def refs(pipeline: Pipeline, path: str, name: str, fmt: str = "json", **args: Any) -> Any:
    r = await pipeline.call_tool(
        "find_references_and_implementations",
        {"target": {"path": path, "symbol_name": name}, "query_type": "references", "format": fmt, **args},
    )
    assert not r.is_error, r.text
    return r


async def calls(
    pipeline: Pipeline, path: str, name: str, direction: str, fmt: str = "json", **args: Any
) -> Any:
    r = await pipeline.call_tool(
        "query_call_hierarchy",
        {"target": {"path": path, "symbol_name": name}, "direction": direction, "format": fmt, **args},
    )
    assert not r.is_error, r.text
    return r


HELPER_SITES = {
    (SUB, lines_of(SUB, "return self.helper()")[0], "Sub.run"),
    (SUB, lines_of(SUB, "return s.helper()")[0], "use"),
    (USE, lines_of(USE, "return s.helper()")[0], "go"),
    (MIXED, lines_of(MIXED, "return self.helper()")[0], "Plain.go"),
    (MIXED, lines_of(MIXED, "return self.helper()")[2], "Shape.go"),
}


async def test_t06_lists_calls_through_subclass_receivers_as_structural(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``Base.helper`` の参照に、派生クラスの受け手（別ファイル、2 段下を含む）からの呼び出しが ``structural`` で出る。"""
    r = await refs(pipeline, BASE, "Base.helper")
    data = r.structured["data"]
    got = {(x["path"], x["line"], x["enclosing_name"]) for x in data["references"]}
    assert got == HELPER_SITES
    for x in data["references"]:
        assert (x["use"], x["confidence_level"]) == ("call", "structural") and "match" not in x
    worker = (MIXED, lines_of(MIXED, "return self.helper()")[1])
    assert worker not in {
        (p, line) for p, line, _ in got
    }  # Worker(threading.Thread, Base): 先の外部の基底が持つかもしれない
    assert data["totals"].get("by_name") is None
    # 結ばなかった Worker.go は、名前の一致の「曖昧」ではなく、先の外部の基底を理由に書く（BUG-0081）
    assert [(u["reason"], [(s["path"], s["line"]) for s in u["sites"]]) for u in data["unattributed_calls"]] == [
        ("external_base", [worker])
    ]
    assert r.structured["_resolution_meta"]["confidence_level"] == "structural"


async def test_t06_compact_has_no_heuristic_marks(pipeline: Pipeline) -> None:
    r = await refs(pipeline, BASE, "Base.helper", "compact")
    assert g.validate(r.text) == [], g.validate(r.text)
    assert "references: 3 files, 5 sites (tests: 0)" in r.text
    assert "conf=heuristic" not in r.text
    line = lines_of(USE, "return s.helper()")[0]
    assert any(
        x.startswith(f"{USE}:{line}:") and "[call] in go  return s.helper()" in x for x in r.text.splitlines()
    )


async def test_t06_override_in_an_intermediate_base_wins(pipeline: Pipeline) -> None:
    """``Sub`` から見た ``shout`` は途中の ``Mid`` が上書きしている。``Base.shout`` には出ず ``Mid.shout`` に出る。"""
    base = await refs(pipeline, BASE, "Base.shout")
    assert base.structured["data"]["references"] == []
    mid = await refs(pipeline, MID, "Mid.shout")
    got = {(x["path"], x["enclosing_name"]) for x in mid.structured["data"]["references"]}
    assert got == {(SUB, "Sub.loud"), (SUB, "fresh")}


async def test_t06_typescript_extends_and_interface_extends(pipeline: Pipeline) -> None:
    r = await refs(pipeline, "web/base.ts", "Base.helper")
    got = {(x["path"], x["enclosing_name"]) for x in r.structured["data"]["references"]}
    assert got == {("web/sub.ts", "Sub.run"), ("web/sub.ts", "use"), ("web/sub.ts", "fresh")}
    ping = await refs(pipeline, "web/base.ts", "Pingable.ping")
    assert [(x["path"], x["enclosing_name"]) for x in ping.structured["data"]["references"]] == [
        ("web/sub.ts", "use")
    ]


async def test_t07_incoming_reaches_callers_through_subclasses(pipeline: Pipeline) -> None:
    """PC:T-07#1 ``Base.helper`` の incoming に ``Sub.run``・``use``・``go``・``Plain.go``・``Shape.go``（どれも structural）。"""
    r = await calls(pipeline, BASE, "Base.helper", "incoming", depth=1)
    nodes = r.structured["data"]["nodes"]
    assert {n["name"] for n in nodes} == {"Sub.run", "use", "go", "Plain.go", "Shape.go"}
    assert all(n["confidence_level"] == "structural" and "match" not in n for n in nodes)
    c = await calls(pipeline, BASE, "Base.helper", "incoming", "compact", depth=1)
    assert g.validate(c.text) == [], g.validate(c.text)
    assert f"<- go @{USE}:{lines_of(USE, 'return s.helper()')[0]}" in c.text
    assert "conf=heuristic" not in c.text


async def test_t07_outgoing_points_at_the_inherited_member(pipeline: Pipeline) -> None:
    run = await calls(pipeline, SUB, "Sub.run", "outgoing", depth=1)
    assert [n["urn"] for n in run.structured["data"]["nodes"]] == [U(BASE, "Base.helper")]
    build = await calls(pipeline, SUB, "Sub.build", "outgoing", depth=1)
    assert [n["urn"] for n in build.structured["data"]["nodes"]] == [U(BASE, "Base.make")]
    loud = await calls(pipeline, SUB, "Sub.loud", "outgoing", depth=1)
    assert [n["urn"] for n in loud.structured["data"]["nodes"]] == [U(MID, "Mid.shout")]
