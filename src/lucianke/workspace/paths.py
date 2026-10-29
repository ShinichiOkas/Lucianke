"""パスの封じ込め（設計書 §7.3.2、§9.4、§9.5、仕様書 §5.3 規則 5、§13.1、§13.4）。

ファイルに触るすべての経路がここを通る（MUST）。``RelPath`` を作ってよいのは ``confine`` だけで、
``FileInfo.path`` と URN の ``path`` は必ずこの関数の出力である。

``confine`` の手順:

1. NUL・制御文字（と、Windows では使えない文字）を拒否する。
2. 区切りを ``/`` にそろえ、相対なら ``ws.root`` と結合する（絶対ならそのまま）。
3. ``os.path.realpath`` でシンボリックリンクと ``..`` を解決する。
4. 大文字小文字を区別しないファイルシステムでは畳んだ上で、結果が ``ws.root`` 配下であることを検査する。
5. 書き込み用（``for_write``）なら ``.git`` 配下、``exclude_globs`` 一致、秘密ファイルを拒否する。
6. ``ws.root`` からの相対パス（``/`` 区切り）を返す。ルートそのものは ``"."``。

返す相対パスの大文字小文字は **ディスク上の実際の綴り** である（存在しない末尾部分だけは入力のまま）。
Windows は ``realpath`` が正規の綴りを返す。macOS など ``realpath`` が正さない環境は、ディレクトリを引いて直す。
"""

from __future__ import annotations

import functools
import os
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from urllib.parse import unquote, urlsplit

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import RelPath
from lucianke.workspace.languages import is_secret_name
from lucianke.workspace.types import Workspace

__all__ = ["confine", "from_file_uri", "is_excluded", "to_abs", "to_file_uri"]

_IS_WINDOWS = os.name == "nt"

# --- 失敗の作り方 -------------------------------------------------------------------------

_REASON_TEXT = {
    "outside_root": "resolves outside the workspace root",
    "symlink_escape": "escapes the workspace root through a symbolic link",
    "git_dir": "is inside a .git directory (not writable)",
    "excluded": "matches exclude_globs (not writable)",
    "secret_file": "looks like a secret file (not writable)",
    "invalid_chars": "contains characters that are not allowed in a path",
}


def _fail(param: str, reason: str, user_path: object) -> LuciankeError:
    shown = repr(user_path if isinstance(user_path, str) else type(user_path).__name__)
    if len(shown) > 200:
        shown = shown[:197] + "..."
    return LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT,
        f"{param} {_REASON_TEXT[reason]}: {shown}",
        details={"param": param, "reason": reason},
    )


# --- 大文字小文字 -------------------------------------------------------------------------


def _case_insensitive(ws: Workspace) -> bool:
    return ws.case_insensitive or _IS_WINDOWS


def _fold(ws: Workspace, text: str) -> str:
    """パス要素の比較用キー。大文字小文字を区別しない環境では畳む（macOS の NFD/NFC の違いも吸収する）。"""
    key = os.path.normcase(text)
    if _case_insensitive(ws):
        key = unicodedata.normalize("NFC", key).casefold()
    return key


def _relative_parts(ws: Workspace, parts: Sequence[str]) -> tuple[str, ...] | None:
    """``parts`` が ``ws.root`` 配下なら、ルートからの残りの要素。配下でなければ None（要素単位の比較）。"""
    root_parts = ws.root.parts
    n = len(root_parts)
    if len(parts) < n:
        return None
    for a, b in zip(root_parts, parts[:n], strict=True):
        if _fold(ws, a) != _fold(ws, b):
            return None
    return tuple(parts[n:])


def _actual_case(root: Path, parts: tuple[str, ...]) -> tuple[str, ...]:
    """``root`` 配下の要素を、ディスク上の実際の綴りに直す（大文字小文字を区別しない FS 用）。

    存在しない要素に着いたら、そこから先は入力のまま返す。``realpath`` が綴りを正さない環境（macOS）用で、
    Windows では ``realpath`` が正すため呼ばない。
    """

    def key(name: str) -> str:
        return unicodedata.normalize("NFC", name).casefold()

    out: list[str] = []
    cur = root
    for i, part in enumerate(parts):
        try:
            names = os.listdir(cur)
        except OSError:
            return (*out, *parts[i:])
        if part in names:
            actual = part
        else:
            wanted = key(part)
            found = next((n for n in names if key(n) == wanted), None)
            if found is None:
                return (*out, *parts[i:])
            actual = found
        out.append(actual)
        cur = cur / actual
    return tuple(out)


# --- 手順 1〜2: 入力の検査 ----------------------------------------------------------------

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WIN_FORBIDDEN = re.compile(r'[<>"|?*]')
_WIN_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?$", re.IGNORECASE)
_WIN_DRIVE = re.compile(r"^[A-Za-z]:")


def _prepare(user_path: object, param: str) -> str:
    """NUL・制御文字を拒否し、Windows では区切りを ``/`` にそろえて危険な形（ストリーム・予約名・ドライブ相対）を拒否する。"""
    if not isinstance(user_path, str) or not user_path or _CONTROL_CHARS.search(user_path):
        raise _fail(param, "invalid_chars", user_path)
    if not _IS_WINDOWS:
        return user_path

    text = user_path.replace("\\", "/")
    if text.startswith(("//?/", "//./")):  # 拡張長パス・デバイス名前空間
        rest = text[4:]
        if not re.match(r"^[A-Za-z]:/", rest):
            raise _fail(param, "outside_root", user_path)
        text = rest
    body = text
    if _WIN_DRIVE.match(text):
        if text[2:3] != "/":  # "C:foo" や "C:" はドライブごとのカレントディレクトリ相対で、ルートとの関係が決まらない
            raise _fail(param, "outside_root", user_path)
        body = text[2:]
    if ":" in body or _WIN_FORBIDDEN.search(body):  # NTFS の代替データストリーム (a.py:s) もここで落ちる
        raise _fail(param, "invalid_chars", user_path)
    for comp in body.split("/"):
        if comp in ("", ".", ".."):
            continue
        # 末尾のドット・空白は Win32 が黙って落とす（".git ." が ".git" になる）。予約名はデバイスを開く。
        if comp[-1] in ". " or _WIN_RESERVED.match(comp):
            raise _fail(param, "invalid_chars", user_path)
    return text


# --- confine -----------------------------------------------------------------------------


def confine(ws: Workspace, user_path: str, *, for_write: bool = False, param: str = "path") -> RelPath:
    """``user_path``（相対または絶対）をワークスペースルート相対の ``RelPath`` にする。

    ルート外・リンク越え・不正な文字は ``E_INVALID_ARGUMENT``（``details.param`` / ``details.reason``）。
    ``for_write`` のときは、``.git`` 配下・``exclude_globs`` 一致・秘密ファイルも拒否する（仕様書 §13.4、§13.5）。
    存在しないパスも通る（新規ファイルの書き込み用）。ルートそのものは ``"."``。
    読み取り用の ``confine`` は ``.git`` 配下も通す（拒否するのは書き込みだけ）。
    """
    text = _prepare(user_path, param)
    joined = os.path.join(str(ws.root), text)  # 絶対なら ws.root は捨てられる
    try:
        real = os.path.realpath(joined)
    except ValueError as exc:
        raise _fail(param, "invalid_chars", user_path) from exc
    except OSError as exc:  # リンクの循環など
        raise _fail(param, "symlink_escape", user_path) from exc

    rel_parts = _relative_parts(ws, PurePath(real).parts)
    if rel_parts is None:
        # 字句だけで見てもルート外なら outside_root、字句ではルート内なのに実体が外ならリンク越え。
        inside_lexically = _relative_parts(ws, PurePath(os.path.normpath(joined)).parts) is not None
        raise _fail(param, "symlink_escape" if inside_lexically else "outside_root", user_path)

    if ws.case_insensitive and not _IS_WINDOWS:
        rel_parts = _actual_case(ws.root, rel_parts)
    rel = RelPath("/".join(rel_parts) or ".")

    if for_write:
        folded = [_fold(ws, p) for p in rel_parts]
        if ".git" in folded:
            raise _fail(param, "git_dir", user_path)
        if is_excluded(ws, rel):
            raise _fail(param, "excluded", user_path)
        if rel_parts and is_secret_name(rel_parts[-1]):
            raise _fail(param, "secret_file", user_path)
    return rel


def to_abs(ws: Workspace, rel: RelPath) -> Path:
    """封じ込め済みの相対パスを絶対パスにする。``rel`` が絶対・``..`` を含む・ルートを出るなら ``ValueError``（呼び出し側のバグ）。"""
    text = str(rel)
    if text in ("", "."):
        return ws.root
    parts = PurePosixPath(text).parts
    if ".." in parts:
        raise ValueError(f"not a confined relative path: {text!r}")
    abs_path = ws.root.joinpath(*parts)
    if not abs_path.is_relative_to(ws.root):  # 絶対パスや "C:/x" はここで落ちる
        raise ValueError(f"not a confined relative path: {text!r}")
    return abs_path


# --- 除外 glob ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Glob:
    regex: re.Pattern[str]
    anchored: bool  # True: ルートからのパス全体（の先頭からの部分）に当てる。False: パスの要素のどれかに当てる
    dir_only: bool  # 末尾が "/" のパターン: 最後の要素（ファイル）には当てない


def _segment_regex(seg: str) -> str:
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        i += 1
        if c == "*":
            while i < n and seg[i] == "*":
                i += 1
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i
            if j < n and seg[j] in "!^":
                j += 1
            if j < n and seg[j] == "]":
                j += 1
            while j < n and seg[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape("["))
                continue
            stuff = seg[i:j]
            i = j + 1
            negate = stuff[:1] in ("!", "^")
            body = stuff[1:] if negate else stuff
            body = body.replace("\\", "\\\\")
            out.append(f"[{'^' if negate else ''}{body}]")
        else:
            out.append(re.escape(c))
    return "".join(out)


@functools.lru_cache(maxsize=512)
def _compile_glob(pattern: str, ignore_case: bool) -> _Glob | None:
    """gitignore に近い記法を正規表現にする。空・否定（``!``）のパターンは None（除外に使わない）。

    * ``/`` を含まないパターン（``*.pyc``、``node_modules``）は、パスのどの要素にも当てる。
    * ``/`` を含む（または先頭が ``/``）パターンはルートに固定する。``*`` と ``?`` は ``/`` を越えず、``**`` は越える。
    * パターンに当たるディレクトリの配下はすべて除外される。末尾 ``/`` はディレクトリだけに当てる。
    """
    pat = pattern.strip()
    if _IS_WINDOWS:
        pat = pat.replace("\\", "/")
    if not pat or pat.startswith("!"):
        return None
    dir_only = pat.endswith("/")
    pat = pat.rstrip("/")
    anchored = "/" in pat
    pat = pat.lstrip("/")
    if not pat:
        return None

    segs = pat.split("/")
    rx = ""
    last = len(segs) - 1
    for idx, seg in enumerate(segs):
        if seg == "**":
            rx += (".+" if idx > 0 else ".*") if idx == last else "(?:.+/)?"
        else:
            rx += _segment_regex(seg)
            if idx < last:
                rx += "/"
    flags = re.IGNORECASE if ignore_case else 0
    return _Glob(re.compile(rx, flags), anchored, dir_only)


def is_excluded(ws: Workspace, rel: RelPath) -> bool:
    """``rel`` が ``ws.exclude_globs`` のどれかに一致するか（一致するディレクトリの配下も除外）。

    記法は ``_compile_glob`` を参照。大文字小文字は、区別しないファイルシステムでは無視する。
    """
    text = str(rel)
    if text in ("", "."):
        return False
    parts = text.split("/")
    ignore_case = _case_insensitive(ws)
    for pattern in ws.exclude_globs:
        glob = _compile_glob(pattern, ignore_case)
        if glob is None:
            continue
        # dir_only のときは最後の要素（ファイル）を除く
        upto = len(parts) - 1 if glob.dir_only else len(parts)
        if glob.anchored:
            if any(glob.regex.fullmatch("/".join(parts[: k + 1])) for k in range(upto)):
                return True
        elif any(glob.regex.fullmatch(parts[k]) for k in range(upto)):
            return True
    return False


# --- file:// URI -------------------------------------------------------------------------


def from_file_uri(ws: Workspace, uri: str) -> RelPath | None:
    """LSP の ``file://`` URI を ``RelPath`` にする。ルート外・``file`` 以外・解釈できない URI は None。

    None は呼び出し側が外部シンボル（``lk://_ext/...``、設計書 §7.4.3）として扱う。
    ``%3A`` で符号化されたドライブ文字（``file:///c%3A/...``）や UNC（``file://server/share/...``）も読む。
    """
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme.lower() != "file":
        return None
    path = unquote(parts.path)
    host = parts.netloc
    if host and host.lower() != "localhost":
        if not _IS_WINDOWS:
            return None
        path = f"//{host}{path}"  # UNC
    elif _IS_WINDOWS and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    try:
        return confine(ws, path, param="uri")
    except LuciankeError:
        return None


def to_file_uri(ws: Workspace, rel: RelPath) -> str:
    """``RelPath`` を ``file://`` URI にする（空白・非 ASCII はパーセントエンコード）。"""
    return to_abs(ws, rel).as_uri()
