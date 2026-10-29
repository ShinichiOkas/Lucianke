"""T-06 ``find_references_and_implementations`` と T-07 ``query_call_hierarchy`` が共有する部品。

PaaD（``relations`` / ``symbols``）から答える ``structural`` 版のための道具をまとめる。

* ``reader``: ``ctx.services.db_path``（T-20 と同じ約束。duck typing）から読み取り接続を開く。
* ``TargetRefArg`` / ``resolve_target``: 仕様書 §5.3 の TargetRef を **索引** で解決する（T-05 は構文木で解決する）。
* ``SourceLines``: 参照箇所の行のテキストをファイルから読む（スニペット用）。
* ``use_of`` / ``enclosing_name_of``: ``relations`` の行から ``use`` と囲みの名前を決める。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import CANDIDATES_MAX
from lucianke.core.model import RelPath, SymbolRecord
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import CodeSel, Urn, parse
from lucianke.index import queries
from lucianke.index.connection import open_reader
from lucianke.tools._base import ToolContext
from lucianke.workspace.paths import confine, to_abs
from lucianke.workspace.types import Workspace

SELECTOR_PREFIXES = ("xpath:", "jsonpath:", "jq:", "toml:", "yaml:", "cell:")

# ``use_kind`` が NULL の行の既定（relation_kind から決める）
DEFAULT_USE: dict[str, str] = {
    "CALLS": "call",
    "IMPORTS": "import",
    "TYPE_USES": "type",
    "INHERITS": "impl",
    "IMPLEMENTS": "impl",
}
USE_VALUES = ("r", "w", "rw", "call", "type", "import", "impl")


class TargetRefArg(BaseModel):
    """仕様書 §5.3 の TargetRef。外部の ``line`` / ``character`` は 1 始まり。"""

    model_config = ConfigDict(extra="forbid")

    urn: str | None = Field(default=None, description='e.g. "lk://app/src/a.py#Cls.method"')
    path: str | None = Field(default=None, description="Workspace-relative path with '/'.")
    symbol_name: str | None = Field(
        default=None, description='"name", "Outer.name" (suffix match on qualified_name).'
    )
    line: int | None = Field(
        default=None, ge=1, description="1-based line; selects the innermost symbol containing it."
    )
    character: int | None = Field(default=None, ge=1, description="1-based column (optional with line).")


def db_path_of(ctx: ToolContext) -> Path:
    raw = getattr(ctx.services, "db_path", None)
    if raw is None:
        raise LuciankeError(
            ErrorCode.E_WORKSPACE_NOT_CONFIGURED, "the index database is not configured for this server"
        )
    path = Path(raw)
    if not path.is_file():
        raise LuciankeError(
            ErrorCode.E_WORKSPACE_NOT_CONFIGURED,
            "the index database does not exist yet (no workspace has been indexed)",
            details={"reason": "index_missing"},
        )
    return path


def workspace_of(ctx: ToolContext, workspace_id: str | None) -> Workspace:
    workspace = getattr(ctx.services, "workspace", None)
    if workspace is None:
        raise LuciankeError(ErrorCode.E_WORKSPACE_NOT_CONFIGURED, "no workspace is known")
    ws: Workspace = workspace(workspace_id)
    return ws


@contextmanager
def reader(db_path: Path) -> Iterator[sqlite3.Connection]:
    """読み取り接続を 1 本開いて閉じる（``asyncio.to_thread`` の中で使う）。"""
    conn = open_reader(db_path)
    try:
        yield conn
    finally:
        conn.close()


# --- 対象の解決（仕様書 §5.3） ----------------------------------------------------------------


def candidate(s: SymbolRecord) -> dict[str, Any]:
    return {"urn": str(s.urn), "signature": s.signature, "path": str(s.path), "line": s.range.start.line + 1}


def _ambiguous(name: str, found: list[SymbolRecord]) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_TARGET_AMBIGUOUS,
        f"{name!r} matches {len(found)} symbols; use a longer symbol_name, a urn, or a line",
        candidates=[candidate(s) for s in found[:CANDIDATES_MAX]],
    )


def _not_found(message: str, **details: Any) -> LuciankeError:
    return LuciankeError(ErrorCode.E_TARGET_NOT_FOUND, message, details=details)


def resolve_target(conn: sqlite3.Connection, ws: Workspace, ref: TargetRefArg) -> SymbolRecord:
    """``urn`` > ``line`` > ``symbol_name`` の順（§5.3 規則 4）。解決できなければ ``LuciankeError``。"""
    ws_id = ws.workspace_id
    if ref.urn is not None:
        try:
            urn = parse(ref.urn)
        except ValueError as exc:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT, f"target.urn: {exc}", details={"param": "target.urn"}
            ) from None
        if not isinstance(urn.selector, CodeSel):
            raise _not_found(f"urn does not name a code symbol: {ref.urn}", urn=ref.urn)
        sym = queries.symbol_by_urn(conn, urn)
        if sym is not None:
            return sym
        if urn.is_external or urn.workspace_id != ws_id:
            raise _not_found(f"no symbol with urn {ref.urn}", urn=ref.urn)
        qualified = urn.selector.qualified  # arity / sighash の違いは緩めて探す
        found = [
            s
            for s in queries.symbols_by_qualified_suffix(conn, ws_id, urn.path, qualified)
            if s.qualified_name == qualified
        ]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise _ambiguous(ref.urn, found)
        raise _not_found(f"no symbol with urn {ref.urn}", urn=ref.urn)
    if ref.path is None:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            "target: give urn, or path with symbol_name or line",
            details={"param": "target"},
        )
    rel = confine(ws, ref.path, param="target")
    if queries.file_by_path(conn, ws_id, str(rel)) is None:
        raise _not_found(f"file is not in the index: {rel}", path=str(rel), param="target")
    if ref.line is not None:
        sym = queries.symbol_at(
            conn, ws_id, str(rel), ref.line - 1, None if ref.character is None else ref.character - 1
        )
        if sym is None:
            raise _not_found(f"no symbol contains line {ref.line} of {rel}", path=str(rel), line=ref.line)
        return sym
    if ref.symbol_name is not None:
        if ref.symbol_name.startswith(SELECTOR_PREFIXES):
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"selector {ref.symbol_name.split(':', 1)[0]}: is not supported (code symbols only)",
                details={"reason": "selector_not_supported"},
            )
        found = queries.symbols_by_qualified_suffix(conn, ws_id, str(rel), ref.symbol_name)
        exact = [s for s in found if s.qualified_name == ref.symbol_name]
        if len(exact) == 1:
            return exact[0]
        found = exact or found
        if not found:
            raise _not_found(
                f"symbol {ref.symbol_name!r} not found in {rel}", path=str(rel), symbol_name=ref.symbol_name
            )
        if len(found) > 1:
            raise _ambiguous(ref.symbol_name, found)
        return found[0]
    raise LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT,
        "target: path needs symbol_name or line (or use urn)",
        details={"param": "target"},
    )


# --- 参照箇所の行 -----------------------------------------------------------------------------


class SourceLines:
    """参照箇所の行のテキストをファイルから読む。ファイルごとに ``LineIndex`` を 1 度だけ作る。"""

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws
        self._cache: dict[str, LineIndex | None] = {}

    def text(self, path: str, line0: int) -> str:
        """``path`` の ``line0``（0 始まり）行目の前後の空白を除いた文。読めなければ空文字列。"""
        if path not in self._cache:
            try:
                self._cache[path] = LineIndex(to_abs(self._ws, RelPath(path)).read_bytes())
            except (OSError, ValueError):
                self._cache[path] = None
        index = self._cache[path]
        if index is None or not 0 <= line0 < index.content_line_count:
            return ""
        return index.line_text(line0).strip()


# --- 関係の行から ---------------------------------------------------------------------------


def use_of(use_kind: str | None, relation_kind: str) -> str:
    """``use`` は 7 値のいずれか。DB の ``use_kind`` が NULL・未知なら ``relation_kind`` から決める。"""
    if use_kind in USE_VALUES:
        return str(use_kind)
    return DEFAULT_USE.get(relation_kind, "r")


def enclosing_name_of(source_urn: str) -> str | None:
    """参照元（囲み）の URN の ``qualified``。モジュール直下（ファイルの URN）なら ``None``。"""
    try:
        urn = parse(source_urn)
    except ValueError:
        return None
    return urn.selector.qualified if isinstance(urn.selector, CodeSel) else None


def file_urn(workspace_id: str, path: str) -> str:
    return str(Urn(workspace_id, path))
