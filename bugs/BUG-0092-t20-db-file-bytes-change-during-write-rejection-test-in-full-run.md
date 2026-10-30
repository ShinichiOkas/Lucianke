# BUG-0092 F-PY の T-20 の契約テスト（書き込み文を断り、DB ファイルが変わらない）が、`tests/contract_fixtures` の全体で流すと DB ファイルの SHA-256 が変わって落ちる

- 報告: 2026-10-31 / 見た人: BUG-0080・BUG-0086 を直す役
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

- 落ちた試験: `tests/contract_fixtures/test_fx_db_T19_T20.py::test_t20_postcondition_1_2_writes_are_rejected_and_the_db_is_untouched[F-PY]`
  の 87 行（`assert sha(path.read_bytes()) == before_hash and counts(path, views) == before_counts`）。左の SHA-256 の比較で落ちた
  （行数の比較は評価されていない）:

  ```
  E       AssertionError: assert ('fb3627bbff56...78ef894363368' == '9c9257c07c9d...941bc6c6eddbe'
  ```

  もう 1 回は `'d0408db0ea2a...df4ac67e2b34c' == '306edf194606...6ed2e5c51d6fe'`。
- 作業ツリー = `8c10a2a`（BUG-0079 & BUG-0081、スキーマ v7）+ BUG-0080・BUG-0086 の直し（`tests/contract_fixtures/_rig.py`・
  `test_fx_read_T00_T08.py`・`test_fx_edit_T10_T13.py` と `_lsp_exact.CallEdgeSource` の予算の既定の読み方。T-19・T-20 の試験と索引・DB の
  コードには触れていない）。`uv run pytest -q -p no:cacheprovider -rfs tests/contract_fixtures` を:
  - 単独で 1 回 → `1 failed, 44 passed, 6 skipped`（377 秒）。落ちたのは上の 1 件。
  - 2 本同時に → 1 本は `45 passed, 6 skipped`、もう 1 本は `1 failed, 44 passed, 6 skipped`（同じ 1 件）。
- 同じ作業ツリーで上の試験だけを 3 回流すと、3 回とも通った（各 8〜11 秒）。
- 載せ直す前（`c7f2b7e`・`9f4ffa3` + 同じ直し）の `tests/contract_fixtures` 全体 5 回では、この試験は毎回通っていた。

## 困ること

- 全体テストで T-20 の契約テストが揺れる。試験は書き込み文を 10 個断ったあと `SELECT count(*)` を 1 回流すだけなので、その間に
  DB ファイルのバイト列を変えたのが何かが分からない（事後条件 T-20#2「DB ファイルの SHA-256 は変わらない」が守られていないのか、
  試験の前提〈索引が落ち着いている〉が崩れているのかを、この出力からは区別できない）。
