"""T-00 ``configure_workspace`` の契約テスト（仕様書 §11.1 の事後条件 1〜4 と、設定の永続・再起動耐性）。

``tools/__init__.py`` の自動登録と ``app.py`` の結線には依存しない。偽のレジストリに ``SPEC`` を載せた ``Pipeline`` で呼ぶ。
サービスは ``lines`` と ``hub`` だけを持つ偽物。``FakeHub`` は ``tools/_runtime.py`` の ``RuntimeHub`` と同じ形（``context`` / ``cache_root``
/ ``runtime()`` / ``sync_roots()`` / ``lsp_pool``）で、書き手（``IndexWriter``）は実物、索引器は

* ``gated``（既定）: ジョブが ``release()`` まで ``running`` のままの偽物（事後条件 2 を決定的にする）、
* ``real``: 実物の ``Indexer``（F-POLY を実際に走査する）

を選べる。F-POLY（``tests/fixtures/poly``）は ``tmp_path`` にコピーして使う。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import compact_grammar as g
import pytest

from lucianke.core.errors import ErrorCode
from lucianke.core.model import LspStatus
from lucianke.engine.indexer import Indexer, JobStatus
from lucianke.index.connection import ReaderPool, db_path_for, open_reader
from lucianke.index.writer import IndexWriter
from lucianke.lsp.pool import PoolStatus
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t00_configure_workspace as t00
from lucianke.tools._base import Services
from lucianke.workspace.context import WorkspaceContext
from lucianke.workspace.types import Workspace

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
ID_RE = r"[A-Za-z0-9._-]+"


# --- 偽物 -------------------------------------------------------------------------------------


class GatedIndexer:
    """``start_scan_job`` が ``running`` のジョブを作り、``release()`` まで終わらない索引器の偽物（決定的な事後条件 2 用）。"""

    def __init__(self, ws: Workspace) -> None:
        self.workspace = ws
        self._jobs: dict[str, JobStatus] = {}
        self._seq = 0
        self.fail_submit = False

    def start_scan_job(self) -> str:
        if self.fail_submit:
            raise RuntimeError("queue is full")
        self._seq += 1
        job_id = f"job_{self._seq:04d}"
        self._jobs[job_id] = JobStatus(
            job_id, "reindex", "running", 0, "2026-10-29T00:00:00+00:00", None, None, None
        )
        return job_id

    def job_status(self, job_id: str) -> JobStatus | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[JobStatus]:
        return list(reversed(self._jobs.values()))

    def release(self) -> None:
        for job_id, job in list(self._jobs.items()):
            if job.state == "running":
                self._jobs[job_id] = dataclasses.replace(
                    job, state="done", progress=100, finished_at="2026-10-29T00:00:01+00:00"
                )

    async def aclose(self) -> None:
        return None


@dataclass
class FakeHandle:
    language_id: str
    role: str
    ws: Workspace
    started_at: str | None = None
    state: LspStatus = LspStatus.STANDBY

    def snapshot(self) -> PoolStatus:
        return PoolStatus(self.state, None, "pyright" if self.role == "types" else "ruff", 4242, None)


class FakePool:
    """``LspPool`` の偽物: T-00 が使う ``handles`` / ``status`` / ``reset`` / ``ensure_started`` だけ。"""

    def __init__(self, ws: Workspace) -> None:
        self.created = [FakeHandle("python", "types", ws), FakeHandle("python", "lint", ws)]
        self.resets: list[tuple[str | None, str]] = []
        self.started: list[str] = []
        self.seen_ws: list[Workspace] = []
        for h in self.created:
            h.started_at = "2026-10-01T00:00:00+00:00"
            h.state = LspStatus.READY

    def handles(self, ws: Workspace | None = None, language_id: str | None = None) -> list[FakeHandle]:
        return [h for h in self.created if language_id is None or h.language_id == language_id]

    def status(self, language_id: str, ws: Workspace, role: str = "types") -> PoolStatus:
        if language_id == "typescript":
            return PoolStatus(LspStatus.STANDBY, None, "vtsls", None, None)
        return PoolStatus(LspStatus.NOT_APPLICABLE, None, None, None, None)

    async def reset(self, language_id: str | None, ws: Workspace) -> None:
        self.resets.append((language_id, ws.workspace_id))
        for h in self.handles(ws, language_id):
            h.state = LspStatus.STANDBY
            h.started_at = None

    async def ensure_started(self, language_id: str, ws: Workspace) -> None:
        await asyncio.sleep(0)
        self.seen_ws.extend(h.ws for h in self.handles(ws, language_id))
        for h in self.handles(ws, language_id):
            h.state = LspStatus.READY
            h.started_at = datetime.now(UTC).isoformat()
        self.started.append(language_id)


@dataclass
class Runtime:
    ws: Workspace
    db_path: Path
    writer: IndexWriter
    readers: ReaderPool
    indexer: Any
    scan_job_id: str | None = None


class FakeHub:
    def __init__(
        self,
        cache_root: Path,
        cwd: Path,
        *,
        real_indexer: bool = False,
        pool: FakePool | None = None,
    ) -> None:
        self.cache_root = cache_root
        self.real_indexer = real_indexer
        self.lsp_pool = pool
        self.failed: dict[str, str] = {}
        self.context = WorkspaceContext(cwd=cwd, env={}, known_roots=t00.load_known_roots(cache_root))
        self.trees = TreeCache()
        self._runtimes: dict[str, Runtime] = {}
        self.changed: list[Workspace] = []

    def runtime(self, workspace_id: str | None = None) -> Runtime | None:
        if workspace_id is None:
            workspace_id = self.context.active().workspace_id
        return self._runtimes.get(workspace_id)

    def _new_indexer(self, ws: Workspace, rt_writer: IndexWriter, readers: ReaderPool) -> Any:
        if self.real_indexer:
            return Indexer(ws, rt_writer, readers, self.trees)
        return GatedIndexer(ws)

    async def sync_roots(self, paths: Sequence[str]) -> list[Workspace]:
        registered = self.context.register_roots(list(paths), replace=True)
        wanted = {ws.workspace_id for ws in registered}
        for ws_id in [i for i in self._runtimes if i not in wanted]:
            await self._close(self._runtimes.pop(ws_id))
        for ws in registered:
            if ws.workspace_id in self._runtimes:
                continue
            db_path = db_path_for(self.cache_root, ws.root)
            writer = await asyncio.to_thread(IndexWriter, db_path)
            readers = ReaderPool(db_path, size=2)
            rt = Runtime(ws, db_path, writer, readers, self._new_indexer(ws, writer, readers))
            rt.scan_job_id = rt.indexer.start_scan_job()
            self._runtimes[ws.workspace_id] = rt
        return registered

    async def workspace_changed(self, ws: Workspace) -> None:
        self.changed.append(ws)
        rt = self._runtimes[ws.workspace_id]
        old = rt.indexer
        rt.ws = ws
        rt.indexer = self._new_indexer(ws, rt.writer, rt.readers)
        await old.aclose()

    async def _close(self, rt: Runtime) -> None:
        await rt.indexer.aclose()
        rt.readers.close()
        await asyncio.to_thread(rt.writer.close)

    async def aclose(self) -> None:
        board = getattr(self, "job_board", None)
        if board is not None:
            await board.aclose()
        for rt in list(self._runtimes.values()):
            await self._close(rt)
        self._runtimes.clear()


@dataclass(slots=True)
class FakeServices(Services):
    lines: ModuleType
    hub: FakeHub

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        if workspace_id is not None:
            return self.hub.context.get(workspace_id).workspace_id  # 未登録なら E_WORKSPACE_NOT_CONFIGURED
        listed = self.hub.context.list()
        return self.hub.context.active().workspace_id if listed else None  # 何も無くても set_roots は通す


@dataclass
class Rig:
    pipeline: Pipeline
    hub: FakeHub
    cache_root: Path
    work: Path
    closers: list[Any] = field(default_factory=list)

    async def call(self, fmt: str = "json", **args: Any):
        return await self.pipeline.call_tool("configure_workspace", {"format": fmt, **args})

    async def status(self, **args: Any) -> dict[str, Any]:
        r = await self.call(action="status", **args)
        assert r.structured is not None and r.structured["ok"], r.text
        return dict(r.structured)

    async def ok(self, **args: Any) -> dict[str, Any]:
        r = await self.call(**args)
        assert r.structured is not None and r.structured["ok"], r.text
        return dict(r.structured)

    async def fail(self, **args: Any) -> dict[str, Any]:
        r = await self.call(**args)
        assert r.structured is not None and not r.structured["ok"], r.text
        return dict(r.structured["error"])

    async def wait_job(self, job_id: str, timeout_s: float = 20.0) -> dict[str, Any]:
        """クライアントと同じに、``status job_id`` を ``done`` / ``failed`` になるまで引く。"""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            st = await self.status(job_id=job_id)
            (job,) = st["data"]["jobs"]
            if job["state"] in ("done", "failed"):
                return job
            assert asyncio.get_running_loop().time() < deadline, f"job {job_id} did not finish: {job}"
            await asyncio.sleep(0.01)


def make_rig(tmp_path: Path, cwd: Path, *, real_indexer: bool = False, pool: FakePool | None = None) -> Rig:
    cache_root = tmp_path / "cache"
    hub = FakeHub(cache_root, cwd, real_indexer=real_indexer, pool=pool)
    registry = ToolRegistry()
    registry.register(t00.SPEC)
    pipeline = Pipeline(registry, services=FakeServices(lines=compact, hub=hub))
    return Rig(pipeline, hub, cache_root, tmp_path)


@pytest.fixture
def poly(tmp_path: Path) -> Path:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root)
    return root


@pytest.fixture
async def rig(tmp_path: Path, poly: Path) -> AsyncIterator[Rig]:
    r = make_rig(tmp_path, poly)
    yield r
    await r.hub.aclose()


def workspaces_of(structured: dict[str, Any]) -> list[dict[str, Any]]:
    return list(structured["data"]["workspaces"])


def rows_in_caches(cache_root: Path) -> list[tuple[Any, ...]]:
    """キャッシュ配下の全 DB の ``workspaces`` 行集合（並べ替え済み）。"""
    out: list[tuple[Any, ...]] = []
    for path in sorted(cache_root.glob("*/paad.sqlite")):
        conn = open_reader(path)
        try:
            out += [
                (path.parent.name, *r) for r in conn.execute("SELECT * FROM workspaces ORDER BY workspace_id")
            ]
        finally:
            conn.close()
    return sorted(out)


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- 事後条件 1: set_roots ---------------------------------------------------------------------


async def test_post1_set_roots_registers_every_root_with_a_valid_id(
    rig: Rig, poly: Path, tmp_path: Path
) -> None:
    odd = tmp_path / "my app (v2)"  # 文字集合の外の文字は _ に置換される
    odd.mkdir()
    (odd / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    r = await rig.ok(action="set_roots", root_paths=[str(poly), str(odd)])
    assert r["data"]["job_id"] is not None  # 初回の索引ジョブ
    st = await rig.status()
    listed = workspaces_of(st)
    assert {Path(w["root_path"]).resolve() for w in listed} == {poly.resolve(), odd.resolve()}

    for w in listed:
        assert re.fullmatch(ID_RE, w["workspace_id"]), w["workspace_id"]
    assert st["data"]["active_workspace_id"] == listed[0]["workspace_id"]  # 最初のルートがアクティブ
    assert (
        next(w for w in listed if w["root_path"] == odd.resolve().as_posix())["workspace_id"] == "my_app__v2_"
    )
    assert [w["active"] for w in listed] == [True, False]


async def test_post1_set_roots_assigns_distinct_ids_to_same_named_roots(rig: Rig, tmp_path: Path) -> None:
    a, b = tmp_path / "x" / "app", tmp_path / "y" / "app"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    await rig.ok(action="set_roots", root_paths=[str(a), str(b)])
    ids = [w["workspace_id"] for w in workspaces_of(await rig.status())]
    assert ids == ["app", "app-2"]


async def test_set_roots_lists_sub_project_markers(rig: Rig, poly: Path) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    (w,) = workspaces_of(await rig.status())
    markers = {(s["path"], s["marker"]) for s in w["sub_projects"]}
    assert (".", "pyproject.toml") in markers


# --- 事後条件 4: 存在しないパスを含む set_roots は何も変えない ------------------------------------------


async def test_post4_set_roots_with_a_missing_path_changes_nothing(
    rig: Rig, poly: Path, tmp_path: Path
) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    before_status = workspaces_of(await rig.status())
    before_rows = rows_in_caches(rig.cache_root)
    other = tmp_path / "other"
    other.mkdir()
    err = await rig.fail(action="set_roots", root_paths=[str(other), str(tmp_path / "does-not-exist")])
    assert err["code"] == "E_INVALID_ARGUMENT"
    assert err["details"]["param"] == "root_paths"
    assert workspaces_of(await rig.status()) == before_status
    assert rows_in_caches(rig.cache_root) == before_rows
    assert not db_path_for(rig.cache_root, other).exists()  # 有効だった 1 つ目のルートも登録されていない


async def test_post4_missing_path_on_a_fresh_server_registers_nothing(rig: Rig, tmp_path: Path) -> None:
    err = await rig.fail(action="set_roots", root_paths=[str(tmp_path / "nope")])
    assert err["code"] == "E_INVALID_ARGUMENT"
    st = await rig.status()
    assert workspaces_of(st) == []
    assert st["data"]["active_workspace_id"] is None


async def test_set_roots_without_root_paths_is_invalid(rig: Rig) -> None:
    for args in ({}, {"root_paths": []}):
        err = await rig.fail(action="set_roots", **args)
        assert err["code"] == "E_INVALID_ARGUMENT" and err["details"]["param"] == "root_paths"


# --- 事後条件 2: reindex は 100 ms 以内に job_id を返す --------------------------------------------------


async def test_post2_reindex_returns_a_job_id_within_100ms_and_the_job_is_visible(
    rig: Rig, poly: Path
) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    rig.hub.runtime().indexer.release()  # 初回のジョブを終わらせる
    r = await rig.ok(action="reindex")
    assert r["timing_ms"] <= 100
    job_id = r["data"]["job_id"]
    assert job_id and job_id != rig.hub.runtime().scan_job_id
    st = await rig.status()
    jobs = {j["job_id"]: j for j in st["data"]["jobs"]}
    assert jobs[job_id]["state"] in ("queued", "running")  # 完了を待たずに返した
    assert jobs[job_id]["kind"] == "reindex"
    rig.hub.runtime().indexer.release()
    assert (await rig.wait_job(job_id))["state"] == "done"


@pytest.mark.parametrize("scope", ["all", "symbols"])
async def test_reindex_all_and_symbols_start_a_scan(rig: Rig, poly: Path, scope: str) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    r = await rig.ok(action="reindex", reindex_scope=scope)
    assert r["data"]["job_id"].startswith("job_")


async def test_reindex_lsp_resets_the_lsp_and_git_is_a_noop_job(tmp_path: Path, poly: Path) -> None:
    rig = make_rig(tmp_path, poly)
    try:
        await rig.ok(action="set_roots", root_paths=[str(poly)])
        ws_id = rig.hub.context.active().workspace_id
        rig.hub.lsp_pool = pool = FakePool(rig.hub.context.active())
        r = await rig.ok(action="reindex", reindex_scope="lsp")
        assert (await rig.wait_job(r["data"]["job_id"]))["state"] == "done"
        assert pool.resets == [(None, ws_id)]
        r = await rig.ok(action="reindex", reindex_scope="git")
        assert (await rig.wait_job(r["data"]["job_id"]))["state"] == "done"
        assert any("git history is not indexed" in w for w in r["warnings"])
    finally:
        await rig.hub.aclose()


async def test_reindex_submit_failure_is_e_internal(rig: Rig, poly: Path) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    rig.hub.runtime().indexer.fail_submit = True
    err = await rig.fail(action="reindex")
    assert err["code"] == "E_INTERNAL"


async def test_reindex_before_any_workspace_is_not_configured(rig: Rig) -> None:
    err = await rig.fail(action="reindex")
    assert err["code"] == "E_WORKSPACE_NOT_CONFIGURED"


# --- 事後条件 3: set_python_env -----------------------------------------------------------------------


async def test_post3_set_python_env_normalizes_the_interpreter_and_restarts_the_python_lsp(
    tmp_path: Path, poly: Path
) -> None:
    rig = make_rig(tmp_path, poly)
    try:
        await rig.ok(action="set_roots", root_paths=[str(poly)])
        pool = FakePool(rig.hub.context.active())
        rig.hub.lsp_pool = pool
        called_at = datetime.now(UTC)
        await asyncio.sleep(
            0.05
        )  # Windows の時計は 15 ms 刻み。呼び出し時刻と再起動時刻が同じ目盛りに載らないようにする
        r = await rig.ok(action="set_python_env", python_interpreter=sys.executable)
        job_id = r["data"]["job_id"]
        assert job_id  # 再起動は非同期で、応答に job_id を含める
        assert (await rig.wait_job(job_id))["state"] == "done"
        st = await rig.status()
        (w,) = workspaces_of(st)
        assert w["python_env"]["interpreter"] == os.path.abspath(sys.executable)
        assert w["python_env"]["manager"] == "explicit"
        python = [d for d in st["data"]["lsp_daemons"] if d["language"] == "python"]
        assert {d["role"] for d in python} == {"types", "lint"}
        for d in python:
            assert datetime.fromisoformat(d["started_at"]) > called_at
            assert d["status"] == "ready"
        assert pool.seen_ws and all(
            ws.python_env.interpreter == os.path.abspath(sys.executable) for ws in pool.seen_ws
        )
    finally:
        await rig.hub.aclose()


async def test_set_python_env_accepts_a_venv_directory(rig: Rig, poly: Path, tmp_path: Path) -> None:
    exe = Path(sys.executable)
    venv = Path(sys.prefix)
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    if not (venv / scripts / exe.name).is_file():
        pytest.skip("not running inside a venv")
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    await rig.ok(action="set_python_env", python_interpreter=str(venv))
    (w,) = workspaces_of(await rig.status())
    assert Path(w["python_env"]["interpreter"]) == venv / scripts / exe.name


async def test_set_python_env_with_a_bad_interpreter_changes_nothing(
    rig: Rig, poly: Path, tmp_path: Path
) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    before = workspaces_of(await rig.status())
    rows = rows_in_caches(rig.cache_root)
    notexe = tmp_path / "plain.txt"
    notexe.write_text("x", encoding="utf-8")
    for bad in (str(tmp_path / "missing"), str(notexe) if sys.platform != "win32" else str(tmp_path)):
        err = await rig.fail(action="set_python_env", python_interpreter=bad)
        assert err["code"] == "E_INVALID_ARGUMENT"
    err = await rig.fail(action="set_python_env")
    assert err["code"] == "E_INVALID_ARGUMENT" and err["details"]["param"] == "python_interpreter"
    assert workspaces_of(await rig.status()) == before
    assert rows_in_caches(rig.cache_root) == rows


# --- set_excludes -----------------------------------------------------------------------------------


async def test_set_excludes_replaces_the_list_and_removes_matching_files_from_the_index(
    tmp_path: Path, poly: Path
) -> None:
    rig = make_rig(tmp_path, poly, real_indexer=True)
    try:
        first = await rig.ok(action="set_roots", root_paths=[str(poly)])
        assert (await rig.wait_job(first["data"]["job_id"]))["state"] == "done"
        rt = rig.hub.runtime()

        def indexed(prefix: str) -> int:
            with rt.readers.reader() as conn:
                return int(
                    conn.execute("SELECT count(*) FROM files WHERE path LIKE ?", (prefix + "%",)).fetchone()[
                        0
                    ]
                )

        assert indexed("legacy/") > 0
        r = await rig.ok(action="set_excludes", exclude_globs=["legacy/"])
        assert (await rig.wait_job(r["data"]["job_id"]))["state"] == "done"
        (w,) = workspaces_of(await rig.status())
        assert w["exclude_globs"] == ["legacy/"]
        assert indexed("legacy/") == 0
        assert indexed("src/") > 0
        # 置換（追記ではない）: 別の glob に替えると、legacy/ は戻り、docs/ が消える
        r = await rig.ok(action="set_excludes", exclude_globs=["docs/"])
        assert (await rig.wait_job(r["data"]["job_id"]))["state"] == "done"
        (w,) = workspaces_of(await rig.status())
        assert w["exclude_globs"] == ["docs/"]
        assert indexed("legacy/") > 0 and indexed("docs/") == 0
        # [] で空に戻る
        r = await rig.ok(action="set_excludes", exclude_globs=[])
        assert (await rig.wait_job(r["data"]["job_id"]))["state"] == "done"
        assert workspaces_of(await rig.status())[0]["exclude_globs"] == []
        assert indexed("docs/") > 0
    finally:
        await rig.hub.aclose()


async def test_set_excludes_requires_exclude_globs(rig: Rig, poly: Path) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    err = await rig.fail(action="set_excludes")
    assert err["code"] == "E_INVALID_ARGUMENT" and err["details"]["param"] == "exclude_globs"
    err = await rig.fail(action="set_excludes", exclude_globs=["ok", " "])
    assert err["code"] == "E_INVALID_ARGUMENT"
    assert workspaces_of(await rig.status())[0]["exclude_globs"] == []


async def test_set_excludes_rebinds_the_runtime(rig: Rig, poly: Path) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    await rig.ok(action="set_excludes", exclude_globs=["a/"])
    assert [ws.exclude_globs for ws in rig.hub.changed] == [("a/",)]


async def test_default_base_ref_is_applied_with_a_setting_action_and_ignored_by_status(
    rig: Rig, poly: Path
) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    assert workspaces_of(await rig.status())[0]["default_base_ref"] == "HEAD"
    r = await rig.call(action="status", default_base_ref="main")
    assert r.structured is not None and any("ignored" in w for w in r.structured["warnings"])
    assert workspaces_of(await rig.status())[0]["default_base_ref"] == "HEAD"
    await rig.ok(action="set_excludes", exclude_globs=[], default_base_ref="origin/main")
    assert workspaces_of(await rig.status())[0]["default_base_ref"] == "origin/main"
    err = await rig.fail(action="set_excludes", exclude_globs=[], default_base_ref="  ")
    assert err["code"] == "E_INVALID_ARGUMENT"


# --- status ----------------------------------------------------------------------------------------


async def test_status_reports_languages_python_git_cache_and_lsp(tmp_path: Path, poly: Path) -> None:
    (poly / ".git").mkdir()  # dulwich が開ける最小の空リポジトリ
    from dulwich.repo import Repo

    shutil.rmtree(poly / ".git")
    Repo.init(str(poly)).close()
    rig = make_rig(tmp_path, poly, real_indexer=True)
    try:
        first = await rig.ok(action="set_roots", root_paths=[str(poly)])
        assert (await rig.wait_job(first["data"]["job_id"]))["state"] == "done"
        rig.hub.lsp_pool = FakePool(rig.hub.context.active())
        st = await rig.status()
        (w,) = workspaces_of(st)
        langs = {d["language"]: d for d in w["languages"]}
        assert langs["python"]["tier"] == 1 and langs["python"]["file_count"] > 0
        assert {"language", "tier", "file_count"} == set(w["languages"][0])
        assert set(w["git"]) == {"branch", "head_sha", "dirty_files"} and w["git"]["dirty_files"] > 0
        cache = st["data"]["cache"]
        assert cache["files"] == sum(d["file_count"] for d in w["languages"]) > 0
        assert cache["symbols"] > 0 and cache["relations"] > 0 and cache["last_sync_at"]
        assert w["cache"]["files"] == cache["files"]
        assert {d["server"] for d in st["data"]["lsp_daemons"]} >= {"pyright", "ruff", "vtsls"}
        assert st["_resolution_meta"]["lsp_status"] == "ready"
        assert st["_resolution_meta"]["confidence_level"] == "exact"
        assert st["_resolution_meta"]["is_degraded"] is False
        assert set(st["data"]["jobs"][0]) >= {
            "job_id",
            "kind",
            "state",
            "progress",
            "started_at",
            "finished_at",
            "error",
        }
    finally:
        await rig.hub.aclose()


async def test_status_without_workspaces_is_ok_and_says_so(rig: Rig) -> None:
    st = await rig.status()
    assert st["data"]["workspaces"] == [] and st["data"]["jobs"] == []
    assert any("set_roots" in w for w in st["warnings"])
    assert st["_resolution_meta"]["lsp_status"] == "n/a"


async def test_status_is_read_only(rig: Rig, poly: Path) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    hashes, rows = tree_hashes(poly), rows_in_caches(rig.cache_root)
    jobs = len((await rig.status())["data"]["jobs"])
    for _ in range(3):
        await rig.status()
    assert tree_hashes(poly) == hashes
    assert rows_in_caches(rig.cache_root) == rows
    assert len((await rig.status())["data"]["jobs"]) == jobs  # ジョブも増えない
    assert rig.hub.changed == []


async def test_status_unknown_workspace_and_job(rig: Rig, poly: Path) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    err = await rig.fail(action="status", workspace_id="nope")
    assert err["code"] == "E_WORKSPACE_NOT_CONFIGURED"
    assert err["details"]["known_workspaces"][0]["workspace_id"] == "poly"
    err = await rig.fail(action="status", job_id="job_9999")
    assert err["code"] == "E_WORKSPACE_NOT_CONFIGURED" and err["details"]["job_id"] == "job_9999"
    st = await rig.status(job_id=rig.hub.runtime().scan_job_id)
    assert [j["job_id"] for j in st["data"]["jobs"]] == [rig.hub.runtime().scan_job_id]


async def test_unknown_action_is_invalid(rig: Rig) -> None:
    err = await rig.fail(action="drop_everything")
    assert err["code"] == "E_INVALID_ARGUMENT"


async def test_revision_is_ignored_with_a_warning(rig: Rig) -> None:
    r = await rig.call(action="status", revision="HEAD")
    assert r.structured is not None and r.structured["ok"], r.text
    assert any("revision" in w for w in r.structured["warnings"])


# --- compact ---------------------------------------------------------------------------------------


async def test_compact_output_is_grammatical(tmp_path: Path, poly: Path) -> None:
    rig = make_rig(tmp_path, poly)
    try:
        r = await rig.call(fmt="compact", action="set_roots", root_paths=[str(poly)])
        assert not r.is_error and g.validate(r.text) == [], r.text
        rig.hub.lsp_pool = FakePool(rig.hub.context.active())
        r = await rig.call(fmt="compact", action="status")
        assert not r.is_error and g.validate(r.text) == [], r.text
        lines = r.text.splitlines()
        assert lines[0].startswith("# configure_workspace ok conf=exact lsp=ready rev=WORKING_TREE ws=poly ")
        assert "| id | root | langs | python | branch | dirty |" in lines
        assert any(line.startswith("| poly | ") for line in lines)
        assert any(line.startswith("workspaces: 1 (active: poly)") for line in lines)
        assert any(line.startswith("lsp: pyright=ready ruff=ready") for line in lines)
        assert any(line.startswith("cache: files=") for line in lines)
        assert any(line.startswith("jobs: job_0001 reindex running") for line in lines)
        r = await rig.call(fmt="compact", action="reindex")
        assert not r.is_error and g.validate(r.text) == [], r.text
        assert any(line.startswith("job: job_0002 (poly)") for line in r.text.splitlines())
        r = await rig.call(fmt="compact", action="set_roots", root_paths=[str(tmp_path / "nope")])
        assert r.is_error and g.validate(r.text) == [], r.text
    finally:
        await rig.hub.aclose()


# --- 永続・再起動耐性（ADR-16） -----------------------------------------------------------------------


async def test_settings_survive_a_restart(tmp_path: Path, poly: Path) -> None:
    first = make_rig(tmp_path, poly)
    try:
        await first.ok(action="set_roots", root_paths=[str(poly)])
        await first.ok(
            action="set_excludes", exclude_globs=["legacy/", "*.min.js"], default_base_ref="origin/main"
        )
        await first.ok(action="set_python_env", python_interpreter=sys.executable)
        (w1,) = workspaces_of(await first.status())
    finally:
        await first.hub.aclose()  # プロセスが死ぬ: 書き手を閉じ、メモリの状態は全部捨てる

    second = make_rig(tmp_path, poly)  # 新しいプロセス: 空の WorkspaceContext、同じキャッシュ
    try:
        assert (
            workspaces_of(await second.status()) == []
        )  # 登録は覚えていない（roots/list か cwd か set_roots から入る）
        await second.ok(action="set_roots", root_paths=[str(poly)])
        (w2,) = workspaces_of(await second.status())
        assert w2["workspace_id"] == w1["workspace_id"]  # URN が安定する
        assert w2["exclude_globs"] == ["legacy/", "*.min.js"]
        assert w2["default_base_ref"] == "origin/main"
        assert w2["python_env"] == w1["python_env"]
        assert w2["python_env"]["interpreter"] == os.path.abspath(sys.executable)
        # 束（索引器）が復元後の Workspace で起きている
        assert second.hub.runtime().indexer.workspace.exclude_globs == ("legacy/", "*.min.js")
    finally:
        await second.hub.aclose()


async def test_restore_does_not_freeze_autodetected_values(tmp_path: Path, poly: Path) -> None:
    """自動検出・``.lucianke.toml`` と同じ値は「ツールでの明示」に固めない。"""
    (poly / ".lucianke.toml").write_text('exclude_globs = ["docs/"]\n', encoding="utf-8")
    first = make_rig(tmp_path, poly)
    try:
        await first.ok(action="set_roots", root_paths=[str(poly)])
        assert workspaces_of(await first.status())[0]["exclude_globs"] == ["docs/"]
    finally:
        await first.hub.aclose()
    (poly / ".lucianke.toml").write_text(
        'exclude_globs = ["web/"]\n', encoding="utf-8"
    )  # 設定ファイルを書き換える
    second = make_rig(tmp_path, poly)
    try:
        await second.ok(action="set_roots", root_paths=[str(poly)])
        # 保存された ["docs/"] は、いまの設定ファイルの値（["web/"]）と違うので「ツールでの明示」として戻る
        # （設計書 §7.3.5 の「次回起動時の初期値」。保存値が設定ファイル由来かツール由来かは区別できない）
        assert workspaces_of(await second.status())[0]["exclude_globs"] == ["docs/"]
    finally:
        await second.hub.aclose()
    unchanged = make_rig(tmp_path / "again", poly)
    try:
        await unchanged.ok(action="set_roots", root_paths=[str(poly)])
        assert workspaces_of(await unchanged.status())[0]["exclude_globs"] == [
            "web/"
        ]  # 保存が無ければ設定ファイル
    finally:
        await unchanged.hub.aclose()


async def test_a_vanished_interpreter_falls_back_to_autodetection_with_a_warning(
    tmp_path: Path, poly: Path
) -> None:
    first = make_rig(tmp_path, poly)
    try:
        await first.ok(action="set_roots", root_paths=[str(poly)])
        fake = tmp_path / ("python.cmd" if sys.platform == "win32" else "python")
        fake.write_text("@echo off\n" if sys.platform == "win32" else "#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o755)
        await first.ok(action="set_python_env", python_interpreter=str(fake))
    finally:
        await first.hub.aclose()
    fake.unlink()
    second = make_rig(tmp_path, poly)
    try:
        r = await second.ok(action="set_roots", root_paths=[str(poly)])
        assert any("is gone" in w for w in r["warnings"])
        (w,) = workspaces_of(await second.status())
        assert w["python_env"]["manager"] != "explicit"
    finally:
        await second.hub.aclose()


async def test_persisted_row_matches_the_workspaces_schema(rig: Rig, poly: Path) -> None:
    await rig.ok(action="set_roots", root_paths=[str(poly)])
    await rig.ok(action="set_excludes", exclude_globs=["a/b"], default_base_ref="dev")
    await asyncio.sleep(0)
    conn = sqlite3.connect(db_path_for(rig.cache_root, poly.resolve()))
    try:
        row = conn.execute(
            "SELECT workspace_id, root_path, default_base_ref, exclude_globs_json FROM workspaces"
        ).fetchall()
    finally:
        conn.close()
    assert row == [("poly", poly.resolve().as_posix(), "dev", '["a/b"]')]


async def test_tool_is_registered_with_the_spec_values() -> None:
    assert t00.SPEC.name == "configure_workspace" and t00.SPEC.tid == "T-00"
    assert t00.SPEC.default_max_tokens == 1500 and t00.SPEC.writes is False
    assert t00.SPEC.accepts_revision is False and t00.SPEC.milestone == "M1"


async def test_missing_hub_is_an_internal_error() -> None:
    registry = ToolRegistry()
    registry.register(t00.SPEC)
    pipeline = Pipeline(registry, services=SimpleNamespace(lines=compact, resolve_workspace_id=lambda w: w))  # type: ignore[arg-type]
    r = await pipeline.call_tool("configure_workspace", {"format": "json"})
    assert r.structured is not None and r.structured["error"]["code"] == ErrorCode.E_INTERNAL.value
