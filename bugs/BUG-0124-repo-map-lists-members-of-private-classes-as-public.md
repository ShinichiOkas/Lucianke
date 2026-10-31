# BUG-0124 get_repo_map が非公開のクラスのメンバーを公開シンボルとして地図に出す（`_DbLock.hold`）

- 報告: 2026-10-31 / 見た人: BUG-0120 を直した役（能力ベンチの T01 の地図を読んでいて）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`main` = `ba80b1e`、Mnemo の固定の写し、`get_repo_map` 既定）

- `src/mnemo/storage/db.py` のブロックの先頭のシンボルが `[Method] pub _DbLock.hold(self, what: str, sql: str | None = None) -> "_Hold" @L1162-1163`。
- 索引（`symbols`）では `_DbLock`（1098 行の Class）の `modifiers_json` が `["priv"]`、そのメソッド `_DbLock.__init__` / `acquire` / `release` / `locked` / `__enter__` / `__exit__` / `hold` は `["pub"]`（`_should_warn` / `_report` は `["priv"]`）。
- 地図はクラスを公開でないとして外し、メソッドだけを親の名前つき（`_DbLock.hold`）で、ファイルの公開シンボルの上位として並べている。

## 利用者（呼ぶ側の AI）として困ること

- モジュールの外から使わない内部のクラスのメソッドが「このファイルの重要な公開 API」として地図の限られた行を使う。
- `pub` の印が付くので、外から呼んでよいものと誤解しうる。
