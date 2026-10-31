# BUG-0122 型の分かる受け手の別ファイルのクラスのインスタンス属性の読み（`a.skipped`）が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0117 を直す役（Python の受け手の注釈の読み方を直した前後で Lucianke 自身を索引して比べていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0117 の直しの前と後のコードで同じ）

```
pkg/__init__.py   （空）
pkg/lx.py
class Src:
    def __init__(self) -> None:
        self.skipped = 0

    def go(self) -> int:
        return self.skipped

pkg/use.py
from pkg import lx
from pkg.lx import Src


def h(a: Src, b: lx.Src) -> int:
    return a.skipped + b.skipped + a.go()
```

- `relations` の `pkg/use.py#h` の行は `pkg/lx.py`（`lx` の import の参照）と `pkg/lx.py#Src.go`（`CALLS`・`structural`）だけ。
- `a.skipped`（6 行 12 列）と `b.skipped`（6 行 24 列）は `relations` にも `_name_calls` にも無い（`REFERENCES` の行も名前だけの行も無い）。
- Lucianke 自身では、BUG-0117 の直しで `source: lx.CallEdgeSource | None` の受け手の型が分かるようになった
  `src/lucianke/tools/t07_query_call_hierarchy.py#handler` の `source.from_lsp`（755 行）・`source.skipped`（830・831 行）・`source.warnings`
  （833 行）の 4 か所が、直す前の `_name_calls`（名前だけの行）から消え、`relations` にも入らなかった（`CallEdgeSource.skipped` などは
  `__init__` の中の `self.skipped = 0` のインスタンス属性で、シンボルではない）。

## 利用者（呼ぶ側の AI）として困ること

- T-06 でインスタンス属性 `skipped` の参照を探しても、型の分かる受け手からの読みが 1 件も出ない（型の分からない受け手の読みは
  BUG-0095 の名前の一致で出るのに、型が分かるほうが取りこぼす）。
