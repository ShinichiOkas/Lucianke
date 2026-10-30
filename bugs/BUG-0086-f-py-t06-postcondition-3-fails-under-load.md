# BUG-0086 F-PY の T-06 の契約テストの事後条件 3（`include_tests=false` の件数が `true` より少ない）が、負荷の高いときに落ちる

- 報告: 2026-10-31 / 見た人: BUG-0079・BUG-0081 を直す役
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

- 作業ツリー = `9f4ffa3`（BUG-0078）+ BUG-0079・BUG-0081 の直し。同じ PC で別の pytest（`tests/unit`・`tests/contract` の 22 ファイル）を
  同時に流しながら、次を流した:

  ```
  uv run pytest -q -p no:randomly tests/contract_fixtures/test_fx_read_T00_T08.py -k "attribute_receiver or t07_postcondition or t06_postcondition" -rs
  ```

  → `1 failed, 2 passed, 3 skipped`。落ちたのは `test_t06_postcondition_1_2_3_4_5_6_references[F-PY]` の 443 行
  （`assert not any(x["is_test"] for x in nt) and len(nt) < len(refs)  # 事後条件 3`）。出力の末尾:

  ```
  E        +  and   9 = len([{'path': 'httpx/_client.py', 'line': 237, 'col': 20, 'use': 'call', ...}, {'path': 'httpx/_client.py', 'line': 410, '...ine': 219, 'col': 23, 'use': 'call', ...}, {'path': 'httpx/_urls.py', 'line': 343, 'col': 21, 'use': 'call', ...}, ...])
  ```

  （`include_tests=true` の 1 ページ目の `references` が 9 件で、どれも `httpx/` の中に見えた。）
- 直後に同じ試験だけを流すと通った: `9f4ffa3` 単独（`git switch --detach origin/main`）で `1 passed, 1 skipped`、BUG-0079・BUG-0081 の
  直しを載せた作業ツリーで `1 passed, 1 skipped`。
- 載せ直す前（`0fee57d` + 直し）の `tests/contract_fixtures` 全体（24 分）では通っていた（`41 passed, 6 skipped`）。

## 困ること

- 負荷しだいで F-PY の T-06 の契約テストが落ちる（揺れる）。全体テストの結果が PC の混み具合で変わる。
