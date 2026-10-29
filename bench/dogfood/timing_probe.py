"""観点 7（往復時間）の簡易版。使い方: uv run --quiet python bench/dogfood/timing_probe.py （標準出力に p50/p95。ウォームアップ 15 回のあと 100 回）"""
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_dogfood import Client, call, jcall, params  # noqa: E402

REL = "src/lucianke/mcp/budget.py"
CASES = (
    ("skeleton_1file", "get_file_skeleton", {"paths": [REL]}),
    ("bodies_1", "get_symbol_bodies", {"targets": [{"path": REL, "symbol_name": "Packer.pack"}]}),
)


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return round(s[min(len(s) - 1, round(p * (len(s) - 1)))], 1)


async def main() -> None:
    for name, tool, args in CASES:
        async with Client(params(), mode="legacy", cache=None) as c:
            for _ in range(15):
                await call(c, tool, args)
            rt, srv = [], []
            for _ in range(100):
                _e, _t, _sc, dt = await call(c, tool, args)
                rt.append(dt)
            for _ in range(100):
                _e, d, _dt = await jcall(c, tool, args)
                if isinstance(d.get("timing_ms"), (int, float)):
                    srv.append(d["timing_ms"])
            print(name, "roundtrip p50/p95/max", pct(rt, .5), pct(rt, .95), round(max(rt), 1),
                  "| server timing_ms p50/p95/max", pct(srv, .5), pct(srv, .95), max(srv), "n", len(srv),
                  "| mean", round(statistics.mean(rt), 1), flush=True)


asyncio.run(main())
