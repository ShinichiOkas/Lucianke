# BUG-0058 F-PY・F-TS で T-06・T-07・T-08 のサーバー内 p95 が Fast Path の 50 ms を大きく超える（M1-3 不合格）

見たことだけを書く（調査していない）。2026-10-29 15:34〜15:48、M1-3 の受入計測（acc-M1-3）で観測。要約は `.pair-agent/acceptance/M1-3-latency-20261029-154219.md`。

## 症状
`python bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200`（母集団は成功かつ `is_degraded == false`、サーバー内 `timing_ms`、format=json、ウォームアップは LSP ready と索引ジョブ完了まで待つ）。同じ結果が 2 回出た（run1 = origin/main `5225ec1`、run2 = `605e79b`）。CPU はシステム全体で平均 24〜26%、最大 53.5% / 41.7%（50% 超は run1 の 1 秒だけ）。サーバー内 p95（ms、run1 / run2）:

| ツール | F-PY | F-TS | p50（F-PY / F-TS） | 最大（F-PY / F-TS） |
|---|---|---|---|---|
| T-06 `find_references_and_implementations` | 551 / 454 | 333 / 292 | 11〜12 / 8〜9 | 1296 / 2840（run1） |
| T-07 `query_call_hierarchy` | 842 / 883 | 1024 / 765 | 26〜27 / 188〜263 | 5297〜5540 / 1115（run1）〜836（run2） |
| T-08 `get_diagnostics` | 332 / 333 | 1015 / 937 | 10 / 13〜14 | 592 / 2423 |

- 遅い呼び出しはすべて `lsp_status: ready`（T-07 の F-TS は `indexing` の呼び出しも多い）、`confidence: exact`。p50 は 8〜27 ms（F-TS の T-07 だけ 188〜263 ms）で、尾が p95 を押し上げている。
- 遅い対象の例（run2）: T-06 F-PY `httpx/_models.py#Response.read` 1233 ms、F-TS `source/utils/type-guards.ts#isNetworkError` 2388 ms。T-07 F-PY `httpx/_client.py#Client.get` 5540 ms、`httpx/_models.py#Cookies.get` 4703 ms。T-08 F-PY `tests/__init__.py` 577 ms、F-TS `test/browser.ts` 2415 ms（`test/fetch.ts` 1644 ms）。
- F-POLY（参考、§14.4 の母集団外）でも T-07 の p95 が 401 / 408 ms（最大 5 s）。
- 合格したのは T-02（F-PY 7・25 / F-TS 7・8）、T-03、T-04（F-PY 27 / F-TS 18〜20）、T-05、T-19、T-20（いずれも 50 ms 未満）。

## 再現
`uv run python bench/fetch_fixtures.py` のあと、typescript-language-server が PATH にある状態で `uv run python bench/run_latency.py --fixtures F-PY,F-TS --tools find_references_and_implementations,query_call_hierarchy,get_diagnostics`。

## 仕様書
§14.4（Fast Path は p95 < 50 ms）、§3.2 NFR-PERF-1、§15.2 M1-3。
