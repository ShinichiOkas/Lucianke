"""LSP の JSON-RPC クライアント（設計書 §7.5.3、ADR-12・ADR-14、仕様書 §12.2・§12.3）。

``LspClient`` は **1 プロセス 1 クライアント**。子プロセスの起動と回収は ``ProcessRegistry`` に任せ、ここは次だけを持つ。

* ``Content-Length`` 枠の読み書き（UTF-8）。読み取りは 1 本のタスクが行い、応答を ``id → Future`` に配り、通知と
  サーバーからの要求をハンドラへ回す。枠が壊れたら（ヘッダ不正・長さ不正・JSON でない本文・途中の EOF）ログに残して
  **切断扱い**にする: 待っている要求は ``ConnectionError`` で失敗し、以後の ``request`` / ``notify`` も ``ConnectionError``。
* 要求のタイムアウト: 超過で ``$/cancelRequest`` を送ってから ``TimeoutError``（呼び手が降格する）。
* サーバーからの要求への既定の応答（``workspace/configuration`` は ``set_settings`` の設定木から引き、無ければ空の
  ``{}``。``client/registerCapability`` などは ``null``。未登録の method は MethodNotFound）。
* 位置の変換（ADR-14）。内部の ``Position``（0 始まり・コードポイント）と、``initialize`` で交渉した単位
  （``negotiated_encoding``）の LSP の位置を相互に変換する。**LSP との位置の受け渡しはこの境界だけで行う**。

ハンドラの規約: 同期関数は読み取りタスクの中でその場で呼ぶ（受信順を保つ。``window/workDoneProgress/create`` の直後の
``$/progress`` など）。コルーチンを返すハンドラは別タスクで待つ（読み取りを止めない。中で ``request`` を待ってよい）。

状態機械・文書同期・診断の版待ち・機能呼び出しは ``pool`` / ``documents`` / ``diagnostics`` / ``features`` の仕事で、ここには無い。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from lucianke.core.model import Position, Range
from lucianke.core.procs import ProcessRegistry
from lucianke.core.textpos import LineIndex, Unit

_log = logging.getLogger("lucianke.lsp.client")

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
REQUEST_CANCELLED = -32800

MAX_MESSAGE_BYTES = 256 * 1024 * 1024
"""1 メッセージの本文の上限。これを超える ``Content-Length`` は壊れた枠として切断する。"""

STDERR_TAIL_LINES = 50

NotificationHandler = Callable[[Any], Any]
"""通知のハンドラ: ``params`` を受け、同期関数か、コルーチンを返す関数。"""

RequestHandler = Callable[[Any], Any]
"""サーバーからの要求のハンドラ: ``params`` を受け、結果（JSON にできる値）を返す。コルーチンも可。``LspError`` で失敗を返せる。"""


class LspError(Exception):
    """サーバーのエラー応答（JSON-RPC の ``error``）。サーバーからの要求にエラーを返すハンドラも、これを送出できる。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"LSP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class _FrameError(Exception):
    """枠（ヘッダ・長さ・本文）の不正。読み取りタスクが切断扱いにする。"""


# --- 位置の変換（ADR-14） ---------------------------------------------------------------


def to_lsp_position(line_index: LineIndex, pos: Position, encoding: Unit = Unit.UTF16) -> dict[str, int]:
    """内部の ``Position`` → LSP の ``{"line", "character"}``（``encoding`` 単位の列）。

    行末を越える列・文字の途中の列は行末 / 直前の文字境界に丸める。範囲外の行は ``ValueError``。
    """
    (line, col), clamped = line_index.to_unit(pos, encoding)
    if clamped:
        _log.debug("position %s was clamped when converting to %s", pos, encoding.value)
    return {"line": line, "character": col}


def from_lsp_position(
    line_index: LineIndex, lsp_pos: Mapping[str, Any], encoding: Unit = Unit.UTF16
) -> Position:
    """LSP の ``{"line", "character"}``（``encoding`` 単位の列）→ 内部の ``Position``。

    LSP の約束どおり、行末を越える列は行末に、文書末を越える行は文書末に丸める（TypeScript の ``2147483647`` など）。
    文字の途中を指す列は直前の文字境界に丸める。負の値は ``ValueError``。
    """
    line, col = int(lsp_pos["line"]), int(lsp_pos["character"])
    if line < 0 or col < 0:
        raise ValueError(f"negative LSP position: {lsp_pos!r}")
    if line >= line_index.line_count:
        last = line_index.line_count - 1
        return Position(last, line_index.line_length(last, Unit.CODEPOINT))
    pos, clamped = line_index.from_unit(line, col, encoding)
    if clamped:
        _log.debug("LSP position %s was clamped when converting from %s", dict(lsp_pos), encoding.value)
    return pos


def to_lsp_range(line_index: LineIndex, rng: Range, encoding: Unit = Unit.UTF16) -> dict[str, dict[str, int]]:
    """内部の ``Range`` → LSP の ``{"start", "end"}``。"""
    return {
        "start": to_lsp_position(line_index, rng.start, encoding),
        "end": to_lsp_position(line_index, rng.end, encoding),
    }


def from_lsp_range(line_index: LineIndex, lsp_range: Mapping[str, Any], encoding: Unit = Unit.UTF16) -> Range:
    """LSP の ``{"start", "end"}`` → 内部の ``Range``。丸めた結果 ``end`` が ``start`` より前になれば ``ValueError``。"""
    return Range(
        from_lsp_position(line_index, lsp_range["start"], encoding),
        from_lsp_position(line_index, lsp_range["end"], encoding),
    )


# --- initialize の既定の capabilities ---------------------------------------------------


def default_client_capabilities() -> dict[str, Any]:
    """M1（ADR-15）の要求に必要なだけの ``ClientCapabilities``。動的登録はしない（静的な能力だけで足りる）。

    ``positionEncodings`` は ``["utf-8", "utf-16"]``（仕様書 §12.2）。サーバーが選んだ単位は ``initialize`` の応答から
    ``LspClient.negotiated_encoding`` に入る。
    """
    kinds = [
        "",
        "quickfix",
        "refactor",
        "refactor.extract",
        "refactor.inline",
        "refactor.rewrite",
        "source",
        "source.organizeImports",
        "source.fixAll",
    ]
    return {
        "general": {"positionEncodings": ["utf-8", "utf-16"]},
        "textDocument": {
            "synchronization": {
                "dynamicRegistration": False,
                "didSave": False,
                "willSave": False,
                "willSaveWaitUntil": False,
            },
            "publishDiagnostics": {
                "relatedInformation": True,
                "versionSupport": True,
                "codeDescriptionSupport": True,
                "dataSupport": True,
                "tagSupport": {"valueSet": [1, 2]},
            },
            "definition": {"dynamicRegistration": False, "linkSupport": False},
            "references": {"dynamicRegistration": False},
            "hover": {"dynamicRegistration": False, "contentFormat": ["markdown", "plaintext"]},
            "rename": {
                "dynamicRegistration": False,
                "prepareSupport": True,
                "prepareSupportDefaultBehavior": 1,
            },
            "codeAction": {
                "dynamicRegistration": False,
                "isPreferredSupport": True,
                "dataSupport": True,
                "resolveSupport": {"properties": ["edit"]},
                "codeActionLiteralSupport": {"codeActionKind": {"valueSet": kinds}},
            },
            "callHierarchy": {"dynamicRegistration": False},
        },
        "workspace": {
            "configuration": True,
            "workspaceFolders": True,
            "applyEdit": False,
            "workspaceEdit": {"documentChanges": True, "resourceOperations": [], "failureHandling": "abort"},
            "didChangeConfiguration": {"dynamicRegistration": False},
            "didChangeWatchedFiles": {"dynamicRegistration": False},
        },
        "window": {"workDoneProgress": True, "showMessage": {}},
    }


def _null(_params: Any) -> None:
    return None


# window/logMessage・showMessage の MessageType（1 Error / 2 Warning / 3 Info / 4 Log）→ ログ水準。
# サーバーの Info / Log は起動時の定型文が多いので debug に落とす。
_MESSAGE_LEVELS = {1: logging.ERROR, 2: logging.WARNING, 3: logging.DEBUG, 4: logging.DEBUG}


class LspClient:
    """1 つの LSP 子プロセスと話す JSON-RPC クライアント。

    使い方: ``c = LspClient("basedpyright")`` → ``await c.start(argv, cwd=..., env=..., registry=...)`` →
    ``await c.initialize(...)`` →（要求・通知）→ ``await c.shutdown(grace_ms)``。
    """

    def __init__(self, name: str = "lsp") -> None:
        self.name = name
        self.negotiated_encoding: Unit = Unit.UTF16
        """位置の単位。``initialize`` の応答の ``positionEncoding``（無ければ UTF-16。仕様書 §12.2）。"""
        self.server_capabilities: dict[str, Any] = {}
        self.server_info: dict[str, Any] | None = None

        self._settings: dict[str, Any] = {}
        self._workspace_folders: list[dict[str, str]] = []
        self._proc: asyncio.subprocess.Process | None = None
        self._registry: ProcessRegistry | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notification_handlers: dict[str, list[NotificationHandler]] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._io_tasks: list[asyncio.Task[None]] = []
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._closed_reason: str | None = None
        self._closed = asyncio.Event()
        self._shutdown_started = False

        self.on_request("workspace/configuration", self._on_configuration)
        self.on_request("workspace/workspaceFolders", self._on_workspace_folders)
        self.on_request("client/registerCapability", _null)
        self.on_request("client/unregisterCapability", _null)
        self.on_request("window/workDoneProgress/create", _null)
        self.on_notification("window/logMessage", self._on_message)
        self.on_notification("window/showMessage", self._on_message)

    # --- 状態 -----------------------------------------------------------------------

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    @property
    def is_connected(self) -> bool:
        """起動済みで、切断扱いにも ``shutdown`` にもなっていない。"""
        return self._proc is not None and self._closed_reason is None

    @property
    def closed_reason(self) -> str | None:
        """切断扱いになった理由（``server closed stdout`` / ``bad frame: ...`` / ``shutdown`` ...）。接続中は ``None``。"""
        return self._closed_reason

    async def wait_closed(self) -> None:
        """切断扱いになる（サーバーの終了・枠の破損・``shutdown``）まで待つ。プールがクラッシュを検知するのに使う。"""
        await self._closed.wait()

    @property
    def stderr_tail(self) -> list[str]:
        """子の ``stderr`` の直近 ``STDERR_TAIL_LINES`` 行（起動失敗・クラッシュの理由を ``status`` に出すため）。"""
        return list(self._stderr_tail)

    # --- ハンドラ -------------------------------------------------------------------

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        """通知 ``method`` のハンドラを追加する（同じ method に複数登録でき、登録順に呼ぶ）。"""
        self._notification_handlers.setdefault(method, []).append(handler)

    def on_request(self, method: str, handler: RequestHandler) -> None:
        """サーバーからの要求 ``method`` のハンドラを登録する（1 つだけ。既定のものは置き換わる）。"""
        self._request_handlers[method] = handler

    def set_settings(self, settings: Mapping[str, Any]) -> None:
        """``workspace/configuration`` の応答の元にする設定木。セクション ``a.b`` は ``settings["a"]["b"]``。"""
        self._settings = copy.deepcopy(dict(settings))

    def _on_configuration(self, params: Any) -> list[Any]:
        items = (params or {}).get("items") or []
        return [self._lookup_setting(item.get("section")) for item in items]

    def _lookup_setting(self, section: str | None) -> Any:
        """``section`` の設定。空のセクションは設定木の全体、見つからなければ空の ``{}``。"""
        if not section:
            return copy.deepcopy(self._settings)
        node: Any = self._settings
        if isinstance(node, dict) and section in node:
            return copy.deepcopy(node[section])
        for part in section.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return {}
        return copy.deepcopy(node)

    def _on_workspace_folders(self, _params: Any) -> list[dict[str, str]] | None:
        return list(self._workspace_folders) or None

    def _on_message(self, params: Any) -> None:
        """``window/logMessage`` / ``window/showMessage`` をログへ。"""
        if not isinstance(params, dict):
            return
        level = _MESSAGE_LEVELS.get(params.get("type"), logging.DEBUG)
        _log.log(level, "[%s] %s", self.name, params.get("message"))

    # --- 起動 -----------------------------------------------------------------------

    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
        registry: ProcessRegistry,
    ) -> None:
        """``argv`` を ``registry`` 経由で起動し、読み取りタスクを始める。起動の失敗（``FileNotFoundError`` など）はそのまま送出する。"""
        if self._proc is not None:
            raise RuntimeError("LspClient is already started")
        proc = await registry.spawn(argv, cwd=cwd, env=env, name=f"lsp:{self.name}")
        self._proc = proc
        self._registry = registry
        self._io_tasks = [
            asyncio.create_task(self._read_loop(proc), name=f"lsp-read:{self.name}"),
            asyncio.create_task(self._stderr_loop(proc), name=f"lsp-stderr:{self.name}"),
        ]

    async def initialize(
        self,
        *,
        root_uri: str,
        root_path: str | os.PathLike[str] | None = None,
        initialization_options: Mapping[str, Any] | None = None,
        capabilities: Mapping[str, Any] | None = None,
        client_name: str = "lucianke",
        client_version: str = "0",
        timeout_ms: int = 60_000,
    ) -> dict[str, Any]:
        """``initialize`` → ``initialized``。応答の ``positionEncoding`` などを保持して、応答の本体を返す。

        ``capabilities`` を省くと ``default_client_capabilities()``。ワークスペースは ``root_uri`` 1 つ。
        """
        name = os.path.basename(os.fspath(root_path or root_uri.rstrip("/"))) or "workspace"
        self._workspace_folders = [{"uri": root_uri, "name": name}]
        params: dict[str, Any] = {
            "processId": os.getpid(),
            "clientInfo": {"name": client_name, "version": client_version},
            "rootUri": root_uri,
            "workspaceFolders": self._workspace_folders,
            "capabilities": dict(capabilities) if capabilities is not None else default_client_capabilities(),
            "trace": "off",
        }
        if root_path is not None:
            params["rootPath"] = os.fspath(root_path)
        if initialization_options is not None:
            params["initializationOptions"] = dict(initialization_options)
        result = await self.request("initialize", params, timeout_ms=timeout_ms)
        self.notify("initialized", {})
        return result

    def _note_initialize_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        caps = result.get("capabilities")
        self.server_capabilities = caps if isinstance(caps, dict) else {}
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else None
        raw = self.server_capabilities.get("positionEncoding")
        if raw is None:
            self.negotiated_encoding = Unit.UTF16
            return
        try:
            self.negotiated_encoding = Unit(raw)
        except ValueError:
            _log.warning("[%s] unknown positionEncoding %r; assuming utf-16", self.name, raw)
            self.negotiated_encoding = Unit.UTF16

    # --- 位置の変換（negotiated_encoding を使う） -------------------------------------

    def to_lsp_position(self, line_index: LineIndex, pos: Position) -> dict[str, int]:
        return to_lsp_position(line_index, pos, self.negotiated_encoding)

    def from_lsp_position(self, line_index: LineIndex, lsp_pos: Mapping[str, Any]) -> Position:
        return from_lsp_position(line_index, lsp_pos, self.negotiated_encoding)

    def to_lsp_range(self, line_index: LineIndex, rng: Range) -> dict[str, dict[str, int]]:
        return to_lsp_range(line_index, rng, self.negotiated_encoding)

    def from_lsp_range(self, line_index: LineIndex, lsp_range: Mapping[str, Any]) -> Range:
        return from_lsp_range(line_index, lsp_range, self.negotiated_encoding)

    # --- 送信 -----------------------------------------------------------------------

    def _check_connected(self) -> None:
        if self._proc is None:
            raise ConnectionError(f"LSP client {self.name!r} is not started")
        if self._closed_reason is not None:
            raise ConnectionError(f"LSP client {self.name!r} is disconnected: {self._closed_reason}")

    def _write(self, msg: dict[str, Any]) -> None:
        self._check_connected()
        assert self._proc is not None
        stdin = self._proc.stdin
        if stdin is None or stdin.is_closing():
            self._mark_closed("server stdin closed")
            raise ConnectionError(f"LSP client {self.name!r} is disconnected: server stdin closed")
        body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug("[%s] -> %s id=%s", self.name, msg.get("method") or "response", msg.get("id"))
        try:
            stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        except (ConnectionError, OSError) as e:
            self._mark_closed(f"write failed: {e!r}")
            raise ConnectionError(f"LSP client {self.name!r} is disconnected: write failed") from e

    def notify(self, method: str, params: Any = None) -> None:
        """通知を送る（待たない）。切断扱いのときは ``ConnectionError``。"""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    async def request(self, method: str, params: Any, *, timeout_ms: int | float) -> Any:
        """要求を送って結果を返す。

        * エラー応答は ``LspError(code, message, data)``。
        * ``timeout_ms`` を超えたら ``$/cancelRequest`` を送ってから ``TimeoutError``（呼び手が降格する）。
        * 切断扱い（未起動を含む）、または待っている間に切断されたら ``ConnectionError``。
        * 待っているタスクが取り消されたときも ``$/cancelRequest`` を送る。
        """
        self._check_connected()
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
            result = await asyncio.wait_for(fut, timeout=max(timeout_ms, 0) / 1000)
        except (TimeoutError, asyncio.CancelledError):
            self._pending.pop(rid, None)
            with contextlib.suppress(ConnectionError):
                self.notify("$/cancelRequest", {"id": rid})
            raise
        except BaseException:
            self._pending.pop(rid, None)
            raise
        if method == "initialize":
            self._note_initialize_result(result)
        return result

    def _respond(self, rid: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        try:
            self._write(msg)
        except ConnectionError:
            _log.debug("[%s] could not answer server request id=%s: disconnected", self.name, rid)

    # --- 受信 -----------------------------------------------------------------------

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        reason = "server closed stdout"
        try:
            assert proc.stdout is not None
            while True:
                msg = await self._read_message(proc.stdout)
                if msg is None:
                    break
                self._dispatch(msg)
        except _FrameError as e:
            reason = f"bad frame: {e}"
            _log.error("[%s] %s; treating the server as disconnected", self.name, reason)
        except (ConnectionError, OSError) as e:
            reason = f"read failed: {e!r}"
        except asyncio.CancelledError:
            reason = "reader cancelled"
            raise
        except Exception:
            reason = "reader crashed"
            _log.exception("[%s] LSP reader crashed", self.name)
        finally:
            self._mark_closed(reason)

    @staticmethod
    async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
        """1 メッセージを読む。メッセージの間の EOF は ``None``、枠が壊れていれば ``_FrameError``。"""
        try:
            header = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as e:
            if not e.partial:
                return None
            raise _FrameError(f"EOF inside a header ({len(e.partial)} bytes)") from e
        except asyncio.LimitOverrunError as e:
            raise _FrameError("header too long") from e
        length: int | None = None
        for line in header[:-4].split(b"\r\n"):
            key, sep, value = line.partition(b":")
            if not sep:
                raise _FrameError(f"malformed header line {line[:80]!r}")
            if key.strip().lower() == b"content-length":
                text = value.strip()
                if not text.isdigit():
                    raise _FrameError(f"invalid Content-Length {text[:40]!r}")
                length = int(text)
        if length is None:
            raise _FrameError("missing Content-Length")
        if length > MAX_MESSAGE_BYTES:
            raise _FrameError(f"Content-Length too large: {length}")
        try:
            body = await reader.readexactly(length)
        except asyncio.IncompleteReadError as e:
            raise _FrameError(f"EOF inside a body ({len(e.partial)} of {length} bytes)") from e
        try:
            msg = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise _FrameError(f"body is not valid UTF-8 JSON: {e}") from e
        if not isinstance(msg, dict):
            raise _FrameError(f"body is not a JSON object: {type(msg).__name__}")
        return msg

    async def _stderr_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                self._stderr_tail.append(text)
                _log.debug("[%s] stderr: %s", self.name, text)
        except (ConnectionError, OSError):
            return

    def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug("[%s] <- %s id=%s", self.name, method or "response", msg.get("id"))
        if isinstance(method, str):
            if "id" in msg:
                self._handle_server_request(msg["id"], method, msg.get("params"))
            else:
                self._handle_notification(method, msg.get("params"))
        elif "id" in msg:
            self._handle_response(msg)
        else:
            _log.warning(
                "[%s] ignoring a message that is neither request, notification nor response", self.name
            )

    def _handle_response(self, msg: dict[str, Any]) -> None:
        fut = self._pending.pop(msg["id"], None) if isinstance(msg["id"], int) else None
        if fut is None or fut.done():
            _log.debug("[%s] dropping a response for unknown or finished id=%r", self.name, msg["id"])
            return
        err = msg.get("error")
        if err is None:
            fut.set_result(msg.get("result"))
        elif isinstance(err, dict):
            code = err.get("code")
            fut.set_exception(
                LspError(
                    code if isinstance(code, int) else INTERNAL_ERROR,
                    str(err.get("message", "")),
                    err.get("data"),
                )
            )
        else:
            fut.set_exception(LspError(INTERNAL_ERROR, f"malformed error object: {err!r}"))

    def _handle_notification(self, method: str, params: Any) -> None:
        for handler in list(self._notification_handlers.get(method, ())):
            try:
                result = handler(params)
            except Exception:
                _log.exception("[%s] notification handler for %s failed", self.name, method)
                continue
            if inspect.isawaitable(result):
                self._spawn(self._await_notification(method, result))

    async def _await_notification(self, method: str, awaitable: Awaitable[Any]) -> None:
        try:
            await awaitable
        except Exception:
            _log.exception("[%s] async notification handler for %s failed", self.name, method)

    def _handle_server_request(self, rid: Any, method: str, params: Any) -> None:
        handler = self._request_handlers.get(method)
        if handler is None:
            self._respond(rid, error={"code": METHOD_NOT_FOUND, "message": f"method not found: {method}"})
            return
        try:
            result = handler(params)
        except Exception as e:
            self._respond(rid, error=self._handler_error(method, e))
            return
        if inspect.isawaitable(result):
            self._spawn(self._await_server_request(rid, method, result))
        else:
            self._respond(rid, result=result)

    async def _await_server_request(self, rid: Any, method: str, awaitable: Awaitable[Any]) -> None:
        try:
            result = await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._respond(rid, error=self._handler_error(method, e))
        else:
            self._respond(rid, result=result)

    def _handler_error(self, method: str, e: Exception) -> dict[str, Any]:
        if isinstance(e, LspError):
            error: dict[str, Any] = {"code": e.code, "message": e.message}
            if e.data is not None:
                error["data"] = e.data
            return error
        _log.error("[%s] request handler for %s failed", self.name, method, exc_info=e)
        return {"code": INTERNAL_ERROR, "message": f"internal error in handler for {method}"}

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    # --- 切断・終了 -----------------------------------------------------------------

    def _mark_closed(self, reason: str) -> None:
        """切断扱いにする（1 回だけ）。待っている要求は ``ConnectionError`` で失敗させる。"""
        if self._closed_reason is not None:
            return
        self._closed_reason = reason
        level = logging.DEBUG if self._shutdown_started else logging.WARNING
        _log.log(level, "[%s] LSP disconnected: %s (exit code %s)", self.name, reason, self.returncode)
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(f"LSP client {self.name!r} is disconnected: {reason}"))
        self._closed.set()

    async def shutdown(self, grace_ms: int = 500) -> None:
        """``shutdown`` → ``exit`` → 終了を待つ → 残っていれば ``ProcessRegistry.terminate``。何度呼んでもよい。

        すでに切断扱い・終了済みのときは ``shutdown`` 要求を省いて回収だけをする。戻った時点で子（と子孫）は残らない。
        """
        proc, registry = self._proc, self._registry
        if proc is None or registry is None or self._shutdown_started:
            return
        self._shutdown_started = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_ms / 1000
        if self._closed_reason is None and proc.returncode is None:
            try:
                await self.request("shutdown", None, timeout_ms=grace_ms)
                self.notify("exit")
                await asyncio.wait_for(proc.wait(), timeout=max(deadline - loop.time(), 0))
            except (TimeoutError, LspError, ConnectionError, OSError) as e:
                _log.debug("[%s] graceful shutdown did not finish: %r", self.name, e)
        try:
            await registry.terminate(proc, grace_ms)
        finally:
            self._mark_closed("shutdown")
            await self._stop_tasks()

    async def _stop_tasks(self) -> None:
        tasks = [*self._io_tasks, *self._handler_tasks]
        current = asyncio.current_task()
        tasks = [t for t in tasks if t is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
