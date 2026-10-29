# BUG-0013: 実プロセスのサーバー（stdio）で、索引を使うツールが E_WORKSPACE_NOT_CONFIGURED / E_UNSUPPORTED を返す

見たことだけを書く（調査していない）。2026-10-29、bench/run_tokens.py（18a）で観測。

## 症状
cwd を fixture（bench/.cache/httpx と bench/.cache/ky）にして `uv run --project S:/work/develop/Lucianke lucianke` を MCP stdio で起動し、tools/list の 10 本（configure_workspace, edit_symbol_body, find_references_and_implementations, get_file_skeleton, get_project_db_schema, get_symbol_bodies, query_call_hierarchy, query_project_db, resolve_symbols, search_workspace_symbols）のうち、次を呼ぶと is_error になる。
- resolve_symbols（14:25 の実行）: `E_WORKSPACE_NOT_CONFIGURED` / `! the project index (PaaD) is not available: the server has no reader_pool / db_path`
- find_references_and_implementations: `E_WORKSPACE_NOT_CONFIGURED` / `! the index database is not configured for this server`
- get_diagnostics（F-PY の実行、14:29 時点）: `E_WORKSPACE_NOT_CONFIGURED` / 同上。F-TS では ok だが `source/utils/options.ts: not in the index (ignored, excluded or not yet indexed); skipped` と 0 件
- edit_symbol_body: `E_UNSUPPORTED` / `! editing is not available in this build (the edit services are not wired)`
- 起動ログに `ERROR lucianke.tools: tool module t08_get_diagnostics declares neither SPEC nor TOOL_SPEC; skipped`（時刻により出たり出なかったりした＝作業中のものを拾った可能性）
get_file_skeleton と get_symbol_bodies は成功する。

## 再現
`python bench/fetch_fixtures.py` のあと `uv run --with tiktoken python bench/run_tokens.py -f F-PY -s S2`。結果の JSON の `scenarios[].L.steps[].response` に応答の全文がある（bench/results/20261029-143045-Windows-tokens.json）。

## 影響
M1-4（NFR-TOK-1、S1・S2・S4）の L が測れない。run_tokens.py はこの状態を verdict=error（測定不成立）として記録し、合否に数えない。
