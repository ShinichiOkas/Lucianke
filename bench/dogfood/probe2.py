"""probe.py の続き: 長い本体の切り詰め（T-05 手順 7）と続きの取得。使い方: uv run --quiet python bench/dogfood/probe2.py （結果は out/probe2.txt）"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_dogfood import OUT, Client, call, jcall, params  # noqa: E402


async def main():
    out = []
    async with Client(params(), mode="legacy", cache=None) as c:
        tgt = {"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer"}
        e, d, _ = await jcall(c, "get_symbol_bodies", {"targets": [tgt]})
        it = d["data"]["items"][0]
        out.append("json Packer default budget: " + json.dumps({"page": d["page"], "budget": d["budget"], "warnings": d["warnings"], "truncated": it.get("truncated"), "truncated_at_line": it.get("truncated_at_line"), "range_returned": it.get("range_returned"), "symbol_range": it["symbol"]["range"], "code_lines": it["code"].count("\n") + 1}, ensure_ascii=False))
        e, t, _, _ = await call(c, "get_symbol_bodies", {"targets": [tgt]})
        out.append("compact tail:\n" + "\n".join(t.splitlines()[-4:]))
        cur = re.search(r"cursor=(\S+)", t)
        if cur:
            e, t2, _, _ = await call(c, "get_symbol_bodies", {"targets": [tgt], "cursor": cur.group(1)})
            out.append("continued with cursor:\n" + "\n".join(t2.splitlines()[:5]) + "\n...\n" + "\n".join(t2.splitlines()[-3:]))
        # line_range で続きを取る
        e, t3, _, _ = await call(c, "get_symbol_bodies", {"targets": [tgt], "line_range": {"start": 325, "end": 340}, "context_lines": 0})
        out.append("line_range 325-340:\n" + "\n".join(t3.splitlines()[:6]))
        # 骨格の予算で落とす順序: 500 トークンで 1 ファイル（3000 トークン級）
        e, t4, _, _ = await call(c, "get_file_skeleton", {"paths": ["src/lucianke/mcp/budget.py"], "max_tokens": 500})
        out.append("skeleton budget.py @500:\n" + t4[:2500])
        e, t5, _, _ = await call(c, "get_file_skeleton", {"paths": ["src/lucianke/mcp/budget.py"], "max_tokens": 1000})
        out.append("skeleton budget.py @1000 (head/tail):\n" + "\n".join(t5.splitlines()[:6]) + "\n...\n" + "\n".join(t5.splitlines()[-4:]))
        out.append("recount used header vs est: " + t5.splitlines()[0])
    (OUT / "probe2.txt").write_text("\n\n".join(out), encoding="utf-8")

asyncio.run(main())
