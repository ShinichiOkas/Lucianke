"""予算・ページネーション・カーソル（設計書 §7.2.1〜7.2.3、仕様書 §5.6・§5.7）。

【仮実装】署名は本実装と同じ。本実装は M1 タスク 4b の役が置き換える。
"""

from __future__ import annotations

from dataclasses import dataclass

from lucianke.core.records import ToolOutput
from lucianke.mcp.types import Page


class TokenEstimator:
    """o200k_base 互換の近似（仕様書 §5.7）。【仮実装】4 文字 = 1 トークン。"""

    def count(self, text: str) -> int:
        return max(1, (len(text) + 3) // 4) if text else 0


@dataclass(frozen=True, slots=True, kw_only=True)
class CursorState:
    tool: str
    args_hash: str
    offset: int
    data_version: int
    workspace_id: str | None


class CursorCodec:
    """``next_cursor`` の符号化と復号（設計書 §7.2.3）。【仮実装】"""

    def encode(self, *, tool: str, args: dict, offset: int, data_version: int, workspace_id: str | None) -> str:
        raise NotImplementedError("CursorCodec.encode (M1 task 4b)")

    def decode(self, token: str, *, tool: str, args: dict, data_version: int, workspace_id: str | None) -> CursorState:
        raise NotImplementedError("CursorCodec.decode (M1 task 4b)")


class Packer:
    """レコード列を予算内に詰める（設計書 §7.2.2）。【仮実装】全部入れる。"""

    def __init__(self, estimator: TokenEstimator | None = None) -> None:
        self.estimator = estimator or TokenEstimator()

    def pack(self, out: ToolOutput, *, max_tokens: int, offset: int = 0, reserved_tokens: int = 0,
             cursor_factory=None) -> Page:
        recs = tuple(r.with_tokens(self.estimator.count("\n".join(r.lines))) for r in out.records[offset:])
        used = reserved_tokens + sum(r.tokens or 0 for r in recs)
        return Page(records=recs, next_cursor=None, summary=None, total_estimate=len(out.records),
                    remaining=0, used_tokens_est=used, max_tokens=max_tokens, truncated=False)
