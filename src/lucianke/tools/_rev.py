"""``revision`` を受ける読み取りツールの共通の入口（仕様書 §8.5、設計書 §7.1.3 手順 6・§7.9.1、BUG-0018）。

* ``open_revision``: ``revision``（``WORKING_TREE`` / ``STAGED`` / ``HEAD`` / ブランチ / タグ / SHA / ``~N``）を解決し、
  ``RevisionView`` を返す。解決できなければ ``E_REVISION_NOT_FOUND``（``details.reason``）。**作業ツリーのときは
  Git を一切開かない**（dulwich の import は約 130 ms かかるので、作業ツリー以外のときだけ遅延 import する）。
* ``read_checked``: ``RevisionView`` の断面から 1 ファイルを読む（封じ込め → 読み込み → 大きさ・秘密・バイナリの検査）。
  作業ツリーでも過去断面でも同じ規約で、``LoadedFile`` を返す。
* ``analyze_at``: T-02 / T-05 が使う ``analyze_file`` の断面対応版。作業ツリーは ``analyze_file`` にそのまま任せ
  （挙動を変えない）、過去断面は ``read_checked`` で読んだバイト列を同じ手順で解析する。
* ``reject_unsupported_revision``: 過去断面を M1 で扱わないツール（T-03・T-04・T-06・T-07・T-08）の入口。
  存在しない参照は ``E_REVISION_NOT_FOUND``、解決できる参照は ``E_UNSUPPORTED``（``revision_not_supported_until_M2``）。

過去断面の読み取りは Git オブジェクトとインデックスを読むだけで、作業ツリーにも ``.git`` にも書かない。
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import MAX_FILE_BYTES
from lucianke.core.model import RelPath
from lucianke.syntax.support import get_support
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.tools._base import WORKING_TREE, ToolContext
from lucianke.tools._services import SYMBOL_LANGUAGES, FileAnalysis, ToolServices, analyze_file
from lucianke.workspace.languages import HEAD_BYTES, Detected, detect
from lucianke.workspace.paths import confine, to_abs
from lucianke.workspace.types import Workspace

if TYPE_CHECKING:  # git.repo は dulwich を import する。型のためだけに読む
    from lucianke.git.vtree import FileSource

__all__ = [
    "LoadedFile",
    "WORKING_VIEW",
    "RevisionView",
    "analyze_at",
    "analyze_bytes",
    "open_revision",
    "read_checked",
    "reject_unsupported_revision",
]

_SHORT_SHA = 7


@dataclass(frozen=True, slots=True)
class RevisionView:
    """解決済みの ``revision``。``source`` は過去断面・ステージのときだけ入る（作業ツリーは ``None``）。

    ``revision`` は利用者が渡した文字列そのまま（ヘッダの ``rev=`` と同じ。``ResolvedRevision.display``）。
    """

    revision: str
    kind: str  # "working_tree" | "staged" | "commit"
    sha: str | None = None
    source: FileSource | None = None

    @property
    def is_working_tree(self) -> bool:
        return self.kind == "working_tree"

    def engine_name(self, base: str = "tree-sitter") -> str:
        """``engine_used``。過去断面は ``tree-sitter@<短縮 SHA>``（ステージは ``@staged``。仕様書 §8.5）。"""
        if self.kind == "commit" and self.sha:
            return f"{base}@{self.sha[:_SHORT_SHA]}"
        if self.kind == "staged":
            return f"{base}@staged"
        return base

    @property
    def warnings(self) -> tuple[str, ...]:
        """過去断面の注意（仕様書 §8.5: LSP を使わず構文木で解析した）。作業ツリーは空。"""
        if self.is_working_tree:
            return ()
        return (
            f"revision {self.revision!r}: read from Git objects without touching the working tree; "
            "syntax tree only (no LSP)",
        )


WORKING_VIEW = RevisionView(WORKING_TREE, "working_tree")


def _open_sync(ws: Workspace, revision: str) -> RevisionView:
    # dulwich を import する（約 130 ms）。作業ツリー以外のときだけここに来る。
    from lucianke.git.repo import GitRepo
    from lucianke.git.vtree import open_file_source

    repo = GitRepo.open(ws.root)
    if repo is None:
        raise LuciankeError(
            ErrorCode.E_REVISION_NOT_FOUND,
            f"workspace is not a Git repository; cannot read revision {revision!r}",
            details={"param": "revision", "revision": revision[:200], "reason": "no_git_repository"},
        )
    resolved = repo.resolve_revision(revision)
    return RevisionView(
        revision=resolved.display,
        kind=resolved.kind,
        sha=resolved.sha,
        source=open_file_source(ws, repo, resolved),
    )


async def open_revision(ws: Workspace, revision: str) -> RevisionView:
    """``revision`` を解決して ``RevisionView`` にする。``WORKING_TREE`` は Git に触れずに返す。"""
    if revision == WORKING_TREE:
        return WORKING_VIEW
    return await asyncio.to_thread(_open_sync, ws, revision)


async def reject_unsupported_revision(ctx: ToolContext, revision: str) -> None:
    """過去断面を M1 で扱わないツールの入口。``WORKING_TREE`` なら何もしない。

    それ以外は、まず ``revision`` を解決する（存在しない参照・不正な構文は ``E_REVISION_NOT_FOUND``）。解決できた参照は
    ``E_UNSUPPORTED``（``details.reason == "revision_not_supported_until_M2"``。設計書 §7.9.1）。
    """
    if revision == WORKING_TREE:
        return
    workspace = getattr(ctx.services, "workspace", None)  # T-08 などのテスト用の入れ物も ``workspace()`` を持つ
    if workspace is None:
        raise LuciankeError(ErrorCode.E_WORKSPACE_NOT_CONFIGURED, "no workspace is known")
    await open_revision(workspace(ctx.args.workspace_id), revision)
    raise LuciankeError(
        ErrorCode.E_UNSUPPORTED,
        f"revision {revision!r} is not supported yet (WORKING_TREE only)",
        details={"reason": "revision_not_supported_until_M2", "revision": revision},
    )


# --- 1 ファイルの読み取り ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedFile:
    """断面から読んで検査済みの 1 ファイル。"""

    rel: RelPath
    data: bytes
    sha256: str
    detected: Detected


def _not_found(rel: str, param: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_TARGET_NOT_FOUND, f"file not found: {rel}", details={"path": rel, "param": param}
    )


def _too_large(size: int, limit: int) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_UNSUPPORTED,
        f"file too large to parse: {size} bytes > {limit}",
        details={"reason": "file_too_large", "size": size, "limit": limit},
    )


async def read_checked(
    view: RevisionView, ws: Workspace, user_path: str, *, param: str = "path", max_bytes: int = MAX_FILE_BYTES
) -> LoadedFile:
    """``user_path`` を ``view`` の断面から読む。失敗は ``LuciankeError``（項目単位で使う）。

    ``confine`` でルート外・リンク越えを退け、無ければ ``E_TARGET_NOT_FOUND``、``max_bytes`` 超・秘密・バイナリは
    ``E_UNSUPPORTED``。過去断面の読み取りは ``FileSource``（Git オブジェクト / インデックス）だけを通す。
    """
    rel = confine(ws, user_path, param=param)
    if view.source is None:  # 作業ツリー
        abs_path = to_abs(ws, rel)
        try:
            is_file = abs_path.is_file()
        except OSError:
            is_file = False
        if not is_file:
            raise _not_found(str(rel), param)
        size = abs_path.stat().st_size
        if size > max_bytes:
            raise _too_large(size, max_bytes)
        data = await asyncio.to_thread(abs_path.read_bytes)
    else:
        source = view.source
        try:
            data = await asyncio.to_thread(source.read, str(rel))
        except LuciankeError as err:
            if err.code is ErrorCode.E_TARGET_NOT_FOUND:
                raise LuciankeError(
                    err.code,
                    f"file not found at {view.revision}: {rel}",
                    details={**err.details, "path": str(rel), "param": param},
                ) from err
            raise
        if len(data) > max_bytes:
            raise _too_large(len(data), max_bytes)
    det = detect(rel, data[:HEAD_BYTES])
    if not det.index_content:
        reason = "secret_file" if det.is_secret else "binary_file"
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED, f"{rel}: content is not readable ({reason})", details={"reason": reason}
        )
    return LoadedFile(rel, data, hashlib.sha256(data).hexdigest(), det)


# --- コードの解析 ------------------------------------------------------------------------


async def analyze_bytes(svc: ToolServices, ws: Workspace, loaded: LoadedFile) -> FileAnalysis:
    """読み込み済みのバイト列を構文木で解析する（``analyze_file`` の後半と同じ手順）。

    ``TreeCache`` のキーは内容の SHA-256 を含むので、作業ツリーの同じ内容の木と自然に共有される。
    """
    language_id = loaded.detected.language_id
    spec = get_support(language_id)
    if language_id not in SYMBOL_LANGUAGES or spec is None or not hasattr(spec, "grammar"):
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"{loaded.rel}: language {language_id!r} has no structural support in this build "
            f"(supported: {', '.join(sorted(SYMBOL_LANGUAGES))})",
            details={"reason": "language_not_supported", "language_id": language_id},
        )
    data = loaded.data
    parsed = await svc.trees.aget_or_parse(
        ws.workspace_id, loaded.rel, loaded.sha256, language_id, lambda: data
    )
    symbols = extract(
        parsed,
        language_id=language_id,
        language=spec.grammar(),
        query_source=load_query_source(language_id),
        workspace_id=ws.workspace_id,
        rel=loaded.rel,
    )
    return FileAnalysis(
        rel=loaded.rel,
        data=data,
        sha256=loaded.sha256,
        language_id=language_id,
        tier=loaded.detected.tier,
        parsed=parsed,
        symbols=tuple(symbols),
    )


async def analyze_at(
    svc: ToolServices, ws: Workspace, view: RevisionView, user_path: str, *, param: str = "path"
) -> FileAnalysis:
    """``analyze_file`` の断面対応版。作業ツリーは ``analyze_file`` にそのまま任せる。"""
    if view.is_working_tree:
        return await analyze_file(svc, ws, user_path, param=param)
    return await analyze_bytes(svc, ws, await read_checked(view, ws, user_path, param=param))
