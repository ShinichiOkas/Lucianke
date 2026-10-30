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

## 原因・直し方・確認（2026-10-31）

### 原因

- **2 つの答えを違う経路で比べていた（試験の側の誤り）。** 事後条件 3 は `include_tests=true` の答え（`refs`）と `false` の答え（`nt`）の
  件数を比べる。`refs` は製品の期限のまま聞いていたので、負荷で T-06 の LSP の要求（`LSP_TIMEOUT_REFERENCES_MS` 3 秒）が切れると
  `structural` に降格する。F-PY の `URL.copy_with` の structural の答えは 9 件で、どれも `httpx/` の中（テストからの 35 か所の
  `.copy_with(...)` は受け手の型が構文から分からず、同名の `ParseResult.copy_with` があるので結ばない。BUG-0071 の仕様どおり、警告と
  `data.unattributed_calls` に出る）。テストの参照が 0 件なので、テストを除いても 9 件のままで `9 < 9` が落ちた。exact の答えは 29 件で
  テストの参照を含むので通る。どちらで答えるかが負荷で変わるので揺れた。
- 票の出力（`refs` が 9 件・どれも `httpx/`）は、BUG-0080 で測った降格した T-06 の答え（`confidence_breakdown: {'structural': 9}`）と同じ。
- **さらに**、basedpyright は references の要求が 1 度取り消されると同じプロセスの references がすべて失敗する（票 BUG-0090）ので、
  1 回目が切れると 2 回目（`nt`）も structural になり、どちらも 9 件になる。
- **製品の不整合ではない。** 同じ経路なら `include_tests=false` の答えは `true` の答えからテストの行を除いたものと一致する（下の確認）。
  structural にテストの参照が無いのは、受け手の型が分からない呼び出しを名前だけでは結ばない仕様（BUG-0071）による。

### 直し方

- `tests/contract_fixtures/test_fx_read_T00_T08.py` の T-06 の試験:
  - `refs` は BUG-0080 の直しで、LSP があれば最初から延ばした期限で exact を得る（`_rig.ask_exact(..., product_first=False)`）。
  - `nt` も同じく `ask_exact(..., product_first=False)` で得る（PaaD の exact 行 `lsp-cache` から答える。LSP が無い F-TS は 2 つとも structural）。
  - 2 つの答えの水準が同じこと（違えば両方のエンジンを書いて落とす）、`refs` にテストの参照があること（無ければ事後条件 3 は検査に
    ならない）、`nt` に `is_test` が無いこと、件数が減ること、どちらもページが切れていなければ `nt` が `refs` からテストを除いた集合と
    一致することを確かめる（弱めず、強めた）。

### 確認

- 降格を強制（`lucianke.lsp.features.LSP_TIMEOUT_REFERENCES_MS = 1` を monkeypatch。負荷で 3 秒を超えたのと同じ）:
  直す前の試験（`9f4ffa3` の `test_fx_read_T00_T08.py`）は票と同じ assert で落ちる（`assert (not False and 9 < 9)`、`refs` も `nt` も
  9 件で `httpx/_client.py:237` などだけ）。直した試験は同じ条件で通る（`1 failed, 1 passed`: 落ちたのは直す前の試験だけ）。
- 直した T-06 の試験を単独で: `1 passed, 1 skipped`（F-TS の skip は TypeScript の LSP が無いときの T-06#1 だけ。F-TS は structural 同士で
  集合の一致まで通った）。
- `tests/contract_fixtures` 全体（`8c10a2a` + BUG-0080・BUG-0086 の直し）: 単独 1 本と、2 本同時（負荷）の計 3 本。T-06 の試験
  （事後条件 1〜6）は 3 本とも F-PY で通った（F-TS は T-06#1 だけ TypeScript の LSP が無い理由で skip）。全体の結果は
  `1 failed, 44 passed, 6 skipped`（377 秒）・`45 passed, 6 skipped`（370 秒）・`1 failed, 44 passed, 6 skipped`（374 秒）で、落ちた
  1 件はどちらも T-06 ではなく T-20 の `test_t20_postcondition_1_2_writes_are_rejected_and_the_db_is_untouched[F-PY]`（DB ファイルの
  SHA-256 が試験の間に変わる。単独では 3 回とも通る。票 BUG-0092。この直しは T-19・T-20 と索引・DB のコードに触れていない）。
- BUG-0080 の直しを載せた `c7f2b7e`・`9f4ffa3` の全体 5 本（単独 2・`tests/unit tests/contract` と並行 1・2 本同時 2）でも、
  T-06 の試験は毎回通った。
