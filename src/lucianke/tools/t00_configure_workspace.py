"""T-00 ``configure_workspace``（仕様書 §11.1）。ワークスペースの登録・Python 環境・除外規則・再索引と、稼働状態の報告。

アクション: ``status``（既定。副作用なし）・``set_roots``・``set_python_env``・``set_excludes``・``reindex``。
どのアクションも、同じ「状態の表」を返す（``status`` だけが Git・言語別ファイル数・キャッシュ統計を調べる。
``reindex`` の 100 ms 以内（事後条件 2）を守るため、他のアクションの応答は軽い項目だけ）。

``ctx.services`` に求めるもの（duck typing。``tools`` は ``mcp`` を import できない。形は ``tools/_runtime.py`` の
``RuntimeHub`` に合わせてある）:

* ``lines``: compact の行の組み立て関数のモジュール（``kv_line``・``table``）。必須。
* ``hub``: 必須。次を持つ。

  - ``hub.context``: ``WorkspaceContext``（登録・解決・設定の更新）。
  - ``hub.cache_root``: ``Path``（保存済みの設定を読む ``<cache_root>/<hash>/paad.sqlite`` の置き場）。
  - ``hub.runtime(workspace_id)``: 束（無ければ ``None``）。束は ``.writer``（``IndexWriter``。設定の保存に使う）・
    ``.indexer``（``start_scan_job()``・``jobs()`` を持つ）・``.db_path``（キャッシュ統計と言語別ファイル数を読む）・
    ``.scan_job_id``（``sync_roots`` が始めた初回の索引ジョブ。無くてもよい）を持つ。
  - ``await hub.sync_roots(paths)``: ルートの登録に合わせて束を起こす・畳む（初回の索引ジョブを始める）。冪等。
  - ``hub.lsp_pool``: ``LspPool`` か ``None``（``handles(ws)``・``status(lang, ws, role)``・``reset``・``ensure_started``）。
  - 任意: ``await hub.workspace_changed(ws)``（同期でもよい）。設定を変えた ``Workspace`` を束（``Indexer``・``Watcher``）と
    LSP に渡し直す。無ければ警告を出す（束は古い除外規則のまま走る）。
  - 任意: ``hub.job_board``（``JobBoard``）。無ければ ``hub`` に作って付ける。
  - 任意: ``hub.failed``（``{workspace_id: 理由}``。索引を開けなかった理由）。

* 起動時に ``hub.sync_roots`` がルートを登録するとき（roots/list・cwd）は、``register_roots`` の直後・束を起こす前に
  ``restore_settings(context, 新しく登録した ws, cache_root)`` を呼ぶこと（保存済みの除外規則などを初期値として戻す）。

設定の永続（ADR-16、設計書 §7.3.5）: ``exclude_globs`` / ``python_env`` / ``default_base_ref`` を PaaD の ``workspaces`` 行に保存し
（``persist_workspaces``）、次回の登録で ``restore_settings`` が初期値として戻す。``.lucianke.toml`` は読むだけで書かない。
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput
from lucianke.index.connection import db_path_for, open_reader
from lucianke.index.ingest import upsert_workspace
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.tools.configure_workspace")

Action = Literal["status", "set_roots", "set_python_env", "set_excludes", "reindex"]
ReindexScope = Literal["all", "symbols", "git", "lsp"]

LSP_ROLES: tuple[str, ...] = ("types", "lint")
PYTHON_LANGUAGE = "python"


class ConfigureWorkspaceArgs(CommonArgs):
    action: Action = Field(
        default="status",
        description='"status" (default, read-only) | "set_roots" | "set_python_env" | "set_excludes" | "reindex".',
    )
    root_paths: list[str] | None = Field(
        default=None,
        description="set_roots: absolute paths (or relative to the active root). All must be existing directories, "
        "otherwise nothing changes. The first becomes the active workspace; the list replaces the registered roots.",
    )
    python_interpreter: str | None = Field(
        default=None, description="set_python_env: a Python executable or a virtual environment directory."
    )
    exclude_globs: list[str] | None = Field(
        default=None,
        description="set_excludes: globs excluded in addition to .gitignore. Replaces the previous list ([] clears).",
    )
    default_base_ref: str | None = Field(
        default=None,
        description='Default comparison base for change-impact tools (default "HEAD"). Applied with set_roots / '
        "set_python_env / set_excludes.",
    )
    reindex_scope: ReindexScope = Field(
        default="all", description='reindex: "all" | "symbols" | "git" | "lsp".'
    )
    job_id: str | None = Field(default=None, description="status: only this job in jobs[].")


# --- 時刻・小道具 ---------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _invalid(param: str, message: str, reason: str) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, message, details={"param": param, "reason": reason})


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


# --- ジョブ盤（インデックス以外のジョブ: LSP の再起動など） ---------------------------------------


@dataclass(slots=True)
class _BoardJob:
    job_id: str
    kind: str
    workspace_id: str | None
    state: str = "queued"
    progress: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "workspace_id": self.workspace_id,
            "state": self.state,
            "progress": self.progress,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class JobBoard:
    """``Indexer`` のジョブ（``job_NNNN``）以外のジョブ（``job_xNNNN``）を持つ。セッション限定の対応表（ADR-16）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, _BoardJob] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._seq = itertools.count(1)

    def submit(self, kind: str, workspace_id: str | None, work: Callable[[], Awaitable[None]]) -> str:
        """``work`` を背景タスクで走らせて ``job_id`` を返す（すぐ戻る）。イベントループの中から呼ぶこと。"""
        job = _BoardJob(job_id=f"job_x{next(self._seq):04d}", kind=kind, workspace_id=workspace_id)
        self._jobs[job.job_id] = job
        task = asyncio.get_running_loop().create_task(self._run(job, work), name=f"lucianke-{job.job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job.job_id

    async def _run(self, job: _BoardJob, work: Callable[[], Awaitable[None]]) -> None:
        try:
            job.state = "running"
            job.started_at = _now()
            await work()
            job.progress = 100
            job.state = "done"
        except asyncio.CancelledError:
            job.state = "failed"
            job.error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001  ジョブの失敗は状態に出す
            _log.warning("job %s failed", job.job_id, exc_info=True)
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = _now()

    def views(self) -> list[dict[str, Any]]:
        """新しい順。"""
        return [j.view() for j in reversed(self._jobs.values())]

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return None if job is None else job.view()

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _board(hub: Any) -> JobBoard:
    board = getattr(hub, "job_board", None)
    if board is None:
        board = JobBoard()
        try:
            hub.job_board = board
        except AttributeError:  # slots の入れ物。ジョブは見えるが、次の呼び出しでは新しい盤になる
            _log.warning("hub has no job_board slot; job tracking is per call")
    return board  # type: ignore[no-any-return]


# --- 設定の永続（workspaces 行） ------------------------------------------------------------------


def persisted_row(ws: Workspace) -> dict[str, object]:
    """``workspaces`` 行に保存する項目（``Indexer._ensure_workspace`` と同じ形。``indexed_at`` は触らない）。"""
    return {
        "workspace_id": ws.workspace_id,
        "root_path": ws.root.as_posix(),
        "python_env": ws.python_env.interpreter if ws.python_env is not None else None,
        "default_base_ref": ws.default_base_ref,
        "exclude_globs_json": json.dumps(list(ws.exclude_globs), ensure_ascii=False),
    }


async def persist_workspaces(hub: Any, workspaces: Sequence[Workspace]) -> list[str]:
    """各ワークスペースの設定を、その束の ``IndexWriter`` 経由で ``workspaces`` 行へ。失敗は警告にして返す（例外にしない）。"""
    warnings: list[str] = []
    for ws in workspaces:
        runtime = hub.runtime(ws.workspace_id)
        writer = getattr(runtime, "writer", None)
        if writer is None:
            warnings.append(f"{ws.workspace_id}: settings were not saved (the index is not open)")
            continue
        row = persisted_row(ws)
        try:
            await writer.asubmit(
                lambda conn, row=row: upsert_workspace(conn, row),
                bumps_version=False,
                label="configure_workspace",
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("could not save settings of %s", ws.workspace_id, exc_info=True)
            warnings.append(f"{ws.workspace_id}: settings were not saved ({type(exc).__name__}: {exc})")
    return warnings


def load_known_roots(cache_root: Path | str) -> dict[str, str]:
    """``<cache_root>/*/paad.sqlite`` の ``workspaces`` 行から ``{root_path: workspace_id}``。``WorkspaceContext(known_roots=...)`` に渡す。

    ID を再起動をまたいで安定させる（URN の安定。設計書 §7.3.1 手順 2）。読めない DB は飛ばす。
    """
    known: dict[str, str] = {}
    root = Path(cache_root)
    if not root.is_dir():
        return known
    for path in sorted(root.glob("*/paad.sqlite")):
        try:
            conn = open_reader(path)
        except sqlite3.Error:
            continue
        try:
            for root_path, ws_id in conn.execute("SELECT root_path, workspace_id FROM workspaces"):
                known[str(root_path)] = str(ws_id)
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return known


def _read_stored(cache_root: Path | str, ws: Workspace) -> dict[str, Any] | None:
    path = db_path_for(cache_root, ws.root)
    if not path.is_file():
        return None
    try:
        conn = open_reader(path)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT python_env, default_base_ref, exclude_globs_json FROM workspaces WHERE root_path = ?",
            (ws.root.as_posix(),),
        ).fetchone()
    except sqlite3.Error:  # 古い・壊れた DB は無視（作り直される）
        return None
    finally:
        conn.close()
    return (
        None
        if row is None
        else {"python_env": row[0], "default_base_ref": row[1], "exclude_globs_json": row[2]}
    )


def restore_settings(context: Any, workspaces: Sequence[Workspace], cache_root: Path | str) -> list[str]:
    """保存済みの設定を、いま登録した ``workspaces`` の初期値として戻す。警告の列を返す。

    ``.lucianke.toml`` や自動検出から得た値と同じなら何もしない（保存された自動検出の結果を「ツールでの明示」に
    固めてしまわないため）。違うものだけを ``set_excludes`` / ``set_python_env`` / ``update`` で戻す
    （ツールでの明示として扱う。優先順位は ``WorkspaceContext.settings``）。
    """
    warnings: list[str] = []
    for ws in workspaces:
        stored = _read_stored(cache_root, ws)
        if stored is None:
            continue
        ws_id = ws.workspace_id
        try:
            globs = json.loads(stored["exclude_globs_json"] or "[]")
        except (TypeError, ValueError):
            globs = None
        if isinstance(globs, list) and all(isinstance(g, str) for g in globs):
            if tuple(globs) != ws.exclude_globs:
                context.set_excludes(ws_id, globs)
        else:
            warnings.append(f"{ws_id}: saved exclude_globs ignored (not an array of strings)")
        interpreter = stored["python_env"]
        current = ws.python_env.interpreter if ws.python_env is not None else None
        if interpreter and interpreter != current:
            try:
                context.set_python_env(ws_id, interpreter)
            except LuciankeError:
                warnings.append(
                    f"{ws_id}: saved python interpreter {interpreter!r} is gone; using auto-detection"
                )
        base = stored["default_base_ref"]
        if isinstance(base, str) and base and base != ws.default_base_ref:
            context.update(ws_id, default_base_ref=base)
    return warnings


# --- 状態の収集（読み取りだけ） -------------------------------------------------------------------


def _empty_cache() -> dict[str, Any]:
    return {"languages": [], "files": 0, "symbols": 0, "relations": 0, "last_sync_at": None}


def _read_cache(db_path: Path) -> dict[str, Any]:
    """言語別ファイル数とキャッシュ統計。DB が無い・読めないときは空。"""
    empty = _empty_cache()
    if not db_path.is_file():
        return empty
    try:
        conn = open_reader(db_path)
    except sqlite3.Error:
        return empty
    try:
        languages = [
            {"language": lang, "tier": int(tier), "file_count": int(n)}
            for lang, tier, n in conn.execute(
                "SELECT language, tier, count(*) FROM files GROUP BY language, tier ORDER BY count(*) DESC, language"
            )
        ]
        files = int(conn.execute("SELECT count(*) FROM files").fetchone()[0])
        symbols = int(conn.execute("SELECT count(*) FROM symbols WHERE is_external = 0").fetchone()[0])
        relations = int(conn.execute("SELECT count(*) FROM relations").fetchone()[0])
        row = conn.execute("SELECT max(indexed_at) FROM workspaces").fetchone()
        return {
            "languages": languages,
            "files": files,
            "symbols": symbols,
            "relations": relations,
            "last_sync_at": row[0] if row else None,
        }
    except sqlite3.Error:
        return empty
    finally:
        conn.close()


def _git_info(root: Path) -> dict[str, Any] | None:
    """``{branch, head_sha, dirty_files}``。Git のワーキングコピーでなければ ``None``。"""
    from lucianke.git.repo import GitRepo  # 遅延 import（dulwich は重い。status 以外では要らない）

    repo = GitRepo.open(root)
    if repo is None:
        return None
    head = repo.head_info()
    return {"branch": head.branch, "head_sha": head.head_sha, "dirty_files": head.dirty_count}


def _lsp_daemons(pool: Any, ws: Workspace, languages: Sequence[str]) -> list[dict[str, Any]]:
    """作られている LSP ハンドル + 言語ごとの standby（``status`` を引くだけ。起動はしない）。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for h in pool.handles(ws):
        st = h.snapshot()
        seen.add((h.language_id, h.role))
        out.append(_daemon(ws, h.language_id, h.role, st, getattr(h, "started_at", None)))
    for lang in languages:
        for role in LSP_ROLES:
            if (lang, role) in seen:
                continue
            st = pool.status(lang, ws, role)
            if str(st.status) != "n/a":
                out.append(_daemon(ws, lang, role, st, None))
    return out


def _daemon(ws: Workspace, language: str, role: str, st: Any, started_at: Any) -> dict[str, Any]:
    return {
        "workspace_id": ws.workspace_id,
        "language": language,
        "role": role,
        "server": st.server_name,
        "status": str(st.status),
        "progress": st.progress,
        "pid": st.pid,
        "started_at": started_at,
        "reason": st.reason,
    }


def _overall_lsp_status(daemons: Sequence[dict[str, Any]]) -> str:
    statuses = {d["status"] for d in daemons}
    for wanted in ("ready", "indexing", "standby", "unavailable"):
        if wanted in statuses:
            return wanted
    return "n/a"


def _jobs_of(hub: Any, workspaces: Sequence[Workspace]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for ws in workspaces:
        indexer = getattr(hub.runtime(ws.workspace_id), "indexer", None)
        if indexer is None:
            continue
        for js in indexer.jobs():
            jobs.append(
                {
                    "job_id": js.job_id,
                    "kind": js.kind,
                    "workspace_id": ws.workspace_id,
                    "state": js.state,
                    "progress": js.progress,
                    "started_at": js.started_at,
                    "finished_at": js.finished_at,
                    "error": js.error,
                }
            )
    jobs.extend(_board(hub).views())
    return jobs


# --- 表示 -----------------------------------------------------------------------------------------


def _python_text(ws: Workspace) -> str:
    env = ws.python_env
    if env is None:
        return "-"
    parts: list[str] = []
    if env.interpreter:
        try:
            shown = Path(env.interpreter).relative_to(ws.root).as_posix()
        except ValueError:
            shown = Path(env.interpreter).as_posix()
        parts.append(shown)
    else:
        parts.append("-")
    if env.version:
        parts.append(env.version)
    if env.manager:
        parts.append(f"(manager: {env.manager})")
    return " ".join(parts)


def _langs_text(languages: Sequence[dict[str, Any]]) -> str:
    return " ".join(f"{d['language']}(T{d['tier']}):{d['file_count']}" for d in languages) or "-"


def _git_text(git: dict[str, Any] | None) -> tuple[str, str]:
    if git is None:
        return "-", "-"
    branch = git["branch"] or ("(detached)" if git["head_sha"] else "-")
    return branch, str(git["dirty_files"])


def _lsp_text(daemons: Sequence[dict[str, Any]]) -> str:
    if not daemons:
        return "(none)"
    return " ".join(f"{d['server'] or d['language']}={d['status']}" for d in daemons)


def _jobs_text(jobs: Sequence[dict[str, Any]]) -> str:
    if not jobs:
        return "(none)"
    return "; ".join(f"{j['job_id']} {j['kind']} {j['state']} {j['progress']}%" for j in jobs)


def _entry(ws: Workspace, active_id: str | None) -> dict[str, Any]:
    env = ws.python_env
    return {
        "workspace_id": ws.workspace_id,
        "root_path": ws.root.as_posix(),
        "active": ws.workspace_id == active_id,
        "python_env": None
        if env is None
        else {"interpreter": env.interpreter, "version": env.version, "manager": env.manager},
        "exclude_globs": list(ws.exclude_globs),
        "default_base_ref": ws.default_base_ref,
        "sub_projects": [{"path": s.path, "marker": s.marker} for s in ws.sub_projects],
    }


async def _view(
    ctx: ToolContext,
    hub: Any,
    *,
    detail: bool,
    job_filter: str | None,
    started: Sequence[tuple[str, str]],
    warnings: list[str],
) -> ToolOutput:
    lines = ctx.services.lines  # type: ignore[attr-defined]
    wc = hub.context
    workspaces = wc.list()
    try:
        active_id: str | None = wc.active().workspace_id
    except LuciankeError:
        active_id = None

    entries = [_entry(ws, active_id) for ws in workspaces]
    cache_total = {"files": 0, "symbols": 0, "relations": 0, "last_sync_at": None}
    if detail:

        async def gather(ws: Workspace) -> tuple[dict[str, Any], dict[str, Any] | None]:
            runtime = hub.runtime(ws.workspace_id)
            db_path = getattr(runtime, "db_path", None)
            cache = (
                await asyncio.to_thread(_read_cache, Path(db_path)) if db_path is not None else _empty_cache()
            )
            try:
                git = await asyncio.to_thread(_git_info, ws.root)
            except Exception as exc:  # noqa: BLE001  Git が読めなくても status は答える
                warnings.append(f"{ws.workspace_id}: git state unavailable ({type(exc).__name__}: {exc})")
                git = None
            return cache, git

        results = await asyncio.gather(*(gather(ws) for ws in workspaces))
        for entry, (cache, git) in zip(entries, results, strict=True):
            entry["languages"] = cache["languages"]
            entry["git"] = git
            entry["cache"] = {k: cache[k] for k in ("files", "symbols", "relations", "last_sync_at")}
            for key in ("files", "symbols", "relations"):
                cache_total[key] += cache[key]
            syncs = [s for s in (cache_total["last_sync_at"], cache["last_sync_at"]) if s]
            cache_total["last_sync_at"] = max(syncs) if syncs else None

    daemons: list[dict[str, Any]] = []
    pool = getattr(hub, "lsp_pool", None)
    if pool is not None:
        for ws, entry in zip(workspaces, entries, strict=True):
            langs = [d["language"] for d in entry.get("languages", [])]
            daemons.extend(_lsp_daemons(pool, ws, langs))

    jobs = _jobs_of(hub, workspaces)
    if job_filter is not None:
        jobs = [j for j in jobs if j["job_id"] == job_filter]

    records: list[Record] = []
    first = ctx.cursor_offset
    for i, (ws, entry) in enumerate(zip(workspaces, entries, strict=True)):
        if detail:
            branch, dirty = _git_text(entry["git"])
            header = ["id", "root", "langs", "python", "branch", "dirty"]
            row = [
                ws.workspace_id,
                entry["root_path"],
                _langs_text(entry["languages"]),
                _python_text(ws),
                branch,
                dirty,
            ]
        else:
            header = ["id", "root", "python", "base_ref", "excludes"]
            row = [
                ws.workspace_id,
                entry["root_path"],
                _python_text(ws),
                ws.default_base_ref,
                len(ws.exclude_globs),
            ]
        records.append(
            Record(
                kind="row",
                key=f"workspace:{ws.workspace_id}",
                priority=i,
                lines=tuple(lines.table(([header] if i == first else []) + [row])),
                data=entry,
                confidence=Confidence.EXACT,
                group=ws.workspace_id,
            )
        )

    head: list[tuple[str, str]] = [
        ("workspaces", f"{len(workspaces)} (active: {active_id or '-'})"),
        ("lsp", _lsp_text(daemons)),
    ]
    if detail:
        head.append(
            (
                "cache",
                f"files={cache_total['files']} symbols={cache_total['symbols']} relations={cache_total['relations']}"
                f" synced={cache_total['last_sync_at'] or '-'}",
            )
        )
    head.append(("jobs", _jobs_text(jobs)))
    for ws_id, job_id in started:
        head.append(("job", f"{job_id} ({ws_id})"))

    static: dict[str, Any] = {
        "active_workspace_id": active_id,
        "lsp_daemons": daemons,
        "jobs": jobs,
        "job_id": started[0][1] if started else None,
        "job_ids": [{"workspace_id": w, "job_id": j} for w, j in started],
    }
    if detail:
        static["cache"] = {
            "files": cache_total["files"],
            "symbols": cache_total["symbols"],
            "relations": cache_total["relations"],
            "last_sync_at": cache_total["last_sync_at"],
        }
    if not workspaces:
        warnings.append("no workspace is registered; call configure_workspace with action set_roots")
    return ToolOutput(
        records=records,
        head_kv=head,
        data_static=static,
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(
            engine="workspace", confidence=Confidence.EXACT, lsp_status=_overall_lsp_status(daemons)
        ),
        page_unit="workspace",
        total_estimate=len(records),
        warnings=warnings,
        workspace_id=active_id,
    )


# --- アクション -----------------------------------------------------------------------------------


def _hub(ctx: ToolContext) -> Any:
    hub = getattr(ctx.services, "hub", None)
    if hub is None or getattr(hub, "context", None) is None:
        raise LuciankeError(
            ErrorCode.E_INTERNAL,
            "the workspace runtime is not wired into this server",
            details={"reason": "no_hub"},
        )
    return hub


def _check_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    if not ref.strip() or any(ord(c) < 32 for c in ref):
        raise _invalid("default_base_ref", "default_base_ref must be a non-empty revision name", "empty")
    return ref


async def _rebind(hub: Any, ws: Workspace, warnings: list[str]) -> None:
    """設定を変えた ``Workspace`` を束と LSP に渡し直す（``hub.workspace_changed``）。"""
    hook = getattr(hub, "workspace_changed", None)
    if hook is None:
        if hub.runtime(ws.workspace_id) is not None:
            warnings.append(
                f"{ws.workspace_id}: the running index keeps the previous settings until the server restarts "
                "(hub.workspace_changed is not wired)"
            )
        return
    await _maybe_await(hook(ws))


async def _set_roots(
    hub: Any, args: ConfigureWorkspaceArgs, started: list[tuple[str, str]], warnings: list[str]
) -> None:
    if not args.root_paths:
        raise _invalid(
            "root_paths", "set_roots needs root_paths (a non-empty array of directory paths)", "missing"
        )
    base_ref = _check_ref(args.default_base_ref)
    wc = hub.context
    before = {ws.workspace_id for ws in wc.list()}
    registered = wc.register_roots(
        args.root_paths, replace=True
    )  # 1 つでも不正なら何も変えずに E_INVALID_ARGUMENT
    new = [ws for ws in registered if ws.workspace_id not in before]
    warnings.extend(restore_settings(wc, new, hub.cache_root))
    if base_ref is not None:
        for ws in registered:
            wc.update(ws.workspace_id, default_base_ref=base_ref)
    registered = [wc.get(ws.workspace_id) for ws in registered]

    await hub.sync_roots([os.fspath(ws.root) for ws in registered])

    new_ids = {ws.workspace_id for ws in new}
    for ws in registered:
        runtime = hub.runtime(ws.workspace_id)
        if runtime is None:
            reason = getattr(hub, "failed", {}).get(ws.workspace_id)
            warnings.append(
                f"{ws.workspace_id}: the index could not be opened" + (f" ({reason})" if reason else "")
            )
            continue
        job_id = getattr(runtime, "scan_job_id", None)
        if ws.workspace_id in new_ids and job_id:
            started.append((ws.workspace_id, job_id))
        elif ws.workspace_id not in new_ids and base_ref is not None:
            await _rebind(hub, ws, warnings)
    kept = sorted(before & {ws.workspace_id for ws in registered})
    if kept:
        warnings.append(f"already registered: {', '.join(kept)}")
    warnings.extend(await persist_workspaces(hub, registered))


async def _set_python_env(
    hub: Any, args: ConfigureWorkspaceArgs, started: list[tuple[str, str]], warnings: list[str]
) -> None:
    if not args.python_interpreter:
        raise _invalid(
            "python_interpreter",
            "set_python_env needs python_interpreter (an executable or a venv directory)",
            "missing",
        )
    base_ref = _check_ref(args.default_base_ref)
    wc = hub.context
    ws = wc.resolve(args.workspace_id)
    new_ws = wc.set_python_env(
        ws.workspace_id, args.python_interpreter
    )  # 実行できなければ E_INVALID_ARGUMENT（変更なし）
    if base_ref is not None:
        new_ws = wc.update(ws.workspace_id, default_base_ref=base_ref)
    await _rebind(hub, new_ws, warnings)

    pool = getattr(hub, "lsp_pool", None)

    async def restart() -> None:
        if pool is None:
            return
        for handle in pool.handles(new_ws, PYTHON_LANGUAGE):
            handle.ws = new_ws  # 起動時の Workspace を持ち続けるので、新しい環境で上がるよう差し替える
        await pool.reset(PYTHON_LANGUAGE, new_ws)
        await pool.ensure_started(PYTHON_LANGUAGE, new_ws)

    if pool is None:
        warnings.append(f"{ws.workspace_id}: no LSP pool is running; nothing to restart")
    started.append((ws.workspace_id, _board(hub).submit("restart_python_lsp", ws.workspace_id, restart)))
    warnings.extend(await persist_workspaces(hub, [new_ws]))


async def _set_excludes(
    hub: Any, args: ConfigureWorkspaceArgs, started: list[tuple[str, str]], warnings: list[str]
) -> None:
    globs = args.exclude_globs
    if globs is None:
        raise _invalid("exclude_globs", "set_excludes needs exclude_globs (use [] to clear)", "missing")
    if any(not g.strip() or "\x00" in g for g in globs):
        raise _invalid("exclude_globs", "exclude_globs must be non-empty strings without NUL", "invalid_glob")
    base_ref = _check_ref(args.default_base_ref)
    wc = hub.context
    ws = wc.resolve(args.workspace_id)
    new_ws = wc.set_excludes(ws.workspace_id, globs)
    if base_ref is not None:
        new_ws = wc.update(ws.workspace_id, default_base_ref=base_ref)
    await _rebind(hub, new_ws, warnings)
    # 一致するファイルを files から除くのは再走査（scan は DB にあって列挙されないファイルを消す）
    started.append((ws.workspace_id, _submit_scan(hub, ws.workspace_id)))
    warnings.extend(await persist_workspaces(hub, [new_ws]))


def _submit_scan(hub: Any, workspace_id: str) -> str:
    indexer = getattr(hub.runtime(workspace_id), "indexer", None)
    if indexer is None:
        raise LuciankeError(
            ErrorCode.E_INTERNAL,
            "the reindex job could not be submitted (the index is not open)",
            details={"reason": "index_not_open", "workspace_id": workspace_id},
        )
    try:
        return str(indexer.start_scan_job())
    except Exception as exc:  # noqa: BLE001
        _log.warning("could not submit the scan job", exc_info=True)
        raise LuciankeError(
            ErrorCode.E_INTERNAL,
            f"the reindex job could not be submitted ({type(exc).__name__})",
            details={"reason": "submit_failed", "workspace_id": workspace_id},
        ) from None


async def _reindex(
    hub: Any, args: ConfigureWorkspaceArgs, started: list[tuple[str, str]], warnings: list[str]
) -> None:
    ws = hub.context.resolve(args.workspace_id)
    scope = args.reindex_scope
    if scope in ("all", "symbols"):
        started.append((ws.workspace_id, _submit_scan(hub, ws.workspace_id)))
        return
    pool = getattr(hub, "lsp_pool", None)
    if scope == "lsp":

        async def reset_lsp() -> None:
            if pool is not None:
                await pool.reset(None, ws)

        if pool is None:
            warnings.append(f"{ws.workspace_id}: no LSP pool is running; nothing to reset")
        started.append((ws.workspace_id, _board(hub).submit("reindex:lsp", ws.workspace_id, reset_lsp)))
        return

    async def nothing() -> None:
        return None

    warnings.append(
        "git history is not indexed in M1 (T-17 and T-18 are M2); the git scope has nothing to do"
    )
    started.append((ws.workspace_id, _board(hub).submit("reindex:git", ws.workspace_id, nothing)))


async def handler(ctx: ToolContext, args: ConfigureWorkspaceArgs) -> ToolOutput:
    hub = _hub(ctx)
    wc = hub.context
    if args.workspace_id is not None:
        wc.get(args.workspace_id)  # 未登録なら E_WORKSPACE_NOT_CONFIGURED（details.known_workspaces[]）
    warnings: list[str] = []
    started: list[tuple[str, str]] = []
    action = args.action

    if action == "set_roots":
        await _set_roots(hub, args, started, warnings)
    elif action == "set_python_env":
        await _set_python_env(hub, args, started, warnings)
    elif action == "set_excludes":
        await _set_excludes(hub, args, started, warnings)
    elif action == "reindex":
        await _reindex(hub, args, started, warnings)
    elif args.default_base_ref is not None:
        warnings.append(
            "default_base_ref ignored (status is read-only; use set_roots, set_python_env or set_excludes)"
        )

    if args.job_id is not None:
        if action != "status":
            warnings.append("job_id ignored (only status reports a single job)")
        elif not any(j["job_id"] == args.job_id for j in _jobs_of(hub, wc.list())):
            raise LuciankeError(
                ErrorCode.E_WORKSPACE_NOT_CONFIGURED,
                f"unknown job_id: {args.job_id!r}",
                details={"job_id": args.job_id, "reason": "unknown_job"},
            )

    warnings.extend(wc.take_warnings())
    return await _view(
        ctx,
        hub,
        detail=action == "status",
        job_filter=args.job_id if action == "status" else None,
        started=started,
        warnings=warnings,
    )


SPEC = ToolSpec(
    name="configure_workspace",
    tid="T-00",
    args_model=ConfigureWorkspaceArgs,
    handler=handler,
    default_max_tokens=1500,
    description=(
        "Inspect and configure the analysis target: action status (default, read-only) reports registered workspaces "
        "(languages by tier, Python env, git branch/dirty), LSP daemons, cache size and background jobs; set_roots registers "
        "project roots (replaces the list; the first becomes active; nothing changes if any path is missing); set_python_env "
        "restarts the Python LSP on another interpreter; set_excludes replaces the extra exclude globs and rescans; reindex "
        "starts a background job and returns its job_id at once. Call it at session start and when switching targets."
    ),
    writes=False,
    max_confidence=Confidence.EXACT,
    milestone="M1",
    accepts_revision=False,
    page_unit="workspace",
    records_key="workspaces",
)
TOOL_SPEC = SPEC
