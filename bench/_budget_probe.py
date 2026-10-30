"""BUG-0053/0054/0055 symptom measurement script.

Run: PYTHONUTF8=1 uv run python bench/_bug_triage.py

Non-ASCII chars are intentionally avoided in this file.
"""
from __future__ import annotations
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lucianke.app import build_services
from lucianke.core.tokens import TokenEstimator
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import register_tools

POLY = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "poly"
est = TokenEstimator()


async def main() -> None:
    registry = ToolRegistry()
    register_tools(registry)
    pl = Pipeline(registry, services=build_services(lambda: [str(POLY)]))

    print("=== BUG-0053: dot / mermaid max_tokens ===")
    print(f"  poly path: {POLY}")

    for budget in (200, 600):
        r = await pl.call_tool("query_call_hierarchy", {
            "target": {"path": "src/shop/services/payment.py", "symbol_name": "PaymentProcessor.process_charge"},
            "direction": "both",
            "depth": 5,
            "format": "dot",
            "max_tokens": budget,
        })
        actual = est.count(r.text)
        header = r.text.splitlines()[0][:80] if r.text else ""
        print(f"  dot  max_tokens={budget}  actual_tokens={actual}  ok={actual <= budget}")
        print(f"    header: {header}")
        print(f"    is_error: {r.is_error}")

    for budget in (200, 600):
        r = await pl.call_tool("query_call_hierarchy", {
            "target": {"path": "src/shop/services/payment.py", "symbol_name": "PaymentProcessor.process_charge"},
            "direction": "both",
            "depth": 5,
            "format": "mermaid",
            "max_tokens": budget,
        })
        actual = est.count(r.text)
        header = r.text.splitlines()[0][:80] if r.text else ""
        print(f"  mermaid max_tokens={budget}  actual_tokens={actual}  ok={actual <= budget}")
        print(f"    header: {header}")
        print(f"    is_error: {r.is_error}")

    print("\n=== BUG-0054: json fixed part over min max_tokens ===")

    for tool_call, args in [
        ("query_call_hierarchy", {
            "target": {"path": "src/shop/services/payment.py", "symbol_name": "PaymentProcessor.process_charge"},
            "direction": "both",
            "depth": 5,
            "format": "json",
            "max_tokens": 200,
        }),
        ("find_references_and_implementations", {
            "urn": "lk://poly/src/shop/services/payment.py#PaymentProcessor.process_charge",
            "format": "json",
            "max_tokens": 200,
        }),
        ("resolve_symbols", {
            "targets": [{"path": "src/shop/services/payment.py", "symbol_name": "PaymentProcessor.process_charge"}],
            "format": "json",
            "max_tokens": 200,
        }),
    ]:
        r = await pl.call_tool(tool_call, args)
        if r.structured:
            used = r.structured["budget"]["used_tokens_est"]
            returned = r.structured["page"]["returned"] if "page" in r.structured else "?"
            warnings = r.structured.get("warnings", [])
        else:
            used = est.count(r.text)
            returned = "?"
            warnings = []
        ok = used <= 200
        print(f"  {tool_call}  used={used}  returned={returned}  ok={ok}")
        if warnings:
            print(f"    warnings: {warnings[0][:100]}")

    print("\n=== BUG-0055: get_file_skeleton json 3x compact ===")
    print("  using payment.py (largest poly file)")

    for budget in (50000, 2000, 200):
        for fmt in ("compact", "json"):
            r = await pl.call_tool("get_file_skeleton", {
                "paths": ["src/shop/services/payment.py"],
                "format": fmt,
                "max_tokens": budget,
            })
            if fmt == "json" and r.structured:
                used = r.structured["budget"]["used_tokens_est"]
                returned = r.structured["page"]["returned"]
                warnings = r.structured.get("warnings", [])
            else:
                used = est.count(r.text)
                returned = "?"
                warnings = []
            ok = used <= budget
            print(f"  {fmt:8s} max_tokens={budget}  used={used}  returned={returned}  ok={ok}")
            if warnings:
                print(f"    warnings: {warnings[0][:100]}")

if __name__ == "__main__":
    asyncio.run(main())
