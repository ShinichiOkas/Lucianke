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

## 追試（2026-10-30・HEAD `dccf78e`・別 PC）— 見たことだけ
`uv run python bench/run_latency.py --fixtures F-PY --tools find_references_and_implementations,query_call_hierarchy,get_diagnostics --calls 200`（各 200 回）。サーバー内 p95（judged = 成功かつ非降格）:

| ツール | p95 | p50 | min | max |
|---|---|---|---|---|
| T-06 `find_references_and_implementations` | 5923 ms | 486 ms | 11 ms | 8329 ms |
| T-07 `query_call_hierarchy` | 5852 ms | 154 ms | 6 ms | 10034 ms |
| T-08 `get_diagnostics` | 2026 ms | - | - | - |

- 分布は**二峰性**。速い群（`tests/...` 配下の末端シンボル）は 6〜40 ms、遅い群（`httpx/...` 本体の中核シンボル: `_client.py`・`_models.py`・`_urls.py`・`_decoders.py` 等）は 5000〜8000 ms。遅延は**対象シンボルのファンイン（被参照数）**に相関して見える。
- 遅い群は `confidence: exact`・`lsp: ready`。`EXACT_BUDGET_S=10.0` のタイムアウトに当たって structural 降格した群が max 側（8000〜10034 ms）。
- **狙い撃ちプローブ（`bench/probe_bug0058.py`。同一対象を各 12 回連続）では遅延が再現しなかった**（ほぼ全て structural・5〜100 ms、計時計装 `lsp_call_hierarchy_slow` は 0 件）。同一対象の反復で LSP/キャッシュが温まり、かつ structural に落ちて計時計装点（exact 経路の `CallEdgeSource`）を通らなかったため。→ 遅延は「**毎回別シンボルの exact 経路コールド解決**」で出るテール問題。
- `a04937c` の計時計装（prime/settle/prepare/calls/sites の段別内訳）は exact 経路に入った呼び出ししか計時しない。段別の主犯（prepare / calls / sites）は**未確定**（この追試では exact のコールドを狙い撃ちでログに残せていない）。

## 優先度
師匠判断（2026-10-30）: 初回分析に数分待てば済む性質なら優先度は高くない。深掘りは後回し。本票は実測の記録として残し、直す役は「毎回別シンボル＋exact 経路強制」で計時計装ログを拾うところから再開する。

## 仕様書
§14.4（Fast Path は p95 < 50 ms）、§3.2 NFR-PERF-1、§15.2 M1-3。
