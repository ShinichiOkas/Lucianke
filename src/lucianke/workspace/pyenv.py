"""Python 環境の検出（設計書 §7.3.4、§9.5、仕様書 §11 T-00 処理規約 3）。

検出順（最初に見つかったものを採る）:

1. 明示指定（実行ファイル、または仮想環境ディレクトリ）。実行できなければ ``E_INVALID_ARGUMENT``。
2. ワークスペースの仮想環境（``WORKSPACE_VENV_NAMES`` の順。Windows は ``Scripts/python.exe``、POSIX は ``bin/python``）:
   ``<root>/.venv/`` → ``<root>/venv/`` → ``<root>/env/`` → ``<root>/.env/``。``.venv`` 以外は ``pyvenv.cfg`` があるときだけ
   （``venv`` は ``python -m venv venv``〈公式チュートリアルの既定〉の名前。``env`` は同名のソース、``.env`` は dotenv のファイルと
   見分けるため。BUG-0111）。``manager`` は仮想環境の隣に ``uv.lock`` があれば ``"uv"``、``poetry.lock`` があれば ``"poetry"``、
   どちらも無ければ ``"venv"``。
3. 環境変数 ``VIRTUAL_ENV``（サーバーを起動した環境）が **ワークスペースの根の中** の仮想環境（``pyvenv.cfg`` とインタプリタがある）を
   指すとき。根の外（Lucianke 自身の venv など）は見ない。インタプリタの無い 4. より先に見る（見つかったインタプリタを捨てない）。
4. ``poetry.lock`` があるが仮想環境が無い: M1 では検出だけ記録する（``interpreter=None, manager="poetry"``）。
   Poetry の仮想環境の置き場（``POETRY_VIRTUALENVS_PATH`` など）は見ない。
5. ``CONDA_PREFIX``（``manager="conda"``）。
6. PATH 上の ``python``（無ければ ``python3``。``manager="path"``）。

M1 では外部コマンドを実行しない。``version`` は ``pyvenv.cfg`` に書かれているときだけ入る。
``uv`` の管理環境は M1 では 2. の探索に任せる（``uv python find`` は呼ばない）。

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

import configparser
import dataclasses
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import sys
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.workspace.types import PythonEnv

__all__ = [
    "PYTEST_CONFIG_NAMES",
    "WORKSPACE_VENV_NAMES",
    "PytestImports",
    "detect_extra_paths",
    "detect_pytest_imports",
    "detect_python_env",
    "workspace_venv_dirs",
]

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


WORKSPACE_VENV_NAMES: tuple[str, ...] = (".venv", "venv", "env", ".env")
"""ワークスペースの根の直下で仮想環境として探す名前（この順。``.venv`` 以外は ``pyvenv.cfg`` があるときだけ。BUG-0111）。"""


def _has_pyvenv_cfg(venv_dir: str | os.PathLike[str]) -> bool:
    try:
        return os.path.isfile(os.path.join(venv_dir, "pyvenv.cfg"))
    except (OSError, ValueError):
        return False


def workspace_venv_dirs(root: Path | str) -> tuple[Path, ...]:
    """``root`` の直下の仮想環境らしいディレクトリ（``WORKSPACE_VENV_NAMES`` の順）。インタプリタの有無は見ない。

    ``.venv`` はディレクトリなら採る（今までどおり）。``venv`` / ``env`` / ``.env`` は ``pyvenv.cfg`` のあるディレクトリだけ。
    LSP の起動コマンドの探索（``lsp/servers.find_command``）も同じ候補を見る（ワークスペースが固定した basedpyright・ruff）。
    """
    root = Path(root)
    out: list[Path] = []
    for name in WORKSPACE_VENV_NAMES:
        path = root / name
        try:
            if path.is_dir() and (name == ".venv" or _has_pyvenv_cfg(path)):
                out.append(path)
        except OSError:
            continue
    return tuple(out)


def _manager_beside(venv_dir: Path, root: Path) -> str:
    """仮想環境の隣（親ディレクトリ。無ければ根）の lock ファイルから ``manager`` を決める。"""
    for base in dict.fromkeys((venv_dir.parent, root)):
        if (base / "uv.lock").is_file():
            return "uv"
        if (base / "poetry.lock").is_file():
            return "poetry"
    return "venv"


def _inside(path: str, root: Path) -> Path | None:
    """``path`` が ``root``（含む）の中なら、その絶対パス。外・解けないときは ``None``。"""
    try:
        target = Path(os.path.abspath(os.path.expanduser(path)))
        base = Path(os.path.abspath(root))
        for t, b in ((target, base), (target.resolve(), base.resolve())):
            if os.path.normcase(os.path.commonpath([t, b])) == os.path.normcase(str(b)):
                return t
    except (OSError, ValueError):
        return None
    return None


def _detect_interpreter(root: Path, explicit: str | None, env: Mapping[str, str]) -> PythonEnv:
    if explicit:
        return _explicit(root, explicit)

    for venv in workspace_venv_dirs(root):
        if interpreter := _venv_python(str(venv)):
            return PythonEnv(
                interpreter=interpreter, version=_venv_version(str(venv)), manager=_manager_beside(venv, root)
            )

    if (
        (active := env.get("VIRTUAL_ENV"))
        and (venv := _inside(active, root)) is not None
        and _has_pyvenv_cfg(venv)
        and (interpreter := _venv_python(str(venv)))
    ):
        return PythonEnv(
            interpreter=interpreter, version=_venv_version(str(venv)), manager=_manager_beside(venv, root)
        )

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


# --- pytest が足す import の根（BUG-0082） ------------------------------------------------

PYTEST_CONFIG_NAMES: tuple[str, ...] = (
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)
"""pytest が設定を探すファイルの名前（1 つのディレクトリの中での優先順。pytest 9 の ``CONFIG_LOADERS_BY_NAME`` と同じ）。"""

_IMPORT_MODES = frozenset({"prepend", "append", "importlib"})


@dataclasses.dataclass(frozen=True, slots=True)
class PytestImports:
    """pytest の設定のうち import の根に効くもの（``detect_pytest_imports``。BUG-0082）。

    * ``pythonpath``: ``pythonpath`` の項目をワークスペース相対（``"/"`` 区切り、``""`` = ルート）に直したもの（書いた順。
      ワークスペースの外を指すもの・絶対パスは捨てる）。
    * ``import_mode``: ``addopts`` の ``--import-mode``（``prepend`` / ``append`` / ``importlib``。既定 ``prepend``）。
    """

    pythonpath: tuple[str, ...] = ()
    import_mode: str = "prepend"

    def fingerprint(self) -> str:
        """内容の短い指紋（同じ設定なら同じ値。``engine/reresolve.py`` が設定の変化を見分けるのに使う）。"""
        text = json.dumps([list(self.pythonpath), self.import_mode], ensure_ascii=False)
        return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _ini_section(text: str, section: str) -> dict[str, str] | None:
    """INI 形式の ``section`` の鍵と値。節が無い・読めないときは ``None``。"""
    parser = configparser.ConfigParser(interpolation=None, strict=False, comment_prefixes=("#", ";"))
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section(section):
        return None
    return dict(parser.items(section, raw=True))


def _pytest_config(directory: Path) -> Mapping[str, Any] | None:
    """``directory`` の中で pytest が採る設定（``PYTEST_CONFIG_NAMES`` の順で最初に当たったファイルの pytest の表）。無ければ ``None``。"""
    for name in PYTEST_CONFIG_NAMES:
        text = _read_text(directory / name)
        if text is None:
            continue
        if name.endswith(".toml"):
            try:
                doc = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            if name != "pyproject.toml":
                return _table(doc, "pytest")  # pytest.toml は ``[pytest]`` が無くても設定ファイル
            if "pytest" not in _table(doc, "tool"):
                continue
            tool = _table(doc, "tool", "pytest")  # ``[tool.pytest]``（TOML の型のまま）か ``[tool.pytest.ini_options]``
            return {k: v for k, v in tool.items() if k != "ini_options"} or _table(tool, "ini_options")
        if name in ("pytest.ini", ".pytest.ini"):
            return _ini_section(text, "pytest") or {}  # 節が無くても設定ファイル
        section = _ini_section(text, "tool:pytest" if name == "setup.cfg" else "pytest")
        if section is not None:
            return section
    return None


def _words(value: Any) -> list[str]:
    """INI の値（空白区切りの文字列。複数行を含む）・TOML の配列を語の列にする。"""
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()
    if isinstance(value, (list, tuple)):
        return [w for v in value if isinstance(v, str) for w in _words(v)]
    return []


def _import_mode(addopts: Any) -> str:
    """``addopts`` の ``--import-mode=X`` / ``--import-mode X``（後のものが勝つ）。無ければ ``prepend``。"""
    mode = "prepend"
    words = _words(addopts)
    for i, word in enumerate(words):
        value = None
        if word.startswith("--import-mode="):
            value = word.partition("=")[2]
        elif word == "--import-mode" and i + 1 < len(words):
            value = words[i + 1]
        if value in _IMPORT_MODES:
            mode = value
    return mode


def detect_pytest_imports(root: Path | str, project: str = "") -> PytestImports:
    """``root`` の下のディレクトリ ``project``（ワークスペース相対。``""`` = ルート。テストフィクスチャのルートなど）に置かれた
    pytest の設定から、import の根に効くもの（``pythonpath``・``--import-mode``）を読む。設定が無い・読めないときは既定値。

    ``pythonpath`` は設定ファイルの置き場（``project``）からの相対。ワークスペースの外を指すもの・絶対パスは捨てる。
    """
    base = _rel(project) or ""
    config = _pytest_config(Path(root) / base if base else Path(root))
    if not config:
        return PytestImports()
    paths: list[str] = []
    for raw in _words(config.get("pythonpath")):
        text = raw.strip().replace("\\", "/")
        if not text or "\x00" in text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            continue
        norm = posixpath.normpath(posixpath.join(base, text))
        if norm == ".." or norm.startswith("../"):
            continue
        rel = "" if norm == "." else norm
        if rel not in paths:
            paths.append(rel)
    return PytestImports(tuple(paths), _import_mode(config.get("addopts")))
