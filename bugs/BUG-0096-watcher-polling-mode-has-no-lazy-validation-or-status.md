# BUG-0096 監視が `polling` に落ちたときの要求時の遅延検証と、T-00 の `watcher: "polling"` の表示が実装に見当たらない

- 報告: 2026-10-31 / 見た人: BUG-0087・BUG-0088 を直す役（`Watcher.expect` の呼び手と監視の状態の使われ方を読んでいて見た）
- 見たことだけを書く（調査していない。再現の試験も書いていない。調査は直す役の仕事）

## 見たこと（origin/main `525bd6e` 時点）

- 仕様書 §12.4 の 6: 「監視が利用できない環境（ネットワークドライブなど）では、要求時に対象ファイルの `sha256` を照合する遅延検証に
  切り替え、`configure_workspace status` に `watcher: "polling"` と表示する」。設計書 §7.3.6 の 6 も同じ（遅延検証は
  `ToolContext.resolve_target` と `EditTransaction` 段 3）。
- `src/` を `polling` と `watcher.mode` / `.mode` で grep すると、当たるのは `workspace/watcher.py` の中（`mode` を立てる所と
  ログ）と、`tools/_runtime.py:168` のログの 1 行（`"watching (%s) and scan job %s started"`）だけ。
- `tools/t00_configure_workspace.py` に `watcher` / `polling` の文字列が無い。
- `resolve_target` を使うツール（T-06・T-07）の周りに、対象ファイルの `sha256` をディスクと照合する処理が見当たらない
  （`grep -rn "resolve_target\|stale"`）。

## 困ること（見たことからの推測）

- 監視を開始できなかった環境では、外部の変更（エディタの保存・`git checkout`）が次の差分索引まで索引に入らず、古い答えを返しうる。
  T-00 の `status` からもそれが分からない。
- BUG-0087 の直し（索引への取り込みが失敗したファイルは `expect` せず、こだまを監視の通常の道で取り込み直させる）も、こだまが
  届かない `polling` では効かない。
