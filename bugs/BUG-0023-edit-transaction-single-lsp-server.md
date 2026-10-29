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

## 原因（16c）
- `EditTransaction` の `documents` / `diagnostics_hub` は 1 サーバー分の口（`DocumentSync` / `DiagnosticsHub`）で、`sync` も `wait_for` も 1 つのサーバーにしか届かない。Python は `types`（basedpyright）と `lint`（ruff）が別プロセスで、それぞれ版の数え方も違う。呼び手（T-13 `_transaction`・`_runtime.edit_transaction(lsp=handle)`・T-11 の `overrides`）が 1 つのハンドルの口だけを渡していたため、もう一方のサーバーは書き込み後の `didChange` を受けず、診断の待ちにも入らなかった。

## 直し方
- 部品（コミット 8a6f40f）: `lsp/documents.py` の `DocumentRouter`。`sync(path, text, language_id)` が言語の `ready` なサーバーすべてへ送り（`didOpen.languageId` はサーバー定義の `document_language_id`）、サーバーごとの `(版, 送信時刻)` を記録する。`diagnostics_hub.wait_for` は、その文書を開いているサーバーすべてを同時に（`timeout_ms` を共有して）待ち、全部揃ったときだけ `True`。版つきの待ちは記録したサーバーごとの版を使う（代表の版を他のサーバーに使わない）。依存元（`min_version=None`）は `lint` を待たない（ruff は再発行しない）。`edit/transaction.py` は触らない（1 組の口のまま）。
- 取り出し口: `LspPool.router(ws)`（同じ `workspace_id` なら同じものを返す）。`router` が `documents`、`router.diagnostics_hub` が `diagnostics_hub`。
- **結線（呼び手の側。16b / T-13 / T-11 の持ち場）**: 既定（`lsp=None`）の `RuntimeHub.edit_transaction(ws)` は 16b が `router` にした（b24f766）。残りは次の 2 か所が 1 サーバーの口を渡している。
  1. `tools/_runtime.py` `edit_transaction` の `if lsp is not None:` 枝（`documents=lsp.documents, diagnostics_hub=lsp.diagnostics`）: `lsp` は言語を示す手がかりにとどめ、`self._router_wiring(ws)` があればそれを使う（`lsp_status` は READY のまま）。T-13 は `factory(ws, lsp=handle)` を呼ぶので、これだけで T-13 が直る。
  2. `tools/t11_rename_symbol.py` `_transaction` の `overrides`（`documents=handle.documents, diagnostics_hub=handle.diagnostics`）: ファクトリがある（実運用）ときは、`documents` / `diagnostics_hub` を渡さずに `lsp_status` だけ渡す。

## 確認
- 単体: `tests/unit/test_lsp_documents_router.py`（17 件）。両サーバーへの `didOpen` / `didChange`・サーバーごとの版（types 版 4・lint 版 2 のとき両方の受信を待つ）・一方が沈黙すれば `False`・同時に待つ（直列だとデッドロックする仕掛け）・依存元は lint を待たない・止まったサーバーは待たない・開き直しで版が戻っても待てる・偽 LSP 2 本の結線（`LspPool.router`）。
- 結線の確認（残り）: 上の 2 か所を直したあと、Python のワークスペースで ruff と basedpyright を `ready` にし、T-13 の ruff の QuickFix 適用の応答で、basedpyright の `source` の診断が `new_diagnostics` の `resolved` / `added` に現れることを見る（再現手順 3）。

## 結線と確認(16b、`7b4fa68`・本コミット)
- 1(`tools/_runtime.py` の `edit_transaction` の `lsp=` 枝): `lsp` は「LSP で検証する」手がかりにとどめ、口は `LspPool.router(ws)` の `DocumentRouter`(`documents=router`、`diagnostics_hub=router.diagnostics_hub`)、`lsp_status` は READY。ルーターを出せないプール(偽物)のときだけ、渡されたハンドルの 1 サーバー。`on_diagnostics` は `(path, diags, version, source, ws)` の 5 引数で済み(`b24f766`)。
- 実機の確認: `tests/contract/test_T13_two_servers.py`。結線済みの `RuntimeHub` / `WiredServices` / 実物の `LspPool` で、Python のワークスペースの basedpyright と ruff を両方 `ready` にし、T-13 の ruff の QuickFix(未使用 import `missing_mod_x` の削除)を適用した。応答の `new_diagnostics`(`status: complete`)の `resolved` に、ruff の `F401` に加えて basedpyright の `reportMissingImports` と `reportUnusedImport` が入り、`added` は空。3 回続けて green。
- この確認で別の不具合を 1 つ見つけて直した(同じ結線の持ち場): `RuntimeHub.on_diagnostics` が診断自身の `source` を使っていて、ruff の `"Ruff"` が行の置換の範囲(サーバー名 `ruff`。`replace_lsp_diagnostics` の `source`)と食い違い、ruff の診断が PaaD の `diagnostics` 表に 1 件も入っていなかった(T-13 の `resolved` に ruff の診断が出なかった)。サーバー名で揃えた。単体: `tests/unit/test_tools_runtime.py`。

## 状態
- T-13 の経路は結線・確認済みで、この票の症状(ruff が供給した編集の `new_diagnostics` に basedpyright の診断が入らない)は解消した。
- 残り 1 か所: `tools/t11_rename_symbol.py` の `_transaction` の `overrides`(`documents=handle.documents, diagnostics_hub=handle.diagnostics`)。19g が直す(ファクトリがあるときは `documents` / `diagnostics_hub` を渡さず `lsp_status` だけ渡す)。
