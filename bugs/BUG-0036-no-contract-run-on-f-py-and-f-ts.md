# BUG-0036 契約テスト（事後条件）を F-PY・F-TS で回す仕組みが無い（M1-1 は F-POLY でしか確かめられない）

- 報告: 2026-10-29 / 見た人: M1 出口条件の検査台本の役（22b）/ 見たことだけ
- 見たこと:
  - 仕様書 §15.2 M1-1 は「14 ツールが第 11 章の事後条件を F-PY、F-TS、F-POLY で満たす」。
  - `tests/contract/*.py` のうち `bench/.cache/`（F-PY = httpx、F-TS = ky）・`bench/fixtures.toml`・`bench/fetch_fixtures.py` を参照する試験は 0 件。契約テストのワークスペースは F-POLY の tmp 複製か、手で入れた合成 DB だけ。F-PY / F-TS を使うのは `bench/run_latency.py`・`run_tokens.py`・`run_edit_safety.py` の台本で、事後条件の判定は入っていない。
  - `bench/postcondition_map.py` の「フィクスチャの被覆」: F-PY / F-TS で事後条件を試験しているツールは 14 本中 0 本。T-19・T-20 は F-POLY も使っていない（合成 DB）。
  - 取得は済んでいる（`bench/.cache/httpx`、`bench/.cache/ky`。SHA は fixtures.toml と一致）。
- 影響: `bench/exit_criteria.toml` の `M1-1.contract-fpy-fts` は `tests/contract_fixtures` が無いため「未整備」。M1-1 は永久に合格にならない。
- 再現: `uv run python bench/run_exit_criteria.py --only M1-1.contract-fpy-fts`、`uv run python bench/postcondition_map.py --m1-only | grep -A5 "フィクスチャの被覆"`。
- 直す役への手がかり（調査ではなく受入側の期待）: `tests/contract_fixtures/` に、取得済みの F-PY / F-TS をワークスペースにして 14 ツールの事後条件（件数は postcondition_map.py の `spec_total`）を確かめる試験を置けば、exit_criteria.toml の検査がそのまま拾う（`skips = "fail"`、下限 28 件）。
