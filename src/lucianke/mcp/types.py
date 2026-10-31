"""MCP 層の内部で共有する型（設計書 §7.1・§7.2）。

budget（パッカー）・render（レンダラ）・pipeline（パイプライン）がこの型だけで繋がる。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lucianke.core.errors import LuciankeError
from lucianke.core.records import Record, ResolutionMeta, ToolOutput


@dataclass(frozen=True, slots=True, kw_only=True)
class Page:
    """パッカーの結果（仕様書 §5.6）。``records`` は予算内に収めたレコード（``tokens`` を埋めたもの）。"""

    records: tuple[Record, ...]
    next_cursor: str | None
    summary: str | None  # ``# summary:`` の本文（接頭辞なし）。続きが無ければ None
    total_estimate: int  # 範囲内のレコード総数（ツールが与えた総数があればそれ）
    remaining: int  # このページに入らなかったレコード数
    used_tokens_est: int  # ヘッダ・警告・トレーラを含む応答全体の推定
    max_tokens: int
    truncated: bool
    warnings: tuple[str, ...] = ()
    # 1 件も入らない（前置きだけで超える）ページだけ: 呼び直しで少なくとも 1 件が返る（応答が収まる）``max_tokens`` の目安。
    # 警告の「raise max_tokens to ~N」の N と同じ値（``mcp/fit.py`` が描いた全文で確かめて上げる。BUG-0110）。それ以外は None
    need_max_tokens: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CallInfo:
    """ヘッダ行（仕様書 §5.4.2）とエンベロープに載せる呼び出しの情報。"""

    tool: str
    revision: str  # "WORKING_TREE" など
    workspace_id: str | None
    format: str  # "compact" | "json" | "dot" | "mermaid"
    max_tokens: int
    timing_ms: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderInput:
    """レンダラへの入力。成功時は ``out`` と ``page``、失敗時は ``error``。"""

    call: CallInfo
    meta: ResolutionMeta
    out: ToolOutput | None = None
    page: Page | None = None
    error: LuciankeError | None = None
    warnings: Sequence[str] = ()
    records_key: str = "items"  # json の data でレコード配列を置くキー（ツールごと。例: "references"）


@dataclass(frozen=True, slots=True, kw_only=True)
class Rendered:
    """レンダラの出力。``text`` は ``content[0].text``、``structured`` は ``structuredContent``（json のときだけ）。"""

    text: str
    structured: Mapping[str, Any] | None = None
    is_error: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)
