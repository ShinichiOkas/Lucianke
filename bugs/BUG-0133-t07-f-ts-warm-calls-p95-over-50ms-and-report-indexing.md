# BUG-0133 F-TS の T-07 の 2 回目以降（温）が p95 101 ms で 50 ms を超え、温の呼び出しの 4 割が `lsp_status: indexing` と答える

見たことだけを書く（調査していない）。2026-10-31 18:07〜、BUG-0058 の直す役が `3c015c9`（M1-3 の判定を冷・温に分けた形。仕様書 §17 OD-23 (b)）で
`bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200` を流したときに見た。typescript-language-server 4.3.3 / TypeScript 5.6.3 は bun 経由
（この PC に node が無い）、ky には `bun install --ignore-scripts` で依存を入れた。CPU の `cpu_busy_pct` は F-TS 25.7。結果 JSON は直す役の作業ツリーの
`bench/results/20261031-180657-windows-latency.json`（push していない）。

## 症状
- T-07 `query_call_hierarchy`（`direction: incoming`、`depth: 2`）の F-TS: 冷 n=13 p50 51 / p95 710 ms（< 2 秒で合格）、**温 n=174 p50 29 / p95 101 / max 647 ms**
  （50 ms 以上で不合格）。同じ計測の F-PY の温は p50 6 / p95 9 ms、F-POLY の温は p50 6 / p95 25 ms。
- 温の 174 回のうち **74 回が `lsp_status: indexing`・`confidence_level: exact`**、100 回が `ready`・`exact`。TS の LSP は計測の間ずっと動いていた
  （同じ実行の T-06・T-08 は `ready`）。
- 温の呼び出しの対象ごとの中央値（ms）: `test/resilience.ts#client` 68（最大 579）、`test/resilience.ts#observe` 46、
  `source/utils/is-network-error.ts#isRawNetworkError` 40、`test/request-body-serialization.ts#assertBytes` 37（最大 647）、
  `source/utils/type-guards.ts#isResponseSizeError` 35 …… `source/utils/merge.ts#newHookValue` 10、`source/utils/timeout.ts#timeout` 9、
  `source/core/Ky.ts#cloneInitHookOptions` 7。同じ対象を何度聞いても速くならない対象と、7〜10 ms で返る対象がある。
- BUG-0058 の計測（`b850152`・`0965fec`）でも F-TS の T-07 の温は p95 73〜115 ms、`indexing` の回があった。

## 再現
上の計測を `--fixtures F-TS --tools query_call_hierarchy` で流し、結果 JSON の `runs[0].tools.query_call_hierarchy.raw.samples` を `first == false` で絞る。

## 仕様書
§14.4（温は p95 < 50 ms）、§3.2 NFR-PERF-1、§15.2 M1-3、§11 T-07。関連: BUG-0058、BUG-0129（F-POLY の `__init__` で同じ `indexing` の取り違え）。
