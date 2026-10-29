"""縦切り（T-02・T-05）が使うサービスの入れ物と、ファイルの読み込み・解析の共通処理。

``Services`` を継承し、合成ルート（``app.build_services``）が組み立てる。いまは「MCP の roots か cwd の先頭を唯一の
ワークスペースにする」最小の入れ物。``workspace/context.py``（``WorkspaceContext``）ができたら ``workspace()`` を差し替える。

compact の行の組み立て関数（``mcp/render/compact.py``）は ``tools`` から import できない（設計書 §4.2、
``tests/unit/test_import_graph.py``）ので、合成ルートが ``lines`` として注入する。ツールは ``svc.lines.symbol_line(...)`` の
ように呼ぶ。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import MAX_FILE_BYTES
from lucianke.core.model import RelPath, SymbolRecord, Tier
from lucianke.core.urn import workspace_id_from_dirname
from lucianke.syntax.parsers import TreeCache
from lucianke.syntax.support import get_support
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import ParsedFile
from lucianke.tools._base import Services
from lucianke.workspace.languages import HEAD_BYTES, detect
from lucianke.workspace.paths import confine, to_abs
from lucianke.workspace.types import Workspace

# 構文木だけでシンボルを取り出せる言語（symbols.py の対応言語）
SYMBOL_LANGUAGES = frozenset({"python", "typescript", "tsx", "javascript"})


@dataclass(slots=True)
class ToolServices(Services):
    """T-02 / T-05 が使うサービス。

    Attributes:
        roots: 現在のルート（MCP の roots か cwd）を返す関数。先頭を唯一のワークスペースにする。
        trees: 構文木キャッシュ。
        lines: compact の行の組み立て関数のモジュール（``lucianke.mcp.render.compact``）。
    """

    roots: Callable[[], Sequence[str]]
    trees: TreeCache
    lines: ModuleType
    _workspaces: dict[str, Workspace] = field(default_factory=dict)

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        roots = list(self.roots())
        if not roots:
            raise LuciankeError(ErrorCode.E_WORKSPACE_NOT_CONFIGURED, "no workspace root is known (no MCP roots, no cwd)")
        real = os.path.realpath(roots[0])
        ws = self._workspaces.get(real)
        if ws is None:
            root = Path(real)
            ws = Workspace(workspace_id=workspace_id_from_dirname(root.name), root=root)
            self._workspaces[real] = ws
        if workspace_id is not None and workspace_id != ws.workspace_id:
            raise LuciankeError(
                ErrorCode.E_WORKSPACE_NOT_CONFIGURED,
                f"unknown workspace_id {workspace_id!r} (the only workspace is {ws.workspace_id!r})",
                details={"workspace_id": workspace_id, "known": [ws.workspace_id]},
            )
        return ws

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        return self.workspace(workspace_id).workspace_id


def batch_warnings(failed: int, total: int) -> tuple[str, ...]:
    """バッチ入力の一部失敗の件数（仕様書 §5.4.4）。全件成功なら空。全件失敗は呼び出し側がエラーにする。"""
    if failed <= 0:
        return ()
    return (f"{failed} of {total} items failed (see the failed items for the error of each)",)


def services_of(obj: Services) -> ToolServices:
    if not isinstance(obj, ToolServices):
        raise TypeError("ctx.services is not a ToolServices (composition root must pass app.build_services())")
    return obj


@dataclass(frozen=True, slots=True)
class FileAnalysis:
    """読み込み・解析済みのファイル 1 つ。"""

    rel: RelPath
    data: bytes
    sha256: str
    language_id: str
    tier: Tier
    parsed: ParsedFile
    symbols: tuple[SymbolRecord, ...]

    @property
    def line_count(self) -> int:
        return self.parsed.line_index.content_line_count


async def analyze_file(svc: ToolServices, ws: Workspace, user_path: str, *, param: str = "path") -> FileAnalysis:
    """``confine`` → 読み込み → 言語判定 → 解析（``TreeCache``）→ ``symbols.extract``。失敗は ``LuciankeError``。"""
    rel = confine(ws, user_path, param=param)
    abs_path = to_abs(ws, rel)
    try:
        is_file = abs_path.is_file()
    except OSError:
        is_file = False
    if not is_file:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND, f"file not found: {rel}", details={"path": str(rel), "param": param}
        )
    size = abs_path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"file too large to parse: {size} bytes > {MAX_FILE_BYTES}",
            details={"reason": "file_too_large", "size": size, "limit": MAX_FILE_BYTES},
        )
    data = await asyncio.to_thread(abs_path.read_bytes)
    det = detect(rel, data[:HEAD_BYTES])
    if not det.index_content:
        reason = "secret_file" if det.is_secret else "binary_file"
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED, f"{rel}: content is not readable ({reason})", details={"reason": reason}
        )
    language_id = det.language_id
    spec = get_support(language_id)
    if language_id not in SYMBOL_LANGUAGES or spec is None or not hasattr(spec, "grammar"):
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"{rel}: language {language_id!r} has no structural support in this build "
            f"(supported: {', '.join(sorted(SYMBOL_LANGUAGES))})",
            details={"reason": "language_not_supported", "language_id": language_id},
        )
    sha256 = hashlib.sha256(data).hexdigest()
    parsed = await svc.trees.aget_or_parse(ws.workspace_id, rel, sha256, language_id, lambda: data)
    symbols = extract(
        parsed,
        language_id=language_id,
        language=spec.grammar(),
        query_source=load_query_source(language_id),
        workspace_id=ws.workspace_id,
        rel=rel,
    )
    return FileAnalysis(
        rel=rel,
        data=data,
        sha256=sha256,
        language_id=language_id,
        tier=det.tier,
        parsed=parsed,
        symbols=tuple(symbols),
    )


def estimate_tokens(text: str) -> int:
    """縮退の判定に使う、やや多めの推定（パッカーの推定器は ``mcp`` にあり import できない。最終判定はパッカーが行う）。"""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return -(-(ascii_chars * 10 + (len(text) - ascii_chars) * 9) // 30)
