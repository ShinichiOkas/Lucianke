# BUG-0005 全体の pytest が赤（トークン推定器の作業中の変更と、Kind に File が無い）

- 報告: 2026-10-29（BUG-0004 の確認で全体を 1 回走らせた担当）
- 見たこと（調査はしていない。直す役の仕事）:
  1. `uv run pytest -q` 全体で 23 failed / 2192 passed（BUG-0004 のコミット 91d43f0 の直後）。
     - `tests/unit/test_mcp_budget.py` の `test_estimator_char_classes[*]`・`test_estimator_mixed_text`・`test_pack_*`（計 20 件前後）、`tests/unit/test_mcp_render.py::test_render_output_within_budget_when_packed[2000]`。
     - `tests/contract/test_T05_bodies.py::test_truncated_body_is_a_prefix_and_tells_where`: `max_tokens=300` で `items` が空、`warnings` は `no record fits in max_tokens=300: the next record needs ~504 tokens; raise max_tokens`（同じテストは 90 分前の BUG-0004 の作業中に 3 回続けて green だった）。
     - `tests/contract/test_T02_skeleton.py::test_compact_output_is_grammatical_and_structural`（全体の 1 回目では green、2 回目で red。実行ごとに揺れた）。
     - この時点で作業ツリーに未コミットの `src/lucianke/core/tokens.py`・`tests/unit/test_core_tokens.py` の変更と、未追跡の `tests/unit/test_core_tokens_calibration.py` があった（別の担当の作業中）。
  2. `tests/unit/test_core_model.py::test_kind_vocabulary_matches_spec_document`: 仕様書 §6.1 の `kind` の規則に `File` があり、`core/model.py` の `Kind` に無い（`Extra items in the left set: 'File'`）。仕様書側は 2fbb15b で `File` が入った。
  3. `tests/unit/test_workspace_watcher.py::test_expansion_of_a_huge_moved_in_directory_becomes_a_rescan`: 全体の 1 回目で failed、単体の再実行では passed（揺れ）。
- 再現手順: `uv run pytest -q`（全体）。1 は未コミットの tokens 変更がある作業ツリーで、2 はコミット済みの状態で再現する。
