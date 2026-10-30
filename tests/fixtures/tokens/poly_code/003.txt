"""HTTP 経路（フレームワーク非依存の形）。"""

from __future__ import annotations

from decimal import Decimal

from shop.services.limits import LimitTable
from shop.services.payment import PaymentProcessor, PaymentRequest

processor = PaymentProcessor(LimitTable())


async def create_order(order_id: str, amount: str, paid: bool = False):
    req = PaymentRequest(order_id=order_id, amount=Decimal(amount))
    if req.amount > 0 and not paid:
        receipt = await processor.process_charge(req)
        return {"receipt": receipt.order_id}
    return {"receipt": None}


async def retry_order(order_id: str, amount: str, attempt: int):
    req = PaymentRequest(order_id=order_id, amount=Decimal(amount))
    try:
        if attempt < 3:
            receipt = await processor.process_charge(req)
            return receipt
    except ValueError:
        return None
    return None
