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

## 原因・直し方・確認（2026-10-31）

**原因**: 設計書 §7.3.6 の 6・仕様書 §12.4 の 6 は書いてあったが、`Watcher` が `mode = "polling"` を立てるだけで、`mode` を読む所が
どこにも無かった（`src/` の `.mode` の当たりは監視自身とログの 1 行だけ）。要求時の遅延検証も、T-00 の `watcher` の表示も未実装。
`Watcher.drain(paths)` は `polling`（走っていない）のとき `paths` を呼び手の文脈で sink に同期する作りで、部品は揃っていたが、
呼び手（解決の前・`EditTransaction` 段 3）が呼んでいなかった（段 3 は `drain()` を `paths` なしで呼ぶので、`polling` では何もしなかった）。

**直し方**（仕様と実装の都合: `resolve_target` は同期で読み取り接続の中にあり、取り込み直し（async）をそこでは呼べない。
呼ぶ側の AI にとって同じ効き目になる、ハンドラが解決の前に呼ぶ入口に置いた。設計書 §7.1.3 の 5 をそれに合わせて直した）:

- `WiredServices.watcher_mode(workspace_id)` と `revalidate(ws, rel)`（`polling` のときだけ `Watcher.drain([rel])`。`sha256` が同じなら何もせず、
  違えば取り込み直し、消えていれば索引から消す。LSP への `didChangeWatchedFiles` も同じ道）。
- `tools/_building.py` の `ensure_indexed`（`ensure_target_indexed` / `ensure_targets_indexed` の本体。T-04・T-06・T-07・T-08 が解決の前に
  呼んでいる）の先頭で `revalidate`。失敗しても応答は止めず古い索引で答える。
- パイプラインが、`polling` の間は索引を読むツール（`reads_index`）の `warnings[]` に `file watching unavailable (polling): the target file is
  checked against the disk on each call, but other files may be stale until the next reindex` を 1 つ付ける（対象以外の参照元などは
  照合しないので、古いことがあると AI が分かるように）。
- `EditTransaction` 段 3: `watcher.mode == "polling"` のときだけ `watcher.drain(paths)`（`native` は従来どおり `drain()`）。
- T-00 `status`: compact の `watcher: native|polling` の行と、`data.workspaces[].watcher`。
- 仕様書 §12.4 の 6・T-00 の出力の表と例、設計書 §7.1.3 の 5・§7.3.6 の 6 を書いた。

**確認**: 新しい `tests/unit/test_tools_polling_watcher.py`（4 件。結線は本番と同じ。`awatch` を `OSError` にして `polling` に落とす）:
書き換えたファイルの新しい関数が、イベントなしで次の T-04・T-07・T-06 に見える／消したファイルは `E_TARGET_NOT_FOUND`／`polling` の
status と警告／`native` の status と警告なし。`tests/unit/test_edit_transaction.py` に 2 件（`polling` で `drain` が対象のパスつき、`native` はパスなし。
前者は修正前に赤）。関係する試験・見張り（監視・索引の途中・T-00・パイプライン・schema_budget・import_graph・launch_guard・stdout_purity・leak_guard）
172 件が通り、`uv run ruff check` はクリーン。

**残り**: `polling` では対象以外のファイル（T-06 の参照元・T-07 の呼び出し元など）は照合しない（警告で知らせるまで。次の `reindex` か差分索引で揃う）。
ネットワークドライブでの実測は環境が無く未検証のまま。BUG-0087 の `forget` の直しは、`polling` ではこだまが来ないので元から効かないが害も無い
（期待は `WATCH_EXPECT_TTL_S` で失効し、`drain([rel])` は期待が一致する間は取り込み直さない = 索引が最新のときだけ）。
