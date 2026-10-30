# BUG-0099 T-13 の種別指定 `quickfix` が、文脈の診断の待ち（2 秒）を超えると「QuickFix は無い」（`E_TARGET_NOT_FOUND`）と答える

- 報告: 2026-10-31 / 見た人: BUG-0097 を直す役（T-13 の試験を負荷の下で流していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（origin/main `18da337` の `tests/contract/test_T13_code_action.py`）

1. CPU を飽和させる負荷（14 コアの PC で忙しく回るプロセス 28 個）の下で
   `uv run pytest -q -p no:cacheprovider -W always tests/contract/test_T13_two_servers.py tests/contract/test_T13_code_action.py` を 3 回
   → 2 回目だけ `1 failed, 25 passed`:
   `test_real_dry_run_writes_nothing_and_predicts_the_sha` で
   `{'code': 'E_TARGET_NOT_FOUND', 'message': "no code action of kind 'quickfix' is offered at src/shop/unused.py:1", 'details': {'reason': 'no_code_action', 'kind': 'quickfix', 'path': 'src/shop/unused.py', 'line': 1}, 'candidates': []}`
   （ファイルは `import os\nimport sys\n…`。1 行目の `import os` は未使用で、ruff は F401 を出す）。
2. 負荷の代わりに、試験の側で ruff の `publishDiagnostics` を 3 秒遅らせて `DiagnosticsHub.on_publish` に渡すと（順序は保つ）、
   同じファイルの実機の QuickFix の試験 9 本が同じ `E_TARGET_NOT_FOUND`（`no_code_action`）で落ちる。
   - `src/lucianke/tools/t13_apply_code_action.py` の `CONTEXT_DIAGNOSTICS_WAIT_MS` は 2000。

## 困ること

- 呼び手（AI エージェント）は「この行に QuickFix は無い」と読む（`E_TARGET_NOT_FOUND` は確定の答え）。実際は診断が届くのが
  遅れただけで、少し後に聞けば候補が出る。LSP を起動した直後・負荷の高いときに起こりうる。
