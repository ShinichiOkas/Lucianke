"""basedpyright / pyright の解析の対象（``include`` / ``exclude``）を決定的に求める（BUG-0132）。

basedpyright は解析の対象（追跡するファイル）にしか参照・呼び出し元を探さない。利用者の設定が ``include`` を絞る
（``include: ["src"]`` など）と、``tests/``・``scripts/`` の呼び出しは LSP の答えに入らないまま ``exact`` で返る。ただし
**開いた文書は対象の外でも探す**（実機で確認: ``include: ["src"]`` の F-POLY で ``tests/test_payment.py`` を開くと、参照にも
incomingCalls にも出る）。T-06・T-07 は対象の外のファイルのうち対象の名前を含むものを先に開かせてから聞く（``tools/_lsp_exact``
の ``prime_set``。tsserver の ``tsconfig`` の外のファイルと同じ扱い。BUG-0047）。

求め方（basedpyright 1.40.2 = pyright 1.1.414 の ``_getConfigOptions`` / ``getFileSpec`` に合わせる）:

* 設定ファイル: 根の ``pyrightconfig.json``（JSONC）→ 無ければ根の ``pyproject.toml`` の ``[tool.basedpyright]`` /
  ``[tool.pyright]``。節が無ければ設定なし（親は探さない: Lucianke が根の ``pyproject.toml`` を明示する。``servers.pinned_pyright_config``）。
  ``extends``（文字列）を辿る（パスは書いた設定のディレクトリから。``.toml`` なら同じ節）。
* ``include`` / ``exclude``: 最も派生した（後に読む）設定の値が勝つ。パスはその設定のディレクトリから。``include`` が無ければ根。
* ``exclude`` には既定（``**/node_modules``・``**/__pycache__``・``**/.*``・``**/__editable__.*``）をいつも足す（1.40.2 の
  ``useDefaultExcludes`` の既定）。仮想環境の自動の除外（``pyvenv.cfg`` などのあるディレクトリ）は見ない（対象の内とみなす:
  開かせない側に倒すだけで、今までの答えを変えない）。
* 照合: パターンをワークスペースの外も含む絶対パスにして、``**`` は 0 個以上の階層、``*`` / ``?`` は階層の中の任意の文字列 / 1 文字。
  末尾は ``($|/)``（そのパス自身か、その下）。Windows は大文字小文字を区別しない。

読めない設定（JSONC / TOML の構文の誤り、``include`` / ``exclude`` が配列でない、両方の節がある）は ``None``（判断しない。呼び手は
開かせない = 今までどおり）。結果は根と設定ファイルの ``(mtime_ns, size)`` で覚える。
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import threading
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_EXCLUDES", "PyrightScope", "pyright_scope"]

DEFAULT_EXCLUDES = ("**/node_modules", "**/__pycache__", "**/.*", "**/__editable__.*")
"""basedpyright 1.40.2 が利用者の ``exclude`` に足す既定（``pathConsts.defaultExcludes``）。"""

_CASE_INSENSITIVE = sys.platform == "win32"
_MAX_EXTENDS = 16
# 文字列は温存し、// と /* */ のコメントと閉じ括弧の前の末尾のカンマを消す（pyrightconfig.json は JSONC。workspace/pyenv と同じ）
_JSONC_NOISE = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/|,(?=\s*[}\]])', re.DOTALL)


class _Unreadable(Exception):
    """設定が読めない（判断しない）。"""


@dataclass(frozen=True, slots=True)
class PyrightScope:
    """解析の対象。``contains(abs_path)`` は、その Python ファイルを basedpyright が追跡するか。"""

    include: tuple[re.Pattern[str], ...]
    exclude: tuple[re.Pattern[str], ...]
    config: str | None
    """採った設定ファイル（``extends`` の根元ではなく、根の側のもの）。無ければ ``None``。"""
    narrowed: bool = False
    """利用者の設定が ``include`` か ``exclude`` を書いた（既定の除外だけなら ``False``: 対象の外はドットのディレクトリ・
    ``node_modules``・``__pycache__``・編集可能インストールの影の木だけで、呼び出し元を開かせる先にならない）。"""

    def contains(self, path: str | os.PathLike[str]) -> bool:
        text = _posix(path)
        return any(p.match(text) for p in self.include) and not any(p.match(text) for p in self.exclude)


def _posix(path: str | os.PathLike[str]) -> str:
    return os.fspath(path).replace("\\", "/")


def _segment(text: str) -> str:
    out = []
    for ch in text:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _file_spec(base: str, spec: str) -> re.Pattern[str]:
    """pyright の ``getFileSpec(base, spec)`` の正規表現（``^(<パターン>)($|/)``）。"""
    absolute = posixpath.normpath(posixpath.join(base, _posix(spec)))
    parts = absolute.split("/")
    pattern = re.escape(parts[0])  # ドライブ（``C:``）か、POSIX の根の空文字列
    for part in parts[1:]:
        if part == "**":
            pattern += "(/[^/][^/]*)*?"
        elif part:
            pattern += "/" + _segment(part)
    return re.compile(f"^({pattern})($|/)", re.IGNORECASE if _CASE_INSENSITIVE else 0)


def _read_jsonc(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise _Unreadable(str(e)) from e
    try:
        data = json.loads(_JSONC_NOISE.sub(lambda m: m.group(1) or "", text))
    except ValueError as e:
        raise _Unreadable(str(e)) from e
    if not isinstance(data, dict):
        raise _Unreadable("not an object")
    return data


def _pyproject_section(path: Path) -> Mapping[str, Any] | None:
    """``pyproject.toml`` の ``[tool.basedpyright]`` / ``[tool.pyright]``。無ければ ``None``。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as e:
        raise _Unreadable(str(e)) from e
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return None
    based, plain = tool.get("basedpyright"), tool.get("pyright")
    if based is not None and plain is not None:
        raise _Unreadable("both [tool.basedpyright] and [tool.pyright]")  # basedpyright は読まない
    section = based if based is not None else plain
    if section is None:
        return None
    if not isinstance(section, dict):
        raise _Unreadable("the pyright section is not a table")
    return section


def _primary(root: Path) -> Path | None:
    config = root / "pyrightconfig.json"
    if config.is_file():
        return config
    pyproject = root / "pyproject.toml"
    return pyproject if pyproject.is_file() else None


def _chain(primary: Path) -> list[tuple[Path, Mapping[str, Any]]]:
    """``primary`` から ``extends`` を辿った設定（根元が先）。``pyproject.toml`` に節が無ければ空。"""
    chain: list[tuple[Path, Mapping[str, Any]]] = []
    seen: set[str] = set()
    current: Path | None = primary
    while current is not None and len(chain) < _MAX_EXTENDS:
        key = os.path.normcase(os.path.abspath(current))
        if key in seen:
            break  # 循環（pyright もそこで止める）
        seen.add(key)
        if not current.is_file():
            if chain:
                break  # 辿れない extends（pyright はそこまでの設定を使う）
            raise _Unreadable(f"{current} is missing")
        obj = _pyproject_section(current) if current.suffix == ".toml" else _read_jsonc(current)
        if obj is None:
            break
        chain.insert(0, (current, obj))
        ext = obj.get("extends")
        if ext is None:
            break
        if not isinstance(ext, str):
            break  # pyright は誤りをログに出して辿らない
        current = Path(os.path.join(current.parent, ext))
    return chain


def _specs(chain: Sequence[tuple[Path, Mapping[str, Any]]], key: str) -> list[re.Pattern[str]] | None:
    found: list[re.Pattern[str]] | None = None
    for path, obj in chain:  # 根元から順に。後の設定の値が勝つ
        value = obj.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise _Unreadable(f"{key} is not an array")
        base = _posix(os.path.abspath(path.parent))
        found = [_file_spec(base, v) for v in value if isinstance(v, str)]
    return found


def _compute(root: Path) -> PyrightScope | None:
    base = _posix(os.path.abspath(root))
    try:
        primary = _primary(root)
        chain = _chain(primary) if primary is not None else []
        user_include = _specs(chain, "include")
        user_exclude = _specs(chain, "exclude")
        include = user_include or [_file_spec(base, ".")]
        exclude = (user_exclude or []) + [_file_spec(base, x) for x in DEFAULT_EXCLUDES]
    except (_Unreadable, OSError, ValueError):
        return None
    config = str(chain[-1][0]) if chain else None
    narrowed = user_include is not None or user_exclude is not None
    return PyrightScope(tuple(include), tuple(exclude), config, narrowed)


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


_MEMO: dict[str, tuple[tuple[Any, ...], PyrightScope | None]] = {}
_MEMO_LOCK = threading.Lock()


def pyright_scope(root: str | os.PathLike[str], *, refresh: bool = False) -> PyrightScope | None:
    """ワークスペースの根 ``root`` の解析の対象。設定が読めなければ ``None``（判断しない）。

    根の ``pyrightconfig.json`` / ``pyproject.toml`` の ``(mtime_ns, size)`` が変わらない間は覚えた答えを返す（``extends`` の先の
    変更は根の設定を触るまで反映しない）。``refresh`` は覚えを見ずに読み直す（監視が根の設定の変更を拾ったとき。同じ大きさで
    時刻の刻みの内に書き換えられても取りこぼさない。BUG-0135）。ブロックする（ファイルを読む）ので、イベントループの上では呼ばない。
    """
    path = Path(root)
    key = os.path.normcase(os.path.abspath(path))
    stamp = (_stamp(path / "pyrightconfig.json"), _stamp(path / "pyproject.toml"))
    with _MEMO_LOCK:
        hit = None if refresh else _MEMO.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    scope = _compute(path)
    with _MEMO_LOCK:
        if len(_MEMO) >= 64:
            _MEMO.clear()
        _MEMO[key] = (stamp, scope)
    return scope
