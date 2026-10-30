"""T-06 / T-07: ``super().m()`` を基底のメソッドへの ``structural`` の呼び出しにし（BUG-0081）、``a.b.m()`` を ``type(a)`` のメソッドに
結ばない（BUG-0079）。

フィクスチャ ``tests/fixtures/receiver_calls``（README に各呼び出しの意図）を ``Indexer`` で取り込んだ実索引で確かめる。
``super()`` の受け手は構文から分かるので、名前の一致（BUG-0071）の「型不明・曖昧」の警告には回らない。中身の分からない外部の基底に
先に当たって結ばなかった呼び出しは、届くかもしれない対象の答えに理由（``reason: external_base``）つきで書く。
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

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "receiver_calls"
WS = "receiver_calls"
BASE = "pkg/base.py"
SUB = "pkg/sub.py"
MID = "pkg/mid.py"
LEAF = "pkg/leaf.py"
DIAMOND = "pkg/diamond.py"
MIXED = "pkg/mixed.py"
CHAIN = "pkg/chain.py"


def U(path: str, qualified: str | None = None) -> str:
    return str(Urn(WS, path, CodeSel(qualified) if qualified else None))


def lines_of(path: str, needle: str) -> list[int]:
    lines = (FIXTURE / path).read_bytes().decode("utf-8").splitlines()
    return [i + 1 for i, text in enumerate(lines) if needle in text]


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("receivers")
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


WORKER_LINE = lines_of(MIXED, "return super().save()")[0]
PLAIN_LINE = lines_of(MIXED, "return super().save()")[1]


async def test_t06_super_calls_are_structural_references_of_the_base_method(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``Base.save`` の参照に ``Sub.save`` の ``super().save()``（別ファイル）と ``Plain.save`` が ``structural`` で出る。
    ``Leaf.save`` の ``super().save()`` は途中の ``Mid.save`` に結ぶので出ない。「型不明・同名 2 つ」の警告は出ない。"""
    r = await refs(pipeline, BASE, "Base.save")
    data = r.structured["data"]
    got = {(x["path"], x["line"], x["enclosing_name"]) for x in data["references"]}
    assert got == {
        (SUB, lines_of(SUB, "return super().save()")[0], "Sub.save"),
        (MIXED, PLAIN_LINE, "Plain.save"),
    }
    for x in data["references"]:
        assert (x["use"], x["confidence_level"]) == ("call", "structural") and "match" not in x
    assert data["totals"].get("by_name") is None
    assert not any("receiver of unknown type" in w for w in r.structured.get("warnings", []))
    mid = await refs(pipeline, MID, "Mid.save")
    assert [(x["path"], x["enclosing_name"]) for x in mid.structured["data"]["references"]] == [(LEAF, "Leaf.save")]


async def test_t06_super_blocked_by_an_external_base_is_reported_with_its_reason(pipeline: Pipeline) -> None:
    """``Worker(threading.Thread, Base)`` の ``super().save()`` は結ばず、``Base.save`` の答えに理由つきで書く（曖昧な名前ではない）。"""
    r = await refs(pipeline, BASE, "Base.save")
    unattributed = r.structured["data"]["unattributed_calls"]
    assert [(u["name"], u["total"], u["reason"]) for u in unattributed] == [("save", 1, "external_base")]
    site = unattributed[0]["sites"][0]
    assert (site["path"], site["line"], site["enclosing_name"]) == (MIXED, WORKER_LINE, "Worker.save")
    assert (site["via_super"], site["receiver_class"], site["blocked_by"]) == (
        True,
        "Worker",
        "lk://_ext/threading#Thread",
    )
    warnings = r.structured["warnings"]
    assert any(
        "may reach Base.save but are not attributed" in w and "external base" in w and f"{MIXED}:{WORKER_LINE}" in w
        for w in warnings
    ), warnings
    c = await refs(pipeline, BASE, "Base.save", "compact")
    assert g.validate(c.text) == [], g.validate(c.text)
    assert "references: 2 files, 2 sites (tests: 0)" in c.text
    # 届く先がワークスペースに無い Runner.run（Runner(threading.Thread)）や、届かない対象には書かない
    load = await refs(pipeline, BASE, "Base.load")
    assert "unattributed_calls" not in load.structured["data"]


async def test_t06_explicit_super_and_diamond(pipeline: Pipeline) -> None:
    load = await refs(pipeline, BASE, "Base.load")
    got = {(x["path"], x["enclosing_name"]) for x in load.structured["data"]["references"]}
    assert got == {(SUB, "Sub.load"), (LEAF, "Leaf.load")}  # super(Sub, self).load()・2 段上
    greet = await refs(pipeline, DIAMOND, "C.greet")
    got = {(x["path"], x["enclosing_name"]) for x in greet.structured["data"]["references"]}
    assert got == {(DIAMOND, "D.greet"), ("pkg/diamond_use.py", "E.greet")}
    a_greet = await refs(pipeline, DIAMOND, "A.greet")
    assert a_greet.structured["data"]["references"] == []


async def test_t06_typescript_super(pipeline: Pipeline) -> None:
    r = await refs(pipeline, "web/base.ts", "Base.save")
    got = {(x["path"], x["enclosing_name"], x["confidence_level"]) for x in r.structured["data"]["references"]}
    assert got == {("web/sub.ts", "Sub.save", "structural")}
    assert len(r.structured["data"]["references"]) == 2  # メソッドの中とアロー関数の中


async def test_t07_incoming_and_outgoing_through_super(pipeline: Pipeline) -> None:
    """PC:T-07#1 ``Base.save`` の incoming に ``Sub.save``・``Plain.save``、``Sub.save`` の outgoing は ``Base.save``（自分自身ではない）。"""
    r = await calls(pipeline, BASE, "Base.save", "incoming", depth=1)
    nodes = r.structured["data"]["nodes"]
    assert {n["name"] for n in nodes} == {"Sub.save", "Plain.save"}
    assert all(n["confidence_level"] == "structural" and "match" not in n for n in nodes)
    unattributed = r.structured["data"]["unattributed_calls"]
    assert [(u["direction"], u["reason"], u["total"]) for u in unattributed] == [("incoming", "external_base", 1)]
    assert any(w.startswith("incoming: 1 call site(s) of .save") for w in r.structured["warnings"])
    out = await calls(pipeline, SUB, "Sub.save", "outgoing", depth=1)
    assert [n["urn"] for n in out.structured["data"]["nodes"] if n["urn"].startswith("lk://receiver_calls/")] == [
        U(BASE, "Base.save")
    ]
    leaf = await calls(pipeline, LEAF, "Leaf.save", "outgoing", depth=1)
    assert [n["urn"] for n in leaf.structured["data"]["nodes"] if n["urn"].startswith("lk://receiver_calls/")] == [
        U(MID, "Mid.save")
    ]
    c = await calls(pipeline, BASE, "Base.save", "incoming", "compact", depth=1)
    assert g.validate(c.text) == [], g.validate(c.text)
    assert f"<- Sub.save @{SUB}:{lines_of(SUB, 'return super().save()')[0]}" in c.text


async def test_t07_attribute_chain_does_not_reach_the_enclosing_class_method(pipeline: Pipeline) -> None:
    """BUG-0079: ``Url.__init__`` の ``other.raw.copy_with()`` は ``Url.copy_with`` の呼び出し元にしない（structural）。"""
    r = await calls(pipeline, CHAIN, "Url.copy_with", "incoming", depth=1)
    assert [n["name"] for n in r.structured["data"]["nodes"]] == []
    out = await calls(pipeline, CHAIN, "Url.__init__", "outgoing", depth=1)
    got = {
        (n["urn"], s["line"])
        for n in out.structured["data"]["nodes"]
        if n["urn"].startswith("lk://receiver_calls/")
        for s in n["call_sites"]
    }
    assert U(CHAIN, "Url.copy_with") not in {u for u, _ in got}
    assert got >= {
        (U(CHAIN, "Parsed.copy_with"), lines_of(CHAIN, "other.parsed.copy_with()")[0]),
        (U(CHAIN, "Parsed.copy_with"), lines_of(CHAIN, "self.view.copy_with()")[0]),
    }
