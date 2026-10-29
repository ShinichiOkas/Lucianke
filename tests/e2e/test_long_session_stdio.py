"""長いセッションの実機確認（BUG-0020）: 実プロセスの ``lucianke`` を stdio の MCP クライアントでつなぎ、同じセッションで 1,000 回呼んでも
止まらないことを確かめる。

BUG-0020 の原因は 2 つあり、どちらもここで押さえる。

(a) 構文木が解析時の ``read_callback``（5 秒の期限つき）を保持し、``Node.text`` のたびに呼び直す。期限を過ぎると ``b""`` を返して
    ``Node.text`` が戻らず、イベントループが空回りした。``get_symbol_bodies`` がコメントの ``Node.text`` を読むので、最初の解析から
    5 秒後の呼び出しで無応答になった（``test_symbol_bodies_after_the_parse_deadline``、``test_one_thousand_calls_with_unread_stderr``）。
(b) ホストが stderr のパイプを読まないと（Windows は約 4 KB）、呼び出しごとのログ 1 行でバッファが満杯になり、書き込みでループが
    止まった（``test_one_thousand_calls_with_unread_stderr``。クライアントは stderr のパイプを作って **誰も読まない**）。

``get_file_skeleton`` と ``get_symbol_bodies`` を交互に、json と compact を混ぜて呼ぶ。待ちは呼び出しごとのタイムアウトのみ。
"""

from __future__ import annotations

import asyncio
import os
import time
import warnings
from pathlib import Path
from typing import Any, TextIO

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPDeprecationWarning

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

CALLS = 1000
CALL_TIMEOUT_S = 30.0  # 1 回の呼び出しは数十 ms。負荷のある機でも余裕を持たせた上限（空回りなら永久に返らない）
PARSE_DEADLINE_S = 5.0  # syntax/parsers.py の既定の解析期限（STRUCTURAL_SEARCH_MS）

MODULE = '''\
"""sample module"""

# leading comment for alpha
def alpha(x: int) -> int:
    """add one"""
    return x + 1


# leading comment for Greeter
class Greeter:
    # about greet
    def greet(self, name: str) -> str:
        return f"hi {name}"

    # about shout
    def shout(self, name: str) -> str:
        return self.greet(name).upper()


# trailing comment
def omega() -> None:
    pass
'''

SYMBOLS = ["alpha", "Greeter.greet", "Greeter.shout", "omega"]


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "mod.py").write_text(MODULE, encoding="utf-8", newline="\n")
    return root


def _params(ws: Path, cache: Path) -> StdioServerParameters:
    import sys

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "lucianke"],
        env={"PYTHONUTF8": "1", "LUCIANKE_CACHE_DIR": str(cache)},
        cwd=str(ws),
    )


def _call(i: int) -> tuple[str, dict[str, Any]]:
    """i 番目の呼び出し。skeleton と bodies を交互に、3 回に 1 回は json。"""
    if i % 2 == 0:
        tool, args = "get_file_skeleton", {"paths": ["mod.py"]}
    else:
        tool, args = "get_symbol_bodies", {"targets": [{"path": "mod.py", "symbol_name": SYMBOLS[(i // 2) % len(SYMBOLS)]}]}
    if i % 3 == 0:
        args = {**args, "format": "json"}
    return tool, args


async def _call_checked(c: Client, i: int) -> None:
    tool, args = _call(i)
    try:
        res = await asyncio.wait_for(c.call_tool(tool, args), CALL_TIMEOUT_S)
    except TimeoutError:
        pytest.fail(f"call #{i} ({tool}) got no answer within {CALL_TIMEOUT_S:.0f} s: the server is stuck")
    assert not res.is_error, f"call #{i} ({tool}) failed: {res.content[0].text if res.content else res}"


async def test_one_thousand_calls_with_unread_stderr(ws: Path, tmp_path: Path) -> None:
    """1,000 回の連続呼び出し。stderr は誰も読まないパイプにしてある（読まれないとログの書き込みでサーバーが止まった）。"""
    read_fd, write_fd = os.pipe()
    errlog: TextIO = os.fdopen(write_fd, "w")
    t0 = time.monotonic()
    try:
        async with Client(stdio_client(_params(ws, tmp_path / "cache"), errlog=errlog), mode="legacy", cache=None) as c:
            for i in range(CALLS):
                await _call_checked(c, i)
        # 全体が解析の期限（5 s）をまたいでいること（またがない速さなら、このテストは (a) を見ていない）
        assert time.monotonic() - t0 > PARSE_DEADLINE_S
    finally:
        errlog.close()
        os.close(read_fd)


async def test_symbol_bodies_after_the_parse_deadline(ws: Path, tmp_path: Path) -> None:
    """最初の解析から期限（5 s）を過ぎたあとの ``get_symbol_bodies`` が答える（コメントの ``Node.text`` が戻る）。"""
    with (tmp_path / "server.stderr.log").open("w", encoding="utf-8") as errlog:
        async with Client(stdio_client(_params(ws, tmp_path / "cache"), errlog=errlog), mode="legacy", cache=None) as c:
            await _call_checked(c, 0)  # get_file_skeleton（json）: 構文木が作られてキャッシュされる
            await _call_checked(c, 1)  # get_symbol_bodies（期限内）
            await asyncio.sleep(PARSE_DEADLINE_S + 1.0)
            for i in range(1, 9, 2):  # get_symbol_bodies を 4 回（4 つの定義すべて）
                await _call_checked(c, i)
