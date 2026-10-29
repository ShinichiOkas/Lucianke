"""Tier 3 データファイルのアダプタ（設計書 §7.4.8）。1 フォーマット 1 ファイル。

``ADAPTERS`` は拡張子（小文字・ドット付き）→ ``FormatAdapter``。``syntax.support.register_adapter`` に渡す登録は別の役が行う
（ここでは登録表を触らない）。各モジュールの ``skeleton(source, opts, *, rel, sha256)`` と ``select(source, selector)`` を直接呼んでもよい。
"""

from __future__ import annotations

from lucianke.syntax.adapters import json_, toml_, xml_, yaml_
from lucianke.syntax.adapters._common import DataAdapter, SelectorError, UnsupportedSelectorError

_ALL: tuple[DataAdapter, ...] = (
    json_.ADAPTER,
    json_.JSONL_ADAPTER,
    yaml_.ADAPTER,
    toml_.ADAPTER,
    xml_.ADAPTER,
)

ADAPTERS: dict[str, DataAdapter] = {ext: a for a in _ALL for ext in a.extensions}

__all__ = ["ADAPTERS", "DataAdapter", "SelectorError", "UnsupportedSelectorError"]
