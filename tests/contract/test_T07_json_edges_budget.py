"""BUG-0136: T-07 の json は、辺の多い木でも ``max_tokens`` に収まり、ノードと辺が食い違わず、予算を上げると単調に増える。

見えた不具合: 1 つのノードに呼び出し箇所が数十あるとき、json の ``data.edges`` が木全体の辺を ``data_static`` に持っていたため、
ノードを 1 件も詰める前に前置きだけで予算を食い、``nodes`` が 0 件・``edges`` が数十本・``used_tokens_est`` が ``max_tokens`` 超過・
``next_cursor`` なしで返った（compact は 2 行・約 75 トークンで返る）。

直し: 辺はそれを持つノードのレコードに付き（``Record.attached``）、json は入ったノードの辺だけを ``data.edges`` に連ねる。
入りきらないノードは呼び出し箇所を先頭から縮め（``call_sites_omitted`` と全部取る ``max_tokens`` の注）、ノードごと消えない。

構文木の経路（LSP なし）で、モジュールの関数が同じ関数を 80 回呼ぶ形（``many`` → ``client`` に辺が 80 本）を作る。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.app import build_services
from lucianke.core.tokens import TokenEstimator
from lucianke.core.urn import CodeSel, Urn
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t07_query_call_hierarchy as t07
from lucianke.tools._services import ToolServices
from lucianke.workspace.types import Workspace

SITES = 80
WS = "bug136"
EST = TokenEstimator()


@dataclass(slots=True)
class Env:
    pipe: Pipeline
    client: dict[str, Any]


@pytest.fixture
async def env(tmp_path: Path):
    root = tmp_path / "ws"
    (root / "app").mkdir(parents=True)
    (root / "app/__init__.py").write_text("", encoding="utf-8")
    (root / "app/lib.py").write_text("def client(x=None):\n    return x\n", encoding="utf-8")
    calls = "".join(f"    client({i})\n" for i in range(SITES))
    (root / "app/use.py").write_text(
        "from app.lib import client\n\n\ndef many():\n" + calls + "\n\ndef few():\n    client(1)\n\n\n"
        "def other():\n    client(2)\n    client(3)\n",
        encoding="utf-8",
    )
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS, root=root.resolve()), writer, readers, TreeCache())
    try:
        for rel in ("app/__init__.py", "app/lib.py", "app/use.py"):
            await indexer.index_file(rel)
        registry = ToolRegistry()
        registry.register(t07.TOOL_SPEC)
        base = build_services(lambda: [str(root)])

        @dataclass(slots=True)
        class Services(ToolServices):
            db_path: Path | None = None

        pipe = Pipeline(registry, services=Services(roots=base.roots, trees=base.trees, lines=base.lines, db_path=db))
        yield Env(pipe, {"urn": str(Urn(WS, "app/lib.py", CodeSel("client")))})
    finally:
        await indexer.aclose()
        readers.close()
        writer.close()


async def call(env: Env, **args: Any):
    return await env.pipe.call_tool(
        "query_call_hierarchy", {"target": env.client, "direction": "incoming", "format": "json", **args}
    )


def sites_of(data: dict[str, Any]) -> int:
    return sum(len(n["call_sites"]) for n in data["nodes"])


def assert_consistent(r: Any, max_tokens: int) -> None:
    """応答は ``max_tokens`` を超えず、辺は入ったノードの呼び出し箇所と 1 対 1 で、端点は入ったノードか根。"""
    assert not r.is_error, r.text
    assert EST.count(r.text) <= max_tokens, (EST.count(r.text), max_tokens)
    assert r.structured["budget"]["used_tokens_est"] <= max_tokens
    data = r.structured["data"]
    ends = {n["urn"] for n in data["nodes"]} | {data["root"]["urn"]}
    kept = {(n["urn"], s["path"], s["line"]) for n in data["nodes"] for s in n["call_sites"]}
    shown = {(e["source_urn"], e["call_site_path"], e["call_site_line"]) for e in data["edges"]}
    assert shown == kept, "辺とノードの呼び出し箇所が食い違う"
    assert len(data["edges"]) == len(kept)
    for e in data["edges"]:
        assert e["source_urn"] in ends and e["target_urn"] in ends


async def test_default_budget_returns_nodes_not_only_edges(env: Env) -> None:
    """票の再現: 既定の予算（2000）でノードが返り、辺はそのノードの分だけ。以前は nodes 0 件・edges が全部・予算超過。"""
    whole = await call(env, max_tokens=20000)
    assert sites_of(whole.structured["data"]) == SITES + 1 + 2  # 前提: 全部で 2000 トークンを超える大きさ
    assert whole.structured["budget"]["used_tokens_est"] > 2000
    r = await call(env)
    assert r.structured["budget"]["max_tokens"] == 2000
    assert_consistent(r, 2000)
    data = r.structured["data"]
    assert data["nodes"], r.text
    first = data["nodes"][0]
    assert first["name"] == "many" and first["call_sites_omitted"] > 0
    assert len(first["call_sites"]) + first["call_sites_omitted"] == SITES
    assert re.search(r"max_tokens>=\d+", first["call_sites_note"])
    assert r.structured["page"]["returned"] == len(data["nodes"]) >= 1


async def test_budget_is_monotone_and_never_exceeded(env: Env) -> None:
    prev_nodes = prev_sites = 0
    for mt in range(500, 6001, 250):
        r = await call(env, max_tokens=mt)
        if not r.structured["data"]["nodes"]:  # 小さすぎる予算は 0 件で、要る max_tokens を名指しする
            assert any("max_tokens" in w for w in r.structured["warnings"]), (mt, r.text)
            assert EST.count(r.text) <= mt or r.structured["page"]["returned"] == 0
            continue
        assert_consistent(r, mt)
        data = r.structured["data"]
        assert len(data["nodes"]) >= prev_nodes and sites_of(data) >= prev_sites, (mt, r.text[:200])
        prev_nodes, prev_sites = len(data["nodes"]), sites_of(data)
    assert prev_sites > 0


async def test_note_names_a_max_tokens_that_returns_the_whole_node(env: Env) -> None:
    r = await call(env, max_tokens=1000)
    first = r.structured["data"]["nodes"][0]
    need = int(re.search(r"max_tokens>=(\d+)", first["call_sites_note"]).group(1))
    again = await call(env, max_tokens=need)
    assert_consistent(again, need)
    top = again.structured["data"]["nodes"][0]
    assert top["name"] == "many" and len(top["call_sites"]) == SITES and "call_sites_omitted" not in top


async def test_pages_follow_the_cursor_without_losing_nodes(env: Env) -> None:
    whole = await call(env, max_tokens=20000)
    names = [n["name"] for n in whole.structured["data"]["nodes"]]
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        r = await call(env, max_tokens=1000, **({"cursor": cursor} if cursor else {}))
        assert_consistent(r, 1000)
        seen += [n["name"] for n in r.structured["data"]["nodes"]]
        cursor = r.structured["page"]["next_cursor"]
        if not cursor:
            break
    assert seen == names


async def test_compact_and_whole_json_are_unchanged(env: Env) -> None:
    c = await call(env, format="compact", max_tokens=2000)
    assert "many" in c.text and "few" in c.text and "other" in c.text
    whole = await call(env, max_tokens=20000)
    data = whole.structured["data"]
    assert len(data["edges"]) == SITES + 3
    assert all("call_sites_omitted" not in n for n in data["nodes"])
    assert {k for k in data} >= {"root", "direction", "depth", "nodes", "edges"}

