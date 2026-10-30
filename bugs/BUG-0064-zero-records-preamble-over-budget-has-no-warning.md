# BUG-0064 レコードが元から 0 件で前置きが予算超過のとき warnings が空（必要な max_tokens を名指ししない）

- 報告: 2026-10-30 / 見た人: BUG-0063 を直す役
- 症状: `format: "json"` でレコード（`records`）が元から 0 件で、前置き（`data_static` → `_resolution_meta.accuracy_warning` や `data.root` など）だけで `max_tokens` を超えるとき、応答は `is_error=False` ・`returned=0` だが **`warnings` が空**。利用者に「この呈び出しにはあとどれだけ `max_tokens` が要るか」が伝わらない。
- 実測（`tests/fixtures/poly`、`PaymentProcessor.process_charge`、`direction=both`、`depth=5`、`format=json`、`max_tokens=200`）:
  - `query_call_hierarchy`: `used=365`、`returned=0`、`warnings=[]`
- 対比（こちらは warning が出る）: レコードが **1 件以上あるが**前置きで入らないときは `_fit` の `m==0` 経路が `no record fits in max_tokens=N: the next record needs ~X tokens; raise max_tokens to ~Y ...` を返す（`budget.py` L490-496）。
- 残る問題の構造: `budget.py` L246 `if start >= n:`（レコードがそもそも 0 件・続きも無い）が `self._page((), reserved_tokens, ..., ())` を返すだけで、`reserved_tokens > max_tokens`（前置きだけで超過）でも warnings を付けない。
- 関連: BUG-0063（固定下限廃止・動的下限化。(A) の振る舞い）。この穴は BUG-0063 の json 側の症状の残り。
- 調査はしていない（直す役の仕事）

## 原因・直し方・確認（2026-10-31）

- 原因: `Packer.pack` の `start >= n`（詰めるレコードが無い）経路が、`reserved_tokens > max_tokens` でも警告なしでページを返していた。警告を出すのはレコードが 1 件以上ある `_fit` の `m == 0` 経路だけだった。
- 直し方: `start >= n` かつ前置きが予算を超えるとき、`the response preamble alone needs ~N tokens, over max_tokens=M; raise max_tokens to ~N or more and call again with the same arguments` を `warnings` に入れ、`used_tokens_est` に警告行の分を足す。dot・mermaid も同じ経路を通る。
- 確認: 単体 `tests/unit/test_mcp_budget.py::test_pack_empty_records_with_preamble_over_budget_names_required_max_tokens`（境界: 予算 = 前置きなら警告なし）。`bench/_budget_probe2.py` の実測: T-07 json は max_tokens=200 で `~365` を名指し、365 で呼び直すと used=365・警告なし。T-06 json は `~384` → 384 で used=384・警告なし。
