# BUG-0046 編集の構文妥当性ベンチの stdio 標本で、F-TS の操作 6/20 が E_UNSUPPORTED になる（プロセス内では成功）

- 報告: 2026-10-29 15:21 / 見た人: 編集の構文妥当性ベンチの役（22c）
- 見たこと:
  - `bench/run_edit_safety.py` の本計測（1,000 件、シード 20261029）で、同じ操作を実サーバー（stdio）にも投げて比べる標本 20 件のうち 6 件が一致しなかった。すべて F-TS（ky）の操作（恒等 replace_body 3、delete_symbol 2、insert_after 1）で、stdio 側が `E_UNSUPPORTED` を返した。プロセス内では成功した。F-PY の標本は一致した。
  - 当時はエラーのメッセージと details を記録していなかった（de03d22 で記録するよう台本を直した）。
  - 同じ ky の複製で `delete_symbol` を単発で stdio に投げると成功した。
  - 結果: `bench/results/20261029-151938-windows-edit-safety.json` の `stdio_sample.mismatches`。
- 再現手順: `uv run python bench/run_edit_safety.py --scale 0.3 --no-lsp`（約 2 分）で `stdio_sample.mismatches[].stdio.message/details` を見る。
- 調査はしていない（直す役の仕事）。
