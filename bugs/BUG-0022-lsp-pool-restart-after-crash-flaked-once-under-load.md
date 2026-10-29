# BUG-0022 test_restart_after_crash_with_backoff が負荷のある全体実行で 1 回だけ落ちた

- 報告: 2026-10-29 / 見た人: BUG-0007 を直す役
- 見たこと:
  - `uv run pytest tests/contract/test_T08*.py tests/unit/test_lsp_pool.py tests/unit/test_edit_transaction.py tests/unit/test_import_graph.py -q` を、15 本ほどの役が並行している共有作業ツリーで実行（所要 115 秒。単独なら 13 秒前後）。結果は `1 failed, 116 passed`。落ちたのは `tests/unit/test_lsp_pool.py::test_restart_after_crash_with_backoff`。
  - キャプチャされたログ: `WARNING lucianke.lsp.client: [fakels] LSP disconnected: server closed stdout (exit code None)` / `WARNING lucianke.lsp.pool: LSP fake/fakels crashed; restart 1/3 in 0.05s`。どの `assert` で落ちたかは出力が切れて控えていない。
  - 落ちたときの `lsp/diagnostics.py`・`lsp/pool.py` は BUG-0007 の変更後（診断の書き込み待ち）。このテストは診断を使わない（`--silent --crash-first 2`、`ready_fallback_s=0.3`）。
  - 再現を試みた結果: 同じテスト単独 3 回 + 12 本の CPU 負荷の下で 6 回、`test_lsp_pool.py` 全体を単独で 3 回、同時 6 本で 1 回ずつ、すべて green（再現せず）。
- 再現手順: 上の全体実行（負荷のあるとき）。確実な手順は未確定。
- 調査はしていない（直す役の仕事）。

## 原因
- 製品の不具合ではなく、テストが「ready になった」の合図に時間（`ready_fallback_s=0.3`）を使っていたこと。偽 LSP は `--crash-first N` のとき `initialized` を受けて `os._exit(3)` で落ちる。`--silent` なので診断も `$/progress` も出さず、ready の合図は pool の `ready_fallback_s`（INDEXING になってから 0.3 秒、信号が無ければ ready）だけだった。負荷で偽サーバーのプロセスが 0.3 秒のあいだ CPU を得られず `initialized` を処理できないと、これから落ちる起動（1 回目か 2 回目）が時間切れで ready に昇格し、`ready_handle` が戻って `env.launches == 3` / `h.restarts == 2` の表明が、落ちる前に走る。票のログが `restart 1/3` までだったのは、2 回目の起動が落ちる前に表明が走ったため。
- pool の fallback は仕様どおり（§7.5.2: 信号が来なければ `ready_fallback_s` 後に ready。既定 15 秒。ready のあとに切れれば監督タスクが `starting` に戻して再起動する）。pool は直さない。
- 同じ組（`--silent` + `--crash-first` + `ready_fallback_s=0.3`）の `test_restart_resets_documents_and_wakes_waiters` も同じ穴を持っていた。
- 実証: 偽サーバーの `os._exit(3)` の前に `time.sleep(0.6)` を足した写し（負荷でプロセスが止まった状態の再現）で元のテスト 2 本を走らせると、毎回落ちる（`env.launches == 3` が `1 == 3`、`h.restarts == 1` が `0 == 1`）。自然発生は、負荷の下で約 200 回（`-k restart_after_crash` を 1 プロセス 40 回 + 並行 10 本）回しても再現しなかった。

## 直し方
- `tests/unit/test_lsp_pool.py` の `test_restart_after_crash_with_backoff` と `test_restart_resets_documents_and_wakes_waiters` を、`--silent` + `ready_fallback_s=0.3` から `--progress 0.05` に変更（`ready_fallback_s` は `Env.pool` の既定 60 秒のまま）。偽サーバーは落ちない起動（最後の起動）だけが `$/progress` を出すので、ready の合図は「落ちない起動の progress end」だけになり、落ちる起動が時間切れで ready になる道が消える。待つのは状態の変化（`wait_ready`）であって時間ではない。
- pool 側はそのまま。`LspPool.__init__` は `sleep`・`clock` を注入できるが、ここでは再起動の待ちを `fake_sleep` に差してある（バックオフは時計に依存しない）。
- 他の `--silent` + `ready_fallback_s=0.2/0.3` のテストは、ready になれば足りて、落ちる起動を含まないので、早く昇格しても結果が変わらない（残した）。DiagnosticsHub の単体テストの `asyncio.sleep(0.05)` は「まだ終わっていない」を見る向きで、止まるほど通る側（上限の `< 0.2` と `< 2.0` だけが理屈の上で揺れうる。必要になったら別票）。

## 確認
- 偽サーバーを 0.6 秒・2.5 秒止めた写しで、直したテスト 2 本が green（元のテストは 0.6 秒で 2 本とも red）。
- 直したテスト 2 本を 1 プロセス 30 回 x 並行 6 本（計 360 回）、CPU 100% の共有機で green（失敗 0）。
- `tests/unit/test_lsp_pool.py` 44 件 green（コミット時点の状態。`ruff check` も通る）。
