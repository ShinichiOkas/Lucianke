"""スパイクのツール引数モデルとスキーマ（server.py と probe_*.py / bench.py が共有）。
使い方: from schemas import Args10, ARGS10_SCHEMA, EMPTY_SCHEMA
Args10 は仕様書 §5.2 の共通パラメータ 5 + 固有 5 = 10 フィールド、extra="forbid"（ADR-2）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Args10(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str | None = None
    revision: str = "WORKING_TREE"
    format: Literal["compact", "json", "dot", "mermaid"] = "compact"
    max_tokens: int = Field(2000, ge=200)
    cursor: str | None = None
    path: str
    symbol_name: str | None = None
    line: int | None = Field(None, ge=1)
    character: int | None = Field(None, ge=0)
    include_private: bool = False


EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
ARGS10_SCHEMA: dict[str, Any] = Args10.model_json_schema()
assert ARGS10_SCHEMA.get("additionalProperties") is False

# Args10 に正しい引数の例（bench.py が使う）
VALID_ARGS10: dict[str, Any] = {
    "path": "src/services/payment.py", "symbol_name": "PaymentService.charge", "line": 120, "character": 4,
    "max_tokens": 2000, "format": "compact", "revision": "WORKING_TREE", "include_private": False,
    "workspace_id": "app", "cursor": None,
}  # fmt: skip
