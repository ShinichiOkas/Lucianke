# BUG-0014 ヘッダの ~used が TokenEstimator の数え直しより常に 28〜30 トークン小さく、max_tokens を超える応答がある

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状: 応答テキスト全体を `lucianke.mcp.budget.TokenEstimator().count()` で数え直すと、ヘッダの `~used/max tok`（json は `budget.used_tokens_est`）より常に 27〜30 大きい。期待は仕様書 §5.7「`used_tokens_est` には応答テキスト全体（ヘッダ・トレーラ含む）」、§5.6-4「ヘッダ・警告行・トレーラも予算に含む」。
- 再現手順（`get_file_skeleton`、`bench/dogfood/run_dogfood.py 3` が自動で測る）:
  - `paths` = 小さめの `.py` 15 本（`bench/dogfood/out/obs3.json` の `files`）、`max_tokens: 500`、カーソルで全ページ取得: 13 ページ中 4 ページが数え直しで 500 超（508・502・526・501）。ヘッダの値は 28 小さい（例: ヘッダ `~435/500`、数え直し 462）。
  - 同じ paths で `max_tokens: 200`: 2 ページとも数え直し 213 > 200（ヘッダ `~185/200`）。
  - `max_tokens: 2000`: 4 ページ中 0 ページが超過だが、全ページでヘッダが 28 小さい（`~1950` と 1978 など）。
  - 単純な `get_symbol_bodies`（1 件、トレーラ無し）でも `~199/3000tok`、数え直し 229。
- 頻度: 全応答（約 40 ページ中 40）で差あり。超過は max_tokens が小さいときに出る（500: 4/13、200: 2/2）。
- 調査はしていない（直す役の仕事）。
