# BUG-0117 Python の受け手の注釈 `x: A | B`・`Union[A, B]` を `A` と読み、`x.run()` を `A.run` に `structural` で結ぶ

- 報告: 2026-10-31 / 見た人: BUG-0113・BUG-0114 を直す役（TS の受け手の注釈を先頭の名前に縮める箇所を探していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0113・BUG-0114 の直しを載せたコード）

```
m.py
from typing import Union


class A:
    def run(self) -> int:
        return 1


class B:
    def run(self) -> int:
        return 2


def f(x: A | B, y: Union[A, B], z: "B | A") -> int:
    return x.run() + y.run() + z.run()
```

- `relations` に `m.py#f` → `m.py#A.run`（`CALLS`・`structural`）が 2 行ある（15 行 14 列の `x.run()`・24 列の `y.run()`）。`x`・`y` の型は
  `A | B` で、`B.run` かもしれない箇所が `A.run` の確かな行になっている。`B.run` への行は無い。
- `z: "B | A"`（前方参照の文字列）の `z.run()` は `_name_calls` の名前だけの行になる（`run`・15 行 34 列）。
- TS の同じ形（`w: Opts | Retry`）は BUG-0113 の直しで型の分からない受け手（名前だけ）になった。

## 利用者（呼ぶ側の AI）として困ること

- T-06 で `A.run` の参照を見ると、`B` の値でも呼ばれうる箇所が `structural`（確かな行）として混ざり、`B.run` の参照からは抜ける。
