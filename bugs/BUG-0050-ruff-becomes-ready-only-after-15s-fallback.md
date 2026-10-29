# BUG-0050 ruff が ready になるのは起動から約 15 秒後（最初の get_diagnostics は、型の LSP が落ちていると 15 秒間 Lint の診断を返せない）

- 報告: 2026-10-29 / 見た人: BUG-0039 を直す役（実 stdio クライアント、使い捨てのワークスペース。`LUCIANKE_LSP_BASEDPYRIGHT_CMD`・`LUCIANKE_LSP_PYRIGHT_CMD` を存在しないパスにして型の LSP を `init_failed` にした）
- 症状: 最初の `get_diagnostics`（ruff の起動の契機）から 6 秒間隔で 5 回呼ぶと、`lsp_sources[]` の ruff（`lint` 役）は 1〜3 回目（約 6・12・18 秒）が `indexing`、4 回目（約 24 秒）が `ready`。`indexing` の間は ruff の診断が 0 件、`ready` になった呼び出しで I001・F401×2 が返る。
  - ruff は `$/progress` を出さない。`ready` の合図は「最初の `publishDiagnostics`」か `ready_fallback_s`（`LspPool` の既定 15 秒）で、T-08 は `ready` でないハンドルに文書を開かせない（診断が届くには文書を開く必要がある）ため、見たかぎり 15 秒のフォールバックで `ready` になっている（見ただけ。調査はしていない）。
  - 期待: ruff（起動 12 ms と設計書にある）が、起動の直後〜数秒で `ready` になる。
- 再現手順: `import os, sys` の 1 ファイルだけのワークスペースを cwd にして `python -m lucianke` を stdio で起動し、`get_diagnostics {paths: ["src/lint_plain.py"], min_severity: "hint", format: "json"}` を 6 秒ごとに呼んで `data.lsp_sources[]` の `lint` 役の `status` を見る。
- 調査はしていない（直す役の仕事）。

## 原因
- `ready` の合図は一律に「`$/progress` の `end`」「最初の `publishDiagnostics`」「`ready_fallback_s`（既定 15 秒）」のいちばん早いものだった（`lsp/pool.py`）。ruff は `$/progress` を出さず、診断も文書を開くまで出さない。文書を開かせるのは T-08 など `ready` なハンドルだけを使う呼び手で、`ready` でないハンドルには開かせない（`tools/t08_get_diagnostics.py` の `_ready_handles`）。最初の `publishDiagnostics` は `ready` になった後でないと来ないので、合図として働かず、毎回 15 秒のフォールバックまで `indexing` のままだった（票の 4 回目の呼び出し ≒ 24 秒で `ready`）。
- 型の LSP が `ready` でも ruff は同じく 15 秒のフォールバックまで `ready` にならない（実の LSP のテストが `ready_fallback_s=5.0` にして ruff の `ready` を最長 60 秒待っているのはこのため）。型の診断が先に返るので目立たず、型の LSP が落ちているワークスペースでだけ、最初の約 15 秒に Lint の診断が丸ごと欠けて見えた（BUG-0039 の直しで Lint の診断だけは返すようにしたが、`ready` になるまでが遅かった）。

## 直し方
- `LspServerDef` に `ready_signal: Literal["progress", "initialized"]`（既定 `"progress"`）を足した（`lsp/servers.py`）。`"progress"` は従来どおり（`$/progress` の `end`・最初の `publishDiagnostics`・`ready_fallback_s`）、`"initialized"` は `initialize` の応答（`initialized` 通知を送った直後）を `ready` の合図にする。`SERVERS` で ruff だけ `"initialized"`、basedpyright・pyright・tsls・vtsls・best effort は `"progress"` のまま。
- `LspPool._run_once`（`lsp/pool.py`）は、`initialize` の成功後 `indexing` に入れた直後に、`ready_signal == "initialized"` なら `_become_ready()` を呼び、そうでなければ従来どおりフォールバックの時計を掛ける。状態の履歴は `standby → starting → indexing → ready` のまま（`indexing` を通って直ちに `ready`）。再起動した後も同じ経路。
- テスト: `tests/unit/test_lsp_pool.py`（`$/progress` を出さず診断も文書を開くまで出さない偽 LSP を `ready_signal="initialized"` にすると、`ready_fallback_s` を 60 秒のままで直ちに `ready`・文書は何も開いていない・フォールバックの時計は無い／`reset` の後の再起動でも `ready`／既定の `progress` は `--silent` の偽 LSP を `initialize` だけでは `ready` にしない／表で ruff だけが `initialized`）、`tests/contract/test_T08_diagnostics.py`（実の ruff。`LUCIANKE_LSP_BASEDPYRIGHT_CMD`・`LUCIANKE_LSP_PYRIGHT_CMD` を存在しないパスにして型の LSP を `init_failed` にし、`ready_fallback_s` は既定の 15 秒のまま、最初の `get_diagnostics` から 1 秒以内に ruff の F401 が返り、`lsp_sources` が types=`unavailable`・lint=`ready`。直す前の HEAD では 1 秒たっても 0 件で落ちる）。

## 確認
- 票の再現手順どおり実の stdio クライアントで（型の LSP を `init_failed` にして）`get_diagnostics` を呼ぶと、1 回目 0.1 秒（索引の準備中）、2 回目 0.7 秒で lint=`indexing`（起動中）、3 回目 1.2 秒で lint=`ready` になり I001・F401×2 が返る（直す前は 24 秒）。契約テストでは最初の呼び出しから ruff の診断までが 0.10〜0.21 秒（5 回）。
- `tests/unit/test_lsp_*.py`・`tests/contract/test_T08_diagnostics.py`・`tests/unit/test_t00_set_python_env_restart.py` を 3 回続けて green（257 件、1 skipped）。`ruff check` は通る。
- 設計書 §7.5.2 の状態表（`indexing` の行の「`ready` は … 早い方」）と `LspServerDef` の説明に、`ready_signal`（ruff は `initialize` の応答で `ready`）を足す必要がある（文書同期の役へ申し送り）。
