"""bench/run_latency.py の判定: LSP の exact 経路のツールは冷（その実行で初めての対象）と温（2 回目以降）に分けて判定する。

仕様書 §14.4・§17 OD-23 (b)。冷は p95 < 1 秒（深さ 2 の T-07 は < 2 秒）、温は p95 < 50 ms、両方とも合格で合格。それ以外の
Fast Path のツールは今までどおり全呼び出しの p95 < 50 ms。合成した記録で確かめる（サーバーは立てない）。
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("run_latency", REPO / "bench" / "run_latency.py")
assert _spec and _spec.loader
rl = importlib.util.module_from_spec(_spec)
sys.modules["run_latency"] = rl
_spec.loader.exec_module(rl)


def plan(tool: str) -> Any:
    return next(p for p in rl.PLANS if p.tool == tool)


def rec(target: str, ms: float, *, ok: bool = True, degraded: bool = False) -> dict[str, Any]:
    return {"target": target, "server_ms": ms, "rtt_ms": ms + 1, "ok": ok, "confidence": "exact",
            "degraded": degraded, "lsp": "ready", "error": None}  # fmt: skip


def test_only_the_lsp_exact_tools_are_split_with_their_cold_thresholds() -> None:
    """T-06・T-07・T-08・T-11（dry_run の rename）だけを冷・温に分ける。T-11 の台本は dry_run で呼ぶ。"""
    split = {p.tool: p.cold_ms for p in rl.PLANS if p.cold_ms is not None}
    assert split == {
        "find_references_and_implementations": 1000,
        "query_call_hierarchy": 2000,  # 台本は depth 2
        "get_diagnostics": 1000,
        "rename_symbol": 1000,  # T-11 の dry_run（OD-23 (b) に 2026-10-31 に足した）
    }
    args, _ = rl._a_calls(random.Random(0), rl.Pool(["a.py"], [rl.Sym("a.py", "f", "f", "function")]))
    assert args["depth"] == 2
    renamed, _ = rl._a_rename(random.Random(0), rl.Pool(["a.py"], [rl.Sym("a.py", "f", "f", "function")]))
    assert renamed["dry_run"] is True


def test_mark_first_counts_probe_and_prime_targets_as_asked() -> None:
    seen = {"a#f"}  # 探り・準備で聞いた対象
    samples = [rec("a#f", 5), rec("b#g", 900), rec("b#g", 6), rec("c#h", 700)]
    rl.mark_first(seen, samples)
    assert [s["first"] for s in samples] == [False, True, False, True]
    assert seen == {"a#f", "b#g", "c#h"}


def test_split_tool_passes_when_cold_is_under_1s_and_warm_under_50ms() -> None:
    samples = [rec(f"t{i}", 800) for i in range(20)] + [rec(f"t{i % 20}", 10) for i in range(80)]
    rl.mark_first(set(), samples)
    res = rl.judge(plan("find_references_and_implementations"), samples)
    assert res["verdict"] == "pass", res["reason"]
    assert res["cold"]["n"] == 20 and res["cold"]["threshold_ms"] == 1000 and res["cold"]["verdict"] == "pass"
    assert res["warm"]["n"] == 80 and res["warm"]["threshold_ms"] == 50 and res["warm"]["verdict"] == "pass"
    assert res["judged"]["server_ms"]["p95"] >= 50  # 全体の p95 は 50 ms を超えるが、判定は冷・温で行う


def test_split_tool_fails_when_warm_is_slow_even_if_cold_passes() -> None:
    samples = [rec("t0", 300)] + [rec("t0", 120) for _ in range(40)]
    rl.mark_first(set(), samples)
    res = rl.judge(plan("query_call_hierarchy"), samples)
    assert res["verdict"] == "fail"
    assert res["cold"]["verdict"] == "pass" and res["warm"]["verdict"] == "fail"
    assert "warm server p95" in res["reason"]


def test_split_tool_fails_when_cold_exceeds_its_threshold() -> None:
    samples = [rec(f"t{i}", 2500) for i in range(10)] + [rec("t0", 5) for _ in range(10)]
    rl.mark_first(set(), samples)
    assert rl.judge(plan("query_call_hierarchy"), samples)["cold"]["verdict"] == "fail"
    assert rl.judge(plan("query_call_hierarchy"), samples)["verdict"] == "fail"


def test_degraded_and_failed_calls_stay_out_of_both_populations() -> None:
    samples = [rec("t0", 9000, degraded=True), rec("t1", 9000, ok=False), rec("t2", 400), rec("t2", 8)]
    rl.mark_first(set(), samples)
    res = rl.judge(plan("get_diagnostics"), samples)
    assert res["cold"]["n"] == 1 and res["warm"]["n"] == 1 and res["verdict"] == "pass"


def test_an_empty_warm_population_does_not_fail_the_tool() -> None:
    samples = [rec(f"t{i}", 200) for i in range(30)]
    rl.mark_first(set(), samples)
    res = rl.judge(plan("get_diagnostics"), samples)
    assert res["warm"]["verdict"] == "no_samples" and res["verdict"] == "pass"


def test_other_fast_tools_are_judged_on_all_calls() -> None:
    samples = [rec(f"t{i}", 60) for i in range(10)] + [rec("t0", 5) for _ in range(90)]
    rl.mark_first(set(), samples)
    res = rl.judge(plan("get_file_skeleton"), samples)
    assert "cold" not in res and "warm" not in res
    assert res["verdict"] == "fail" and res["cold_threshold_ms"] is None  # 冷の分けが無いので全呼び出しの p95 で判定
