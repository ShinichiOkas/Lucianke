"""開発用の ``ping`` ツール（``LUCIANKE_DEV_TOOLS=1`` のときだけ登録される。通常の ``tools/list`` には出ない）。

* ``echo`` をそのまま kv 行で返す（往復と描画の確認用）。
* ``noise=true`` なら ``print`` と ``os.write(1, …)`` をしてから応答する。stdout 隔離（ADR-3）の検査用で、
  これらが本物の stdout（JSON-RPC の流れ）に混ざらないことを ``tests/invariants/test_stdout_purity.py`` が確かめる。
"""

from __future__ import annotations

import os

from pydantic import Field

from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec


class PingArgs(CommonArgs):
    echo: str = Field(default="pong", description="Text returned as 'echo: <text>'.")
    noise: bool = Field(default=False, description="Dev only: write to fd 1 / sys.stdout before replying (isolation check).")


def _one_line(text: str) -> str:
    return text.replace("\r", "\\r").replace("\n", "\\n")


async def handler(ctx: ToolContext, args: PingArgs) -> ToolOutput:
    if args.noise:
        print("noise")
        os.write(1, b"noise\n")
    line = f"echo: {_one_line(args.echo)}"
    record = Record(
        kind="kv",
        key="echo",
        priority=0,
        lines=(line,),
        data={"echo": args.echo},
        confidence=Confidence.EXACT,
    )
    return ToolOutput(
        records=[record],
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="none", confidence=Confidence.EXACT),
        page_unit="line",
    )


SPEC = ToolSpec(
    name="ping",
    tid="DEV",
    args_model=PingArgs,
    handler=handler,
    default_max_tokens=500,
    description="Development-only echo tool (LUCIANKE_DEV_TOOLS=1). Returns 'echo: <text>'.",
    page_unit="line",
    records_key="lines",
)
