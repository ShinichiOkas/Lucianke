"""BUG-0053/0054 symptom via contract test pipeline.

Run: PYTHONUTF8=1 uv run pytest bench/_bug_triage2.py -s -q

Uses the same DB-backed pipeline as test_T06_T07.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
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
from lucianke.index.connection import open_writer
from lucianke.index.ingest import FileFacts, ingest_file, upsert_workspace
from lucianke.index.migrate import ensure_schema
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import t06_find_references_and_implementations as t06
from lucianke.tools import t07_query_call_hierarchy as t07
from lucianke.tools._services import ToolServices, analyze_file

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


async def test_bug_0063_graph_shrinks_or_zero(pipeline):
    """BUG-0063 (A): dot/mermaid shrink the graph to fit; if even the preamble cannot fit
    the smallest max_tokens, the response is still produced (no crash) and over-budget is
    confined to the irreducible preamble. The larger budget (600) must fit."""
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
            assert r.is_error is False, f"{fmt} max_tokens={budget}: unexpected error response"
            if budget >= 600:
                assert actual <= budget, f"{fmt} max_tokens={budget}: got {actual} tokens"


async def test_bug_0063_json_zero_fit_warns_with_required_max_tokens(pipeline):
    """BUG-0063 (A): when the preamble alone exceeds max_tokens, json returns 0 records,
    is_error=False, and a warning naming the max_tokens needed to call again."""
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
            "target": {"path": PAY, "symbol_name": PROCESS_CHARGE},
            "format": "json",
            "max_tokens": 200,
        }),
    ]:
        r = await pipeline.call_tool(tool_name, args)
        used = r.structured["budget"]["used_tokens_est"]
        returned = r.structured["page"]["returned"]
        warnings = r.structured.get("warnings", [])
        print(f"  {tool_name}  used={used}  returned={returned}")
        if warnings:
            print(f"    warn: {warnings[0][:120]}")
        # (A): preamble can exceed the smallest budget; the response is still a success with 0 records.
        assert r.is_error is False, f"{tool_name}: unexpected error response"
        assert returned == 0, f"{tool_name}: expected 0 records when preamble exceeds budget, got {returned}"
        assert warnings, f"{tool_name}: expected a warning naming the required max_tokens"
        assert "raise max_tokens to" in warnings[0], f"{tool_name}: warning missing the required max_tokens hint: {warnings[0]!r}"
        # BUG-0064: the named max_tokens must be actionable: calling again with it fits without the warning.
        need = int(re.search(r"raise max_tokens to ~(\d+)", warnings[0]).group(1))
        r2 = await pipeline.call_tool(tool_name, {**args, "max_tokens": need})
        used2 = r2.structured["budget"]["used_tokens_est"]
        warn2 = r2.structured.get("warnings", [])
        print(f"    re-call max_tokens={need}  used={used2}  warnings={len(warn2)}")
        assert r2.is_error is False and used2 <= need, f"{tool_name}: re-call with max_tokens={need} used {used2}"
        assert not any("raise max_tokens to" in w for w in warn2), f"{tool_name}: re-call still over budget: {warn2!r}"
