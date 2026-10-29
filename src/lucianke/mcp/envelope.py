"""``_resolution_meta`` の組み立て（設計書 §7.2、仕様書 §5.5、ADR-5）。

【仮実装】署名は本実装と同じ。本実装は M1 タスク 4c の役が置き換える。
"""

from __future__ import annotations

from lucianke.core.model import Confidence, Tier
from lucianke.core.records import ResolutionMeta, ToolOutput
from lucianke.mcp.types import Page


def build_meta(out: ToolOutput | None, page: Page | None, *, tier: Tier = Tier.T1,
               detected_language: str = "n/a") -> ResolutionMeta:
    """【仮実装】精度はツールの最高水準をそのまま使う。"""
    level = out.max_confidence if out is not None else Confidence.EXACT
    engine = out.engine.engine if out is not None else "n/a"
    lsp = str(out.engine.lsp_status) if out is not None else "n/a"
    return ResolutionMeta(confidence_level=level, is_degraded=False, tier=tier,
                          detected_language=detected_language, engine_used=engine, lsp_status=lsp)
