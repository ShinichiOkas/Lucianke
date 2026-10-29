"""最小の LSP クライアント（S-4 / S-5 / S-11 のスパイク用）。使い捨て。

使い方: `from lsp_probe import LspClient, ...` で読み込む。直接実行すると診断だけの簡易確認ができる:
  uv run python spikes/s04_lsp/lsp_probe.py basedpyright   # 起動 → initialize → shutdown だけ
Content-Length 枠の JSON-RPC を asyncio のサブプロセスで話し、サーバーからの要求に応答し、通知を時刻つきで記録する。
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TRACE = bool(os.environ.get("LSP_TRACE"))


def path_to_uri(p: Path) -> str:
    """Windows は file:///S:/... にする（ドライブ文字は大文字のまま。サーバー側の正規化は別途見る）。"""
    return p.resolve().as_uri()


def norm_uri(u: str) -> str:
    """URI の比較用正規化。basedpyright は file:///s%3A/... のようにドライブ文字を小文字・%3A で返すことがある。"""
    from urllib.parse import unquote
    return unquote(u).lower()


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def col_in(s: str, cp_index: int, unit: str) -> int:
    """行 s の先頭から cp_index 個のコードポイントを進めた位置の列を、unit（utf-8/utf-16/cp）で返す。"""
    head = s[:cp_index]
    if unit == "utf-8":
        return utf8_len(head)
    if unit == "utf-16":
        return utf16_len(head)
    return len(head)


def cp_from_col(s: str, col: int, unit: str) -> int | None:
    """行 s で、unit の列 col がコードポイント何個ぶんの位置かを返す（境界に合わなければ None）。"""
    for i in range(len(s) + 1):
        if col_in(s, i, unit) == col:
            return i
    return None


def apply_text_edits(text: str, edits: list[dict[str, Any]], unit: str) -> str:
    """LSP の TextEdit 列を text に適用する。列は unit（utf-8 / utf-16 / cp）で解釈する。後ろの編集から適用。"""
    lines = text.split("\n")
    starts = [0]
    for ln in lines[:-1]:
        starts.append(starts[-1] + len(ln) + 1)

    def idx(pos: dict[str, int]) -> int:
        line = pos["line"]
        if line >= len(lines):
            return len(text)
        cp = cp_from_col(lines[line], pos["character"], unit)
        if cp is None:
            raise ValueError(f"列 {pos['character']} は {unit} の文字境界に合わない: {lines[line]!r}")
        return starts[line] + cp

    out = text
    for e in sorted(edits, key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]), reverse=True):
        a, b = idx(e["range"]["start"]), idx(e["range"]["end"])
        out = out[:a] + e["newText"] + out[b:]
    return out


class LspError(Exception):
    def __init__(self, error: dict[str, Any]):
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.error = error


@dataclass
class Notif:
    t: float  # クライアント生成からの秒
    method: str
    params: Any


@dataclass
class LspClient:
    name: str
    cmd: list[str]
    cwd: Path
    config_provider: Callable[[dict[str, Any]], Any] | None = None  # workspace/configuration の 1 項目 → 値
    env: dict[str, str] | None = None  # None なら親の環境を継承
    notifications: list[Notif] = field(default_factory=list)
    server_requests: list[Notif] = field(default_factory=list)
    stderr_lines: list[tuple[float, str]] = field(default_factory=list)
    proc: asyncio.subprocess.Process | None = None
    encoding: str = "utf-16"
    server_capabilities: dict[str, Any] = field(default_factory=dict)
    server_info: dict[str, Any] | None = None
    timings: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._t0 = time.perf_counter()
        self._tasks: list[asyncio.Task[None]] = []
        self._wakeup = asyncio.Event()
        self.root_uri = path_to_uri(self.cwd)

    # ---- 時刻 ----
    def now(self) -> float:
        return time.perf_counter() - self._t0

    # ---- 起動・終了 ----
    async def start(self) -> None:
        self._t0 = time.perf_counter()
        self.proc = await asyncio.create_subprocess_exec(
            *self.cmd,
            cwd=str(self.cwd),
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.timings["spawned"] = self.now()
        self._tasks.append(asyncio.create_task(self._read_loop()))
        self._tasks.append(asyncio.create_task(self._stderr_loop()))

    async def shutdown(self, grace_s: float = 5.0) -> dict[str, Any]:
        """shutdown → exit → 終了確認。残ったら terminate → kill。結果を返す。"""
        res: dict[str, Any] = {"shutdown_ok": False, "exit_code": None, "forced": None}
        if self.proc is None:
            return res
        try:
            if self.proc.returncode is None:
                t = time.perf_counter()
                await self.request("shutdown", None, timeout=10)
                res["shutdown_ms"] = round((time.perf_counter() - t) * 1000, 1)
                res["shutdown_ok"] = True
                self.notify("exit", None)
            await asyncio.wait_for(self.proc.wait(), timeout=grace_s)
        except Exception as e:  # noqa: BLE001
            res["error"] = repr(e)
            if self.proc.returncode is None:
                res["forced"] = "terminate"
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2)
                except TimeoutError:
                    res["forced"] = "kill"
                    self.proc.kill()
                    await self.proc.wait()
        res["exit_code"] = self.proc.returncode
        for t in self._tasks:
            t.cancel()
        return res

    # ---- 送受信 ----
    def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        if TRACE:
            print(f"[{self.now():8.3f}] -> {msg.get('method') or 'response'} id={msg.get('id')}", file=sys.stderr, flush=True)
        body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)

    async def request(self, method: str, params: Any, *, timeout: float = 30.0) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(rid, None)
            self._send({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": rid}})
            raise

    def notify(self, method: str, params: Any) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        r = self.proc.stdout
        try:
            while True:
                header = await r.readuntil(b"\r\n\r\n")
                length = None
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if length is None:
                    continue
                body = await r.readexactly(length)
                msg = json.loads(body.decode("utf-8"))
                self._dispatch(msg)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("server closed stdout"))

    async def _stderr_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self.stderr_lines.append((self.now(), line.decode("utf-8", "replace").rstrip()))

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if TRACE:
            print(f"[{self.now():8.3f}] <- {msg.get('method') or 'response'} id={msg.get('id')}", file=sys.stderr, flush=True)
        if "method" in msg and "id" in msg:  # サーバーからの要求
            self.server_requests.append(Notif(self.now(), msg["method"], msg.get("params")))
            self._answer_server_request(msg)
        elif "method" in msg:  # 通知
            self.notifications.append(Notif(self.now(), msg["method"], msg.get("params")))
            self._wakeup.set()
        else:  # 応答
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                fut.set_exception(LspError(msg["error"]))
            else:
                fut.set_result(msg.get("result"))

    def _answer_server_request(self, msg: dict[str, Any]) -> None:
        method, params, rid = msg["method"], msg.get("params") or {}, msg["id"]
        result: Any = None
        if method == "workspace/configuration":
            items = params.get("items", [])
            result = [self.config_provider(it) if self.config_provider else None for it in items]
        elif method in ("client/registerCapability", "client/unregisterCapability"):
            result = None
        elif method == "window/workDoneProgress/create":
            result = None
        elif method == "workspace/workspaceFolders":
            result = [{"uri": self.root_uri, "name": self.cwd.name}]
        elif method == "window/showMessageRequest":
            result = None
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "spike client does not apply edits"}
        elif method in ("workspace/diagnostic/refresh", "workspace/inlayHint/refresh",
                        "workspace/semanticTokens/refresh", "workspace/codeLens/refresh"):
            result = None
        else:
            self._send({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": f"method not found: {method}"}})
            return
        self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    # ---- 通知の待機 ----
    def find(self, method: str, pred: Callable[[Any], bool] | None = None, *, after: float = 0.0,
             start: int = 0) -> Notif | None:
        for n in self.notifications[start:]:
            if n.method == method and n.t >= after and (pred is None or pred(n.params)):
                return n
        return None

    async def wait_for(self, method: str, pred: Callable[[Any], bool] | None = None, *,
                       timeout: float = 30.0, after: float = 0.0, start: int = 0) -> Notif | None:
        deadline = time.perf_counter() + timeout
        while True:
            n = self.find(method, pred, after=after, start=start)
            if n is not None:
                return n
            left = deadline - time.perf_counter()
            if left <= 0:
                return None
            self._wakeup.clear()
            n = self.find(method, pred, after=after, start=start)
            if n is not None:
                return n
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=left)
            except TimeoutError:
                return None

    # ---- 初期化 ----
    def client_capabilities(self, *, document_changes: bool = True, pull_diagnostics: bool = False) -> dict[str, Any]:
        kinds = ["", "quickfix", "refactor", "refactor.extract", "refactor.inline", "refactor.rewrite",
                 "source", "source.organizeImports", "source.fixAll"]
        td: dict[str, Any] = {
            "synchronization": {"dynamicRegistration": False, "didSave": False,
                                "willSave": False, "willSaveWaitUntil": False},
            "publishDiagnostics": {"relatedInformation": True, "versionSupport": True,
                                   "codeDescriptionSupport": True, "dataSupport": True,
                                   "tagSupport": {"valueSet": [1, 2]}},
            "definition": {"dynamicRegistration": False, "linkSupport": False},
            "references": {"dynamicRegistration": False},
            "hover": {"dynamicRegistration": False, "contentFormat": ["markdown", "plaintext"]},
            "rename": {"dynamicRegistration": False, "prepareSupport": True,
                       "prepareSupportDefaultBehavior": 1},
            "codeAction": {
                "dynamicRegistration": False,
                "isPreferredSupport": True,
                "dataSupport": True,
                "resolveSupport": {"properties": ["edit"]},
                "codeActionLiteralSupport": {"codeActionKind": {"valueSet": kinds}},
            },
            "callHierarchy": {"dynamicRegistration": False},
        }
        if pull_diagnostics:
            td["diagnostic"] = {"dynamicRegistration": False, "relatedDocumentSupport": False}
        return {
            "general": {"positionEncodings": ["utf-8", "utf-16"]},
            "textDocument": td,
            "workspace": {
                "configuration": True,
                "workspaceFolders": True,
                "applyEdit": False,
                "workspaceEdit": {"documentChanges": document_changes, "resourceOperations": [],
                                  "failureHandling": "abort"},
                "didChangeConfiguration": {"dynamicRegistration": False},
                "didChangeWatchedFiles": {"dynamicRegistration": False},
            },
            "window": {"workDoneProgress": True, "showMessage": {}},
        }

    async def initialize(self, *, init_options: dict[str, Any] | None = None, document_changes: bool = True,
                         pull_diagnostics: bool = False,
                         encodings: list[str] | None = ('utf-8', 'utf-16'),  # type: ignore[assignment]
                         locale: str | None = None, timeout: float = 60.0) -> dict[str, Any]:
        caps = self.client_capabilities(document_changes=document_changes, pull_diagnostics=pull_diagnostics)
        if encodings is None:
            caps["general"].pop("positionEncodings")
        else:
            caps["general"]["positionEncodings"] = list(encodings)
        params: dict[str, Any] = {
            "processId": os.getpid(),
            "clientInfo": {"name": "lucianke-spike", "version": "0"},
            "rootUri": self.root_uri,
            "rootPath": str(self.cwd),
            "workspaceFolders": [{"uri": self.root_uri, "name": self.cwd.name}],
            "capabilities": caps,
            "trace": "off",
        }
        if locale is not None:
            params["locale"] = locale
        if init_options is not None:
            params["initializationOptions"] = init_options
        t = self.now()
        res = await self.request("initialize", params, timeout=timeout)
        self.timings["initialize_ms"] = round((self.now() - t) * 1000, 1)
        self.timings["initialize_done"] = self.now()
        self.server_capabilities = res.get("capabilities", {})
        self.server_info = res.get("serverInfo")
        self.encoding = self.server_capabilities.get("positionEncoding", "utf-16")
        self.notify("initialized", {})
        self.timings["initialized_sent"] = self.now()
        return res

    # ---- 文書 ----
    def did_open(self, path: Path, language_id: str, version: int = 1, text: str | None = None) -> str:
        uri = path_to_uri(path)
        if text is None:
            text = path.read_text(encoding="utf-8")
        self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language_id,
                                                              "version": version, "text": text}})
        return uri

    def did_change(self, path: Path, version: int, text: str) -> float:
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": path_to_uri(path), "version": version},
            "contentChanges": [{"text": text}],
        })
        return self.now()

    def did_close(self, path: Path) -> None:
        self.notify("textDocument/didClose", {"textDocument": {"uri": path_to_uri(path)}})

    def diag_for(self, path: Path) -> list[Notif]:
        uri = norm_uri(path_to_uri(path))
        return [n for n in self.notifications
                if n.method == "textDocument/publishDiagnostics" and norm_uri(n.params["uri"]) == uri]


async def time_requests(client: LspClient, method: str, params: Any, n: int = 10,
                        timeout: float = 30.0) -> dict[str, Any]:
    """同じ要求を 1 回（cold）+ n 回（warm）送り、応答時間 ms を返す。"""
    t = time.perf_counter()
    first = await client.request(method, params, timeout=timeout)
    cold = (time.perf_counter() - t) * 1000
    warm: list[float] = []
    last: Any = first
    for _ in range(n):
        t = time.perf_counter()
        last = await client.request(method, params, timeout=timeout)
        warm.append((time.perf_counter() - t) * 1000)
    return {"cold_ms": round(cold, 1), "warm_median_ms": round(statistics.median(warm), 1),
            "warm_min_ms": round(min(warm), 1), "warm_max_ms": round(max(warm), 1), "result": last}


def find_venv_exe(name: str) -> str | None:
    """.venv/Scripts|bin 配下の実行ファイルを探す（探索順の最初の段）。"""
    for sub in ("Scripts", "bin"):
        for ext in (".exe", ""):
            p = REPO / ".venv" / sub / f"{name}{ext}"
            if p.is_file():
                return str(p)
    return None


async def _demo(which: str) -> None:
    root = Path(__file__).parent / "fixture_py"
    exe = {"basedpyright": [find_venv_exe("basedpyright-langserver"), "--stdio"],
           "ruff": [find_venv_exe("ruff"), "server"]}[which]
    c = LspClient(which, [x for x in exe if x], root)
    await c.start()
    try:
        await c.initialize()
        print(which, c.server_info, "encoding:", c.encoding, c.timings)
    finally:
        print(await c.shutdown())


if __name__ == "__main__":
    asyncio.run(_demo(sys.argv[1] if len(sys.argv) > 1 else "basedpyright"))
