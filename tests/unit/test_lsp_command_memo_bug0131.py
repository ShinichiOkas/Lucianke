"""``LspPool.status`` が standby のときの「次に起こすサーバーの名前」の探索を覚える（BUG-0131 の残り）。

呼び出しのたびに ``servers.find_command`` が venv などを stat していた（約 0.3〜2 ms）。（ワークスペース・言語・役・探索の入力）ごとに
結果を覚え、捨てるのは次のとき: 監視の仮想環境・``node_modules`` のイベント（``Watcher`` の ``on_command_candidates_changed``。
除外される場所でも）・``reset`` / ``rebind``・``notify_watched``・保険の TTL。環境変数による上書きと Python 環境は鍵に入れる。
正しさ（venv を作った直後の ``status`` が古い名前を言い続けない）を、実ファイルと実際の ``watchfiles`` で押さえる。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.model import LspStatus, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.pool import COMMAND_NAME_TTL_S, FileChangeType, LspPool
from lucianke.lsp.servers import LspServerDef, servers_for
from lucianke.workspace.pyenv import touches_lsp_command_candidates
from lucianke.workspace.types import PythonEnv, Workspace
from lucianke.workspace.watcher import Watcher

DEFS = servers_for("python", "types")
FIRST, SECOND = DEFS[0].name, DEFS[1].name


def make_ws(root: Path, **kw: Any) -> Workspace:
    return Workspace(workspace_id="w", root=root, case_insensitive=False, **kw)


class FileResolver:
    """``<root>/.venv/<サーバー名>`` のファイルがあればそれが起動コマンド（実ファイルを stat する。呼んだ回数を数える）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, defn: LspServerDef, ws: Workspace) -> list[str] | None:
        self.calls += 1
        path = ws.root / ".venv" / defn.name
        return [str(path)] if path.is_file() else None


def install(root: Path, name: str) -> None:
    (root / ".venv").mkdir(exist_ok=True)
    (root / ".venv" / name).write_text("x")


def make_pool(**kw: Any) -> tuple[LspPool, FileResolver]:
    resolver = FileResolver()
    return LspPool(ProcessRegistry(), resolver=resolver, **kw), resolver


# --- 覚える ---------------------------------------------------------------------------------


def test_repeated_status_resolves_once(tmp_path: Path) -> None:
    pool, resolver = make_pool()
    ws = make_ws(tmp_path)
    assert pool.status("python", ws).server_name == FIRST
    first = resolver.calls
    assert first >= 1
    for _ in range(50):
        r = pool.status("python", ws)
        assert (r.status, r.server_name) == (LspStatus.STANDBY, FIRST)
    assert resolver.calls == first  # 2 回目からは探さない


def test_each_language_role_and_workspace_has_its_own_memo(tmp_path: Path) -> None:
    pool, resolver = make_pool()
    ws = make_ws(tmp_path)
    other = Workspace(workspace_id="w2", root=tmp_path, case_insensitive=False)
    pool.status("python", ws)
    n = resolver.calls
    pool.status("python", other)
    assert resolver.calls > n  # ワークスペースが違えば別
    n = resolver.calls
    pool.status("python", ws, "lint")
    assert resolver.calls > n  # 役が違えば別
    n = resolver.calls
    pool.status("python", ws)
    pool.status("python", ws, "lint")
    pool.status("python", other)
    assert resolver.calls == n


# --- 正しさ: 変わったら捨てる ----------------------------------------------------------------


def test_status_keeps_the_old_name_until_invalidated_then_sees_the_new_venv(tmp_path: Path) -> None:
    pool, _ = make_pool()
    ws = make_ws(tmp_path)
    assert pool.status("python", ws).server_name == FIRST  # どれも無い: 既定（先頭）
    install(tmp_path, SECOND)  # venv を作って 2 番目のサーバーを入れた
    pool.invalidate_commands()  # 監視のイベントが呼ぶ
    assert pool.status("python", ws).server_name == SECOND  # 作った直後の status が古い名前を言わない
    install(tmp_path, FIRST)
    pool.invalidate_commands()
    assert pool.status("python", ws).server_name == FIRST
    (tmp_path / ".venv" / FIRST).unlink()
    (tmp_path / ".venv" / SECOND).unlink()
    pool.invalidate_commands()
    assert pool.status("python", ws).server_name == FIRST  # どれも無くなったら既定に戻る


def test_an_invalidation_during_the_lookup_is_not_lost(tmp_path: Path) -> None:
    """引いている間に出来事が起きたら、その結果は覚えとして使わない（引く前の世代で書く）。"""
    ws = make_ws(tmp_path)
    calls: list[int] = []
    holder: dict[str, LspPool] = {}

    def resolver(defn: LspServerDef, w: Workspace) -> list[str] | None:
        calls.append(1)
        if len(calls) == 1:
            holder["pool"].invalidate_commands()  # 別スレッドの監視が、stat と覚えの間に割り込んだ
        return None

    pool = LspPool(ProcessRegistry(), resolver=resolver)
    holder["pool"] = pool
    pool.status("python", ws)
    n = len(calls)
    pool.status("python", ws)
    assert len(calls) > n  # 覚えていない: 引き直す
    n = len(calls)
    pool.status("python", ws)
    assert len(calls) == n  # 今度は覚える


def test_env_var_override_change_is_a_different_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pool, resolver = make_pool()
    ws = make_ws(tmp_path)
    for d in DEFS:
        monkeypatch.delenv(d.server_env_var, raising=False)
        monkeypatch.delenv(d.env_var, raising=False)
    pool.status("python", ws)
    n = resolver.calls
    pool.status("python", ws)
    assert resolver.calls == n
    monkeypatch.setenv(DEFS[0].server_env_var, "somewhere/else")
    pool.status("python", ws)
    assert resolver.calls > n  # 設定（環境変数）が変わったら引き直す
    n = resolver.calls
    monkeypatch.setenv("PATH", os.environ.get("PATH", "") + os.pathsep + str(tmp_path))
    pool.status("python", ws)
    assert resolver.calls > n  # PATH も探索の入力


def test_python_env_change_is_a_different_key(tmp_path: Path) -> None:
    pool, resolver = make_pool()
    pool.status("python", make_ws(tmp_path))
    n = resolver.calls
    pool.status("python", make_ws(tmp_path, python_env=PythonEnv(interpreter=str(tmp_path / "v" / "python"))))
    assert resolver.calls > n  # set_python_env の後の Workspace


async def test_reset_and_rebind_drop_the_memo(tmp_path: Path) -> None:
    pool, _ = make_pool()
    ws = make_ws(tmp_path)
    assert pool.status("python", ws).server_name == FIRST
    install(tmp_path, SECOND)
    assert pool.status("python", ws).server_name == FIRST  # 覚えている（変更の通知が無い）
    await pool.reset(None, ws)  # configure_workspace の reindex_scope="lsp"
    assert pool.status("python", ws).server_name == SECOND
    (tmp_path / ".venv" / SECOND).unlink()
    install(tmp_path, FIRST)
    assert pool.status("python", ws).server_name == SECOND
    await pool.rebind(ws)  # set_python_env など（workspace_changed）
    assert pool.status("python", ws).server_name == FIRST


async def test_notify_watched_with_a_venv_path_drops_the_memo(tmp_path: Path) -> None:
    pool, _ = make_pool()
    ws = make_ws(tmp_path)
    pool.status("python", ws)
    install(tmp_path, SECOND)
    await pool.notify_watched(ws, [(RelPath("src/a.py"), FileChangeType.CHANGED)])
    assert pool.status("python", ws).server_name == FIRST  # 関係の無い変更では捨てない
    await pool.notify_watched(ws, [(RelPath(f".venv/{SECOND}"), FileChangeType.CREATED)])
    assert pool.status("python", ws).server_name == SECOND


def test_the_ttl_is_a_safety_net_for_missed_events(tmp_path: Path) -> None:
    now = [100.0]
    pool, resolver = make_pool(clock=lambda: now[0])
    ws = make_ws(tmp_path)
    assert pool.status("python", ws).server_name == FIRST
    install(tmp_path, SECOND)  # イベントを取りこぼした（監視が動いていない）
    now[0] += COMMAND_NAME_TTL_S - 1
    assert pool.status("python", ws).server_name == FIRST
    now[0] += 2
    assert pool.status("python", ws).server_name == SECOND
    n = resolver.calls
    pool.status("python", ws)
    assert resolver.calls == n


def test_a_started_handle_does_not_use_the_memo(tmp_path: Path) -> None:
    """起こした後の名前は ``server_name``（実際に起こした定義）。覚えには触らない。"""
    pool, resolver = make_pool()
    ws = make_ws(tmp_path)
    pool.status("python", ws)
    h = pool._handle("python", ws, "types")
    h.server_name = SECOND
    from lucianke.lsp.pool import LspState

    h.state = LspState.READY
    n = resolver.calls
    assert pool.status("python", ws).server_name == SECOND
    assert resolver.calls == n


# --- 監視のイベント ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        (".venv", True),
        (".venv/pyvenv.cfg", True),
        (".venv/Scripts/basedpyright-langserver.exe", True),
        (".venv/bin/ruff", True),
        ("venv/pyvenv.cfg", True),
        ("env/Scripts", True),
        (".env", True),
        ("node_modules", True),
        ("node_modules/.bin", True),
        ("node_modules/.bin/typescript-language-server.cmd", True),
        ("node_modules/typescript/lib/tsserver.js", True),
        (".venv/Lib/site-packages/x/y.py", False),
        ("node_modules/left-pad/index.js", False),
        ("node_modules/typescript/lib/typescript.js/x", False),
        ("src/a.py", False),
        ("src/.venv/pyvenv.cfg", False),
        ("venvs/a", False),
        ("", False),
    ],
)
def test_touches_lsp_command_candidates(rel: str, expected: bool) -> None:
    assert touches_lsp_command_candidates(rel) is expected


def test_touches_lsp_command_candidates_folds_case_when_asked() -> None:
    assert touches_lsp_command_candidates(".VENV/Scripts/x.exe", fold=True)
    assert not touches_lsp_command_candidates(".VENV/Scripts/x.exe", fold=False)
    assert touches_lsp_command_candidates("Node_Modules/.BIN/x", fold=True)


class NullSink:
    async def changed(self, rel: RelPath) -> None: ...
    async def removed(self, rel: RelPath) -> None: ...
    async def rescan(self) -> None: ...
    def known_paths_under(self, prefix: str) -> list[RelPath]:
        return []


def test_watcher_filter_calls_the_hook_even_for_excluded_paths(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, exclude_globs=(".venv", "node_modules"))
    hits: list[int] = []
    w = Watcher(ws, NullSink(), on_command_candidates_changed=lambda: hits.append(1))
    sep = os.sep
    assert w._accept(None, f"{tmp_path}{sep}.venv{sep}Scripts{sep}x.exe") is False  # 除外は変わらない
    assert len(hits) == 1  # だが探索の覚えは捨てる
    assert w._accept(None, f"{tmp_path}{sep}src{sep}a.py") is True
    assert w._accept(None, f"{tmp_path}{sep}.venv{sep}Lib{sep}site-packages{sep}p{sep}m.py") is False
    assert len(hits) == 1  # site-packages の中は関係しない
    Watcher(ws, NullSink())._accept(None, f"{tmp_path}{sep}.venv")  # フックなしでも動く


async def test_creating_a_venv_under_a_real_watcher_changes_status(tmp_path: Path) -> None:
    """実際の ``watchfiles``: ``.venv`` を作ったら、除外される場所でも、次の ``status`` が新しい名前を言う。"""
    pool, _ = make_pool()
    ws = make_ws(tmp_path, exclude_globs=(".venv",))
    w = Watcher(ws, NullSink(), on_command_candidates_changed=pool.invalidate_commands)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    try:
        await asyncio.wait_for(w.started.wait(), 60)
        assert pool.status("python", ws).server_name == FIRST
        install(tmp_path, SECOND)
        deadline = asyncio.get_running_loop().time() + 60
        while pool.status("python", ws).server_name != SECOND:
            assert asyncio.get_running_loop().time() < deadline, "the venv event never reached the pool"
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, 60)
