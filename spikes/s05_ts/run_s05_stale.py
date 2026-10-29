"""S-5 追加 3（使い捨て）: vtsls で「壊して元に戻す」と空配列でなく古い診断が届くか（run_s05_extra2.py で観測）を詰める。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "s04_lsp"))
from run_s05_extra import FIX, TS6, TSLS, VTSLS  # noqa: E402
from run_s05_extra2 import seq  # noqa: E402
from lsp_probe import LspClient  # noqa: E402


async def one(label, cmd, io):
    c = LspClient(name=label, cmd=cmd, cwd=FIX, config_provider=lambda it: {})
    await c.start()
    await c.initialize(init_options=io)
    f = FIX / "shapes.ts"
    st = f.read_text(encoding="utf-8")
    c.did_open(f, "typescript")
    await asyncio.sleep(2.5)
    R = {"label": label}
    i = len(c.notifications); c.did_change(f, 2, st + "\nexport const bad: number = 'x';\n"); R["break_v2"] = await seq(c, f, i, 2.0)
    i = len(c.notifications); c.did_change(f, 3, st); R["revert_v3_same_as_v1"] = await seq(c, f, i, 2.0)
    i = len(c.notifications); c.did_change(f, 4, st + "\n"); R["trailing_nl_v4"] = await seq(c, f, i, 2.0)
    i = len(c.notifications); c.did_change(f, 5, st + "\nexport const bad2: number = 'y';\n"); R["break_again_v5"] = await seq(c, f, i, 2.0)
    i = len(c.notifications); c.did_change(f, 6, st.replace("return 0;", "return 1;")); R["edit_other_v6_clean"] = await seq(c, f, i, 2.0)
    R["shutdown"] = await c.shutdown()
    return R


async def main():
    out = [await asyncio.wait_for(one("vtsls", VTSLS, None), 40)]
    out.append(await asyncio.wait_for(one("tsls", TSLS, {"tsserver": {"path": str(TS6 / "lib" / "tsserver.js")}}), 40))
    print(json.dumps(out, ensure_ascii=False))

asyncio.run(main())
