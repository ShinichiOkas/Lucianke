# BUG-0097 負荷の高いときに T-13 の 2 サーバーの試験で、basedpyright の診断が `resolved` に入らなかった（1 回。揺れるテスト）

- 報告: 2026-10-31 / 見た人: BUG-0087・BUG-0088 を直す役（編集の契約テストを流していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = origin/main `525bd6e` + BUG-0087・BUG-0088 の作業中の変更）

同じ PC で `tests/contract_fixtures/test_fx_edit_T10_T13.py tests/unit/test_tools_runtime.py`（実 LSP を使う）を流している間に:

- `uv run pytest -q -p no:cacheprovider tests/contract/test_T10_*.py tests/contract/test_T11_*.py tests/contract/test_T13_code_action.py tests/contract/test_T13_two_servers.py tests/contract/test_unsupported_tools.py tests/unit/test_edit_writer.py tests/unit/test_core_limits.py`
  → `1 failed, 321 passed, 1 skipped in 422.66s`
  - `tests/contract/test_T13_two_servers.py::test_a_ruff_quickfix_reports_the_basedpyright_diagnostics_it_resolved` の `:122`
    `assert ("basedpyright", "reportMissingImports") in resolved, nd`
  - `nd` = `{'status': 'complete', 'added': [], 'resolved': [{'path': 'pkg/unused.py', 'severity': 'warning', 'source': 'ruff', 'code': 'F401', ...}], 'checked_files': [{'path': 'pkg/unused.py', 'fresh': True}]}`
    （`resolved` は ruff の F401 だけ。basedpyright の 2 件が無い。`status` は `complete`）
  - 捕まえたログに `reindex failed` などの警告は無い。
- 直後に同じファイルだけを 3 回 → 3 回とも `1 passed`（10〜13 秒）。
- 全文は `tests/conftest.py` の記録（BUG-0088 の見張り）で `%TEMP%\lucianke-test-errors\20261031-075330-4820.log` に残った。

## 困ること

- 全体テストを並行で流すと、負荷しだいで赤になりうる（揺れるテストは無価値）。
