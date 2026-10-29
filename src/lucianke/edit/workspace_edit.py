"""LSP の ``WorkspaceEdit`` → ``Plan``（ワークスペースを知る層。設計書 §7.7.2、仕様書 §10.3 段 2、T-11・T-13 が共用）。

位置の変換・整列・重なりの検出・``changes`` / ``documentChanges`` の両形の読み取りは ``edit.plan.from_workspace_edit``
（純関数。ファイルを知らない）が行う。このモジュールはそれに **ワークスペースの事実** を足す:

1. **URI → ルート相対パス**: ``workspace.paths.from_file_uri`` → ``confine(for_write=True)``。ルート外は
   ``E_INVALID_ARGUMENT``（``details.reason="edit_outside_workspace"``）、``.git`` 配下・除外・秘密ファイルは
   ``confine`` の拒否のまま。1 件でも不正なら計画を作らずに失敗（どのファイルも書かない）。
2. **現在の内容の読み取り**: 編集を受けるファイルをディスクから読み、``LineIndex``（BOM を剥がした内容。LSP の列に
   BOM は無い）を作る。UTF-8 でないファイル・``MAX_FILE_BYTES`` 超のファイルは ``E_UNSUPPORTED``。
   読んだバイト列の SHA-256 を ``Plan.resolved_shas`` に入れる（``touched_paths`` のうち既存のファイル全部）。
3. **文書の版の検査**（設計書 §7.7.2 手順 3）: ``TextDocumentEdit.textDocument.version`` が整数で、``documents.version_of``
   が返す LSP 文書の現在版と違えば ``E_PRECONDITION_FAILED``（``details.reason="lsp_document_version_mismatch"``）。
   ``version: null``（basedpyright の rename）や、LSP に開かれていない文書は検査しない。
4. **ファイル改名（``RenameFile``）**: ``allow_renames=False``（既定）なら ``E_UNSUPPORTED``
   （``details.reason="file_rename_not_supported"``）。``EditTransaction`` / ``writer`` が対応したら ``True`` にする。
   作成・削除は ``plan.from_workspace_edit`` が常に ``E_UNSUPPORTED``（``details.reason="resource_operation"``）。

``new_positions`` は、適用後のファイルでの各 hunk の開始位置（応答の ``edits[]`` の行・列。仕様書 T-11 の出力）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import MAX_FILE_BYTES
from lucianke.core.model import Position, RelPath
from lucianke.core.textpos import LineIndex, detect_newline, strip_bom
from lucianke.edit.plan import Hunk, Plan, apply_hunks, from_workspace_edit
from lucianke.workspace.paths import confine, from_file_uri, to_abs
from lucianke.workspace.types import Workspace

__all__ = ["DocumentVersions", "convert_workspace_edit", "new_positions", "plan_from_workspace_edit"]

_ANY_NEWLINE = re.compile(r"\r\n|\r|\n")


class DocumentVersions(Protocol):
    """``lsp.documents.DocumentSync`` の一部（開いている文書の現在版）。"""

    def version_of(self, path: RelPath) -> int | None: ...


def _unsupported(message: str, **details: object) -> LuciankeError:
    return LuciankeError(ErrorCode.E_UNSUPPORTED, message, details=dict(details))


def _read(ws: Workspace, rel: RelPath) -> bytes | None:
    """``rel`` の現在の内容。無ければ ``None``。UTF-8 でない・大きすぎるファイルは ``E_UNSUPPORTED``。"""
    path = to_abs(ws, rel)
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise _unsupported(
                f"file too large to edit: {rel} ({size} bytes > {MAX_FILE_BYTES})",
                reason="file_too_large",
                path=str(rel),
                size=size,
            )
        data = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise LuciankeError(
            ErrorCode.E_PRECONDITION_FAILED,
            f"cannot read {rel}: {exc.strerror or exc}",
            details={"reason": "unreadable_file", "path": str(rel)},
        ) from exc
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise _unsupported(f"{rel}: not a UTF-8 file", reason="non_utf8_file", path=str(rel)) from None
    return data


def _check_versions(we: Mapping[str, Any], to_rel: Any, documents: DocumentVersions) -> None:
    doc_changes = we.get("documentChanges")
    if not isinstance(doc_changes, Sequence) or isinstance(doc_changes, (str, bytes)):
        return
    for item in doc_changes:
        if not isinstance(item, Mapping) or item.get("kind") is not None:
            continue
        doc = item.get("textDocument")
        if not isinstance(doc, Mapping):
            continue
        version = doc.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            continue
        uri = doc.get("uri")
        rel = to_rel(uri) if isinstance(uri, str) else None
        if rel is None:
            continue  # ルート外などは from_workspace_edit が失敗させる
        current = documents.version_of(rel)
        if current is not None and current != version:
            raise LuciankeError(
                ErrorCode.E_PRECONDITION_FAILED,
                f"the LSP edit was computed for version {version} of {rel}, but the open document is at version {current}",
                details={
                    "reason": "lsp_document_version_mismatch",
                    "path": str(rel),
                    "expected": version,
                    "actual": current,
                },
            )


def convert_workspace_edit(
    ws: Workspace,
    we: Mapping[str, Any],
    *,
    encoding: str = "utf-16",
    documents: DocumentVersions | None = None,
    allow_renames: bool = False,
    target_urns: Sequence[str] = (),
) -> Plan:
    """同期版（ファイルをブロッキングで読む）。asyncio のループからは ``plan_from_workspace_edit`` を使う。"""
    contents: dict[RelPath, bytes | None] = {}

    def read_cached(rel: RelPath) -> bytes | None:
        if rel not in contents:
            contents[rel] = _read(ws, rel)
        return contents[rel]

    def to_rel(uri: str) -> RelPath | None:
        rel = from_file_uri(ws, uri)
        if rel is None:
            return None
        return confine(ws, str(rel), for_write=True, param="workspace_edit")

    def line_index_for(rel: RelPath) -> LineIndex:
        data = read_cached(rel)
        if data is None:
            raise LuciankeError(
                ErrorCode.E_PRECONDITION_FAILED,
                f"the LSP edit names a file that no longer exists: {rel}",
                details={"reason": "file_missing", "path": str(rel)},
            )
        return LineIndex(strip_bom(data))

    if documents is not None:
        _check_versions(we, to_rel, documents)
    plan = from_workspace_edit(we, to_rel=to_rel, line_index_for=line_index_for, encoding=encoding)
    if plan.renames and not allow_renames:
        old, new = plan.renames[0]
        raise _unsupported(
            "WorkspaceEdit renames a file; file renames are not supported yet",
            reason="file_rename_not_supported",
            old=str(old),
            new=str(new),
        )
    shas: dict[RelPath, str] = {}
    for rel in plan.touched_paths:
        data = read_cached(rel)
        if data is not None:  # 改名先のようにまだ無いパスは前提を持たない
            shas[rel] = hashlib.sha256(data).hexdigest()
    return replace(plan, resolved_shas=shas, target_urns=tuple(target_urns))


async def plan_from_workspace_edit(
    ws: Workspace,
    we: Mapping[str, Any],
    *,
    encoding: str = "utf-16",
    documents: DocumentVersions | None = None,
    allow_renames: bool = False,
    target_urns: Sequence[str] = (),
) -> Plan:
    """LSP の ``WorkspaceEdit`` を ``Plan`` にする。

    Args:
        ws: 対象のワークスペース（URI → 相対パス、ファイルの読み取り）。
        we: LSP が返した ``WorkspaceEdit``（dict のまま）。
        encoding: LSP の列の単位（``client.negotiated_encoding.value``: ``"utf-16"`` / ``"utf-8"`` / ``"utf-32"``）。
        documents: 文書の版の検査に使う（``DocumentSync``）。``None`` なら検査しない。
        allow_renames: ``RenameFile`` を ``Plan.renames`` に通すか（既定は ``E_UNSUPPORTED``）。
        target_urns: ``Plan.target_urns``（再索引後の対応付けに使う）。

    Returns:
        ``hunks``（ファイルごとに開始位置の昇順、同じ位置への挿入は LSP の配列順）、``touched_paths``、
        ``resolved_shas``（読んだ内容の SHA-256）を持つ ``Plan``。

    Raises:
        LuciankeError: ルート外・除外パスの編集（``E_INVALID_ARGUMENT``）、作成・削除・（既定では）改名・繰り返しの
            文書編集・非 UTF-8（``E_UNSUPPORTED``）、重なる編集（``E_INVALID_ARGUMENT``、``overlapping_edits``）、
            版の不一致・消えたファイル（``E_PRECONDITION_FAILED``）、形の崩れ（``E_INTERNAL``）。
    """
    return await asyncio.to_thread(
        convert_workspace_edit,
        ws,
        we,
        encoding=encoding,
        documents=documents,
        allow_renames=allow_renames,
        target_urns=target_urns,
    )


def new_positions(source: bytes, hunks: Iterable[Hunk]) -> list[tuple[Hunk, Position]]:
    """同じファイルの ``hunks`` を ``source`` に適用した **あとのファイル** での、各 hunk の ``new_text`` の開始位置。

    戻り値は開始位置の昇順（同じ位置は与えた順）。位置は 0 始まりの行・コードポイント列で、先行する hunk の増減
    （同じ行の前方の改名による列のずれ、改行の増減による行のずれ）を反映する。``new_text`` の改行は ``source`` の改行コードに揃えて数える
    （``apply_hunks`` と同じ）。
    """
    hs = list(hunks)
    if not hs:
        return []
    idx = LineIndex(source)
    newline = detect_newline(source)

    def offset(pos: Position) -> int:
        if pos.line >= idx.line_count:
            return len(source)
        return idx.position_to_offset(pos).value

    spans = sorted(
        ((offset(h.range.start), offset(h.range.end), order, h) for order, h in enumerate(hs)),
        key=lambda t: (t[0], t[1], t[2]),
    )
    out: list[tuple[int, Hunk]] = []
    delta = 0
    for start, end, _order, h in spans:
        text = (
            _ANY_NEWLINE.sub(newline, h.new_text)
            if ("\n" in h.new_text or "\r" in h.new_text)
            else h.new_text
        )
        out.append((start + delta, h))
        delta += len(text.encode("utf-8", "surrogateescape")) - (end - start)
    # 適用後のバイト列は、位置を求めるためだけに組み立てる（hunk の外はそのまま）
    new_index = LineIndex(apply_hunks(source, hs))
    result: list[tuple[Hunk, Position]] = []
    for off, h in out:
        result.append((h, new_index.offset_to_position(min(off, len(new_index.data))).value))
    return result
