# BUG-0099 T-13 の種別指定 `quickfix` が、文脈の診断の待ち（2 秒）を超えると「QuickFix は無い」（`E_TARGET_NOT_FOUND`）と答える

- 報告: 2026-10-31 / 見た人: BUG-0097 を直す役（T-13 の試験を負荷の下で流していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（origin/main `18da337` の `tests/contract/test_T13_code_action.py`）

1. CPU を飽和させる負荷（14 コアの PC で忙しく回るプロセス 28 個）の下で
   `uv run pytest -q -p no:cacheprovider -W always tests/contract/test_T13_two_servers.py tests/contract/test_T13_code_action.py` を 3 回
   → 2 回目だけ `1 failed, 25 passed`:
   `test_real_dry_run_writes_nothing_and_predicts_the_sha` で
   `{'code': 'E_TARGET_NOT_FOUND', 'message': "no code action of kind 'quickfix' is offered at src/shop/unused.py:1", 'details': {'reason': 'no_code_action', 'kind': 'quickfix', 'path': 'src/shop/unused.py', 'line': 1}, 'candidates': []}`
   （ファイルは `import os\nimport sys\n…`。1 行目の `import os` は未使用で、ruff は F401 を出す）。
2. 負荷の代わりに、試験の側で ruff の `publishDiagnostics` を 3 秒遅らせて `DiagnosticsHub.on_publish` に渡すと（順序は保つ）、
   同じファイルの実機の QuickFix の試験 9 本が同じ `E_TARGET_NOT_FOUND`（`no_code_action`）で落ちる。
   - `src/lucianke/tools/t13_apply_code_action.py` の `CONTEXT_DIAGNOSTICS_WAIT_MS` は 2000。

## 困ること

- 呼び手（AI エージェント）は「この行に QuickFix は無い」と読む（`E_TARGET_NOT_FOUND` は確定の答え）。実際は診断が届くのが
  遅れただけで、少し後に聞けば候補が出る。LSP を起動した直後・負荷の高いときに起こりうる。

## 原因・直し方・確認（2026-10-31）

### 原因

- 種別指定の `quickfix` は、問い合わせの前に各サーバーがいまの内容の診断を出すのを最大 `CONTEXT_DIAGNOSTICS_WAIT_MS`（2 秒）待ち、
  範囲に重なる診断を `context.diagnostics` で渡す。待ちの結果（届いたか）を捨てていたので、待ちが切れて文脈が空のまま問い合わせ、
  ruff（文脈の診断が無いと QuickFix を出さない）が 0 件を返すと、確定の答え `E_TARGET_NOT_FOUND`（`no_code_action`）にしていた。
  同じ理由で、遅れたサーバーの分が欠けた 1 件（たとえば basedpyright の `# pyright: ignore` だけ）を自動で適用しうる。
- 証拠: 試験の側で ruff の発行をハブに渡す前に止めると、直す前のコードでは票と同じ `E_TARGET_NOT_FOUND`。下の新しい試験 4 本が
  直す前のコードで赤になることを確かめた。

### 直し方

- `_context_diagnostics` は「いまの内容の診断が届いたか」も返す。`_choose_by_kind` は、届かなかったサーバーがあり候補が 1 件以下なら
  `E_TARGET_NOT_FOUND` でも 1 件の自動適用でもなく `E_TIMEOUT`（`details.reason: "diagnostics_pending"`・`server`・`servers`・`kind`・
  `path`・`line`・`retry_after_ms` = 1000。1 件あれば ID を付けて `candidates[]` に入れる。何も適用しない）。2 件以上は
  `E_TARGET_AMBIGUOUS` のまま `details.diagnostics_pending` に遅れたサーバーを書く。`E_TARGET_NOT_FOUND` は、問い合わせたすべての
  サーバーがいまの内容の診断を出したうえで 0 件のときだけ。製品の期限（2 秒）は変えていない。
- 仕様書 §5.8（`E_TIMEOUT` の `diagnostics_pending`）・T-13 処理規約 3・エラー、設計書 §7.7.7 の T-13 の行に理由（その時点の 0 件・
  1 件は確かめられていない。確定の形で答えると、呼び手の AI は直せる箇所を諦めるか、抑止コメントだけを適用される）とともに書いた。

### 確認

- 偽 LSP（`tests/contract/test_T13_code_action.py`。偽サーバーに「診断を発行しない」を足した）: 発行しないまま候補 0 件 →
  `E_TIMEOUT diagnostics_pending`（`server: "fake"`・`retry_after_ms`）、候補 1 件 → 同じ `E_TIMEOUT` で ID つきの候補 1 件・ファイルは
  不変・その ID で呼べば適用できる、候補 2 件 → `E_TARGET_AMBIGUOUS` に `details.diagnostics_pending: ["fake"]`。発行したうえで 0 件 →
  `E_TARGET_NOT_FOUND`。
- 実機の ruff: `HeldPublishes` で ruff の発行を止めて `quickfix` → `E_TIMEOUT diagnostics_pending`（`server: "ruff"`、compact も文法どおり）、
  放した後は同じ呼び出しで `E_TARGET_AMBIGUOUS`（候補が出る）。既存の `test_real_no_candidate_is_not_found_and_changes_nothing` は、
  ruff がいまの内容の診断を出した後で聞くようにした（出す前の 0 件は `E_TIMEOUT` が正しい答えになったため。検査は緩めていない）。
- `uv run ruff check` → `All checks passed!`。BUG-0098 と同じ一式 → `558 passed, 6 skipped`。CPU 飽和の負荷の下で T-13 の 2 ファイルを
  3 回 → 3 回とも `33 passed`（170 秒 / 143 秒 / 149 秒。参照元の実機の試験を足す前の 33 本。やり直しの警告 0 件）。全体テストは流していない。
