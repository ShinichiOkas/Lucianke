# BUG-0012 test_real_hover_returns_the_type_string が負荷の下で落ちる

- 報告: 2026-10-29 14:31 / 見た人: BUG-0005 の担当（`uv run pytest -q tests/unit` の 3 回目）
- 見たこと:
  - `tests/unit/test_lsp_features.py::test_real_hover_returns_the_type_string`（実の basedpyright）が全体実行の 3 回目で落ちた。1・2 回目は通った。
  - 単独の再実行では通る。失敗時の出力は取れていない。
  - 同時に 20 本ほどの役がテスト・LSP を走らせていた（負荷あり）。
- 再現手順: `uv run pytest -q tests/unit/test_lsp_features.py -k real_hover` を、別のプロセスで pytest を数本走らせながら 10 回。
- 調査はしていない（直す役の仕事）。

## 追記（見たことだけ）
- 2026-10-29 14:36 / 見た人: BUG-0008 の担当。`uv run pytest -q tests/unit tests/contract`（3 分 43 秒、他の役が並行で走行中）で `tests/unit/test_lsp_features.py::test_real_definition_and_references` も `test_real_hover_returns_the_type_string` と一緒に落ちた（実の basedpyright の 2 件）。単独の再実行（同ファイル 34 件）では全件通った。失敗時の出力は取れていない。
