"""合成ルート（設計書 §2.2、§6.5）。設定 → ログ → ツール登録表 → パイプライン → MCP サーバー。

起動順（§6.5）の 1〜4 と 6〜7: ``bootstrap_io`` → ログ → 設定 → MCP サーバー（``initialize`` 受付、``roots/list``）。
索引・監視（5・8・9）は後続のタスクで足す。

import の順序が要: このモジュールの先頭では標準ライブラリと軽い自前モジュール（``bootstrap_io`` / ``config`` /
``logging_setup``）だけを import する。SDK（``mcp``・``anyio``）や pydantic は ``bootstrap_io()`` の **後で**、関数の中で import する
（C 拡張や依存が import 時に fd 1 へ書いても、すでに stderr に重なっている）。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from lucianke.bootstrap_io import bootstrap_io, open_transport_streams
from lucianke.config import Settings, load_settings
from lucianke.logging_setup import setup_logging

if TYPE_CHECKING:
    from lucianke.mcp.registry import ToolRegistry


def build_registry(settings: Settings) -> ToolRegistry:
    """ツール登録表を作る。開発用ツール（ping）は ``LUCIANKE_DEV_TOOLS=1`` のときだけ。実ツールは後でここに足す。"""
    from lucianke.mcp.registry import ToolRegistry

    registry = ToolRegistry()
    if settings.dev_tools:
        from lucianke.tools import dev_ping

        registry.register(dev_ping.SPEC)
    return registry


def run_stdio() -> int:
    """ホストから子プロセスとして起動され、stdio で MCP を話す。``stdin`` の EOF で終わり、終了コード 0（ADR-16）。"""
    fds = bootstrap_io()  # 最初の 1 バイトの前。以降 print / os.write(1) / 子プロセスの fd 1 は stderr へ
    settings = load_settings()
    log = setup_logging(settings)

    import anyio

    from lucianke import __version__
    from lucianke.mcp.pipeline import Pipeline
    from lucianke.mcp.server import RootsState, build_server

    registry = build_registry(settings)
    pipeline = Pipeline(registry)
    roots = RootsState.from_cwd()
    server = build_server(registry, pipeline, roots, version=__version__)
    log.info("lucianke %s starting (tools=%d)", __version__, len(registry), extra={"event": "startup"})

    async def serve() -> None:
        from mcp.server.stdio import stdio_server

        stdin, stdout = open_transport_streams(fds)
        # 引数なしの stdio_server() は呼ばない（fd 0 が NUL なので入力 EOF と見て即終了する。S-1）
        async with stdio_server(stdin=anyio.wrap_file(stdin), stdout=anyio.wrap_file(stdout)) as (read, write):
            await server.run(read, write, server.create_initialization_options())  # stdin の EOF で戻る

    try:
        anyio.run(serve)
    except KeyboardInterrupt:
        log.info("interrupted", extra={"event": "shutdown"})
    except Exception:  # noqa: BLE001
        log.error("server crashed", exc_info=True, extra={"event": "crash"})
        return 1
    log.info("shutdown complete", extra={"event": "shutdown"})
    sys.stderr.flush()
    return 0
