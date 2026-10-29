# BUG-0023 EditTransaction は LSP サーバーを 1 つ（documents / diagnostics_hub）しか受けない

## 見たこと
- `edit/transaction.py` の `EditTransaction` は `documents` と `diagnostics_hub` を 1 組だけ受ける。Python は `types`（basedpyright）と `lint`（ruff）の 2 サーバーが動く。
- T-13 の QuickFix・organizeImports は ruff が供給する。T-13 は提案したサーバー（ruff）の `documents` / `diagnostics` を渡すので、書き込み後の `didChange` と診断待ちは ruff にだけ届く。
- basedpyright の `DocumentSync` は書き込み後の内容をその場では受け取らない（次にそのサーバーへ要求したとき、ディスクとの差で `didChange` が送られる）。そのため、ruff が供給した編集の直後の `new_diagnostics` には、basedpyright 由来の診断の変化（例: 未使用 import を消した結果の型の診断）が入らない。

## 再現
1. Python のワークスペースで ruff と basedpyright を `ready` にする。
2. T-13 で ruff の QuickFix を適用する。
3. 応答の `new_diagnostics.resolved` / `added` に basedpyright の診断が現れるかを見る（`source` が `ruff` のものだけ）。

## 見ていないこと
- 診断 DB（`diagnostics` 表）に basedpyright の行が古いまま残るかどうか（DB の配線は 16a）。
