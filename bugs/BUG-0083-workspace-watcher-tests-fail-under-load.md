# BUG-0083 負荷の高いときに `test_workspace_watcher.py` の試験が落ちる（落ちる試験と落ち方が毎回変わる。揺れるテスト）

- 報告: 2026-10-31 / 見た人: BUG-0076 を直す役（負荷を掛けた試験の背景の負荷として `tests/unit` を回していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `547a320` + BUG-0076 の作業中の変更。BUG-0076 の変更は `tests/unit/test_workspace_watcher.py` と `workspace/watcher` に触れていない）

同じ PC（14 コア）で pytest を 4 本並べて流していたとき（`tests/unit tests/invariants`・`tests/contract`・`tests/unit` の一部の繰り返し・
実 LSP の契約テスト）:

1. `uv run pytest -q -p no:cacheprovider tests/unit tests/invariants` → `2 failed, 3092 passed, 18 skipped in 538.43s`
   - `FAILED tests/unit/test_workspace_watcher.py::test_git_tmp_and_excluded_paths_never_reach_the_sink`
   - `FAILED tests/unit/test_workspace_watcher.py::test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes`
2. 続けて `uv run pytest -q -p no:cacheprovider tests/unit/test_workspace_watcher.py`（他の 3 本は流れたまま）→ `1 failed, 25 passed in 21.37s`
   - `test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes`: `tests\unit\test_workspace_watcher.py:77`
     `AssertionError: timed out waiting for the condition`（試験の `until`。上限は `WAIT_S = 5.0`）
3. もう一度同じコマンド → 2 件落ちた（どの試験かは控えていない。落ち方は 1・2 と違った）:
   - `:279` `assert "a.py" not in sink.of("changed")` → `AssertionError: assert 'a.py' not in ['a.py', 'b1.py']`
   - `assert sink.of("changed") == ["zz_barrier.py"]` → `['d0/f540.py', ... ]`（`Left contains 60 more items, first extra item: 'd0/f546.py'`）
4. もう一度 → `1 failed, 25 passed in 15.27s`（`test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes`）

負荷を外してから（他の pytest がすべて終わってから）同じコマンドを 3 回 → `26 passed in 22.92s` / `26 passed in 9.39s` / `26 passed in 11.48s`。
同じ負荷の下でも、`tests/unit tests/invariants` の 2 回目・3 回目は `3094 passed` / `3098 passed`（落ちない回もある）。

## 再現手順

- 何か CPU を飽和させる負荷（例: pytest を 3 本並べて `tests/unit` などを繰り返す）を掛けながら
  `uv run pytest -q -p no:cacheprovider tests/unit/test_workspace_watcher.py` を数回流す。

## 困ること

- 全体テストを並行で流すと、負荷しだいで赤になる（揺れるテストは無価値。BUG-0076 と同じ類）。
