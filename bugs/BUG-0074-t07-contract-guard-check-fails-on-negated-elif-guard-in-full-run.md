# BUG-0074 全体テストで T-07 の契約テスト（F-PY）が `when` の検査で落ちる。単独で流すと通る（揺れるテスト）

- 報告: 2026-10-31 / 見た人: BUG-0070 を直す役（全体テストの途中で見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

- 全体テスト `uv run pytest -q -p no:cacheprovider`（基は 08b946e + BUG-0070 の作業中の変更。3,795 passed・2 failed・32 skipped、1,401 秒）で落ちた:

```
FAILED tests/contract_fixtures/test_fx_read_T00_T08.py::test_t07_postcondition_1_2_3_4_5_6_call_hierarchy[F-PY]
E   AssertionError: ('!(isinstance(url, str)) && isinstance(url, URL)', 119)
E   assert '!(isinstance(url,str' in '"scheme":str,"username":str, ... uri_reference=urlparse(url,**kwargs)elifisinstance(url,URL):self._uri_reference=url._uri_reference.copy_with(**kwargs)'
tests\contract_fixtures\test_fx_read_T00_T08.py:487: AssertionError
```

  （もう 1 件は BUG-0072 の import の向きのテスト。origin/main の 8536a5f で直っている）
- 同じ作業ツリーで、このテストだけを流すと通る:
  `uv run pytest -q -p no:cacheprovider "tests/contract_fixtures/test_fx_read_T00_T08.py::test_t07_postcondition_1_2_3_4_5_6_call_hierarchy"` → `1 passed, 1 skipped in 27.26s`
- 落ちたときの辺の `guard` は `!(isinstance(url, str)) && isinstance(url, URL)`（F-PY〈httpx〉の 119 行目の呼び出し。`if isinstance(url, str): ... elif isinstance(url, URL): ...` の `elif` の中）。テストの検査（事後条件 3）は `guard` を `&&` / `||` / `and` / `or` / `not` で分けて、各部分がソースの窓にあるかを見る。`!(...)` の `!` は分けない。

## 再現手順

- 全体テストを流す（約 23 分。負荷の高い状態）。単独では再現しなかった（1 回）。
