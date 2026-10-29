"""workspace/context.py・pyenv.py・settings_file.py（設計書 §7.3.1、§7.3.4、§7.3.5、§9.3、仕様書 §11 T-00）。"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from lucianke.core import limits
from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.workspace import pyenv, settings_file
from lucianke.workspace.pyenv import detect_python_env
from lucianke.workspace.settings_file import SETTINGS_FILENAME, env_settings, load_settings, merge
from lucianke.workspace.types import PythonEnv

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
