"""LSP の状態機械と遅延起動（設計書 §7.5.2・§8.3、仕様書 §12.3、S-04 の実測）。

``LspPool`` は **言語 × ワークスペース × 役（``types`` / ``lint``）ごとに 1 つの ``LspHandle``** を持つ。
各ハンドルは次の状態を渡る（``LspState``。外へ出す ``LspStatus`` では ``starting`` は ``indexing`` に含める）。

``standby``（初期・``reset``・アイドル停止） → ``starting``（コマンド探索・``spawn``・``initialize``） →
``indexing``（``initialize`` 成功。``$/progress`` を ``progress`` に反映） → ``ready`` / ``unavailable``。

* **起動は要求経路を止めない。** ``ensure_started`` は standby なら起動タスクを投げて即戻る。起動・再起動・停止は
  ハンドルごとの 1 本の「監督タスク」が行い、``standby`` からの遷移は同期（``await`` なし）で 1 回だけなので二重起動しない。
* ``ready`` の合図（S-04: basedpyright は単一文書で ``$/progress`` を出さず、診断の無いファイルでは何も届けないサーバーもある）:
  (1) ``$/progress`` の ``begin`` を見たサーバーは、**すべての ``end`` が揃ったとき**。
  (2) 進捗中でなければ、**最初の ``publishDiagnostics``**。
  (3) どちらも来なければ ``ready_fallback_s`` 後（進捗が続いていなければ。続いていても ``indexing_max_s`` で打ち切り）。
* クラッシュ（切断）・``initialize`` の失敗は ``LSP_RESTART_BACKOFF_S`` = 1/2/4 秒のバックオフで最大 ``LSP_RESTART_MAX`` 回
  再起動し、超えたら ``unavailable``（``crashed_3x`` / ``init_failed``）。起動後 ``stable_after_s`` 以上動いていたクラッシュは
  数え直す。コマンドが見つからなければ即 ``unavailable``（``not_found``）。
* 呼び手（機能呼び出し）が数える要求のタイムアウトは ``LspHandle.record_timeout`` / ``record_ok``。連続 3 回で
  プロセスを落として ``unavailable``（``timeout_3x``）。
* 終了は ``LspClient.shutdown``（``shutdown`` → ``exit`` → ``ProcessRegistry.terminate``。Windows は Job Object で孫ごと）。
* ``LspHandle.documents`` / ``.diagnostics`` はハンドルと同じ寿命で、再起動のたびに中身だけ空になる（呼び手が参照を持ち続けてよい）。
* index への診断の書き込みは呼び手の ``on_diagnostics(path, diags, version, source)`` に任せる（``source`` はサーバー名）。
  書き込みを非同期にするときは、その完了を表す awaitable か ``IndexWriter.submit`` の ``Future`` を **返す**こと
  （``DiagnosticsHub.wait_for`` はそれが済むまで返らない。捨てると EP-7 の順序が崩れる。BUG-0007）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, NamedTuple

from lucianke.core.limits import (
    LSP_IDLE_SHUTDOWN_S,
    LSP_OPEN_DOCS_MAX,
    LSP_RESTART_BACKOFF_S,
    LSP_RESTART_MAX,
)
from lucianke.core.model import LspStatus, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.client import LspClient, LspError
from lucianke.lsp.diagnostics import PUBLISH_DIAGNOSTICS, DiagnosticsHub
from lucianke.lsp.documents import DocumentSync
from lucianke.lsp.servers import (
    LspServerDef,
    Role,
    build_initialization_options,
    build_settings,
    find_command,
    find_tsserver,
    servers_for,
)
from lucianke.workspace.paths import to_file_uri
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.lsp.pool")

TIMEOUTS_TO_UNAVAILABLE = 3
"""連続したタイムアウトがこの回数に達したら ``unavailable``（設計書 §7.5.2）。"""

_ROLES: tuple[Role, ...] = ("types", "lint")

OnDiagnostics = Callable[[RelPath, list[dict[str, Any]], int | None, str], Any]
"""``on_diagnostics(path, diags, version, source)``: ``source`` は LSP サーバー名（``basedpyright`` / ``ruff`` …）。

返り値は ``DiagnosticsCallback`` と同じ: 非同期の書き込みなら awaitable か ``concurrent.futures.Future`` を返す（``lsp/diagnostics.py``）。
"""

Resolver = Callable[[LspServerDef, Workspace], list[str] | None]
"""``resolver(defn, ws) -> argv | None``: 起動コマンドの探索。``None`` は ``not_found``。"""


class LspState(StrEnum):
    STANDBY = "standby"
    STARTING = "starting"
    INDEXING = "indexing"
    READY = "ready"
    UNAVAILABLE = "unavailable"

    @property
    def public(self) -> LspStatus:
        """``_resolution_meta.lsp_status`` の値。起動中（``starting``）は降格応答の側から見て ``indexing``。"""
        return _PUBLIC[self]


_PUBLIC = {
    LspState.STANDBY: LspStatus.STANDBY,
    LspState.STARTING: LspStatus.INDEXING,
    LspState.INDEXING: LspStatus.INDEXING,
    LspState.READY: LspStatus.READY,
    LspState.UNAVAILABLE: LspStatus.UNAVAILABLE,
}


class FileChangeType(IntEnum):
    """``workspace/didChangeWatchedFiles`` の ``FileChangeType``。"""

    CREATED = 1
    CHANGED = 2
    DELETED = 3


class PoolStatus(NamedTuple):
    """``LspPool.status`` の戻り値 ``(status, progress, server_name, pid, reason)``。"""

    status: LspStatus
    progress: int | None
    server_name: str | None
    pid: int | None
    reason: str | None


def venv_root_of(ws: Workspace) -> Path | None:
    """``ws.python_env.interpreter`` が venv の中のものなら、その venv の根（``pyvenv.cfg`` のある所）。"""
    env = ws.python_env
    if env is None or not env.interpreter:
        return None
    candidate = Path(env.interpreter).parent.parent
    try:
        return candidate if (candidate / "pyvenv.cfg").is_file() else None
    except OSError:
        return None


def default_resolver(defn: LspServerDef, ws: Workspace) -> list[str] | None:
    """既定の探索（``find_command``）: 環境変数 → ワークスペースの venv → ``node_modules/.bin`` → ``PATH``。"""
    return find_command(defn, workspace_root=ws.root, venv=venv_root_of(ws))


class _Unavailable(Exception):
    """再試行しても直らない起動の失敗（``reason`` は ``not_found`` / ``init_failed``）。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class LspHandle:
    """言語 × ワークスペース × 役の LSP 1 つ。状態の読み取りと、機能呼び出しが使う ``client`` / ``documents`` / ``diagnostics``。"""

    def __init__(self, pool: LspPool, language_id: str, ws: Workspace, role: Role) -> None:
        self.language_id = language_id
        self.ws = ws
        self.role: Role = role
        self.state = LspState.STANDBY
        self.reason: str | None = None
        self.progress: int | None = None
        self.server_name: str | None = None
        self.defn: LspServerDef | None = None
        self.client: LspClient | None = None
        self.restarts = 0
        self.timeouts = 0
        self.last_error: str | None = None
        self.last_used = pool._clock()
        self.history: deque[LspState] = deque([LspState.STANDBY], maxlen=64)
        self.documents = DocumentSync(ws, self._notify, max_open=pool._open_docs_max)
        self.diagnostics = DiagnosticsHub(ws, source=language_id, on_diagnostics=self._on_diagnostics)
        self.documents.on_close = self.diagnostics.forget

        self._pool = pool
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._waiters: list[asyncio.Future[None]] = []
        self._tokens: set[Any] = set()
        self._gen = 0
        self._indexing_since = 0.0
        self._up_since: float | None = None
        self._fallback: asyncio.TimerHandle | None = None

    # --- 読み取り -------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self.state is LspState.READY

    @property
    def pid(self) -> int | None:
        client = self.client
        return client.pid if client is not None and client.is_connected else None

    def snapshot(self) -> PoolStatus:
        return PoolStatus(self.state.public, self.progress, self.server_name, self.pid, self.reason)

    def touch(self) -> None:
        """要求で使った（アイドル停止の判定を延ばす）。"""
        self.last_used = self._pool._clock()

    # --- 要求のタイムアウトの計数（設計書 §8.3） ---------------------------------------

    def record_ok(self) -> None:
        """要求が時間内に答えた: 連続タイムアウトの数え直し。"""
        self.timeouts = 0

    def record_timeout(self) -> bool:
        """要求がタイムアウトした: 連続 3 回に達したらプロセスを落として ``unavailable``（``timeout_3x``）にして ``True``。"""
        self.timeouts += 1
        if self.timeouts < TIMEOUTS_TO_UNAVAILABLE or self._stopping:
            return False
        if self.state in (LspState.UNAVAILABLE, LspState.STANDBY):
            return False
        self._stopping = True  # 二重に止めない（_stop が最後に戻す）
        _log.warning(
            "LSP %s/%s: %d consecutive request timeouts; marking it unavailable",
            self.language_id,
            self.server_name,
            self.timeouts,
        )
        self._pool._spawn_bg(self._pool._stop(self, LspState.UNAVAILABLE, "timeout_3x"))
        return True

    # --- 内部: 状態 -----------------------------------------------------------------

    def _set_state(self, state: LspState, reason: str | None = None) -> None:
        if state is not self.state:
            self.history.append(state)
            _log.debug(
                "LSP %s/%s: %s -> %s (%s)",
                self.language_id,
                self.server_name,
                self.state.value,
                state.value,
                reason,
            )
        self.state = state
        self.reason = reason
        if state is LspState.READY:
            self.progress = None
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    async def _wait_state(self, done: Callable[[], bool], timeout_s: float) -> bool:
        """``done()`` が真になるまで状態の変化を待つ。時間切れで ``False``。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_s, 0)
        while not done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            fut: asyncio.Future[None] = loop.create_future()
            self._waiters.append(fut)
            try:
                await asyncio.wait_for(fut, timeout=remaining)
            except TimeoutError:
                return done()
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)
        return True

    # --- 内部: クライアントのハンドラ -------------------------------------------------

    def _notify(self, method: str, params: Any) -> None:
        client = self.client
        if client is None:
            raise ConnectionError(f"LSP {self.language_id!r} is not running")
        client.notify(method, params)

    def _on_diagnostics(self, path: RelPath, diags: list[dict[str, Any]], version: int | None) -> Any:
        cb = self._pool._on_diagnostics
        if cb is None:
            return None
        return cb(path, diags, version, self.server_name or self.language_id)

    def _on_progress_create(self, _params: Any) -> None:
        return None

    def _on_progress(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        value = params.get("value")
        if not isinstance(value, dict):
            return
        token = params.get("token")
        kind = value.get("kind")
        pct = value.get("percentage")
        if isinstance(pct, int | float) and not isinstance(pct, bool) and self.state is not LspState.READY:
            self.progress = max(0, min(100, int(pct)))
        if kind == "begin":
            with contextlib.suppress(TypeError):
                self._tokens.add(token)
        elif kind == "end":
            self._tokens.discard(token)
            if not self._tokens:
                self.diagnostics.settle_all()
                self._become_ready()

    def _on_first_publish(self, _params: Any) -> None:
        if not self._tokens:
            self._become_ready()

    def _become_ready(self) -> None:
        if self.state is LspState.INDEXING:
            self._cancel_fallback()
            self._set_state(LspState.READY)

    def _arm_fallback(self) -> None:
        self._cancel_fallback()
        gen = self._gen
        self._fallback = asyncio.get_running_loop().call_later(
            self._pool._ready_fallback_s, self._fallback_fire, gen
        )

    def _cancel_fallback(self) -> None:
        if self._fallback is not None:
            self._fallback.cancel()
            self._fallback = None

    def _fallback_fire(self, gen: int) -> None:
        self._fallback = None
        if gen != self._gen or self.state is not LspState.INDEXING:
            return
        if self._tokens and self._pool._clock() - self._indexing_since < self._pool._indexing_max_s:
            self._arm_fallback()  # 進捗が続いている間は待つ
            return
        _log.debug("LSP %s/%s: no readiness signal; promoting to ready", self.language_id, self.server_name)
        self._become_ready()


class LspPool:
    """言語 × ワークスペース × 役ごとの ``LspHandle`` の表と、その生涯の管理。"""

    def __init__(
        self,
        registry: ProcessRegistry,
        *,
        on_diagnostics: OnDiagnostics | None = None,
        servers: Sequence[LspServerDef] | None = None,
        resolver: Resolver | None = None,
        backoff_s: Sequence[float] = LSP_RESTART_BACKOFF_S,
        restart_max: int = LSP_RESTART_MAX,
        open_docs_max: int = LSP_OPEN_DOCS_MAX,
        init_timeout_ms: int = 60_000,
        ready_fallback_s: float = 15.0,
        indexing_max_s: float = 300.0,
        stable_after_s: float = 60.0,
        grace_ms: int = 500,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not backoff_s:
            raise ValueError("backoff_s must not be empty")
        self._registry = registry
        self._on_diagnostics = on_diagnostics
        self._servers = tuple(servers) if servers is not None else None
        self._resolver: Resolver = resolver or default_resolver
        self._backoff_s = tuple(backoff_s)
        self._restart_max = restart_max
        self._open_docs_max = open_docs_max
        self._init_timeout_ms = init_timeout_ms
        self._ready_fallback_s = ready_fallback_s
        self._indexing_max_s = indexing_max_s
        self._stable_after_s = stable_after_s
        self._grace_ms = grace_ms
        self._sleep = sleep
        self._clock = clock
        self._handles: dict[tuple[str, str, Role], LspHandle] = {}
        self._bg: set[asyncio.Task[Any]] = set()
        self._closed = False

    # --- 定義の引き -------------------------------------------------------------------

    def _defs(self, language_id: str, role: Role | None = None) -> tuple[LspServerDef, ...]:
        if self._servers is None:
            return servers_for(language_id, role)
        return tuple(
            d for d in self._servers if d.language_id == language_id and (role is None or d.role == role)
        )

    def _roles(self, language_id: str) -> list[Role]:
        have = {d.role for d in self._defs(language_id)}
        return [r for r in _ROLES if r in have]

    def _handle(self, language_id: str, ws: Workspace, role: Role) -> LspHandle:
        key = (language_id, ws.workspace_id, role)
        h = self._handles.get(key)
        if h is None:
            h = self._handles[key] = LspHandle(self, language_id, ws, role)
        return h

    def _spawn_bg(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # --- 読み取り -------------------------------------------------------------------

    def handles(self, ws: Workspace | None = None, language_id: str | None = None) -> list[LspHandle]:
        """作られているハンドル（``ws`` / ``language_id`` で絞れる）。"""
        return [
            h
            for h in self._handles.values()
            if (ws is None or h.ws.workspace_id == ws.workspace_id)
            and (language_id is None or h.language_id == language_id)
        ]

    def status(self, language_id: str, ws: Workspace, role: Role = "types") -> PoolStatus:
        """``(status, progress, server_name, pid, reason)``。ブロックしない。サーバー定義の無い言語は ``n/a``。"""
        h = self._handles.get((language_id, ws.workspace_id, role))
        if h is not None:
            return h.snapshot()
        defs = self._defs(language_id, role)
        if not defs:
            return PoolStatus(LspStatus.NOT_APPLICABLE, None, None, None, None)
        return PoolStatus(LspStatus.STANDBY, None, defs[0].name, None, None)

    def get(
        self, language_id: str, ws: Workspace, *, role: Role = "types", start_if_needed: bool = True
    ) -> LspHandle | None:
        """``ready`` のハンドルを返す。そうでなければ ``None``（ブロックしない）。standby なら起動を投げる。"""
        if not self._defs(language_id, role):
            return None
        h = self._handle(language_id, ws, role)
        h.touch()
        if start_if_needed and h.state is LspState.STANDBY:
            self._begin(h)
        return h if h.state is LspState.READY else None

    # --- 起動 -----------------------------------------------------------------------

    async def ensure_started(self, language_id: str, ws: Workspace) -> None:
        """standby の役があれば起動を投げて **待たずに** 戻る（設計書 §8.3）。起動済み・起動中・unavailable は何もしない。"""
        for role in self._roles(language_id):
            h = self._handle(language_id, ws, role)
            h.touch()
            if h.state is LspState.STANDBY:
                self._begin(h)

    async def wait_ready(
        self, language_id: str, ws: Workspace, timeout_ms: int | float, *, role: Role = "types"
    ) -> bool:
        """``ready`` になるまで待つ（standby なら起動する）。``ready`` で ``True``、``unavailable``・時間切れ・定義なしで ``False``。"""
        if not self._defs(language_id, role):
            return False
        h = self._handle(language_id, ws, role)
        h.touch()
        if h.state is LspState.STANDBY:
            self._begin(h)
        await h._wait_state(
            lambda: h.state in (LspState.READY, LspState.UNAVAILABLE), max(timeout_ms, 0) / 1000
        )
        return h.state is LspState.READY

    def _begin(self, h: LspHandle) -> None:
        """standby → starting（同期。ここから先の ``await`` が無いので二重起動しない）。"""
        if self._closed or h.state is not LspState.STANDBY:
            return
        h._gen += 1
        h._stopping = False
        h.timeouts = 0
        if h.server_name is None:
            defs = self._defs(h.language_id, h.role)
            h.server_name = defs[0].name if defs else None
        h._set_state(LspState.STARTING)
        h._task = asyncio.create_task(self._supervise(h), name=f"lsp-pool:{h.language_id}:{h.role}")

    # --- 監督タスク -----------------------------------------------------------------

    async def _supervise(self, h: LspHandle) -> None:
        try:
            while not h._stopping:
                try:
                    failure = await self._run_once(h)
                except _Unavailable as e:
                    h.last_error = str(e)
                    _log.warning("LSP %s/%s unavailable: %s", h.language_id, h.server_name, e)
                    h._set_state(LspState.UNAVAILABLE, e.reason)
                    return
                if h._stopping or failure is None:
                    return
                if h._up_since is not None and self._clock() - h._up_since >= self._stable_after_s:
                    h.restarts = 0  # 長く動いていた: 起動直後の連続クラッシュとは別扱い
                if h.restarts >= self._restart_max:
                    reason = "init_failed" if failure == "init_failed" else "crashed_3x"
                    _log.error(
                        "LSP %s/%s: giving up after %d restarts (%s)",
                        h.language_id,
                        h.server_name,
                        h.restarts,
                        failure,
                    )
                    h._set_state(LspState.UNAVAILABLE, reason)
                    return
                delay = self._backoff_s[min(h.restarts, len(self._backoff_s) - 1)]
                h.restarts += 1
                _log.warning(
                    "LSP %s/%s %s; restart %d/%d in %ss",
                    h.language_id,
                    h.server_name,
                    failure,
                    h.restarts,
                    self._restart_max,
                    delay,
                )
                h.progress = None
                h._set_state(LspState.STARTING, "restarting")
                await self._sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("LSP supervisor for %s crashed", h.language_id)
            h._set_state(LspState.UNAVAILABLE, "init_failed")

    def _pick(self, h: LspHandle) -> tuple[LspServerDef, list[str], Path | None]:
        """起動する定義・argv・tsserver の場所。優先順に探し、``tsserver.js`` を要るのに無い定義（tsls）は飛ばす（S-5）。"""
        defs = self._defs(h.language_id, h.role)
        tsserver: Path | None = None
        tsserver_known = False
        for defn in defs:
            if defn.tsserver_via is not None or defn.needs_workspace_tsserver:
                if not tsserver_known:
                    tsserver, tsserver_known = find_tsserver(h.ws.root), True
                if defn.needs_workspace_tsserver and tsserver is None:
                    continue
            argv = self._resolver(defn, h.ws)
            if argv:
                return defn, argv, tsserver
        raise _Unavailable("not_found", ", ".join(d.name for d in defs) or h.language_id)

    async def _run_once(self, h: LspHandle) -> str | None:
        """1 回起動して、切断されるまで見張る。戻り値は失敗の種類（``init_failed`` / ``crashed``）。止めている最中は ``None``。"""
        defn, argv, tsserver = self._pick(h)
        h.defn = defn
        h.server_name = defn.name
        h.diagnostics.source = defn.name
        h._tokens.clear()
        h._up_since = None
        h.documents.reset()
        h.diagnostics.reset()

        client = LspClient(defn.name)
        python = h.ws.python_env.interpreter if h.ws.python_env is not None else None
        client.set_settings(build_settings(defn, python_path=python, tsserver_path=tsserver))
        h.diagnostics.attach(client)
        client.on_notification("$/progress", h._on_progress)
        client.on_notification(PUBLISH_DIAGNOSTICS, h._on_first_publish)
        client.on_request("window/workDoneProgress/create", h._on_progress_create)
        h.client = client
        try:
            try:
                async with h._lock:
                    await client.start(argv, cwd=h.ws.root, registry=self._registry)
            except (
                OSError,
                RuntimeError,
            ) as e:  # 実行ファイルが開けない・台帳が閉じている: 再試行しても直らない
                raise _Unavailable("init_failed", f"{argv[0]}: {e!r}") from e
            try:
                await client.initialize(
                    root_uri=h.ws.root.as_uri(),
                    root_path=h.ws.root,
                    initialization_options=build_initialization_options(defn, tsserver_path=tsserver),
                    locale=defn.locale,
                    timeout_ms=self._init_timeout_ms,
                )
            except (ConnectionError, TimeoutError, LspError) as e:
                h.last_error = f"initialize failed: {e!r}; stderr: {' | '.join(client.stderr_tail[-5:])}"
                return "init_failed"
            h._up_since = self._clock()
            h._indexing_since = h._up_since
            h.progress = None
            h._set_state(LspState.INDEXING)
            h._arm_fallback()
            await client.wait_closed()
            if h._stopping:
                return None
            h.last_error = f"{client.closed_reason}; exit code {client.returncode}; stderr: {' | '.join(client.stderr_tail[-5:])}"
            return "crashed"
        finally:
            h._cancel_fallback()
            h._gen += 1
            await client.shutdown(self._grace_ms)  # 回収（何度呼んでもよい）
            if h.client is client:
                h.client = None
            h.documents.reset()
            h.diagnostics.reset()

    # --- 停止 -----------------------------------------------------------------------

    async def _stop(
        self, h: LspHandle, final: LspState = LspState.STANDBY, reason: str | None = None
    ) -> None:
        """監督タスクを止め、子プロセスを回収して ``final`` にする。"""
        h._stopping = True
        h._cancel_fallback()
        task = h._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client = h.client
        if client is not None:
            await client.shutdown(self._grace_ms)
            h.client = None
        h._task = None
        h.documents.reset()
        h.diagnostics.reset()
        h._tokens.clear()
        h.progress = None
        h._up_since = None
        h._set_state(final, reason)
        h._stopping = False

    async def reset(self, language_id: str | None, ws: Workspace) -> None:
        """``reindex_scope: "lsp"``: 該当のハンドルを止めて ``standby`` に戻す（``unavailable`` も解く。次の要求で再起動）。"""
        for h in self.handles(ws, language_id):
            if h.state is not LspState.STANDBY or h._task is not None:
                await self._stop(h)
            h.restarts = 0
            h.timeouts = 0
            h.reason = None

    async def shutdown_all(self, grace_ms: int | None = None) -> None:
        """全 LSP を落として回収する（終了手順 §6.5）。以後の起動は行わない。戻った時点で子プロセスは残らない。"""
        self._closed = True
        if grace_ms is not None:
            self._grace_ms = grace_ms
        handles = list(self._handles.values())
        results = await asyncio.gather(*(self._stop(h) for h in handles), return_exceptions=True)
        for h, result in zip(handles, results, strict=True):
            if isinstance(result, BaseException):
                _log.error("failed to stop LSP %s/%s: %r", h.language_id, h.server_name, result)
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    async def sweep_idle(
        self, now: float | None = None, idle_s: float = LSP_IDLE_SHUTDOWN_S
    ) -> list[LspHandle]:
        """``idle_s`` 秒要求の無かった稼働中の LSP を止めて ``standby`` に戻す（MAY。定期タスクから呼ぶ）。止めたハンドルを返す。"""
        t = self._clock() if now is None else now
        idle = [
            h
            for h in self._handles.values()
            if h.state in (LspState.INDEXING, LspState.READY) and t - h.last_used >= idle_s
        ]
        for h in idle:
            await self._stop(h)
        return idle

    # --- ファイル監視 ---------------------------------------------------------------

    async def notify_watched(self, ws: Workspace, changes: Iterable[tuple[RelPath, int]]) -> float:
        """``workspace/didChangeWatchedFiles`` を、``ws`` の稼働中（``indexing`` / ``ready``）の全 LSP へ送る。

        ディスクだけを書き換えても basedpyright の依存元の診断は更新されない（S-04）ので、外部変更では必須。
        送信前の時刻（``time.perf_counter()``）を返す: ``DiagnosticsHub.wait_for(sent_at=...)`` にそのまま渡せる。
        """
        sent_at = time.perf_counter()
        items = [{"uri": to_file_uri(ws, rel), "type": int(kind)} for rel, kind in changes]
        if not items:
            return sent_at
        for h in self.handles(ws):
            client = h.client
            if h.state not in (LspState.INDEXING, LspState.READY) or client is None:
                continue
            try:
                client.notify("workspace/didChangeWatchedFiles", {"changes": items})
            except ConnectionError:
                _log.debug("didChangeWatchedFiles skipped: %s is disconnected", h.server_name)
        return sent_at
