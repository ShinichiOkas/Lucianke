# BUG-0054 format=json で、前置き・固定部（封筒・root・target・警告）だけで max_tokens を超える応答がある（max_tokens 200 で 223〜859）

- 報告: 2026-10-29 / 見た人: BUG-0014・BUG-0017 を仕上げた役
- 症状: 期待は仕様書 §5.2（`max_tokens` の最小は 200）と §5.7（`used_tokens_est` ≤ `max_tokens`）。実際は json で最小の `max_tokens=200` を指定すると、レコード 0 件（「入らない」警告つき）の応答でも封筒と固定部だけで 200 を超える。`used_tokens_est` は超過した実測値を正しく申告している（`Packer` の docstring の「保証できず、レコード 0 件で返す」）。
- 再現手順（`tests/fixtures/poly`、いずれも `format: "json"`、`max_tokens: 200`）と `budget.used_tokens_est`:
  - `query_call_hierarchy`（`PaymentProcessor.process_charge`、`both`、`depth: 5`）: 859（`data` に root 記号の JSON と固定部が約 2,300 文字。警告 `no record fits ... the next record needs ~144 tokens; raise max_tokens to ~974`）。depth 1・3 でも 700 超（`tests/contract/test_T06_T07.py::test_t07_postcondition_5_budget` が 200 のとき 731）。
  - `find_references_and_implementations`（`lk://_ext/decimal#Decimal`）: 356（`data` の target・totals・implementations などの固定部）。
  - `resolve_symbols`（`PaymentProcessor.process_charge` ほか 3 対象、LSP なしの降格）: 223（封筒 + 日本語の `accuracy_warning` + 「入らない」警告。`next record needs ~386 tokens`）。
  - 参考: `search_workspace_symbols`（`pay`、`limit: 50`）は 172 で収まるが、1 件 約 117 トークンなので 200 では 0 件。
- 頻度: 上の 3 ツールで 3/3（最小の予算のとき）。compact は同じ呼び出しで収まる。
- 備考: BUG-0017 の直しで json の封筒と `data` の固定部を予算に数えるようになった結果、以前は申告の過小で隠れていたもの。契約テストの 200 を使っていた json の予算は、実際の大きさに合わせて上げた（`test_T03_T04.py`・`test_T06_T07.py`）。
- 調査はしていない（直す役の仕事）。
