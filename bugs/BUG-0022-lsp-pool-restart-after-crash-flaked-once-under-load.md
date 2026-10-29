# BUG-0022 test_restart_after_crash_with_backoff が負荷のある全体実行で 1 回だけ落ちた

- 報告: 2026-10-29 / 見た人: BUG-0007 を直す役
- 見たこと:
  - `uv run pytest tests/contract/test_T08*.py tests/unit/test_lsp_pool.py tests/unit/test_edit_transaction.py tests/unit/test_import_graph.py -q` を、15 本ほどの役が並行している共有作業ツリーで実行（所要 115 秒。単独なら 13 秒前後）。結果は `1 failed, 116 passed`。落ちたのは `tests/unit/test_lsp_pool.py::test_restart_after_crash_with_backoff`。
  - キャプチャされたログ: `WARNING lucianke.lsp.client: [fakels] LSP disconnected: server closed stdout (exit code None)` / `WARNING lucianke.lsp.pool: LSP fake/fakels crashed; restart 1/3 in 0.05s`。どの `assert` で落ちたかは出力が切れて控えていない。
  - 落ちたときの `lsp/diagnostics.py`・`lsp/pool.py` は BUG-0007 の変更後（診断の書き込み待ち）。このテストは診断を使わない（`--silent --crash-first 2`、`ready_fallback_s=0.3`）。
  - 再現を試みた結果: 同じテスト単独 3 回 + 12 本の CPU 負荷の下で 6 回、`test_lsp_pool.py` 全体を単独で 3 回、同時 6 本で 1 回ずつ、すべて green（再現せず）。
- 再現手順: 上の全体実行（負荷のあるとき）。確実な手順は未確定。
- 調査はしていない（直す役の仕事）。
