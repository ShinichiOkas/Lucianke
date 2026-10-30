# BUG-0076 負荷の高いときの全体テストで、実 LSP の契約テストが時間切れになり、書き手スレッドが残って別のテストまで落ちる

- 報告: 2026-10-31 / 見た人: BUG-0071 を直す役（全体テストの実行中。同じ PC で他の役も並行で動いていた）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `8536a5f` + BUG-0071 の変更）

`uv run pytest -q -p no:cacheprovider`: `2 failed, 3811 passed, 29 skipped, 1 error in 1324.51s (0:22:04)`（いつもは約 11 分）。

- `ERROR tests/contract/test_T06_T07_lsp.py::test_t07_editing_a_file_makes_the_cache_stale`（setup）:
  `asyncio.wait_for(..., timeout=3.0)` の `TimeoutError`。
- `FAILED tests/contract/test_T04_lsp.py::test_real_exact_cold_warm_and_after_the_lsp_is_stopped`:
  `tests/contract/test_T04_lsp.py:224` `assert item["ok"] and item["confidence_level"] == "exact"` が `'structural' == 'exact'` で落ちた。
- `FAILED tests/unit/test_index_core.py::test_writer_start_failure_raises_and_leaves_no_thread`:
  `tests/unit/test_index_core.py:443` `assert not any(t.name == "lucianke-index-writer" and t.is_alive() ...)` が落ちた
  （同じプロセスに `lucianke-index-writer` スレッドが生きていた）。

3 件とも、続けてその 3 つだけを流すと通った（`9 passed in 31.41s`。`test_T06_T07_lsp.py` は全件）。

## 困ること

- 全体テストが負荷しだいで赤になる（揺れるテスト）。並行で役を走らせるほど起きやすい。
- 3 件目は、前のテストの時間切れの後始末に左右されているように見える（見ただけ）。

## 追記: 別の役の全体テストでも（2026-10-31、BUG-0067 を直した役。見たことだけ）

- 作業ツリー = `8536a5f` + BUG-0067 の直し（パッカー）。同じ PC で python のプロセスが 12 個（他の役の全体テストが並行）。
  `uv run pytest -q -p no:cacheprovider -rfs` → 3 failed, 3792 passed, 29 skipped（1257 秒）。1 件は BUG-0067 の仕様変更の結果（e2e の縦切り。直した）で、残り 2 件:
  - `tests/contract/test_T04_lsp.py::test_real_exact_cold_warm_and_after_the_lsp_is_stopped`: 上と同じ `:224` で `'structural' == 'exact'`。
  - `tests/contract/test_T13_code_action.py::test_fake_the_lsp_sees_the_new_content_after_the_write`（偽サーバー）: `:800` の
    `assert texts and texts[-1] == "def a():\n    return 2\n"` が `assert ([])`（偽サーバーが受けた `textDocument/didChange` が 0 件）。
- 2 件とも、同じ作業ツリーで単独に流すと通った（`test_T13_code_action.py` 全件 + T-04 のその 1 件 + e2e で 26 passed。落ちたのは直す前の e2e だけ）。
