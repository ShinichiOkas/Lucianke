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

import の根（``PythonEnv.extra_paths``）: src レイアウトのワークスペースは、``tests/`` などから ``src/`` の下のパッケージを
import 解決できない（pyright / basedpyright は ``extraPaths`` が無いと根だけを見る）。そこで次を決定的に検出して LSP の
``python.analysis.extraPaths`` に渡す（``detect_extra_paths``。インタープリタの検出とは独立で、どの経路でも入る）。

* ``pyproject.toml`` の宣言: ``[tool.setuptools] package-dir``・``[tool.setuptools.packages.find] where``・
  ``[tool.hatch.build(.targets.wheel)] packages / sources``・``[tool.poetry] packages`` の ``from``・
  ``[tool.pdm.build] package-dir``・``[tool.uv.build-backend] module-root``。
* 宣言が無くても、``src/<pkg>/__init__.py`` があり（``src/__init__.py`` が無く）、根直下に同名のパッケージ・モジュールが無ければ ``src``。
* **利用者の設定が先**: ``pyrightconfig.json`` または ``pyproject.toml`` の ``[tool.pyright]`` / ``[tool.basedpyright]`` が
  ``extraPaths`` か ``executionEnvironments`` を持つときは何も足さない。
"""

from __future__ import annotations

import dataclasses
import json
import os
import posixpath
import re
import shutil
import sys
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.workspace.types import PythonEnv

__all__ = ["detect_extra_paths", "detect_python_env"]

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
    ``extra_paths`` は、どの経路でも ``detect_extra_paths(root)``。
    """
    root = Path(root)
    found = _detect_interpreter(root, explicit, os.environ if env is None else env)
    return dataclasses.replace(found, extra_paths=detect_extra_paths(root))


def _detect_interpreter(root: Path, explicit: str | None, env: Mapping[str, str]) -> PythonEnv:
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


# --- import の根（src レイアウト） -------------------------------------------------------------

_USER_KEYS = ("extraPaths", "executionEnvironments")
# 文字列は温存し、// と /* */ のコメントと閉じ括弧の前の末尾のカンマを消す（pyrightconfig.json は JSONC）
_JSONC_NOISE = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/|,(?=\s*[}\]])', re.DOTALL)
_USER_KEY_TEXT = re.compile(r'"(?:extraPaths|executionEnvironments)"\s*:')


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _read_pyproject(root: Path) -> dict[str, Any]:
    text = _read_text(root / "pyproject.toml")
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def _table(parent: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    """入れ子の表を辿る。途中が無い・表でないときは空の表。"""
    node: Any = parent
    for key in keys:
        node = node.get(key) if isinstance(node, Mapping) else None
    return node if isinstance(node, Mapping) else {}


def _user_configured(root: Path, pyproject: Mapping[str, Any]) -> bool:
    """利用者の pyright 設定が import の根（``extraPaths`` / ``executionEnvironments``）を決めているか。"""
    text = _read_text(root / "pyrightconfig.json")
    if text is not None:
        try:
            data = json.loads(_JSONC_NOISE.sub(lambda m: m.group(1) or "", text))
        except ValueError:  # 読めない設定は、書いてあるかもしれないものとして触らない
            if _USER_KEY_TEXT.search(text):
                return True
        else:
            if isinstance(data, dict) and any(k in data for k in _USER_KEYS):
                return True
    tool = _table(pyproject, "tool")
    return any(any(k in _table(tool, name) for k in _USER_KEYS) for name in ("pyright", "basedpyright"))


def _strings(value: Any) -> list[str]:
    """文字列、または文字列の配列（表なら鍵）を文字列の配列にする。それ以外は空。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, Mapping)):
        return [v for v in value if isinstance(v, str)]
    return []


def _rel(raw: str) -> str | None:
    """宣言された相対ディレクトリを ``'/'`` 区切りに正規化する。根そのもの・絶対パス・根の外は ``None``。"""
    text = raw.strip().replace("\\", "/")
    if not text or "\x00" in text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    norm = posixpath.normpath(text)
    if norm in (".", "..") or norm.startswith("../"):
        return None
    return norm


def _parent(raw: str) -> str | None:
    """``src/shop`` → ``src``（パッケージのパスから import の根を取る）。"""
    rel = _rel(raw)
    return _rel(posixpath.dirname(rel)) if rel else None


def _declared_roots(pyproject: Mapping[str, Any]) -> Iterator[str]:
    """``pyproject.toml`` が宣言するパッケージの根の候補（存在はまだ確かめない）。"""
    tool = _table(pyproject, "tool")

    setuptools = _table(tool, "setuptools")
    for key, value in _table(setuptools, "package-dir").items():
        if not isinstance(value, str):
            continue
        if key == "":
            yield value
        elif (rel := _rel(value)) is not None:  # {"shop" = "src/shop"}: パスの末尾が名前なら、その親が根
            pkg = key.replace(".", "/")
            if rel == pkg or rel.endswith("/" + pkg):
                yield rel[: len(rel) - len(pkg)]
    yield from _strings(_table(setuptools, "packages", "find").get("where"))

    hatch_build = _table(tool, "hatch", "build")
    for table in (hatch_build, _table(hatch_build, "targets", "wheel")):
        for pkg in _strings(table.get("packages")):
            if (parent := _parent(pkg)) is not None:
                yield parent
        yield from _strings(table.get("sources"))

    for entry in _table(tool, "poetry").get("packages") or ():
        if isinstance(entry, Mapping) and isinstance(entry.get("from"), str):
            yield entry["from"]

    yield from _strings(_table(tool, "pdm", "build").get("package-dir"))
    yield from _strings(_table(tool, "uv", "build-backend").get("module-root"))


def _src_layout(root: Path) -> bool:
    """``src/<pkg>/__init__.py`` があり、``src`` 自身はパッケージでなく、根直下に同名のものが無い ``<pkg>`` がある。"""
    src = root / "src"
    try:
        if not src.is_dir() or (src / "__init__.py").exists():
            return False
        return any(
            child.is_dir()
            and (child / "__init__.py").is_file()
            and not (root / child.name / "__init__.py").exists()
            and not (root / (child.name + ".py")).exists()
            for child in src.iterdir()
        )
    except OSError:
        return False


def detect_extra_paths(root: Path | str) -> tuple[str, ...]:
    """LSP の ``python.analysis.extraPaths`` に足す、``root`` からの相対ディレクトリ（昇順・存在するものだけ）。

    利用者の pyright 設定が ``extraPaths`` / ``executionEnvironments`` を持つなら空（利用者が先）。
    それ以外は ``pyproject.toml`` の宣言と ``src/<pkg>/__init__.py`` の形から決める（モジュール docstring）。
    """
    root = Path(root)
    pyproject = _read_pyproject(root)
    if _user_configured(root, pyproject):
        return ()
    found = {rel for raw in _declared_roots(pyproject) if (rel := _rel(raw)) is not None}
    if _src_layout(root):
        found.add("src")
    out: list[str] = []
    for rel in sorted(found):
        try:
            if (root / rel).is_dir():
                out.append(rel)
        except OSError:
            continue
    return tuple(out)
