"""T-06 / T-07: 再び公開した名前を定義元までたどる（BUG-0100）・名前空間パッケージを外部にしない（BUG-0101）。

フィクスチャ ``tests/fixtures/reexports``（README に各箇所の意図）を ``Indexer`` で取り込んだ実索引（LSP なしの ``structural``）で確かめる。
パッケージの ``__init__.py`` が再び公開した関数・クラスを、そのパッケージから import して使う呼び出し元は、定義元のシンボルの参照・
呼び出し階層に ``structural`` で出る（パッケージの公開 API の呼び出し元が 0 件に見えない）。
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "reexports"
WS = "reexports"


def U(path: str, qualified: str | None = None) -> str:  # noqa: N802
    return str(Urn(WS, path, CodeSel(qualified) if qualified else None))


def line_of(path: str, needle: str) -> int:
    lines = (FIXTURE / path).read_bytes().decode("utf-8").splitlines()
    return next(i + 1 for i, text in enumerate(lines) if needle in text)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("reexports")
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


async def refs(pipeline: Pipeline, target: dict[str, Any], **args: Any) -> Any:
    r = await pipeline.call_tool(
        "find_references_and_implementations",
        {"target": target, "query_type": "references", "format": "json", **args},
    )
    assert not r.is_error, r.text
    return r


async def calls(pipeline: Pipeline, path: str, name: str, direction: str, **args: Any) -> Any:
    r = await pipeline.call_tool(
        "query_call_hierarchy",
        {"target": {"path": path, "symbol_name": name}, "direction": direction, "format": "json", **args},
    )
    assert not r.is_error, r.text
    return r


def sites(r: Any) -> set[tuple[str, int, str, str]]:
    return {(x["path"], x["line"], x["use"], x["confidence_level"]) for x in r.structured["data"]["references"]}


async def test_t06_reexported_function_lists_callers_through_the_package(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``pkg/helpers.py#flush`` の参照に、``from pkg import flush``・``from pkg import absolute``（絶対 import の別名）・
    ``from pkg.alias import fl``（普通のモジュールの別名）経由の呼び出しと読みが ``structural`` で出る（名前の一致ではない）。"""
    r = await refs(pipeline, {"path": "pkg/helpers.py", "symbol_name": "flush"})
    got = sites(r)
    for needle in ("    flush()", "fl()", "absolute()"):
        assert ("use.py", line_of("use.py", needle), "call", "structural") in got, (needle, got)
    assert ("use.py", line_of("use.py", "return flush"), "r", "structural") in got
    assert all(x.get("match") is None for x in r.structured["data"]["references"])
    assert not any(x["confidence_level"] == "heuristic" for x in r.structured["data"]["references"])


async def test_t06_two_hop_star_and_class_member(pipeline: Pipeline) -> None:
    """PC:T-06#2 2 段の ``deep_go``、``from .stars import *`` の ``starred``、再び公開したクラスのメソッド ``TokenEstimator.count``
    （構築した値の呼び出しと ``EST = TokenEstimator()`` の ``EST.count()``）と基底のメソッド ``Base.reset`` の呼び出し元。"""
    deep = sites(await refs(pipeline, {"path": "pkg/sub/impl.py", "symbol_name": "deep_go"}))
    assert ("use.py", line_of("use.py", "deep_go()"), "call", "structural") in deep
    starred = sites(await refs(pipeline, {"path": "pkg/stars.py", "symbol_name": "starred"}))
    assert ("use.py", line_of("use.py", "starred()"), "call", "structural") in starred
    hidden = sites(await refs(pipeline, {"path": "pkg/stars.py", "symbol_name": "hidden"}))
    assert not any(path == "use.py" for path, *_ in hidden)  # ``__all__`` に無いので ``*`` は公開しない
    count = sites(await refs(pipeline, {"path": "pkg/est.py", "symbol_name": "TokenEstimator.count"}))
    assert {
        ("use.py", line_of("use.py", 'TokenEstimator().count("x")'), "call", "structural"),
        ("use.py", line_of("use.py", 'EST.count("y")'), "call", "structural"),
    } <= count
    reset = sites(await refs(pipeline, {"path": "pkg/est.py", "symbol_name": "Base.reset"}))
    assert ("use.py", line_of("use.py", "EST.reset()"), "call", "structural") in reset


async def test_t07_incoming_and_outgoing_through_reexports(pipeline: Pipeline) -> None:
    """PC:T-07#2 ``deep_go`` の incoming に ``run``（``structural``）。``run`` の outgoing に定義元の関数・メソッドが並び、
    ``pkg/__init__.py`` の無い先・循環の ``loop``・公開しない ``hidden`` は出ない。"""
    r = await calls(pipeline, "pkg/sub/impl.py", "deep_go", "incoming", depth=1)
    got = {(n["urn"], n["confidence_level"]) for n in r.structured["data"]["nodes"]}
    assert (U("use.py", "run"), "structural") in got
    out = await calls(pipeline, "use.py", "run", "outgoing", depth=1)
    urns = {n["urn"] for n in out.structured["data"]["nodes"]}
    assert {
        U("pkg/helpers.py", "flush"),
        U("pkg/sub/impl.py", "deep_go"),
        U("pkg/stars.py", "starred"),
        U("pkg/loose.py", "public"),
        U("pkg/est.py", "TokenEstimator.count"),
        U("pkg/est.py", "Base.reset"),
    } <= urns
    assert not any("pkg/__init__.py#" in u or "cyc_" in u or u.endswith("#hidden") for u in urns)


async def test_t06_t07_namespace_package_has_no_external_package_references(pipeline: Pipeline) -> None:
    """PC:T-06#2 名前空間パッケージの ``render`` は 3 つの書き方（``nsp.tools.fmt.render()``・``nt.fmt.render()``・
    ``tools.fmt.render()``）のどれでも ``structural`` で出る（前は 2 つが外部 ``lk://_ext/nsp#tools.fmt.render`` だった）。
    ``run`` の outgoing にも外部の ``nsp`` は出ない（T-06 はモジュール・パッケージを対象に取らないので、ディレクトリそのものへの
    参照は索引に作らない。BUG-0101）。"""
    r = await refs(pipeline, {"path": "nsp/tools/fmt.py", "symbol_name": "render"})
    assert sites(r) == {
        ("use_ns.py", line_of("use_ns.py", "nsp.tools.fmt.render()"), "call", "structural"),
        ("use_ns.py", line_of("use_ns.py", "nt.fmt.render()"), "call", "structural"),
        ("use_ns.py", line_of("use_ns.py", "    tools.fmt.render()"), "call", "structural"),
    }
    out = await calls(pipeline, "use_ns.py", "run", "outgoing", depth=1)
    urns = {n["urn"] for n in out.structured["data"]["nodes"]}
    assert U("nsp/tools/fmt.py", "render") in urns
    assert not any(u == "lk://_ext/nsp" or u.startswith("lk://_ext/nsp#tools") for u in urns)
