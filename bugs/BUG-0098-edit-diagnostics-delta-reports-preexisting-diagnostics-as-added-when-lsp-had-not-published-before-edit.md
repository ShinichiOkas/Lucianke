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
