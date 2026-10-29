"""編集の計画（設計書 §7.7.2、仕様書 §10.2・§10.3 段 2）。

* ``Hunk``: 1 件の書き換え。位置は **適用前の座標**（0 始まりの行・コードポイント列。ADR-14）。挿入は ``start == end``。
* ``Plan``: hunk の集合と、前提検証・再索引のための付随情報。
* ``apply_hunks``: 同じファイルの hunk をメモリ上で適用する（バイト列 → バイト列）。
* ``from_workspace_edit``: LSP の ``WorkspaceEdit`` を ``Plan`` に変換する。
* ``unified_diff``: 適用前後のバイト列から統一 diff を作る。

位置の変換は ``textpos.LineIndex`` だけが行う（LSP の UTF-16 / UTF-8 の列 → コードポイント、コードポイント → バイト）。
トランザクション・書き込み・構文検証・診断差分はここでは行わない。
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Position, Range, RelPath
from lucianke.core.textpos import LineIndex, Unit, detect_newline

__all__ = ["Hunk", "Plan", "apply_hunks", "check_no_overlap", "from_workspace_edit", "unified_diff"]

_ANY_NEWLINE = re.compile(r"\r\n|\r|\n")


# --- データ型 ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hunk:
    """書き換え 1 件。``range`` は適用前の座標（半開区間）。

    ``new_text`` の改行は ``"\\n"`` でも対象ファイルの改行でもよい。``apply_hunks`` が対象ファイルの改行に揃える。
    """

    path: RelPath
    range: Range
    new_text: str
    label: str | None = None  # 表示用（"replace_body", "rename" など）


@dataclass(frozen=True, slots=True)
class Plan:
    """書き換えの集合（仕様書 §10.2 の「計画」）。

    * ``renames``: ``(旧パス, 新パス)``。``WorkspaceEdit`` の ``RenameFile`` 由来のみ。
      改名と同じ文書への ``TextDocumentEdit`` は、hunk の ``path`` を **旧パス**（編集前の内容があるほう）にする。
    * ``touched_paths``: hunk のパスと ``renames`` の両側の和集合。渡した値を先頭に、足りないものを後ろに足して正規化する。
    * ``resolved_shas``: 解決段で読んだ ``sha256``（``touched_paths`` 全部のうち既存ファイル）。
    * ``target_urns``: 編集対象。再索引後の対応付けに使う。

    リストを渡してもタプルに直す（``Plan`` は不変）。
    """

    hunks: tuple[Hunk, ...] = ()
    touched_paths: tuple[RelPath, ...] = ()
    resolved_shas: Mapping[RelPath, str] = field(default_factory=dict)
    target_urns: tuple[str, ...] = ()
    renames: tuple[tuple[RelPath, RelPath], ...] = ()
    expects_syntax: bool = True  # False なら構文検証は "skipped"（Tier 4）

    def __post_init__(self) -> None:
        hunks = tuple(self.hunks)
        renames = tuple((RelPath(old), RelPath(new)) for old, new in self.renames)
        touched: dict[RelPath, None] = dict.fromkeys(RelPath(p) for p in self.touched_paths)
        for h in hunks:
            touched.setdefault(h.path)
        for old, new in renames:
            touched.setdefault(old)
            touched.setdefault(new)
        object.__setattr__(self, "hunks", hunks)
        object.__setattr__(self, "renames", renames)
        object.__setattr__(self, "touched_paths", tuple(touched))
        object.__setattr__(self, "target_urns", tuple(self.target_urns))
        object.__setattr__(self, "resolved_shas", dict(self.resolved_shas))

    def hunks_for(self, path: RelPath) -> list[Hunk]:
        """``path`` の hunk（計画の順）。"""
        return [h for h in self.hunks if h.path == path]

    @property
    def hunk_paths(self) -> tuple[RelPath, ...]:
        """hunk を持つパス（初出順）。"""
        return tuple(dict.fromkeys(h.path for h in self.hunks))


# --- 適用 ----------------------------------------------------------------------------------


def _overlap_error(path: RelPath | str, a: Any, b: Any) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT,
        f"overlapping edits in {path}",
        details={"reason": "overlapping_edits", "path": str(path), "ranges": [a, b]},
    )


def _range_json(rng: Range) -> dict[str, int]:
    return rng.to_external()


def check_no_overlap(hunks: Iterable[Hunk]) -> None:
    """同じファイルの hunk が重なっていれば ``E_INVALID_ARGUMENT``（``details.reason="overlapping_edits"``）。

    隣り合う範囲（``a.end == b.start``）と、同じ位置への複数の挿入（空の範囲）は重ならない。
    ファイルごとに判定する。
    """
    by_path: dict[RelPath, list[Hunk]] = {}
    for h in hunks:
        by_path.setdefault(h.path, []).append(h)
    for path, hs in by_path.items():
        ordered = sorted(hs, key=lambda h: (h.range.start, h.range.end))
        prev: Hunk | None = None
        for h in ordered:
            if prev is not None and h.range.start < prev.range.end:
                raise _overlap_error(path, _range_json(prev.range), _range_json(h.range))
            prev = h


def _offset(idx: LineIndex, pos: Position, path: RelPath) -> int:
    """``Position``（コードポイント）→ バイトオフセット。行末を越える列は行末へ。最終行の次の行は文書末（LSP と同じ）。"""
    if pos.line >= idx.line_count:
        if pos.line == idx.line_count:
            return len(idx.data)
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            f"hunk position is beyond the end of {path}: line {pos.line + 1}",
            details={"reason": "hunk_out_of_range", "path": str(path), "line": pos.line + 1},
        )
    return idx.position_to_offset(pos).value


def _encode(text: str, newline: str) -> bytes:
    if "\n" in text or "\r" in text:
        text = _ANY_NEWLINE.sub(lambda _m: newline, text)
    return text.encode("utf-8", "surrogateescape")


def apply_hunks(source: bytes, hunks: Iterable[Hunk]) -> bytes:
    """``source`` に同じファイルの hunk をすべて適用した、新しいバイト列を返す。

    * hunk の座標はすべて適用前のもの。実装は範囲を昇順に並べて 1 回で組み立てる
      （「後ろから適用」と同じ結果。同じ位置への挿入は与えた順に並ぶ）。
    * 重なる範囲は ``E_INVALID_ARGUMENT``（``details.reason="overlapping_edits"``）。隣り合う範囲は可。
    * BOM はそのまま保つ（行 0 の列 0 は BOM の後ろ）。hunk の外のバイトには一切触れない。
    * ``new_text`` の改行（``\\n`` / ``\\r\\n`` / ``\\r``）は、``source`` の改行コード（``detect_newline``）に揃える。
      したがって対象ファイルの多数派の改行は適用前後で変わらず、``new_text`` が元の断片そのままなら
      バイト単位で一致する（CRLF / LF が揃ったファイル）。
    * ``hunks`` が空なら ``source`` をそのまま返す。複数のパスの hunk を混ぜると ``ValueError``。
    """
    hs = list(hunks)
    if not hs:
        return source
    paths = {h.path for h in hs}
    if len(paths) > 1:
        raise ValueError(f"apply_hunks takes the hunks of a single file, got {sorted(paths)}")
    path = hs[0].path
    idx = LineIndex(source)
    newline = detect_newline(source)

    spans = sorted(
        (
            (_offset(idx, h.range.start, path), _offset(idx, h.range.end, path), order, h)
            for order, h in enumerate(hs)
        ),
        key=lambda t: (t[0], t[1], t[2]),
    )
    out: list[bytes] = []
    cursor = 0
    prev: Hunk | None = None
    for start, end, _order, h in spans:
        if start < cursor:
            assert prev is not None
            raise _overlap_error(path, _range_json(prev.range), _range_json(h.range))
        out.append(source[cursor:start])
        out.append(_encode(h.new_text, newline))
        cursor = end
        prev = h
    out.append(source[cursor:])
    return b"".join(out)


# --- WorkspaceEdit → Plan --------------------------------------------------------------------


def _malformed(what: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_INTERNAL,
        f"malformed WorkspaceEdit: {what}",
        details={"reason": "malformed_workspace_edit", "what": what},
    )


def _outside(uri: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT,
        f"the edit touches a file outside the workspace: {uri}",
        details={"reason": "edit_outside_workspace", "uri": uri},
    )


def _unsupported_resource_op(kind: str, uri: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_UNSUPPORTED,
        f"WorkspaceEdit contains a file {kind}; only file renames are supported",
        details={"reason": "resource_operation", "operation": kind, "uri": uri},
    )


def _convert_position(idx: LineIndex, raw: Mapping[str, Any], unit: Unit) -> Position:
    try:
        line, character = int(raw["line"]), int(raw["character"])
    except (KeyError, TypeError, ValueError):
        raise _malformed(f"bad position {raw!r}") from None
    if line < 0 or character < 0:
        raise _malformed(f"negative position {raw!r}")
    if line >= idx.line_count:  # LSP: 文書の行数を超える行は文書末に丸める
        last = idx.line_count - 1
        return Position(last, idx.line_length(last))
    return idx.from_unit(line, character, unit).value


def _convert_range(idx: LineIndex, raw: Mapping[str, Any], unit: Unit) -> Range:
    try:
        start = _convert_position(idx, raw["start"], unit)
        end = _convert_position(idx, raw["end"], unit)
        return Range(start, end)
    except KeyError:
        raise _malformed(f"bad range {raw!r}") from None
    except ValueError as e:
        raise _malformed(f"bad range {raw!r}: {e}") from None


def from_workspace_edit(
    we: Mapping[str, Any],
    *,
    to_rel: Callable[[str], RelPath | None],
    line_index_for: Callable[[RelPath], LineIndex],
    encoding: str,
) -> Plan:
    """LSP の ``WorkspaceEdit`` を ``Plan`` にする（設計書 §7.7.2）。

    * ``documentChanges`` があればそれを、無ければ ``changes`` を使う。
    * ``to_rel(uri)``: URI → ルート相対パス。ルート外なら ``None``（``E_INVALID_ARGUMENT``、``details.reason="edit_outside_workspace"``）。
    * ``line_index_for(path)``: そのファイルの **現在の内容** の ``LineIndex``。LSP が見ていた文書と同じ内容であること。
      ``encoding="utf-8"`` のときは BOM を剥がした内容の ``LineIndex`` を渡す（LSP の列に BOM は無い）。
    * ``encoding``: LSP の列の単位（``"utf-16"`` / ``"utf-8"`` / ``"utf-32"``）。hunk はコードポイントに直す。
      文書の行数を超える位置は文書末に丸める（LSP の規定）。
    * ``RenameFile`` は ``renames`` に（hunk の ``path`` は旧パス）。``CreateFile`` / ``DeleteFile`` は ``E_UNSUPPORTED``
      （``details.reason="resource_operation"``）。同じ文書への ``TextDocumentEdit`` が 2 つ以上あれば、座標が逐次になるため
      ``E_UNSUPPORTED``（``details.reason="repeated_document_edit"``）。
    * ``newText`` は正規化しない（改行だけ ``"\\n"`` に揃える）。同じファイル内で範囲が重なれば ``E_INVALID_ARGUMENT``。
    * 戻り値の ``resolved_shas`` と ``target_urns`` は空。呼び出し側が ``dataclasses.replace`` で埋める。
    """
    try:
        unit = Unit(encoding.lower())
    except ValueError:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            f"unknown position encoding: {encoding!r}",
            details={"param": "encoding", "value": encoding},
        ) from None

    per_path: dict[RelPath, list[Hunk]] = {}
    renames: list[tuple[RelPath, RelPath]] = []
    new_to_old: dict[RelPath, RelPath] = {}
    seen_docs: set[RelPath] = set()
    index_cache: dict[RelPath, LineIndex] = {}

    def resolve(uri: str) -> RelPath:
        rel = to_rel(uri)
        if rel is None:
            raise _outside(uri)
        return rel

    def add_text_edits(uri: str, edits: Iterable[Mapping[str, Any]]) -> None:
        rel = resolve(uri)
        src = new_to_old.get(rel, rel)  # 改名後の URI への編集は、編集前の内容（旧パス）に対する hunk にする
        if src in seen_docs:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"WorkspaceEdit edits {src} more than once",
                details={"reason": "repeated_document_edit", "path": str(src)},
            )
        seen_docs.add(src)
        edits = list(edits)
        if not edits:
            return
        idx = index_cache.get(src)
        if idx is None:
            idx = index_cache[src] = line_index_for(src)
        bucket = per_path.setdefault(src, [])
        for e in edits:
            if not isinstance(e, Mapping) or "range" not in e or "newText" not in e:
                raise _malformed(f"bad TextEdit {e!r}")
            if "snippet" in e:
                raise LuciankeError(
                    ErrorCode.E_UNSUPPORTED,
                    "SnippetTextEdit is not supported",
                    details={"reason": "snippet_text_edit", "path": str(src)},
                )
            text = _ANY_NEWLINE.sub("\n", str(e["newText"]))
            bucket.append(Hunk(src, _convert_range(idx, e["range"], unit), text))

    doc_changes = we.get("documentChanges")
    changes = we.get("changes")
    if doc_changes is not None and (doc_changes or not changes):
        for item in doc_changes:
            if not isinstance(item, Mapping):
                raise _malformed(f"bad documentChanges item {item!r}")
            kind = item.get("kind")
            if kind is None:
                try:
                    uri = item["textDocument"]["uri"]
                    edits = item["edits"]
                except (KeyError, TypeError):
                    raise _malformed(f"bad TextDocumentEdit {item!r}") from None
                add_text_edits(uri, edits)
            elif kind == "rename":
                try:
                    old_uri, new_uri = item["oldUri"], item["newUri"]
                except KeyError:
                    raise _malformed(f"bad RenameFile {item!r}") from None
                old, new = resolve(old_uri), resolve(new_uri)
                old = new_to_old.get(old, old)
                renames.append((old, new))
                new_to_old[new] = old
            elif kind in ("create", "delete"):
                raise _unsupported_resource_op(kind, str(item.get("uri", "")))
            else:
                raise LuciankeError(
                    ErrorCode.E_UNSUPPORTED,
                    f"unknown WorkspaceEdit resource operation: {kind!r}",
                    details={"reason": "resource_operation", "operation": str(kind)},
                )
    elif changes:
        for uri, edits in changes.items():
            add_text_edits(uri, edits)

    hunks: list[Hunk] = []
    for hs in per_path.values():
        hs.sort(key=lambda h: (h.range.start, h.range.end))  # 安定ソート: 同じ位置への挿入は LSP の配列順
        hunks.extend(hs)
    check_no_overlap(hunks)
    return Plan(hunks=tuple(hunks), renames=tuple(renames))


# --- 統一 diff -------------------------------------------------------------------------------

_DIFF_LINE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+")


def _diff_lines(data: bytes) -> list[str]:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return _DIFF_LINE.findall(data.decode("utf-8", "replace"))


def unified_diff(path: RelPath | str, before: bytes, after: bytes, *, context: int = 3) -> str:
    """``before`` → ``after`` の統一 diff（``--- a/<path>`` / ``+++ b/<path>`` 始まり）。同一なら空文字列。

    行の区切りは ``\\r\\n`` / ``\\n`` / ``\\r`` のみ（``\\f`` などでは割らない）。行の終端は diff の中でもそのまま
    残す（CRLF のファイルの diff は CRLF のまま。改行だけの変更も差分に出る）。BOM は比べない。
    末尾に改行が無い最終行には ``\\ No newline at end of file`` を付ける。
    """
    if before == after:
        return ""
    a, b = _diff_lines(before), _diff_lines(after)
    out: list[str] = []
    for i, line in enumerate(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=context, lineterm="\n")):
        out.append(line)
        if i >= 2 and line[:1] in (" ", "-", "+") and not line.endswith(("\n", "\r")):  # 先頭 2 行は見出し
            out.append("\n\\ No newline at end of file\n")
    return "".join(out)
