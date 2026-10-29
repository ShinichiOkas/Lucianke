"""通貨ごとの上限。"""

from __future__ import annotations

from decimal import Decimal


class LimitExceeded(Exception):
    pass


class LimitTable:
    def __init__(self, table: dict[str, Decimal] | None = None) -> None:
        self._table = table or {"JPY": Decimal("1000000"), "USD": Decimal("10000")}

    def get(self, currency: str) -> Decimal | None:
        return self._table.get(currency)


def retry(attempt: int) -> int:
    """schedule と互いに呼び合う（循環呼び出しのフィクスチャ）。"""
    if attempt >= 3:
        return attempt
    return schedule(attempt + 1)


def schedule(attempt: int) -> int:
    return retry(attempt)
