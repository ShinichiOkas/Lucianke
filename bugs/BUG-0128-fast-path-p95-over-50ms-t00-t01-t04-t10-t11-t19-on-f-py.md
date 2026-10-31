# BUG-0128 14 ツールの計測で T-00・T-01・T-04・T-10・T-11・T-19 も F-PY で p95 50 ms を超える（負荷あり）

見たことだけを書く（調査していない）。2026-10-31 16:03〜16:14、BUG-0058 の直す役が今の main（`bfe71c3`）で `uv run python bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200` を流したときに見た。結果 JSON は直す役の作業ツリーの `bench/results/20261031-160333-windows-latency.json`（push していない）。

## 症状
- 機材: Intel Core Ultra 7 165U（12 コア / 14 論理）/ Windows 11。**ほかの役が並行して走っており、システム全体の CPU は 85〜100%**（計測の記録 `cpu_busy_pct` 84.5）。完全に空いた状態の計測ではない。
- この PC には node が無く、F-TS は typescript-language-server なしで走った（T-04・T-06・T-07・T-08・T-11・T-13 は母集団 0 か skip）。
- サーバー内 `timing_ms` の p95（ms。p50 / p95 / max）。BUG-0058 の T-06・T-07・T-08 以外で 50 ms を超えたもの:

| ツール | F-POLY | F-PY | F-TS |
|---|---|---|---|
| T-00 `configure_workspace`（`action: status`） | 16 / 18 / 34（合格） | **201 / 346 / 503** | **165 / 267 / 370** |
| T-01 `get_repo_map` | 19 / 25 / 36（合格） | **71 / 126 / 245** | **43 / 62 / 258** |
| T-04 `resolve_symbols` | 12 / 26 / 654（合格） | **31 / 77 / 1319** | 母集団 0 |
| T-10 `edit_symbol_body`（dry_run の delete_symbol） | 14 / 20 / 30（合格） | **34 / 67 / 156** | **17 / 51 / 59** |
| T-11 `rename_symbol`（dry_run） | skip（E_LSP_UNAVAILABLE） | **79 / 215 / 490** | skip |
| T-19 `get_project_db_schema` | **51 / 386 / 733** | **73 / 373 / 709** | **33 / 70 / 399** |

- T-19 は引数なしの同じ呼び出しを 200 回繰り返しているのに、F-POLY の 1 回ごとの値が 20 ms 前後の区間と 200〜700 ms の区間を行き来した（最初の約 40 回と 100〜150 回目付近が遅い）。2026-10-29 の受入（M1-3、48 論理の機材）では T-19 は 10〜22 ms だった。
- T-00 の `status` は副作用の無い照会で、F-POLY では 18 ms なのに F-PY・F-TS では p50 が 165〜201 ms。
- stdout の純度・子プロセスの監視は違反なし。

## 再現
上の計測をもう一度流す（ほかの役が走っている状態）。各ツールの 1 回ごとの値は結果 JSON の `runs[].tools.<tool>.raw.samples[].server_ms`。

## 仕様書
§14.4（Fast Path は p95 < 50 ms）、§3.2 NFR-PERF-1、§15.2 M1-3。関連: BUG-0058（T-06・T-07・T-08）、BUG-0059（台本に 5 ツールを足した票）。
