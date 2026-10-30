# BUG-0088 負荷の高いときに `tests/unit/test_edit_transaction.py` の試験が 37 件まとめて ERROR になった（1 回だけ。揺れるテスト）

- 報告: 2026-10-31 / 見た人: BUG-0083 を直す役（watcher の変更の巻き添えが無いかを確かめる試験を流していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = origin/main `0fee57d` + BUG-0083 の作業中の変更。BUG-0083 は `workspace/watcher.py` だけを変え、
`test_edit_transaction.py` は偽の watcher（`FakeWatcher`）を使う）

同じ PC（14 コア）で、別の pytest を 3 本（`tests/unit tests/invariants`・`tests/contract`・`tests/unit` の索引/編集の重いファイルの
繰り返し）を**流し始めた直後**に:

- `uv run pytest -q -p no:cacheprovider tests/unit/test_tools_runtime.py tests/unit/test_edit_transaction.py tests/unit/test_core_limits.py`
  → `27 passed, 37 errors in 86.40s`
  - 出力の最後に見えた 2 行: `ERROR tests/unit/test_edit_transaction.py::test_zero_wait_answers_pending_without_waiting`、
    `ERROR tests/unit/test_edit_transaction.py::test_without_an_lsp_the_delta_is_the_tree_sitter_error_difference`
  - エラーの本文は控えていない（`tail -3` しか見ていなかった）。37 = `test_edit_transaction.py` の 41 件のほとんど（ERROR なので
    セットアップか後始末で落ちている）。
- 直後に同じ負荷の下で `test_edit_transaction.py` だけ → `41 passed`。同じ 3 ファイルを 4 回 → 4 回とも `64 passed`。

## 再現手順（未確認）

- 別の pytest を何本か同時に**起動しながら**、上の 3 ファイルを流す。

## 困ること

- 全体テストを並行で流すと、負荷しだいで赤になりうる（揺れるテストは無価値）。
