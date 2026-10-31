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
  ただし ``LspServerDef.ready_signal == "initialized"`` のサーバー（ruff。``$/progress`` を出さず、診断も文書を開くまで出さない）は、
  上の合図を待たず **``initialize`` の応答で ``ready``**（``indexing`` を通って直ちに ``ready``。BUG-0050）。
* クラッシュ（切断）・``initialize`` の失敗は ``LSP_RESTART_BACKOFF_S`` = 1/2/4 秒のバックオフで最大 ``LSP_RESTART_MAX`` 回
  再起動し、超えたら ``unavailable``（``crashed_3x`` / ``init_failed``）。起動後 ``stable_after_s`` 以上動いていたクラッシュは
  数え直す。コマンドが見つからなければ即 ``unavailable``（``not_found``）。
* 呼び手（機能呼び出し）が数える要求のタイムアウトは ``LspHandle.record_timeout`` / ``record_ok``。連続 3 回で
  プロセスを落として ``unavailable``（``timeout_3x``）。
* プロセスが生きたまま、ある method に以後答えられない壊れ方をした（``LspServerDef.restart_on_errors`` のエラーが返った）と
  機能呼び出しが知らせたら ``LspHandle.record_broken``: その場で ``starting``（理由 ``restarting``）にして ``ready`` と偽らず、
  落として起動し直す。``stable_after_s`` 内に ``LSP_RESTART_MAX`` 回続いたら ``unavailable``（``broken_3x``）。BUG-0090。
* 終了は ``LspClient.shutdown``（``shutdown`` → ``exit`` → ``ProcessRegistry.terminate``。Windows は Job Object で孫ごと）。
* ``LspHandle.started_at``（ISO 8601・UTC・ミリ秒）は直近に起動を始めた時刻（T-00 ``lsp_daemons[].started_at``）。再起動のたびに
  進み、止めると ``None``。``LspHandle.rebind(ws)`` / ``LspPool.rebind(ws)`` はワークスペースの設定変更を取り込む
  （Python のインタープリタが変わったら Python 系は再起動する。仕様書 T-00 事後条件 3）。
* ``LspPool.notify_watched`` は外部変更を ``workspace/didChangeWatchedFiles`` で届ける。根の ``pyrightconfig.json`` /
  ``pyproject.toml`` の変更なら、先に basedpyright / pyright の解析の対象（``LspHandle.analysis_scope``）と設定ファイルの明示
  （``LspHandle.pinned_config``）を読み直し、明示が変わったら設定を差し替えて ``workspace/didChangeConfiguration`` を送る（BUG-0135）。
* ``LspPool.router(ws)`` は言語ごとに正しいハンドルへ文書を振り分ける ``DocumentRouter``（1 言語に複数のサーバー〔basedpyright と ruff〕が
  あるとき、全サーバーに同期し、診断の待ちを合わせる。編集トランザクションには 1 組の口として渡す。BUG-0023）。
* ``LspHandle.documents`` / ``.diagnostics`` はハンドルと同じ寿命で、再起動のたびに中身だけ空になる（呼び手が参照を持ち続けてよい）。
* index への診断の書き込みは呼び手の ``on_diagnostics(path, diags, version, source, ws)`` に任せる（``source`` はサーバー名、
  ``ws`` はそのハンドルのワークスペース。複数ルートでも診断を送り元の DB へ書ける）。
  書き込みを非同期にするときは、その完了を表す awaitable か ``IndexWriter.submit`` の ``Future`` を **返す**こと
  （``DiagnosticsHub.wait_for`` はそれが済むまで返らない。捨てると EP-7 の順序が崩れる。BUG-0007）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
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
from lucianke.lsp.documents import DocumentRouter, DocumentSync
from lucianke.lsp.pyright_scope import PyrightScope, pyright_scope
from lucianke.lsp.servers import (
    LspServerDef,
    Role,
    build_initialization_options,
    build_settings,
    find_command,
    find_tsserver,
    host_venv,
    is_pyright_root_config,
    pinned_pyright_config,
    servers_for,
)
from lucianke.workspace.paths import to_file_uri
from lucianke.workspace.pyenv import touches_lsp_command_candidates
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.lsp.pool")

TIMEOUTS_TO_UNAVAILABLE = 3
"""連続したタイムアウトがこの回数に達したら ``unavailable``（設計書 §7.5.2）。"""

_ROLES: tuple[Role, ...] = ("types", "lint")

OnDiagnostics = Callable[[RelPath, list[dict[str, Any]], int | None, str, Workspace], Any]
"""``on_diagnostics(path, diags, version, source, ws)``: ``source`` は LSP サーバー名（``basedpyright`` / ``ruff`` …）、
``ws`` はそのハンドルのワークスペース（``LspHandle.rebind`` の後は新しい方）。

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
    """既定の探索（``find_command``）: 環境変数 → ワークスペースの venv → ``node_modules/.bin`` → Lucianke 自身の Python 環境 → ``PATH``。

    Lucianke 自身の環境（``host_venv``）を PATH より先に見るのは、``.venv`` の無いワークスペースでも Lucianke と一緒に入った
    basedpyright・ruff を使うため（起動した venv が有効化されていなければ PATH に無く、PATH 上の別の pyright に落ちる。BUG-0037）。"""
    return find_command(defn, workspace_root=ws.root, venv=venv_root_of(ws), host_venv=host_venv())


def _is_python_types(defn: LspServerDef | None) -> bool:
    """basedpyright / pyright（Python の型解決のサーバー）か。根の設定の明示と解析の対象を持つのはこれだけ。"""
    return defn is not None and defn.language_id == "python" and defn.role == "types"


def _client_settings(
    ws: Workspace, defn: LspServerDef, tsserver: Path | None, pinned: Path | None
) -> dict[str, Any]:
    """``workspace/configuration`` に答える設定木（``build_settings``）。インタープリタ・import の根は ``ws.python_env`` から。"""
    env = ws.python_env
    python = env.interpreter if env is not None else None
    extra = [ws.root / p for p in env.extra_paths] if env is not None else []
    return build_settings(defn, python_path=python, extra_paths=extra, tsserver_path=tsserver, config_file=pinned)


def _read_pyright_config(root: Path) -> tuple[Path | None, PyrightScope | None]:
    """根の設定の明示の判断と解析の対象を読み直す（ブロックする。スレッドで呼ぶ）。"""
    return pinned_pyright_config(root), pyright_scope(root, refresh=True)


def _brief(text: str | None, limit: int = 300) -> str:
    """ログ用に ``last_error``（切断の理由・stderr の末尾）を 1 行 ``limit`` 文字までに縮める。"""
    one = " ".join((text or "-").split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


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
        # basedpyright / pyright が読んでいる解析の対象（include / exclude）。起動のたびと、根の設定の変更をサーバーに届けたとき
        # （``LspPool.notify_watched``）に引き直す。サーバーの読んだものに揃える（BUG-0132・BUG-0135）。Python の型解決のサーバーでなければ ``None``
        self.analysis_scope: PyrightScope | None = None
        # サーバーに明示している設定ファイル（``servers.pinned_pyright_config``。``analysis.configFilePath``）。明示していなければ ``None``
        self.pinned_config: Path | None = None
        self.client: LspClient | None = None
        self.restarts = 0
        self.timeouts = 0
        self.last_error: str | None = None
        self.last_used = pool._clock()
        self.started_at: str | None = None
        """直近に起動を始めた時刻（ISO 8601・UTC・ミリ秒）。起動前・``_stop`` の後は ``None``。"""
        self.history: deque[LspState] = deque([LspState.STANDBY], maxlen=64)
        self.documents = DocumentSync(ws, self._notify, max_open=pool._open_docs_max)
        self.diagnostics = DiagnosticsHub(ws, source=language_id, on_diagnostics=self._on_diagnostics)
        self.diagnostics.sent_at_of = self.documents.sent_at_of
        self.documents.on_close = self.diagnostics.forget

        self._pool = pool
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._broken_restart = False
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

    def _mark_started(self) -> None:
        self.started_at = self._pool._wall_clock().isoformat(timespec="milliseconds")

    def snapshot(self) -> PoolStatus:
        name = self.server_name
        if self.state is LspState.STANDBY:
            # 止まっている間の名前は、次に起こす定義（前回の定義が残っていても、環境が変わりうる）
            defs = self._pool._defs(self.language_id, self.role)
            if defs:
                name = self._pool._expected_name(self.language_id, self.ws, self.role, defs)
        return PoolStatus(self.state.public, self.progress, name, self.pid, self.reason)

    def touch(self) -> None:
        """要求で使った（アイドル停止の判定を延ばす）。"""
        self.last_used = self._pool._clock()

    async def rebind(self, ws: Workspace) -> bool:
        """ワークスペースの設定が変わった（``set_python_env``・除外・``default_base_ref`` …）。新しい ``Workspace`` に差し替える。

        ``self.ws`` と、``documents`` / ``diagnostics``（URI の変換に ``ws`` を使う）が同じ ``ws`` を持つ。``on_diagnostics`` の
        ``ws`` 引数も新しい方になる。除外などの変更は LSP の設定に出ない（``build_settings`` が使うのは Python のインタープリタ・
        import の根〔``extra_paths``〕・tsserver だけ）ので、再起動は要らず
        ``workspace/didChangeConfiguration`` も送らない。

        **Python 系（``language_id == "python"``）はインタープリタか import の根が変わったら再起動する**（仕様書 T-00 事後条件 3: ``set_python_env`` の
        後の ``started_at`` が呼び出し時刻より後。basedpyright はインタープリタを ``workspace/configuration`` の応答でしか受け取らず、
        起動時の値で環境を作る〔設計書 §7.5.1〕）。起動中・起動済み（``starting`` / ``indexing`` / ``ready``）なら止めてすぐ起動し直す
        （クラッシュではないので ``restarts`` は増やさない）。``unavailable``（新しい venv にサーバーが入っているかもしれない）は
        ``standby`` に戻す（``reset`` と同じ。次の要求で起動）。``standby`` は差し替えだけ。ルートが変わったときは言語を問わず同じ。

        再起動したら ``True``。``workspace_id`` が違うと ``ValueError``（別のハンドルの仕事）。
        """
        if ws.workspace_id != self.ws.workspace_id:
            raise ValueError(f"workspace_id mismatch: {ws.workspace_id!r} != {self.ws.workspace_id!r}")
        old = self.ws
        self.ws = ws
        self.documents.ws = ws
        self.diagnostics.ws = ws
        old_py = (
            (old.python_env.interpreter, old.python_env.extra_paths) if old.python_env is not None else None
        )
        new_py = (ws.python_env.interpreter, ws.python_env.extra_paths) if ws.python_env is not None else None
        needs_restart = ws.root != old.root or (self.language_id == "python" and old_py != new_py)
        if not needs_restart or self.state is LspState.STANDBY:
            return False
        pool = self._pool
        if self.state is LspState.UNAVAILABLE:
            await pool.reset_handle(self)
            return False
        await pool._stop(self)
        self.restarts = 0
        self.timeouts = 0
        pool._begin(self)  # ``_stop`` の戻りから ``_begin`` まで ``await`` が無い: 他の要求に割り込まれない
        return self.state is LspState.STARTING

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

    def record_broken(self, method: str, detail: str) -> LspState | None:
        """プロセスは生きているが ``method`` に以後答えられない（``LspServerDef.restart_on_errors`` のエラーが返った。BUG-0090）。

        その場で（``await`` なしで）状態を変えて、以後の要求の経路と ``status`` が ``ready`` と言わないようにし、プロセスを落とす
        （``LspPool._restart_broken``）。戻り値はその状態:

        * ``STARTING``（理由 ``restarting``）: 起動し直す（すでに起動し直している最中も）。
        * ``UNAVAILABLE``（理由 ``broken_3x``）: 起動から ``stable_after_s`` 未満で ``LSP_RESTART_MAX`` 回壊れ続けたので、もう起こさない。
        * ``None``: 止めている最中（``timeout_3x`` など）・止まっているので何もしない。
        """
        if self._broken_restart:
            return (
                self.state if self.state in (LspState.STARTING, LspState.UNAVAILABLE) else LspState.STARTING
            )
        if self._stopping or self.state not in (LspState.INDEXING, LspState.READY):
            return None
        pool = self._pool
        if self._up_since is not None and pool._clock() - self._up_since >= pool._stable_after_s:
            self.restarts = 0  # 長く動いていた: 起動直後から壊れ続けるのとは別扱い
        give_up = self.restarts >= pool._restart_max
        self._stopping = True  # 二重に止めない（_stop が最後に戻す）
        self._broken_restart = True
        self.last_error = f"{method} failed: {detail}"
        if give_up:
            _log.error(
                "LSP %s/%s can no longer answer %s after %d restarts; marking it unavailable: %s",
                self.language_id,
                self.server_name,
                method,
                self.restarts,
                _brief(detail),
            )
            self._set_state(LspState.UNAVAILABLE, "broken_3x")
        else:
            _log.warning(
                "LSP %s/%s can no longer answer %s (%s); restarting it",
                self.language_id,
                self.server_name,
                method,
                _brief(detail),
            )
            self._set_state(LspState.STARTING, "restarting")
        pool._spawn_bg(pool._restart_broken(self, give_up=give_up))
        return self.state

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
        return cb(path, diags, version, self.server_name or self.language_id, self.ws)

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


COMMAND_NAME_TTL_S = 30.0
"""``LspPool.status`` が standby のときの「次に起こすサーバーの名前」を覚えておく長さ（秒。BUG-0131）。覚えは監視のイベント・
``reset`` / ``rebind``・環境変数と Python 環境の変更で捨てる。これは、イベントを取りこぼしたとき（監視が動いていない・
イベントが落ちた）の保険で、普段は効かない。"""


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
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
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
        self._wall_clock = wall_clock
        self._handles: dict[tuple[str, str, Role], LspHandle] = {}
        self._command_gen = 0
        """起動コマンドの探索の結果が変わりうる出来事（``invalidate_commands``）の数。``_name_memo`` の覚えは、書いたときの値と
        同じときだけ使う（監視のスレッドが数を進める。取りこぼしなく捨てるため、覚えを消さずに世代で見る）。"""
        self._name_memo: dict[tuple[Any, ...], tuple[str, int, float]] = {}
        self._routers: dict[str, DocumentRouter] = {}
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
        return PoolStatus(
            LspStatus.STANDBY, None, self._expected_name(language_id, ws, role, defs), None, None
        )

    def state_of(self, language_id: str, ws: Workspace, role: Role = "types") -> LspStatus:
        """``status(...).status`` だけ。起動コマンドを探さない（ファイルを stat しない）。

        ``status`` は止まっている（standby）ときの名前のために優先順で起動コマンドを探し、呼び出しごとに venv などを
        stat する（BUG-0131: 約 1 ms）。編集のたびに要る「LSP が使えるか」の判定は状態だけで足りる。
        """
        h = self._handles.get((language_id, ws.workspace_id, role))
        if h is not None:
            return h.state.public
        if not self._defs(language_id, role):
            return LspStatus.NOT_APPLICABLE
        return LspStatus.STANDBY

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
            h.server_name = defs[0].name if defs else None  # 直後の ``_run_once`` が実際の名前に直す
        h._set_state(LspState.STARTING)
        h._mark_started()
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
                        "LSP %s/%s: giving up after %d restarts (%s): %s",
                        h.language_id,
                        h.server_name,
                        h.restarts,
                        failure,
                        _brief(h.last_error),
                    )
                    h._set_state(LspState.UNAVAILABLE, reason)
                    return
                delay = self._backoff_s[min(h.restarts, len(self._backoff_s) - 1)]
                h.restarts += 1
                _log.warning(
                    "LSP %s/%s %s; restart %d/%d in %ss: %s",
                    h.language_id,
                    h.server_name,
                    failure,
                    h.restarts,
                    self._restart_max,
                    delay,
                    _brief(h.last_error),
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
        return self._pick_for(h.language_id, h.ws, h.role)

    def _pick_for(
        self, language_id: str, ws: Workspace, role: Role
    ) -> tuple[LspServerDef, list[str], Path | None]:
        defs = self._defs(language_id, role)
        tsserver: Path | None = None
        tsserver_known = False
        for defn in defs:
            if defn.tsserver_via is not None or defn.needs_workspace_tsserver:
                if not tsserver_known:
                    tsserver, tsserver_known = find_tsserver(ws.root), True
                if defn.needs_workspace_tsserver and tsserver is None:
                    continue
            argv = self._resolver(defn, ws)
            if argv:
                return defn, argv, tsserver
        names = ", ".join(f"{d.name} ({d.argv_candidates[0][0]})" for d in defs) or language_id
        raise _Unavailable(
            "not_found",
            f"no LSP server found for {language_id}/{role}: {names}; searched LUCIANKE_LSP_*_CMD, the workspace and "
            "Lucianke Python environments, node_modules/.bin and PATH",
        )

    def invalidate_commands(self) -> None:
        """起動コマンドの探索の覚え（``_expected_name``）を捨てる。仮想環境・``node_modules`` の作り直し、設定の変更のとき。

        どのスレッドから呼んでもよい（世代を 1 つ進めるだけ。覚えは次の ``status`` が引き直す）。"""
        self._command_gen += 1

    def _command_memo_key(
        self, language_id: str, ws: Workspace, role: Role, defs: tuple[LspServerDef, ...]
    ) -> tuple[Any, ...]:
        """``_expected_name`` の覚えの鍵: ワークスペース・言語・役と、探索の入力（環境変数による上書き・``PATH``・Python 環境・根）。

        環境変数・Python 環境は鍵に入れておく（変わったら別の鍵になる）ので、変更の通知は要らない。引くのは辞書だけ（stat しない）。"""
        env = ws.python_env
        overrides = tuple(os.environ.get(v) for d in defs for v in (d.server_env_var, d.env_var))
        return (
            language_id,
            ws.workspace_id,
            role,
            str(ws.root),
            env.interpreter if env is not None else None,
            overrides,
            os.environ.get("PATH"),
        )

    def _expected_name(
        self, language_id: str, ws: Workspace, role: Role, defs: tuple[LspServerDef, ...]
    ) -> str:
        """起こすサーバーの名前: 優先順で最初に見つかる定義。どれも見つからなければ既定（先頭）の名前。

        ブロックしない。まだ起こしていない（standby）ときの ``status`` が、起こした後に出る名前と
        食い違わないようにする（既定が見つからず代替に落ちるのに、起こす前は既定の名前を返していた。BUG-0037）。

        探すには venv などを stat する（約 0.3〜2 ms）ので、（ワークスペース・言語・役・探索の入力）ごとに結果を覚え、
        呼び出しのたびには引かない（BUG-0131）。捨てるのは ``invalidate_commands``（監視の ``.venv`` / ``venv`` /
        ``node_modules`` のイベント・``reset`` / ``rebind``）と ``COMMAND_NAME_TTL_S`` の経過。"""
        key = self._command_memo_key(language_id, ws, role, defs)
        now = self._clock()
        hit = self._name_memo.get(key)
        if hit is not None and hit[1] == self._command_gen and now - hit[2] < COMMAND_NAME_TTL_S:
            return hit[0]
        gen = self._command_gen  # 引いている間に起きた出来事を取りこぼさないよう、引く前の世代で書く
        try:
            name = self._pick_for(language_id, ws, role)[0].name
        except _Unavailable:
            name = defs[0].name
        self._name_memo[key] = (name, gen, now)
        return name

    async def _run_once(self, h: LspHandle) -> str | None:
        """1 回起動して、切断されるまで見張る。戻り値は失敗の種類（``init_failed`` / ``crashed``）。止めている最中は ``None``。"""
        defn, argv, tsserver = self._pick(h)
        h._mark_started()  # クラッシュ後の再起動（バックオフの後）では、ここで実際の起動の時刻に進む
        h.defn = defn
        h.server_name = defn.name
        h.diagnostics.source = defn.name
        h.diagnostics.provisional_empty = defn.provisional_empty
        h._tokens.clear()
        h._up_since = None
        h.documents.reset()
        h.diagnostics.reset()

        client = LspClient(defn.name, no_cancel_methods=defn.no_cancel_methods)
        # 根の pyproject.toml を設定ファイルとして明示し、親のディレクトリの pyright の設定を採らせない（BUG-0132）
        python_types = _is_python_types(defn)
        h.pinned_config = pinned_pyright_config(h.ws.root) if python_types else None
        h.analysis_scope = pyright_scope(h.ws.root) if python_types else None
        client.set_settings(_client_settings(h.ws, defn, tsserver, h.pinned_config))
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
            if defn.ready_signal == "initialized":
                h._become_ready()  # 索引を持たないサーバー（ruff）: initialize の応答がそのまま ready の合図（BUG-0050）
            else:
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
            # 別の ``_stop`` がすでに取り消した監督タスクは取り消し直さない（終わるのを待つだけ）。取り消し直すと、監督タスクの
            # ``finally`` が待っている ``client.shutdown`` の途中に CancelledError が入り、``ProcessRegistry.terminate`` に届かず
            # 子プロセスが残る（``record_broken`` / ``record_timeout`` の停止と ``shutdown_all`` が重なったとき。BUG-0090 の試験で実測）。
            if not task.done() and not task.cancelling():
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
        h.started_at = None
        h._set_state(final, reason)
        h._stopping = False

    async def _restart_broken(self, h: LspHandle, *, give_up: bool) -> None:
        """``LspHandle.record_broken`` の続き: 落として起動し直す（BUG-0090）。クラッシュの再起動と同じ ``restarts`` を数える。

        ``give_up``（``record_broken`` が決めた。短い間に ``restart_max`` 回壊れ続けた）なら起動し直さず ``unavailable``（``broken_3x``）。
        起動し直している間の ``reason`` は ``restarting``（``status`` に出る）。
        """
        try:
            if give_up:
                await self._stop(h, LspState.UNAVAILABLE, "broken_3x")
                return
            await self._stop(h)
            h.restarts += 1
            h.timeouts = 0
            self._begin(h)  # ``_stop`` の戻りから ``_begin`` まで ``await`` が無い: 他の要求に割り込まれない
            if h.state is LspState.STARTING:
                h.reason = "restarting"
        finally:
            h._broken_restart = False

    async def reset(self, language_id: str | None, ws: Workspace) -> None:
        """``reindex_scope: "lsp"``: 該当のハンドルを止めて ``standby`` に戻す（``unavailable`` も解く。次の要求で再起動）。"""
        self.invalidate_commands()  # venv にサーバーを入れ直した後の ``reset`` で、古い名前を言い続けない
        for h in self.handles(ws, language_id):
            await self.reset_handle(h)

    async def reset_handle(self, h: LspHandle) -> None:
        """1 ハンドルの ``reset``（止めて ``standby`` に戻し、失敗の計数を解く）。"""
        if h.state is not LspState.STANDBY or h._task is not None:
            await self._stop(h)
        h.restarts = 0
        h.timeouts = 0
        h.reason = None

    async def rebind(self, ws: Workspace) -> list[LspHandle]:
        """``ws``（``workspace_id`` が同じ既存のハンドルが対象）の設定変更を取り込む（``LspHandle.rebind``）。再起動したハンドルを返す。

        T-00 の ``set_python_env`` / ``set_roots``（設定の更新）が呼ぶ。ハンドルが 1 つも無い（まだ一度も使っていない）ときは何もしない:
        ハンドルは作られる時点の ``Workspace`` を持つので、呼び手は更新後の ``Workspace`` を渡し続ければよい。
        """
        restarted: list[LspHandle] = []
        self.invalidate_commands()  # 根・Python 環境が変わった（探索の入力が変わる）
        for h in self.handles(ws):
            if await h.rebind(ws):
                restarted.append(h)
        return restarted

    def router(self, ws: Workspace) -> DocumentRouter:
        """``ws`` の ``DocumentRouter``（同じ ``workspace_id`` なら同じものを返す）。編集トランザクションに ``documents=`` と
        ``diagnostics_hub=router.diagnostics_hub`` を渡す。"""
        r = self._routers.get(ws.workspace_id)
        if r is None:
            r = self._routers[ws.workspace_id] = DocumentRouter(self, ws)
        r.ws = ws
        return r

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

        根の ``pyrightconfig.json`` / ``pyproject.toml``（``servers.is_pyright_root_config``）が変わったら、先に basedpyright / pyright の
        解析の対象（``LspHandle.analysis_scope``）と設定ファイルの明示の判断（``LspHandle.pinned_config``）を読み直す（BUG-0135。
        ``_reload_pyright_config``）。basedpyright 1.40.2 は ``didChangeWatchedFiles`` で根の設定を読み直すが、明示の判断が変わったとき
        （根に ``pyrightconfig.json`` を作った・消した）はそれだけでは足りないので、設定を差し替えて ``didChangeConfiguration`` を送る。
        そのハンドルには、設定ファイルの変更を ``didChangeWatchedFiles`` に入れない（古い明示のまま一度読み直させない）。
        """
        sent_at = time.perf_counter()
        changes = [(rel, int(kind)) for rel, kind in changes]
        if not changes:
            return sent_at
        if any(touches_lsp_command_candidates(str(rel), fold=ws.case_insensitive) for rel, _ in changes):
            self.invalidate_commands()  # 監視が除外する場所のイベントはここに来ない（``Watcher`` が直接呼ぶ）。来たものは念のため
        configs = {rel for rel, _ in changes if is_pyright_root_config(str(rel), case_insensitive=ws.case_insensitive)}
        repinned = await self._reload_pyright_config(ws) if configs else set()
        items = [{"uri": to_file_uri(ws, rel), "type": kind} for rel, kind in changes]
        others = [item for (rel, _), item in zip(changes, items, strict=True) if rel not in configs]
        for h in self.handles(ws):
            client = h.client
            if h.state not in (LspState.INDEXING, LspState.READY) or client is None:
                continue
            sending = others if h in repinned else items
            if not sending:
                continue
            try:
                client.notify("workspace/didChangeWatchedFiles", {"changes": sending})
            except ConnectionError:
                _log.debug("didChangeWatchedFiles skipped: %s is disconnected", h.server_name)
        return sent_at

    async def _reload_pyright_config(self, ws: Workspace) -> set[LspHandle]:
        """``ws`` の basedpyright / pyright の解析の対象と明示の判断を、根の設定の今の内容で読み直す（BUG-0135）。

        起動中（``starting``）のハンドルは覚えを直すだけ（``initialize`` の前に通知を送らない。サーバーは起動の後に設定を引く）。
        ``indexing`` / ``ready`` で明示の判断が変わったら ``workspace/configuration`` の設定を差し替えて
        ``workspace/didChangeConfiguration`` を送る（サーバーは設定を引き直し、根の設定を読み直す）。送ったハンドルを返す。
        読んでいる間に起動し直したハンドルは触らない（起動が読み直している）。
        """
        targets = [
            (h, h.client, h._gen)
            for h in self.handles(ws)
            if _is_python_types(h.defn)
            and h.client is not None
            and h.state in (LspState.STARTING, LspState.INDEXING, LspState.READY)
        ]
        repinned: set[LspHandle] = set()
        read: dict[Path, tuple[Path | None, PyrightScope | None]] = {}
        for h, client, gen in targets:
            root = h.ws.root
            if root not in read:
                read[root] = await asyncio.to_thread(_read_pyright_config, root)
            pinned, scope = read[root]
            if h.client is not client or h._gen != gen or client is None or h.defn is None:
                continue
            h.analysis_scope = scope
            if pinned == h.pinned_config:
                continue
            _log.info(
                "LSP %s: the root pyright config changed; configFilePath %s -> %s",
                h.server_name, h.pinned_config, pinned,
                extra={"event": "lsp_config_repinned"},
            )  # fmt: skip
            h.pinned_config = pinned
            client.set_settings(_client_settings(h.ws, h.defn, None, pinned))
            if h.state not in (LspState.INDEXING, LspState.READY):
                continue
            try:
                client.notify("workspace/didChangeConfiguration", {"settings": None})
            except ConnectionError:
                _log.debug("didChangeConfiguration skipped: %s is disconnected", h.server_name)
                continue
            repinned.add(h)
        return repinned
