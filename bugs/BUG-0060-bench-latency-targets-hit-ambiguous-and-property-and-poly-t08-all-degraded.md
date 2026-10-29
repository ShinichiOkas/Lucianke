# BUG-0060 M1-3 の計測で、F-TS では 200 回中 20〜33 回が E_TARGET_AMBIGUOUS、F-PY の T-07 は 13 回が E_UNSUPPORTED。F-POLY の T-08 は 200 回すべて降格

見たことだけを書く（調査していない。いずれも判定の母集団から外れた呼び出し）。2026-10-29、M1-3 の受入計測（acc-M1-3）で観測、2 回の計測で同数。

## 症状
- F-TS（ky `35418888`）: `resolve_symbols` 20 件、`get_symbol_bodies` 15 件、`find_references_and_implementations` 23 件、`query_call_hierarchy` 33 件（200 回中）が `E_TARGET_AMBIGUOUS`。例: `test/helpers/create-http-test-server.ts#createHttpTestServer` → `'createHttpTestServer' matches 3 symbols; use a longer symbol_name, a urn, or a line`。台本の対象は `{path, symbol_name: <qualified>}`。F-TS の対象シンボル数は 33（F-PY は 1144）。
- F-PY: `query_call_hierarchy` 13 件が `E_UNSUPPORTED`（`details.reason = not_callable`）。例: `httpx/_urls.py#URL.fragment is a Property, which cannot be called (use a Function, Method, Constructor or Class)`。
- F-POLY の `get_diagnostics`: 200 回すべて `is_degraded: true`（`confidence_level: structural`、`lsp_status: unavailable`）で、母集団が 0 件（`no_samples`）。同じ F-POLY の `query_call_hierarchy` は 200 回中 164 回が `lsp_status: unavailable`（83 回が降格）、`resolve_symbols` は 20 回、`find_references_and_implementations` は 13 回が `unavailable`。F-POLY は §14.4 の母集団外（参考値）。
- F-TS の `get_diagnostics` で `E_TIMEOUT` が 1 件（run1 のみ。run2 は 0 件）。
- F-TS の `query_call_hierarchy` は 200 回中 74 回が `lsp_status: indexing`（ウォームアップ完了後）。

## 再現
`uv run python bench/run_latency.py --fixtures F-TS,F-PY,poly` の結果 JSON の `runs[].tools.<tool>.errors` と `raw.samples[].lsp` を見る。

## 仕様書
§14.4 の 2（母集団）、§5.5、NFR-OPS-3（索引中も降格応答で答える）。
