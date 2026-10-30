# BUG-0080 F-PY の契約テストで、LSP が ready なのに応答が `structural` に降格すると、exact の事後条件を「no TypeScript LSP」の理由で skip する

- 報告: 2026-10-31 / 見た人: BUG-0074 を直す役
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `208f4a0` + BUG-0074 の直し）

- `tests/contract_fixtures/test_fx_read_T00_T08.py::test_t07_postcondition_1_2_3_4_5_6_call_hierarchy[F-PY]` を、T-07 の LSP の予算を 0 秒にして
  （`lucianke.tools._lsp_exact.CallEdgeSource.__init__` の `budget_s` の既定を 0.0 に monkeypatch。負荷で予算を使い切ったときと同じ降格になる）
  3 回流した → 3 回とも `SKIPPED`。理由は
  `no TypeScript LSP: F-TS exact postconditions unverified (T-07#2 call hierarchy depth-1 vs LSP)`。
  F-PY の `rig` は basedpyright が ready（`rig.lsp_ready` が真）で、TypeScript の LSP とは関係が無い。
- 同じ書き方が T-06 の試験（同じファイルの `# 事後条件 1: exact 水準のとき、LSP の参照検索結果と一致する` の `else`）にもある:
  `assert not rig.lsp_ready or meta_level(r) != "exact"` の後に同じ文言で skip する（`else` の中なので assert は常に真）。
- BUG-0074 の全体テスト（負荷の高い状態）では、同じ T-07 の試験の depth = 3 の応答が `structural` に降格していた（`structural` の層にしか
  無い辺の `when` で落ちた）。降格は単独で流したときも、取得直後の 1 回目で 1 度起きた（3 回中 1 回）。
- `bench/exit_criteria.toml` の `tests/contract_fixtures` の項は `skips = "report"`（BUG-0057。F-TS の skip を未検証扱いにしないため）。
  試験ファイルの docstring は「skip はしない（`skips = "fail"`）」と書いている。

## 再現手順

1. 上の monkeypatch を当てて T-07（または T-06）の F-PY の試験を流す → `SKIPPED`（理由は TypeScript の LSP が無い、と書く）。

## 困ること

- 負荷しだいで、F-PY の exact の事後条件（T-06#1・T-07#2）が検査されないまま「passed + skipped」になる（揺れる。検査していないことが
  理由の文言から分からない）。
