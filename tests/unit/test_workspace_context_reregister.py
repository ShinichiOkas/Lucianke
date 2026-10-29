"""登録済みルートの再登録で設定が落ちない（BUG-0021。設計書 §7.3.5、仕様書 §11 T-00 処理規約 5）。

``register_roots`` を同じルートに対してもう一度呼んでも（``replace`` の有無を問わず）、ツールでの明示
（``set_excludes``・``set_python_env``）と ``.lucianke.toml`` 由来の設定は保たれ、``settings()`` と ``Workspace`` の
値が食い違わない。``replace=True`` が消すのは **一覧から外れたルートの分だけ**。
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.workspace.context import WorkspaceContext
from lucianke.workspace.settings_file import SETTINGS_FILENAME

IS_WINDOWS = sys.platform == "win32"


@pytest.fixture
def ctx(tmp_path: Path) -> WorkspaceContext:
    return WorkspaceContext(cwd=tmp_path, env={"PATH": ""})


def _dir(tmp_path: Path, *parts: str) -> Path:
    d = tmp_path.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _assert_consistent(ctx: WorkspaceContext) -> None:
    """``settings()`` の除外 glob と ``Workspace`` の除外 glob が、登録済みの全ワークスペースで一致する。"""
    for ws in ctx.list():
        assert tuple(ctx.settings(ws.workspace_id)["exclude_globs"]) == ws.exclude_globs, ws.workspace_id


# --- 票の実測 ----------------------------------------------------------------------------


def test_replace_true_keeps_tool_excludes_for_a_registered_root(ctx: WorkspaceContext, tmp_path: Path):
    d = _dir(tmp_path, "app")
    (ws,) = ctx.register_roots([str(d)])
    ctx.set_excludes(ws.workspace_id, ["a/"])
    assert ctx.settings(ws.workspace_id)["exclude_globs"] == ("a/",)
    assert ctx.get(ws.workspace_id).exclude_globs == ("a/",)

    (again,) = ctx.register_roots([str(d)], replace=True)

    assert again.workspace_id == ws.workspace_id
    assert ctx.settings(ws.workspace_id)["exclude_globs"] == ("a/",)  # 旧: ()
    assert ctx.get(ws.workspace_id).exclude_globs == ("a/",)
    _assert_consistent(ctx)


def test_replace_false_second_registration_keeps_tool_excludes(ctx: WorkspaceContext, tmp_path: Path):
    d = _dir(tmp_path, "app")
    (ws,) = ctx.register_roots([str(d)])
    ctx.set_excludes(ws.workspace_id, ["a/"])
    ctx.register_roots([str(d)])
    assert ctx.settings(ws.workspace_id)["exclude_globs"] == ("a/",)
    _assert_consistent(ctx)


def test_repeated_registration_is_stable(ctx: WorkspaceContext, tmp_path: Path):
    """T-00 ``set_roots`` は ``register_roots(replace=True)`` のあと ``hub.sync_roots`` がもう一度呼ぶ。さらに ``list_changed`` も。"""
    d = _dir(tmp_path, "app")
    (ws,) = ctx.register_roots([str(d)])
    ctx.set_excludes(ws.workspace_id, ["a/", "*.log"])
    for replace in (True, True, False, True):
        (again,) = ctx.register_roots([str(d)], replace=replace)
        assert again is ctx.get(ws.workspace_id)  # 同じ ID・同じオブジェクト
        assert ctx.settings(ws.workspace_id)["exclude_globs"] == ("a/", "*.log")
        assert ctx.get(ws.workspace_id).exclude_globs == ("a/", "*.log")
    _assert_consistent(ctx)


def test_set_excludes_after_reregistration_still_replaces(ctx: WorkspaceContext, tmp_path: Path):
    d = _dir(tmp_path, "app")
    (ws,) = ctx.register_roots([str(d)])
    ctx.set_excludes(ws.workspace_id, ["a/"])
    ctx.register_roots([str(d)], replace=True)
    ctx.set_excludes(ws.workspace_id, ["b/"])  # 置換（追記ではない）
    assert ctx.settings(ws.workspace_id)["exclude_globs"] == ("b/",)
    assert ctx.get(ws.workspace_id).exclude_globs == ("b/",)
    _assert_consistent(ctx)


# --- 他の設定の層 ------------------------------------------------------------------------


def test_replace_true_keeps_the_python_interpreter_choice(ctx: WorkspaceContext, tmp_path: Path):
    d = _dir(tmp_path, "app")
    (ws,) = ctx.register_roots([str(d)])
    mine = _exe(tmp_path / "mine" / ("py.exe" if IS_WINDOWS else "py"))
    ctx.set_python_env(ws.workspace_id, str(mine))
    ctx.register_roots([str(d)], replace=True)
    assert ctx.settings(ws.workspace_id)["python_interpreter"] == str(mine)
    python_env = ctx.get(ws.workspace_id).python_env
    assert python_env is not None
    assert python_env.manager == "explicit"


def test_replace_true_keeps_values_from_lucianke_toml(ctx: WorkspaceContext, tmp_path: Path):
    d = _dir(tmp_path, "cfg")
    (d / SETTINGS_FILENAME).write_text(
        'exclude_globs = ["build/**"]\nmax_file_bytes = 99\n', encoding="utf-8"
    )
    ctx.register_roots([str(d)])
    assert ctx.settings("cfg")["max_file_bytes"] == 99

    ctx.register_roots([str(d)], replace=True)

    assert ctx.settings("cfg")["max_file_bytes"] == 99  # 旧: 既定値に戻る
    assert tuple(ctx.settings("cfg")["exclude_globs"]) == ("build/**",)
    assert ctx.get("cfg").exclude_globs == ("build/**",)
    _assert_consistent(ctx)


def test_tool_excludes_still_beat_the_file_after_reregistration(ctx: WorkspaceContext, tmp_path: Path):
    """優先順位（ツール > 環境変数 > ``.lucianke.toml``）が再登録のあとも保たれる。"""
    d = _dir(tmp_path, "cfg")
    (d / SETTINGS_FILENAME).write_text('exclude_globs = ["from-file/**"]\n', encoding="utf-8")
    ctx.register_roots([str(d)])
    ctx.set_excludes("cfg", ["from-tool/**"])
    ctx.register_roots([str(d)], replace=True)
    assert tuple(ctx.settings("cfg")["exclude_globs"]) == ("from-tool/**",)
    assert ctx.get("cfg").exclude_globs == ("from-tool/**",)


# --- replace が消すもの ------------------------------------------------------------------


def test_replace_keeps_each_remaining_root_own_settings(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    ctx.register_roots([str(a), str(b)])
    ctx.set_excludes("a", ["only-a/"])
    ctx.set_excludes("b", ["only-b/"])
    ctx.register_roots([str(b), str(a)], replace=True)  # 順序を変えても各ルートの設定は動かない
    assert ctx.settings("a")["exclude_globs"] == ("only-a/",)
    assert ctx.settings("b")["exclude_globs"] == ("only-b/",)
    assert ctx.active().workspace_id == "b"
    _assert_consistent(ctx)


def test_replace_adding_a_root_keeps_the_existing_one_and_starts_the_new_one_clean(
    ctx: WorkspaceContext, tmp_path: Path
):
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    ctx.register_roots([str(a)])
    ctx.set_excludes("a", ["only-a/"])
    ctx.register_roots([str(a), str(b)], replace=True)
    assert ctx.settings("a")["exclude_globs"] == ("only-a/",)
    assert ctx.settings("b")["exclude_globs"] == ()
    _assert_consistent(ctx)


def test_replace_drops_settings_of_roots_that_left_the_list(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "a", "app"), _dir(tmp_path, "b", "app")
    (wa, wb) = ctx.register_roots([str(a), str(b)])
    assert (wa.workspace_id, wb.workspace_id) == ("app", "app-2")
    ctx.set_excludes("app", ["gone/"])
    ctx.set_excludes("app-2", ["stay/"])

    ctx.register_roots([str(b)], replace=True)

    assert [w.workspace_id for w in ctx.list()] == ["app-2"]
    assert ctx.settings("app-2")["exclude_globs"] == ("stay/",)
    with pytest.raises(LuciankeError) as err:
        ctx.settings("app")  # 外れたルートは引けない
    assert err.value.code is ErrorCode.E_WORKSPACE_NOT_CONFIGURED

    # 外したルートを戻すと、同じ ID でまっさらな設定（前の明示は持ち越さない）
    (back,) = ctx.register_roots([str(a)])
    assert back.workspace_id == "app"
    assert ctx.settings("app")["exclude_globs"] == ()
    assert back.exclude_globs == ()
    assert ctx.settings("app-2")["exclude_globs"] == ("stay/",)
    _assert_consistent(ctx)


def test_replace_drop_then_readd_reloads_the_settings_file_not_old_state(
    ctx: WorkspaceContext, tmp_path: Path
):
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    (a / SETTINGS_FILENAME).write_text("max_file_bytes = 11\n", encoding="utf-8")
    ctx.register_roots([str(a), str(b)])
    ctx.register_roots([str(b)], replace=True)
    (a / SETTINGS_FILENAME).write_text("max_file_bytes = 22\n", encoding="utf-8")
    ctx.register_roots([str(a)])
    assert ctx.settings("a")["max_file_bytes"] == 22


def test_failed_registration_leaves_settings_untouched(ctx: WorkspaceContext, tmp_path: Path):
    d = _dir(tmp_path, "app")
    ctx.register_roots([str(d)])
    ctx.set_excludes("app", ["a/"])
    with pytest.raises(LuciankeError) as err:
        ctx.register_roots([str(d), str(tmp_path / "missing")], replace=True)
    assert err.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ctx.settings("app")["exclude_globs"] == ("a/",)
    assert ctx.get("app").exclude_globs == ("a/",)
    _assert_consistent(ctx)
