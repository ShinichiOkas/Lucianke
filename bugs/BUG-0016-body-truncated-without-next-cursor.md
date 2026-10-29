# BUG-0016 get_symbol_bodies で本体が予算に収まらないとき、next_cursor が無く「line_range で続きを」の注記だけ（budget.truncated も false）

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状: 期待は仕様書 §11 T-05 処理規約 7「1 対象が予算に収まらないときは先頭から詰め、`truncated_at_line` を記録して `next_cursor` で同じ対象の続きを返す」。実際は `items[].truncated: true`・`truncated_at_line` は出るが、`page.next_cursor` は null、compact の最終行は `- truncated at line 387: pass line_range to continue`（`# more: cursor=` は無い）。`budget.truncated` も false のまま。
- 再現手順: `get_symbol_bodies {"targets":[{"path":"src/lucianke/mcp/budget.py","symbol_name":"Packer"}], "format":"json"}`（max_tokens 既定 3000）→ `page: {"next_cursor": null, "returned": 1, "total_estimate": 1}`、`budget: {"max_tokens": 3000, "used_tokens_est": 2995, "truncated": false}`、`items[0].truncated: true`、`truncated_at_line: 387`、`range_returned` 190-386（シンボルの範囲は 190-395）。compact は同じ引数で最終行が上記の注記。
- 補足の観察: `max_tokens: 200` では本体が 1 行も返らず（BUG-0015）、先頭から詰めた部分も出ない。
- 頻度: 1 件中 1（予算を超える本体で毎回）。
- 調査はしていない（直す役の仕事）。
