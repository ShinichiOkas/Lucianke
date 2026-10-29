# BUG-0007 DiagnosticsHub が DB への書き込みより先に待ち手を起こす（設計書 §7.7.6-4 と逆の順）

- 報告: 2026-10-29 14:18 / 見た人: 編集トランザクションの担当（9c）
- 見たこと:
  - `src/lucianke/lsp/diagnostics.py` の `DiagnosticsHub.on_publish` は、待ち手（`wait_for`）を起こした後で `on_diagnostics`（DB への書き込み）を非同期に走らせる。
  - 設計書 §7.7.6-4 は「書き込んだ後に通知する」順を求めている。
  - この順だと EP-7（編集の応答の診断差分と、直後の `get_diagnostics` の結果が一致する）が競合しうる。
  - 9c は transaction 側で `sleep(0)` を 2 回挟み、IndexWriter に空のジョブ（barrier）を投げて待つ回避を入れている（`src/lucianke/edit/transaction.py`）。
- 再現手順: 未作成（直す役の仕事）。
- 調査はしていない（直す役の仕事）。
