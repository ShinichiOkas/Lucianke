"""Git の直読（M1 の範囲）: リビジョン解決・過去断面のファイル取得・ステージ・``.gitignore``・HEAD の状態。

設計書 §7.9.1、仕様書 §5.2、§8.5、実測は ``docs/spikes/S-08.md``。dulwich（Apache-2.0 側）で ``.git`` を直接読む。
シェルの ``git`` は起動しない。

不変条件:

* **何も書かない。** 作業ツリー、``.git/index``、refs、オブジェクトのどれも変えない（MUST）。``status`` は
  ``optional_locks=False`` で呼ぶ（既定は stat キャッシュ更新のために ``index`` を書き換える）。
* **``Repo`` を保持しない。** 要求ごとに開いて閉じる。Windows では ``Repo`` を開いたままだと、利用者の
  ``git gc`` / ``repack`` が pack の改名で ``winerror=32`` で失敗する。
* ツリー項目のパスは UTF-8 の ``bytes``。``..``・絶対パス・``\\``・制御文字・``.git`` 要素を含むものは
  **字句の検査で捨てる**（設計書 §7.9.1 手順 6）。ここで作る ``RelPath`` は過去断面の仮想パスなので、
  ディスクを引く ``confine`` は通さない（ディスク上の綴りや実体と無関係であるべきため）。
"""

from __future__ import annotations

import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from dulwich.errors import NotGitRepository
from dulwich.ignore import IgnoreFilterManager
from dulwich.index import ConflictedIndexEntry, IndexEntry
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit, Tree
from dulwich.objectspec import AmbiguousShortId, parse_commit, parse_object
from dulwich.repo import Repo

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import RelPath

__all__ = ["GitRepo", "HeadInfo", "ResolvedRevision", "RevisionKind"]

RevisionKind = Literal["working_tree", "staged", "commit"]

WORKING_TREE = "WORKING_TREE"
STAGED = "STAGED"

_HINT = "対応する指定: WORKING_TREE, STAGED, HEAD, HEAD~N, ブランチ, タグ, SHA"

# 基点（`~N` / `^N` の前）に出てはいけないもの: 空白・制御文字・``: ? * [ \ { } ~ ^``、``..``、``@{``。
_BAD_BASE = re.compile(r"[\s\x00-\x1f\x7f:?*\[\\{}~^]|\.\.|@\{")
# `HEAD~3` `HEAD^` `main~2^2` `a9477ab~1`。基点 + `~`/`^` の連なり（各々に最大 9 桁の数）。
_SPLIT = re.compile(r"(?P<base>.+?)(?P<suffix>(?:[~^]\d{0,9})*)")
_DRIVE = re.compile(r"[A-Za-z]:")
_SHA40 = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class ResolvedRevision:
    """``resolve_revision`` の結果。

    ``kind == "commit"`` のときだけ ``sha`` が 40 桁の 16 進で入る。``display`` はヘッダの ``rev=`` に出す
    ユーザー入力そのまま（設計書 §7.9.1）。
    """

    kind: RevisionKind
    sha: str | None = None
    display: str = ""


class HeadInfo(NamedTuple):
    """``head_info()`` の結果。"""

    branch: str | None  # detached HEAD は None
    head_sha: str | None  # コミットがまだ無いリポジトリは None
    dirty_count: int  # ステージ済み・未ステージ・未追跡（`.gitignore` 除く）の異なるパスの数


def _clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _revision_not_found(rev: str, reason: str, message: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_REVISION_NOT_FOUND,
        message,
        details={"param": "revision", "revision": _clip(rev), "reason": reason, "hint": _HINT},
    )


def _safe_tree_path(raw: bytes | str) -> str | None:
    """ツリー・インデックスのパスを検査して ``/`` 区切りの文字列にする。安全でなければ None。

    拒否: 空・絶対・``\\`` 区切り・NUL/制御文字・ドライブ文字・空/``.``/``..``/``.git`` の要素・UTF-8 でないもの。
    """
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        text = raw
    if not text or text.startswith("/") or "\\" in text or _DRIVE.match(text):
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return None
    for part in text.split("/"):
        if part in ("", ".", "..") or part.casefold() == ".git":
            return None
    return text


def _dir_prefix(prefix: str) -> str | None:
    """``list_*`` の ``prefix``（ディレクトリ）を正規化する。ルートは ``""``。安全でなければ None。"""
    text = prefix.replace("\\", "/").rstrip("/")
    if text in ("", "."):
        return ""
    return _safe_tree_path(text)


def _regular_staged(entry: IndexEntry | ConflictedIndexEntry) -> IndexEntry | None:
    """インデックス項目を通常ファイルの ``IndexEntry`` にする。競合中は stage 2（this）を優先する。"""
    if isinstance(entry, ConflictedIndexEntry):
        entry_or_none = entry.this or entry.ancestor or entry.other
        if entry_or_none is None:
            return None
        entry = entry_or_none
    return entry if stat.S_ISREG(entry.mode) else None


class GitRepo:
    """作業ルート直下の ``.git`` を読む。``Repo`` は要求ごとに開閉する（保持しない）。"""

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = root

    @classmethod
    def open(cls, root: Path) -> GitRepo | None:
        """``root`` が Git のワーキングコピーなら ``GitRepo``、そうでなければ None（Git 無しでも動く）。

        ``Repo.discover`` は使わない（親ディレクトリの ``.git`` を拾わない）。モノレポの
        サブディレクトリを作業ルートにする場合のパス前置は M2 で扱う。
        """
        try:
            with Repo(str(root)):
                pass
        except (NotGitRepository, FileNotFoundError, NotADirectoryError):
            return None
        return cls(root)

    @property
    def root(self) -> Path:
        return self._root

    @contextmanager
    def _open(self) -> Iterator[Repo]:
        repo = Repo(str(self._root))
        try:
            yield repo
        finally:
            repo.close()

    # --- リビジョン解決 -------------------------------------------------------------------

    def resolve_revision(self, rev: str) -> ResolvedRevision:
        """仕様書 §5.2 の ``revision`` を解決する。

        許可する形: ``WORKING_TREE``、``STAGED``、``HEAD``、ブランチ名、``origin/<branch>`` などのリモート追跡名、
        タグ名（注釈付きは剥がす）、``refs/...`` の完全名、4〜40 桁の 16 進（短縮可）、およびそれらに
        ``~N`` / ``^`` / ``^N`` を続けたもの（``HEAD~3``、``HEAD^``）。
        ``@{N}``・``a..b``・``HEAD:path``・``^{}`` など、それ以外の構文と、見つからないものは
        ``E_REVISION_NOT_FOUND``。短縮 SHA が複数に当たるときは同じコードで ``details.reason == "ambiguous"``。
        """
        if rev == WORKING_TREE:
            return ResolvedRevision("working_tree", None, rev)
        if rev == STAGED:
            return ResolvedRevision("staged", None, rev)

        m = _SPLIT.fullmatch(rev) if isinstance(rev, str) else None
        if m is None:
            raise _revision_not_found(
                str(rev), "unsupported_syntax", f"unsupported revision syntax: {_clip(str(rev))!r}"
            )
        base, suffix = m["base"], m["suffix"]
        if (
            _BAD_BASE.search(base)
            or base.startswith("/")
            or base.endswith("/")
            or "//" in base
            or base == "@"
        ):
            raise _revision_not_found(
                rev, "unsupported_syntax", f"unsupported revision syntax: {_clip(rev)!r}"
            )

        with self._open() as repo:
            try:
                commit = parse_commit(
                    repo, base
                )  # 参照・リモート追跡・短縮 SHA（4 桁〜）・注釈付きタグの剥がし
                if suffix:
                    commit = parse_commit(repo, parse_object(repo, commit.id + suffix.encode("ascii")))
            except AmbiguousShortId as exc:
                raise _revision_not_found(rev, "ambiguous", f"revision is ambiguous: {_clip(rev)!r}") from exc
            except (KeyError, ValueError) as exc:
                raise _revision_not_found(rev, "not_found", f"revision not found: {_clip(rev)!r}") from exc
            sha = commit.id.decode("ascii")
        return ResolvedRevision("commit", sha, rev)

    # --- 過去断面・ステージの読み取り ------------------------------------------------------

    @staticmethod
    def _commit(repo: Repo, commit_sha: str) -> Commit:
        if not _SHA40.fullmatch(commit_sha):
            raise _revision_not_found(
                commit_sha, "not_found", f"not a full commit sha: {_clip(commit_sha)!r}"
            )
        try:
            obj = repo[commit_sha.encode("ascii")]
        except KeyError as exc:
            raise _revision_not_found(commit_sha, "not_found", f"commit not found: {commit_sha}") from exc
        if not isinstance(obj, Commit):
            raise _revision_not_found(commit_sha, "not_found", f"not a commit: {commit_sha}")
        return obj

    def read_blob(self, commit_sha: str, rel: str) -> bytes | None:
        """``commit_sha`` の断面にある通常ファイルの内容。無い・ディレクトリ・シンボリックリンク・サブモジュール・
        安全でないパスは None。``commit_sha`` が 40 桁の既存コミットでなければ ``E_REVISION_NOT_FOUND``。
        """
        text = _safe_tree_path(rel)
        with self._open() as repo:
            commit = self._commit(repo, commit_sha)
            if text is None:
                return None
            tree = repo[commit.tree]
            assert isinstance(tree, Tree)
            try:
                mode, sha = tree.lookup_path(repo.object_store.__getitem__, text.encode("utf-8"))
            except Exception as exc:  # KeyError / NotTreeError / SubmoduleEncountered
                if type(exc).__name__ in ("KeyError", "NotTreeError", "SubmoduleEncountered"):
                    return None
                raise
            if not stat.S_ISREG(mode):
                return None
            try:
                obj = repo[sha]
            except KeyError:
                return None
            return obj.data if isinstance(obj, Blob) else None

    def read_staged_blob(self, rel: str) -> bytes | None:
        """インデックス（ステージ）にある通常ファイルの内容。未追跡・無い・安全でないパスは None。"""
        text = _safe_tree_path(rel)
        if text is None:
            return None
        with self._open() as repo:
            try:
                raw = repo.open_index()[text.encode("utf-8")]  # 読むだけ（ロックも書き込みもしない）
            except KeyError:
                return None
            entry = _regular_staged(raw)
            if entry is None:
                return None
            try:
                obj = repo[entry.sha]
            except KeyError:
                return None
            return obj.data if isinstance(obj, Blob) else None

    def list_tree(self, commit_sha: str, prefix: str = "") -> list[RelPath]:
        """``commit_sha`` の断面にある通常ファイルのパス（再帰、辞書順）。

        ``prefix`` はディレクトリ（``""`` は全体）。配下だけを返し、パスはルート相対のまま。
        シンボリックリンク・サブモジュール・安全でないパスは含めない。
        """
        pre = _dir_prefix(prefix)
        with self._open() as repo:
            commit = self._commit(repo, commit_sha)
            if pre is None:
                return []
            tree_id = commit.tree
            if pre:
                tree = repo[tree_id]
                assert isinstance(tree, Tree)
                try:
                    mode, sub = tree.lookup_path(repo.object_store.__getitem__, pre.encode("utf-8"))
                except Exception as exc:
                    if type(exc).__name__ in ("KeyError", "NotTreeError", "SubmoduleEncountered"):
                        return []
                    raise
                if not stat.S_ISDIR(mode):
                    return []
                tree_id = sub
            out: list[RelPath] = []
            for e in iter_tree_contents(repo.object_store, tree_id):
                if e.mode is None or not stat.S_ISREG(e.mode) or e.path is None:
                    continue
                safe = _safe_tree_path(e.path)
                if safe is None:
                    continue
                out.append(RelPath(f"{pre}/{safe}" if pre else safe))
            out.sort()
            return out

    def list_staged(self, prefix: str = "") -> list[RelPath]:
        """インデックス（ステージ）にある通常ファイルのパス（辞書順）。``prefix`` の意味は ``list_tree`` と同じ。"""
        pre = _dir_prefix(prefix)
        if pre is None:
            return []
        out: list[RelPath] = []
        with self._open() as repo:
            for raw_path, raw_entry in repo.open_index().items():
                if _regular_staged(raw_entry) is None:
                    continue
                safe = _safe_tree_path(raw_path)
                if safe is None or (pre and not safe.startswith(pre + "/")):
                    continue
                out.append(RelPath(safe))
        out.sort()
        return out

    # --- .gitignore と HEAD の状態 ---------------------------------------------------------

    def is_ignored(self, rel: str, is_dir: bool = False) -> bool:
        """``rel``（ルート相対、``/`` 区切り）が ``.gitignore`` / ``info/exclude`` / グローバル除外で無視されるか。

        ディレクトリは ``is_dir=True`` を渡す（内部で末尾 ``/`` を付ける。付けないと dulwich が FS を引いて遅く、
        仮想パスではファイル扱いになる。実在するディレクトリは ``is_dir=False`` でも FS の判定でディレクトリ扱いになる）。親ディレクトリが無視されていれば配下も True。インデックスは見ない
        （追跡済みファイルの扱いは対象外）。ルート自身・安全でないパスは False。
        """
        text = rel.replace("\\", "/").rstrip("/")
        safe = _safe_tree_path(text)
        if safe is None:
            return False
        with self._open() as repo:
            mgr = IgnoreFilterManager.from_repo(repo)
            return mgr.is_ignored(safe + "/" if is_dir else safe) is True

    def head_info(self) -> HeadInfo:
        """現在のブランチ名・``HEAD`` の SHA・変更のあるパスの数。

        ``status`` は ``optional_locks=False``（MUST）: 既定だと stat キャッシュ更新で ``.git/index`` を書き換える。
        """
        from dulwich import porcelain  # 遅延 import（+70 ms）

        with self._open() as repo:
            symbolic = repo.refs.read_ref(b"HEAD")
            branch: str | None = None
            if symbolic is not None and symbolic.startswith(b"ref: "):
                target = symbolic[5:].strip().decode("utf-8", "replace")
                branch = target.removeprefix("refs/heads/")
            try:
                head_sha: str | None = repo.head().decode("ascii")
            except KeyError:  # コミットがまだ無い
                head_sha = None
            st = porcelain.status(repo, untracked_files="normal", optional_locks=False)
            dirty: set[bytes] = set()
            for paths in st.staged.values():
                dirty.update(paths)
            dirty.update(st.unstaged)
            dirty.update(st.untracked)
        return HeadInfo(branch, head_sha, len(dirty))
