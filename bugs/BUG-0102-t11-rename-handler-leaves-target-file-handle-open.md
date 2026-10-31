# BUG-0102 T-11 `rename_symbol` の呼び出しで、対象ファイルを開いたハンドルが閉じられずに残る（`ResourceWarning: unclosed file`）

- 報告: 2026-10-31 / 見た人: BUG-0098・BUG-0099 を直す役（`tests/contract_fixtures/test_fx_edit_T10_T13.py` を流していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（origin/main `8ef3c8d` の上の作業ツリー）

`uv run pytest -q -p no:cacheprovider -rs -W always tests/contract_fixtures/test_fx_edit_T10_T13.py` → `10 passed, 2 skipped, 10 warnings`。
警告のうち 4 件が `test_t11_postcondition_4_unapplied_renames_change_nothing[F-TS]` で出た次のもの:

```
src\lucianke\tools\t11_rename_symbol.py:650: ResourceWarning: unclosed file <_io.BufferedReader name='C:\\Users\\...\\pytest-25670\\test_t11_postcondition_4_unapp1\\ky\\source\\utils\\merge.ts'>
    head = await asyncio.to_thread(lambda: to_abs(ws, rel).open("rb").read(HEAD_BYTES))
```

## 困ること

- 呼び出しのたびに対象ファイルのハンドルが GC まで開いたまま残る。Windows では開いたハンドルがあるファイルの置き換え・削除が
  共有違反で失敗しうる（編集ツールの原子的な書き込みや、利用者のエディタの保存）。
