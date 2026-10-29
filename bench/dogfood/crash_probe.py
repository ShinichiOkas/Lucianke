"""サーバーが get_symbol_bodies で落ちる件の再現。使い方: uv run --quiet python bench/dogfood/crash_probe.py N （N 回 skeleton を呼んでから bodies を 5 回）"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run_dogfood as r  # noqa: E402


async def main(n: int) -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # 応答の日本語や → を cp932 のパイプへ出して化けさせない（BUG-0038）
    rel = "src/lucianke/mcp/budget.py"
    async with r.Client(r.params(), mode="legacy", cache=None) as c:
        for _ in range(n):
            await r.call(c, "get_file_skeleton", {"paths": [rel]})
        for i in range(5):
            try:
                e, t, _sc, dt = await r.call(c, "get_symbol_bodies", {"targets": [{"path": rel, "symbol_name": "Packer.pack"}]})
                print("bodies", i, e, round(dt), t[:60].replace("\n", " | "), flush=True)
            except Exception as ex:  # noqa: BLE001
                print("bodies", i, "EXC", repr(ex)[:120], flush=True)
                break


asyncio.run(main(int(sys.argv[1])))
