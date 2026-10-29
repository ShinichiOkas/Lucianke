"""仮想ツリー ``VirtualTree`` とファイルの入口 ``FileSource``（設計書 §7.9.1、§7.1.3 手順 6、仕様書 §8.5）。

ツール（T-02 / T-05）は ``ToolContext.file_source(rel)`` の 1 つの入口だけでファイルを読み、``revision`` を意識しない。
その裏は ``open_file_source`` が ``ResolvedRevision.kind`` で選ぶ。

* ``working_tree`` → ``WorkingTreeSource``（ディスクから読む薄い実装。``confine`` を通す）
* ``commit`` / ``staged`` → ``VirtualTree``（Git オブジェクトとインデックスから読む。**作業ツリーに触れない**）

``VirtualTree`` は要求の間だけ生きる。同じパスの読み取りはメモリに載せる（一度読んだ内容を要求内で使い回す）。
解析結果の一時テーブルは ``index`` 層の仕事で、ここは「パス → バイト列」だけを担う。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import RelPath
from lucianke.git.repo import GitRepo, ResolvedRevision
from lucianke.workspace.paths import confine, is_excluded, to_abs
from lucianke.workspace.types import Workspace

__all__ = ["FileSource", "VirtualTree", "WorkingTreeSource", "open_file_source"]

_RelPaths = list[RelPath]


class FileSource(Protocol):
    """``revision`` ごとのファイル内容の入口。``read`` は無ければ ``E_TARGET_NOT_FOUND``。"""

    def read(self, rel: str) -> bytes: ...

    def exists(self, rel: str) -> bool: ...

    def list(self, prefix: str = "") -> _RelPaths:
        """``prefix``（ディレクトリ。``""`` は全体）配下の通常ファイルのパス（辞書順）。"""
        ...


def _not_found(rel: str, revision: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_TARGET_NOT_FOUND,
        f"file not found at {revision}: {rel}",
        details={"path": rel, "param": "path", "revision": revision},
    )


class VirtualTree:
    """過去断面（``commit``）またはステージ（``staged``）の仮想ツリー。読み取り専用。"""

    def __init__(self, repo: GitRepo, resolved: ResolvedRevision) -> None:
        if resolved.kind == "commit":
            if resolved.sha is None:
                raise ValueError("a commit revision needs a sha")
        elif resolved.kind != "staged":
            raise ValueError(f"VirtualTree does not serve {resolved.kind!r}; use WorkingTreeSource")
        self._repo = repo
        self._resolved = resolved
        self._cache: dict[str, bytes | None] = {}

    @property
    def revision(self) -> ResolvedRevision:
        return self._resolved

    def _load(self, rel: str) -> bytes | None:
        if rel in self._cache:
            return self._cache[rel]
        if self._resolved.kind == "staged":
            data = self._repo.read_staged_blob(rel)
        else:
            assert self._resolved.sha is not None
            data = self._repo.read_blob(self._resolved.sha, rel)
        self._cache[rel] = data
        return data

    def read(self, rel: str) -> bytes:
        data = self._load(rel)
        if data is None:
            raise _not_found(rel, self._resolved.display or self._resolved.kind)
        return data

    def exists(self, rel: str) -> bool:
        return self._load(rel) is not None

    def list(self, prefix: str = "") -> _RelPaths:
        if self._resolved.kind == "staged":
            return self._repo.list_staged(prefix)
        assert self._resolved.sha is not None
        return self._repo.list_tree(self._resolved.sha, prefix)


class WorkingTreeSource:
    """作業ツリー（ディスク）から読む薄い実装。パスは ``confine`` を通す（ルート外・リンク越えは ``E_INVALID_ARGUMENT``）。

    ``list`` はディスクを歩いて ``.git`` と ``exclude_globs`` 一致を除く。``.gitignore`` は見ない
    （索引の対象判定は engine 層の仕事）。
    """

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws

    def _abs(self, rel: str) -> tuple[RelPath, Path]:
        confined = confine(self._ws, rel)
        return confined, to_abs(self._ws, confined)

    def read(self, rel: str) -> bytes:
        confined, abs_path = self._abs(rel)
        try:
            return abs_path.read_bytes()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError) as exc:
            raise _not_found(str(confined), "WORKING_TREE") from exc

    def exists(self, rel: str) -> bool:
        _, abs_path = self._abs(rel)
        try:
            return abs_path.is_file()
        except OSError:
            return False

    def list(self, prefix: str = "") -> _RelPaths:
        _, start = self._abs(prefix or ".")
        out: _RelPaths = []
        stack = [start]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                if entry.name == ".git":
                    continue
                try:
                    rel = RelPath(Path(entry.path).relative_to(self._ws.root).as_posix())
                    if is_excluded(self._ws, rel):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        out.append(rel)
                except (OSError, ValueError):
                    continue
        out.sort()
        return out


def open_file_source(ws: Workspace, repo: GitRepo | None, resolved: ResolvedRevision) -> FileSource:
    """``resolved.kind`` に合う ``FileSource`` を返す（``ToolContext.file_source`` の裏）。

    ``working_tree`` は Git が無くても使える。``commit`` / ``staged`` で ``repo`` が None（Git が無い）なら
    ``E_REVISION_NOT_FOUND``（``details.reason == "no_git_repository"``）。
    """
    if resolved.kind == "working_tree":
        return WorkingTreeSource(ws)
    if repo is None:
        raise LuciankeError(
            ErrorCode.E_REVISION_NOT_FOUND,
            f"workspace is not a Git repository; cannot read revision {resolved.display or resolved.kind!r}",
            details={"param": "revision", "revision": resolved.display, "reason": "no_git_repository"},
        )
    return VirtualTree(repo, resolved)
