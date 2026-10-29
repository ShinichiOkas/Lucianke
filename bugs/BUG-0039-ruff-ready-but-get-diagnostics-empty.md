# BUG-0039 ruff が ready でも get_diagnostics が 0 件（同じ行に T-13 は ruff の quickfix を返す）

- 報告: 2026-10-29 / 見た人: dogfood-2（実 stdio クライアント、使い捨て作業ツリー b24f766、F-POLY のコピー）
- 症状: ruff が `ready` なのに `get_diagnostics` が 0 件（`diagnostics: 0 in 0 files`）を返す。同じ行・同じファイルに対して `apply_code_action`（T-13）は ruff の quickfix を 4〜6 件返す（`E401 Split imports`・`F401 Remove unused import` など）ので、ruff は診断を持っている。
  - 対象: `src/shop/models/lint_plain.py`（`import os, sys\n` の 1 行だけ。BOM も CRLF も無い対照）と `src/shop/models/bom_lint.py`（BOM + CRLF の同じ 1 行）。どちらも 0 件。`min_severity: hint`・`sources` 省略。
  - 応答: `lsp=unavailable`、`lsp_sources=[{pyright: unavailable}, {ruff: ready}]`、`! LSP が利用できないため、構文エラー（Tree-sitter）だけを返しています。…Lint の診断は含まれていません。`。pyright 側は BUG-0037（init_failed）。
  - 期待: ruff が ready なら、ruff の診断（E401・F401）は返る。型 LSP が落ちていることで lint 側の診断まで捨てているように見える（見ただけ）。
  - 影響: 観点 7（BOM 付き CRLF の行 0 を utf-8 を採る ruff に開かせたときの T-08 の診断の列）を、この環境では確かめられない。
- 再現手順: `configure_workspace set_roots`（F-POLY のコピー。`lint_plain.py` を足す）→ 10 秒待つ → `get_diagnostics {paths: ["src/shop/models/lint_plain.py"], min_severity: "hint"}`。台本: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> ro`（`obs2_t08.json` の `n`・`lsp_sources`）。
- 調査はしていない（直す役の仕事）。
