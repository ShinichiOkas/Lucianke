"""``.lucianke.toml`` の読み込みと設定の優先順位（設計書 §7.3.5、§9.3、仕様書 §12.7）。

``.lucianke.toml`` はルート直下に置き、**読むだけ**（MUST NOT 書く）。キーは次のとおり。

* ``exclude_globs`` / ``test_globs``: 文字列の配列。
* ``python_interpreter``: 文字列（実行ファイルまたは仮想環境ディレクトリ）。
* ``co_change_window`` / ``max_file_bytes``: 正の整数。
* ``lsp.<lang>.cmd``: 起動コマンド。文字列（空白で分割する）または文字列の配列。

未知のキー・型の違う値・解析できないファイルは **無視し**、人が読む警告の文字列を返す
（呼び出し側が T-00 ``status`` の ``warnings[]`` に出す）。

合成後の設定は次の形の ``dict``:

``{"exclude_globs": tuple[str, ...], "test_globs": tuple[str, ...], "python_interpreter": str,``
``"co_change_window": int, "max_file_bytes": int, "lsp": {lang: {"cmd": tuple[str, ...]}},``
``"disable_lsp": tuple[str, ...]}``

``python_interpreter`` は未設定なら入らない。優先順位は ``merge`` を参照。
"""

from __future__ import annotations

import os
import shlex
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from lucianke.core import limits

__all__ = ["SETTINGS_FILENAME", "Loaded", "env_settings", "load_settings", "merge"]

SETTINGS_FILENAME = ".lucianke.toml"

_LIST_KEYS = ("exclude_globs", "test_globs", "disable_lsp")
_SCALAR_KEYS = ("python_interpreter", "co_change_window", "max_file_bytes")


class Loaded(NamedTuple):
    """読み込み結果。``values`` は正規化済みの設定、``warnings`` は無視した項目の説明。"""

    values: dict[str, Any]
    warnings: tuple[str, ...]


# --- 値の検査 -----------------------------------------------------------------------------


def _split_cmd(text: str) -> tuple[str, ...]:
    """起動コマンドの文字列を argv にする。Windows ではバックスラッシュを escape と見なさない。"""
    if os.name == "nt":
        parts = shlex.split(text, posix=False)
        return tuple(p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts)
    return tuple(shlex.split(text))


def _cmd(value: object) -> tuple[str, ...] | None:
    if isinstance(value, str):
        try:
            argv = _split_cmd(value)
        except ValueError:
            return None
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        argv = tuple(value)
    else:
        return None
    return argv if argv and all(argv) else None


def _str_list(value: object) -> tuple[str, ...] | None:
    if isinstance(value, list) and all(isinstance(v, str) and v for v in value):
        return tuple(value)
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


# --- .lucianke.toml ----------------------------------------------------------------------


def load_settings(root: Path | str) -> Loaded:
    """``<root>/.lucianke.toml`` を読む。無ければ空。解析できなければ空と警告 1 件。"""
    path = Path(root) / SETTINGS_FILENAME
    if not path.is_file():
        return Loaded({}, ())
    try:
        raw = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return Loaded({}, (f"{SETTINGS_FILENAME}: ignored the whole file ({type(exc).__name__}: {exc})",))

    values: dict[str, Any] = {}
    warnings: list[str] = []

    def bad(key: str, expected: str) -> None:
        warnings.append(f"{SETTINGS_FILENAME}: ignored {key!r} (expected {expected})")

    for key, value in raw.items():
        if key in ("exclude_globs", "test_globs"):
            if (items := _str_list(value)) is None:
                bad(key, "an array of non-empty strings")
            else:
                values[key] = items
        elif key == "python_interpreter":
            if isinstance(value, str) and value:
                values[key] = value
            else:
                bad(key, "a non-empty string")
        elif key in ("co_change_window", "max_file_bytes"):
            if (number := _positive_int(value)) is None:
                bad(key, "a positive integer")
            else:
                values[key] = number
        elif key == "lsp":
            lsp = _load_lsp(value, warnings)
            if lsp:
                values["lsp"] = lsp
        else:
            warnings.append(f"{SETTINGS_FILENAME}: ignored unknown key {key!r}")
    return Loaded(values, tuple(warnings))


def _load_lsp(value: object, warnings: list[str]) -> dict[str, dict[str, tuple[str, ...]]]:
    out: dict[str, dict[str, tuple[str, ...]]] = {}
    if not isinstance(value, dict):
        warnings.append(f"{SETTINGS_FILENAME}: ignored 'lsp' (expected a table of tables)")
        return out
    for lang, table in value.items():
        if not isinstance(table, dict):
            warnings.append(f"{SETTINGS_FILENAME}: ignored 'lsp.{lang}' (expected a table)")
            continue
        for key, item in table.items():
            if key != "cmd":
                warnings.append(f"{SETTINGS_FILENAME}: ignored unknown key 'lsp.{lang}.{key}'")
            elif (argv := _cmd(item)) is None:
                warnings.append(
                    f"{SETTINGS_FILENAME}: ignored 'lsp.{lang}.cmd' (expected a command string or an array of strings)"
                )
            else:
                out.setdefault(str(lang).lower(), {})["cmd"] = argv
    return out


# --- 環境変数 ----------------------------------------------------------------------------


def env_settings(env: Mapping[str, str]) -> Loaded:
    """``LUCIANKE_MAX_FILE_BYTES``・``LUCIANKE_LSP_<LANG>_CMD``・``LUCIANKE_DISABLE_LSP`` を設定の形にする。

    空の値は未設定として扱う。解釈できない値は無視して警告を返す。``LUCIANKE_CACHE_DIR`` と
    ``LUCIANKE_LOG_LEVEL`` はサーバー全体の設定でワークスペース設定ではないので、ここでは読まない。
    """
    values: dict[str, Any] = {}
    warnings: list[str] = []
    for name, text in env.items():
        if not name.startswith("LUCIANKE_") or not text.strip():
            continue
        if name == "LUCIANKE_MAX_FILE_BYTES":
            try:
                number = _positive_int(int(text))
            except ValueError:
                number = None
            if number is None:
                warnings.append(f"{name}: ignored (expected a positive integer)")
            else:
                values["max_file_bytes"] = number
        elif name == "LUCIANKE_DISABLE_LSP":
            values["disable_lsp"] = tuple(p.strip().lower() for p in text.split(",") if p.strip())
        elif (
            name.startswith("LUCIANKE_LSP_")
            and name.endswith("_CMD")
            and len(name) > len("LUCIANKE_LSP__CMD")
        ):
            lang = name[len("LUCIANKE_LSP_") : -len("_CMD")].lower()
            if (argv := _cmd(text)) is None:
                warnings.append(f"{name}: ignored (expected a command line)")
            else:
                values.setdefault("lsp", {}).setdefault(lang, {})["cmd"] = argv
    return Loaded(values, tuple(warnings))


# --- 優先順位の合成 -----------------------------------------------------------------------


def _normalize(key: str, value: object) -> object:
    if key in _LIST_KEYS and isinstance(value, Sequence) and not isinstance(value, str):
        return tuple(str(v) for v in value)
    return value


def merge(
    tool_args: Mapping[str, Any] | None,
    env: Mapping[str, str] | None,
    file_settings: Mapping[str, Any] | None,
    detected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """優先順位 ツールでの明示 > 環境変数 > ``.lucianke.toml`` > 自動検出 で設定を合成する（仕様書 §12.7）。

    ``env`` は生の環境変数（``os.environ``）。キーごとに、先に値を持つ層が勝つ（``None`` は未設定）。
    ``lsp`` は言語・項目ごとに同じ規則で合成する。どの層にも無い項目は既定値
    （``max_file_bytes``・``co_change_window`` は ``core/limits.py``、配列は空）で埋める。
    """
    layers: list[Mapping[str, Any]] = [
        tool_args or {},
        env_settings(env).values if env is not None else {},
        file_settings or {},
        detected or {},
    ]
    out: dict[str, Any] = {}
    for key in (*_LIST_KEYS, *_SCALAR_KEYS):
        for layer in layers:
            if layer.get(key) is not None:
                out[key] = _normalize(key, layer[key])
                break

    lsp: dict[str, dict[str, Any]] = {}
    for layer in reversed(layers):  # 低い層から上書きしていく
        for lang, table in (layer.get("lsp") or {}).items():
            for item, value in table.items():
                if value is not None:
                    lsp.setdefault(str(lang).lower(), {})[item] = tuple(value)
    out["lsp"] = lsp

    out.setdefault("exclude_globs", ())
    out.setdefault("test_globs", ())
    out.setdefault("disable_lsp", ())
    out.setdefault("max_file_bytes", limits.MAX_FILE_BYTES)
    out.setdefault("co_change_window", limits.CO_CHANGE_WINDOW)
    return out
