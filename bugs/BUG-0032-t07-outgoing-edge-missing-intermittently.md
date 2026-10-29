# BUG-0032 T-07 outgoing（LSP 経路）で process_charge → Receipt の辺が出たり出なかったりする

- 報告: 2026-10-29 15:05 / 見た人: T-06・T-07 の LSP 経路の担当（20b）
- 見たこと:
  - 実の basedpyright で F-POLY の `PaymentProcessor.process_charge` の outgoing（depth 3）を T-07 で取ると、`("Receipt", 2)`（process_charge → Receipt）の辺が、探索の 1 回では出たが、テストの 2 回では出なかった。
  - 応答の警告に `call edge(s) were left out` が出ていた回がある。
  - 担当はこの辺をテストの必須キーから外した（揺れる検査を残さないため）。原因は調べていない。
- 再現手順: `tests/contract/test_T06_T07_lsp.py` の outgoing のテストで、`("Receipt", 2)` を必須に戻して 10 回。
- 調査はしていない（直す役の仕事）。
