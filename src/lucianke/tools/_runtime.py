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
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lucianke.core.errors import LuciankeError
from lucianke.core.model import RelPath
from lucianke.edit.lock import WorkspaceLocks
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool, db_path_for, open_reader
from lucianke.index.ingest import DiagnosticFact, replace_lsp_diagnostics
from lucianke.index.writer import IndexWriter
from lucianke.lsp.pool import FileChangeType, LspPool
from lucianke.syntax.parsers import TreeCache
from lucianke.tools._services import ToolServices
from lucianke.workspace.context import WorkspaceContext
from lucianke.workspace.types import Workspace
from lucianke.workspace.watcher import Watcher

_log = logging.getLogger("lucianke.runtime")
WATCHER_START_TIMEOUT_S = 5.0
WATCHER_STOP_TIMEOUT_S = 2.0


class IndexerSink:
    """``Watcher`` の ``IngestSink``: 監視のイベントを ``Indexer`` に渡す（冪等。同じ内容なら ``index_file`` が何もしない）。"""

    def __init__(
        self, ws: Workspace, indexer: Indexer, readers: ReaderPool, lsp_pool: LspPool | None = None
    ) -> None:
        self._ws = ws
        self._indexer = indexer
        self._readers = readers
        self._lsp = lsp_pool

    async def changed(self, rel: RelPath) -> None:
        stats = await self._indexer.index_file(rel)
        if stats is not None:
            await self._notify(rel, FileChangeType.CHANGED)

    async def removed(self, rel: RelPath) -> None:
        if await self._indexer.remove(rel):
            await self._notify(rel, FileChangeType.DELETED)

    async def rescan(self) -> None:
        _log.info("watcher burst: switching to a differential rescan")
        await self._indexer.scan()

    def known_paths_under(self, prefix: str) -> list[RelPath]:
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._readers.reader() as conn:
            rows = conn.execute(
                "SELECT path FROM files WHERE workspace_id = ? AND path LIKE ? ESCAPE '\\'",
                (self._ws.workspace_id, like),
            ).fetchall()
        return [RelPath(r[0]) for r in rows]

    async def _notify(self, rel: RelPath, kind: FileChangeType) -> None:
        if self._lsp is None:
            return
        try:
            await self._lsp.notify_watched(self._ws, [(rel, kind)])
        except Exception:  # noqa: BLE001  LSP への通知の失敗で索引を止めない
            _log.warning("didChangeWatchedFiles failed for %s", rel, exc_info=True)


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
        watcher = Watcher(ws, IndexerSink(ws, indexer, readers, lsp_pool))
        _log.info(
            "index opened: ws=%s db=%s schema=%s", ws.workspace_id, db_path, writer.schema_action,
            extra={"event": "index_open"},
        )  # fmt: skip
        return cls(ws, db_path, writer, readers, indexer, watcher, db_path.parent / "journal")

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


def _known_roots(cache_root: Path, roots: Iterable[str]) -> dict[str, str]:
    """前回までに割り当てた ``{ルートのパス: workspace_id}``（各 DB の ``workspaces`` 表。ID を再起動をまたいで安定させる。§7.3.1 手順 2）。"""
    known: dict[str, str] = {}
    for raw in roots:
        path = db_path_for(cache_root, os.path.realpath(raw))
        if not path.is_file():
            continue
        try:
            conn = open_reader(path)
        except Exception:  # noqa: BLE001
            continue
        try:
            for root_path, ws_id in conn.execute("SELECT root_path, workspace_id FROM workspaces"):
                known[str(root_path)] = str(ws_id)
        except Exception:  # noqa: BLE001  古い・壊れた DB は無視（作り直される）
            _log.debug("could not read workspaces from %s", path, exc_info=True)
        finally:
            conn.close()
    return known


def _diagnostic_facts(diags: Sequence[dict[str, Any]], source: str) -> list[DiagnosticFact]:
    facts: list[DiagnosticFact] = []
    for d in diags:
        try:
            rng = d["range"]
            code = d.get("code")
            facts.append(
                DiagnosticFact(
                    severity=int(d.get("severity") or 1),
                    source=str(d.get("source") or source),
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
        self.failed: dict[str, str] = {}  # workspace_id -> 開けなかった理由
        self._cwd = cwd
        self._env = env
        self._context: WorkspaceContext | None = None
        self._runtimes: dict[str, WorkspaceRuntime] = {}
        self._lock = asyncio.Lock()
        self._applied: tuple[str, ...] | None = None

    # --- 参照 ---

    @property
    def context(self) -> WorkspaceContext:
        if self._context is None:
            self._context = WorkspaceContext(cwd=self._cwd, env=self._env)
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
        async with self._lock:
            key = tuple(os.path.realpath(p) for p in paths)
            if key == self._applied:
                return self.context.list()
            if self._context is None:
                known = _known_roots(self.cache_root, paths)
                self._context = WorkspaceContext(cwd=self._cwd, env=self._env, known_roots=known)
            try:
                registered = self.context.register_roots(list(paths), replace=True)
            except LuciankeError as exc:
                _log.warning("roots not registered: %s", exc.message)
                return self.context.list()
            self._applied = key
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

    # --- LSP の診断 → PaaD ---

    async def on_diagnostics(
        self, path: RelPath, diags: list[dict[str, Any]], version: int | None, source: str
    ) -> None:
        """``LspPool`` の ``on_diagnostics``。アクティブなワークスペースの PaaD の ``source`` の行を置換する（``data_version`` は進めない）。"""
        rt = self.runtime()
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


__all__ = ["IndexerSink", "RuntimeHub", "WiredServices", "WorkspaceRuntime"]
