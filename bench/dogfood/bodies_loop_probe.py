"""get_symbol_bodies の連続呼び出しが止まる件の再現。使い方: uv run --quiet python bench/dogfood/bodies_loop_probe.py （進捗を標準出力。25 秒で諦める呼び出しの番号を出す）"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run_dogfood as r  # noqa: E402


async def main() -> None:
    rel = "src/lucianke/mcp/budget.py"
    args = {"targets": [{"path": rel, "symbol_name": "Packer.pack"}]}
    async with r.Client(r.params(), mode="legacy", cache=None) as c:
        for i in range(120):
            e, t, _sc, dt = await r.call(c, "get_symbol_bodies", args)
            if e or dt > 1000:
                print("call", i, "is_error", e, round(dt), t[:120].replace("\n", " | "), flush=True)
                break
            if i % 20 == 19:
                print("ok", i + 1, flush=True)


asyncio.run(main())
