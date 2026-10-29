"""Tier 3 データファイルのアダプタ（設計書 §7.4.8）。1 フォーマット 1 ファイル。

``ADAPTERS`` は拡張子（小文字・ドット付き）→ ``DataAdapter``（JSON / JSONL / YAML / TOML / XML）。``REGISTERED`` は
``syntax.support.register_adapter`` に渡すアダプタの一覧（``.ipynb`` を含む）で、``syntax/support.py`` が import の末尾で 1 回だけ登録する。
このパッケージは ``syntax.support`` を import しない（登録の向きは ``support`` → ``adapters`` の一方向で、循環しない）。
各モジュールの ``skeleton(source, opts, *, rel, sha256)`` と ``select(source, selector)`` を直接呼んでもよい。
"""

from __future__ import annotations

from lucianke.syntax.adapters import ipynb, json_, toml_, xml_, yaml_
from lucianke.syntax.adapters._common import DataAdapter, SelectorError, UnsupportedSelectorError

_ALL: tuple[DataAdapter, ...] = (
    json_.ADAPTER,
    json_.JSONL_ADAPTER,
    yaml_.ADAPTER,
    toml_.ADAPTER,
    xml_.ADAPTER,
)

ADAPTERS: dict[str, DataAdapter] = {ext: a for a in _ALL for ext in a.extensions}

REGISTERED: tuple[DataAdapter | ipynb.IpynbAdapter, ...] = (*_ALL, ipynb.ADAPTER)
"""``support.register_adapter`` に渡す全アダプタ（``language_id`` ごとに 1 つ）。"""

__all__ = ["ADAPTERS", "REGISTERED", "DataAdapter", "SelectorError", "UnsupportedSelectorError"]
