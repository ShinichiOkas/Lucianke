# BUG-0040 事後条件の番号が仕様書と契約テストでずれている（T-06・T-07）。名乗る試験が無い事後条件が M1 の 14 ツールで 10 件

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

## 原因
- 仕様書 §11 の事後条件は箇条書きの出現順が番号で、T-06・T-07 に項目 5（外部シンボル）が増えたとき、後ろの項目の番号が 1 つ動いた。契約テストは古い番号を名前に焼いたまま（T-06 の予算は 5 → 6、T-07 の循環は 3 → 4、予算は 5 → 6）で、仕様書と試験の対応を見張る物が無かった（設計書 R-9）。`bench/postcondition_map.py` は名前の番号の一致だけを見るので、ずれた番号がたまたま範囲内だと「対応あり」に見えた（T-07 の 3 など）。
- 未対応の 10 件の内訳:
  - 番号が動いただけ（試験は在る）: T-06 の 6（予算）、T-07 の 4（循環）・6（予算）、T-07 の 3（`when:` の試験が番号を名乗っていなかった）。
  - 試験は在るが事後条件の番号を名乗っていない: T-02 の 4（21 件）、T-11 の 4（`applied: false` で全ファイル不変）。
  - 試験が事後条件の文を確かめていなかった: T-02 の 3（`outputs` の文字列が応答に出ない。固定の合言葉の不在しか見ていなかった）、T-06 の 1・T-07 の 2（期待集合をテスト内で手書きしていて、LSP に直接聞いた結果と突き合わせていなかった）、T-06 の 5・T-07 の 5（外部シンボル。T-07 は他の試験の一部として確かめるだけで、T-06 は名前が別の項目を名乗っていた）。
  - ツールが未実装: T-05 の 3（`cell:<n>`）・4（`lexical_fallback` の窓）。`E_UNSUPPORTED` で断るので、ツール経由では確かめられない（BUG-0049）。

## 直し方
- 番号を仕様書の並びに合わせた（関数名の番号が最優先で読まれるので、名前を直した）: `test_t06_postcondition_4_5_…` -> `…_4_6_…`（`PC:T-06#4`・`PC:T-06#6`）、`test_t07_postcondition_3_cycle_…` -> `…_4_cycle_…`、`test_t07_postcondition_5_budget` -> `…_6_budget`、`test_t07_incoming_depth1_has_when_strings` -> `test_t07_postcondition_3_incoming_depth1_has_when_strings`。
- 印（docstring の `PC:T-NN#n`）を付けた: T-02 の 4（`test_batch_size_is_rejected_without_items`）、T-11 の 4（`test_stub_prepare_rename_rejection_…`・`test_stub_rename_timeout_…`・`test_stub_unacceptable_edits_write_no_file`）、T-06 の 5・T-07 の 5（`test_real_external_nodes_have_stable_kinds_and_are_leaves`）、T-07 の 3（実索引の `when:`）。
- 試験を足した（F-POLY）:
  - T-02 の 3: `test_t02_postcondition_3_notebook_outputs_never_appear_in_the_response`（Notebook JSON の `outputs` から集めた文字列が compact・json のどちらにも出ない。空の突き合わせにしない）。
  - T-06 の 1: `test_t06_postcondition_1_exact_references_equal_the_lsp_result`（`include_declaration` の False・True で、`features.references` に直接聞いた集合と等しい。実機の basedpyright。無ければ skip）。
  - T-06 の 5: `test_real_t06_postcondition_5_unresolved_import_is_an_external_symbol_with_import_sites`（`lk://_ext/decimal#Decimal` が `is_external` で索引に載り、`use_kinds=["import"]` の参照が 5 ファイルの `from decimal import Decimal` の行と等しい）。
  - T-07 の 2: `test_t07_postcondition_2_exact_incoming_depth1_equals_the_lsp_result`（`features.incoming_calls` に直接聞いた集合と等しい）。T-06 の 1 と同じ新ファイル `tests/contract/test_T06_T07_lsp_postconditions.py`（`test_T06_T07_lsp.py` は他の役の未コミット差分があったので、フィクスチャだけ import して別ファイルにした）。
  - T-07 の 5: `test_t07_postcondition_5_unresolved_import_callee_is_an_external_leaf`（構造版）。
  - T-11 の 4: `test_t11_postcondition_4_applied_false_leaves_every_file_unchanged`（`dry_run` と期待ハッシュ不一致のブロックで、全ファイルの SHA-256 が不変）。
  - T-05 の 3・4: ツールが未実装（BUG-0049）。仕様書どおりの本物のアサーションを持つ `test_t05_postcondition_3_…`・`test_t05_postcondition_4_…` を `xfail(strict=True)` で足した。**対応表の 0 はこの 2 件が xfail で名乗ることで出ている**（ツール経由では満たされていない。BUG-0049 を直すと XPASS で赤になり、xfail を外させる）。
- 各試験ファイルの冒頭の docstring にあった古い番号の記述（T-02 の「未知言語（事後条件 3）」、T-06/T-07 の「2〜5」「1・3・4・5」）を直した。

## 確認
- `.venv/Scripts/python.exe bench/postcondition_map.py --m1-only` -> 「M1 事後条件 68 件、未対応 0 件、EP 未対応 0 件、範囲外 0 件、ツール不明 0 件」（直す前は未対応 10 件）。`bench/exit_criteria.toml` の `M1-1.postcondition-map`（`unmapped_total == 0`）が満たされる。
- 触った試験（`test_T02_skeleton.py`・`test_T02_data_files.py`・`test_T05_bodies.py`・`test_T06_T07.py`・`test_T06_T07_lsp_postconditions.py`・`test_T11_rename.py`）: 118 passed、2 xfailed（T-05 の 3・4）。`ruff check` は green。
- 残り（この票の外）: 対応表は番号の一致しか見ない（中身の突き合わせは人が印を付けて直す）。T-05 の 3・4 の実装は BUG-0049。
