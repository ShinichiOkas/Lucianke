"""縦切り（T-02・T-05）の実機確認: ``.mcp.json`` と同じコマンドで ``lucianke`` を子プロセスとして起動し、SDK のクライアントで話す。

``tools/list`` に 2 ツールが載ること、リポジトリ自身のファイルの骨格と関数 1 つの本体が返ることを確かめる。
応答の compact テキストは ``-s`` で見える。
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "contract"))  # 文法検査器 compact_grammar を共有する
import compact_grammar as g  # noqa: E402


def _server_params() -> StdioServerParameters:
    """``.mcp.json`` の ``lucianke`` と同じコマンド（``--directory`` だけ、このチェックアウトの場所に置き換える）。"""
    spec = json.loads((REPO / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["lucianke"]
    args = list(spec["args"])
    args[args.index("--directory") + 1] = str(REPO)
    return StdioServerParameters(command=spec["command"], args=args, env={"PYTHONUTF8": "1"})


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


async def test_stdio_vertical_slice() -> None:
    async with Client(_server_params(), mode="legacy", cache=None) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert {"get_file_skeleton", "get_symbol_bodies"} <= set(tools)
        assert "ping" not in tools  # 開発用ツールは通常の tools/list に出ない
        assert tools["get_file_skeleton"].input_schema["additionalProperties"] is False

        skel = await c.call_tool(
            "get_file_skeleton", {"paths": ["src/lucianke/core/urn.py", "src/lucianke/mcp/pipeline.py"]}
        )
        text = skel.content[0].text
        print("\n" + "\n".join(text.splitlines()[:15]))
        assert not skel.is_error and g.validate(text) == []
        assert text.startswith("# get_file_skeleton ok conf=structural")
        assert re.search(r" ws=\S+ ", text.splitlines()[0]) and " ws=- " not in text.splitlines()[0]  # 解決後の ID（BUG-0004）
        assert "=== src/lucianke/core/urn.py (" in text and "=== src/lucianke/mcp/pipeline.py (" in text
        assert "[Function] pub parse(text: str) -> Urn" in text
        assert "[Class] pub Pipeline @L" in text

        body = await c.call_tool(
            "get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "workspace_id_from_dirname"}]}
        )
        btext = body.content[0].text
        assert " ws=- " not in btext.splitlines()[0]
        print("\n".join(btext.splitlines()[:15]))
        assert not body.is_error and g.validate(btext) == []
        assert "def workspace_id_from_dirname(name: str) -> str:" in btext
        assert re.search(r"^@L\d+: def workspace_id_from_dirname", btext, re.M)

        # 一部失敗: 全体は ok、失敗項目の件数が warnings[] に出る（§5.4.4、BUG-0004）
        part = await c.call_tool(
            "get_file_skeleton", {"paths": ["src/lucianke/core/urn.py", "no/such/file.py"], "format": "json"}
        )
        assert not part.is_error and part.structured_content["ok"] is True
        assert len(part.structured_content["warnings"]) == 1 and "1 of 2 items failed" in part.structured_content["warnings"][0]

        # 構造化結果（json）でも同じ本体
        j = await c.call_tool(
            "get_symbol_bodies",
            {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "workspace_id_from_dirname"}], "format": "json"},
        )
        item = j.structured_content["data"]["items"][0]
        src = (REPO / "src/lucianke/core/urn.py").read_text(encoding="utf-8").splitlines()
        rr = item["range_returned"]
        assert item["code"].split("\n") == src[rr["start_line"] - 1 : rr["end_line"]]
