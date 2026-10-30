# BUG-0094 import したモジュール直下のインスタンス `settings` の `settings.reload()` が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0085 を直す役（受け手の決め方を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。`8c10a2a` のコードでも BUG-0085 の直しを載せたコードでも同じ）

```
pkg/conf.py   class Settings:\n    def reload(self):\n        return 1\n\n\nsettings = Settings()
use.py        from pkg.conf import settings\n...\ndef f():\n    settings.reload()
```

- 抽出器（`syntax/relations.extract_relations`、import 表 `{"settings": "lk://ws/pkg/conf.py#settings"}`）の候補は
  `CALLS reload -> lk://ws/pkg/conf.py#settings.reload`（`name_only=False`）。
- 索引した後の `relations` に `use.py#f` の `reload` の行は無く、`_name_calls` にも無い（`use.py` の `CALLS` は 0 行、ワークスペース全体の
  `_name_calls` は別の 1 行だけ）。
- `pkg/conf.py#settings.reload` という URN のシンボルは無い（`settings` は変数）。

## 利用者（呼ぶ側の AI）として困ること

- `Settings.reload` を変える前に T-06 / T-07 で呼び出し元を見ても、`from x import logger` / `settings` のような共有インスタンス経由の呼び出しは
  `structural` でも名前の一致（`heuristic`）でも出ず、警告も無い。
