"""F-POLY フィクスチャを生成する（仕様書 §14.2）。バイト列を正確に書くため、すべて bytes で書き出す。"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])

FILES: dict[str, str | bytes] = {}

FILES["README.md"] = """# F-POLY（合成フィクスチャ）

仕様書 §14.2 の F-POLY。Tier 1〜4 のファイルと、意図的な型エラー・循環呼び出し・同名の再定義・非 ASCII 識別子・
CRLF / BOM / 末尾改行なしのファイルを含む。**バイト列そのものが検査対象なので、エディタで保存し直さない**
（`.gitattributes` で `tests/fixtures/** -text`）。生成元は `tests/fixtures/make_fpoly.py`。

| ファイル | 目的 |
|---|---|
| src/shop/services/payment.py | クラス・メソッド・デコレータ・docstring・ガード節・意図的な型エラー・未使用 import |
| src/shop/services/limits.py | 別ファイルからの参照、循環呼び出し（retry と schedule） |
| src/shop/api/routes.py | ガード条件付きの呼び出し（if / try） |
| src/shop/models/receipt.py | 同名の再定義（条件分岐内）、非 ASCII 識別子・絵文字 |
| src/shop/models/crlf_bom.py | CRLF・BOM あり |
| src/shop/models/no_final_newline.py | 末尾改行なし |
| tests/test_payment.py | テストからの参照 |
| notebooks/eda.ipynb | 出力（base64 画像・大きな stream。文字列 OUTPUT_SENTINEL を含む）を持つ Notebook |
| reports/junit.xml | 40 件中 2 件が失敗の JUnit XML |
| db/schema.sql, proto/order.proto, docs/flow.dot, docs/flow.mmd | Tier 2 |
| conf/settings.json, conf/app.yaml, pyproject.toml, templates/receipt.j2, docs/api.rst | Tier 3 |
| legacy/calc.cob, conf/rules.custom | Tier 4（文法なし） |
| web/src/*.ts, web/tsconfig.json | TypeScript（Tier 1）。意図的な型エラーとオーバーロード宣言 |
| conf/.env | 秘密ファイル（本文を索引しない。仕様書 §13.5） |
"""

FILES["pyproject.toml"] = """[project]
name = "shop"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "pydantic>=2",
  "httpx>=0.27",
]

[tool.ruff]
line-length = 100
"""

for p in ["src/shop/__init__.py", "src/shop/services/__init__.py", "src/shop/api/__init__.py",
          "src/shop/models/__init__.py"]:
    FILES[p] = ""

FILES["src/shop/services/payment.py"] = '''"""決済サービス。"""

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
'''

FILES["src/shop/services/limits.py"] = '''"""通貨ごとの上限。"""

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
'''

FILES["src/shop/api/routes.py"] = '''"""HTTP 経路（フレームワーク非依存の形）。"""

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
'''

FILES["src/shop/models/receipt.py"] = '''"""領収書。"""

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
'''

FILES["src/shop/models/crlf_bom.py"] = (
    b"\xef\xbb\xbf"
    + '"""CRLF and BOM fixture."""\n\n\ndef crlf_function(x: int) -> int:\n    """Doubles x."""\n    return x * 2\n\n\n'
      'class CrlfClass:\n    def method(self) -> str:\n        return "crlf"\n'.replace("\n", "\r\n").encode("utf-8")
)

FILES["src/shop/models/no_final_newline.py"] = b"def no_newline_at_end(value: int) -> int:\n    return value + 1"

FILES["tests/test_payment.py"] = '''from decimal import Decimal

import pytest

from shop.services.limits import LimitTable
from shop.services.payment import PaymentProcessor, PaymentRequest


@pytest.mark.asyncio
async def test_charge_ok():
    p = PaymentProcessor(LimitTable())
    r = await p.process_charge(PaymentRequest(order_id="o1", amount=Decimal("100")))
    assert r.order_id == "o1"
'''

png = base64.b64encode(bytes(range(256)) * 40).decode()
nb = {
    "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": ["# 売上データの探索\n", "\n", "この Notebook はフィクスチャ。"]},
        {"cell_type": "code", "execution_count": 3, "metadata": {"tags": []}, "outputs": [],
         "source": ["import json\n", "from pathlib import Path"]},
        {"cell_type": "code", "execution_count": 12, "metadata": {}, "outputs": [
            {"name": "stdout", "output_type": "stream",
             "text": [f"OUTPUT_SENTINEL_STREAM row {i}\n" for i in range(300)]},
            {"data": {"image/png": png, "text/plain": ["<Figure OUTPUT_SENTINEL_IMAGE>"]}, "metadata": {},
             "output_type": "display_data"},
        ], "source": [
            "def load_frame(path: str) -> list[dict]:\n",
            '    """JSON Lines を読む。"""\n',
            "    rows = []\n",
            '    for line in Path(path).read_text(encoding="utf-8").splitlines():\n',
            "        rows.append(json.loads(line))\n",
            "    return rows\n",
            "\n",
            'frame = load_frame("data.jsonl")',
        ]},
        {"cell_type": "code", "execution_count": 13, "metadata": {}, "outputs": [
            {"output_type": "execute_result", "execution_count": 13,
             "data": {"text/plain": ["OUTPUT_SENTINEL_RESULT 42"]}, "metadata": {}}],
         "source": ["len(frame)"]},
    ],
    "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                 "language_info": {"name": "python"}},
    "nbformat": 4, "nbformat_minor": 5,
}
FILES["notebooks/eda.ipynb"] = json.dumps(nb, ensure_ascii=False, indent=1) + "\n"

cases = []
for i in range(40):
    if i in (7, 23):
        cases.append(f'    <testcase classname="tests.test_payment" name="test_case_{i}" time="0.01">'
                     f'<failure message="assert 1 == 2">Traceback FAILURE_SENTINEL_{i}</failure></testcase>')
    else:
        cases.append(f'    <testcase classname="tests.test_payment" name="test_case_{i}" time="0.01"/>')
FILES["reports/junit.xml"] = ('<?xml version="1.0" encoding="utf-8"?>\n<testsuites>\n'
                              '  <testsuite name="pytest" tests="40" failures="2" errors="0" time="0.40">\n'
                              + "\n".join(cases) + "\n  </testsuite>\n</testsuites>\n")

FILES["db/schema.sql"] = """CREATE TABLE orders (
  order_id TEXT PRIMARY KEY,
  amount NUMERIC NOT NULL CHECK (amount > 0),
  currency TEXT NOT NULL DEFAULT 'JPY'
);

CREATE TABLE receipts (
  receipt_id INTEGER PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  issued_at TEXT NOT NULL
);
"""

FILES["proto/order.proto"] = """syntax = "proto3";

package shop.v1;

message ChargeRequest {
  string order_id = 1;
  string amount = 2;
  string currency = 3;
}

message ChargeReply {
  string receipt_id = 1;
}

service Payment {
  rpc Charge (ChargeRequest) returns (ChargeReply);
}
"""

FILES["docs/flow.dot"] = """digraph order_flow {
  rankdir=LR;
  subgraph cluster_api { label="api"; create_order; retry_order; }
  create_order -> process_charge [label="CALLS"];
  retry_order -> process_charge [label="CALLS"];
  process_charge -> validate_limit;
}
"""

FILES["docs/flow.mmd"] = """flowchart TD
  A[create_order] --> B[process_charge]
  C[retry_order] --> B
  B --> D{validate_limit}
  D -->|ok| E[Receipt]
  D -->|ng| F[PaymentDeclined]
"""

FILES["docs/api.rst"] = """決済 API
========

.. py:module:: shop.services.payment

:class:`PaymentProcessor` の :func:`process_charge` を呼ぶ。上限は :class:`shop.services.limits.LimitTable` を参照。
"""

FILES["conf/settings.json"] = json.dumps({
    "service": {"name": "shop", "port": 8080},
    "limits": [{"currency": "JPY", "max": 1000000}, {"currency": "USD", "max": 10000},
               {"currency": "EUR", "max": 9000}],
    "features": {"discount": True, "retry": {"max_attempts": 3}},
}, indent=2) + "\n"

FILES["conf/app.yaml"] = """service:
  name: shop
  workers: 4
retry:
  max_attempts: 3
  backoff_ms: [100, 200, 400]
"""

FILES["conf/.env"] = "API_TOKEN=SECRET_SENTINEL_do_not_index\n"

FILES["templates/receipt.j2"] = """領収書 {{ receipt.order_id }}
金額: {{ receipt.amount }} {{ context.currency }}
{% if context.user_id %}お客様番号: {{ context.user_id }}{% endif %}
"""

FILES["legacy/calc.cob"] = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. CALC.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-AMOUNT PIC 9(7)V99.
       01 WS-TAX    PIC 9(7)V99.
       PROCEDURE DIVISION.
       MAIN-PARA.
           PERFORM COMPUTE-TAX.
           PERFORM WRITE-REPORT.
           STOP RUN.
       COMPUTE-TAX.
           COMPUTE WS-TAX = WS-AMOUNT * 0.10.
       WRITE-REPORT.
           DISPLAY "TAX: " WS-TAX.
"""

FILES["conf/rules.custom"] = """# 独自 DSL（文法なし）
rule MAX_RETRY = 3
rule BACKOFF = MAX_RETRY * 100
when order.amount > LIMIT then decline
"""

FILES["web/tsconfig.json"] = """{
  "compilerOptions": { "target": "ES2022", "module": "ES2022", "strict": true, "outDir": "dist" },
  "include": ["src"]
}
"""

FILES["web/src/payment.ts"] = """export interface PaymentRequest {
  orderId: string;
  amount: number;
  currency: "JPY" | "USD";
}

export class PaymentClient {
  constructor(private readonly baseUrl: string) {}

  charge(req: PaymentRequest): Promise<string>;
  charge(req: PaymentRequest, retries: number): Promise<string>;
  /** 決済を要求する。amount は 0 より大きいこと。 */
  async charge(req: PaymentRequest, retries = 0): Promise<string> {
    if (req.amount <= 0) {
      throw new Error("amount must be > 0");
    }
    return `${this.baseUrl}/charge/${req.orderId}?r=${retries}`;
  }
}
"""

FILES["web/src/app.ts"] = """import { PaymentClient } from "./payment";

const client = new PaymentClient("https://example.invalid");

export async function checkout(orderId: string): Promise<string> {
  const total: number = "100"; // 意図的な型エラー
  return client.charge({ orderId, amount: total, currency: "JPY" });
}
"""


def main() -> None:
    for rel, content in FILES.items():
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        path.write_bytes(data)
    print(f"wrote {len(FILES)} files under {ROOT}")


if __name__ == "__main__":
    main()
