"""S-1 の 3（roots）と、SDK クライアント経由の 1・2 の往復を確かめる。
使い方: uv run --frozen python spikes/s01_mcp/probe_roots.py
  legacy = initialize ハンドシェイク（いまのホストが話す版）/ auto = 2026-07-28（roots・initialized・list_changed が廃止された版）
"""

from __future__ import annotations

import asyncio
import json
import sys
import warnings
from typing import Any

from mcp import Client, StdioServerParameters, types
from mcp.client.session import ClientRequestContext
from mcp.shared.exceptions import MCPDeprecationWarning
from rawclient import PY, SERVER
from schemas import ARGS10_SCHEMA, EMPTY_SCHEMA

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)
ok_all = True


def check(name: str, ok: bool, detail: str = "") -> None:
    global ok_all
    ok_all &= ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def params(*extra: str) -> StdioServerParameters:
    return StdioServerParameters(command=PY, args=[SERVER, "--divert=manual", *extra], env={"PYTHONUTF8": "1"})


def sc(res: types.CallToolResult) -> Any:
    return res.structured_content


async def legacy() -> None:
    print("== legacy（initialize ハンドシェイク）+ roots 対応クライアント")
    roots = [types.Root(uri="file:///S:/work/proj_a", name="a")]

    async def list_roots_cb(ctx: ClientRequestContext) -> types.ListRootsResult:
        return types.ListRootsResult(roots=list(roots))

    async with Client(params(), mode="legacy", list_roots_callback=list_roots_cb, cache=None) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        check("1  SDK クライアントで list_tools: noop スキーマ一致", tools["noop"].input_schema == EMPTY_SCHEMA)
        check("1  SDK クライアントで list_tools: validated スキーマ一致 + additionalProperties:false",
              tools["validated"].input_schema == ARGS10_SCHEMA and tools["validated"].input_schema["additionalProperties"] is False)  # fmt: skip
        r = await c.call_tool("structured")
        check("2  call_tool(structured): content/structuredContent", r.content[0].text.startswith("# structured") and sc(r)["ok"] is True and r.is_error is False)  # fmt: skip
        r = await c.call_tool("fail")
        check("2  call_tool(fail): is_error=True の通常結果（例外なし）", r.is_error is True and sc(r)["error"]["code"] == "E_TARGET_NOT_FOUND")  # fmt: skip

        st = sc(await c.call_tool("roots_state"))
        check("3a initialized の直後に roots/list を要求して取得できる", st["initial_roots"] == [str(r.uri) for r in roots], f"{st['initial_roots']} errors={st['roots_errors']}")  # fmt: skip
        check("3b クライアントが roots 能力を宣言していることをサーバーが読める", st["client_caps_roots"] == {"listChanged": True}, str(st["client_caps_roots"]))  # fmt: skip
        roots[:] = [types.Root(uri="file:///S:/work/proj_a", name="a"), types.Root(uri="file:///S:/work/proj_b", name="b")]
        await c.send_roots_list_changed()
        for _ in range(40):
            st = sc(await c.call_tool("roots_state"))
            if st["roots_after_change"] is not None:
                break
            await asyncio.sleep(0.05)
        check("3c notifications/roots/list_changed を受信し、再取得できる",
              st["list_changed_count"] == 1 and st["roots_after_change"] == [str(r.uri) for r in roots],
              f"count={st['list_changed_count']} roots={st['roots_after_change']}")  # fmt: skip
        r = await c.call_tool("roots_now")
        check("3d ツール呼び出しの最中にも roots/list を要求できる", not r.is_error and len(sc(r)["roots"]) == 2, str(sc(r)))

    print("== legacy + roots 非対応クライアント（宣言なし）")
    async with Client(params(), mode="legacy", cache=None) as c:
        st = sc(await c.call_tool("roots_state"))
        check("3e roots 非対応なら要求しない（能力は None）", st["client_caps_roots"] is None and st["initial_roots"] is None)
        r = await c.call_tool("roots_now")
        print("     (参考) 非対応クライアントへ roots/list を要求した結果:", json.dumps(sc(r), ensure_ascii=False))


async def modern() -> None:
    print("== auto（server/discover → 2026-07-28）")
    roots = [types.Root(uri="file:///S:/work/proj_a", name="a")]

    async def list_roots_cb(ctx: ClientRequestContext) -> types.ListRootsResult:
        return types.ListRootsResult(roots=list(roots))

    async with Client(params(), mode="auto", list_roots_callback=list_roots_cb, cache=None) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        check("1  [2026-07-28] list_tools: スキーマ一致", tools["validated"].input_schema == ARGS10_SCHEMA)
        r = await c.call_tool("fail")
        check("2  [2026-07-28] is_error=True の通常結果", r.is_error is True and sc(r)["error"]["code"] == "E_TARGET_NOT_FOUND")
        st = sc(await c.call_tool("roots_state"))
        print("     (参考) [2026-07-28] initialized 後フックでの roots 取得:", st["initial_roots"], "protocol=", st["protocol_version"])
        r = await c.call_tool("roots_now")
        print("     (参考) [2026-07-28] ツール中の ctx.session.list_roots():", json.dumps(sc(r), ensure_ascii=False))
        r = await c.call_tool("roots_mrtr")
        check("3f [2026-07-28] 結果に埋め込んだ要求(InputRequired)で roots を取得できる", not r.is_error and sc(r)["roots"] == [str(x.uri) for x in roots], str(sc(r)) if r.structured_content else str(r.content))  # fmt: skip


async def main() -> None:
    await legacy()
    try:
        await modern()
    except Exception as exc:  # noqa: BLE001
        check("modern 節が最後まで走る", False, f"{type(exc).__name__}: {exc}")
    print("\n" + ("ALL PASS" if ok_all else "FAIL あり"))
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    asyncio.run(main())
