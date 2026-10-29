"""観点 7 が終わらない原因の切り分け。使い方: uv run --quiet python bench/dogfood/loop_probe.py （進捗を標準出力）"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run_dogfood as r  # noqa: E402


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # 応答の日本語や → を cp932 のパイプへ出して化けさせない（BUG-0038）
    rel = "src/lucianke/mcp/budget.py"
    async with r.Client(r.params(), mode="legacy", cache=None) as c:
        for tool, args in (
            ("get_file_skeleton", {"paths": [rel]}),
            ("get_symbol_bodies", {"targets": [{"path": rel, "symbol_name": "Packer.pack"}]}),
        ):
            t0 = time.time()
            slow = []
            for i in range(200):
                _e, _t, _sc, dt = await r.call(c, tool, args)
                if dt > 200:
                    slow.append((i, round(dt)))
                if i % 50 == 49:
                    print(tool, i + 1, f"wall={time.time() - t0:.1f}s slow={slow[:6]}", flush=True)
            _e, d, dt = await r.jcall(c, tool, args)
            print("json", _e, round(dt), d.get("timing_ms"), flush=True)


asyncio.run(main())
