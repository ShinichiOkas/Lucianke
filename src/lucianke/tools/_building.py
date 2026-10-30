"""索引の走査（初回・差分）の途中の応答（BUG-0070。仕様書 §5.3 規則 6、§5.8）。

呼ぶ側の AI は ``E_TARGET_NOT_FOUND`` を「指定の誤り」と読み、別の探し方へ移る。走査がまだ届いていないだけのファイルを「無い」と
言わないために:

* ``ensure_indexed`` / ``ensure_target_indexed`` / ``ensure_targets_indexed``: 走査の途中で、対象のファイルがまだ索引に無ければ、
  解決の前にその 1 ファイルを先取りする（``ingest_ahead``。1 ファイルの解析は数 ms）。対象外（除外・``.gitignore``）・ディスクに
  無いファイルは従来どおり（呼び手の解決が ``E_TARGET_NOT_FOUND`` を返す。それは正しい）。先取りに失敗したら、再試行を促す
  ``E_TIMEOUT``（``details.reason: "index_building"``・進み・``retry_after_ms``）にする。``E_TARGET_NOT_FOUND`` にはしない。
* ``building_warning``: 索引を読むツール（``ToolSpec.reads_index``）の応答に、パイプラインが進みつきの警告を添える
  （``index building: 412/1300 files (32%); ...``）。0 件の検索も、索引の途中なら本当の 0 件と見分けられる。

サービスは duck typing（``index_building(workspace_id)`` / ``ingest_ahead(ws, rel)``。``tools._runtime.WiredServices``）。
持たないサービス（テストの偽物・束の無い構成）では何もしない。走査を終えた後は何も変えない。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.urn import parse
from lucianke.engine.indexer import ScanProgress
from lucianke.tools._base import ToolContext
from lucianke.workspace.paths import confine
from lucianke.workspace.types import Workspace

REASON = "index_building"
RETRY_AFTER_MS = 1000


def building_of(services: object, workspace_id: str | None) -> ScanProgress | None:
    """そのワークスペースの索引が走査の途中なら進み。終えている・分からないときは ``None``。"""
    fn = getattr(services, "index_building", None)
    if fn is None:
        return None
    try:
        progress = fn(workspace_id)
    except Exception:  # noqa: BLE001  進みは付加情報。取れなくても応答は返す
        return None
    return progress if isinstance(progress, ScanProgress) else None


def progress_text(p: ScanProgress) -> str:
    if p.files_total is None:
        return "listing files"
    return f"{p.files_done}/{p.files_total} files ({p.percent}%)"


def building_warning(p: ScanProgress) -> str:
    return f"index building: {progress_text(p)}; results may be incomplete - call again when done"


def building_error(p: ScanProgress, path: str) -> LuciankeError:
    """先取りできなかった対象の、再試行を促すエラー（``E_TIMEOUT`` の ``index_opening`` と同じ形。BUG-0046）。"""
    return LuciankeError(
        ErrorCode.E_TIMEOUT,
        f"{path} is not indexed yet (index building: {progress_text(p)}); retry in a moment",
        details={
            "reason": REASON,
            "path": path,
            "retry_after_ms": RETRY_AFTER_MS,
            "files_done": p.files_done,
            "files_total": p.files_total,
        },
    )


def ref_path(ws: Workspace, ref: Any) -> str | None:
    """TargetRef の指すファイル（ワークスペース相対）。解けない指定は ``None``（エラーは呼び手の解決が出す）。"""
    urn_text = getattr(ref, "urn", None)
    if urn_text is not None:
        try:
            urn = parse(urn_text)
        except ValueError:
            return None
        if urn.is_external or urn.workspace_id != ws.workspace_id:
            return None
        return str(urn.path)
    raw = getattr(ref, "path", None)
    if raw is None:
        return None
    try:
        return str(confine(ws, raw, param="target"))
    except LuciankeError:
        return None


async def ensure_indexed(ctx: ToolContext, ws: Workspace, path: str | None) -> None:
    """走査の途中で ``path`` がまだ索引に無ければ、先に取り込む。先取りできなければ ``building_error``。"""
    if path is None or path in ("", "."):
        return
    progress = building_of(ctx.services, ws.workspace_id)
    ahead = getattr(ctx.services, "ingest_ahead", None)
    if progress is None or ahead is None:
        return
    try:
        await ahead(
            ws, path
        )  # "present" / "indexed" / "skipped"（対象外・ディスクに無い: 呼び手の解決に任せる）
    except LuciankeError:
        raise
    except Exception:
        ctx.log.warning("could not index %s ahead of the running scan", path, exc_info=True)
        raise building_error(building_of(ctx.services, ws.workspace_id) or progress, path) from None


async def ensure_target_indexed(ctx: ToolContext, ws: Workspace, ref: Any) -> None:
    await ensure_indexed(ctx, ws, ref_path(ws, ref))


async def ensure_targets_indexed(
    ctx: ToolContext, ws: Workspace, refs: Sequence[Any]
) -> dict[int, LuciankeError]:
    """バッチの TargetRef ごとに ``ensure_target_indexed``。失敗は項目の添字ごとに返す（項目単位の失敗。仕様書 §5.4.4）。"""
    failed: dict[int, LuciankeError] = {}
    for i, ref in enumerate(refs):
        try:
            await ensure_target_indexed(ctx, ws, ref)
        except LuciankeError as err:
            failed[i] = err
    return failed
