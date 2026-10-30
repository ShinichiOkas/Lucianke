# BUG-0093 `import pkg.helpers` の `pkg.helpers.flush()`（ワークスペースのモジュールの関数）が名前だけの呼び出しになり、どこにも結ばれない

- 報告: 2026-10-31 / 見た人: BUG-0085 を直す役（受け手の決め方を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。`8c10a2a` のコードでも BUG-0085 の直しを載せたコードでも同じ）

```
pkg/__init__.py   （空）
pkg/helpers.py    def flush():\n    return 0
use.py            import pkg.helpers\n...\ndef f():\n    pkg.helpers.flush()
```

- `relations` に `use.py#f` → `pkg/helpers.py#flush` の `CALLS` 行は無い。
- `_name_calls` に `('lk://obs/use.py#f', 'flush', 'use.py', 6, 17)` が入る（受け手の型が分からない属性呼び出しの扱い）。
- その行の名前の判定（`tools/_name_calls.NameIndex.verdict_for`）は `None`（候補なし。`flush` という `Method` が無い）。
- 同じ関数を `from pkg import helpers` の `helpers.flush()` で呼ぶと `structural` の `pkg/helpers.py#flush` になる（BUG-0085 の単体テスト
  `test_module_receiver_calls_the_module_function` と同じ形）。外部の `import os` の `os.path.join(...)` は `lk://_ext/os#path.join` になる。

## 利用者（呼ぶ側の AI）として困ること

- `flush` を変える前に T-06 / T-07 で呼び出し元を見ても、`pkg.helpers.flush()` の形の呼び出し元は `structural` でも `heuristic` でも出ず、
  警告も無い（モジュールの関数の呼び出しが「型の分からない受け手のメソッド呼び出し」として扱われている）。
