"""MCP SDK の低水準サーバーの組み立て（設計書 ADR-2、スパイク S-1 の「使うコードの型」）。

* ``tools/list``: レジストリの入力スキーマをそのまま返す（``additionalProperties: false`` 込み）。
* ``tools/call``: パイプラインの結果を ``CallToolResult``（``content`` / ``structuredContent`` / ``isError``）にする。
  通常の失敗は ``isError: true`` の結果。未知のツール名だけ ``MCPError(INVALID_PARAMS)``。
  **``on_call_tool`` から例外を漏らさない**（SDK は ``code=0`` の JSON-RPC エラーにして例外文面をそのまま返す）。
* roots: ``notifications/initialized`` の後（と ``roots/list_changed``）に ``roots/list`` を要求する。クライアントが
  roots 能力を宣言していなければ要求しない。取れなければ cwd を根のままにする（``RootsState``）。
  ``initialize`` の処理中に要求を待つとデッドロックするため、通知ハンドラ（別タスク）で行う。

合成ルート（``app`` / ``config`` / ``logging_setup``）は import しない。入れ物と設定は ``app.py`` が渡す。
"""

from __future__ import annotations

import inspect
import logging
import os
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import anyio
from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPDeprecationWarning, MCPError

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.mcp.pipeline import INTERNAL_MESSAGE, Pipeline, UnknownToolError
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.types import Rendered

# roots は SDK 2.3 で非推奨（SEP-2577）。呼ぶたびの警告を止める（S-1）
warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

ROOTS_TIMEOUT_S = 5  # 設計書 §6.5 / S-1: roots/list の待ち上限

_log = logging.getLogger("lucianke.server")


@dataclass(slots=True)
class RootsState:
    """クライアントが教えた作業ツリーの根（後でワークスペース層に渡す単純な入れ物）。

    ``paths`` はローカルのパス。クライアントから取れなければ cwd 1 件（``source == "cwd"``）。
    """

    paths: list[str] = field(default_factory=list)
    uris: list[str] = field(default_factory=list)
    source: str = "cwd"  # "cwd" | "client"
    listeners: list[Callable[[RootsState], Awaitable[None] | None]] = field(default_factory=list)
    """``roots/list`` の取得が済んだ（取れた・取れない・要求しない、のどれでも）たびに呼ぶ。合成ルートがワークスペースの登録に使う。"""

    @classmethod
    def from_cwd(cls) -> RootsState:
        return cls(paths=[os.getcwd()], uris=[], source="cwd")

    async def notify(self) -> None:
        """``listeners`` を呼ぶ（同期でも非同期でもよい）。1 つの失敗が他と接続を止めない。"""
        for fn in list(self.listeners):
            try:
                result = fn(self)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                _log.warning("roots listener failed", exc_info=True, extra={"event": "roots_listener_failed"})

    def set_from_client(self, uris: list[str]) -> None:
        paths = [p for u in uris if (p := file_uri_to_path(u))]
        if paths:  # 空の応答なら cwd のまま
            self.paths, self.uris, self.source = paths, list(uris), "client"


def file_uri_to_path(uri: str) -> str | None:
    """``file://`` URI をローカルのパスにする（Windows のドライブ・UNC に対応）。file 以外は None。"""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        path = f"//{parsed.netloc}{path}"
    return url2pathname(path)


def to_call_tool_result(rendered: Rendered) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=rendered.text)],
        structured_content=dict(rendered.structured) if rendered.structured is not None else None,
        is_error=rendered.is_error,
    )


def _internal_failure(tool: str) -> types.CallToolResult:
    """パイプライン自体が壊れたときの最後の砦。SDK に例外を漏らさない。"""
    err = LuciankeError(ErrorCode.E_INTERNAL, INTERNAL_MESSAGE)
    return to_call_tool_result(
        Rendered(
            text=f"# {tool} error {err.code.value}\n! {err.message}\n",
            structured={"ok": False, "tool": tool, "error": err.to_json()},
            is_error=True,
        )
    )


def build_server(
    registry: ToolRegistry, pipeline: Pipeline, roots: RootsState, *, version: str, list_planned: bool = False
) -> Server[Any]:
    """``list_planned`` が偽（既定）なら、``tools/list`` にパイプラインの現在のマイルストーンより先のツールを載せない。

    ホストはセッションごとに ``tools/list`` を丸ごと読むので、M1 では呼べない M2・M3 の入口（約 4,000 トークン）は固定費になる。
    載せないだけで、呼ばれれば従来どおり ``E_UNSUPPORTED``（``not_in_milestone``）。
    """

    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=d.name,
                    description=d.description,
                    input_schema=d.input_schema,
                    annotations=types.ToolAnnotations(read_only_hint=d.read_only),
                )
                for d in registry.descriptors(
                    include_planned=list_planned, current_milestone=pipeline.current_milestone
                )
            ]
        )

    async def on_call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            rendered = await pipeline.call_tool(params.name, params.arguments)
            return to_call_tool_result(rendered)
        except UnknownToolError:
            raise MCPError(code=types.INVALID_PARAMS, message=f"unknown tool: {params.name}") from None
        except Exception:  # noqa: BLE001  パイプラインの外側の不具合。スタックはログだけ
            _log.error("pipeline failure", exc_info=True, extra={"event": "pipeline_failure", "tool": params.name})
            return _internal_failure(params.name)

    async def refresh_roots(ctx: ServerRequestContext) -> None:
        caps = ctx.session.client_capabilities
        if caps is None or caps.roots is None:  # 能力を宣言していないクライアントには要求しない
            return
        with anyio.fail_after(ROOTS_TIMEOUT_S):
            res = await ctx.session.list_roots()
        roots.set_from_client([str(r.uri) for r in res.roots])
        _log.info("roots updated: %s (%s)", roots.paths, roots.source, extra={"event": "roots"})

    async def on_roots_event(ctx: ServerRequestContext, params: types.NotificationParams) -> None:
        try:
            await refresh_roots(ctx)
        except Exception:  # noqa: BLE001  取れなくても動き続ける（cwd と T-00 set_roots に退避）
            _log.warning("roots/list failed; keeping %s", roots.source, exc_info=True, extra={"event": "roots_failed"})
        await roots.notify()  # 取れても取れなくても、根が決まったので合成ルートに知らせる（索引の起動。設計書 §6.5 手順 7〜8）

    server: Server[Any] = Server(
        "lucianke", version=version, on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    server.add_notification_handler("notifications/initialized", types.NotificationParams, on_roots_event)
    server.add_notification_handler("notifications/roots/list_changed", types.NotificationParams, on_roots_event)
    return server
