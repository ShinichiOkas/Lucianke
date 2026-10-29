# BUG-0008 limit を超えても next_cursor が出ない（パッカーが total_estimate を見ていない）

- 報告: 2026-10-29 14:20 / 見た人: T-03・T-04 の担当（14a）
- 見たこと:
  - `search_workspace_symbols`（T-03）で一致が `limit` を超えるとき、応答に `next_cursor` が出ない。`returned ≤ limit` と `total_estimate` は満たしている。
  - 担当の観察: パッカー（`src/lucianke/mcp/budget.py`）は `ToolOutput.total_estimate` を見ず、`len(records)` から残り（remaining）を決めている。T-03 は `limit` 件までしかレコードを渡さないので、残りが 0 に見える。
- 期待: 仕様書 §5.4（ページング）— 残りがあれば `next_cursor` を返す。
- 再現手順: `tests/contract/test_T03_T04.py` の F-POLY で、`limit` を一致件数より小さくして T-03 を呼ぶ。
- 調査はしていない（直す役の仕事）。
