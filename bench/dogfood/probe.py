"""個別の呼び出しを 1 回ずつ確かめる補助。使い方: uv run --quiet python bench/dogfood/probe.py  （結果は bench/dogfood/out/probe.txt、UTF-8）"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_dogfood import OUT, Client, call, params  # noqa: E402

CASES = [
    ("skel_partial_compact", "get_file_skeleton", {"paths": ["src/lucianke/core/urn.py", "no/such.py"], "max_tokens": 300}),
    ("skel_all_fail_two", "get_file_skeleton", {"paths": ["no/a.py", "no/b.py"]}),
    ("body_partial_compact", "get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "nope"}, {"path": "src/lucianke/core/urn.py", "symbol_name": "parse"}], "max_tokens": 400}),
    ("body_class_default_budget", "get_symbol_bodies", {"targets": [{"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer"}]}),
    ("body_class_json_default_budget", "get_symbol_bodies", {"targets": [{"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer"}], "format": "json"}),
    ("body_mid_budget_600", "get_symbol_bodies", {"targets": [{"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer.pack"}], "max_tokens": 600}),
    ("body_decorated_property", "get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "Urn.is_external"}]}),
    ("skel_include_flags", "get_file_skeleton", {"paths": ["src/lucianke/core/urn.py"], "include_private": False, "include_doc_summary": False, "include_urns": "none", "include_imports": False, "max_tokens": 600}),
    ("skel_one_big_default", "get_file_skeleton", {"paths": ["src/lucianke/mcp/budget.py", "src/lucianke/syntax/relations.py"]}),
]

async def main():
    out = []
    async with Client(params(), mode="legacy", cache=None) as c:
        for name, tool, args in CASES:
            e, t, sc, dt = await call(c, tool, args)
            out.append(f"##### {name} is_error={e} {dt:.0f}ms\n{t[:3500]}\n")
    (OUT / "probe.txt").write_text("\n".join(out), encoding="utf-8")

asyncio.run(main())
