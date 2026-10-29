"""S-9: 往復オーバーヘッドの測定（何もしないツール / 約 8 KB を返すツール / pydantic 検証つきツール）。
使い方: uv run --frozen python spikes/s01_mcp/bench.py [N=500] [WARMUP=50] [REPEAT=3]    （全構成を REPEAT 回ずつ回す）
  RTT = クライアントが call を出してから結果を得るまで / mw = サーバーのミドルウェア内（SDK の検証・整形を含む）/ handler = ハンドラ本体
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import sys
import time
import warnings
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning
from pydantic import ValidationError
from rawclient import PY, SERVER, RawServer
from schemas import VALID_ARGS10, Args10

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 500
WARM = int(sys.argv[2]) if len(sys.argv) > 2 else 50
REPEAT = int(sys.argv[3]) if len(sys.argv) > 3 else 3
SCENARIOS: list[tuple[str, dict[str, Any]]] = [("noop", {}), ("big", {}), ("validated", VALID_ARGS10)]


def pct(xs_ns: list[int], p: float) -> float:
    xs = sorted(xs_ns)
    return xs[max(0, math.ceil(p / 100 * len(xs)) - 1)] / 1e6  # ms


def trio(xs_ns: list[int]) -> tuple[float, float, float]:
    return pct(xs_ns, 50), pct(xs_ns, 95), pct(xs_ns, 99)


Result = dict[str, tuple[list[int], list[int], list[int]]]  # scenario -> (rtt, middleware, handler) ns


async def bench_sdk(mode: str, extra: list[str]) -> Result:
    out: Result = {}
    params = StdioServerParameters(command=PY, args=[SERVER, "--divert=manual", *extra], env={"PYTHONUTF8": "1"})
    async with Client(params, mode=mode, cache=None) as c:
        await c.list_tools()
        for name, args in SCENARIOS:
            for _ in range(WARM):
                await c.call_tool(name, args)
            await c.call_tool("reset")
            rtt = []
            for _ in range(N):
                t = time.perf_counter_ns()
                r = await c.call_tool(name, args)
                rtt.append(time.perf_counter_ns() - t)
                assert not r.is_error
            st = (await c.call_tool("stats")).structured_content
            out[name] = (rtt, st["middleware_ns"][name], st["handler_ns"][name])
    return out


def bench_raw(extra: list[str]) -> Result:
    out: Result = {}
    s = RawServer(["--divert=manual", *extra])
    s.initialize()
    for name, args in SCENARIOS:
        for _ in range(WARM):
            s.call(name, args)
        s.call("reset")
        rtt = []
        for _ in range(N):
            t = time.perf_counter_ns()
            r = s.call(name, args)
            rtt.append(time.perf_counter_ns() - t)
            assert not r["result"]["isError"]
        st = s.call("stats")["result"]["structuredContent"]
        out[name] = (rtt, st["middleware_ns"][name], st["handler_ns"][name])
    s.close()
    return out


def micro_validate() -> None:
    """プロセス内の素の pydantic 検証コスト（通信なし）。"""
    k = 100_000
    raw = json.dumps(VALID_ARGS10)
    t = time.perf_counter_ns()
    for _ in range(k):
        Args10.model_validate(VALID_ARGS10)
    a = (time.perf_counter_ns() - t) / k / 1000
    t = time.perf_counter_ns()
    for _ in range(k):
        Args10.model_validate_json(raw)
    b = (time.perf_counter_ns() - t) / k / 1000
    t = time.perf_counter_ns()
    for _ in range(k):
        try:
            Args10.model_validate({**VALID_ARGS10, "bogus": 1})
        except ValidationError:
            pass
    c = (time.perf_counter_ns() - t) / k / 1000
    print(f"\n[micro] Args10(extra=forbid, 10 フィールド) 検証 1 回: model_validate(dict) {a:.1f} µs / model_validate_json {b:.1f} µs / 失敗(例外込み) {c:.1f} µs  (n={k})")


def first_calls() -> None:
    """起動直後（initialize の直後）の最初の数回。遅延 import や初回コンパイルが乗るかを見る。"""
    rows = []
    for _ in range(5):
        s = RawServer(["--divert=manual"])
        s.initialize()
        r = []
        for name, args in [("noop", {}), ("noop", {}), ("validated", VALID_ARGS10), ("validated", VALID_ARGS10), ("big", {})]:
            t = time.perf_counter_ns()
            s.call(name, args)
            r.append((time.perf_counter_ns() - t) / 1e6)
        rows.append(r)
        s.close()
    med = [statistics.median(x[i] for x in rows) for i in range(5)]
    print("\n[first calls] 起動直後の連続呼び出し（生パイプ, 5 回起動の中央値 ms）: noop#1 %.2f / noop#2 %.2f / validated#1 %.2f / validated#2 %.2f / big#1 %.2f" % tuple(med))


def report(label: str, runs: list[Result]) -> None:
    print(f"\n[{label}]  N={N} warmup={WARM} repeat={len(runs)}   値は ms。3 回の p50/p95/p99 の中央値（括弧内は p95 の最小〜最大）")
    print(f"{'scenario':10s} | {'RTT p50':>8s} {'p95':>7s} {'p99':>7s} {'(p95 range)':>16s} | {'mw p50':>7s} {'p95':>6s} {'p99':>6s} | {'handler p50':>11s} {'p95':>6s} {'p99':>6s}")
    for name, _ in SCENARIOS:
        def med(idx: int) -> tuple[float, float, float]:
            ts = [trio(r[name][idx]) for r in runs]
            return tuple(statistics.median(t[i] for t in ts) for i in range(3))  # type: ignore[return-value]

        rtt, mw, h = med(0), med(1), med(2)
        p95s = [trio(r[name][0])[1] for r in runs]
        print(f"{name:10s} | {rtt[0]:8.2f} {rtt[1]:7.2f} {rtt[2]:7.2f} {f'({min(p95s):.2f}-{max(p95s):.2f})':>16s} | {mw[0]:7.3f} {mw[1]:6.3f} {mw[2]:6.3f} | {h[0]:11.3f} {h[1]:6.3f} {h[2]:6.3f}")


def main() -> None:
    configs: list[tuple[str, Any]] = [
        ("SDK Client legacy / OTel あり(既定)", lambda: asyncio.run(bench_sdk("legacy", []))),
        ("SDK Client legacy / OTel なし", lambda: asyncio.run(bench_sdk("legacy", ["--no-otel"]))),
        ("SDK Client 2026-07-28(auto) / OTel あり", lambda: asyncio.run(bench_sdk("auto", []))),
        ("生パイプ client legacy / OTel あり(サーバー単体の下限)", lambda: bench_raw([])),
        ("生パイプ client legacy / OTel なし", lambda: bench_raw(["--no-otel"])),
    ]
    only = sys.argv[4] if len(sys.argv) > 4 else None
    for label, fn in configs:
        if only and only not in label:
            continue
        report(label, [fn() for _ in range(REPEAT)])
    micro_validate()
    first_calls()


if __name__ == "__main__":
    main()
