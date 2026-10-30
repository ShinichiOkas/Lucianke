"""領収書。"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Receipt:
    order_id: str
    amount: Decimal


if sys.version_info >= (3, 12):
    def format_receipt(r: Receipt) -> str:
        return f"{r.order_id}: {r.amount}"
else:
    def format_receipt(r: Receipt) -> str:
        return "%s: %s" % (r.order_id, r.amount)


def 合計金額(receipts: list[Receipt]) -> Decimal:
    """非 ASCII 識別子のフィクスチャ。👍 を含む docstring。"""
    total = Decimal(0)
    for r in receipts:
        total += r.amount  # 👍 絵文字の後ろの識別子: total
    return total
