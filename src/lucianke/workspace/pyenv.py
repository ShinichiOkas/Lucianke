"""Python 環境の検出（設計書 §7.3.4、§9.5、仕様書 §11 T-00 処理規約 3）。

検出順（最初に見つかったものを採る）:

1. 明示指定（実行ファイル、または仮想環境ディレクトリ）。実行できなければ ``E_INVALID_ARGUMENT``。
2. ``<root>/.venv/``（Windows は ``Scripts/python.exe``、POSIX は ``bin/python``）。``uv.lock`` が並んでいれば
   ``manager="uv"``、``poetry.lock`` が並んでいれば ``"poetry"``、どちらも無ければ ``"venv"``。
3. ``poetry.lock`` があるが ``.venv`` が無い: M1 では検出だけ記録する（``interpreter=None, manager="poetry"``）。
   Poetry の仮想環境の置き場（``POETRY_VIRTUALENVS_PATH`` など）は見ない。
4. ``CONDA_PREFIX``（``manager="conda"``）。
5. PATH 上の ``python``（無ければ ``python3``。``manager="path"``）。

M1 では外部コマンドを実行しない。``version`` は ``pyvenv.cfg`` に書かれているときだけ入る。
``uv`` の管理環境は M1 では ``.venv`` の探索に任せる（``uv python find`` は呼ばない）。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.workspace.types import PythonEnv

__all__ = ["detect_python_env"]

_IS_WINDOWS = sys.platform == "win32"
_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?")


def _is_exe(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except ValueError:  # NUL を含むパス
        return False


def _venv_python(venv_dir: str) -> str | None:
    """仮想環境ディレクトリの中のインタプリタ。Windows は ``Scripts/python.exe``、POSIX は ``bin/python``。"""
    rel = ("Scripts", "python.exe") if _IS_WINDOWS else ("bin", "python")
    candidate = os.path.join(venv_dir, *rel)
    return candidate if _is_exe(candidate) else None


def _prefix_python(prefix: str) -> str | None:
    """conda 環境のインタプリタ。Windows は ``<prefix>/python.exe``、POSIX は ``<prefix>/bin/python``。"""
    candidate = os.path.join(prefix, "python.exe") if _IS_WINDOWS else os.path.join(prefix, "bin", "python")
    return candidate if _is_exe(candidate) else None


def _venv_version(venv_dir: str) -> str | None:
    """``pyvenv.cfg`` の ``version`` / ``version_info`` から ``X.Y[.Z]`` を読む（実行はしない）。"""
    try:
        text = Path(venv_dir, "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("version", "version_info") and (m := _VERSION_RE.match(value.strip())):
            return m.group(0)
    return None


def _explicit(root: Path, explicit: str) -> PythonEnv:
    path = os.path.abspath(os.path.join(root, os.path.expanduser(explicit)))
    if os.path.isdir(path):
        interpreter = _venv_python(path) or _prefix_python(path)
        if interpreter:
            return PythonEnv(interpreter=interpreter, version=_venv_version(path), manager="explicit")
    elif _is_exe(path):
        return PythonEnv(interpreter=path, manager="explicit")
    raise LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT,
        f"python_interpreter is not an executable or a virtual environment directory: {explicit!r}",
        details={"param": "python_interpreter", "reason": "not_executable"},
    )


def detect_python_env(
    root: Path | str, explicit: str | None = None, *, env: Mapping[str, str] | None = None
) -> PythonEnv:
    """``root`` の Python 環境を検出する。何も見つからなければ ``PythonEnv(interpreter=None)``。

    ``explicit`` は絶対パス、または ``root`` からの相対パス。``env`` は環境変数（既定は ``os.environ``）。
    """
    root = Path(root)
    env = os.environ if env is None else env
    if explicit:
        return _explicit(root, explicit)

    venv = os.path.join(root, ".venv")
    if interpreter := _venv_python(venv):
        if (root / "uv.lock").is_file():
            manager = "uv"
        elif (root / "poetry.lock").is_file():
            manager = "poetry"
        else:
            manager = "venv"
        return PythonEnv(interpreter=interpreter, version=_venv_version(venv), manager=manager)

    if (root / "poetry.lock").is_file():
        return PythonEnv(interpreter=None, manager="poetry")

    if (prefix := env.get("CONDA_PREFIX")) and (interpreter := _prefix_python(prefix)):
        return PythonEnv(interpreter=interpreter, manager="conda")

    search = env.get("PATH", "")
    for name in ("python", "python3"):
        if found := shutil.which(name, path=search):
            return PythonEnv(interpreter=os.path.abspath(found), manager="path")
    return PythonEnv(interpreter=None)
