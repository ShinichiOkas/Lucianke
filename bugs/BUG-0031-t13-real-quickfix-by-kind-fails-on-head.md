# BUG-0031 test_real_quickfix_by_kind_is_ambiguous_and_lists_candidate_ids が HEAD でも落ちる

- 報告: 2026-10-29 15:03 / 見た人: BUG-0016 を直す役（HEAD に自分の変更だけを載せた隔離ワークツリーで、周辺の契約テストを回したとき）
- 見たこと:
  - `tests/contract/test_T13_code_action.py::test_real_quickfix_by_kind_is_ambiguous_and_lists_candidate_ids`（実の ruff）が落ちた。
  - BUG-0016 の変更を載せない HEAD でも同じく落ちた。
  - 失敗の出力は報告に無い。
- 再現手順: HEAD の使い捨て作業ツリーで `.venv/Scripts/python.exe -m pytest -q tests/contract/test_T13_code_action.py -k quickfix_by_kind`。
- 調査はしていない（直す役の仕事）。

## 原因
- 製品の不具合ではなく、**テストが実の ruff を探す場所**の問題だった。報告の再現環境は `.venv` の無い隔離ワークツリー（git worktree）で、テストは `find_command(_RUFF, workspace_root=REPO, venv=REPO / ".venv")` と `REPO` 基準で `.venv` を探す。`.venv` が無いと PATH に落ち、この機械の PATH にある **ruff 0.11.13**（`ruff --version`）が使われた。uv.lock が固定する dev グループの ruff は 0.17.3。
- ruff 0.11.13 は QuickFix に `isPreferred` を付けない（診断の `data` に `is_preferred` が無く、codeAction の応答にも `isPreferred` が出ない）。0.17.3 は「Remove unused import」に `isPreferred: true` を付ける（ruff server を JSON-RPC で直接叩いて確認。診断の `data.is_preferred: true` → 応答の `isPreferred: true`）。
- T-13 の種別指定は候補を `isPreferred` → 供給元の役 → kind → title の順に並べる（`_choose_by_kind`、docstring のとおり。c20de15 から変更なし）。`isPreferred` が付かない ruff だとタイトル順になり、`Ruff (F401): Disable for this line` が `Ruff (F401): Remove unused import: \`os\`` より先に来る。テストは「先頭が Remove unused import かつ is_preferred」を要求しているので、そこで落ちた（実測: `candidates[0].title == "Ruff (F401): Disable for this line"`、両方 `is_preferred: false`）。
- 切り分け: 同じ HEAD（1d8700d）の使い捨て作業ツリーに `.venv`（共有ツリーのものへのジャンクション）を置くと T-13 の契約テスト 23 件が全部 green。lsp/client.py（BUG-0010 の変更、applyEdit）・lsp/features.py（`code_actions` は応答の dict をそのまま通す）・T-13 本体・負荷による揺れは無関係。
- 同じ `venv=REPO / ".venv"` の探索が basedpyright のテスト（T-04・T-06/T-07・T-11 の harness・lsp_features）にもあり、`.venv` の無い作業ツリーでは PATH 上の別の版を掴む恐れがあった。

## 直し方
- 実機の LSP を探す `venv` を、テストを走らせている Python 環境（`Path(sys.prefix)`。uv.lock が固定する dev グループ）にした。対象は `tests/contract/test_T13_code_action.py`・`test_T04_lsp.py`・`test_T06_T07_lsp.py`・`_t11_harness.py`、`tests/unit/test_lsp_features.py`（ruff と basedpyright の両方）。`.venv` の有無・作業ツリーの場所に依らず、固定された版を使う。
- 期待値は変えない。タイムアウトの水増し・再試行・skip は入れていない。`isPreferred` が付かない版の ruff を掴んだときに原因が分かるよう、T-13 のそのアサートに使った ruff のパスと候補の中身を失敗メッセージに出す。
- 製品コードは変更なし（`isPreferred` を付けない LSP のときにタイトル順になるのは仕様の docstring どおり）。

## 確認
- `.venv` の無い作業ツリー（1d8700d）で、修正前は `test_real_quickfix_by_kind_is_ambiguous_and_lists_candidate_ids` が失敗、修正後は `tests/contract/test_T13_code_action.py` 23 件 green。
- 同じ作業ツリーで `test_T04_lsp.py`・`test_T06_T07_lsp.py`・`test_T11_rename_real.py`・`tests/unit/test_lsp_features.py` の 56 件 green。
- 負荷（CPU を食う 16 プロセスを並行）の下で `tests/contract/test_T13_code_action.py` を 10 回続けて実行: 10 回とも 23 件 green（18〜34 秒）。
