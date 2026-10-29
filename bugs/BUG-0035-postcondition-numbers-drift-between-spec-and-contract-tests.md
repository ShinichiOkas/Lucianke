# BUG-0035 事後条件の番号が仕様書と契約テストでずれている（T-06・T-07）。名乗る試験が無い事後条件が M1 の 14 ツールで 10 件

- 報告: 2026-10-29 / 見た人: M1 出口条件の検査台本の役（22b）/ 見たことだけ（調査・修正はしていない）
- 見たこと（`uv run python bench/postcondition_map.py --m1-only`、仕様書 HEAD 時点）:
  - 仕様書 §11 の T-06 は事後条件 6 項目（5 = 解決できない import の外部シンボル `lk://_ext/...`、6 = `budget.used_tokens_est ≤ max_tokens`）。
    試験 `tests/contract/test_T06_T07.py::test_t06_postcondition_4_5_pages_have_no_duplicates_and_fit_the_budget` は「4・5」を名乗り、予算の確認を 5 として扱っている（仕様書では 6）。5（外部シンボル）と 6 を名乗る試験は無い。
  - T-07 は 6 項目（3 = `when:`、4 = 循環を 1 度だけ示し展開しない、5 = 外部の葉ノード、6 = 予算）。
    `test_t07_postcondition_3_cycle_is_shown_once_and_not_expanded` は循環の試験だが 3 を名乗る（仕様書では 4）。`test_t07_postcondition_5_budget` は予算の試験だが 5 を名乗る（仕様書では 6）。
    `when:` を確かめる `test_t07_incoming_depth1_has_when_strings` は番号を名乗らない。T-07 の未対応は 2・4・6。
  - 番号の突き合わせで未対応と出た事後条件（計 10 件）: T-02 の 3・4、T-05 の 3・4、T-06 の 1・6、T-07 の 2・4・6、T-11 の 4。
    （T-06 の 1・T-07 の 2 は LSP 一致の事後条件。LSP を使う試験は在るが事後条件の番号を名乗っていない。）
  - 仕様書の項目が増減すると番号がずれるのに、対応を検査する物が無かった（設計書 R-9）。`bench/postcondition_map.py` は名前の一致だけを見るので、ずれた番号は「対応あり」に見える（T-07 の 3 は誤って対応ありと出る）。
- 再現: `uv run python bench/postcondition_map.py --m1-only`（`--json out.json` で `unmapped` と `tools`）。
- 直す役への手がかり: 試験の docstring に `PC:T-07#4` の形の印を付ければ、番号の名乗りと別に対応が取れる（postcondition_map.py の docstring 参照）。
