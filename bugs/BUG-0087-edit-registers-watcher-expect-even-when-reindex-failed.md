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
