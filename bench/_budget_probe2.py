"""BUG-0053/0054 symptom via contract test pipeline.

Run: PYTHONUTF8=1 uv run pytest bench/_bug_triage2.py -s -q

Uses the same DB-backed pipeline as test_T06_T07.py.
"""
from __future__ import annotations
import asyncio
import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.app import build_services
from lucianke.core.model import Confidence, RelPath, Tier
from lucianke.core.textpos import LineIndex
from lucianke.core.tokens import TokenEstimator
from lucianke.core.urn import CodeSel, Urn
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import open_writer
from lucianke.index.ingest import FileFacts, RelationFact, ingest_file, upsert_workspace
from lucianke.index.migrate import ensure_schema
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import t06_find_references_and_implementations as t06
from lucianke.tools import t07_query_call_hierarchy as t07
from lucianke.tools._services import ToolServices, analyze_file
from lucianke.workspace.types import Workspace

POLY = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "poly"
WS = "poly"
PAY = "src/shop/services/payment.py"
LIM = "src/shop/services/limits.py"
ROUTES = "src/shop/api/routes.py"
TESTS = "tests/test_payment.py"
FILES = (PAY, LIM, ROUTES, TESTS)
RST = "docs/api.rst"

PROCESS_CHARGE = "PaymentProcessor.process_charge"
est = TokenEstimator()


def U(path: str, qualified: str | None = None) -> str:
    return str(Urn(WS, path, CodeSel(qualified) if qualified else None))


def lines_of(path: str):
    return (POLY / path).read_text(encoding="utf-8").splitlines()


def find_line(path: str, needle: str, nth: int = 0) -> int:
    hits = [i + 1 for i, text in enumerate(lines_of(path)) if needle in text]
    return hits[nth]


def tx(conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], Any]) -> Any:
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(conn)
        conn.execute("UPDATE _meta SET data_version = data_version + 1")
        conn.execute("COMMIT")
        return result
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def build_db(db: Path) -> None:
    svc = build_services(lambda: [str(POLY)])
    ws = svc.workspace()

    async def analyse():
        return {p: await analyze_file(svc, ws, p) for p in FILES}

    analysed = asyncio.run(analyse())

    conn = open_writer(db)
    try:
        ensure_schema(conn)
        tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS, "root_path": str(POLY)}))
        for p in FILES:
            an = analysed[p]
            facts = FileFacts(
                workspace_id=WS,
                path=an.rel,
                language=an.language_id,
                tier=an.tier,
                line_count=an.line_count,
                byte_size=len(an.data),
                sha256=an.sha256,
                is_test=p.startswith("tests/"),
                confidence=Confidence.STRUCTURAL,
                symbols=an.symbols,
                relations=[],
            )
            tx(conn, lambda c, f=facts: ingest_file(c, f))
        rst = (POLY / RST).read_bytes()
        rst_facts = FileFacts(
            workspace_id=WS,
            path=RelPath(RST),
            language="rst",
            tier=Tier.T3,
            line_count=LineIndex(rst).content_line_count,
            byte_size=len(rst),
            sha256=hashlib.sha256(rst).hexdigest(),
            is_test=False,
            confidence=Confidence.LEXICAL_FALLBACK,
        )
        tx(conn, lambda c: ingest_file(c, rst_facts))
    finally:
        conn.close()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("triage") / "triage.sqlite"
    build_db(path)
    return path


@pytest.fixture
def pipeline(db_path):
    registry = ToolRegistry()
    registry.register(t06.TOOL_SPEC)
    registry.register(t07.TOOL_SPEC)
    base = build_services(lambda: [str(POLY)])

    @dataclass(slots=True)
    class Svc(ToolServices):
        db_path: Path | None = None

    svc = Svc(roots=base.roots, trees=base.trees, lines=base.lines, db_path=db_path)
    return Pipeline(registry, services=svc)


async def test_bug_0053_dot_mermaid(pipeline):
    """BUG-0053: dot/mermaid must respect max_tokens."""
    print("")
    for fmt in ("dot", "mermaid"):
        for budget in (200, 600):
            r = await pipeline.call_tool("query_call_hierarchy", {
                "target": {"path": PAY, "symbol_name": PROCESS_CHARGE},
                "direction": "both",
                "depth": 5,
                "format": fmt,
                "max_tokens": budget,
            })
            actual = est.count(r.text)
            header = r.text.splitlines()[0][:100] if r.text else ""
            print(f"  {fmt} max_tokens={budget}  actual={actual}  ok={actual <= budget}")
            print(f"    header: {header}")
            assert actual <= budget, f"{fmt} max_tokens={budget}: got {actual} tokens"


async def test_bug_0054_json_fixed_part(pipeline):
    """BUG-0054: json used_tokens_est must be <= max_tokens=200."""
    print("")
    for tool_name, args in [
        ("query_call_hierarchy", {
            "target": {"path": PAY, "symbol_name": PROCESS_CHARGE},
            "direction": "both",
            "depth": 5,
            "format": "json",
            "max_tokens": 200,
        }),
        ("find_references_and_implementations", {
            "urn": U(PAY, PROCESS_CHARGE),
            "format": "json",
            "max_tokens": 200,
        }),
    ]:
        r = await pipeline.call_tool(tool_name, args)
        used = r.structured["budget"]["used_tokens_est"]
        returned = r.structured["page"]["returned"]
        warnings = r.structured.get("warnings", [])
        print(f"  {tool_name}  used={used}  returned={returned}  ok={used <= 200}")
        if warnings:
            print(f"    warn: {warnings[0][:120]}")
        assert used <= 200, f"{tool_name}: used={used} > max_tokens=200"
