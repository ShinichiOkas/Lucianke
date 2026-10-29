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

## 調査メモ（BUG-0020 の役。2026-10-29 15:15）
- 落ちた 2 件のうち「終了に 2 秒を超えた」（:259）は、BUG-0020 の空回り（原因 1）とは別。BUG-0020 を直した作業ツリーでも、`tests/invariants/test_stdout_purity.py` を 8 並列で走らせた 1 回（24 本中 5 本）で同じ箇所が落ちた（終了までの時間が 2.19〜2.23 秒、終了コードは 0。**止まったのではなく遅かった**）。そのあとの 8 並列×2 回と 10 並列×3 回（計 76 本）は全部 green だった。
- 見たこと: このテストはサーバーを cwd（＝リポジトリ直下）で起動するので、cwd がワークスペースになり、起動直後に全ファイルの走査が走る。終了は走査中のバッチの完了待ちになるので、負荷が高いと 2 秒を超える。この不変条件（stdout の純度）に索引の負荷は関係ない。
- 直し: `RawSession` が空の tmp ディレクトリを cwd にして起動する（`tests/invariants/test_stdout_purity.py`）。空の cwd にしたあと 10 並列×3 回（30 本）が green。ただし 2.2 秒の遅れが再現した回は 1 回だけで、空の cwd で遅れが消えたことまでは確かめられていない（手元の負荷が下がっていた）。
- 「`server closed stdout`」（:92）の 1 件は再現していない（76 本中 0 回）。診断していない。参考: 共有の作業ツリーで起動すると、ほかの役が書きかけの `src/` を読んで起動・ツール呼び出しが落ちることがある（15:01 に `get_file_skeleton` が `NameError` で `E_INTERNAL`、15:09 に `bench/run_latency.py` が起動の 2 秒後に `server closed stdout`。いずれも次の試行では直っていた）。
- この票は閉じない（残り: 空の cwd で遅れが消えるかの確認、`server closed stdout` の再現）。
