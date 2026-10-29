"""T-02 のデータファイルの骨格（Tier 3 の JSON・JSONL・YAML・TOML・XML と Tier 2 の ``.ipynb``。仕様書 §11 T-02 処理規約 3〜5）。

``syntax.support`` に登録されたデータ形式アダプタ（``get_adapter``）の ``skeleton_record`` を呼ぶ。コードの言語は
``_services.analyze_file`` が受け持つので、このモジュールはアダプタのある拡張子・ファイル名のときだけ答え、それ以外は ``None`` を返す
（呼び手がコードのパスに回す）。構文木は作らない。

注意: json / yaml / toml / xml は文法（``LanguageSpec``）も持つので ``get_support`` ではアダプタを引けない。``get_adapter`` を使う。
読み込みの検査（存在・大きさ・秘密/バイナリ）は ``analyze_file`` と同じ規約で、ここに写している（``_services.py`` は結線の役の持ち場）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import RelPath, Tier
from lucianke.core.records import Record
from lucianke.syntax.parsers import parse_source
from lucianke.syntax.support import SUPPORT, FormatAdapter, get_adapter, language_for_extension
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import SkeletonOptions
from lucianke.workspace.languages import HEAD_BYTES, detect
from lucianke.workspace.paths import confine, to_abs
from lucianke.workspace.types import Workspace

__all__ = ["DATA_FILE_MAX_BYTES", "DataSkeleton", "adapter_for", "data_skeleton"]

# 仕様書 §8.6 B4: 5 MB 超のデータファイルも、深さと型推論の標本を絞って骨格だけ返す（アダプタが絞る）。丸ごと読むので上限は置く。
DATA_FILE_MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DataSkeleton:
    """データファイル 1 つの骨格。``record`` は ``file-block``（``priority`` は呼び手が付けた順）。"""

    record: Record
    language_id: str
    tier: Tier


def adapter_for(rel: str) -> FormatAdapter | None:
    """ファイル名・拡張子から、骨格を作れるデータ形式アダプタを引く。無ければ ``None``（コードのパス）。

    まず ``workspace.languages.detect`` の言語（``uv.lock`` → toml など、名前で決まるもの）、次に拡張子。
    OpenAPI と判定された ``openapi.yaml`` は ``detect`` の言語にアダプタが無いので、拡張子（yaml）に落ちる。
    """
    adapter = get_adapter(detect(RelPath(rel), b"").language_id)
    if adapter is not None:
        return adapter
    dot = rel.rfind(".")
    ext = rel[dot:] if dot > rel.rfind("/") + 1 else ""
    lid = language_for_extension(ext) if ext else None
    return get_adapter(lid) if lid is not None else None


def _python_symbols(workspace_id: str, rel: RelPath, sha256: str) -> Callable[[bytes], Sequence[Any]]:
    """``.ipynb`` の仮想 Python ファイル → 定義の一覧（スレッドの中で呼ばれる）。木はキャッシュしない（仮想ファイルの木なので）。"""

    def symbols(virtual: bytes) -> Sequence[Any]:
        spec = SUPPORT["python"]
        parsed, _timed_out = parse_source(virtual, "python", sha256)
        return extract(
            parsed,
            language_id="python",
            language=spec.grammar(),
            query_source=load_query_source("python"),
            workspace_id=workspace_id,
            rel=rel,
        )

    return symbols


async def data_skeleton(
    ws: Workspace, user_path: str, opts: SkeletonOptions, *, priority: int, param: str = "paths"
) -> DataSkeleton | None:
    """``user_path`` がデータ形式なら骨格を作る。コードのパスなら ``None``。失敗は ``LuciankeError``（項目単位）。

    構文が壊れたファイルは ``E_UNSUPPORTED``（``details.reason == "invalid_data_file"``。``E_SYNTAX_INVALID`` は編集の検証用）。
    """
    rel = confine(ws, user_path, param=param)
    adapter = adapter_for(str(rel))
    if adapter is None:
        return None
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
    if size > DATA_FILE_MAX_BYTES:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"file too large to parse: {size} bytes > {DATA_FILE_MAX_BYTES}",
            details={"reason": "file_too_large", "size": size, "limit": DATA_FILE_MAX_BYTES},
        )
    data = await asyncio.to_thread(abs_path.read_bytes)
    det = detect(rel, data[:HEAD_BYTES])
    if not det.index_content:
        reason = "secret_file" if det.is_secret else "binary_file"
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED, f"{rel}: content is not readable ({reason})", details={"reason": reason}
        )
    sha256 = hashlib.sha256(data).hexdigest()
    python_symbols = _python_symbols(ws.workspace_id, rel, sha256) if adapter.language_id == "ipynb" else None
    try:
        record = await asyncio.to_thread(
            adapter.skeleton_record,
            data,
            opts,
            rel=str(rel),
            sha256=sha256,
            python_symbols=python_symbols,
        )
    except (ValueError, RecursionError) as e:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"{rel}: cannot be read as {adapter.language_id}: {e}",
            details={"reason": "invalid_data_file", "language_id": adapter.language_id},
        ) from e
    return DataSkeleton(dataclasses.replace(record, priority=priority), adapter.language_id, adapter.tier)
