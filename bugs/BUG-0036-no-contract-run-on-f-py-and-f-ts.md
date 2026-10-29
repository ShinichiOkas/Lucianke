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

## 原因
- 契約テストのワークスペースは F-POLY の複製か合成 DB に限られ、取得済みの F-PY・F-TS（bench/.cache）を結線済みのサーバーで開く土台が tests/ に無かった（bench の台本だけが持っていた）。

## 直し方
- `tests/contract_fixtures/` を新設。`_rig.py` が F-PY・F-TS を tmp_path に複製（`.git`・`node_modules` を除く。原本は書き換えない）し、本番の合成ルートと同じ結線（RuntimeHub + LspPool(on_diagnostics) + WiredServices + Pipeline、全ツール登録）を組んで索引の完了と LSP の ready まで待つ。`conftest.py` が `rig`（F-PY / F-TS の 2 通り）・`slow` の印・sys.path（tests/contract の `_t11_harness`・`apply_unified_diff` を再利用）を用意。取得されていないフィクスチャは理由つきで skip。
- 試験: `test_fx_read_T00_T08.py`（T-00〜T-08）、`test_fx_edit_T10_T13.py`（T-10・T-11・T-13）、`test_fx_db_T19_T20.py`（T-19・T-20）。関数名・docstring に `postcondition_N` / `PC:T-xx#n` を入れた。LSP が無い F-TS は exact 水準が条件の事後条件を降格応答の約束で確かめる（skip しない。BUG-0057）。
- `bench/exit_criteria.toml` の `M1-1.contract-fpy-fts` は cmd がすでに `tests/contract_fixtures` を指しているので、古い「未整備」コメントだけ直した。

## 確認
- 全 44 件（14 ツール × F-PY・F-TS、うち T-02・T-03・T-08 は 2 本）が 1 回通しで green（352 秒、他の役の負荷あり）。min_tests = 28 を満たす。その後 T-13 を書き換えて T-10・T-11・T-13 の 12 件を再実行、T-11 の edits[] 長の比較を外して F-PY の T-11 を再実行して green。
- ツール × フィクスチャ: T-00・T-01・T-02・T-03・T-04・T-05・T-06・T-07・T-08・T-10・T-11・T-13・T-19・T-20 すべて F-PY・F-TS とも green。
- 見たこと（票にしていない）: T-11 の JSON 応答は httpx の rename で edits_count = 10 のとき edits[] が空だった回があった（max_tokens による切り詰めと見える。edits_count で比較に変更）。
- 適用外で試験していない事後条件: T-02#3・T-05#3（ipynb）、T-05#4（lexical_fallback の窓。BUG-0049）。F-PY・F-TS に該当ファイルが無い。
- 残り: bench/postcondition_map.py の SEARCH_DIRS に tests/contract_fixtures が無く、被覆表の F-PY / F-TS 列は 0 のまま（触れる範囲外）。
