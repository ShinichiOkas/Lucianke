"""後で mcp 層を作る役がそのまま写せる最小の MCP サーバー（S-1 の合格パターンを全部入れたもの）。
使い方: uv run --frozen python spikes/s01_mcp/minimal_server.py     （stdio。確認は probe_minimal.py）
構成: fd 退避 → 低水準 Server → tools/list・tools/call → initialized 後の roots/list → 退避 fd のストリームで serve
"""

from __future__ import annotations

# ---- 1. 最初の 1 バイトの前（標準ライブラリだけ import）: fd 1 を stderr に重ね、本物の stdin/stdout は退避 fd で持つ ----
import io
import os
import sys


def bootstrap_io() -> tuple[int, int]:
    real_out = os.dup(1)  # 退避（非継承 fd。子プロセスには渡らない）
    real_in = os.dup(0)
    # 以後 fd 1 への書き込み（print / os.write / C の printf / 子プロセス）は stderr へ。
    # CPython 3.12 + UCRT の dup2 は Win32 の標準ハンドルも付け替える（実測）。
    os.dup2(2, 1)
    nul = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nul, 0)  # 子プロセスがプロトコルの入力を横取りしないよう fd 0 を NUL に
    os.close(nul)
    sys.stdout = sys.stderr
    return real_in, real_out


REAL_IN, REAL_OUT = bootstrap_io()

# ---- 2. ここから重い import ----
import warnings  # noqa: E402
from typing import Any  # noqa: E402

import anyio  # noqa: E402
from mcp import types  # noqa: E402
from mcp.server import Server, ServerRequestContext  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.shared.exceptions import MCPDeprecationWarning, MCPError  # noqa: E402
from pydantic import BaseModel, ConfigDict, ValidationError  # noqa: E402

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)  # roots は SDK 2.3 で非推奨。呼ぶたびの警告を止める


# ---- 3. ツール表（ToolSpec の最小形）: 引数モデルが入力スキーマの唯一の出どころ ----
class PingArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")  # → スキーマに additionalProperties:false が付く
    text: str = "pong"


async def ping(args: PingArgs) -> tuple[str, dict[str, Any]]:
    return f"# ping ok\n{args.text}", {"ok": True, "tool": "ping", "data": {"text": args.text}}


TOOLS: dict[str, tuple[type[BaseModel], Any]] = {"ping": (PingArgs, ping)}
ROOTS: list[str] = []  # initialized 後と list_changed 後に更新される作業ツリーの候補


def error_result(tool: str, code: str, message: str) -> types.CallToolResult:
    """isError:true の通常結果。例外は投げない（仕様書 §5.1）。"""
    env = {"ok": False, "tool": tool, "error": {"code": code, "message": message}}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"# {tool} error {code}\n! {message}")],
        structured_content=env,
        is_error=True,
    )


# ---- 4. 低水準ハンドラ ----
async def on_list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[types.Tool(name=n, description=f"{n} tool", input_schema=m.model_json_schema()) for n, (m, _h) in TOOLS.items()]
    )


async def on_call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    spec = TOOLS.get(params.name)
    if spec is None:  # 未知のツール名だけは JSON-RPC エラー（プロトコル違反）
        raise MCPError(code=types.INVALID_PARAMS, message=f"unknown tool: {params.name}")
    model, handler = spec
    try:
        args = model.model_validate(params.arguments or {})
        text, structured = await handler(args)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], structured_content=structured, is_error=False
        )
    except ValidationError as exc:
        return error_result(params.name, "E_INVALID_ARGUMENT", str(exc.errors()[0]["loc"]))
    except Exception as exc:  # noqa: BLE001  SDK は漏れた例外を code=0 の JSON-RPC エラーにして文面も返す。必ずここで止める
        return error_result(params.name, "E_INTERNAL", type(exc).__name__)


async def refresh_roots(ctx: ServerRequestContext) -> None:
    caps = ctx.session.client_capabilities
    if caps is None or caps.roots is None:  # 能力を宣言していないクライアントには要求しない
        return
    with anyio.fail_after(5):
        res = await ctx.session.list_roots()
    ROOTS[:] = [str(r.uri) for r in res.roots]


async def on_initialized(ctx: ServerRequestContext, params: types.NotificationParams) -> None:
    """initialize の応答の後。通知ハンドラは別タスクで走るので、ここで要求を待ってよい（initialize の中で待つとデッドロック）。"""
    try:
        await refresh_roots(ctx)
    except Exception:  # noqa: BLE001  roots が無い・失敗しても動き続ける（T-00 set_roots / cwd に退避）
        pass


async def on_roots_list_changed(ctx: ServerRequestContext, params: types.NotificationParams) -> None:
    try:
        await refresh_roots(ctx)
    except Exception:  # noqa: BLE001
        pass


server: Server[Any] = Server("lucianke", version="0.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
server.add_notification_handler("notifications/initialized", types.NotificationParams, on_initialized)
server.add_notification_handler("notifications/roots/list_changed", types.NotificationParams, on_roots_list_changed)


# ---- 5. 退避した fd からトランスポート用ストリームを作り、明示的に渡す（引数なしの stdio_server() は呼ばない） ----
async def main() -> None:
    stdin = io.TextIOWrapper(os.fdopen(REAL_IN, "rb", closefd=False), encoding="utf-8", errors="replace", newline="\n")
    # newline="\n": 既定のままだと Windows で行末が CRLF になる
    stdout = io.TextIOWrapper(os.fdopen(REAL_OUT, "wb", closefd=False), encoding="utf-8", newline="\n")
    async with stdio_server(stdin=anyio.wrap_file(stdin), stdout=anyio.wrap_file(stdout)) as (read, write):
        await server.run(read, write, server.create_initialization_options())  # stdin の EOF で戻る


if __name__ == "__main__":
    anyio.run(main)
