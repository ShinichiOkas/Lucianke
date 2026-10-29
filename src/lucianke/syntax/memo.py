"""内容（SHA-256）をキーにした小さな LRU（BUG-0027）。

骨格（``skeleton.build_file_record``）とシンボル抽出（``symbols.extract_detailed``）は、同じ内容のファイルに対して何度呼んでも
同じ結果になる純粋な計算で、2,000 行のファイルで 1 回 10 ms 前後かかる。呼び出しごとに作り直さず、直近の数十ファイル分だけ
覚えておく。木（``ParsedFile``）は持たない（木のメモリは ``TreeCache`` が数えて上限をかけている）。値は不変の記録だけを入れる。

スレッドから呼んでよい（``asyncio.to_thread`` の中と索引器のスレッド）。ロックは辞書の出し入れの間だけ持つ。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Hashable
from typing import Any

__all__ = ["BoundedMemo", "content_key"]


def content_key(source: bytes) -> bytes:
    """``source`` の内容の指紋（16 バイト）。``ParsedFile.sha256`` は呼び手が付けた値で、内容と一致する保証が無いので、キーには内容そのものから作る。"""
    return hashlib.blake2b(source, digest_size=16).digest()


class BoundedMemo:
    """件数と重み（呼び手が決める。シンボル数など）の両方で上限をかける LRU。上限を超える 1 件は覚えない。"""

    def __init__(self, max_entries: int, max_weight: int) -> None:
        self._max_entries = max_entries
        self._max_weight = max_weight
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[Any, int]] = OrderedDict()
        self._weight = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> Any | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry[0]

    def put(self, key: Hashable, value: Any, weight: int = 1) -> None:
        if weight > self._max_weight:
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._weight -= old[1]
            self._entries[key] = (value, weight)
            self._weight += weight
            while self._entries and (
                len(self._entries) > self._max_entries or self._weight > self._max_weight
            ):
                _, (_, w) = self._entries.popitem(last=False)
                self._weight -= w

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._weight = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
