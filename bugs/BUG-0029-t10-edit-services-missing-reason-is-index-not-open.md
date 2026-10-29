# BUG: T-10 の「編集サービス未結線」テストが reason=index_not_open を受け取る

見たことだけ（調査していない）。

- 症状: `tests/contract/test_T10_edit_body.py::test_tool_without_wired_edit_services_says_so` が失敗する。
  期待は `E_UNSUPPORTED` + `details.reason == "edit_services_missing"`、実際は `E_UNSUPPORTED` + `details.reason == "index_not_open"`。
- 再現: `uv run pytest -q tests/contract/test_T10_edit_body.py::test_tool_without_wired_edit_services_says_so`
- 観測の状況: T-02 のデータファイル対応の確認で `uv run pytest -q tests/contract` を通したとき（425 passed, 2 failed）。
  もう 1 件の `tests/contract/test_T11_rename.py::test_real_rename_argument_errors`（行 397 のアサーション）は最初の全体実行で落ち、
  単独で再実行したら通った（揺れている可能性）。
- 関係しそうな作業ツリー上の他の役の変更（未調査）: `tools/_runtime` の RuntimeHub・WiredServices の結線。
