# BUG-0004 応答ヘッダが ws=- のまま、バッチの失敗件数が warnings[] に出ない

- 報告: 2026-10-29 14:05 / 見た人: 縦切りの結線の担当、親（実機の stdio クライアントで確認）
- 症状 1: `get_file_skeleton` / `get_symbol_bodies` を `workspace_id` 省略で呼ぶと、compact のヘッダが `ws=-` になる（例: `# get_symbol_bodies ok conf=structural lsp=n/a rev=WORKING_TREE ws=- ~130/800tok`）。
  - 期待: 仕様書 §5.4.2・§5.2 — 解決後のワークスペース ID（このリポジトリなら `Lucianke` 由来の ID）が出る。
  - 担当の観察: `CallInfo.workspace_id` は引数の値のまま（引数なしなら null）。
- 症状 2: バッチ入力の一部が失敗したとき、失敗項目の件数が `warnings[]` に出ない（`! …` 行と `items[].ok` だけ）。
  - 期待: 仕様書 §5.4.4「1 件以上成功していれば応答全体は ok: true とし、失敗項目の件数を warnings[] に書く」。
  - 担当の観察: `ToolOutput` に warnings の欄が無い。
- 再現手順: `uv run --directory S:/work/develop/Lucianke lucianke` を MCP クライアントで起動し、`get_file_skeleton` に `{"paths": ["src/lucianke/core/urn.py", "no/such/file.py"], "format": "json"}`。
- 調査はしていない（直す役の仕事）。
