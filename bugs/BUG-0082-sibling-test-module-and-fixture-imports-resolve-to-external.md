# BUG-0082 テストの兄弟モジュールの import（`from test_T06_T07_lsp import Env`・`from _t11_harness import Rig`）とフィクスチャの中の import（`from app.engine import Packer`）が、ワークスペースのファイルに結ばれず `lk://_ext/...` になる

- 報告: 2026-10-31 / 見た人: BUG-0077 を直す役（名前だけの一致の候補を測っている途中で見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`fa82dc7` のスナップショットを in-process で索引。LSP なしの `structural`。ワークスペース ID `Lucianke`）

`relations`（`source_file_id` のファイルで、先の URN に名前を含む行）:

```
tests/contract/test_T06_T07_exact_marks.py:18   IMPORTS    lk://_ext/test_T06_T07_lsp#Env
tests/contract/test_T06_T07_exact_marks.py:229  TYPE_USES  lk://_ext/test_T06_T07_lsp#Env
tests/contract/test_T06_T07_exact_marks.py:254  CALLS      lk://_ext/test_T06_T07_lsp#build_env
tests/contract/test_T11_rename.py:24            IMPORTS    lk://_ext/_t11_harness#Rig
tests/contract/test_T11_rename.py:134           CALLS      lk://_ext/_t11_harness#Rig.pos
tests/fixtures/untyped_calls/app/service.py:5   IMPORTS    lk://_ext/app#engine.Packer
tests/fixtures/untyped_calls/app/service.py:44  CALLS      lk://_ext/app#engine.Packer.pack
```

- `Env` は `tests/contract/test_T06_T07_lsp.py`、`Rig` は `tests/contract/_t11_harness.py`、`Packer` は
  `tests/fixtures/untyped_calls/app/engine.py` に定義がある（どれもワークスペースの中）。
- `tests/contract/test_T06_T07_exact_marks.py` から `lk://Lucianke/tests/contract/test_T06_T07_lsp.py#Env` への行は 0。
- `find_references_and_implementations {"target": {"path": "tests/contract/test_T06_T07_lsp.py", "symbol_name": "Env"}, "query_type": "references", "format": "json"}`
  の参照のファイルは `tests/contract/test_T06_T07_lsp.py` だけ（`test_T06_T07_exact_marks.py` の行は 0 件）。
- `lk://_ext/test_%`・`lk://_ext/_t11_harness%`・`lk://_ext/app#%` の外部シンボルは 76 個。
- `tests/fixtures/untyped_calls/` だけをワークスペースにした索引（`tests/contract/test_T06_T07_name_calls.py`）では、同じ
  `Packer().pack(["x"])`（44 行）は `app/engine.py#Packer.pack` への `structural` の参照として出る。

## 再現

1. Lucianke のリポジトリを索引する（stdio でも in-process でもよい）。
2. `find_references_and_implementations {"target": {"path": "tests/contract/test_T06_T07_lsp.py", "symbol_name": "Env"}, "query_type": "references"}`
   に、`tests/contract/test_T06_T07_exact_marks.py:18` の import と 229 行・252 行の型の使用が出ない。
3. `query_project_db` で上の `relations` の行を見る。

## 利用者（呼ぶ側の AI）として困ること

- テストの土台（`Env`・`Rig`）やフィクスチャのクラスを変えるときに、それを import して使うテストファイルが参照にも呼び出し
  階層にも出ない（影響範囲を見落とす）。
