# BUG-0059 bench/run_latency.py の台本に T-00・T-01・T-10・T-11・T-13 の計画が無く、M1-3 を 14 ツールで判定できない

見たことだけを書く（調査していない）。2026-10-29 15:34〜15:48、M1-3 の受入計測（acc-M1-3）で観測。

## 症状
- `bench/run_latency.py` の `PLANS` は 9 ツール（T-02、T-03、T-04、T-05、T-06、T-07、T-08、T-19、T-20）だけ。
- 結果 JSON の `runs[].unplanned_tools` に `apply_code_action`、`configure_workspace`、`edit_symbol_body`、`get_repo_map`、`rename_symbol` が出る（`tools_listed` には載っている = サーバーは 21 ツールを返す）。
- `m1_3_by_tool` ではこの 5 ツールが `not_implemented` と記録される（`tools/list` に載っているので実装済みの表示と食い違う）。T-00・T-01・T-10・T-11・T-13 の p95 はまだ 1 度も測られていない。
- 仕様書 §14.4 の 5 は「編集ツールは `diagnostics_wait_ms: 0` で呼び、診断待ちを含めない時間で判定する」。T-10・T-11 は台本が呼ぶ対象を壊さない手当て（一時コピーや恒等編集）が要りそう（これは台本を書く役の判断）。

## 再現
`uv run python bench/run_latency.py --fixtures poly --calls 5` を走らせ、結果 JSON の `m1_3_by_tool` と `runs[0].unplanned_tools` を見る。

## 仕様書
§14.4（母集団と判定）、§15.2 M1-3（14 ツール）。
