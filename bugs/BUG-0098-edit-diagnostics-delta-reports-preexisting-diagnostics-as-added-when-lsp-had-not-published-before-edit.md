# BUG-0098 編集の前に LSP がそのファイルの診断をまだ出していないと、編集の診断差分が前からある診断を `added`（`status: complete`・`exact`）と答える

- 報告: 2026-10-31 / 見た人: BUG-0097 を直す役（T-13 の 2 サーバーの試験の原因を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（origin/main `18da337`）

`tests/contract/test_T13_two_servers.py` と同じ組み立て（実物の `LspPool` に basedpyright と ruff、`ready_fallback_s=1.0`）で、
basedpyright の `publishDiagnostics` だけを試験の側で 3 秒遅らせて `DiagnosticsHub.on_publish` に渡した（負荷で basedpyright の
最初の解析が遅れたときの代わり。順序は保つ）。ファイル `pkg/unused.py` の中身:

```python
import missing_mod_x
import os


def main() -> int:
    return 1
```

`apply_code_action(path="pkg/unused.py", line=2, action_kind_or_id=<ruff の "Remove unused import: `os`" の ID>, diagnostics_wait_ms=10000)`
（ruff が 2 行目の `import os` を消す。1 行目の `import missing_mod_x` は残る）の `new_diagnostics`:

- 遅らせないとき:
  `{'status': 'complete', 'added': [], 'resolved': [ruff F401 (os), basedpyright reportUnusedImport (os)], 'checked_files': [{'path': 'pkg/unused.py', 'fresh': True}]}`
- 遅らせたとき（3 秒）:
  `{'status': 'complete', 'added': [basedpyright reportMissingImports (missing_mod_x, 1 行目), basedpyright reportUnusedImport (missing_mod_x, 1 行目)], 'resolved': [ruff F401 (os)], 'checked_files': [{'path': 'pkg/unused.py', 'fresh': True}]}`
  - `added` の 2 件の `confidence_level` は `exact`。2 件とも編集の前からある（編集は 1 行目に触れていない）。

同じ遅らせ方で `import missing_mod_x\nimport sys …`（1 行目を消す QuickFix。T-13 の 2 サーバーの試験のまま）のときは、`resolved` が
ruff の F401 だけになり basedpyright の 2 件が入らない（BUG-0097 の赤と同じ形。`status` は `complete`）。そのときの basedpyright の
受信（サーバーが送った時刻, 版, 件数）: `(168232.98, 1, 2)`, `(168235.02, 2, 0)`（ハブに届くのはそれぞれ 3 秒後。1 秒遅らせたときは
赤にならない。ツールの中の QuickFix の文脈の診断の待ち `CONTEXT_DIAGNOSTICS_WAIT_MS` は 2 秒）。

## 困ること

- 呼び手（AI エージェント）は「この編集で basedpyright のエラーが 2 件増えた」と読む（`status: complete`・`exact`）。実際は前からある。
  負荷の高いとき・LSP を起動した直後の最初の編集で起こりうる。

## 原因・直し方・確認（2026-10-31）

### 原因

- 編集前の診断の写し（設計書 §7.7.6-1）は `diagnostics` 表をそのまま読んでいた。LSP がそのファイルのいまの内容の診断をまだ
  出していない（負荷・起動直後・まだ開いていない参照元）と、表にそのサーバーの行が無い（か古い）。編集後は版の待ちで最新の診断が
  揃うので、前からある診断が `after − before` に入り `added`（`status: complete`、診断の `confidence_level: exact`）になっていた。
  写しを取れなかったとき（例外）も、空の写しと比べて同じことになっていた。
- 証拠: 試験の側で basedpyright の発行をハブに渡す前に止める／遅らせると、直す前のトランザクションでは票と同じ答え
  （`status: complete`、`added` に basedpyright の reportMissingImports・reportUnusedImport〔missing_mod_x〕）。下の新しい試験のうち
  実機 2 本と単体 4 本が、直す前のコードで赤になることを確かめた。

### 直し方

- **編集の前に基準を待つ**（`EditTransaction._snapshot` / `_baseline`）: 書き込みをする呼び出しでは、表を読む前に各ファイル（対象と
  直接の参照元）のいまの内容を LSP に見せ（同じ内容なら何も送らない。参照元もここで開く）、サーバーごとに「その版の診断を受信し表に
  書き終えた」まで並べて待つ。上限は `diagnostics_wait_ms`（書き込み後の待ちとは別枠。出ていれば即戻る。ロックの外）。`dry_run`・
  インターロックの強制 dry_run・`diagnostics_wait_ms == 0` では待たない（LSP にも見せない）。
- **サーバーごとの結果**: `RoutedDiagnostics.wait_sources`（`DocumentRouter._wait_each`）が `wait_for` と同じ待ちを
  `{サーバー名: 揃ったか}` で返す（サーバー名は `diagnostics.source` と同じ `server_name`）。口が持たなければ `{"*": 揃ったか}`。
- **基準が不明なサーバー**（`transaction._unknown_baseline`）: 編集の前後どちらかで待ったサーバーのうち、編集前に揃わなかったもの
  （写しが取れなければ全部 `"*"`）。前後どちらでも待たなかった source（`tree-sitter`・`lucianke-*`・止まったサーバーの古い行）は
  前後で同じなのでふつうに比べる。
- **差分**（`verify.compute_delta(unknown_baseline=)`）: そのサーバーの行は比べず、編集後の行を `present_after` に入れ、`resolved` も
  判定しない。`checked_files[]` に `unknown_baseline: [サーバー名]`、`status` は `partial`（`pending` が優先）、警告にサーバー名と次に
  すること（`present_after` の位置と diff の変更箇所を見比べる・`get_diagnostics`）。基準が分からないまま `complete` は返さない。
- **出力**（T-10・T-11・T-13 共通。`tools/_edit_output.diagnostics_kv` / `diagnostics_json`）: json の `new_diagnostics.present_after`、
  compact の `new_diagnostics: partial (+0 -1 ?2) checked=1 files`（`?N` は `present_after` の件数。0 なら書かない）と `present_after:` の行。
- 仕様書 §10.7（比較の基準・基準が不明なサーバー・`status`）・§11.5.0（`diagnostics_wait_ms`・共通規約 3・`new_diagnostics`）、
  設計書 §7.7.6（1・5・6・`DiagDelta`）に理由（前からある診断を `added` と答えると、呼び手の AI は自分の編集が壊したと読み、無い誤りを
  追うか正しい編集を戻す）とともに書いた。

### 確認

- 単体（`tests/unit/test_edit_transaction.py`）: 本物の `DocumentRouter` / `DocumentSync` / `DiagnosticsHub` に、本文から診断を作る
  プロセスなしのサーバー 2 つ（basedpyright・ruff の名前）を結び、basedpyright の発行を編集の `didChange`（版 2）まで溜める（時間に
  頼らない）→ `partial`・`added: []`・`present_after` に basedpyright の 2 件・`resolved` は ruff の F401 だけ・
  `unknown_baseline: ["basedpyright"]`・警告。0.05 秒遅れて届く → `complete`・`resolved` に ruff F401 と basedpyright
  reportUnusedImport（`os`）。`dry_run` / 0 は待たない。`wait_sources` の無い口は `"*"`（`tree-sitter` の行はふつうに比べる）。
  写しの失敗も `added` にしない。`compute_delta` の単体 2 本。既存の 3 本（ロックの外の待ち・EP-7 の 2 本）は、編集前の基準の待ちに
  合わせて前提を直した（検査は緩めていない）。`tests/unit/test_lsp_documents_router.py` に `wait_sources` の 1 本。
- 実機（`tests/contract/test_T13_two_servers.py`、basedpyright + ruff。`tests/contract/_lsp_settle.HeldPublishes` で basedpyright の
  発行をハブに渡す前に止める／遅らせる。順序は保つ）: 票と同じ編集（2 行目の `import os` を ruff の QuickFix で消す）で、
  版 2 まで止める → `partial`・`added: []`・`present_after` は basedpyright の missing_mod_x の診断・`resolved` は ruff の F401・警告。
  3 秒遅らせる（票の再現）→ `complete`・`added: []`・`resolved` = {ruff F401, basedpyright reportUnusedImport}。
  まだ開いていない直接の参照元を基準の待ちで開いても、basedpyright は編集の後にその参照元を診断し直す（編集トランザクション〔T-10 の口〕で
  `pkg/a.py` の戻り値の型を変える → `complete`・`pkg/b.py` は fresh・`added` に `pkg/b.py` の reportReturnType）。
- `uv run ruff check` → `All checks passed!`。編集トランザクション・router・T-10/T-11/T-13 の契約テスト（実機を含む）・
  `tests/contract_fixtures/test_fx_edit_T10_T13.py` を一度に → `558 passed, 6 skipped`（skip は既存の「TypeScript の LSP が無い」ほか）。
  CPU を飽和させる負荷（14 コアで忙しく回るプロセス 28 個）の下で T-13 の 2 ファイルを 3 回 → 3 回とも `33 passed`（170 秒 / 143 秒 / 149 秒。参照元の実機の試験を足す前の 33 本。やり直しの警告 0 件）。全体テストは流していない。
