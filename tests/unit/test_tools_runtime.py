"""結線の束（``tools/_runtime.py``）: ``RuntimeHub.sync_roots`` が束を起こし、差分索引が走り、``WiredServices`` が ``db_path`` などを出す。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import RelPath
from lucianke.edit.transaction import EditTransaction
from lucianke.syntax.parsers import TreeCache
from lucianke.tools._runtime import RuntimeHub, WiredServices
from lucianke.tools.t00_configure_workspace import persist_workspaces


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    return root


def _services(hub: RuntimeHub | None, roots: list[str]) -> WiredServices:
    import lucianke.mcp.render.compact as compact

    return WiredServices(roots=lambda: roots, trees=TreeCache(), lines=compact, hub=hub)


def test_without_a_hub_nothing_is_indexed(project: Path) -> None:
    svc = _services(None, [str(project)])
    assert svc.db_path is None and svc.reader_pool is None and svc.runtime() is None
    assert svc.workspace().root == project.resolve()  # 従来どおり roots の先頭
    with pytest.raises(LuciankeError):
        svc.edit_transaction(svc.workspace())


async def test_sync_roots_opens_indexes_and_closes(project: Path, tmp_path: Path) -> None:
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    svc = _services(hub, [str(project)])
    assert svc.db_path is None and hub.data_version() == 0  # まだ束が無い
    registered = await hub.sync_roots([str(project)])
    assert hub._started.is_set() and await hub.wait_started(0.1)
    try:
        assert [ws.root for ws in registered] == [project.resolve()]
        rt = hub.runtime()
        assert rt is not None and rt.scan_job_id is not None
        status = await rt.indexer.wait_job(rt.scan_job_id)
        assert status is not None and status.state == "done"

        assert svc.db_path == rt.db_path and rt.db_path.is_file() and svc.reader_pool is rt.readers
        assert svc.workspace().workspace_id == registered[0].workspace_id
        with rt.readers.reader() as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM symbols WHERE is_external = 0")}
        assert {"alpha", "beta"} <= names
        assert hub.data_version() > 0

        sink = rt.watcher._sink
        assert sorted(sink.known_paths_under("pkg/")) == [RelPath("pkg/a.py"), RelPath("pkg/b.py")]
        assert sink.known_paths_under("nope/") == []

        again = await hub.sync_roots([str(project)])  # 冪等: 束は増えず、新しい走査も始まらない
        assert again == registered and hub.runtime() is rt and rt.scan_job_id == "job_0001"

        tx = svc.edit_transaction(svc.workspace())
        assert isinstance(tx, EditTransaction)
        assert svc.locks is hub.locks
    finally:
        await hub.stop_watchers()
        await hub.stop_indexers()
        await hub.close_storage()


async def test_a_second_session_reuses_the_workspace_id_and_the_persisted_index(
    project: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    first = RuntimeHub(cache, TreeCache())
    (ws1,) = await first.sync_roots([str(project)])
    rt = first.runtime()
    assert rt is not None and rt.scan_job_id is not None
    await rt.indexer.wait_job(rt.scan_job_id)
    await first.stop_watchers()
    await first.stop_indexers()
    await first.close_storage()

    second = RuntimeHub(cache, TreeCache())
    (ws2,) = await second.sync_roots([str(project)])
    try:
        assert ws2.workspace_id == ws1.workspace_id
        rt2 = second.runtime()
        assert rt2 is not None and rt2.scan_job_id is not None
        await rt2.indexer.wait_job(rt2.scan_job_id)
        assert rt2.indexer.ingest_calls == 0  # 変更の無いワークスペースでは取り込みが 1 回も走らない（設計書 §7.6.5）
    finally:
        await second.stop_watchers()
        await second.stop_indexers()
        await second.close_storage()


async def test_wired_services_expose_every_attribute_the_tools_ask_for(project: Path, tmp_path: Path) -> None:
    """T-08（``code_action_store``）・T-10/11/13（``locks`` / ``writer_index`` / ``journal_dir`` ほか）・T-03〜T-20（``db_path`` / ``reader_pool``）。"""
    from lucianke.core.procs import ProcessRegistry
    from lucianke.lsp.pool import LspPool

    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    hub.lsp_pool = LspPool(ProcessRegistry(), on_diagnostics=hub.on_diagnostics)
    svc = _services(hub, [str(project)])
    await hub.sync_roots([str(project)])
    try:
        for name in (
            "db_path", "reader_pool", "lines", "trees", "locks", "lsp_pool", "code_action_store",
            "writer_index", "journal_dir", "indexer", "watcher",
        ):  # fmt: skip
            assert getattr(svc, name, None) is not None, name
        assert svc.code_action_store is hub.code_action_store
    finally:
        await hub.stop_watchers()
        await hub.stop_indexers()
        await hub.close_storage()


async def _settled(hub: RuntimeHub) -> None:
    rt = hub.runtime()
    assert rt is not None and rt.scan_job_id is not None
    status = await rt.indexer.wait_job(rt.scan_job_id)
    assert status is not None and status.state == "done"


def _paths(hub: RuntimeHub) -> set[str]:
    rt = hub.runtime()
    assert rt is not None
    with rt.readers.reader() as conn:
        return {r[0] for r in conn.execute("SELECT path FROM files")}


async def _shut(hub: RuntimeHub) -> None:
    await hub.stop_watchers()
    await hub.stop_indexers()
    await hub.close_storage()


@pytest.fixture
def two_dirs(project: Path) -> Path:
    (project / "legacy").mkdir()
    (project / "legacy" / "old.py").write_text("def old():\n    return 0\n", encoding="utf-8")
    return project


async def test_saved_settings_are_restored_before_the_runtime_opens(two_dirs: Path, tmp_path: Path) -> None:
    """T-00 が保存した除外・基準リビジョンは、再起動後の ``sync_roots``（roots/list か cwd）で戻り、最初の走査から効く。"""
    cache = tmp_path / "cache"
    first = RuntimeHub(cache, TreeCache())
    (ws1,) = await first.sync_roots([str(two_dirs)])
    try:
        await _settled(first)
        assert "legacy/old.py" in _paths(first)
        first.context.set_excludes(ws1.workspace_id, ["legacy/"])
        new_ws = first.context.update(ws1.workspace_id, default_base_ref="origin/develop")
        await first.workspace_changed(new_ws)
        assert await persist_workspaces(first, [new_ws]) == []
    finally:
        await _shut(first)

    second = RuntimeHub(cache, TreeCache())
    (ws2,) = await second.sync_roots([str(two_dirs)])
    try:
        assert ws2.workspace_id == ws1.workspace_id
        assert ws2.exclude_globs == ("legacy/",) and ws2.default_base_ref == "origin/develop"
        assert second.context.get(ws2.workspace_id) is ws2
        rt = second.runtime()
        assert rt is not None and rt.ws is ws2 and rt.indexer.workspace is ws2 and rt.watcher._ws is ws2
        await _settled(second)
        assert _paths(second) >= {"pkg/a.py", "pkg/b.py"}
        assert "legacy/old.py" not in _paths(second)  # 再起動後の走査も、保存した除外を守る（戻さないと入り直す）
    finally:
        await _shut(second)


async def test_settings_the_caller_already_restored_are_not_overwritten(two_dirs: Path, tmp_path: Path) -> None:
    """T-00 の ``set_roots`` は自分で登録・復元してから ``sync_roots`` を呼ぶ。その設定を ``sync_roots`` が上書きしない。"""
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    (registered,) = hub.context.register_roots([str(two_dirs)], replace=True)
    hub.context.set_excludes(registered.workspace_id, ["legacy/"])
    (ws,) = await hub.sync_roots([str(two_dirs)])
    try:
        assert ws.exclude_globs == ("legacy/",)
        await _settled(hub)
        assert "legacy/old.py" not in _paths(hub)
    finally:
        await _shut(hub)


async def test_the_context_knows_ids_from_every_cache_before_the_first_sync(project: Path, tmp_path: Path) -> None:
    """``hub.context`` を ``sync_roots`` より先に引いても ``known_roots`` が入る（T-00 の status が先に来た場合）。"""
    cache = tmp_path / "cache"
    first = RuntimeHub(cache, TreeCache())
    (ws1,) = await first.sync_roots([str(project)])
    await _settled(first)
    await _shut(first)

    other = tmp_path / "elsewhere" / project.name  # 同じディレクトリ名の別のルート
    other.mkdir(parents=True)
    second = RuntimeHub(cache, TreeCache())
    assert second.context.list() == []  # 引いただけで登録はしない
    (ws_other,) = second.context.register_roots([str(other)])
    assert ws_other.workspace_id != ws1.workspace_id  # 前回の ID は別のルートに割り当てない
    (again,) = await second.sync_roots([str(project)])
    try:
        assert again.workspace_id == ws1.workspace_id  # 同じルートには同じ ID
    finally:
        await _shut(second)


async def test_workspace_changed_hands_the_new_rules_to_the_bundle(two_dirs: Path, tmp_path: Path) -> None:
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    (ws,) = await hub.sync_roots([str(two_dirs)])
    try:
        await _settled(hub)
        rt = hub.runtime()
        assert rt is not None and "legacy/old.py" in _paths(hub)
        new_ws = hub.context.set_excludes(ws.workspace_id, ["legacy/"])
        await hub.workspace_changed(new_ws)
        assert rt.ws is new_ws and rt.indexer.workspace is new_ws and rt.watcher._ws is new_ws
        assert rt.sink is not None and rt.sink._ws is new_ws
        await rt.indexer.scan()  # 差し替えた規則で走査し、除外に入ったファイルを files から外す
        assert "legacy/old.py" not in _paths(hub) and "pkg/a.py" in _paths(hub)
        await hub.workspace_changed(hub.context.set_excludes(ws.workspace_id, []))  # 除外の解除も届く
        await rt.indexer.scan()
        assert "legacy/old.py" in _paths(hub)
    finally:
        await _shut(hub)


async def test_workspace_changed_without_a_runtime_or_a_pool_is_harmless(project: Path, tmp_path: Path) -> None:
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    (ws,) = hub.context.register_roots([str(project)])
    await hub.workspace_changed(ws)  # 束が無い


async def test_workspace_changed_rebinds_the_lsp_handles_that_can(project: Path, tmp_path: Path) -> None:
    class Handle:
        def __init__(self, language: str, *, boom: bool = False) -> None:
            self.language_id = language
            self.boom = boom
            self.seen: list[Any] = []

        def rebind(self, ws: Any) -> None:
            if self.boom:
                raise RuntimeError("boom")
            self.seen.append(ws)

    class Old:  # rebind の無い（16c より前の）ハンドル
        language_id = "typescript"

    class Pool:
        def __init__(self, handles: list[Any]) -> None:
            self._handles = handles

        def handles(self, ws: Any = None, language_id: Any = None) -> list[Any]:
            return list(self._handles)

    good, bad = Handle("python"), Handle("lint", boom=True)
    hub = RuntimeHub(tmp_path / "cache", TreeCache(), lsp_pool=Pool([good, bad, Old()]))  # type: ignore[arg-type]
    (ws,) = await hub.sync_roots([str(project)])
    try:
        await _settled(hub)
        new_ws = hub.context.set_excludes(ws.workspace_id, ["pkg/"])
        await hub.workspace_changed(new_ws)  # 1 つ失敗しても、rebind の無いものがあっても止まらない
        assert good.seen == [new_ws]
    finally:
        await _shut(hub)


async def test_resolve_workspace_id_passes_none_through_while_no_root_is_known(project: Path, tmp_path: Path) -> None:
    """T-00 ``set_roots`` の最初の呼び出しを通す。他のツールは、その後の ``workspace()`` / ``db_path`` で未構成になる。"""
    import lucianke.mcp.render.compact as compact

    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    svc = WiredServices(roots=lambda: [], trees=TreeCache(), lines=compact, hub=hub)
    assert svc.resolve_workspace_id(None) is None
    assert svc.db_path is None
    with pytest.raises(LuciankeError) as err:
        svc.workspace()
    assert err.value.code is ErrorCode.E_WORKSPACE_NOT_CONFIGURED
    with pytest.raises(LuciankeError) as err:
        svc.resolve_workspace_id("nope")  # 明示した ID は従来どおり検査する
    assert err.value.code is ErrorCode.E_WORKSPACE_NOT_CONFIGURED

    (ws,) = await hub.sync_roots([str(project)])
    try:
        assert svc.resolve_workspace_id(None) == ws.workspace_id
        assert svc.resolve_workspace_id(ws.workspace_id) == ws.workspace_id
        assert svc.workspace().workspace_id == ws.workspace_id  # roots が空でも hub が知っていれば解決する
    finally:
        await _shut(hub)
