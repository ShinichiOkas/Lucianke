"""結線の束（``tools/_runtime.py``）: ``RuntimeHub.sync_roots`` が束を起こし、差分索引が走り、``WiredServices`` が ``db_path`` などを出す。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lucianke.core.errors import LuciankeError
from lucianke.core.model import RelPath
from lucianke.edit.transaction import EditTransaction
from lucianke.syntax.parsers import TreeCache
from lucianke.tools._runtime import RuntimeHub, WiredServices


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
