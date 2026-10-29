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

## 原因（16a 結線の役、2026-10-29 14:40）
観測した時点の `app.build_services` は `ToolServices` に `roots` / `trees` / `lines` しか渡さず、`db_path` / `reader_pool`（索引）も `locks` / `writer_index` / `journal_dir`（編集）も無かった。索引器・監視・書き手は合成ルートにつながっていなかった（結線の役の仕事そのもの）。

## 直し方（コミット 9c42d46・2ef3b40 ほか）
- `tools/_runtime.py`: `RuntimeHub`（ルート登録と、ワークスペースごとの `IndexWriter` / `ReaderPool` / `Indexer` / `Watcher` の束）と `WiredServices`（`db_path`・`reader_pool`・`locks`・`lsp_pool`・`code_action_store`・`writer_index`・`journal_dir`・`indexer`・`watcher`・`edit_transaction(ws, lsp=)`）。
- `app.py`: 起動（roots 確定 → 束を開く〈スキーマ移行込み〉→ 監視 → 背景の差分索引）と終了（`Lifecycle` 6 段）。`Pipeline` に `shutdown_guard`・`ready_gate`（起動直後の呼び出しが束の準備を待つ）・`data_version_provider`。
- `tools/__init__.py`: `tNN_*.py` の自動登録。

## 確認
tests/e2e/test_wired_index_stdio.py（3 回連続 green）と、実プロセスで T-03・T-06・T-08・T-10(dry_run) が is_error にならないこと（poly の複製、2026-10-29 14:36）。bench の S2・S4 は未実行（run_tokens.py の再実行は 18a が行う）。
注意: 起動から索引が育つまでの間（走査中）は T-03 などが 0 件を返す（降格応答。初回の重さは許容 §6.4）。
