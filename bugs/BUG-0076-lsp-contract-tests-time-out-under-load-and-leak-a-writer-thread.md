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

## 原因・直し方・確認（2026-10-31）

### 再現

- CPU を飽和させる負荷（14 コアの PC で、忙しく回るだけのプロセスを 28 個）を掛けながら
  `uv run pytest -q -p no:cacheprovider tests/contract/test_T04_lsp.py tests/contract/test_T06_T07_lsp.py tests/unit/test_index_core.py`
  → `2 failed, 52 passed, 5 errors`（票と同じ 3 種: T-04 実機の `'structural' == 'exact'`、`test_T06_T07_lsp.py` の実機 5 件のセットアップのエラー、
  `test_index_core` の書き手スレッドの検査）。負荷なしでは `59 passed`。

### 原因

1. **書き手スレッドが残る道（後始末が走らない）。** `test_T06_T07_lsp.py` のフィクスチャ `real` / `silent` は
   `build_env`（`IndexWriter` を作る）→ LSP の起動・`warm_up` → `yield` → `close_env` の順で、`yield` より前に try/finally が無かった。
   セットアップが落ちると `close_env` が走らず、`lucianke-index-writer`（daemon スレッド）がプロセスに残る。
   同じ形が `test_T06_ts_outside_tsconfig.py` の `tsenv`・`test_T07_ts_outside_tsconfig.py` の `tsenv7`・`contract_fixtures/_rig.py` の
   `open_rig`（索引の待ちの時間切れ）・`test_T13_two_servers.py`（`sync_roots` が try の外）にもあった。後始末の側も、
   `close_env`・`close_rig`・T-04 の `environment` は途中の 1 つが例外になると、その後ろ（書き手の `close`）を飛ばしていた。
   製品にも 1 か所: `IndexWriter.__init__` は起動の待ち（`START_TIMEOUT_S` = 60 秒）が時間切れになると、スレッドを止めずに
   `TimeoutError` を上げていた（遅れて DB を開いたスレッドは、誰も止めないキューを待ち続けて残る）。
2. **巻き添え。** `test_index_core::test_writer_start_failure_raises_and_leaves_no_thread` は、プロセス中の**すべての**
   `lucianke-index-writer` を数えていたので、前の試験が残したスレッドで落ちた。
3. **固定の短い期限で実 LSP の準備を待っていた。** `warm_up` は 60 秒の条件待ちだったが、中の `features.references` は本番の要求の期限
   （`LSP_TIMEOUT_REFERENCES_MS` = 3000 → `LspClient.request` の `asyncio.wait_for(..., timeout=3.0)`）のまま。負荷で起動直後の解析が
   遅れると最初の要求が 3 秒を超え、`TimeoutError` がそのまま上がってセットアップごと落ちた（票の「3 秒」。連続 3 回なら
   `timeout_3x` で LSP が止まる計数にも入る）。T-04 の実機の試験は `ready`（`ready_fallback_s` = 1 秒）の直後にツールを呼ぶので、
   「冷」の `definition` / `hover` が 3 秒を超えて `structural` に降格していた（降格そのものは製品として正しい）。
4. **追記の T-13（偽サーバー）。** `test_fake_the_lsp_sees_the_new_content_after_the_write` は、ツールが戻った直後に偽サーバーの記録を
   読んでいた。`textDocument/didChange` は通知（応答が無い）なので、負荷で偽サーバーがまだ読んでいないと 0 件になる（競合）。

### 直し方

- 見張り（新規 `tests/conftest.py`）: 試験ごと・関数より広いスコープのフィクスチャごとに、作った `lucianke-index-writer` を数え、
  後始末の後も生きていれば**作った側の試験**の teardown をエラーにする（「leaked threads (BUG-0076 ...)」。module などのフィクスチャは
  片づいた時点で名前つきで）。残ったスレッドは持ち主の `IndexWriter.close` で止めるので、後の試験は巻き添えにならない。
  見張りそのものの試験: `tests/unit/test_leak_guard.py`（別の pytest で、残した試験だけがエラーになり、後の試験・module スコープの
  書き手は無傷なことを確かめる）。
- 後始末を必ず走らせる: `test_T06_T07_lsp.py` に `opened_env`（try/finally）を足し、`real` / `silent` / `tsenv` / `tsenv7` はその中で
  準備する。`build_env` は途中で落ちたら作った分を閉じる。`close_env`・`_rig._close_parts`（`close_rig` と `open_rig` の失敗時）・
  T-04 の `environment` は `AsyncExitStack` で、1 つが失敗しても残りを閉じる（`close_env` は索引器の `aclose` も足した）。
  `test_T13_two_servers.py` は `sync_roots` を try の中へ。
- 製品: `IndexWriter.__init__` の起動の時間切れでは、止めの印（`None`）をキューに積んでから上げる。遅れて起動したスレッドは
  ジョブを待たずに止まる。試験 `test_writer_start_timeout_stops_the_thread_when_it_starts_late`（直す前は落ちることを確かめた）。
- `test_index_core` の検査は、**その試験が作ったスレッドだけ**を見る（前のスナップショットとの差。数えない検査にはしていない）。
- 実 LSP の準備は条件待ちに（新規 `tests/contract/_lsp_settle.py`）: `first_analysis` は製品と同じ合図（その文書の最初の
  `publishDiagnostics`）を上限 240 秒で待つ。`settle` は試験が LSP に直接聞く準備の問い合わせだけ期限を 120 秒に延ばし
  （`probe_timeouts`。ツールの呼び出しは入れない）、期待どおりの答えが揃うまで繰り返す。`test_T06_T07_lsp.warm_up` と
  T-04 の実機の試験（「冷」の前に、同じ位置の定義・hover が答えるまで待つ。素の `features` を使うので spy の数・`_hover_cache` は
  変わらない）がこれを使う。`warm_up` が開くのは対象のファイルだけ（呼び元のファイルを開くと、ディスクを書き換えて
  `didChangeWatchedFiles` を送る編集の試験の前提が変わる。最初の版で 2 件落ちたので戻した）。
- T-13: 偽サーバーが `didChange` を記録するまで条件待ち（上限 60 秒）。記録の書きかけの最後の行は読まない。
- 製品の期限（`LSP_TIMEOUT_*`・`EXACT_BUDGET_S`）は変えていない。負荷で期限を超えた要求は `structural` に降格して警告を付ける
  （答えが間違うのではなく、精度を下げたと明示する）ので、票は起こしていない。

### 確認

- **CPU を飽和させる負荷**（忙しいプロセス 28 個）の下で
  `tests/contract/test_T04_lsp.py tests/contract/test_T06_T07_lsp.py tests/contract/test_T06_T07_lsp_postconditions.py tests/unit/test_index_core.py tests/unit/test_leak_guard.py`
  を 3 回 → `64 passed`（253 秒 / 224 秒 / 226 秒）。直す前は同じ負荷で `2 failed, 5 errors`（上の再現）。
- **pytest を 3 本並べた負荷**（`tests/unit tests/invariants`・`tests/contract`・`tests/unit` の重いファイルの繰り返し）の下で、上に
  `test_T13_code_action.py`・`test_T13_two_servers.py`・`test_T11_rename_real.py` を足した 8 ファイルを 3 回 → `97 passed`
  （216 秒 / 182 秒 / 203 秒）。
- 負荷なし（origin/main `4ca4684` に載せ替えた後）: 触った試験のファイル 11 本 → `107 passed, 6 skipped`（skip は TypeScript の LSP が
  無い環境の試験）。
- 見張りの誤検知が無いこと: 背景の負荷として流した `tests/unit tests/invariants` 3 回（1 回目は `test_workspace_watcher.py` の 2 件で
  落ちた → 票 BUG-0083。2・3 回目は `3094 passed` / `3098 passed`）と `tests/contract` 3 回（`685` / `685` / `686 passed`）、負荷なしの
  `tests/e2e tests/contract_fixtures`（`54 passed, 5 skipped`）で、見張りのエラーは 0 件。
- `uv run ruff check` → `All checks passed!`。全体テストは流していない（親が main で 1 本流す）。
- 見つけた別の不具合: BUG-0083（負荷の高いときに `test_workspace_watcher.py` が落ちる。見たことだけの票）。
