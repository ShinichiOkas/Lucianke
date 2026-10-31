"""合成ルート（設計書 §2.2、§6.5）。設定 → ログ → ツール登録表 → パイプライン → MCP サーバー。

起動順（§6.5）: ``bootstrap_io`` → ログ → 設定 → MCP サーバー（``initialize`` 受付、``roots/list``）→ ワークスペースの登録と
``IndexWriter`` の起動・スキーマ移行 → 差分索引の背景ジョブ → 監視（``tools._runtime.RuntimeHub``）。終了は ``lifecycle.py`` の段。

import の順序が要: このモジュールの先頭では標準ライブラリと軽い自前モジュール（``bootstrap_io`` / ``config`` /
``logging_setup``）だけを import する。SDK（``mcp``・``anyio``）や pydantic は ``bootstrap_io()`` の **後で**、関数の中で import する
（C 拡張や依存が import 時に fd 1 へ書いても、すでに stderr に重なっている）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING, Any

from lucianke.bootstrap_io import bootstrap_io, open_transport_streams
from lucianke.config import Settings, load_settings
from lucianke.logging_setup import setup_logging

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable, Mapping, Sequence

    from lucianke.core.procs import ProcessRegistry
    from lucianke.lifecycle import Lifecycle
    from lucianke.lsp.pool import LspPool
    from lucianke.mcp.registry import ToolRegistry
    from lucianke.mcp.server import RootsState
    from lucianke.syntax.parsers import TreeCache
    from lucianke.tools._runtime import RuntimeHub
    from lucianke.tools._services import ToolServices


def build_registry(settings: Settings) -> ToolRegistry:
    """ツール登録表を作る。開発用ツール（ping）は ``LUCIANKE_DEV_TOOLS=1`` のときだけ。実ツールは後でここに足す。"""
    from lucianke.mcp.registry import ToolRegistry
    from lucianke.tools import register_tools

    registry = ToolRegistry()
    register_tools(registry)
    if settings.dev_tools:
        from lucianke.tools import dev_ping

        registry.register(dev_ping.SPEC)
    return registry


def build_services(
    roots: Callable[[], Sequence[str]], *, trees: TreeCache | None = None, hub: RuntimeHub | None = None
) -> ToolServices:
    """ツールが使うサービスの入れ物を組み立てる。``roots`` は現在の作業ツリーの根（MCP の roots か cwd）を返す関数。

    ``hub`` があればワークスペース・索引・LSP・編集ロックはそこから（``tools._runtime.WiredServices``）。無ければ ``roots`` の先頭だけ。
    ``tools`` は ``mcp`` を import できないので、compact の行の組み立て関数（``mcp.render.compact``）はここで注入する。
    """
    from lucianke.mcp.render import compact
    from lucianke.syntax.parsers import TreeCache
    from lucianke.tools._runtime import WiredServices

    return WiredServices(roots=roots, trees=trees if trees is not None else TreeCache(), lines=compact, hub=hub)


ROOTS_FALLBACK_S = 8.0  # ``notifications/initialized`` が来ないホストでも、この秒数の後に cwd で索引を始める（roots の待ち上限 5 s より長く）
ROOTS_FALLBACK_ENV = "LUCIANKE_ROOTS_FALLBACK_S"  # 上の秒数の上書き（試験用。e2e が「待ち切った後でも set_roots が残る」を短い時間で確かめる）
EDIT_DRAIN_POLL_S = 0.05


def roots_fallback_seconds(env: Mapping[str, str] | None = None) -> float:
    raw = (os.environ if env is None else env).get(ROOTS_FALLBACK_ENV)
    try:
        value = float(raw) if raw else ROOTS_FALLBACK_S
    except ValueError:
        return ROOTS_FALLBACK_S
    return value if value > 0 else ROOTS_FALLBACK_S


class RootsDecision:
    """ワークスペースの根をいつ・何に決めるか（設計書 §6.5 手順 7。BUG-0042）。

    決める筋は 3 つ。どれも ``hub.sync_roots`` に行き着き、**最初の 1 つが勝つ**（cwd は既定にすぎない）:

    * ``roots/list`` の結果（``notifications/initialized`` の後。``on_roots``）。取れなければ cwd。
    * roots を渡せない相手（roots 能力なし・2026-07-28 版）の最初の呼び出し: cwd を待たずに既定にする（``gate``）。
      ただし T-00 ``set_roots`` は決定そのもの: 待たず、cwd の索引も始めない（根が決まるのを待つのは循環）。
    * ``initialized`` が来ないホストの退避: ``fallback_s`` 秒の後に cwd。
    """

    def __init__(self, hub: RuntimeHub, roots: RootsState, log: logging.Logger) -> None:
        self.hub = hub
        self.roots = roots
        self.log = log
        self.settled = False

    async def on_roots(self, state: RootsState) -> None:
        """``roots/list`` の結果（か、取れなかった結果）が出た。"""
        self.settled = True
        if state.source == "cwd" and self.hub.started:
            return  # cwd は既定。すでに誰か（T-00 ``set_roots`` など）が決めたものを cwd で上書きしない
        await self.hub.sync_roots(state.paths)

    async def fallback(self, fallback_s: float) -> None:
        await asyncio.sleep(fallback_s)
        if not self.settled and not self.hub.started:
            self.log.warning("no roots decision after %.0f s; indexing the cwd", fallback_s)
            await self.on_roots(self.roots)

    async def gate(self, tool: str, arguments: Mapping[str, Any]) -> None:
        """``tools/call`` の入口。根が決まるまで待つ（上限つき）。roots を渡せない相手には待たずに決める。"""
        if self.roots.client_can_list is False:  # roots の決定は来ない。待つ相手がいない
            if tool == "configure_workspace" and arguments.get("action") == "set_roots":
                return  # このツールが根を決める。cwd の索引は始めない
            if not self.settled and not self.hub.started:
                await self.on_roots(self.roots)
        await self.hub.wait_started()


def register_shutdown_steps(lifecycle: Lifecycle, hub: RuntimeHub, pool: LspPool, procs: ProcessRegistry) -> None:
    """終了の段（設計書 §6.5）: 編集の完了待ち → 監視停止 → 索引器の停止 → LSP → 残った子プロセス → 書き手の close。

    段の本体は ``hub`` の **現在の** 束を回る（後から増えたルートも含む）。
    """
    from lucianke.lifecycle import DEFAULT_TIMEOUT_MS, ShutdownOrder, make_terminate_children_step

    async def drain_edits() -> None:
        while any(hub.locks.is_locked(ws_id) for ws_id in hub.runtimes):
            await asyncio.sleep(EDIT_DRAIN_POLL_S)

    def t(order: ShutdownOrder) -> int:
        return DEFAULT_TIMEOUT_MS[order]

    add = lifecycle.add_shutdown_step
    add("edits", drain_edits, order=ShutdownOrder.DRAIN_EDITS, timeout_ms=t(ShutdownOrder.DRAIN_EDITS))
    add("watchers", hub.stop_watchers, order=ShutdownOrder.STOP_WATCHER, timeout_ms=3000)  # 取り込み中の束を待つ（束側の上限 2 s）
    add("indexers", hub.stop_indexers, order=ShutdownOrder.STOP_WATCHER + 5, timeout_ms=t(ShutdownOrder.STOP_WATCHER))
    add("lsp", pool.shutdown_all, order=ShutdownOrder.LSP, timeout_ms=t(ShutdownOrder.LSP))
    add("children", make_terminate_children_step(procs), order=ShutdownOrder.KILL_CHILDREN,
        timeout_ms=t(ShutdownOrder.KILL_CHILDREN))  # fmt: skip
    add("index_close", hub.close_storage, order=ShutdownOrder.INDEX_CLOSE, timeout_ms=t(ShutdownOrder.INDEX_CLOSE))


CRASH_LOG_NAME = "crash.log"
_crash_file: Any = None  # faulthandler が書くファイル（プロセスの終わりまで開いておく）


def install_crash_diagnostics(settings: Settings, log: logging.Logger) -> None:
    """プロセスが黙って落ちないようにする（BUG-0112: 大きなワークスペースの初回の索引の途中で、ログに何も残さず終わった）。

    * ``faulthandler``: 致命的なエラー（アクセス違反・``abort``・スタックの溢れ）で、全スレッドの Python のスタックを
      ``<log_dir>/crash.log`` に書く（ログのハンドラが使えない状態でも書ける）。
    * ``threading.excepthook`` / ``sys.excepthook``: どこにも拾われなかった例外をログに残す（書き手スレッドなどが例外で止まったとき）。
    * ``atexit``: 終了の印をログに残す（ログの最後の行で、正常な終了か、印の無いまま消えた〈外から止められた・C の層で落ちた〉かを見分ける）。

    どれも失敗しても起動は止めない（診断は付加）。
    """
    import atexit
    import faulthandler
    import threading

    global _crash_file
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        _crash_file = (settings.log_dir / CRASH_LOG_NAME).open("a", encoding="utf-8")  # noqa: SIM115  終了まで開いておく
        _crash_file.write(f"--- lucianke pid {os.getpid()} started\n")
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError as exc:
        log.warning("crash log disabled: %s", exc, extra={"event": "crash_log_disabled"})

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.critical(
            "uncaught exception in thread %s", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback), extra={"event": "crash"},
        )  # fmt: skip

    def main_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb), extra={"event": "crash"})
        sys.__excepthook__(exc_type, exc, tb)

    threading.excepthook = thread_hook
    sys.excepthook = main_hook
    atexit.register(lambda: log.info("process exiting", extra={"event": "exit"}))


def run_stdio() -> int:
    """ホストから子プロセスとして起動され、stdio で MCP を話す。``stdin`` の EOF で終わり、終了コード 0（ADR-16）。

    起動（設計書 §6.5）: ``bootstrap_io`` → ログ → 設定 → MCP サーバー（``initialize`` 受付）→ ``roots/list``（取れたら。
    取れなければ cwd）→ ワークスペース登録・``IndexWriter`` 起動とスキーマ移行 → 監視開始 → 差分索引を背景ジョブで投入。
    終了は ``Lifecycle`` の段（``register_shutdown_steps``）を ``run_until_shutdown`` とシグナルで回す。
    """
    fds = bootstrap_io()  # 最初の 1 バイトの前。以降 print / os.write(1) / 子プロセスの fd 1 は stderr へ
    settings = load_settings()
    log = setup_logging(settings)
    install_crash_diagnostics(settings, log)

    import anyio

    from lucianke import __version__
    from lucianke.core.procs import ProcessRegistry
    from lucianke.lifecycle import Lifecycle, install_signal_handlers, make_shutdown_guard, run_until_shutdown
    from lucianke.lsp.pool import LspPool
    from lucianke.mcp.pipeline import Pipeline
    from lucianke.mcp.server import RootsState, build_server
    from lucianke.syntax.parsers import TreeCache
    from lucianke.tools._runtime import RuntimeHub

    registry = build_registry(settings)
    roots = RootsState.from_cwd()
    trees = TreeCache()
    procs = ProcessRegistry()
    hub = RuntimeHub(settings.cache_root, trees)
    pool = LspPool(procs, on_diagnostics=hub.on_diagnostics, on_initialized=hub.on_lsp_initialized)
    hub.lsp_pool = pool
    decision = RootsDecision(hub, roots, log)
    lifecycle = Lifecycle()
    register_shutdown_steps(lifecycle, hub, pool, procs)
    pipeline = Pipeline(
        registry,
        services=build_services(lambda: roots.paths, trees=trees, hub=hub),
        data_version_provider=hub.data_version,
        shutdown_guard=make_shutdown_guard(lifecycle),
        ready_gate=decision.gate,
    )
    server = build_server(registry, pipeline, roots, version=__version__, list_planned=settings.list_planned_tools)
    log.info("lucianke %s starting (tools=%d)", __version__, len(registry), extra={"event": "startup"})

    async def serve() -> None:
        from mcp.server.stdio import stdio_server

        roots.listeners.append(decision.on_roots)
        fallback = asyncio.get_running_loop().create_task(
            decision.fallback(roots_fallback_seconds()), name="lucianke-roots-fallback"
        )  # initialized が来ないホスト向け
        watch = install_signal_handlers(asyncio.get_running_loop(), lifecycle)
        stdin, stdout = open_transport_streams(fds)

        async def run_server() -> None:
            # 引数なしの stdio_server() は呼ばない（fd 0 が NUL なので入力 EOF と見て即終了する。S-1）
            async with stdio_server(stdin=anyio.wrap_file(stdin), stdout=anyio.wrap_file(stdout)) as (read, write):
                await server.run(read, write, server.create_initialization_options())  # stdin の EOF で戻る

        try:
            await run_until_shutdown(lifecycle, run_server())
        finally:
            fallback.cancel()
            watch.close()
            await asyncio.gather(fallback, return_exceptions=True)

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
