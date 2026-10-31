# BUG-0129 F-POLY の T-07 で `LimitTable.__init__`・`LimitTable.get` の呼び出しが何度も 10 秒（予算）かかり、その後 `lsp_status` が `indexing` になる

見たことだけを書く（調査していない）。2026-10-31 16:52 ごろ、BUG-0058 の直す役が `b850152`（BUG-0058 の直しの後）で
`bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200 --tools find_references_and_implementations,query_call_hierarchy,get_diagnostics`
を流したときに見た（typescript-language-server は bun 経由。ほかの役の負荷で CPU 85〜100%）。

## 症状
- T-07 `query_call_hierarchy`（`direction: incoming`、`depth: 2`）の F-POLY の 200 回のうち、`src/shop/services/limits.py#LimitTable.__init__` と
  `src/shop/services/limits.py#LimitTable.get` の呼び出し（24 回）の値（ms、`i` は呼び出しの番号、括弧は `lsp_status`）:
  `i=2` 938（indexing）、4: 568（indexing）、32: 606（indexing）、**34: 10005、41: 10025、44: 10038、54: 10008、59: 10006**（ready）、
  73: 7636（indexing）、81: 12、93: 7、98: 507（indexing）、106: 463（indexing）、109: 7、123: 487（indexing）、134: 434、139: 559、143: 515、
  146: 11、147: 535、163: 428、188: 5、189: 439（indexing）、199: 7。どれも `confidence_level: exact`・`is_degraded: false` で返った。
- 10 秒は T-07 の 1 問い合わせの LSP の予算（`EXACT_BUDGET_S = 10.0`）と同じ。10 秒の呼び出しは同じ 2 つの対象に 5 回続けて出た（i=34〜59）。
- 同じ計測の `bfe71c3`（直しの前）でも、`PaymentProcessor.__init__`・`LimitTable.get`・`LimitTable.__init__` などで 10 秒の呼び出しが
  多数あった（F-POLY の T-07 の p95 10033 ms）。直しの後は p95 568 ms だが、上の 10 秒は残った。
- 2 回目以降に 5〜12 ms で返る回（PaaD から）と、400〜900 ms で `lsp_status: indexing` の回が交互に出る。
- F-PY・F-TS では T-07 の最大は 1.6 秒・0.65 秒（10 秒の呼び出しは無かった）。

## 再現
上の計測を `--fixtures poly --tools query_call_hierarchy` で流し、結果 JSON の `runs[0].tools.query_call_hierarchy.raw.samples` を対象で絞る。
結果 JSON: 直す役の作業ツリーの外（使い捨ての checkout）の `bench/results/20261031-165213-windows-latency.json`（push していない）。

## 仕様書
§11 T-07、§14.4。関連: BUG-0058、BUG-0091（T-07 の予算）、BUG-0123（予算で打ち切った要求を裏で待つ）。
