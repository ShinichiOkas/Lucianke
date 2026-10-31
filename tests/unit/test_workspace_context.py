"""workspace/context.py・pyenv.py・settings_file.py（設計書 §7.3.1、§7.3.4、§7.3.5、§9.3、仕様書 §11 T-00）。"""

from __future__ import annotations

import dataclasses
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from lucianke.core import limits
from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.workspace import context as context_module
from lucianke.workspace import pyenv, settings_file
from lucianke.workspace.context import SUBPROJECT_MAX_DEPTH, WorkspaceContext, detect_subprojects
from lucianke.workspace.pyenv import detect_python_env
from lucianke.workspace.settings_file import SETTINGS_FILENAME, env_settings, load_settings, merge
from lucianke.workspace.types import PythonEnv, SubProject

IS_WINDOWS = sys.platform == "win32"


def _touch_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _venv_exe(venv: Path, *, windows: bool = IS_WINDOWS) -> Path:
    return venv / "Scripts" / "python.exe" if windows else venv / "bin" / "python"


def _same(a: str | None, b: Path) -> bool:
    return a is not None and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# =========================================================================================
# settings_file
# =========================================================================================


def _write_settings(root: Path, text: str) -> None:
    (root / SETTINGS_FILENAME).write_text(text, encoding="utf-8")


def test_load_settings_missing_file_is_empty(tmp_path: Path):
    loaded = load_settings(tmp_path)
    assert loaded.values == {}
    assert loaded.warnings == ()


def test_load_settings_reads_every_known_key(tmp_path: Path):
    _write_settings(
        tmp_path,
        """
exclude_globs = ["build/**", "*.min.js"]
python_interpreter = ".venv"
test_globs = ["spec/**"]
co_change_window = 200
max_file_bytes = 1048576

[lsp.python]
cmd = ["pyright-langserver", "--stdio"]

[lsp.typescript]
cmd = "vtsls --stdio"
""",
    )
    loaded = load_settings(tmp_path)
    assert loaded.warnings == ()
    assert loaded.values == {
        "exclude_globs": ("build/**", "*.min.js"),
        "python_interpreter": ".venv",
        "test_globs": ("spec/**",),
        "co_change_window": 200,
        "max_file_bytes": 1048576,
        "lsp": {
            "python": {"cmd": ("pyright-langserver", "--stdio")},
            "typescript": {"cmd": ("vtsls", "--stdio")},
        },
    }


def test_load_settings_ignores_unknown_and_malformed_keys_with_warnings(tmp_path: Path):
    _write_settings(
        tmp_path,
        """
exclude_globs = ["ok/**"]
colour = "red"
co_change_window = -3
max_file_bytes = true
test_globs = "tests/**"
python_interpreter = ""

[lsp.python]
cmd = ["pyright", 3]
flavour = "x"
""",
    )
    loaded = load_settings(tmp_path)
    assert loaded.values == {"exclude_globs": ("ok/**",)}
    text = "\n".join(loaded.warnings)
    for needle in (
        "'colour'",
        "'co_change_window'",
        "'max_file_bytes'",
        "'test_globs'",
        "'python_interpreter'",
        "'lsp.python.cmd'",
        "'lsp.python.flavour'",
    ):
        assert needle in text, needle
    assert len(loaded.warnings) == 7


def test_load_settings_unparsable_file_is_ignored_with_one_warning(tmp_path: Path):
    _write_settings(tmp_path, "exclude_globs = [\n")
    loaded = load_settings(tmp_path)
    assert loaded.values == {}
    assert len(loaded.warnings) == 1
    assert SETTINGS_FILENAME in loaded.warnings[0]


def test_load_settings_accepts_utf8_bom(tmp_path: Path):
    (tmp_path / SETTINGS_FILENAME).write_bytes(b"\xef\xbb\xbfco_change_window = 7\n")
    assert load_settings(tmp_path).values == {"co_change_window": 7}


def test_load_settings_never_writes(tmp_path: Path):
    _write_settings(tmp_path, "co_change_window = 7\n")
    before = (tmp_path / SETTINGS_FILENAME).read_bytes()
    load_settings(tmp_path)
    assert (tmp_path / SETTINGS_FILENAME).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [SETTINGS_FILENAME]


def test_env_settings_reads_documented_variables():
    loaded = env_settings(
        {
            "LUCIANKE_MAX_FILE_BYTES": "2048",
            "LUCIANKE_DISABLE_LSP": "Rust, go",
            "LUCIANKE_LSP_PYTHON_CMD": "pyright-langserver --stdio",
            "LUCIANKE_CACHE_DIR": "/somewhere",  # ワークスペース設定ではない
            "PATH": "/bin",
        }
    )
    assert loaded.warnings == ()
    assert loaded.values == {
        "max_file_bytes": 2048,
        "disable_lsp": ("rust", "go"),
        "lsp": {"python": {"cmd": ("pyright-langserver", "--stdio")}},
    }


def test_env_settings_ignores_bad_values_with_warnings():
    loaded = env_settings({"LUCIANKE_MAX_FILE_BYTES": "lots", "LUCIANKE_LSP_GO_CMD": "'unclosed"})
    assert loaded.values == {}
    assert len(loaded.warnings) == 2
    assert env_settings({"LUCIANKE_MAX_FILE_BYTES": "  ", "LUCIANKE_DISABLE_LSP": ""}) == ({}, ())


def test_merge_priority_tool_over_env_over_file_over_detected():
    tool = {"max_file_bytes": 10}
    env = {"LUCIANKE_MAX_FILE_BYTES": "20"}
    file_values = {"max_file_bytes": 30, "co_change_window": 31, "python_interpreter": "from-file"}
    detected = {
        "max_file_bytes": 40,
        "co_change_window": 41,
        "python_interpreter": "detected",
        "test_globs": ["d/**"],
    }
    merged = merge(tool, env, file_values, detected)
    assert merged["max_file_bytes"] == 10  # ツール
    assert merge({}, env, file_values, detected)["max_file_bytes"] == 20  # 環境変数
    assert merge({}, {}, file_values, detected)["max_file_bytes"] == 30  # ファイル
    assert merge({}, {}, {}, detected)["max_file_bytes"] == 40  # 自動検出
    assert merged["co_change_window"] == 31  # ファイルが自動検出に勝つ
    assert merged["python_interpreter"] == "from-file"
    assert merged["test_globs"] == ("d/**",)  # 上の層に無ければ下の層


def test_merge_none_means_unset_and_defaults_fill_the_rest():
    merged = merge({"python_interpreter": None, "exclude_globs": None}, {}, {"python_interpreter": "x"})
    assert merged["python_interpreter"] == "x"
    assert merged["exclude_globs"] == ()
    assert merged["test_globs"] == ()
    assert merged["disable_lsp"] == ()
    assert merged["max_file_bytes"] == limits.MAX_FILE_BYTES
    assert merged["co_change_window"] == limits.CO_CHANGE_WINDOW
    assert "python_interpreter" not in merge(None, None, None)


def test_merge_normalizes_lists_to_tuples_and_does_not_concatenate_layers():
    merged = merge({"exclude_globs": ["a"]}, {}, {"exclude_globs": ("b", "c")})
    assert merged["exclude_globs"] == ("a",)  # 置換であって追記ではない


def test_merge_lsp_commands_per_language_and_item():
    file_values = {"lsp": {"python": {"cmd": ("file-py",)}, "go": {"cmd": ("file-go",)}}}
    env = {"LUCIANKE_LSP_PYTHON_CMD": "env-py --stdio"}
    tool = {"lsp": {"rust": {"cmd": ["tool-rs"]}}}
    merged = merge(tool, env, file_values)
    assert merged["lsp"] == {
        "python": {"cmd": ("env-py", "--stdio")},
        "go": {"cmd": ("file-go",)},
        "rust": {"cmd": ("tool-rs",)},
    }


def test_merge_does_not_mutate_inputs():
    file_values = {"exclude_globs": ("a",), "lsp": {"go": {"cmd": ("g",)}}}
    snapshot = repr(file_values)
    merge({"exclude_globs": ["z"]}, {"LUCIANKE_LSP_GO_CMD": "x"}, file_values)
    assert repr(file_values) == snapshot


def test_split_cmd_handles_quotes_and_keeps_windows_backslashes():
    assert settings_file._split_cmd('ls "two words" --stdio') == ("ls", "two words", "--stdio")
    if os.name == "nt":
        argv = settings_file._split_cmd(r'"C:\Program Files\x\ls.exe" --stdio')
        assert argv == (r"C:\Program Files\x\ls.exe", "--stdio")


# =========================================================================================
# pyenv
# =========================================================================================


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    return r


NO_ENV: dict[str, str] = {"PATH": ""}


def test_pyenv_venv_present_uses_platform_specific_interpreter(root: Path):
    exe = _touch_exe(_venv_exe(root / ".venv"))
    found = detect_python_env(root, None, env=NO_ENV)
    assert found.manager == "venv"
    assert _same(found.interpreter, exe)
    assert found.interpreter is not None
    assert found.interpreter.endswith("python.exe" if IS_WINDOWS else "bin" + os.sep + "python")
    assert found.version is None  # pyvenv.cfg が無ければ版は不明


@pytest.mark.parametrize("windows", [True, False])
def test_pyenv_venv_layout_follows_the_platform_flag(
    root: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
):
    monkeypatch.setattr(pyenv, "_IS_WINDOWS", windows)
    exe = _touch_exe(_venv_exe(root / ".venv", windows=windows))
    other = _venv_exe(root / ".venv", windows=not windows)
    assert not other.exists()
    found = detect_python_env(root, None, env=NO_ENV)
    assert _same(found.interpreter, exe)
    # 逆の形の配置は見ない
    exe.unlink()
    _touch_exe(other)
    assert detect_python_env(root, None, env=NO_ENV).interpreter is None


def test_pyenv_venv_dir_without_interpreter_is_not_a_venv(root: Path):
    (root / ".venv").mkdir()
    assert detect_python_env(root, None, env=NO_ENV) == PythonEnv(interpreter=None)


def test_pyenv_nothing_found_is_none(root: Path, tmp_path: Path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    assert detect_python_env(root, None, env={"PATH": str(empty)}) == PythonEnv(interpreter=None)


def test_pyenv_reads_version_from_pyvenv_cfg_without_running_anything(root: Path):
    _touch_exe(_venv_exe(root / ".venv"))
    (root / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12.3\n", encoding="utf-8"
    )
    assert detect_python_env(root, None, env=NO_ENV).version == "3.12.3"
    (root / ".venv" / "pyvenv.cfg").write_text("version_info = 3.11.9.final.0\n", encoding="utf-8")
    assert detect_python_env(root, None, env=NO_ENV).version == "3.11.9"


def test_pyenv_manager_label_follows_lock_file_next_to_venv(root: Path):
    _touch_exe(_venv_exe(root / ".venv"))
    (root / "poetry.lock").write_text("", encoding="utf-8")
    assert detect_python_env(root, None, env=NO_ENV).manager == "poetry"
    (root / "uv.lock").write_text("", encoding="utf-8")
    assert detect_python_env(root, None, env=NO_ENV).manager == "uv"


def test_pyenv_poetry_without_venv_is_recorded_but_not_resolved(root: Path):
    (root / "poetry.lock").write_text("", encoding="utf-8")
    assert detect_python_env(root, None, env=NO_ENV) == PythonEnv(interpreter=None, manager="poetry")


def test_pyenv_uv_lock_without_venv_falls_through_to_path(root: Path, tmp_path: Path):
    (root / "uv.lock").write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    exe = _touch_exe(bin_dir / ("python.exe" if IS_WINDOWS else "python"))
    found = detect_python_env(root, None, env={"PATH": str(bin_dir)})
    assert found.manager == "path"
    assert _same(found.interpreter, exe)


@pytest.mark.parametrize("windows", [True, False])
def test_pyenv_conda_prefix(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool):
    monkeypatch.setattr(pyenv, "_IS_WINDOWS", windows)
    prefix = tmp_path / "conda-env"
    exe = _touch_exe(prefix / "python.exe" if windows else prefix / "bin" / "python")
    found = detect_python_env(root, None, env={"CONDA_PREFIX": str(prefix), "PATH": ""})
    assert found.manager == "conda"
    assert _same(found.interpreter, exe)
    # インタプリタが無い CONDA_PREFIX は使わない
    assert (
        detect_python_env(root, None, env={"CONDA_PREFIX": str(tmp_path / "gone"), "PATH": ""}).interpreter
        is None
    )


def test_pyenv_priority_venv_over_conda_over_path(root: Path, tmp_path: Path):
    bin_dir = tmp_path / "bin"
    path_exe = _touch_exe(bin_dir / ("python.exe" if IS_WINDOWS else "python"))
    prefix = tmp_path / "conda-env"
    conda_exe = _touch_exe(prefix / "python.exe" if IS_WINDOWS else prefix / "bin" / "python")
    env = {"CONDA_PREFIX": str(prefix), "PATH": str(bin_dir)}
    assert _same(detect_python_env(root, None, env=env).interpreter, conda_exe)
    venv_exe = _touch_exe(_venv_exe(root / ".venv"))
    assert _same(detect_python_env(root, None, env=env).interpreter, venv_exe)
    (root / "poetry.lock").write_text("", encoding="utf-8")
    (venv_exe).unlink()
    # .venv が無く poetry.lock だけ: conda や PATH より poetry の記録が先
    assert detect_python_env(root, None, env=env) == PythonEnv(interpreter=None, manager="poetry")
    assert path_exe.exists()


def test_pyenv_path_prefers_python_then_python3(root: Path, tmp_path: Path):
    if IS_WINDOWS:
        pytest.skip("Windows には python3 の別名を探す慣習がない")
    bin_dir = tmp_path / "bin"
    py3 = _touch_exe(bin_dir / "python3")
    assert _same(detect_python_env(root, None, env={"PATH": str(bin_dir)}).interpreter, py3)
    py = _touch_exe(bin_dir / "python")
    assert _same(detect_python_env(root, None, env={"PATH": str(bin_dir)}).interpreter, py)


def test_pyenv_explicit_wins_over_venv(root: Path, tmp_path: Path):
    _touch_exe(_venv_exe(root / ".venv"))
    mine = _touch_exe(tmp_path / "mine" / ("py.exe" if IS_WINDOWS else "py"))
    found = detect_python_env(root, str(mine), env=NO_ENV)
    assert found.manager == "explicit"
    assert _same(found.interpreter, mine)


def test_pyenv_explicit_venv_directory_and_relative_path(root: Path):
    exe = _touch_exe(_venv_exe(root / "envs" / "x"))
    (root / "envs" / "x" / "pyvenv.cfg").write_text("version = 3.10.4\n", encoding="utf-8")
    for given in (str(root / "envs" / "x"), "envs/x"):
        found = detect_python_env(root, given, env=NO_ENV)
        assert found.manager == "explicit"
        assert _same(found.interpreter, exe)
        assert found.version == "3.10.4"


@pytest.mark.parametrize("given", ["no/such/python", "bad\x00path"])
def test_pyenv_explicit_not_executable_is_invalid_argument(root: Path, given: str):
    with pytest.raises(LuciankeError) as err:
        detect_python_env(root, given, env=NO_ENV)
    assert err.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert err.value.details["param"] == "python_interpreter"


def test_pyenv_explicit_plain_directory_and_non_executable_file_are_invalid(root: Path):
    (root / "plain").mkdir()
    for given in ("plain",):
        with pytest.raises(LuciankeError) as err:
            detect_python_env(root, given, env=NO_ENV)
        assert err.value.code is ErrorCode.E_INVALID_ARGUMENT
    if not IS_WINDOWS:  # Windows には実行ビットが無い
        data = root / "data.txt"
        data.write_text("x", encoding="utf-8")
        data.chmod(0o644)
        with pytest.raises(LuciankeError):
            detect_python_env(root, str(data), env=NO_ENV)


def test_pyenv_defaults_to_process_environment(root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    prefix = tmp_path / "conda-env"
    exe = _touch_exe(prefix / "python.exe" if IS_WINDOWS else prefix / "bin" / "python")
    monkeypatch.setenv("CONDA_PREFIX", str(prefix))
    assert _same(detect_python_env(root).interpreter, exe)


# --- BUG-0111: venv/・env/・.env/・VIRTUAL_ENV ----------------------------------------------


def _make_venv(venv: Path, *, windows: bool = IS_WINDOWS, cfg: bool = True, version: str = "3.12.4") -> Path:
    """偽の仮想環境（インタプリタ + 任意で ``pyvenv.cfg``）。インタプリタのパスを返す。"""
    exe = _touch_exe(_venv_exe(venv, windows=windows))
    if cfg:
        (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {version}\n", encoding="utf-8")
    return exe


def test_pyenv_venv_without_dot_is_detected(root: Path):
    exe = _make_venv(root / "venv")
    found = detect_python_env(root, None, env=NO_ENV)
    assert _same(found.interpreter, exe)
    assert (found.manager, found.version) == ("venv", "3.12.4")


def test_pyenv_dot_venv_wins_over_venv(root: Path):
    dot = _make_venv(root / ".venv", version="3.13.0")
    _make_venv(root / "venv")
    found = detect_python_env(root, None, env=NO_ENV)
    assert _same(found.interpreter, dot)
    assert found.version == "3.13.0"


def test_pyenv_order_is_venv_then_env_then_dot_env(root: Path):
    dot_env = _make_venv(root / ".env", version="3.10.1")
    assert _same(detect_python_env(root, None, env=NO_ENV).interpreter, dot_env)
    env = _make_venv(root / "env", version="3.11.2")
    assert _same(detect_python_env(root, None, env=NO_ENV).interpreter, env)
    venv = _make_venv(root / "venv")
    assert _same(detect_python_env(root, None, env=NO_ENV).interpreter, venv)
    assert [p.name for p in pyenv.workspace_venv_dirs(root)] == ["venv", "env", ".env"]


@pytest.mark.parametrize("name", ["venv", "env", ".env"])
def test_pyenv_named_dir_without_pyvenv_cfg_is_ignored(root: Path, name: str):
    _make_venv(root / name, cfg=False)  # 同名のソースのディレクトリなど
    assert detect_python_env(root, None, env=NO_ENV) == PythonEnv(interpreter=None)
    assert pyenv.workspace_venv_dirs(root) == ()


def test_pyenv_dotenv_file_is_ignored(root: Path):
    (root / ".env").write_text("API_KEY=x\n", encoding="utf-8")  # dotenv のファイル
    assert detect_python_env(root, None, env=NO_ENV) == PythonEnv(interpreter=None)
    assert pyenv.workspace_venv_dirs(root) == ()


def test_pyenv_venv_manager_label_follows_lock_files(root: Path):
    _make_venv(root / "venv")
    assert detect_python_env(root, None, env=NO_ENV).manager == "venv"
    (root / "poetry.lock").write_text("", encoding="utf-8")
    assert detect_python_env(root, None, env=NO_ENV).manager == "poetry"
    (root / "uv.lock").write_text("", encoding="utf-8")
    assert detect_python_env(root, None, env=NO_ENV).manager == "uv"


@pytest.mark.parametrize("windows", [True, False])
def test_pyenv_venv_without_dot_follows_the_platform_layout(
    root: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
):
    monkeypatch.setattr(pyenv, "_IS_WINDOWS", windows)
    exe = _make_venv(root / "venv", windows=windows)
    assert _same(detect_python_env(root, None, env=NO_ENV).interpreter, exe)
    exe.unlink()
    _touch_exe(_venv_exe(root / "venv", windows=not windows))  # 逆の形の配置は見ない
    assert detect_python_env(root, None, env=NO_ENV).interpreter is None


def test_pyenv_virtual_env_inside_root_is_used(root: Path):
    exe = _make_venv(root / "envs" / "py312")
    (root / "envs" / "uv.lock").write_text("", encoding="utf-8")
    found = detect_python_env(root, None, env={"PATH": "", "VIRTUAL_ENV": str(root / "envs" / "py312")})
    assert _same(found.interpreter, exe)
    assert (found.manager, found.version) == ("uv", "3.12.4")


def test_pyenv_virtual_env_outside_root_is_ignored(root: Path, tmp_path: Path):
    _make_venv(tmp_path / "lucianke-own-venv")
    env = {"PATH": "", "VIRTUAL_ENV": str(tmp_path / "lucianke-own-venv")}
    assert detect_python_env(root, None, env=env) == PythonEnv(interpreter=None)
    # 根の名前で始まるだけの隣のディレクトリも外
    _make_venv(tmp_path / "proj-other")
    env["VIRTUAL_ENV"] = str(tmp_path / "proj-other")
    assert detect_python_env(root, None, env=env) == PythonEnv(interpreter=None)


def test_pyenv_virtual_env_needs_pyvenv_cfg_and_loses_to_workspace_venv(root: Path):
    _make_venv(root / "custom", cfg=False)
    env = {"PATH": "", "VIRTUAL_ENV": str(root / "custom")}
    assert detect_python_env(root, None, env=env).interpreter is None
    (root / "custom" / "pyvenv.cfg").write_text("version = 3.12.4\n", encoding="utf-8")
    assert detect_python_env(root, None, env=env).interpreter is not None
    venv = _make_venv(root / "venv")
    assert _same(detect_python_env(root, None, env=env).interpreter, venv)


def test_pyenv_virtual_env_wins_over_poetry_without_interpreter_and_conda(root: Path, tmp_path: Path):
    exe = _make_venv(root / "custom")
    (root / "poetry.lock").write_text("", encoding="utf-8")
    conda = tmp_path / "conda"
    _touch_exe(conda / "python.exe" if IS_WINDOWS else conda / "bin" / "python")
    env = {"PATH": "", "VIRTUAL_ENV": str(root / "custom"), "CONDA_PREFIX": str(conda)}
    found = detect_python_env(root, None, env=env)
    assert _same(found.interpreter, exe)
    assert found.manager == "poetry"


def test_pyenv_explicit_still_wins_over_venv_without_dot(root: Path, tmp_path: Path):
    _make_venv(root / "venv")
    other = _make_venv(tmp_path / "elsewhere")
    found = detect_python_env(root, str(tmp_path / "elsewhere"), env=NO_ENV)
    assert _same(found.interpreter, other)
    assert found.manager == "explicit"


# =========================================================================================
# detect_subprojects
# =========================================================================================


def _mk(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_detect_subprojects_finds_every_marker_with_posix_relative_paths(tmp_path: Path):
    _mk(tmp_path / "pyproject.toml")
    _mk(tmp_path / "uv.lock")
    (tmp_path / ".git").mkdir()
    _mk(tmp_path / "packages" / "web" / "package.json")
    _mk(tmp_path / "packages" / "core" / "Cargo.toml")
    _mk(tmp_path / "tools" / "cli" / "go.mod")
    _mk(tmp_path / "tools" / "cli" / "poetry.lock")
    _mk(tmp_path / "plain" / "readme.txt")
    assert detect_subprojects(tmp_path) == (
        SubProject(path=".", marker="pyproject.toml"),
        SubProject(path=".", marker="uv.lock"),
        SubProject(path=".", marker=".git"),
        SubProject(path="packages/core", marker="Cargo.toml"),
        SubProject(path="packages/web", marker="package.json"),
        SubProject(path="tools/cli", marker="poetry.lock"),
        SubProject(path="tools/cli", marker="go.mod"),
    )


def test_detect_subprojects_does_not_descend_into_venv_node_modules_or_git(tmp_path: Path):
    _mk(tmp_path / ".venv" / "lib" / "pyproject.toml")
    _mk(tmp_path / "node_modules" / "left-pad" / "package.json")
    _mk(tmp_path / ".git" / "modules" / "sub" / "go.mod")
    _mk(tmp_path / "app" / ".venv" / "x" / "package.json")
    _mk(tmp_path / "app" / "pyproject.toml")
    # ルートの .git は見つかるが、その中・.venv・node_modules の中のマーカーは見つからない
    assert detect_subprojects(tmp_path) == (
        SubProject(path=".", marker=".git"),
        SubProject(path="app", marker="pyproject.toml"),
    )


def test_detect_subprojects_depth_limit_is_six_below_the_root(tmp_path: Path):
    assert SUBPROJECT_MAX_DEPTH == 6
    inside = tmp_path.joinpath(*"abcdef")  # 深さ 6
    outside = tmp_path.joinpath(*"abcdefg")  # 深さ 7
    _mk(inside / "go.mod")
    _mk(outside / "Cargo.toml")
    assert detect_subprojects(tmp_path) == (SubProject(path="a/b/c/d/e/f", marker="go.mod"),)


def _make_dir_link(link: Path, target: Path) -> None:
    """ディレクトリへのリンクを作る。Windows ではシンボリックリンクの権限が無くてもジャンクションで代用する。"""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if not IS_WINDOWS:
            pytest.skip("symbolic links are not available")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_detect_subprojects_does_not_follow_directory_links(tmp_path: Path):
    outside = tmp_path / "outside"
    _mk(outside / "pyproject.toml")
    root = tmp_path / "root"
    root.mkdir()
    _make_dir_link(root / "link", outside)
    assert (root / "link" / "pyproject.toml").exists()  # リンクは機能している
    assert detect_subprojects(root) == ()


def test_detect_subprojects_on_a_missing_directory_is_empty(tmp_path: Path):
    assert detect_subprojects(tmp_path / "nope") == ()


# =========================================================================================
# WorkspaceContext.register_roots
# =========================================================================================

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"


@pytest.fixture
def ctx(tmp_path: Path) -> WorkspaceContext:
    return WorkspaceContext(cwd=tmp_path, env={"PATH": ""})


def _dir(tmp_path: Path, *parts: str) -> Path:
    d = tmp_path.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_register_two_roots_first_becomes_active(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "alpha"), _dir(tmp_path, "beta")
    got = ctx.register_roots([str(a), str(b)])
    assert [w.workspace_id for w in got] == ["alpha", "beta"]
    assert [w.root for w in ctx.list()] == [Path(os.path.realpath(a)), Path(os.path.realpath(b))]
    assert ctx.active().workspace_id == "alpha"
    assert ctx.get("beta") is got[1]
    assert all(isinstance(w.case_insensitive, bool) for w in got)
    assert all(w.python_env is not None for w in got)


def test_register_resolves_to_real_path_through_dotdot_and_links(ctx: WorkspaceContext, tmp_path: Path):
    real = _dir(tmp_path, "real")
    _dir(tmp_path, "other")
    (ws,) = ctx.register_roots([str(tmp_path / "other" / ".." / "real")])
    assert ws.root == Path(os.path.realpath(real))
    link = tmp_path / "lnk"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        return  # リンクが作れない環境では、上の .. の検査だけで足りる
    again = WorkspaceContext(cwd=tmp_path, env={"PATH": ""}).register_roots([str(link)])[0]
    assert again.root == ws.root


def test_register_ids_use_the_workspace_id_character_set(ctx: WorkspaceContext, tmp_path: Path):
    names = ["my proj (1)", "日本語", "ok-name_1.2", "a#b"]
    got = ctx.register_roots([str(_dir(tmp_path, n)) for n in names])
    for ws in got:
        assert re.fullmatch(r"[A-Za-z0-9._-]+", ws.workspace_id), ws.workspace_id
    assert got[2].workspace_id == "ok-name_1.2"
    assert len({w.workspace_id for w in got}) == len(names)


def test_register_colliding_names_get_numeric_suffixes(ctx: WorkspaceContext, tmp_path: Path):
    paths = [_dir(tmp_path, d, "app") for d in ("one", "two", "three")]
    got = ctx.register_roots([str(p) for p in paths])
    assert [w.workspace_id for w in got] == ["app", "app-2", "app-3"]
    # 別の呼び出しでも衝突を避ける
    more = ctx.register_roots([str(_dir(tmp_path, "four", "app"))])
    assert more[0].workspace_id == "app-4"
    # 実在する "app-2" という名前のディレクトリとも衝突しない
    clash = WorkspaceContext(cwd=tmp_path, env={"PATH": ""})
    named = str(_dir(tmp_path, "x", "app-2"))
    ids = [w.workspace_id for w in clash.register_roots([str(paths[0]), named, str(paths[1])])]
    assert ids == ["app", "app-2", "app-3"]


def test_register_probes_whether_the_filesystem_ignores_case(ctx: WorkspaceContext, tmp_path: Path):
    probe = _mk(tmp_path / "CaseProbe.tmp")
    actually_insensitive = (tmp_path / "caseprobe.tmp").exists()
    probe.unlink()
    (ws,) = ctx.register_roots([str(_dir(tmp_path, "MixedCase"))])
    assert ws.case_insensitive is actually_insensitive


def test_register_never_hands_out_the_external_namespace_id(ctx: WorkspaceContext, tmp_path: Path):
    (ws,) = ctx.register_roots([str(_dir(tmp_path, "_ext"))])
    assert ws.workspace_id == "_ext-2"


def test_register_same_root_twice_keeps_id_and_does_not_duplicate(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "a", "app"), _dir(tmp_path, "b", "app")
    first = ctx.register_roots([str(a)])[0]
    ctx.register_roots([str(b)])
    again = ctx.register_roots([str(a)])[0]
    assert again is first
    assert [w.workspace_id for w in ctx.list()] == ["app", "app-2"]
    assert ctx.active() is first  # 呼び出しの最初のルートがアクティブ


def test_register_duplicate_paths_in_one_call_collapse(ctx: WorkspaceContext, tmp_path: Path):
    a = _dir(tmp_path, "a")
    got = ctx.register_roots([str(a), str(a / "."), str(a)])
    assert len(got) == 1
    assert len(ctx.list()) == 1


def test_register_reuses_ids_from_a_previous_session(tmp_path: Path):
    a, b = _dir(tmp_path, "a", "app"), _dir(tmp_path, "b", "app")
    known = {str(Path(os.path.realpath(b))): "app", str(Path(os.path.realpath(a))): "app-2"}
    fresh = WorkspaceContext(cwd=tmp_path, env={"PATH": ""}, known_roots=known)
    got = fresh.register_roots([str(a), str(b)])
    assert [w.workspace_id for w in got] == ["app-2", "app"]
    # 前回の ID は、今回登録していない別のルートにも渡さない
    solo = WorkspaceContext(cwd=tmp_path, env={"PATH": ""}, known_roots=known)
    (c,) = solo.register_roots([str(_dir(tmp_path, "c", "app"))])
    assert c.workspace_id == "app-3"


def test_register_with_a_missing_path_changes_nothing(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    ctx.register_roots([str(a)])
    before_list, before_active = ctx.list(), ctx.active()
    some_file = _mk(tmp_path / "file.txt")
    for bad in (str(tmp_path / "missing"), str(some_file), "", "nul\x00byte"):
        with pytest.raises(LuciankeError) as err:
            ctx.register_roots([str(b), bad])
        assert err.value.code is ErrorCode.E_INVALID_ARGUMENT
        assert err.value.details["param"] == "root_paths"
        assert ctx.list() == before_list
        assert ctx.active() is before_active
    assert [w.workspace_id for w in ctx.list()] == ["a"]
    # 失敗した呼び出しは ID を使い込んでいない
    assert ctx.register_roots([str(b)])[0].workspace_id == "b"


@pytest.mark.parametrize("bad", [[], "not-a-list", None])
def test_register_requires_a_non_empty_list(ctx: WorkspaceContext, bad):
    with pytest.raises(LuciankeError) as err:
        ctx.register_roots(bad)
    assert err.value.code is ErrorCode.E_INVALID_ARGUMENT


def test_register_relative_path_is_relative_to_active_root_else_cwd(tmp_path: Path):
    cwd = _dir(tmp_path, "cwd")
    _dir(cwd, "sub")
    first = WorkspaceContext(cwd=cwd, env={"PATH": ""})
    (sub,) = first.register_roots(["sub"])  # アクティブが無いので cwd 基準
    assert sub.root == Path(os.path.realpath(cwd / "sub"))
    _dir(sub.root, "inner")
    (inner,) = first.register_roots(["inner"])  # アクティブ (sub) 基準
    assert inner.root == sub.root / "inner"


def test_register_replace_drops_others_but_remembers_ids(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "a", "app"), _dir(tmp_path, "b", "app")
    ctx.register_roots([str(a), str(b)])
    ctx.register_roots([str(b)], replace=True)
    assert [w.workspace_id for w in ctx.list()] == ["app-2"]
    assert ctx.active().workspace_id == "app-2"
    assert ctx.register_roots([str(a)])[0].workspace_id == "app"  # 外した ID を再び得る
    with pytest.raises(LuciankeError):
        ctx.get("missing")


def test_register_poly_fixture_detects_its_subproject():
    ctx = WorkspaceContext(env={"PATH": ""})
    (ws,) = ctx.register_roots([str(POLY)])
    assert ws.workspace_id == "poly"
    assert SubProject(path=".", marker="pyproject.toml") in ws.sub_projects
    assert all("/" not in sp.marker for sp in ws.sub_projects)


def test_register_records_python_env(ctx: WorkspaceContext, tmp_path: Path):
    root = _dir(tmp_path, "withvenv")
    exe = _touch_exe(_venv_exe(root / ".venv"))
    (ws,) = ctx.register_roots([str(root)])
    assert ws.python_env is not None
    assert ws.python_env.manager == "venv"
    assert _same(ws.python_env.interpreter, exe)


def test_register_applies_lucianke_toml_and_collects_warnings(ctx: WorkspaceContext, tmp_path: Path):
    root = _dir(tmp_path, "cfg")
    _write_settings(
        root,
        'exclude_globs = ["build/**"]\npython_interpreter = "does/not/exist"\nmystery = 1\nmax_file_bytes = 99\n',
    )
    (ws,) = ctx.register_roots([str(root)])
    assert ws.exclude_globs == ("build/**",)
    assert ctx.settings("cfg")["max_file_bytes"] == 99
    warnings = ctx.take_warnings()
    assert any("mystery" in w and w.startswith("cfg:") for w in warnings)
    assert any("python_interpreter" in w and "not executable" in w for w in warnings)
    assert ctx.take_warnings() == []  # 取り出したら空


def test_register_environment_variables_beat_the_settings_file(tmp_path: Path):
    root = _dir(tmp_path, "cfg")
    _write_settings(root, "max_file_bytes = 99\n")
    ctx = WorkspaceContext(cwd=tmp_path, env={"PATH": "", "LUCIANKE_MAX_FILE_BYTES": "7"})
    ctx.register_roots([str(root)])
    assert ctx.settings("cfg")["max_file_bytes"] == 7


# =========================================================================================
# WorkspaceContext.from_mcp_roots
# =========================================================================================


def test_from_mcp_roots_reads_file_uris(ctx: WorkspaceContext, tmp_path: Path):
    a = _dir(tmp_path, "with space")
    b = _dir(tmp_path, "日本")
    got = ctx.from_mcp_roots([a.as_uri(), b.as_uri()])
    assert [w.root for w in got] == [Path(os.path.realpath(a)), Path(os.path.realpath(b))]
    assert ctx.take_warnings() == []


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows のドライブ文字つき URI")
def test_from_mcp_roots_reads_percent_encoded_drive_letter(ctx: WorkspaceContext, tmp_path: Path):
    a = _dir(tmp_path, "drive")
    uri = "file:///" + str(a).replace("\\", "/").replace(":", "%3A", 1)
    (ws,) = ctx.from_mcp_roots([uri])
    assert ws.root == Path(os.path.realpath(a))


def test_from_mcp_roots_skips_unusable_entries_with_warnings(ctx: WorkspaceContext, tmp_path: Path):
    good = _dir(tmp_path, "good")
    got = ctx.from_mcp_roots(["https://example.com/x", (tmp_path / "gone").as_uri(), good.as_uri()])
    assert [w.workspace_id for w in got] == ["good"]
    assert len(ctx.take_warnings()) == 2


@pytest.mark.parametrize("uris", [None, [], ["https://example.com/x"]])
def test_from_mcp_roots_falls_back_to_cwd(tmp_path: Path, uris):
    cwd = _dir(tmp_path, "the-cwd")
    ctx = WorkspaceContext(cwd=cwd, env={"PATH": ""})
    (ws,) = ctx.from_mcp_roots(uris)
    assert ws.root == Path(os.path.realpath(cwd))
    assert ctx.active() is ws


# =========================================================================================
# WorkspaceContext.resolve
# =========================================================================================


def test_resolve_explicit_id(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    ctx.register_roots([str(a), str(b)])
    assert ctx.resolve("b").workspace_id == "b"
    assert ctx.resolve("b", path_hint=str(a / "x.py")).workspace_id == "b"  # ID が最優先


def test_resolve_unknown_id_lists_known_workspaces(ctx: WorkspaceContext, tmp_path: Path):
    ctx.register_roots([str(_dir(tmp_path, "a"))])
    with pytest.raises(LuciankeError) as err:
        ctx.resolve("nope")
    assert err.value.code is ErrorCode.E_WORKSPACE_NOT_CONFIGURED
    assert [k["workspace_id"] for k in err.value.details["known_workspaces"]] == ["a"]
    assert err.value.details["known_workspaces"][0]["root_path"].endswith("/a")


def test_resolve_by_path_prefers_containing_root_over_active(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    ctx.register_roots([str(a), str(b)])  # a がアクティブ
    assert ctx.resolve(path_hint=str(b / "src" / "new_file.py")).workspace_id == "b"  # 存在しないパスでもよい
    assert ctx.resolve(path_hint=str(a / "x.py")).workspace_id == "a"


def test_resolve_by_path_picks_the_deepest_nested_root(ctx: WorkspaceContext, tmp_path: Path):
    outer = _dir(tmp_path, "outer")
    inner = _dir(outer, "inner")
    ctx.register_roots([str(outer), str(inner)])
    assert ctx.resolve(path_hint=str(inner / "m.py")).workspace_id == "inner"
    assert ctx.resolve(path_hint=str(outer / "m.py")).workspace_id == "outer"


def test_resolve_relative_hint_is_relative_to_the_active_root(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    ctx.register_roots([str(a), str(b)])
    assert ctx.resolve(path_hint="src/x.py").workspace_id == "a"
    assert ctx.resolve(path_hint="../b/x.py").workspace_id == "b"


def test_resolve_falls_back_to_the_active_workspace(ctx: WorkspaceContext, tmp_path: Path):
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    ctx.register_roots([str(a), str(b)])
    assert ctx.resolve().workspace_id == "a"
    assert ctx.resolve(path_hint=str(tmp_path / "elsewhere" / "x.py")).workspace_id == "a"
    ctx.set_active("b")
    assert ctx.resolve().workspace_id == "b"
    assert ctx.active().workspace_id == "b"
    with pytest.raises(LuciankeError):
        ctx.set_active("nope")


def test_resolve_auto_registers_the_nearest_marker_directory(ctx: WorkspaceContext, tmp_path: Path):
    project = _dir(tmp_path, "proj")
    _mk(project / "pyproject.toml")
    _mk(project / "packages" / "web" / "package.json")
    target = (
        project / "packages" / "web" / "src" / "deep" / "index.ts"
    )  # ファイルも途中のディレクトリも存在しない
    ws = ctx.resolve(path_hint=str(target))
    assert ws.root == Path(os.path.realpath(project / "packages" / "web"))  # 最寄りのマーカー
    assert ws.workspace_id == "web"
    assert ctx.active() is ws
    assert ctx.list() == [ws]
    (warning,) = ctx.take_warnings()
    assert "auto-registered" in warning
    assert "package.json" in warning
    assert "web" in warning
    # 登録後は ①（パスを含むルート）で解決され、再び警告は出ない
    assert ctx.resolve(path_hint=str(target)) is ws
    assert ctx.take_warnings() == []


def test_resolve_marker_search_starts_at_an_existing_file(ctx: WorkspaceContext, tmp_path: Path):
    project = _dir(tmp_path, "proj2")
    _mk(project / ".git" / "HEAD", "ref: refs/heads/main\n")
    source = _mk(project / "src" / "mod.py")
    assert ctx.resolve(path_hint=str(source)).root == Path(os.path.realpath(project))


def test_resolve_with_nothing_raises_workspace_not_configured(
    ctx: WorkspaceContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with pytest.raises(LuciankeError) as err:
        ctx.resolve()
    assert err.value.code is ErrorCode.E_WORKSPACE_NOT_CONFIGURED
    assert err.value.details["known_workspaces"] == []
    # マーカーの無い場所（上位に偶然マーカーがあっても結果が変わらないよう、探索を差し替えて固定する）
    monkeypatch.setattr(context_module, "_nearest_marker_dir", lambda start: None)
    with pytest.raises(LuciankeError) as err2:
        ctx.resolve(path_hint=str(tmp_path / "no" / "markers" / "x.py"))
    assert err2.value.code is ErrorCode.E_WORKSPACE_NOT_CONFIGURED
    assert ctx.list() == []


def test_resolve_rejects_hints_with_control_characters(ctx: WorkspaceContext, tmp_path: Path):
    ctx.register_roots([str(_dir(tmp_path, "a"))])
    with pytest.raises(LuciankeError) as err:
        ctx.resolve(path_hint="bad\x00path")
    assert err.value.code is ErrorCode.E_INVALID_ARGUMENT


# =========================================================================================
# WorkspaceContext.set_excludes / update / set_python_env
# =========================================================================================


def test_set_excludes_replaces_and_swaps_the_workspace_object(ctx: WorkspaceContext, tmp_path: Path):
    root = _dir(tmp_path, "cfg")
    _write_settings(root, 'exclude_globs = ["from-file/**"]\n')
    (original,) = ctx.register_roots([str(root)])
    assert original.exclude_globs == ("from-file/**",)
    updated = ctx.set_excludes("cfg", ["a/**", "*.log"])
    assert updated.exclude_globs == ("a/**", "*.log")  # 置換（追記ではない）
    assert updated is not original
    assert ctx.get("cfg") is updated
    assert ctx.active() is updated
    assert original.exclude_globs == ("from-file/**",)  # 古いオブジェクトは不変
    assert ctx.settings("cfg")["exclude_globs"] == ("a/**", "*.log")  # ツールの指定がファイルに勝つ
    assert ctx.set_excludes("cfg", []).exclude_globs == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        updated.exclude_globs = ()  # type: ignore[misc]


def test_set_excludes_validates_input_and_id(ctx: WorkspaceContext, tmp_path: Path):
    ctx.register_roots([str(_dir(tmp_path, "a"))])
    for bad in ("a/**", ["ok", 3]):
        with pytest.raises(LuciankeError) as err:
            ctx.set_excludes("a", bad)  # type: ignore[arg-type]
        assert err.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ctx.get("a").exclude_globs == ()
    with pytest.raises(LuciankeError) as err2:
        ctx.set_excludes("nope", [])
    assert err2.value.code is ErrorCode.E_WORKSPACE_NOT_CONFIGURED


def test_update_swaps_fields_but_not_identity(ctx: WorkspaceContext, tmp_path: Path):
    (ws,) = ctx.register_roots([str(_dir(tmp_path, "a"))])
    assert ctx.update("a", default_base_ref="origin/main").default_base_ref == "origin/main"
    assert ctx.get("a").root == ws.root
    for forbidden in ({"workspace_id": "z"}, {"root": tmp_path}):
        with pytest.raises(ValueError, match="cannot be changed"):
            ctx.update("a", **forbidden)


def test_set_python_env_validates_before_changing(ctx: WorkspaceContext, tmp_path: Path):
    _dir(tmp_path, "a")
    ctx.register_roots([str(tmp_path / "a")])
    mine = _touch_exe(tmp_path / "mine" / ("py.exe" if IS_WINDOWS else "py"))
    updated = ctx.set_python_env("a", str(mine))
    assert updated.python_env is not None
    assert updated.python_env.manager == "explicit"
    assert _same(updated.python_env.interpreter, mine)
    with pytest.raises(LuciankeError) as err:
        ctx.set_python_env("a", str(tmp_path / "missing-python"))
    assert err.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ctx.get("a") is updated
    assert ctx.settings("a")["python_interpreter"] == str(mine)


def test_context_method_detect_subprojects_matches_the_function(ctx: WorkspaceContext, tmp_path: Path):
    _mk(tmp_path / "go.mod")
    assert ctx.detect_subprojects(tmp_path) == detect_subprojects(tmp_path)
