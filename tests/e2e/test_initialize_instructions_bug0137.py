"""BUG-0137: ``initialize`` が ``instructions`` を返し、載せたツール名が既定の ``tools/list`` に実在する。

ホストは instructions を毎セッションのシステムプロンプトに入れる。ツール名が消えたり改名されたりしたら、ここで落ちる。
起動は ``tests/_server_cmd.py``（``python -m lucianke``）。
"""

from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
from _server_cmd import server_argv  # noqa: E402


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


async def test_initialize_returns_instructions_naming_real_tools() -> None:
    argv = server_argv()
    params = StdioServerParameters(command=argv[0], args=argv[1:], env={"PYTHONUTF8": "1"}, cwd=str(REPO))
    async with Client(params, mode="legacy", cache=None) as c:
        text = c.instructions
        assert text and text.strip()
        assert len(text) < 2400  # 約 300 トークン以下。全セッションの固定費
        names = {t.name for t in (await c.list_tools()).tools}
    mentioned = set(re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", text))
    mentioned -= {"expected_file_sha256", "max_tokens", "dry_run"}  # 引数名
    assert mentioned, "ツール名が 1 つも載っていない"
    assert mentioned <= names, f"tools/list に無い名前: {mentioned - names}"
    assert {"get_repo_map", "get_file_skeleton", "get_symbol_bodies", "edit_symbol_body"} <= mentioned
