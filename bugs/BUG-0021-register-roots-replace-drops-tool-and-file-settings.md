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
