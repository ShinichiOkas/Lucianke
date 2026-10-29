"""S-1 / S-9 スパイク用の MCP サーバー（低水準 API）。直接は動かさず、probe_*.py / bench.py が起動する。
使い方: uv run --frozen python spikes/s01_mcp/server.py [--divert=manual|sdk|naive|manual-nohandle|none] [--no-otel]
  --divert  fd 1 の扱い。manual = ADR-3（既定）/ sdk = SDK 任せ / naive = 二重に退避（罠の再現）/ none = 退避なし（対照）
  --no-otel Server.middleware から既定の OpenTelemetryMiddleware を外す（S-9 の計測用）
  --stub-http  starlette / uvicorn / httpx2 等を空のスタブに差し替えてから mcp を import する（遅延 import の実験。製品には入れない）
"""

from __future__ import annotations

# --- 1. 最初の 1 バイトの前に fd を退避する（ADR-3）。ここでは標準ライブラリしか import しない -----------
import io
import os
import sys
import time

_T0 = time.perf_counter()


def _arg(name: str, default: str) -> str:
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


DIVERT = _arg("divert", "manual")
NO_OTEL = "--no-otel" in sys.argv
STUB_HTTP = "--stub-http" in sys.argv


def bootstrap_io(set_std_handle: bool = True) -> tuple[int, int]:
    """fd 1 を stderr に重ね、本物の stdout / stdin は退避 fd で返す。fd 0 は NUL に差し替える。"""
    real_out = os.dup(1)  # 非継承 fd。子プロセスには渡らない
    real_in = os.dup(0)
    os.dup2(2, 1)
    nul = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nul, 0)
    os.close(nul)
    if set_std_handle and sys.platform == "win32":
        # os.dup2 は CRT の fd 表しか変えない。subprocess は Win32 の標準ハンドル枠を継承するので、そちらも付け替える。
        import ctypes
        import msvcrt
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        k32.SetStdHandle.restype = wintypes.BOOL
        for std_id, fd in ((-10, 0), (-11, 1)):
            if not k32.SetStdHandle(wintypes.DWORD(std_id & 0xFFFFFFFF), msvcrt.get_osfhandle(fd)):
                raise OSError(ctypes.get_last_error(), "SetStdHandle failed")
    sys.stdout = sys.stderr
    return real_in, real_out




class _Stub:  # 実験用: HTTP 系パッケージの代役（属性アクセスで空のクラスを返す）
    pass


if STUB_HTTP:
    import importlib.abc
    import importlib.machinery
    import types as _pytypes

    class _StubModule(_pytypes.ModuleType, _Stub):
        __path__: list[str] = []

        def __getattr__(self, name: str) -> object:
            if name.startswith("__"):
                raise AttributeError(name)
            cls = type(name, (), {"__init__": lambda self, *a, **k: None})
            setattr(self, name, cls)
            return cls

    class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        PREFIXES = ("starlette", "uvicorn", "sse_starlette", "httpx2", "httpcore2", "multipart", "python_multipart", "jwt", "cryptography")  # fmt: skip

        def find_spec(self, name: str, path: object, target: object = None):  # type: ignore[no-untyped-def]
            if name.split(".")[0] in self.PREFIXES:
                return importlib.machinery.ModuleSpec(name, self, is_package=True)
            return None

        def create_module(self, spec):  # type: ignore[no-untyped-def]
            return _StubModule(spec.name)

        def exec_module(self, module) -> None:  # type: ignore[no-untyped-def]
            pass

    sys.meta_path.insert(0, _StubFinder())

_streams: tuple[int, int] | None = None
if DIVERT in ("manual", "naive"):
    _streams = bootstrap_io()
elif DIVERT == "manual-nohandle":
    _streams = bootstrap_io(set_std_handle=False)
elif DIVERT == "none":
    _streams = (os.dup(0), os.dup(1))  # 退避も差し替えもしない（対照）。fd 1 は本物の stdout のまま
_T_BOOTSTRAP = time.perf_counter()

# --- 2. ここから重い import --------------------------------------------------------------------
import ctypes  # noqa: E402
import subprocess  # noqa: E402
import warnings  # noqa: E402
from collections import defaultdict  # noqa: E402
from typing import Any  # noqa: E402

import anyio  # noqa: E402
from mcp import types  # noqa: E402
from mcp.server import Server, ServerRequestContext  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.shared.exceptions import MCPDeprecationWarning, MCPError  # noqa: E402
from schemas import ARGS10_SCHEMA, EMPTY_SCHEMA, Args10  # noqa: E402

_T_IMPORT = time.perf_counter()

# roots は SDK 2.3 で非推奨（SEP-2577）。呼ぶたびの警告（stderr 行き）を止める。
warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

# --- 3. ツール定義（Lucianke の ToolSpec 相当を最小化したもの） -----------------------------------------


BIG_TEXT = ("def example_function(argument_one, argument_two):  # skeleton line\n" * 130)[:8192]  # 約 8 KB


def _text(s: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=s)]


STATE: dict[str, Any] = {
    "initial_roots": None,
    "roots_after_change": None,
    "list_changed_count": 0,
    "roots_errors": [],
    "client_caps_roots": None,
    "protocol_version": None,
}
HANDLER_NS: dict[str, list[int]] = defaultdict(list)
MW_NS: dict[str, list[int]] = defaultdict(list)

WATCH_MODULES = [
    "starlette", "uvicorn", "httpx2", "httpcore2", "httpx", "sse_starlette", "multipart", "python_multipart",
    "jwt", "cryptography", "jsonschema", "opentelemetry", "win32api", "pywintypes", "h11", "pydantic", "anyio",
    "mcp.client.streamable_http", "mcp.server.streamable_http", "mcp.server.auth",
]  # fmt: skip


async def t_noop(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(content=_text("ok"))


async def t_big(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(content=_text(BIG_TEXT))


async def t_validated(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    Args10.model_validate(args)  # 失敗したら ValidationError → isError に変換される（下の on_call_tool）
    return types.CallToolResult(content=_text("ok"))


async def t_structured(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    data = {"ok": True, "tool": "structured", "data": {"items": [1, 2, 3], "nested": {"a": None}}, "warnings": []}
    return types.CallToolResult(content=_text("# structured ok\nbody"), structured_content=data)


async def t_fail(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    env = {"ok": False, "tool": "fail", "error": {"code": "E_TARGET_NOT_FOUND", "message": "no such symbol"}}
    return types.CallToolResult(content=_text("# fail error E_TARGET_NOT_FOUND\n! no such symbol"),
                                structured_content=env, is_error=True)  # fmt: skip


async def t_raise(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    raise RuntimeError("boom (tool bug)")


async def t_junk(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    """stdout に書こうとするあらゆる経路。どれも本物の stdout に出てはいけない。"""
    done: list[str] = []
    print("junk-print")
    done.append("print")
    os.write(1, b"junk-os-write\n")
    done.append("os.write(1)")
    sys.__stdout__.write("junk-sys-dunder-stdout\n")  # type: ignore[union-attr]
    sys.__stdout__.flush()  # type: ignore[union-attr]
    done.append("sys.__stdout__")
    if sys.platform == "win32":
        crt = ctypes.CDLL("ucrtbase")  # Python 3.12 と同じ UCRT。C 拡張が printf / puts で書く経路
        crt.puts(b"junk-crt-puts")
        crt.fflush(None)
        done.append("crt.puts")
        k32 = ctypes.WinDLL("kernel32")
        k32.GetStdHandle.restype = ctypes.c_void_p
        h = k32.GetStdHandle(ctypes.c_uint32(-11 & 0xFFFFFFFF))
        n = ctypes.c_uint32(0)
        k32.WriteFile(ctypes.c_void_p(h), b"junk-winapi-writefile\n", 22, ctypes.byref(n), None)  # Win32 を直接
        done.append("WriteFile(GetStdHandle)")
        subprocess.run(["cmd", "/c", "echo junk-child-inherit"], check=False)  # stdout を継承する子プロセス
        done.append("subprocess(inherit)")
    else:
        subprocess.run(["sh", "-c", "echo junk-child-inherit"], check=False)
        done.append("subprocess(inherit)")
    return types.CallToolResult(content=_text("junk done: " + ", ".join(done)), structured_content={"done": done})


async def t_child_stdin(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    """stdin を継承する子プロセスが、プロトコルの入力を横取りしないこと（fd 0 を NUL に差し替えた効果）。"""
    try:
        r = subprocess.run([sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
                           capture_output=True, text=True, timeout=5)  # fmt: skip
        out = r.stdout.strip()
    except subprocess.TimeoutExpired:
        out = "timeout"
    return types.CallToolResult(content=_text(f"child read {out} bytes of stdin"), structured_content={"child_stdin": out})


async def t_roots_state(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(content=_text("roots state"), structured_content=dict(STATE))


async def t_roots_now(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    """ツール呼び出しの最中にサーバー側から roots/list を要求する。"""
    try:
        with anyio.fail_after(5):
            res = await ctx.session.list_roots()
    except BaseException as exc:  # noqa: BLE001 - 結果として返すのが目的
        return types.CallToolResult(content=_text(f"roots_now failed: {type(exc).__name__}: {exc}"),
                                    structured_content={"error": type(exc).__name__, "message": str(exc)},
                                    is_error=True)  # fmt: skip
    roots = [str(r.uri) for r in res.roots]
    return types.CallToolResult(content=_text("roots: " + ", ".join(roots)), structured_content={"roots": roots})


async def t_stats(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    loaded = {  # 本物がロードされているか（スタブは数えない）
        m: any((k == m or k.startswith(m + ".")) and not isinstance(v, _Stub) for k, v in sys.modules.items())
        for m in WATCH_MODULES
    }
    data = {
        "handler_ns": {k: list(v) for k, v in HANDLER_NS.items()},
        "middleware_ns": {k: list(v) for k, v in MW_NS.items()},
        "boot_ms": {
            "bootstrap_io": (_T_BOOTSTRAP - _T0) * 1000,
            "import": (_T_IMPORT - _T_BOOTSTRAP) * 1000,
        },
        "loaded": loaded,
        "divert": DIVERT,
        "no_otel": NO_OTEL,
        "stub_http": STUB_HTTP,
        "n_sys_modules": len(sys.modules),
    }
    return types.CallToolResult(content=_text("stats"), structured_content=data)


async def t_reset(ctx: ServerRequestContext, args: dict[str, Any]) -> types.CallToolResult:
    HANDLER_NS.clear()
    MW_NS.clear()
    return types.CallToolResult(content=_text("reset"))


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "noop": ("何もしないツール", EMPTY_SCHEMA, t_noop),
    "big": ("約 8 KB のテキストを返す", EMPTY_SCHEMA, t_big),
    "validated": ("pydantic(extra=forbid, 10 フィールド)で引数を検証する", ARGS10_SCHEMA, t_validated),
    "structured": ("content + structuredContent", EMPTY_SCHEMA, t_structured),
    "fail": ("isError: true の通常結果（例外を投げない）", EMPTY_SCHEMA, t_fail),
    "raise": ("ハンドラが例外を投げる（SDK の挙動の確認用）", EMPTY_SCHEMA, t_raise),
    "junk": ("stdout に書く各経路を試す", EMPTY_SCHEMA, t_junk),
    "child_stdin": ("stdin を継承する子プロセス", EMPTY_SCHEMA, t_child_stdin),
    "roots_state": ("起動時・変更時に取得した roots", EMPTY_SCHEMA, t_roots_state),
    "roots_now": ("呼び出し中に roots/list を要求", EMPTY_SCHEMA, t_roots_now),
    "roots_mrtr": ("2026-07-28: 結果に埋め込んだ roots 要求", EMPTY_SCHEMA, t_noop),  # 本体は on_call_tool 内
    "stats": ("サーバー内計測と import 状況", EMPTY_SCHEMA, t_stats),
    "reset": ("計測を消す", EMPTY_SCHEMA, t_reset),
}

# --- 4. 低水準ハンドラ -------------------------------------------------------------------------------


async def on_list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    tools = [types.Tool(name=n, description=d, input_schema=s) for n, (d, s, _h) in TOOLS.items()]
    return types.ListToolsResult(tools=tools)


async def on_call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    if params.name == "roots_mrtr":  # 2026-07-28 の作法: 要求を結果に埋め込み、クライアントが再呼び出しする
        resp = (params.input_responses or {}).get("roots")
        if resp is None:
            return types.InputRequiredResult(input_requests={"roots": types.ListRootsRequest()})  # type: ignore[return-value]
        raw = resp if isinstance(resp, dict) else resp.model_dump(by_alias=True)
        roots = [str(r["uri"]) for r in raw["roots"]]
        return types.CallToolResult(content=_text("roots(mrtr): " + ", ".join(roots)), structured_content={"roots": roots})
    if params.name == "raise_uncaught":  # ラッパの外で投げる（SDK が例外をどう扱うかの確認用）
        raise RuntimeError("boom (uncaught in on_call_tool)")
    spec = TOOLS.get(params.name)
    if spec is None:  # 未知のツール名はプロトコル違反 = JSON-RPC エラー（仕様書 §5.1）
        raise MCPError(code=types.INVALID_PARAMS, message=f"unknown tool: {params.name}")
    handler = spec[2]
    t0 = time.perf_counter_ns()
    try:
        result = await handler(ctx, params.arguments or {})
    except Exception as exc:  # noqa: BLE001 - パイプラインの「その他の例外 → E_INTERNAL」相当
        result = types.CallToolResult(
            content=_text(f"# {params.name} error E_INTERNAL\n! {type(exc).__name__}"),
            structured_content={"ok": False, "error": {"code": "E_INTERNAL", "type": type(exc).__name__}},
            is_error=True,
        )
    HANDLER_NS[params.name].append(time.perf_counter_ns() - t0)
    return result


async def on_initialized(ctx: ServerRequestContext, params: types.NotificationParams) -> None:
    """initialize の応答の後。ここで roots/list を要求する（initialize の中で待つとデッドロックする）。"""
    STATE["protocol_version"] = ctx.protocol_version
    caps = ctx.session.client_capabilities
    STATE["client_caps_roots"] = None if caps is None or caps.roots is None else caps.roots.model_dump(by_alias=True)
    if caps is None or caps.roots is None:
        return
    try:
        with anyio.fail_after(5):
            res = await ctx.session.list_roots()
        STATE["initial_roots"] = [str(r.uri) for r in res.roots]
    except BaseException as exc:  # noqa: BLE001
        STATE["roots_errors"].append(f"initial: {type(exc).__name__}: {exc}")


async def on_roots_list_changed(ctx: ServerRequestContext, params: types.NotificationParams) -> None:
    STATE["list_changed_count"] += 1
    try:
        with anyio.fail_after(5):
            res = await ctx.session.list_roots()
        STATE["roots_after_change"] = [str(r.uri) for r in res.roots]
    except BaseException as exc:  # noqa: BLE001
        STATE["roots_errors"].append(f"changed: {type(exc).__name__}: {exc}")


async def timing_middleware(ctx: ServerRequestContext, call_next: Any) -> Any:
    """受信（ディスパッチ後）から応答の整形完了まで。SDK のパラメータ検証と結果の直列化前整形を含む。"""
    if ctx.method != "tools/call":
        return await call_next(ctx)
    t0 = time.perf_counter_ns()
    try:
        return await call_next(ctx)
    finally:
        name = (ctx.params or {}).get("name", "?")
        MW_NS[name].append(time.perf_counter_ns() - t0)


def build_server() -> Server[Any]:
    server: Server[Any] = Server(
        "lucianke-spike",
        version="0.0.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    server.add_notification_handler("notifications/initialized", types.NotificationParams, on_initialized)
    server.add_notification_handler("notifications/roots/list_changed", types.NotificationParams, on_roots_list_changed)
    if NO_OTEL:
        server.middleware.clear()
    server.middleware.append(timing_middleware)
    return server


def make_streams() -> tuple[Any, Any] | tuple[None, None]:
    """退避した fd から作るトランスポート用ストリーム。newline="\\n" で Windows の CRLF 変換を止める。"""
    if _streams is None:
        return None, None
    real_in, real_out = _streams
    stdin = io.TextIOWrapper(os.fdopen(real_in, "rb", closefd=False), encoding="utf-8", errors="replace", newline="\n")
    stdout = io.TextIOWrapper(os.fdopen(real_out, "wb", closefd=False), encoding="utf-8", newline="\n")
    return anyio.wrap_file(stdin), anyio.wrap_file(stdout)


async def amain() -> None:
    server = build_server()
    if DIVERT == "sdk":
        transport = stdio_server()  # SDK が fd 1 の退避と stderr への重ねを自分でやる
    elif DIVERT == "naive":
        transport = stdio_server()  # 手動で退避したあとに引数なしで呼ぶ罠（SDK が stderr を「本物」と思い込む）
    else:
        stdin, stdout = make_streams()
        transport = stdio_server(stdin=stdin, stdout=stdout)
    async with transport as (read_stream, write_stream):
        print(
            f"[boot] divert={DIVERT} bootstrap_io={(_T_BOOTSTRAP - _T0) * 1000:.1f}ms "
            f"import={(_T_IMPORT - _T_BOOTSTRAP) * 1000:.1f}ms ready={(time.perf_counter() - _T0) * 1000:.1f}ms",
            file=sys.stderr,
            flush=True,
        )
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(amain)
