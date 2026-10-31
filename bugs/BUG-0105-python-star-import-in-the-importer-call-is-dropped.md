# BUG-0105 Python の `from helpers import *` で取り込んだ `flush` の `flush()` が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0100・BUG-0101 を直す役（`from x import *` の再公開をたどる直しの範囲の外を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0100 の直しを載せたコード）

```
helpers.py    def flush():\n    return 0
use_star.py   from helpers import *\n\n\ndef run():\n    return flush()
```

- `use_star.py` の `relations` は 0 行（`flush()` の `CALLS` が無い）。`_name_calls` も 0 行。
- `syntax/imports.wildcard_sources` は `src/` のどこからも呼ばれていない（grep）。BUG-0100 の直しは、`__init__.py` などの **再公開する側**
  の `from .x import *` だけをたどる（`pkg/__init__.py` の `from .stars import *` を経由した `from pkg import starred` は結ばれる）。

## 利用者（呼ぶ側の AI）として困ること

- `helpers.py#flush` を変える前に T-06 / T-07 で呼び出し元を見ても、`from helpers import *` で使っている呼び出し元は出ず、警告も無い。
