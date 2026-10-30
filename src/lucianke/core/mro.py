"""クラスのメンバーを探す順序（Python の C3 線形化。BUG-0075）。

``syntax/relations.py``（同じファイルの中だけで分かる継承）と ``index/inherited.py``（索引の ``INHERITS`` 行をたどる、ファイルを
またいだ継承）が同じ順序でメンバーを探すための共通部品。TS / JS は単一継承（``extends`` 1 つ）なので C3 は左から右の深さ優先と同じ。

外部の基底（``lk://_ext/...``）は中身が分からないので、``bases_of`` が ``[]`` を返す葉として扱う（呼び手が「そこで止める」か
「素通りする」かを決める）。
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from typing import Final

__all__ = ["MAX_MRO_DEPTH", "TRANSPARENT_EXTERNAL_BASES", "c3_linearize"]

MAX_MRO_DEPTH: Final = 32
"""継承をたどる深さの上限（循環・異常に深い階層で止まらないため）。"""

TRANSPARENT_EXTERNAL_BASES: Final = frozenset(
    {
        "lk://_ext/builtins#object",
        "lk://_ext/typing#Generic",
        "lk://_ext/typing#Protocol",
        "lk://_ext/typing_extensions#Protocol",
        "lk://_ext/abc#ABC",
        "lk://_ext/globals#Object",
    }
)
"""ユーザーのメソッド名を定義しない外部の基底。継承をたどるとき、ここで止めずに次の基底へ進む
（``class Base(ABC)``・``class Box(Generic[T], Base)`` のメンバーを探せるようにするため）。それ以外の外部の基底は、
そのクラスが同名を定義しているかもしれないので、そこで探すのをやめる（別の定義に誤って結ばない）。"""


def c3_linearize[T: Hashable](
    start: T, bases_of: Callable[[T], Sequence[T]], *, max_depth: int = MAX_MRO_DEPTH
) -> list[T] | None:
    """``start`` の C3 線形化（``start`` 自身が先頭）。基底の並びが矛盾する・循環する・深すぎるときは ``None``。

    ``bases_of(c)`` は ``c`` の直接の基底を書いた順に返す（無ければ ``[]``）。同じ ``c`` について何度も呼ぶので、呼び手が覚えておく。
    """
    memo: dict[T, list[T] | None] = {}
    visiting: set[T] = set()

    def lin(cls: T, depth: int) -> list[T] | None:
        if cls in memo:
            return memo[cls]
        if depth > max_depth or cls in visiting:
            return None
        visiting.add(cls)
        try:
            bases: list[T] = []
            for b in bases_of(cls):
                if b not in bases and b != cls:
                    bases.append(b)
            seqs: list[list[T]] = []
            for b in bases:
                sub = lin(b, depth + 1)
                if sub is None:
                    return None
                seqs.append(list(sub))
            seqs.append(list(bases))
            merged = _merge(seqs)
            result = None if merged is None else [cls, *merged]
        finally:
            visiting.discard(cls)
        memo[cls] = result
        return result

    return lin(start, 0)


def _merge[T: Hashable](seqs: list[list[T]]) -> list[T] | None:
    out: list[T] = []
    seqs = [s for s in seqs if s]
    while seqs:
        for seq in seqs:
            head = seq[0]
            if not any(head in s[1:] for s in seqs):
                break
        else:
            return None  # 矛盾した並び（Python ならクラスを作るときに TypeError）
        out.append(head)
        seqs = [s[1:] if s[0] == head else s for s in seqs]
        seqs = [s for s in seqs if s]
    return out
