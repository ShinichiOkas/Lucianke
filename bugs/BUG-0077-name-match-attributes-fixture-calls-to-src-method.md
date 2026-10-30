# BUG-0077 名前だけの一致（BUG-0071）が、自前の同名メソッドを持つテスト・fixture 内の呼び出しを src のメソッドに結び付ける

- 報告: 2026-10-31 / 見た人: 親（dogfood。`main` = `105af18`、Lucianke 自身のリポジトリを stdio で索引、LSP は indexing 中）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

`find_references_and_implementations {"target": {"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer.pack"}}`:

```
! 7 call site(s) marked conf=heuristic are matched by name only: the receiver's type is unknown and Packer.pack is the only method named 'pack' in the workspace (besides 3 in test files)
references: 8 files, 45 sites (tests: 43; by name: 7)
src/lucianke/mcp/fit.py:102:23  [call] in pack_and_render  ...  conf=heuristic
src/lucianke/mcp/fit.py:142:23  [call] in pack_and_render  ...  conf=heuristic
tests/fixtures/untyped_calls/app/service.py:10:23  [call] in render  return packer.pack(items)  conf=heuristic
tests/fixtures/untyped_calls/app/service.py:15:18  [call] in render_untyped  out = packer.pack(items)  ...
```

- `tests/fixtures/untyped_calls/app/engine.py:4-9` に自前の `class Packer` / `def pack(self, items)` がある（`tests/fixtures/untyped_calls/tests/test_service.py:5` にも `def pack`）。
- その fixture の中の `packer.pack(...)`（`service.py:10`・`:15` ほか、計 5 件）が、`src/lucianke/mcp/budget.py#Packer.pack` の参照として数えられている。
- `query_call_hierarchy`（`Packer._fit` incoming depth 2）でも、`render @tests/fixtures/untyped_calls/app/service.py:10`・`Holder.flush`・`from_factory`・`test_pack` が `Packer.pack` の呼び元として出る。
- `search_workspace_symbols "Packer"` の 2 件目が `tests/fixtures/untyped_calls/app/engine.py#Packer`。

## 利用者（呼ぶ側の AI）として困ること

- 名前の一致による推定が、明らかに別のクラス（呼び出しと同じ fixture の中にある同名のクラス）への呼び出しを、対象への参照として返す。取りこぼしより誤りのほうが悪い（影響範囲を誤認する）。
