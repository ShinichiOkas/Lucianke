# BUG-0035 set_python_env で Python の LSP が二重に再起動されうる

- 報告: 2026-10-29 15:10 / 見た人: 結線の役（16b）
- 見たこと（コードの読みから。実機での再現はしていない）:
  - T-00 の `_set_python_env`（`src/lucianke/tools/t00_configure_workspace.py`）は、`hub.workspace_changed(ws)` を呼んだあと、背景ジョブで `handle.ws = new` → `pool.reset` → `ensure_started` を行う。
  - `hub.workspace_changed(ws)`（`tools/_runtime.py`、34fcb14）は `LspPool.rebind(ws)` を await する。`LspPool.rebind`（16c、8a6f40f / c84b7a0）は、Python のインタープリタが変わると Python 系のハンドルを自分で止めて起動し直す。
  - そのため 1 回の `set_python_env` で、Python 系の LSP が 2 回再起動される可能性がある。
- 再現手順: 実プロセスで Python のワークスペースを開き、basedpyright を ready にしてから `configure_workspace` の `set_python_env` を呼び、LSP の起動ログ（`started_at` の変化）を数える。
- 調査はしていない（直す役の仕事）。
