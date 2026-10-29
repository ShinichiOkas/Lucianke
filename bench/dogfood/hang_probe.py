"""観点 7 の台本が止まった呼び出しの特定。使い方: uv run --quiet python bench/dogfood/hang_probe.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run_dogfood as r  # noqa: E402


async def main() -> None:
    files = r.py_files()[:5]
    print(files, flush=True)
    rel = "src/lucianke/mcp/budget.py"
    cases = [
        ("skel5", "get_file_skeleton", {"paths": files, "max_tokens": 4000}),
        ("skel5_json", "get_file_skeleton", {"paths": files, "max_tokens": 4000, "format": "json"}),
        ("bodies5", "get_symbol_bodies", {"targets": [{"path": rel, "symbol_name": s} for s in ("Packer.pack", "CursorCodec.decode", "trailer_lines", "default_summary", "CursorState")]}),
        ("bodies5_json", "get_symbol_bodies", {"targets": [{"path": rel, "symbol_name": s} for s in ("Packer.pack", "CursorCodec.decode", "trailer_lines", "default_summary", "CursorState")], "format": "json"}),
    ]
    async with r.Client(r.params(), mode="legacy", cache=None) as c:
        for name, tool, args in cases:
            e, t, _sc, dt = await r.call(c, tool, args)
            print(name, e, round(dt), t[:100].replace("\n", " | "), flush=True)


asyncio.run(main())
