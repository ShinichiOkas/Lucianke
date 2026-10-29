from decimal import Decimal

import pytest

from shop.services.limits import LimitTable
from shop.services.payment import PaymentProcessor, PaymentRequest


@pytest.mark.asyncio
async def test_charge_ok():
    p = PaymentProcessor(LimitTable())
    r = await p.process_charge(PaymentRequest(order_id="o1", amount=Decimal("100")))
    assert r.order_id == "o1"
