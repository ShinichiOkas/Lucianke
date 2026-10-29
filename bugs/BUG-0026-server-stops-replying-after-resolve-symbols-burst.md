# BUG-0026 実プロセスのサーバーが resolve_symbols を約 100 回呼んだあと応答しなくなり、stdin を閉じても 10 秒で終わらない

見たことだけを書く（調査していない）。2026-10-29 14:35〜14:42、bench/run_latency.py（18b）で観測。作業ツリーは多数の役が並行編集中・CPU 100%（`load.cpu_busy_pct`）。

## 症状
- F-POLY（tests/fixtures/poly）を cwd+roots にして `uv run --project <repo> lucianke` を起動。warm-up は `query_project_db` で files=29 が安定するまで待って完了（`lsp_status` は T-20 の応答で n/a、resolve_symbols の応答では `indexing`・`is_degraded: true`）。
- `resolve_symbols`（`targets=[{path, symbol_name}]`、format=json）を 1 件ずつ逐次に呼ぶ。5 件の準備 + 98 件は成功（サーバー内 3〜8 ms）。99 件目以降は 30 秒待っても応答なし（CLIENT_TIMEOUT 3 連続）。
- サーバーの stderr の最後の行は `tool call finished tool=resolve_symbols timing_ms=7`（14:41:11）。ERROR 行なし。
- そのあと stdin を閉じても 10 秒で終了せず（exit_timeout）、こちらで強制終了（終了コード 15）。
- 別の実行（14:35）では `search_workspace_symbols` を 200 回呼んだ直後の最初の `resolve_symbols` が応答せず、以降の全ツール（get_symbol_bodies, find_references_and_implementations, query_call_hierarchy, get_diagnostics, get_project_db_schema, query_project_db）の最初の呼び出しも 30 秒で CLIENT_TIMEOUT。
- 同じコマンドを `--tools resolve_symbols --calls 20` に絞ると 20 件とも成功（再現しない）。

## 再現
`uv run python bench/run_latency.py --fixtures poly --warmup-timeout 90`（その時点の作業ツリー）。結果は `bench/results/<日時>-windows-latency.json` の `runs[0].tools.<tool>.raw.samples[].error` と `runs[0].shutdown`、`server_stderr_tail`。
