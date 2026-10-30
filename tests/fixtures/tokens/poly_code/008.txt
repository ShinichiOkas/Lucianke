"""決済サービス。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from shop.models.receipt import Receipt
from shop.services.limits import LimitExceeded, LimitTable


def transactional(fn):
    """トランザクション境界を示す印（何もしない）。"""
    return fn


@dataclass
class PaymentRequest:
    """決済要求。amount > 0 が必須。"""

    order_id: str
    amount: Decimal
    currency: str = "JPY"


class PaymentDeclined(Exception):
    pass


class PaymentProcessor:
    """注文の決済を担う。"""

    def __init__(self, limits: LimitTable) -> None:
        self._limits = limits
        self.currency = "JPY"

    @transactional
    async def process_charge(self, req: PaymentRequest) -> Receipt:
        """注文に対する決済を実行し領収書を発行する。

        Args:
            req: 決済要求。amount は 0 より大きいこと。

        Raises:
            PaymentDeclined: 上限を超えたとき。
        """
        if req.amount <= 0:
            raise ValueError("amount must be > 0")
        if not self._validate_limit(req.amount):
            raise PaymentDeclined(req.order_id)
        return Receipt(order_id=req.order_id, amount=req.amount)

    def _validate_limit(self, amount: Decimal) -> bool:
        # 通貨ごとの上限を確認する
        limit = self._limits.get(self.currency)
        if limit is None:
            raise LimitExceeded(self.currency)
        return amount <= limit

    def apply_discount(self, price: Decimal, rate: int) -> Decimal:
        """割引後の価格を返す。rate は 0 以上 100 以下。"""
        factor: Decimal = "1"  # 意図的な型エラー（str を Decimal に）
        return price * (100 - rate) / 100 * Decimal(factor)
