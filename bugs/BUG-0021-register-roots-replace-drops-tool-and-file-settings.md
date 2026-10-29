# BUG-0021 register_roots(replace=True) が登録済みルートの `_tool_args` と `_file_settings` を空にする

- 報告: 2026-10-29 / 見た人: T-00 を作る役（17a）
- 症状: `WorkspaceContext.register_roots([root], replace=True)` を、既に登録済みで `set_excludes` 済みのルートに対して呼ぶと、`settings(workspace_id)` の `exclude_globs` が空に戻る。`get(workspace_id).exclude_globs`（`Workspace` の値）は残る。`replace=False`（既定）の 2 回目の `register_roots` でも `settings()` は空のまま。
  - 実測（env={} の `WorkspaceContext`、tmp ディレクトリ 1 つ）: `register_roots([d])` → `set_excludes(id, ["a/"])` →
    - その時点: `settings(id)["exclude_globs"] == ('a/',)`、`get(id).exclude_globs == ('a/',)`
    - `register_roots([d], replace=True)` の後: `settings(id)["exclude_globs"] == ()`、`get(id).exclude_globs == ('a/',)`
    - さらに `register_roots([d])` の後: `settings(id)["exclude_globs"] == ()`
  - 期待: 設計書 §7.3.5 の優先順位（ツールでの明示 > 環境変数 > .lucianke.toml > 自動検出）が、再登録しても保たれる。
  - 観察: `workspace/context.py` の `register_roots` で、既存ルートの分岐（`existing`）は `table[...] = existing` と `result.append` だけで `continue` し、`file_settings` / `tool_args` に戻さない。最後に `self._file_settings` / `self._tool_args` を `table` に残る ID で絞って差し替える。
  - 影響の見通し: T-00 の `set_roots` は `register_roots` のあと `hub.sync_roots`（もう一度 `register_roots(replace=True)`）を呼ぶ。起動時の roots/list 再取得（`list_changed`）も同じ経路。
- 再現手順: 上の実測のとおり。
- 調査はしていない（直す役の仕事）。

## 原因
- `WorkspaceContext.register_roots` は、`replace=True` のとき `table`・`file_settings`・`tool_args` の 3 つを空の辞書から作り直していた。`table` には「登録済みのルート」の分岐（`existing`）で既存の `Workspace` を戻していたが、`file_settings` と `tool_args` には何も戻さなかった。最後の `self._file_settings = {i: v … if i in table}` は絞るだけで、足さない。結果、残ったルートの「ツールでの明示」（`set_excludes`・`set_python_env`）と `.lucianke.toml` 由来の設定が消え、`settings()`（`_tool_args` ＋ `_file_settings` を合成）は既定値に戻る一方、`Workspace`（不変オブジェクトを差し替えて持つ）は `exclude_globs` などを保っていた。2 つの表が食い違う。
- 票の「`replace` なしの 2 回目でも同じ」は、直前の `replace=True` で既に空になった状態を見ていた。`replace=False` の分岐は元から `dict(self._…)` で写していたので、単独では落とさなかった（`replace=False` だけを続けた実測で確認）。
- 起きる経路: T-00 `set_roots` は `register_roots(replace=True)` のあと `hub.sync_roots`（`RuntimeHub._sync_roots`）が同じ `replace=True` をもう一度呼ぶ。起動時の `roots/list` 取り直し（`list_changed`）も同じ。

## 直し方
- `register_roots` の `file_settings` と `tool_args` を `replace` に関わらず現在の表の写しから始める。`replace=True` が消すのは「一覧から外れたルート」の分だけにし、それは従来どおり最後に `table` に残る ID で絞る処理（`self._file_settings = {… if i in table}`）が担う。`table` だけが `replace` で空から作り直される（設計書 §7.3.1・§7.3.5: ツール > 環境変数 > `.lucianke.toml` の優先順位は再登録で変わらない）。
- 新しく作るルートには `tool_args.pop(ws_id, None)` を入れ、前の持ち主の明示が同じ ID 経由で紛れ込まないようにした（外したルートを戻すと、同じ ID でまっさらな設定になる）。
- docstring に「登録済みのルートはツールでの明示と `.lucianke.toml` 由来の設定も保つ」「`replace=True` は外れたルートの設定も消す」を追記。
- テスト: `tests/unit/test_workspace_context_reregister.py`（新規 11 件）。票の実測（`set_excludes` → `replace=True`）、`replace=False` の 2 回目、`replace=True`・`False` の混ぜた繰り返し、再登録後の `set_excludes` が置換のまま、`set_python_env` の保持、`.lucianke.toml` 値（`max_file_bytes`・`exclude_globs`）の保持、ツール > ファイルの優先、複数ルートの並べ替え・追加で各ルートの設定が動かない、外れたルートの設定が消え同じ ID で戻るとまっさら（ファイルは読み直し）、不正パスで失敗したときは何も変わらない。各ケースで `settings()` と `Workspace` の `exclude_globs` が一致することも検める。

## 確認
- 票の実測のスクリプトを直す前に実行して `('a/',) ('a/',)` → `() ('a/',)` を再現し、直したあとは `('a/',) ('a/',)` のまま。
- `tests/unit/test_workspace_context_reregister.py`・`tests/unit/test_workspace_context.py` を 3 回続けて green（89 件・1 skip）。
