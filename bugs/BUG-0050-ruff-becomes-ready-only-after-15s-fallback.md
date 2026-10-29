# BUG-0050 ruff が ready になるのは起動から約 15 秒後（最初の get_diagnostics は、型の LSP が落ちていると 15 秒間 Lint の診断を返せない）

- 報告: 2026-10-29 / 見た人: BUG-0039 を直す役（実 stdio クライアント、使い捨てのワークスペース。`LUCIANKE_LSP_BASEDPYRIGHT_CMD`・`LUCIANKE_LSP_PYRIGHT_CMD` を存在しないパスにして型の LSP を `init_failed` にした）
- 症状: 最初の `get_diagnostics`（ruff の起動の契機）から 6 秒間隔で 5 回呼ぶと、`lsp_sources[]` の ruff（`lint` 役）は 1〜3 回目（約 6・12・18 秒）が `indexing`、4 回目（約 24 秒）が `ready`。`indexing` の間は ruff の診断が 0 件、`ready` になった呼び出しで I001・F401×2 が返る。
  - ruff は `$/progress` を出さない。`ready` の合図は「最初の `publishDiagnostics`」か `ready_fallback_s`（`LspPool` の既定 15 秒）で、T-08 は `ready` でないハンドルに文書を開かせない（診断が届くには文書を開く必要がある）ため、見たかぎり 15 秒のフォールバックで `ready` になっている（見ただけ。調査はしていない）。
  - 期待: ruff（起動 12 ms と設計書にある）が、起動の直後〜数秒で `ready` になる。
- 再現手順: `import os, sys` の 1 ファイルだけのワークスペースを cwd にして `python -m lucianke` を stdio で起動し、`get_diagnostics {paths: ["src/lint_plain.py"], min_severity: "hint", format: "json"}` を 6 秒ごとに呼んで `data.lsp_sources[]` の `lint` 役の `status` を見る。
- 調査はしていない（直す役の仕事）。
