# BUG-0028 14:51 の作業ツリーで、F-POLY に対して全ツールの最初の呼び出しが E_INTERNAL（query_project_db も）

見たことだけを書く（調査していない）。2026-10-29 14:51、bench/run_latency.py（18b）で観測。多数の役が並行編集中の作業ツリーの一時的な状態かもしれない。

## 症状
F-POLY を cwd+roots にして起動。`get_file_skeleton`・`search_workspace_symbols`・`get_symbol_bodies`・`find_references_and_implementations`・`query_call_hierarchy`・`get_diagnostics`・`get_project_db_schema`・`query_project_db` がすべて `E_INTERNAL`（`Internal error. Quote details.log_ref when reporting.`）。`resolve_symbols` は `E_TARGET_NOT_FOUND`（`file is not in the index: src/shop/models/receipt.py`）。
同じコマンドは 14:41 には成功していた。ログ行（log_ref）は取っていない。

## 再現
`uv run python bench/run_latency.py --fixtures poly`。結果 JSON の `runs[0].tools.<tool>.probe_errors` と `server_stderr_errors`。
