# BUG-0016 get_symbol_bodies で本体が予算に収まらないとき、next_cursor が無く「line_range で続きを」の注記だけ（budget.truncated も false）

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状: 期待は仕様書 §11 T-05 処理規約 7「1 対象が予算に収まらないときは先頭から詰め、`truncated_at_line` を記録して `next_cursor` で同じ対象の続きを返す」。実際は `items[].truncated: true`・`truncated_at_line` は出るが、`page.next_cursor` は null、compact の最終行は `- truncated at line 387: pass line_range to continue`（`# more: cursor=` は無い）。`budget.truncated` も false のまま。
- 再現手順: `get_symbol_bodies {"targets":[{"path":"src/lucianke/mcp/budget.py","symbol_name":"Packer"}], "format":"json"}`（max_tokens 既定 3000）→ `page: {"next_cursor": null, "returned": 1, "total_estimate": 1}`、`budget: {"max_tokens": 3000, "used_tokens_est": 2995, "truncated": false}`、`items[0].truncated: true`、`truncated_at_line: 387`、`range_returned` 190-386（シンボルの範囲は 190-395）。compact は同じ引数で最終行が上記の注記。
- 補足の観察: `max_tokens: 200` では本体が 1 行も返らず（BUG-0015）、先頭から詰めた部分も出ない。
- 頻度: 1 件中 1（予算を超える本体で毎回）。
- 調査はしていない（直す役の仕事）。

## 原因
- 本体を切る縮退（`Record.shrink`）の結果を、パッカーが「そのレコードは全部入った」と扱っていた。T-05 の縮退版は 1 レコードの途中で切れた版（続きがある）だが、パッカーには「レコードの途中」を表す道が無く、最後の 1 件が縮退で入ると `next_cursor`・`remaining`・`budget.truncated` を出さなかった（T-02 の縮退は「省略した全体」で、続きが無いので同じ扱いでよかった）。カーソルも「レコードの添字」しか持てず、同じ対象の続き（行）を指せなかった。
- 注記が `pass line_range to continue` だけで、`end` が応答のどこにも無かった。
- 副次: 縮退の二分探索の下限が前置き（`context_lines`）の先頭だったので、切り口が前置きの途中に落ちると本体が 1 行も入らないまま切れ、続き（`line_range` は本体範囲に丸める）で前置きの残りの行が隙間になりうる。

## 直し方
- `Record.resume`（`core/records.py`）: 縮退版が途中で切れているときの続きの位置（T-05 は続きの行）。カーソルのオフセットの上位 32 ビットに載せる（`join_offset` / `split_offset`。既存ツールの添字だけのオフセットはそのまま）。カーソルの符号化・パイプライン・`ToolContext` は変えていない。
- パッカー（`mcp/budget.py`）: 途中で切れた縮退版を最後の 1 件に入れたら、`next_cursor` を「同じレコードの `resume` から」にし、`remaining` にそのレコードの残りを数え、`budget.truncated` を立てる。続きのトレーラ（再開カーソルは長い）を実際の形で見込んで縮退の予算を取り、切れたレコードの後ろは詰めない（順を保つ）。
- T-05（`tools/t05_get_symbol_bodies.py`）: 切った版に `resume=truncated_at_line` を付け、続きのカーソルでは `split_offset(ctx.cursor_offset)` の対象をその行から作り直す（前置きなし。行がずれていれば `E_CURSOR_EXPIRED`）。応答に `items[].continue_with.line_range = {start, end}` と注記 `truncated at line N of M: continue with the cursor, or line_range N-M` を出す。縮退の下限を本体の先頭の行にして、前置きの途中では切らない。
- テスト: `tests/unit/test_mcp_budget_resume.py`（パッカーの続き）、`tests/contract/test_T05_bodies.py`（予算 3 通り × `context_lines` 2 通りでカーソルを辿り、行が連続し連結が全体とバイト一致する／前置き 20 行の対象で切り口が前置きに落ちない／`continue_with` の `line_range` で続きが取れ compact に `# more: cursor=` が出る／複数対象で切った対象の後ろに次を詰めない）。

## 確認
- 票の再現（`Packer` を予算 3000 で）と同じ形（予算に収まらない本体）で、`page.next_cursor` が出て `budget.truncated: true`、`# more: cursor=` と注記が出ることを契約テストで確認。カーソルで辿ると、各ページの `range_returned` が前のページの終わりの次から始まり、連結が `full["code"]` とバイト一致。
- `tests/contract/test_T05_bodies.py`・`tests/unit/test_mcp_budget.py`・`tests/unit/test_mcp_budget_resume.py` を 3 回続けて green（115 件）。前置きの途中で切る版に戻すと前置きのテストが落ちることを確認。
- 観察（範囲外・見たことだけ）: HEAD のままで `tests/contract/test_T10_edit_body.py::test_tool_without_wired_edit_services_says_so` と `tests/contract/test_T13_code_action.py::test_real_quickfix_by_kind_is_ambiguous_and_lists_candidate_ids` が落ちる（BUG-0016 の変更なしで再現）。
