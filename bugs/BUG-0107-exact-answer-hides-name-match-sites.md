# BUG-0107 exact の答え（LSP ready）で、名前の一致の本番の呼び出しが件数だけになり位置が見えない（構文木の答えより情報が減る）

- 報告: 2026-10-31 / 見た人: 親（dogfood。`main` = `13a728f`、Lucianke 自身を stdio で索引、basedpyright が ready になるまで待って同じ問い合わせ）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

`find_references_and_implementations {"target": {"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer.pack"}, "include_tests": false}`

- LSP indexing 中（構文木の答え）:
  ```
  # find_references_and_implementations ok conf=heuristic lsp=indexing ...
  references: 1 files, 2 sites (tests: 0; by name: 2)
  src/lucianke/mcp/fit.py:102:23  [call] in pack_and_render  page = packer.pack(...)  conf=heuristic
  src/lucianke/mcp/fit.py:142:23  ...  conf=heuristic
  ```
- basedpyright ready（exact の答え、2.74 秒）:
  ```
  # find_references_and_implementations ok conf=exact lsp=ready ...
  ! 2 call site(s) of .pack() whose receiver type is unknown to the syntax tree are not in this exact answer (the LSP could not type the receiver, e.g. Any); Packer.pack is the only method named 'pack' in the workspace (besides 7 in ...
  references: 0 files, 0 sites (tests: 0)
  ```
  位置（`fit.py:102`・`:142`）は compact の本文に出ない（警告の件数だけ）。

## 利用者（呼ぶ側の AI）として困ること

- より正確なはずの exact の答えで、本番の呼び出し元が「0 sites」に見える。構文木の答えでは位置まで見えていたのに、LSP が温まると見えなくなる。
- exact の一覧に推定を混ぜない方針（BUG-0071）は正しいが、別の欄として位置を出さないと、影響範囲を見落とす。
