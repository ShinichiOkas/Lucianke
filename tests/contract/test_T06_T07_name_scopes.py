"""T-06 / T-07: 型の分からない受け手の呼び出しを名前で結ぶとき、候補を呼び出し箇所の近くから選ぶ（BUG-0077）。

フィクスチャ ``tests/fixtures/name_scopes`` を ``Indexer`` で取り込んだ実索引で確かめる（README に各呼び出しの意図）。

* 段は「同じファイル → 呼び出し箇所を含むテストフィクスチャ（``tests/fixtures/<名前>/``）→ ワークスペース」。最初に候補のある段で決める。
* フィクスチャの中の呼び出しは、フィクスチャ自身の同名のクラスに結び、``app/`` の同名のメソッドには結ばない。
* 本番の呼び出し・より近い定義の無いテストの呼び出しは、今までどおりワークスペースの唯一の定義に結ぶ。
* テストダブル（テストファイルの定義）は同じファイルの呼び出しにだけ結ぶ。同じ段に同名が複数なら曖昧として要約する。
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

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "name_scopes"
WS = "name_scopes"
BUDGET = "app/budget.py"
JOBS = "app/jobs.py"
FIT = "app/fit.py"
TEST_FIT = "tests/test_fit.py"
TEST_OTHER = "tests/test_other.py"
SAMPLE = "tests/fixtures/sample"
S_ENGINE = f"{SAMPLE}/app/engine.py"
S_SERVICE = f"{SAMPLE}/app/service.py"
S_DOUBLES = f"{SAMPLE}/tests/test_service.py"
S_TEST = f"{SAMPLE}/tests/test_render.py"
OTHER = "tests/fixtures/other/app/mod.py"


def U(path: str, qualified: str | None = None) -> str:
    return str(Urn(WS, path, CodeSel(qualified) if qualified else None))


def line_of(path: str, needle: str) -> int:
    lines = (FIXTURE / path).read_text(encoding="utf-8").splitlines()
    return next(i + 1 for i, text in enumerate(lines) if needle in text)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("scopes")
    root = base / WS  # ワークスペース ID はフォルダ名
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


async def calls(pipeline: Pipeline, path: str, name: str, direction: str, fmt: str = "json", **args: Any) -> Any:
    r = await pipeline.call_tool(
        "query_call_hierarchy",
        {"target": {"path": path, "symbol_name": name}, "direction": direction, "format": fmt, **args},
    )
    assert not r.is_error, r.text
    return r


def by_name(r: Any) -> set[tuple[str, int]]:
    return {(x["path"], x["line"]) for x in r.structured["data"]["references"] if x.get("match") == "name"}


def warnings_of(r: Any) -> list[str]:
    return list(r.structured.get("warnings") or [])


# --- T-06 -----------------------------------------------------------------------------------------


async def test_t06_fixture_calls_are_not_attributed_to_the_workspace_method(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``app/budget.py#Packer.pack`` には本番（``app/fit.py``）とテスト（``tests/test_fit.py``）の呼び出しだけ。
    同名のクラスを持つフィクスチャ（``sample``）・兄弟のフィクスチャ（``other``）の中の ``packer.pack()`` は入れない。"""
    r = await refs(pipeline, BUDGET, "Packer.pack")
    assert by_name(r) == {
        (FIT, line_of(FIT, 'packer.pack(["a"])')),
        (TEST_FIT, line_of(TEST_FIT, 'packer.pack(["b"])')),
    }
    paths = {x["path"] for x in r.structured["data"]["references"]}
    assert not paths & {S_SERVICE, S_TEST, OTHER}
    assert "unattributed_calls" not in r.structured["data"]
    assert any(
        "2 call site(s) marked conf=heuristic are matched by name only" in w
        and "Packer.pack is the only method named 'pack' in the workspace (besides 3 in test files)" in w
        for w in warnings_of(r)
    ), warnings_of(r)


async def test_t06_fixture_calls_go_to_the_fixtures_own_class(pipeline: Pipeline) -> None:
    """フィクスチャ ``sample`` の ``Packer.pack`` には、そのフィクスチャの本体とテストの呼び出し（偽物 ``FakePacker`` は数えない）。"""
    r = await refs(pipeline, S_ENGINE, "Packer.pack")
    assert by_name(r) == {
        (S_SERVICE, line_of(S_SERVICE, 'packer.pack(["c"])')),
        (S_TEST, line_of(S_TEST, 'packer.pack(["d"])')),
    }
    assert any(
        f"Packer.pack is the only method named 'pack' in the test fixture {SAMPLE}/ that holds the call"
        " (besides 1 in test files)" in w
        for w in warnings_of(r)
    ), warnings_of(r)
    # 注釈の無い引数 ``p`` の ``p.pack()`` は、同じファイルに ``pack`` が 1 つでも構文木の抽出器は結ばない（``structural`` の印で
    # 確かに見せない。BUG-0085）。名前の一致の同じファイルの段が ``conf=heuristic`` で結ぶ
    other = await refs(pipeline, OTHER, "Packer.pack")
    assert {(x["path"], x["line"]) for x in other.structured["data"]["references"]} == {(OTHER, line_of(OTHER, "p.pack([])"))}
    assert by_name(other) == {(OTHER, line_of(OTHER, "p.pack([])"))}
    assert any("Packer.pack is the only method named 'pack' in the calling file" in w for w in warnings_of(other)), (
        warnings_of(other)
    )


async def test_t06_equally_close_duplicates_in_a_fixture_are_ambiguous(pipeline: Pipeline) -> None:
    """``sink.settle()`` はフィクスチャの ``Left.settle`` と ``Right.settle``（同じ近さ）。どちらにも足さず要約する。
    外の ``app/budget.py#Packer.settle`` には結ばず、候補でもないので要約も書かない。"""
    site = (S_SERVICE, line_of(S_SERVICE, "sink.settle()"))
    for qualified in ("Left.settle", "Right.settle"):
        r = await refs(pipeline, S_ENGINE, qualified)
        data = r.structured["data"]
        assert data["references"] == [] and data["totals"].get("by_name") is None
        [u] = data["unattributed_calls"]
        assert (u["name"], u["total"], u["reason"], u["candidates_total"]) == ("settle", 1, "ambiguous", 2)
        assert {c["qualified_name"] for c in u["candidates"]} == {"Left.settle", "Right.settle"}
        assert [(s["path"], s["line"]) for s in u["sites"]] == [site]
        assert any(
            f"2 methods share the name in the test fixture {SAMPLE}/ that holds the call" in w for w in warnings_of(r)
        ), warnings_of(r)
    outer = await refs(pipeline, BUDGET, "Packer.settle")
    assert outer.structured["data"]["references"] == []
    assert "unattributed_calls" not in outer.structured["data"]


async def test_t06_workspace_duplicates_stay_ambiguous(pipeline: Pipeline) -> None:
    """本番の ``job.start()`` は ``Runner.start`` と ``Engine.start``。ディレクトリの近さでは選ばない（BUG-0071 のまま）。"""
    r = await refs(pipeline, JOBS, "Runner.start")
    [u] = r.structured["data"]["unattributed_calls"]
    assert (u["name"], u["reason"], u["candidates_total"]) == ("start", "ambiguous", 2)
    assert [(s["path"], s["line"]) for s in u["sites"]] == [(FIT, line_of(FIT, "job.start()"))]
    assert any(
        "1 call site(s) of .start() with a receiver of unknown type are not listed: 2 methods share the name (" in w
        for w in warnings_of(r)
    ), warnings_of(r)


async def test_t06_a_method_of_the_enclosing_class_is_never_the_match(pipeline: Pipeline) -> None:
    """``Session.terminate`` の中の ``self.proc.terminate()`` は包んだオブジェクトへの委譲。``terminate`` がワークスペースに
    ``Session.terminate`` しか無くても結ばない（要約も書かない）。``self.proc.reset()`` は ``Calc.reset`` と ``Session.reset`` の
    曖昧として要約し、``Calc.reset`` にも結ばない（囲みのクラスのメソッドは候補に数える）。"""
    term = await refs(pipeline, JOBS, "Session.terminate")
    assert term.structured["data"]["references"] == [] and "unattributed_calls" not in term.structured["data"]
    site = (JOBS, line_of(JOBS, "self.proc.reset()"))
    calc = await refs(pipeline, BUDGET, "Calc.reset")
    assert by_name(calc) == set()
    [u] = calc.structured["data"]["unattributed_calls"]
    assert (u["name"], u["reason"], u["candidates_total"]) == ("reset", "ambiguous", 2)
    assert {c["qualified_name"] for c in u["candidates"]} == {"Calc.reset", "Session.reset"}
    assert [(s["path"], s["line"]) for s in u["sites"]] == [site]
    out = await calls(pipeline, JOBS, "Session.terminate", "outgoing", depth=1)
    assert out.structured["data"]["nodes"] == [] and "unattributed_calls" not in out.structured["data"]


async def test_t06_test_doubles_compete_only_in_their_own_file(pipeline: Pipeline) -> None:
    """``FakeConn.execute`` は同じファイルの ``self.conn.execute()`` だけ。本番（``app/fit.py``）・別のテストファイルの呼び出しは結ばない。"""
    r = await refs(pipeline, TEST_FIT, "FakeConn.execute")
    assert by_name(r) == {(TEST_FIT, line_of(TEST_FIT, 'self.conn.execute("q")'))}
    assert any("FakeConn.execute is the only method named 'execute' in the calling file" in w for w in warnings_of(r))
    assert "unattributed_calls" not in r.structured["data"]


async def test_t06_fixture_with_only_a_test_double_does_not_fall_through(pipeline: Pipeline) -> None:
    """フィクスチャに ``send`` の偽物しか無いので、``mailer.send()`` は外の ``Mailer.send`` に結ばない（偽物にも結ばない）。
    フィクスチャに名前が無い ``calc.compute()`` は、ワークスペースの唯一の ``Calc.compute`` に結ぶ。"""
    mailer = await refs(pipeline, BUDGET, "Mailer.send")
    assert mailer.structured["data"]["references"] == [] and "unattributed_calls" not in mailer.structured["data"]
    fake = await refs(pipeline, S_DOUBLES, "FakeMailer.send")
    assert fake.structured["data"]["references"] == []
    calc = await refs(pipeline, BUDGET, "Calc.compute")
    assert by_name(calc) == {(S_SERVICE, line_of(S_SERVICE, "calc.compute()"))}


async def test_t06_compact_counts_only_in_scope_name_matches(pipeline: Pipeline) -> None:
    r = await refs(pipeline, BUDGET, "Packer.pack", "compact")
    assert g.validate(r.text) == [], g.validate(r.text)
    assert "references: 2 files, 2 sites (tests: 1; by name: 2)" in r.text
    assert SAMPLE not in r.text


# --- T-07 -----------------------------------------------------------------------------------------


async def test_t07_incoming_skips_fixture_callers_of_the_workspace_method(pipeline: Pipeline) -> None:
    r = await calls(pipeline, BUDGET, "Packer.pack", "incoming", depth=1)
    nodes = {(n["name"], n["path"]) for n in r.structured["data"]["nodes"]}
    assert nodes == {("render", FIT), ("test_render", TEST_FIT)}
    sample = await calls(pipeline, S_ENGINE, "Packer.pack", "incoming", depth=1)
    assert {(n["name"], n["path"]) for n in sample.structured["data"]["nodes"]} == {
        ("render", S_SERVICE),
        ("test_render", S_TEST),
    }


async def test_t07_outgoing_uses_the_nearest_scope(pipeline: Pipeline) -> None:
    render = await calls(pipeline, S_SERVICE, "render", "outgoing", depth=1)
    [node] = render.structured["data"]["nodes"]
    assert (node["urn"], node["confidence_level"], node["match"]) == (U(S_ENGINE, "Packer.pack"), "heuristic", "name")
    drain = await calls(pipeline, S_SERVICE, "drain", "outgoing", depth=1)
    assert drain.structured["data"]["nodes"] == []
    [u] = drain.structured["data"]["unattributed_calls"]
    assert (u["direction"], u["name"], u["reason"], u["candidates_total"]) == ("outgoing", "settle", "ambiguous", 2)
    query = await calls(pipeline, FIT, "query", "outgoing", depth=1)
    assert query.structured["data"]["nodes"] == [] and "unattributed_calls" not in query.structured["data"]
    fit = await calls(pipeline, FIT, "render", "outgoing", depth=1)
    [node] = fit.structured["data"]["nodes"]
    assert node["urn"] == U(BUDGET, "Packer.pack")
