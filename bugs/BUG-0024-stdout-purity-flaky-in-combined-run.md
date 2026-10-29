# BUG-0024 tests/invariants/test_stdout_purity.py が、ほかのテストと一緒の実行（負荷あり）で断続的に落ちる

- 報告: 2026-10-29 / 見た人: 21b（M2・M3 のツールの入口）
- 見たこと（15 本ほどの役が並行している作業ツリー。自分の変更は `tools/` に新規ファイルを足しただけ）:
  - `uv run pytest tests/contract/test_unsupported_tools.py tests/unit/test_tools_autoregister.py tests/unit/test_import_graph.py tests/invariants -q` を 6 回続けた。
  - 1 回目: `test_roots_are_requested_after_initialized_and_stdout_stays_pure` が `tests/invariants/test_stdout_purity.py:259`（`assert rc == 0 and dt < EXIT_DEADLINE_S`）で落ちた（237 passed / 1 failed、65 s）。
  - 2 回目: green（238 passed、77 s）。
  - 3 回目: `test_stdout_stays_pure_jsonrpc_even_when_a_tool_writes_to_fd1` が `tests/invariants/test_stdout_purity.py:92`（`request` 内の `assert msg is not None`＝「server closed stdout before answering …」）で落ちた（237 passed / 1 failed、92 s）。
  - 4〜6 回目（同じ組み合わせ）: すべて green（238 passed、73〜143 s）。
  - `tests/invariants/test_stdout_purity.py` だけを 5 回続けた実行は、5 回とも green（4 passed、9〜18 s）。
- 落ちた 2 件は別のテストで、どちらも実プロセスの `lucianke` を起動して stdout/終了を見る箇所。BUG-0020（断続的な Connection closed）と同じ系統に見える（調べてはいない）。
- 期待: 不変条件のテスト（NFR-OPS-1）は負荷に関係なく同じ結果。
- 再現手順: 上のコマンドを、ほかの pytest／役が並行している状態で繰り返す（6 回中 2 回）。
- 調査はしていない（直す役の仕事）。
