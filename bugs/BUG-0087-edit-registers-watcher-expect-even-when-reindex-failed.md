# BUG-0087 編集の再索引（`index_file`）が失敗しても `Watcher.expect` を登録するので、自分の書き込みのイベントが捨てられ、索引が古い内容のまま残りうる

- 報告: 2026-10-31 / 見た人: BUG-0083 を直す役（`Watcher.expect` の呼び手を読んでいて見た）
- 見たことだけを書く（調査していない。再現の試験も書いていない。調査は直す役の仕事）

## 見たこと（`src/lucianke/edit/transaction.py`、origin/main `9f4ffa3` 時点）

`EditTransaction._reindex` は、書き込んだファイルごとに次の順で動く:

1. `:804` `await _maybe_await(self._indexer.index_file(path))` — 例外は `:806`〜`:807` で
   `_log.warning("reindex failed ...")` と `warnings.append("reindex failed for ...")` にして続ける。
2. （LSP の `didChange`）
3. `:818` `self._watcher.expect(path, after_sha[path])` — 1 の成否を見ずに、いつも登録する。

`Watcher.expect` は「内容が `after_sha` と一致するこのパスのイベントを捨てる」（設計書 §7.3.6-8。BUG-0083 で、一致する間は
`WATCH_EXPECT_TTL_S` = 10 秒まで捨て続けるようにした）。1 で索引への取り込みが失敗した場合も、ディスクの内容は `after_sha` なので、
監視に届く自分の書き込みのイベントは捨てられ、取り込みをやり直す道が無いように見える（次にそのファイルが別の内容に変わるか、
差分索引が走るまで、索引は編集前の内容のまま）。

BUG-0083 の前（期待を 1 回で消費していた）は、自分の書き込みのイベントが負荷で 2 つの窓に割れたときだけ、2 つ目が取り込まれて
偶然直ることがあった。BUG-0083 の後はその道も無い。

## 困ること

- 編集ツールは `warnings` に `reindex failed` を返すが、その後の検索・参照は（最大で次の変更まで）古い索引で答えうる。

## 原因・直し方・確認（2026-10-31）

### 原因

- 票のとおり。`EditTransaction._reindex`（`edit/transaction.py`）は、`indexer.index_file(path)` が例外で終わっても
  `watcher.expect(path, after_sha)` を登録していた。期待は「ディスクの内容が `sha256_after` と一致するこのパスのイベントを捨てる」
  もので、BUG-0083 から一致する間は `WATCH_EXPECT_TTL_S`（10 秒）まで捨て続ける。取り込みが失敗してもディスクは `after_sha` の
  ままなので、こだま（`os.replace` の deleted と added）は全部捨てられ、索引は編集前の内容のまま残る。
- 決定的な再現: 本物の `Watcher` に `changed → index_file` の sink（`tools/_runtime.IndexerSink` と同じ形）を付け、最初の
  `index_file` を例外にする偽の索引で編集し、こだまの窓を `_consume` に直接流す。直す前は `index_file` が二度と呼ばれない
  （下の試験 1 が `calls == []` で落ちることを確かめた）。
- 同じ型の 2 つ目: 前の編集の期待が失効前に残っていて、書いた内容と `sha256` が一致する（同じ内容に戻す編集）と、`expect` を
  呼ばなくても古い期待がこだまを捨てる（試験 2）。
- 同じ型の 3 つ目: 索引が無い（`indexer=None`）トランザクションも `expect` していた（取り込んでいないのに捨てさせる。試験 5）。
- 他の `expect` の呼び手: `src/` には `edit/transaction.py` の 1 か所だけ（`grep -rn "\.expect(" src`）。

### 直し方

- `_reindex`: `index_file` が例外なく終わったファイルだけ `watcher.expect(path, after_sha)`。取り込めなかった（例外・索引なし）
  ファイルは `watcher.forget(path)` を呼び、こだまを捨てさせずに監視の通常の道（`sink.changed` → `index_file`）で取り込み直させる。
  `warnings` の `reindex failed for …` は従来どおり。`index_file` が `None` を返す（内容が DB と同じ・対象外で `remove`）のは、
  索引がディスクと揃っているので成功として扱う。
- `Watcher.forget(rel)` を足した（そのパスの期待を消す。無ければ何もしない）。`WatcherLike` プロトコルにも足した。
- 選んだ理由: その場での再試行（`index_file` をもう 1 回）は、決まって失敗する原因（取り込みの不具合・書き手の停止）では同じ
  失敗を繰り返して応答を遅らせるだけ。こだまは数十 ms〜1 秒後に届くので、一時的な失敗にはこの遅れた再試行のほうが効く。
  `drain([path])` で即座に監視の道を通す案は、ロックを持ったまま監視のキュー（バーストの合算・rescan）を待つことになるので採らない。
- 残る穴: 監視が `polling` に落ちているとこだまが届かないので、取り込みの失敗の後はそのファイルが次に変わるか差分索引が走る
  まで古い。仕様書 §12.4 の 6 の要求時の遅延検証が実装に見当たらない（見たことだけの票 BUG-0096）。
- 設計書: §7.3.6 の API（`forget` を追加、`expect` は取り込めたファイルだけ）と手順 8、§7.7.1 の Reindexed の行、§8.4 の 7b を合わせた。

### 確認

- 試験（`tests/unit/test_edit_transaction.py`。本物の `Watcher`・窓は `_consume` に直接流す・時刻は監視モジュールの `time` を
  差し替えて手で進める。`awatch` を使わないので負荷に依らない）:
  1. `test_reindex_failure_does_not_drop_the_echo_so_the_watcher_reindexes`: `index_file` が例外 → こだまは捨てられず、監視の道で取り込み直す。
  2. `test_reindex_failure_also_clears_a_stale_expectation_for_the_same_content`: 同じ内容の古い期待が残っていても捨てられない。
  3. `test_reindexed_edit_drops_its_echo_even_when_split_into_windows`: 取り込めたら、2 つの窓に割れたこだまも捨てる（二重取り込みなし）。
  4. `test_after_the_ttl_an_external_write_of_the_same_content_is_ingested`: TTL の後、外部が同じ内容を書いたら取り込む。
  5. `test_without_an_indexer_the_echo_is_left_to_the_watcher`: 索引が無ければ `expect` せず `forget`。
  直す前のコードでは 1・2・5 が落ち、3・4 は通る（回帰の見張り）ことを確かめた。`tests/unit/test_workspace_watcher.py` に
  `test_forget_removes_the_expectation` を足した。
- 流した試験（全体テストは流していない。親が main で 1 本流す）:
  - `uv run ruff check` → `All checks passed!`。変えたファイルの `ruff format --check` と `basedpyright` も通る
    （`test_edit_transaction.py` の `key_of` の 7 件の型の指摘は変える前からある）。
  - `tests/unit/test_workspace_watcher.py tests/unit/test_tools_runtime.py tests/unit/test_edit_transaction.py tests/unit/test_edit_writer.py tests/unit/test_core_limits.py`
    → `140 passed, 1 skipped`。origin/main `525bd6e` に載せた後、BUG-0088 の再現の負荷の下で
    `test_edit_transaction.py test_workspace_watcher.py` → `75 passed`、`test_tools_runtime.py`（と下の fx）も通る。
  - 編集の契約テスト（T-10 の 4 ファイル・T-11 の 3 ファイル・T-13 の 2 ファイル・`test_unsupported_tools.py`）→ 載せる前 `277 passed`。
    載せた後（実 LSP の fx の試験と並べて流した）→ `1 failed, 321 passed, 1 skipped`。落ちたのは
    `test_T13_two_servers.py` の basedpyright の診断が `resolved` に入らない 1 件で、単独では 3 回とも通り、捕まえたログに
    `reindex failed` は無い（この変更は取り込みが失敗したときだけ振る舞いを変える）。見たことだけの票 BUG-0097 にした。
  - `tests/contract_fixtures/test_fx_edit_T10_T13.py tests/unit/test_tools_runtime.py` → `30 passed, 2 skipped`。
