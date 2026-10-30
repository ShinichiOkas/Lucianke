# BUG-0071 型の分からない受け手のメソッド呼び出しが、参照・呼び出し階層から黙って落ちる（名前が一意のメソッドでも）

- 報告: 2026-10-31 / 見た人: 親（dogfood。Lucianke 自身のリポジトリ、LSP は indexing 中で structural の応答）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `cf4497c`）

- `find_references_and_implementations {"target": {"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer.pack"}}`:
  `references: 5 files, 38 sites (tests: 38)`。38 件すべてテスト。
- 本番の呼び出し `src/lucianke/mcp/fit.py:102` `page = packer.pack(out, ...)` と `src/lucianke/mcp/fit.py:142` `page = packer.pack(` が載っていない。
  `fit.py` の `pack_and_render` は引数 `packer: Any` で受けている。
- `pack` という名前のメソッドの定義は、ワークスペース（`src/`）に `Packer.pack`（`budget.py:218`）の 1 つだけ（`grep -rn "def pack" src/lucianke`）。
- `query_call_hierarchy {"target": {... "Packer._fit"}, "direction": "incoming", "depth": 2}`: `<- Packer.pack` の下は 13 件すべてテスト関数。`pack_and_render` が出ない。
- 応答の警告は「動的ディスパッチ（実装の取り違え・見落とし）と同名メソッドの区別が落ちます」とだけ書き、何件落としたか・どこかは言わない。

## 利用者（呼ぶ側の AI）として困ること

- 本番の呼び出し元が 0 件に見え、変更の影響を見落とす（テストだけが使っていると誤認する）。
