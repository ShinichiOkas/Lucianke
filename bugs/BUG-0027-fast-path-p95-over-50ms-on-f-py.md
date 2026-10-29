# BUG-0027 F-PY（httpx）で get_file_skeleton と search_workspace_symbols のサーバー内 p95 が 50 ms を大きく超える

見たことだけを書く（調査していない）。2026-10-29 14:30 頃、bench/run_latency.py（18b）で観測。CPU 100% の負荷あり（参考値）。

## 症状
`bench/.cache/httpx`（F-PY, SHA b5addb64）を cwd+roots にして、ツールごとに 200 回（シード固定、Tier 1 の .py からランダム）、format=json、逐次に呼んだ `timing_ms`（サーバー内）の p95:
- `get_file_skeleton`: 835 ms（F-POLY では 5 ms）
- `search_workspace_symbols`: 701 ms（F-POLY では 3 ms）
どちらも warm-up（`query_project_db` の files 件数が安定）後。NFR-PERF-1 は p95 < 50 ms。
その前の 20 件の試走（F-POLY）では `get_project_db_schema` が p50 290 ms / p95 445 ms（索引を作っている最中の可能性あり。本走の F-POLY では 12 ms）。

## 再現
`uv run python bench/fetch_fixtures.py F-PY` のあと `uv run python bench/run_latency.py --fixtures F-PY --tools get_file_skeleton,search_workspace_symbols`。
