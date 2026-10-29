"""ワークスペースごとの実行時の束（索引・監視・書き手）と、ツールへ渡す結線済みの ``WiredServices``（設計書 §2.2、§6.5、ADR-6/7/16）。

* ``WorkspaceRuntime``: 1 ワークスペース分の ``IndexWriter``（起動時にスキーマを移行する）・``ReaderPool``・``Indexer``・``Watcher``。
  ``paad.sqlite`` は ``<cache_root>/<workspace_hash>/`` にワークスペースごとに 1 つ（``index.connection.db_path_for``）。
* ``RuntimeHub``: ルートの登録（``WorkspaceContext``）と ``WorkspaceRuntime`` の表、LSP プール、編集ロック。``sync_roots`` は冪等で、
  ルートが増えれば束を起こし、外れれば畳む。起動の順は「書き手を開く → 監視を始める → 差分索引を背景ジョブで走らせる」。
  終了は ``Lifecycle`` の段ごとに ``stop_watchers`` → ``stop_indexers`` → ``close_storage`` を呼ぶ（合成ルートが登録する）。
* ``WiredServices``: ``ToolServices`` に、ツールが duck typing で求める ``db_path`` / ``reader_pool`` / ``workspace()`` を足す。
  ``db_path`` と ``reader_pool`` は **アクティブなワークスペース** のもの（複数ルートでも、ツールの契約は 1 つの DB を見る）。

``tools`` は ``mcp`` を import できないので、``data_version`` の供給などは ``hub`` のメソッドとして出し、合成ルートが ``Pipeline`` へ渡す。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import LspStatus, RelPath
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.transaction import EditTransaction
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool, db_path_for
from lucianke.index.ingest import DiagnosticFact, replace_lsp_diagnostics
from lucianke.index.writer import IndexWriter
from lucianke.lsp.action_store import CodeActionStore
from lucianke.lsp.pool import FileChangeType, LspHandle, LspPool
from lucianke.syntax.parsers import TreeCache
from lucianke.tools._services import ToolServices
from lucianke.tools.t00_configure_workspace import load_known_roots, restore_settings
from lucianke.workspace.context import WorkspaceContext
from lucianke.workspace.types import Workspace
from lucianke.workspace.watcher import Watcher

_log = logging.getLogger("lucianke.runtime")
WATCHER_START_TIMEOUT_S = 5.0
WATCHER_STOP_TIMEOUT_S = 2.0
STARTUP_WAIT_S = 10.0  # roots の待ち上限（5 s）と cwd への退避（8 s）より長く


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class IndexerSink:
    """``Watcher`` の ``IngestSink``: 監視のイベントを ``Indexer`` に渡す（冪等。同じ内容なら ``index_file`` が何もしない）。

    索引に反映したら、同じ変更（作成・変更・削除）をそのワークスペースの起動中の LSP へ ``LspPool.notify_watched``
    （``workspace/didChangeWatchedFiles``）で送る。basedpyright は作業ツリーで新しく作られたファイルを、これか ``didOpen`` を受けるまで
    自分では見つけず、rename の参照に含めない（T-11 の実機の観察）。バースト（``rescan``）は前後の ``files`` の差から送る。
    """

    def __init__(
        self, ws: Workspace, indexer: Indexer, readers: ReaderPool, lsp_pool: LspPool | None = None
    ) -> None:
        self._ws = ws
        self._indexer = indexer
        self._readers = readers
        self._lsp = lsp_pool

    def rebind(self, ws: Workspace) -> None:
        """設定を変えた ``Workspace`` に差し替える（``known_paths_under`` と LSP への通知の宛先）。"""
        self._ws = ws

    async def changed(self, rel: RelPath) -> None:
        stats = await self._indexer.index_file(rel)
        if stats is not None:
            await self._notify([(rel, FileChangeType.CREATED if stats.file_created else FileChangeType.CHANGED)])

    async def removed(self, rel: RelPath) -> None:
        if await self._indexer.remove(rel):
            await self._notify([(rel, FileChangeType.DELETED)])

    async def rescan(self) -> None:
        _log.info("watcher burst: switching to a differential rescan")
        watching = self._lsp is not None and bool(self._lsp.handles(self._ws))
        before = await asyncio.to_thread(self._snapshot) if watching else None
        await self._indexer.scan()
        if before is not None:  # 起動中の LSP がある: 走査で増えた・変わった・消えたファイルを知らせる
            after = await asyncio.to_thread(self._snapshot)
            await self._notify(_file_changes(before, after))

    def _snapshot(self) -> dict[str, str]:
        with self._readers.reader() as conn:
            rows = conn.execute("SELECT path, sha256 FROM files WHERE workspace_id = ?", (self._ws.workspace_id,)).fetchall()
        return {str(path): str(sha) for path, sha in rows}

    def known_paths_under(self, prefix: str) -> list[RelPath]:
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._readers.reader() as conn:
            rows = conn.execute(
                "SELECT path FROM files WHERE workspace_id = ? AND path LIKE ? ESCAPE '\\'",
                (self._ws.workspace_id, like),
            ).fetchall()
        return [RelPath(r[0]) for r in rows]

    async def _notify(self, changes: Sequence[tuple[RelPath, FileChangeType]]) -> None:
        if self._lsp is None or not changes:
            return
        try:
            await self._lsp.notify_watched(self._ws, changes)
        except Exception:  # noqa: BLE001  LSP への通知の失敗で索引を止めない
            _log.warning("didChangeWatchedFiles failed (%d paths, first %s)", len(changes), changes[0][0], exc_info=True)


def _file_changes(before: dict[str, str], after: dict[str, str]) -> list[tuple[RelPath, FileChangeType]]:
    """``files`` の ``{path: sha256}`` の前後の差。新規は CREATED、``sha256`` が違えば CHANGED、無くなれば DELETED（パス順）。"""
    out: list[tuple[RelPath, FileChangeType]] = []
    for path in sorted(before.keys() | after.keys()):
        if path not in before:
            out.append((RelPath(path), FileChangeType.CREATED))
        elif path not in after:
            out.append((RelPath(path), FileChangeType.DELETED))
        elif before[path] != after[path]:
            out.append((RelPath(path), FileChangeType.CHANGED))
    return out


@dataclass(slots=True)
class WorkspaceRuntime:
    """1 ワークスペースの束。``open`` で書き手を開き（スキーマ移行込み）、``begin`` で監視と差分索引を始める。"""

    ws: Workspace
    db_path: Path
    writer: IndexWriter
    readers: ReaderPool
    indexer: Indexer
    watcher: Watcher
    journal_dir: Path
    sink: IndexerSink | None = None
    scan_job_id: str | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _watch_task: asyncio.Task[None] | None = None

    @classmethod
    async def open(
        cls, ws: Workspace, cache_root: Path, trees: TreeCache, lsp_pool: LspPool | None = None
    ) -> WorkspaceRuntime:
        db_path = db_path_for(cache_root, ws.root)
        writer = await asyncio.to_thread(IndexWriter, db_path)  # スレッド起動とスキーマの作成・移行
        readers = ReaderPool(db_path)
        indexer = Indexer(ws, writer, readers, trees)
        sink = IndexerSink(ws, indexer, readers, lsp_pool)
        watcher = Watcher(ws, sink)
        _log.info(
            "index opened: ws=%s db=%s schema=%s", ws.workspace_id, db_path, writer.schema_action,
            extra={"event": "index_open"},
        )  # fmt: skip
        return cls(ws, db_path, writer, readers, indexer, watcher, db_path.parent / "journal", sink)

    async def begin(self) -> str:
        """監視を始め、始まったのを見てから差分索引を背景ジョブで投入する（始まる前の変更の取りこぼしを防ぐ）。すぐ戻る。"""
        self._watch_task = asyncio.get_running_loop().create_task(
            self.watcher.run(self._stop), name=f"lucianke-watch-{self.ws.workspace_id}"
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.watcher.started.wait(), WATCHER_START_TIMEOUT_S)
        self.scan_job_id = self.indexer.start_scan_job()
        _log.info(
            "watching (%s) and scan job %s started: ws=%s", self.watcher.mode, self.scan_job_id, self.ws.workspace_id,
            extra={"event": "index_begin"},
        )  # fmt: skip
        return self.scan_job_id

    def rebind(self, ws: Workspace) -> None:
        """設定（除外規則・Python 環境・基準リビジョン）を変えた ``Workspace`` を束の全員に渡す（監視・索引器・LSP への通知）。"""
        self.indexer.rebind(ws)
        self.watcher.rebind(ws)
        if self.sink is not None:
            self.sink.rebind(ws)
        self.ws = ws

    async def stop_watcher(self) -> None:
        self._stop.set()
        task, self._watch_task = self._watch_task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), WATCHER_STOP_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except Exception:  # noqa: BLE001
            _log.warning("watcher ended with an error", exc_info=True)

    async def stop_indexer(self) -> None:
        await self.indexer.aclose()

    async def close_storage(self) -> None:
        """キューを空にしてチェックポイントし、DB を閉じる（ブロッキングなのでスレッドで）。"""
        self.readers.close()
        await asyncio.to_thread(self.writer.close)

    async def aclose(self) -> None:
        await self.stop_watcher()
        await self.stop_indexer()
        await self.close_storage()

    def data_version(self) -> int:
        return self.writer.data_version()


def _diagnostic_facts(diags: Sequence[dict[str, Any]], source: str) -> list[DiagnosticFact]:
    facts: list[DiagnosticFact] = []
    for d in diags:
        try:
            rng = d["range"]
            code = d.get("code")
            facts.append(
                DiagnosticFact(
                    severity=int(d.get("severity") or 1),
                    source=source,  # サーバー名で揃える（ruff は診断に "Ruff" と書く。行の置換の範囲〔replace_lsp_diagnostics の source〕と同じでないと落ちる・残る）
                    message=str(d.get("message", "")),
                    start_line=int(rng["start"]["line"]),
                    start_col=int(rng["start"]["character"]),
                    end_line=int(rng["end"]["line"]),
                    end_col=int(rng["end"]["character"]),
                    code=None if code is None else str(code),
                )
            )
        except (KeyError, TypeError, ValueError):
            _log.debug("skipping a malformed diagnostic: %r", d)
    return facts


class RuntimeHub:
    """ルートの登録と ``WorkspaceRuntime`` の表。イベントループ上でだけ使う。"""

    def __init__(
        self,
        cache_root: Path,
        trees: TreeCache,
        *,
        lsp_pool: LspPool | None = None,
        cwd: Path | str | None = None,
        env: Any = None,
    ) -> None:
        self.cache_root = Path(cache_root)
        self.trees = trees
        self.lsp_pool = lsp_pool
        self.locks = WorkspaceLocks()
        self.code_action_store = CodeActionStore()  # T-08 が QuickFix を載せ、T-13 が引く（1 つを共有する）
        self.failed: dict[str, str] = {}  # workspace_id -> 開けなかった理由
        self._cwd = cwd
        self._env = env
        self._context: WorkspaceContext | None = None
        self._runtimes: dict[str, WorkspaceRuntime] = {}
        self._lock = asyncio.Lock()
        self._applied: tuple[str, ...] | None = None
        self._started = asyncio.Event()  # 最初の sync_roots が終わった（束が開いた・開けなかった、のどちらでも）

    async def wait_started(self, timeout: float = STARTUP_WAIT_S) -> bool:
        """最初の ``sync_roots`` が終わるまで待つ（``tools/call`` の入口が呼ぶ。起動直後の呼び出しが束の準備より先に着く競合を埋める）。"""
        if self._started.is_set():
            return True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._started.wait(), timeout)
        return self._started.is_set()

    # --- 参照 ---

    @property
    def context(self) -> WorkspaceContext:
        if self._context is None:
            # 全キャッシュの ``workspaces`` 行から、前回までに割り当てた ID を引き継ぐ（URN を再起動をまたいで安定させる。§7.3.1 手順 2）
            known = load_known_roots(self.cache_root)
            self._context = WorkspaceContext(cwd=self._cwd, env=self._env, known_roots=known)
        return self._context

    @property
    def runtimes(self) -> dict[str, WorkspaceRuntime]:
        return dict(self._runtimes)

    def has_workspaces(self) -> bool:
        return self._context is not None and bool(self._context.list())

    def workspace(self, workspace_id: str | None) -> Workspace:
        ctx = self.context
        return ctx.active() if workspace_id is None else ctx.get(workspace_id)

    def runtime(self, workspace_id: str | None = None) -> WorkspaceRuntime | None:
        """束（無ければ ``None``: 開けなかった・まだ開いていない）。省略はアクティブなワークスペース。"""
        if workspace_id is None:
            if not self.has_workspaces():
                return None
            workspace_id = self.context.active().workspace_id
        return self._runtimes.get(workspace_id)

    def data_version(self) -> int:
        """アクティブなワークスペースの ``data_version``（カーソルの失効判定）。DB が無ければ 0。"""
        rt = self.runtime()
        if rt is None:
            return 0
        try:
            return rt.data_version()
        except Exception:  # noqa: BLE001
            return 0

    # --- ルート ---

    async def sync_roots(self, paths: Sequence[str]) -> list[Workspace]:
        """ルートを登録し、束を起こす／畳む。同じ ``paths`` の再呼び出しは何もしない。"""
        try:
            return await self._sync_roots(paths)
        finally:
            self._started.set()

    async def _sync_roots(self, paths: Sequence[str]) -> list[Workspace]:
        async with self._lock:
            key = tuple(os.path.realpath(p) for p in paths)
            if key == self._applied:
                return self.context.list()
            context = self.context
            before = {ws.workspace_id for ws in context.list()}
            try:
                registered = context.register_roots(list(paths), replace=True)
            except LuciankeError as exc:
                _log.warning("roots not registered: %s", exc.message)
                return context.list()
            self._applied = key
            new = [ws for ws in registered if ws.workspace_id not in before]
            if new:  # 新しく登録した分だけ、保存済みの設定（除外・Python 環境・基準リビジョン）を初期値として戻す（束を起こす前に）
                registered = self._restore(new, registered)
            wanted = {ws.workspace_id for ws in registered}
            for ws_id in [i for i in self._runtimes if i not in wanted]:
                await self._runtimes.pop(ws_id).aclose()
            started: list[WorkspaceRuntime] = []
            for ws in registered:
                if ws.workspace_id in self._runtimes:
                    continue
                try:
                    rt = await WorkspaceRuntime.open(ws, self.cache_root, self.trees, self.lsp_pool)
                except Exception as exc:  # noqa: BLE001  キャッシュに書けない等。索引なしで動き続ける
                    self.failed[ws.workspace_id] = f"{type(exc).__name__}: {exc}"
                    _log.error(
                        "could not open the index for %s", ws.root, exc_info=True, extra={"event": "index_open_failed"}
                    )
                    continue
                self.failed.pop(ws.workspace_id, None)
                self._runtimes[ws.workspace_id] = rt
                started.append(rt)
            for rt in started:
                await rt.begin()
            return registered

    def _restore(self, new: Sequence[Workspace], registered: list[Workspace]) -> list[Workspace]:
        try:
            for message in restore_settings(self.context, new, self.cache_root):
                _log.warning("saved settings: %s", message)
        except Exception:  # noqa: BLE001  保存済みの設定が読めなくても、既定の設定で動き続ける
            _log.warning("could not restore the saved settings", exc_info=True)
        return [self.context.get(ws.workspace_id) for ws in registered]

    async def workspace_changed(self, ws: Workspace) -> list[LspHandle]:
        """設定を変えた ``Workspace``（T-00 の ``set_excludes`` / ``set_python_env`` / ``default_base_ref``）を束と LSP に渡し直す。

        束（``Indexer``・``Watcher``・通知の宛先）は新しい規則に差し替える。LSP のハンドルは ``LspPool.rebind`` があればそれを、
        無ければハンドルごとの ``LspHandle.rebind`` を呼ぶ。**LSP を再起動する責任はここ（``rebind``）にある**: Python 環境が変わって
        再起動した（止めて起動し直した）ハンドルを返す。呼び手（T-00 ``set_python_env``）は、返ったハンドルをもう一度再起動してはならない
        （BUG-0035: 二重に起動し直していた）。
        """
        rt = self._runtimes.get(ws.workspace_id)
        if rt is not None:
            rt.rebind(ws)
        pool = self.lsp_pool
        if pool is None:
            return []
        try:
            pool_rebind = getattr(pool, "rebind", None)
            if pool_rebind is not None:  # ``LspPool.rebind``: ハンドルごとの差し替えと、Python 環境が変われば Python 系の再起動
                return list(await _maybe_await(pool_rebind(ws)) or [])
        except Exception:  # noqa: BLE001  LSP 側の差し替えの失敗で設定の変更を止めない
            _log.warning("could not rebind the LSP pool for %s", ws.workspace_id, exc_info=True)
            return []
        restarted: list[LspHandle] = []
        for handle in pool.handles(ws):
            rebind = getattr(handle, "rebind", None)
            if rebind is None:
                _log.debug("LspHandle.rebind is not available; %s keeps its Workspace", handle.language_id)
                continue
            try:
                if await _maybe_await(rebind(ws)) is True:  # ``LspHandle.rebind`` は再起動したら ``True``
                    restarted.append(handle)
            except Exception:  # noqa: BLE001
                _log.warning("could not rebind the LSP handle %s", handle.language_id, exc_info=True)
        return restarted

    # --- LSP の診断 → PaaD ---

    async def on_diagnostics(
        self,
        path: RelPath,
        diags: list[dict[str, Any]],
        version: int | None,
        source: str,
        ws: Workspace | None = None,
    ) -> None:
        """``LspPool`` の ``on_diagnostics``。診断を送ってきたハンドルのワークスペース（``ws``）の束の PaaD に、``source`` の行を置換する。

        ``data_version`` は進めない。書き込みが済むまで返らない（``DiagnosticsHub.wait_for`` が EP-7 の順序をそれで守る）。
        ``ws`` が無い呼び手（4 引数）はアクティブなワークスペース。束が無い（開けなかった・畳まれた）ときは捨てる。
        """
        rt = self.runtime(None if ws is None else ws.workspace_id)
        if rt is None:
            return
        facts = _diagnostic_facts(diags, source)
        ws_id = rt.ws.workspace_id

        def job(conn: Any) -> int:
            return replace_lsp_diagnostics(conn, ws_id, str(path), facts, source=source)

        await rt.writer.asubmit(job, bumps_version=False, label=f"diag:{source}:{path}")

    # --- 終了の段（Lifecycle へ登録する。現在の束の集合を回るので、後から増えた分も含む） ---

    async def stop_watchers(self) -> None:
        await asyncio.gather(*(rt.stop_watcher() for rt in self._runtimes.values()), return_exceptions=True)

    async def stop_indexers(self) -> None:
        await asyncio.gather(*(rt.stop_indexer() for rt in self._runtimes.values()), return_exceptions=True)

    async def close_storage(self) -> None:
        await asyncio.gather(*(rt.close_storage() for rt in self._runtimes.values()), return_exceptions=True)


@dataclass(slots=True)
class WiredServices(ToolServices):
    """結線済みの入れ物。``hub`` があればワークスペースと DB は ``hub`` から、無ければ（起動直後・テスト）従来どおり ``roots`` の先頭。

    ツールが ``getattr`` で求めるもの: ``db_path``（アクティブな ``paad.sqlite``。束が無ければ ``None`` → ``E_WORKSPACE_NOT_CONFIGURED``）、
    ``reader_pool``、``workspace(workspace_id)``、``lines``。編集ツールは ``runtime()`` の束（``writer`` / ``indexer`` / ``watcher`` /
    ``readers`` / ``journal_dir``）と ``locks``・``lsp_pool``・``trees`` から ``EditTransaction`` を組む。
    """

    hub: RuntimeHub | None = None

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        hub = self.hub
        if hub is not None and hub.has_workspaces():
            return hub.workspace(workspace_id)
        return ToolServices.workspace(self, workspace_id)  # slots の dataclass では引数なしの super() が使えない

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        """省略を実在の ID にする。ルートが 1 つも無い間は ``None`` を素通しする（T-00 ``set_roots`` の最初の呼び出しを通すため）。

        他のツールはこの後、``workspace()`` / ``db_path`` で ``E_WORKSPACE_NOT_CONFIGURED`` になる。明示した ID は従来どおり検査する。
        """
        hub = self.hub
        if workspace_id is None and (hub is None or not hub.has_workspaces()) and not list(self.roots()):
            return None
        return self.workspace(workspace_id).workspace_id

    def runtime(self, workspace_id: str | None = None) -> WorkspaceRuntime | None:
        return None if self.hub is None else self.hub.runtime(workspace_id)

    @property
    def db_path(self) -> Path | None:
        rt = self.runtime()
        return None if rt is None else rt.db_path

    @property
    def reader_pool(self) -> ReaderPool | None:
        rt = self.runtime()
        return None if rt is None else rt.readers

    @property
    def lsp_pool(self) -> LspPool | None:
        return None if self.hub is None else self.hub.lsp_pool

    @property
    def locks(self) -> WorkspaceLocks | None:
        return None if self.hub is None else self.hub.locks

    @property
    def code_action_store(self) -> CodeActionStore | None:
        return None if self.hub is None else self.hub.code_action_store

    # 編集ツールが属性で組む場合の入り口（アクティブなワークスペースの束）。普通は ``edit_transaction`` を使う。
    @property
    def writer_index(self) -> IndexWriter | None:
        rt = self.runtime()
        return None if rt is None else rt.writer

    @property
    def journal_dir(self) -> Path | None:
        rt = self.runtime()
        return None if rt is None else rt.journal_dir

    @property
    def indexer(self) -> Indexer | None:
        rt = self.runtime()
        return None if rt is None else rt.indexer

    @property
    def watcher(self) -> Watcher | None:
        rt = self.runtime()
        return None if rt is None else rt.watcher

    def _router_wiring(self, ws: Workspace) -> dict[str, Any]:
        pool = self.lsp_pool
        make_router = getattr(pool, "router", None)
        if pool is None or make_router is None:
            return {}  # LSP が無い（構文検証だけに降格する）
        router = make_router(ws)

        def lsp_status(language_id: str) -> LspStatus:
            return pool.status(language_id, ws).status

        return {"documents": router, "diagnostics_hub": router.diagnostics_hub, "lsp_status": lsp_status}

    def edit_transaction(self, ws: Workspace, *, lsp: Any = None, **overrides: Any) -> EditTransaction:
        """編集トランザクションを組む（T-10・T-11・T-13 が呼ぶ）。束の ``writer`` / ``indexer`` / ``watcher`` / ``readers`` と共有の ``trees``・``locks``。

        LSP プールが ``router(ws)`` を持てば ``DocumentRouter`` を口にする（言語ごとに正しいハンドルへ文書を振り分け、1 言語に複数の
        サーバーがあれば全部に同期して診断の待ちを合わせる。BUG-0023）。``lsp`` に ``LspHandle`` が渡る（T-13）ときも口は同じ ``DocumentRouter``
        で、``lsp`` は「LSP で検証する」手がかりにとどまり、LSP の状態は READY とする。ルーターを出せないプールのときだけ、その
        ハンドルの ``documents`` / ``diagnostics``。``lsp`` が無いときの LSP の状態は言語ごとにプールの ``status``。``overrides`` が最優先。
        """
        rt = self.runtime(ws.workspace_id)
        if rt is None or self.hub is None:
            hub = self.hub
            if hub is not None and not hub._started.is_set():  # noqa: SLF001  同じモジュールの束
                # 起動の最中（最初の ``sync_roots`` が終わっていない。入口の ``ready_gate`` の上限を超えた）。待てば直る状態を
                # 「未対応」（E_UNSUPPORTED = その操作を提供しない）として返さない。再試行を促す（BUG-0046）
                raise LuciankeError(
                    ErrorCode.E_TIMEOUT,
                    "the workspace index is still opening; retry in a moment",
                    details={"reason": "index_opening", "workspace_id": ws.workspace_id, "retry_after_ms": 1000},
                )
            details: dict[str, Any] = {"reason": "index_not_open", "workspace_id": ws.workspace_id}
            if hub is not None and ws.workspace_id in hub.failed:
                details["open_error"] = hub.failed[ws.workspace_id]  # 開けなかった理由（次に起きたとき、応答だけで原因が分かる）
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                "editing is not available: the index for this workspace is not open",
                details=details,
            )
        kw: dict[str, Any] = {
            "locks": self.hub.locks,
            "writer_index": rt.writer,
            "journal_dir": rt.journal_dir,
            "indexer": rt.indexer,
            "watcher": rt.watcher,
            "reader_pool": rt.readers,
            "trees": self.trees,
        }
        wiring = self._router_wiring(ws)
        if lsp is not None:
            # ``lsp``（T-13 が QuickFix を供給したサーバー）は「LSP で検証する」の手がかり。口は 1 サーバー分ではなく ``DocumentRouter``:
            # 書き込み後の ``didChange`` と診断の待ちを、その言語の動いている全サーバー（basedpyright と ruff）へ届ける（BUG-0023）。
            if wiring:
                kw.update(wiring)
            else:  # ルーターを出せないプール（偽物など）: 渡されたハンドルの 1 サーバー
                kw.update(documents=lsp.documents, diagnostics_hub=lsp.diagnostics)
            kw["lsp_status"] = lambda _lang: LspStatus.READY
        else:
            kw.update(wiring)
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return EditTransaction(ws, **kw)


__all__ = ["IndexerSink", "RuntimeHub", "WiredServices", "WorkspaceRuntime"]
