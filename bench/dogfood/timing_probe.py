"""往復時間の素早い下見。使い方: uv run --quiet python bench/dogfood/timing_probe.py （標準出力に p50/p95）"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_dogfood import Client, call, params  # noqa: E402


async def main() -> None:
    async with Client(params(), mode="legacy", cache=None) as c:
        for tool, args in (
            ("get_file_skeleton", {"paths": ["src/lucianke/mcp/budget.py"]}),
            ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer.pack"}]}),
        ):
            xs = []
            t0 = time.time()
            for _ in range(12):
                _e, _t, _sc, dt = await call(c, tool, args)
                xs.append(dt)
            print(tool, [round(x) for x in xs], f"wall={time.time() - t0:.1f}s", flush=True)


asyncio.run(main())
