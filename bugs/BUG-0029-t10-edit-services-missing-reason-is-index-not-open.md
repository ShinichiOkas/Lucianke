# BUG: T-10 の「編集サービス未結線」テストが reason=index_not_open を受け取る

見たことだけ（調査していない）。

- 症状: `tests/contract/test_T10_edit_body.py::test_tool_without_wired_edit_services_says_so` が失敗する。
  期待は `E_UNSUPPORTED` + `details.reason == "edit_services_missing"`、実際は `E_UNSUPPORTED` + `details.reason == "index_not_open"`。
- 再現: `uv run pytest -q tests/contract/test_T10_edit_body.py::test_tool_without_wired_edit_services_says_so`
- 観測の状況: T-02 のデータファイル対応の確認で `uv run pytest -q tests/contract` を通したとき（425 passed, 2 failed）。
  もう 1 件の `tests/contract/test_T11_rename.py::test_real_rename_argument_errors`（行 397 のアサーション）は最初の全体実行で落ち、
  単独で再実行したら通った（揺れている可能性）。
- 関係しそうな作業ツリー上の他の役の変更（未調査）: `tools/_runtime` の RuntimeHub・WiredServices の結線。

## 原因
- 実装の不具合ではなく、テストの前提が古かった。`tests/contract/test_T10_edit_body.py::test_tool_without_wired_edit_services_says_so` は「編集サービスを結線していないビルド」のつもりで `app.build_services(lambda: [str(root)])` を渡していたが、結線の役が `build_services` を `WiredServices`（`tools/_runtime.py`）を返すように変えた後は、これは「結線済みだが `hub` が無い（索引の束が 1 つも開いていない）サービス」になる。
- T-10 の振り分け（`t10_edit_symbol_body._transaction`）は 2 段: ⑴ サービスが `edit_transaction` を持てば呼ぶ（`WiredServices.edit_transaction` は束が無ければ `E_UNSUPPORTED` + `reason=index_not_open`）、⑵ 持たなければ `locks` / `writer_index` / `journal_dir` を探し、無ければ `reason=edit_services_missing`。テストの入力は ⑴ に入るので `index_not_open` が正しい。`edit_services_missing` が出る入力は、そのどれも持たない素の `ToolServices` だけ。
- 仕様書 §5.8 は `E_UNSUPPORTED` に `details.reason` を縛っていない（理由の語は設計の裁量）。2 つの理由は「ビルドに編集サービスが無い（直らない）」と「束が開いていない（起動直後など。待てば直る）」の区別として意味がある。

## 直し方
- 実装は変えない（T-10・`_runtime.py` とも無変更）。
- `tests/contract/test_T10_edit_body.py`: 1 件を 2 件に分けた。
  - `test_bare_tool_services_without_edit_services_says_so`: 素の `ToolServices(roots=, trees=, lines=)` → `reason=edit_services_missing`、`missing == {locks, writer_index, journal_dir}`、ファイル無変更。
  - `test_wired_services_with_the_index_not_open_says_so`: `build_services(...)`（`WiredServices`、`hub` 無し）→ `reason=index_not_open`、`details.workspace_id` がある、`missing` は無い、ファイル無変更。
- `tests/unit/test_tools_runtime.py::test_without_a_hub_nothing_is_indexed`: `pytest.raises(LuciankeError)` だけだったのを、`E_UNSUPPORTED` + `reason=index_not_open` まで確かめるようにした（束の側の固定）。

## 確認
- `uv run --no-sync pytest -q tests/contract/test_T10_edit_body.py` → 47 passed（`test_T10_insert.py`・`test_T10_delete.py` は別の役の持ち分。削除は cc031cc で入った分を含めて 47 件が緑）。
- `uv run --no-sync pytest -q tests/unit/test_tools_runtime.py` → 15 passed。
- `uv run --no-sync ruff check` / `ruff format --check`（2 ファイル）→ clean。
- 票にあったもう 1 件（`tests/contract/test_T11_rename.py::test_real_rename_argument_errors`）は本票の範囲外。直している時点では別の役が `test_T11_rename.py` を `test_T11_rename_real.py` へ分けている最中（未コミット）で、このファイルは収集時に `NameError: WS` で止まっていたため、確かめていない。
