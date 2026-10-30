# BUG-0065 全体テストで T-02（skeleton）系の 7 件が落ちる（`KeyError: 'children'`・`'signature'` が無い・F-PY で 2 ファイル目が返らない）

- 報告: 2026-10-31 / 見た人: BUG-0063・BUG-0064 を直した役（全体テストを流したとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 症状

`uv run pytest -q -p no:cacheprovider`（作業ツリー = `87d9451` + BUG-0063/0064 の未コミット差分。langs extra 同期済み・`bench/.cache` に httpx・ky あり）:
`7 failed, 3774 passed, 32 skipped in 989.66s`。落ちたのは次の 7 件だけ。

```
FAILED tests/contract_fixtures/test_fx_read_T00_T08.py::test_t02_postcondition_1_2_5_skeleton_matches_the_source[F-PY]
FAILED tests/unit/test_syntax_skeleton.py::test_fold_lines_and_data_agree - KeyError: 'children'
FAILED tests/unit/test_syntax_skeleton.py::test_json_item_shape - AssertionError
FAILED tests/unit/test_syntax_skeleton.py::test_include_private_false_drops_private
FAILED tests/unit/test_syntax_skeleton.py::test_raises_do_not_leak_across_nested_definitions
FAILED tests/unit/test_syntax_skeleton.py::test_signature_and_doc_text_come_from_source[src/shop/services/payment.py-python]
FAILED tests/unit/test_syntax_skeleton.py::test_signature_and_doc_text_come_from_source[web/src/payment.ts-typescript]
```

## 実測（エラーの行）

- `tests/unit/test_syntax_skeleton.py:74`（`flatten`）: `out.extend(flatten(s["children"]))` → `KeyError: 'children'`（5 件がこれ。対象の記号は `PaymentRequest.orderId` など子を持たない記号）
- `test_json_item_shape`: `AssertionError: assert 'signature' in {'urn': 'lk://app/src/shop/services/payment.py#PaymentRequest', 'path': ..., 'name': 'PaymentRequest', 'qualified_name': 'PaymentRequest', ...}`
- `test_t02_postcondition_1_2_5_skeleton_matches_the_source[F-PY]`（`get_file_skeleton`、`max_tokens=6000`）:
  `assert [i["path"] for i in items] == list(t.skeleton)` → 返った `items` は `['httpx/_urls.py']` の 1 件だけ。期待は `httpx/_urls.py` と `httpx/_api.py` の 2 件（`Right contains one more item: 'httpx/_api.py'`）

## 手がかり（見たことだけ）

- 落ちた 7 件はすべて T-02 / skeleton の json の形に関するもの。BUG-0063/0064 の差分（`core/limits.py`・`mcp/budget.py`・`mcp/pipeline.py`・`mcp/registry.py`・`tools/_base.py`・`mcp/render/graph.py`）は skeleton のコードを触っていない
- 直近に T-02 の json を変えたコミット: `1e1a9aa BUG-0055: drop null/empty fields from T-02 json symbol output to reduce token bloat`

## 再現

```powershell
uv run pytest -q tests/unit/test_syntax_skeleton.py
uv run pytest -q "tests/contract_fixtures/test_fx_read_T00_T08.py::test_t02_postcondition_1_2_5_skeleton_matches_the_source"
```
