# BUG-0001 公開 DB の読み取り専用テストが全体実行のときだけ落ちる（揺れ）

- 報告: 2026-10-29 14:00 / 見た人: シンボル抽出の担当（全体 pytest を実行した）
- 症状: `uv run pytest -q` の全体実行で `tests/invariants/test_public_db_readonly.py` が 5 件落ちる。
  - delete-multi_drop、snapshot の delete・wal、authorizer-pragma_journal_delete、wal-multi_drop
- 対照: `uv run pytest -q tests/invariants/test_public_db_readonly.py` の単独実行では 190 passed。
- 再現手順: リポジトリ直下で全体実行 → 上の 5 件が落ちる。単独実行 → 落ちない。
- 調査はしていない（直す役の仕事）。

## 原因

**再現せず。原因は特定できていない。** 製品（`public.py`）とテストのどちらの不備とも言えない。

見たこと（調査の記録）:

- 報告時刻 14:00 は、テスト（cdee5ee 13:55:56）と `public.py` の最後の変更（01d137b 13:58:42）の直後で、他の役が同じ作業ツリーを書き換え続けていた。
- 01d137b の差分は `open_public` の未使用の `timeout_ms` 引数を外しただけで、落ちた 5 件（`multi_drop` は静的検査だけ、`snapshot` の検査は `public.py` を使わない）を説明しない。
- ファイルの共有状態は見つからなかった: モジュール固定の `template` は不変の bytes を返すだけ、`db` は 1 テストごとに自分の `tmp_path/db` を作る、`sqlite3` や `os` を差し替えるテストは `tests/invariants` より前（`contract`・`e2e`）に無い、`--basetemp` や固定の一時パスも無い。
- ランダム順・xdist のプラグインは入っていない（pytest 9.2.0、プラグインは asyncio のみ）。
- 負荷での再現を試した: このファイルの単独実行を 24 並列 × 3 回、6 並列 × 3 回（いずれも 190 passed）、全体実行を直接 4 並列・`uv run` 6 並列。公開 DB のテストは 1 度も落ちなかった。

## 直し方

コードは変えていない（変えるべき不備を特定できなかったため）。「揺れ」を隠すための再試行や `snapshot` の緩和は入れない（検査の価値を下げるため）。
再発したときに原因を取れるよう、落ちた時点の pytest の出力（例外メッセージ）を票に貼ること。今回の票には症状がテスト名だけで、メッセージが無かった。

## 確認（全体実行 3 回 green）

`uv run pytest -v` の全体実行を 3 回続けて、`tests/invariants/test_public_db_readonly.py` は毎回 190 passed / 0 failed。
全体の他の失敗は他の役の作業中の変更で、このバグ票の範囲外（下記を別票として報告）:

- `tests/unit/test_syntax_heuristic.py::test_outline_uses_ctags_through_a_child_process` は並列実行で 2〜3 割の確率で落ちる（`tempfile.gettempdir()` の `lucianke-ctags-*` を数えるので、同時に走る別セッションの一時物を拾う疑い。見たことだけ）
- `tests/unit/test_mcp_budget.py`・`tests/unit/test_lsp_features.py`・`tests/unit/test_core_model.py::test_kind_vocabulary_matches_spec_document`・`tests/unit/test_syntax_adapters.py` 5 件・`tests/unit/test_workspace_watcher.py::test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes`・`tests/contract/test_T03_T04.py::test_t03_in_tools_list`・`tests/contract/test_T05_bodies.py::test_truncated_body_is_a_prefix_and_tells_where` が、実行によって落ちた（他の役が作業中）
