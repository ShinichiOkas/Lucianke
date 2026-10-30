"""T-06 / T-07: 型の分からない受け手のメソッド呼び出し（``x.pack(...)`` で ``x`` が ``Any`` など）を名前で結ぶ（BUG-0071）。

フィクスチャ ``tests/fixtures/untyped_calls`` を ``Indexer`` で取り込んだ実索引で確かめる（README に各呼び出しの意図）。

* 名前のメソッドがワークスペースに 1 つ（テストダブルは数えない）なら、その位置を ``conf=heuristic`` ・ ``match: "name"`` で足す。
* 同名が複数・組み込み型と同名なら足さず、件数と先頭の位置を ``warnings`` と ``data.unattributed_calls`` に書く。
* 受け手の型が分かっていて（別のクラス）メンバーが無い呼び出しは、名前で結ばない。言語族（Python / TS）をまたがない。
* ``exact`` の答えには足さない（件数と先頭を ``warnings`` に書くだけ）。
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
from lucianke.index.connection import ReaderPool, open_reader
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import _name_calls as nc
from lucianke.tools import t06_find_references_and_implementations as t06
from lucianke.tools import t07_query_call_hierarchy as t07
from lucianke.tools._nav import SourceLines
from lucianke.tools._services import ToolServices
from lucianke.workspace.types import Workspace

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "untyped_calls"
WS = "untyped_calls"
ENGINE = "app/engine.py"
SERVICE = "app/service.py"
TESTS = "tests/test_pack.py"  # 型不明の ``packer.pack`` を呼ぶテスト（同じファイルに pack の定義が無い）
SENDER = "web/sender.ts"
RELAY = "web/relay.ts"


def U(path: str, qualified: str | None = None) -> str:
    return str(Urn(WS, path, CodeSel(qualified) if qualified else None))


def line_of(path: str, needle: str) -> int:
    """``needle`` を含む最初の行（1 始まり）。"""
    lines = (FIXTURE / path).read_text(encoding="utf-8").splitlines()
    return next(i + 1 for i, text in enumerate(lines) if needle in text)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("untyped")
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
def services(db: Path) -> _Services:
    base = build_services(lambda: [str(db.parent.parent / WS)])
    return _Services(roots=base.roots, trees=base.trees, lines=base.lines, db_path=db)


@pytest.fixture
def pipeline(services: _Services) -> Pipeline:
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


def warnings_of(r: Any) -> list[str]:
    return list(r.structured.get("warnings") or [])


# --- 索引 -----------------------------------------------------------------------------------------


def test_index_keeps_untyped_calls_and_not_known_receivers(db: Path) -> None:
    """``_name_calls`` は型不明の受け手の呼び出しだけ。``runner.pack()``（``Runner`` と分かる）・``Runner().pack()`` は入らない。
    ``packer: Any`` は ``lk://_ext/typing#Any.pack`` という外部メソッドに結ばない。"""
    conn = open_reader(db)
    try:
        rows = conn.execute(
            "SELECT f.path, nc.call_site_line, nc.name FROM _name_calls nc JOIN files f ON f.file_id = nc.file_id"
            " WHERE nc.name = 'pack' ORDER BY 1, 2"
        ).fetchall()
        any_ext = conn.execute("SELECT count(*) FROM symbols WHERE urn LIKE 'lk://_ext/typing#Any.%'").fetchone()[0]
    finally:
        conn.close()
    got = {(p, line) for p, line, _ in rows}
    assert got == {
        (SERVICE, line_of(SERVICE, "return packer.pack(items)")),
        (SERVICE, line_of(SERVICE, "out = packer.pack(items)")),
        (SERVICE, line_of(SERVICE, 'self.inner.pack(["z"])')),
        (SERVICE, line_of(SERVICE, 'make().pack(["y"])')),
        (TESTS, line_of(TESTS, 'packer.pack(["b"])')),
    }
    assert (SERVICE, line_of(SERVICE, "return runner.pack()")) not in got
    assert (ENGINE, line_of(ENGINE, "return r.pack()")) not in got
    assert any_ext == 0


# --- T-06 -----------------------------------------------------------------------------------------


async def test_t06_unique_name_adds_untyped_calls_marked_heuristic(pipeline: Pipeline) -> None:
    """PC:T-06#2 型不明の受け手の ``.pack()`` は、``pack`` のメソッドが ``Packer.pack`` だけ（テストダブルは数えない）なので足す。"""
    r = await refs(pipeline, ENGINE, "Packer.pack")
    data = r.structured["data"]
    got = {(x["path"], x["line"]): x for x in data["references"]}
    by_name = {
        (SERVICE, line_of(SERVICE, "return packer.pack(items)")): "render",
        (SERVICE, line_of(SERVICE, "out = packer.pack(items)")): "render_untyped",
        (SERVICE, line_of(SERVICE, 'self.inner.pack(["z"])')): "Holder.flush",
        (SERVICE, line_of(SERVICE, 'make().pack(["y"])')): "from_factory",
        (TESTS, line_of(TESTS, 'packer.pack(["b"])')): "test_pack",
    }
    structural = {
        (ENGINE, line_of(ENGINE, 'return self.pack(["a"])')): "Packer.run",
        (SERVICE, line_of(SERVICE, 'Packer().pack(["x"])')): "known",
    }
    assert set(got) == set(by_name) | set(structural)
    for key, enclosing in by_name.items():
        ref = got[key]
        assert (ref["use"], ref["enclosing_name"], ref["confidence_level"], ref["match"]) == (
            "call",
            enclosing,
            "heuristic",
            "name",
        )
    for key, enclosing in structural.items():
        ref = got[key]
        assert ref["enclosing_name"] == enclosing and ref["confidence_level"] == "structural" and "match" not in ref
    assert data["totals"]["by_name"] == 5
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "heuristic" and meta["mixed"] is True
    assert any(
        "5 call site(s) marked conf=heuristic are matched by name only" in w
        and "Packer.pack is the only method named 'pack' in the workspace (besides 1 in test files)" in w
        for w in warnings_of(r)
    ), warnings_of(r)
    assert "unattributed_calls" not in data


async def test_t06_compact_marks_name_matches_and_counts_them(pipeline: Pipeline) -> None:
    r = await refs(pipeline, ENGINE, "Packer.pack", "compact", include_tests=False)
    assert g.validate(r.text) == [], g.validate(r.text)
    assert "references: 2 files, 6 sites (tests: 0; by name: 4)" in r.text
    line = line_of(SERVICE, "return packer.pack(items)")
    hit = next(x for x in r.text.splitlines() if x.startswith(f"{SERVICE}:{line}:"))
    assert "[call] in render  return packer.pack(items)" in hit and hit.endswith("  conf=heuristic")
    known = next(x for x in r.text.splitlines() if "Packer().pack" in x)
    assert "conf=" not in known


async def test_t06_known_receiver_of_another_class_is_not_attributed(pipeline: Pipeline) -> None:
    """``runner: Runner`` の ``runner.pack()``・``Runner().pack()`` は ``Runner`` に ``pack`` が無いだけ。``Packer.pack`` に結ばない。"""
    r = await refs(pipeline, ENGINE, "Packer.pack")
    lines = {(x["path"], x["line"]) for x in r.structured["data"]["references"]}
    assert (SERVICE, line_of(SERVICE, "return runner.pack()")) not in lines
    assert (ENGINE, line_of(ENGINE, "return r.pack()")) not in lines


async def test_t06_shared_name_is_summarized_not_attributed(pipeline: Pipeline) -> None:
    """``run`` は ``Packer.run`` と ``Runner.run``。``job.run()`` はどちらにも足さず、件数と位置を warnings と data に書く。"""
    for qualified in ("Runner.run", "Packer.run"):
        r = await refs(pipeline, ENGINE, qualified)
        data = r.structured["data"]
        site = (SERVICE, line_of(SERVICE, "return job.run()"))
        assert site not in {(x["path"], x["line"]) for x in data["references"]}
        assert data["totals"].get("by_name") is None
        [u] = data["unattributed_calls"]
        assert (u["name"], u["total"], u["reason"], u["candidates_total"], u["builtin_name"]) == (
            "run",
            1,
            "ambiguous",
            2,
            False,
        )
        assert {c["qualified_name"] for c in u["candidates"]} == {"Packer.run", "Runner.run"}
        assert [(s["path"], s["line"], s["enclosing_name"]) for s in u["sites"]] == [(*site, "start")]
        assert any(
            "1 call site(s) of .run() with a receiver of unknown type are not listed: 2 methods share the name" in w
            and f"{SERVICE}:{site[1]}" in w
            for w in warnings_of(r)
        ), warnings_of(r)


async def test_t06_builtin_method_name_is_summarized_not_attributed(pipeline: Pipeline) -> None:
    """``get`` は ``dict.get`` と同名。ワークスペースに ``Runner.get`` しか無くても ``store.get(key)`` は足さない。"""
    r = await refs(pipeline, ENGINE, "Runner.get")
    data = r.structured["data"]
    assert data["references"] == []
    [u] = data["unattributed_calls"]
    assert (u["name"], u["reason"], u["builtin_name"], u["total"]) == ("get", "builtin_name", True, 1)
    assert any("built-in types also have a method of that name" in w for w in warnings_of(r))


async def test_t06_name_matching_stays_within_the_language_family(pipeline: Pipeline) -> None:
    py = await refs(pipeline, ENGINE, "Mailer.send")
    assert [(x["path"], x["match"]) for x in py.structured["data"]["references"]] == [(SERVICE, "name")]
    ts = await refs(pipeline, SENDER, "Sender.send")
    got = [(x["path"], x["line"], x["enclosing_name"], x["match"]) for x in ts.structured["data"]["references"]]
    assert got == [(RELAY, line_of(RELAY, "return s.send(msg)"), "relay", "name")]


async def test_t06_filters_apply_to_name_matches(pipeline: Pipeline) -> None:
    r = await refs(pipeline, ENGINE, "Packer.pack", use_kinds=["r"])
    assert r.structured["data"]["references"] == [] and not any("conf=heuristic" in w for w in warnings_of(r))
    scoped = await refs(pipeline, ENGINE, "Packer.pack", scope_paths=["tests"])
    assert [(x["path"], x["match"]) for x in scoped.structured["data"]["references"]] == [(TESTS, "name")]


def test_t06_exact_answers_get_a_note_not_records(db: Path, services: _Services) -> None:
    """exact の答えには足さない。結べるのに LSP の答えに無い位置の件数と先頭を warnings と data に書く。"""
    conn = open_reader(db)
    try:
        ws = services.workspace()
        sym = next(
            s
            for s in t06.queries.symbols_by_qualified_suffix(conn, WS, ENGINE, "Packer.pack")
            if s.qualified_name == "Packer.pack"
        )
        args = t06.FindReferencesArgs(target={"urn": str(sym.urn)})  # type: ignore[arg-type]
        found = t06._Found(target=sym, language="python", tier=t06.Tier.T1)
        t06._name_refs(conn, ws, sym, args, SourceLines(ws), found, exact=True)
    finally:
        conn.close()
    assert found.refs == [] and found.by_name == 0
    [u] = found.unattributed
    assert (u["name"], u["total"], u["reason"]) == ("pack", 5, "not_in_exact_answer")
    [w] = found.warnings
    assert "5 call site(s) of .pack() whose receiver type is unknown to the syntax tree are not in this exact answer" in w


# --- T-07 -----------------------------------------------------------------------------------------


async def test_t07_incoming_depth_2_reaches_through_untyped_callers(pipeline: Pipeline) -> None:
    """PC:T-07#1 incoming に型不明の呼び出し元（render など）が ``conf=heuristic`` で出て、その先（test_render）まで辿る。"""
    r = await calls(pipeline, ENGINE, "Packer.pack", "incoming", depth=2)
    nodes = r.structured["data"]["nodes"]
    first = {n["name"]: n for n in nodes if n["depth"] == 1}
    assert {"render", "render_untyped", "Holder.flush", "from_factory", "test_pack", "Packer.run", "known"} == set(
        first
    )
    assert (first["render"]["confidence_level"], first["render"]["match"]) == ("heuristic", "name")
    assert first["render"]["call_sites"][0]["match"] == "name"
    assert first["known"]["confidence_level"] == "structural" and "match" not in first["known"]
    second = [n for n in nodes if n["depth"] == 2 and n["parent_urn"] == U(SERVICE, "render")]
    assert [n["name"] for n in second] == ["test_render"]
    edges = r.structured["data"]["edges"]
    assert any(e["source_urn"] == U(SERVICE, "render") and e.get("match") == "name" for e in edges)
    c = await calls(pipeline, ENGINE, "Packer.pack", "incoming", "compact", depth=2)
    assert g.validate(c.text) == [], g.validate(c.text)
    line = line_of(SERVICE, "return packer.pack(items)")
    assert f"<- render @{SERVICE}:{line}  when: items  conf=heuristic" in c.text
    assert "matched by method name only" in c.text


async def test_t07_incoming_shared_name_is_reported(pipeline: Pipeline) -> None:
    r = await calls(pipeline, ENGINE, "Runner.run", "incoming", depth=1)
    assert "start" not in {n["name"] for n in r.structured["data"]["nodes"]}
    [u] = r.structured["data"]["unattributed_calls"]
    assert (u["direction"], u["name"], u["total"], u["reason"]) == ("incoming", "run", 1, "ambiguous")
    assert any(w.startswith("incoming: 1 call site(s) of .run()") for w in warnings_of(r)), warnings_of(r)


async def test_t07_outgoing_attributes_unique_names_and_reports_shared_ones(pipeline: Pipeline) -> None:
    render = await calls(pipeline, SERVICE, "render", "outgoing", depth=1)
    [node] = [n for n in render.structured["data"]["nodes"] if n["urn"] == U(ENGINE, "Packer.pack")]
    assert (node["confidence_level"], node["match"]) == ("heuristic", "name")
    start = await calls(pipeline, SERVICE, "start", "outgoing", depth=1)
    assert start.structured["data"]["nodes"] == []
    [u] = start.structured["data"]["unattributed_calls"]
    assert (u["direction"], u["name"], u["reason"]) == ("outgoing", "run", "ambiguous")
    lookup = await calls(pipeline, SERVICE, "lookup", "outgoing", depth=1)
    assert "unattributed_calls" not in lookup.structured["data"]  # outgoing は組み込み型と同名（get）を数えない
    typed = await calls(pipeline, SERVICE, "typed", "outgoing", depth=1)
    assert U(ENGINE, "Packer.pack") not in {n["urn"] for n in typed.structured["data"]["nodes"]}


def test_t07_exact_tree_gets_a_note_not_nodes(db: Path) -> None:
    conn = open_reader(db)
    try:
        sym = next(
            s
            for s in t07.queries.symbols_by_qualified_suffix(conn, WS, ENGINE, "Packer.pack")
            if s.qualified_name == "Packer.pack"
        )
        tree = t07._Tree(root=sym, language="python", tier=t07.Tier.T1)
        tree.roots["incoming"] = []
        t07._exact_name_note(nc.NameIndex(conn, WS), sym, tree, include_tests=False)
    finally:
        conn.close()
    assert tree.roots["incoming"] == []
    [u] = tree.unattributed
    assert (u["direction"], u["total"], u["reason"]) == ("incoming", 4, "not_in_exact_answer")
    [w] = tree.warnings
    assert w.startswith("incoming: 4 call site(s) of .pack() whose receiver type is unknown to the syntax tree")
