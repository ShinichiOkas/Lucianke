"""minimal_server.py が S-1 の 1〜4 を満たすことを確かめる（ドキュメントのスニペットが本当に動く証拠）。
使い方: uv run --frozen python spikes/s01_mcp/probe_minimal.py
  SDK クライアントで list_tools / call_tool / roots、生パイプで stdout の純度（print と os.write を混ぜた上で全行 JSON-RPC）を検査する。
"""

from __future__ import annotations

import asyncio
import json
import warnings

from mcp import Client, StdioServerParameters, types
from mcp.shared.exceptions import MCPDeprecationWarning
from rawclient import HERE, PY, RawServer

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)
MINIMAL = str(HERE / "minimal_server.py")


async def sdk_part() -> None:
    roots = [types.Root(uri="file:///S:/work/x", name="x")]

    async def cb(ctx):  # type: ignore[no-untyped-def]
        return types.ListRootsResult(roots=roots)

    params = StdioServerParameters(command=PY, args=[MINIMAL], env={"PYTHONUTF8": "1"})
    async with Client(params, mode="legacy", list_roots_callback=cb, cache=None) as c:
        t = (await c.list_tools()).tools[0]
        assert t.input_schema["additionalProperties"] is False, t.input_schema
        r = await c.call_tool("ping", {"text": "hi"})
        assert r.structured_content["data"]["text"] == "hi" and not r.is_error
        r = await c.call_tool("ping", {"bogus": 1})
        assert r.is_error and r.structured_content["error"]["code"] == "E_INVALID_ARGUMENT", r
    print("PASS  minimal_server (SDK client): list_tools の additionalProperties:false / structuredContent / isError")


def raw_part() -> None:
    def on_server_request(m: dict) -> dict:
        assert m["method"] == "roots/list", m
        return {"roots": [{"uri": "file:///S:/work/x", "name": "x"}]}

    s = RawServer([], server=MINIMAL, on_server_request=on_server_request)
    s.initialize({"roots": {"listChanged": True}})
    s.call("ping", {})
    s.call("ping", {"text": "x"})
    rc, dt = s.close()
    ok = all(json.loads(line).get("jsonrpc") == "2.0" and not line.endswith(b"\r\n") for line in s.stdout_lines)
    assert rc == 0 and ok, (rc, s.stdout_lines)
    methods = [m.get("method") for m in s.incoming]
    print(f"PASS  minimal_server (raw): stdout {len(s.stdout_lines)} 行すべて JSON-RPC・LF のみ / 終了 {rc} ({dt * 1000:.0f} ms) / "
          f"サーバーからの要求={methods}")  # fmt: skip


if __name__ == "__main__":
    asyncio.run(sdk_part())
    raw_part()
