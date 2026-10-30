# BUG-0095 型の分からない受け手の属性の読み書き（`mon.stdout_lines += 1`）は、構文木でも名前の一致でも結ばれず、T-06 に何も書かれない

- 報告: 2026-10-31 / 見た人: BUG-0085 を直す役（直しの前後を比べていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（Lucianke 自身 `8c10a2a` のスナップショットを in-process に索引。LSP なしの `structural`）

- BUG-0085 の直し（型の分からない名前の受け手を同じファイルの唯一の同名に `structural` で結ばない）の前後で、`relations` の
  `REFERENCES` が 1,293 行消えた。どれも `_name_calls` には入らない（`_name_calls` に入ったのは `CALLS` の 288 行だけ）。
  BUG-0079（受け手が式のとき）でも同じ形で `REFERENCES` が 135 行消えている（BUG-0079 の票の確認）。
- 消えた行の例（直しの前の `relations`。受け手はどれも型の分からないローカル・引数）:

```
bench/_stdio.py:344  McpStdioClient._read_loop    -> bench/_stdio.py#Monitor.stdout_lines      mon.stdout_lines += 1
bench/_stdio.py:345  McpStdioClient._read_loop    -> bench/_stdio.py#Monitor.stdout_bytes      mon.stdout_bytes += len(line)
bench/_stdio.py:406  McpStdioClient._observe      -> bench/_stdio.py#Monitor.samples           mon.samples += 1
bench/_stdio.py:423  McpStdioClient.watch_report  -> bench/_stdio.py#Monitor.violation_count   stdout_ok = mon.violation_count == 0
```

- `RelationCandidate.name_only` は `CALLS` のときだけ立つ（`syntax/relations.py` の `_ref_candidate`）。T-06 の名前の判定
  （`tools/_name_calls.NameIndex.target_name`）は対象が `Method` のときだけ。

## 利用者（呼ぶ側の AI）として困ること

- フィールド・属性（`Monitor.stdout_lines` など）の参照を T-06 で見ると、型の分からない受け手を通した読み書きは出ず、件数も警告も
  無い（BUG-0071 の「黙って 0 件に見える」と同じ形。メソッドの呼び出しなら名前の一致が `conf=heuristic` で結ぶか、曖昧と書く）。
