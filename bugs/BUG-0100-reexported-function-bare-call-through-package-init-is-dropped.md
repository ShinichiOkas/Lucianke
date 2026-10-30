# BUG-0100 `__init__.py` が再び公開した関数を `from pkg import flush` で import した `flush()` が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0093・BUG-0094・BUG-0095 を直す役（import した変数の受け手を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0093〜0095 の直しを載せたコード）

```
pkg/__init__.py   from pkg.impl import flush\n\n__all__ = ["flush"]
pkg/impl.py       def flush():\n    return 0
use.py            from pkg import flush\n\n\ndef f():\n    flush()\n    return flush
```

- `use.py` の `relations` は 0 行（`flush()` の `CALLS` も、`return flush` の `REFERENCES` も、`IMPORTS` 以外は何も無い）。
- `use.py` の `_name_calls` も 0 行。
- `pkg/__init__.py#flush` という URN のシンボルは無い（import は `symbols` にならない）。
- 同じ形で Lucianke 自身の `EST = TokenEstimator()`（`tests/unit/test_mcp_budget.py`。`TokenEstimator` は `mcp/budget.py` が
  `core/tokens.py` から再び公開している）の `EST.count(...)` は、BUG-0094 の直しで名前だけの呼び出し（`builtin_name`）になったが、
  `core/tokens.py#TokenEstimator.count` への `structural` の行にはならない。

## 利用者（呼ぶ側の AI）として困ること

- `pkg/impl.py#flush` を変える前に T-06 / T-07 で呼び出し元を見ても、パッケージの `__init__.py` 経由で import した呼び出し元は
  `structural` でも `heuristic` でも出ず、警告も無い（パッケージの公開 API を `__init__.py` で再び公開するのはよくある書き方）。
