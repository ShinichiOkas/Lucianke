# BUG-0097 負荷の高いときに T-13 の 2 サーバーの試験で、basedpyright の診断が `resolved` に入らなかった（1 回。揺れるテスト）

- 報告: 2026-10-31 / 見た人: BUG-0087・BUG-0088 を直す役（編集の契約テストを流していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = origin/main `525bd6e` + BUG-0087・BUG-0088 の作業中の変更）

同じ PC で `tests/contract_fixtures/test_fx_edit_T10_T13.py tests/unit/test_tools_runtime.py`（実 LSP を使う）を流している間に:

- `uv run pytest -q -p no:cacheprovider tests/contract/test_T10_*.py tests/contract/test_T11_*.py tests/contract/test_T13_code_action.py tests/contract/test_T13_two_servers.py tests/contract/test_unsupported_tools.py tests/unit/test_edit_writer.py tests/unit/test_core_limits.py`
  → `1 failed, 321 passed, 1 skipped in 422.66s`
  - `tests/contract/test_T13_two_servers.py::test_a_ruff_quickfix_reports_the_basedpyright_diagnostics_it_resolved` の `:122`
    `assert ("basedpyright", "reportMissingImports") in resolved, nd`
  - `nd` = `{'status': 'complete', 'added': [], 'resolved': [{'path': 'pkg/unused.py', 'severity': 'warning', 'source': 'ruff', 'code': 'F401', ...}], 'checked_files': [{'path': 'pkg/unused.py', 'fresh': True}]}`
    （`resolved` は ruff の F401 だけ。basedpyright の 2 件が無い。`status` は `complete`）
  - 捕まえたログに `reindex failed` などの警告は無い。
- 直後に同じファイルだけを 3 回 → 3 回とも `1 passed`（10〜13 秒）。
- 全文は `tests/conftest.py` の記録（BUG-0088 の見張り）で `%TEMP%\lucianke-test-errors\20261031-075330-4820.log` に残った。

## 困ること

- 全体テストを並行で流すと、負荷しだいで赤になりうる（揺れるテストは無価値）。

## 原因・直し方・確認（2026-10-31）

### 原因（試験の前提の待ちが無かった。製品の期限ではない）

- 編集前の診断の写し（設計書 §7.7.6-1）は `diagnostics` 表から取る。試験は `wait_ready`（`ready_fallback_s=1.0` で、ファイルを
  開く前に ready になる）の直後にツールを呼び、basedpyright がそのファイルの診断を出すのを、ツールの中の QuickFix の文脈の診断の待ち
  （`CONTEXT_DIAGNOSTICS_WAIT_MS` = 2 秒）に頼っていた。負荷で basedpyright の最初の解析が 2 秒を超えると、編集の時点の表に
  basedpyright の行が無く、編集後の版の診断（空）と比べても `resolved` に入らない（`status` は `complete`。ruff は速いので F401 だけ入る）。
  basedpyright が出す QuickFix（`# pyright: ignore`）は自分の解析から作るので、候補の一覧には文脈の診断が無くても basedpyright が出る
  （試験の候補の検査は通る）。
- 証拠: 試験の側で basedpyright の `publishDiagnostics` だけを遅らせて `DiagnosticsHub.on_publish` に渡すと（順序は保つ）、
  1 秒では緑、3 秒では票と同じ赤（`resolved` は ruff の F401 だけ、`status: complete`、`added: []`）。basedpyright の受信は
  `(版 1, 2 件)` → `(版 2, 0 件)` で、版 1 の 2 件は編集の後にハブに届いていた。
- 同じ形が `tests/contract/test_T13_code_action.py` の実機の QuickFix の試験にもあった（ruff だけ）。CPU 飽和の負荷の下の 3 回のうち
  1 回、`test_real_dry_run_writes_nothing_and_predicts_the_sha` が `E_TARGET_NOT_FOUND`（`no_code_action`）で落ちた。ruff の診断を
  3 秒遅らせると実機の QuickFix の試験 9 本が同じ赤（ruff は文脈の診断が空だと QuickFix を出さない）。

### 製品の側（票にした。直していない）

- 試験の赤は前提の待ちの欠けだが、同じ状況で製品の答えが誤解を招くことを 2 つ見た（見たことだけの票）:
  - **BUG-0098**: 編集の前に LSP がそのファイルの診断をまだ出していないと、前からある診断を `added`（`status: complete`・`exact`）と
    答える（`import missing_mod_x` を残して `import os` を消す編集で、basedpyright の 2 件が `added` に入った）。
  - **BUG-0099**: 文脈の診断の待ち（2 秒）を超えると、種別指定の `quickfix` が「QuickFix は無い」（`E_TARGET_NOT_FOUND`）と確定の形で答える。
- 製品の期限（`CONTEXT_DIAGNOSTICS_WAIT_MS`・`DIAGNOSTICS_WAIT_MAX_MS`・`LSP_TIMEOUT_*`）は変えていない。

### 直し方（試験）

- `tests/contract/_lsp_settle.py` に `diagnostics_published(handle, rel, codes)` を足した: いまのディスクの内容を LSP に見せ、
  製品と同じ合図（`DiagnosticsHub.wait_for`: 版 ≥ いまの版、かつ `on_diagnostics` の表への書き込み済み）と受信した中身（期待の
  `code` がすべてある）で待つ。中身が足りなければ次の受信を待つ。上限は `SETTLE_TIMEOUT_S`（240 秒。条件待ちなので揃えば即戻る）。
- `test_T13_two_servers.py`: 編集の前に、両方のサーバーが編集前の診断（basedpyright の `reportMissingImports`・`reportUnusedImport`、
  ruff の `F401`）を出し終えて表に入るまで待つ。製品が降格した答え（`new_diagnostics` が `pending`・`E_TIMEOUT`）は、新しい作業ツリーと
  サーバーで最初からやり直して聞き直す（最大 2 回。やり直したら `UserWarning` に各回の理由を書く。全回降格なら fail）。編集を
  巻き戻すより、組み立てごと新しくするほうが前提が崩れない。fixture `rig` は `open_rig`（非同期の文脈管理）にした。検査は 1 つも緩めていない。
- `test_T13_code_action.py`: `ambiguous_quickfix` が、ruff の ready といまの内容の `F401` を待ってから聞く。実機の組み立てだけ
  `diagnostics_wait_ms` を上限の 10 秒に（呼び手の引数。条件待ちなので揃えば即戻る。偽 LSP の試験は 4 秒のまま）。

### 確認

- 遅らせて確かめた（直す前 → 直した後）: basedpyright +3 秒 → 赤 / 緑、+6 秒 → 緑。ruff +3 秒で `test_T13_code_action.py -k real`
  → 9 本赤 / `13 passed`。basedpyright +12 秒（製品の上限 10 秒を毎回超える）では 3 回とも `pending` で、
  「the product degraded on every try: …」で fail（検査を緩めていない）。最初の組み立てだけ +12 秒にすると、やり直しの 2 回目で緑に
  なり `UserWarning: T-13 two servers: answered only after a re-ask; #1 applying: new_diagnostics=pending …` が出る。
- **CPU を飽和させる負荷**（14 コアの PC で、忙しく回るプロセス 28 個）の下で
  `tests/contract/test_T13_two_servers.py tests/contract/test_T13_code_action.py` を 3 回 → `26 passed`（79 秒 / 73 秒 / 81 秒）。やり直しの警告は 0 件。T-13 の 2 サーバーの試験だけを直した段階では、
  同じ負荷の 3 回のうち 1 回 `test_T13_code_action.py` が落ちた（上の原因の 3 つ目。それで両方を直した）。
- 負荷なし: 同じ 2 ファイルを 3 回 → `26 passed`（12 秒 / 13 秒 / 15 秒）。
- `uv run ruff check` → `All checks passed!`。全体テストは流していない。
