# BUG-0006 全体の pytest が赤（トークン推定器の作業中の変更、watcher テストの揺れ）

- 報告: 2026-10-29（BUG-0004 の確認で全体を 1 回走らせた担当）
- 見たこと（調査はしていない。直す役の仕事）:
  1. `uv run pytest -q` 全体で 23 failed / 2192 passed（BUG-0004 のコミット 91d43f0 の直後）。
     - `tests/unit/test_mcp_budget.py` の `test_estimator_char_classes[*]`・`test_estimator_mixed_text`・`test_pack_*`（計 20 件前後）、`tests/unit/test_mcp_render.py::test_render_output_within_budget_when_packed[2000]`。
     - `tests/contract/test_T05_bodies.py::test_truncated_body_is_a_prefix_and_tells_where`: `max_tokens=300` で `items` が空、`warnings` は `no record fits in max_tokens=300: the next record needs ~504 tokens; raise max_tokens`（同じテストは 90 分前の BUG-0004 の作業中に 3 回続けて green だった）。
     - `tests/contract/test_T02_skeleton.py::test_compact_output_is_grammatical_and_structural`（全体の 1 回目では green、2 回目で red。実行ごとに揺れた）。
     - この時点で作業ツリーに未コミットの `src/lucianke/core/tokens.py`・`tests/unit/test_core_tokens.py` の変更と、未追跡の `tests/unit/test_core_tokens_calibration.py` があった（別の担当の作業中）。
  2. `tests/unit/test_core_model.py::test_kind_vocabulary_matches_spec_document` も赤だった（`Kind` に `File` が無い）。別票 BUG-0005-kind-vocabulary-missing-file.md が先にあるので、そちらで扱う。
  3. `tests/unit/test_workspace_watcher.py::test_expansion_of_a_huge_moved_in_directory_becomes_a_rescan`: 全体の 1 回目で failed、単体の再実行では passed（揺れ）。
- 再現手順: `uv run pytest -q`（全体）。1 は未コミットの tokens 変更がある作業ツリーで再現した。

## 追記（2026-10-29 直す役）: 見たこと（調査の入口）
- 追加の揺れ: `tests/unit/test_workspace_watcher.py::test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes` が全体実行で落ちる（BUG-0001 の役の観察）。負荷なしの単体は green。

## 原因
- **burst 600 件（実 FS）**: `awatch` は「新しいパスが 1 step（10 ms）来なければ窓を配信する」。テストは 600 ファイルを同期で作る。負荷で書き手のスレッドが 10 ms 以上止まると、先頭の約 42〜67 件が小さい窓（`WATCH_BURST_HINT`=100 未満）として配信され、S-06 どおり「小さい窓は待たず即処理」で `changed` が個別に出る。残りは合算されて rescan 1 回になる。つまり実装は仕様どおりの動きで、`changed` は冪等なので索引は正しい。テストの待ち方（`step=10` のまま、書き込み中の停止に弱い）が原因。再現: 48 コアに 56 本の busy プロセスを走らせた負荷の下で、watcher のテスト 1 ファイルを繰り返すと 10 回中 6 回、毎回このテストだけが同じ形（`d0/f0.py` から 42〜67 件の changed が先頭に混じる）で落ちる。
- **巨大ディレクトリ移し入れ**: 負荷の下でも、全体実行 2 回でも再現しなかった（単体 30 回・負荷 25 回・全体 2 回で green）。落ちた全体の出力が票に残っていないため、確証は無い。コードの読みで、テストに外の揺れが入りうる 2 点を見つけた。(a) `os.rename` による大きなディレクトリの移動は、Windows でウイルス対策や索引作成が開いていると `PermissionError` になりうる。(b) 移した直後に同じ要因が子の属性を触ると、少数の `changed` が後から届き、`changed == ["zz_barrier.py"]` の厳密比較が落ちる。
- **T-02（`test_compact_output_is_grammatical_and_structural`）**: テスト自体の非決定性は見つからなかった。純関数（`core/tokens.py` に状態なし）、`PYTHONHASHSEED` は実行ごとに変わるが 25 回中の落ちは下記の 1 種類だけ、フィクスチャを書き換えるテストなし（編集系は `copytree` の複製）。赤・緑が変わった実体は、**共有の作業ツリーで別の役が未コミットのまま `compact_grammar.py`・`core/tokens.py`・`mcp/budget.py`・`core/model.py` を書き換えていた**こと。裏付け: 同じ 25 回の繰り返しの最後の 3 回（run 23〜25）が連続で `TypeError: Packer.pack() got an unexpected keyword argument 'record_text'`（`mcp/budget.py` と呼び手の途中状態の食い違い）による E_INTERNAL で落ち、その間に `git status` で `mcp/budget.py`・`mcp/pipeline.py` ほかが M だった。
- **定数**: `core/limits.py` の `WATCH_DEBOUNCE_MS = 50` が S-06 の結論（debounce=1000、step=10）と食い違い、`STEP_MS`・`DEBOUNCE_MS`・`BURST_HINT`・`BURST_QUIET_S`・`EXPECT_TTL_S` は `watcher.py` にベタ書きだった（limits.py の「数値は limits.py にだけ置く」に反する）。

## 直し方
- 定数（コミット 9c16f93、57f519d）: `WATCH_STEP_MS=10`、`WATCH_DEBOUNCE_MS=1000`、`WATCH_BURST_HINT=100`、`WATCH_BURST_QUIET_S=0.3`、`WATCH_EXPECT_TTL_S=10.0` を `core/limits.py` に置き、`watcher.py` は import する（`__all__` からは外した）。`tests/unit/test_core_limits.py` の表を S-06 の値に更新。設計書 §7.3.6-3 の定数名を合わせた。
- burst 600 件（コミット 5b3af6c）: 実 FS のテストは `step_ms=400` で動かし（書き込み途中の停止に負けない）、窓の合算の規則は **決定的なテスト 2 件** で確かめる（`_consume` に窓を直接流す: 120 件×5 窓は rescan 1 回・個別 0 回、100 件未満の窓は待たずに個別処理）。実装は変えていない（仕様どおり）。
- 巨大ディレクトリ移し入れ（コミット 4414fad の前後の次のコミット）: ディレクトリの移動を `move_dir`（`PermissionError` を最大 5 秒再試行）に統一し、主張を「最初の呼び出しが rescan」「rescan は 1 回」「見張りが届く」「`changed` が `WATCH_BURST_HINT` 未満」に改めた（後から届く少数の `changed` は冪等で無害なので許す）。
- T-02: テストは変えていない（直すべき揺れが見つからなかった）。別の役の未コミットの途中状態が原因なので、その役がコミットすれば解ける。

## 確認
- 56 本の busy プロセス（48 コア）の下で `tests/unit/test_workspace_watcher.py`（26 件）を繰り返し: 直す前は 10 回中 6 回赤（いずれも burst 600 件）。直した後は 16 回連続 green（burst 修正後）、さらに 14 回連続 green（巨大ディレクトリの堅牢化後）。
- 全体 `uv run pytest -q tests`（負荷つき）を 2 回: T-02、`test_mcp_budget`、`test_mcp_render[2000]`、T-05 の `test_truncated_body_is_a_prefix_and_tells_where` は green（トークン推定器の作業は 27e0821 でコミット済み）。burst 600 件だけが赤で、これは直した（その 2 回の実行は修正前に始めたもの）。
- T-02 を負荷つきで 25 回: 22 回 green、最後の 3 回は他の役の途中状態（上記）。

## 残り（別の役へ）
- 全体の 2 回で赤だった他の項目（見たことだけ。未調査）: `tests/unit/test_index_ingest.py::test_external_symbols_get_a_minimal_row`、`tests/unit/test_lsp_features.py::test_real_*`（定義と参照・stdlib の定義・hover・call hierarchy・code action）、`tests/unit/test_syntax_heuristic.py::test_outline_uses_ctags_through_a_child_process`（BUG-0009 の直し 8a9d3af の後も赤）、`tests/contract/test_T00_configure.py::test_post3_set_python_env_normalizes_the_interpreter_and_restarts_the_python_lsp`（1 回のみ）。
- `test_single_write_changed_once_within_budget` の `count("a.py") == 1` は、書き手が open と write の間で 10 ms 以上止まると 2 回届きうる（負荷 25 回で未再現）。`changed` は冪等なので `>= 1` に緩める案。
- 実装の穴（仕様の範囲内だが記録）: 窓が全部 100 件未満に割れて合計が 500 を超える場合は rescan にならず個別処理になる（正しさは保つ）。窓をまたいだ累計で rescan に切り替える案があるが、S-06 の型からの変更なので師匠の判断。
