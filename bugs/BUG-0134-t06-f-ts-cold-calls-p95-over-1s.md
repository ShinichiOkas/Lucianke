# BUG-0134 F-TS の T-06 の、その実行で初めての対象（冷）の p95 が 1 秒を超える

見たことだけを書く（調査していない）。2026-10-31 18:07〜、BUG-0058 の直す役が `3c015c9`（M1-3 の判定を冷・温に分けた形。仕様書 §17 OD-23 (b)）で
`bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200` を流したときに見た。typescript-language-server 4.3.3 / TypeScript 5.6.3 は bun 経由
（この PC に node が無い）、ky には `bun install --ignore-scripts` で依存を入れた。CPU の `cpu_busy_pct` は F-TS 25.7。結果 JSON は直す役の作業ツリーの
`bench/results/20261031-180657-windows-latency.json`（push していない）。

## 症状
- T-06 `find_references_and_implementations`（`query_type: references`）の F-TS: **冷 n=14 p50 169 / p95 1234 / max 1234 ms**（1 秒以上で不合格）、
  温 n=186 p50 11 / p95 22 ms（合格）。同じ計測の F-PY の冷は p95 48 ms、F-POLY の冷は p95 21 ms。
- 冷の値（ms、`i` は呼び出しの番号）: `source/utils/delay.ts#delay` 1234（i=3）、`source/utils/type-guards.ts#isKyError` 994（i=44）、
  `source/utils/timeout.ts#timeout` 546、`source/utils/type-guards.ts#isNetworkError` 543、`test/helpers/with-performance.ts#withPerformance` 458、
  `source/utils/type-guards.ts#isForceRetryError` 272、それ以外は 47〜171。
- BUG-0058 の計測でも F-TS の冷の p95 は 1358 / 1276（`b850152`）・2137（`07b2c06`）・1032（`0965fec`）ms で、冷の対象は 14〜15 件しかないので、
  最も遅い 1 件で p95 が決まる。

## 再現
上の計測を `--fixtures F-TS --tools find_references_and_implementations` で流し、結果 JSON の
`runs[0].tools.find_references_and_implementations.raw.samples` を `first == true` で絞る。

## 仕様書
§14.4（冷は p95 < 1 秒）、§3.2 NFR-PERF-1、§15.2 M1-3、§11 T-06。関連: BUG-0058、BUG-0047（tsconfig の外のファイルを開いてから聞く）。
