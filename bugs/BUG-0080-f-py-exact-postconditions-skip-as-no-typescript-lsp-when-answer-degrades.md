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

## 原因・直し方・確認（2026-10-31）

### 原因

- **試験の側の誤り。** T-06#1・T-07#2 は「応答が exact のときだけ LSP と比べ、そうでなければ skip」と書いていた。skip の理由は F-TS 用の
  固定の文言（「no TypeScript LSP」）で、F-PY の basedpyright が ready でも、応答が降格すれば同じ文言で skip した。応答が exact に
  なるかは負荷で変わる（T-07 は 1 問い合わせの LSP の予算 `EXACT_BUDGET_S` 10 秒、T-06 は要求の期限 `LSP_TIMEOUT_REFERENCES_MS` 3 秒）
  ので、負荷しだいで検査が黙って外れた（passed と skipped の間で揺れる）。
- **同じ書き方が他にもあった。** T-04#4・T-11・T-13 も、F-PY の LSP が ready にならなかったときに「no TypeScript LSP」で skip する。
  T-06#5・T-07#5（exact でない事後条件）は skip の後ろにあり、F-TS では検査されていなかった（skip の理由の文言にも出ない）。
  T-04#4 は逆に、負荷で T-04 が降格すると「the LSP is ready but no item is exact」で落ちる（揺れる fail）。
- **製品の側で見たこと（票に書いた。直していない）。**
  - 冷えた LSP での F-PY の T-07 depth 3 は、負荷をかけなくても 1 回目が予算 10 秒（または要求の期限 5 秒）に届き、5 回中 4 回
    降格した（BUG-0091）。
  - basedpyright は references の要求が 1 度取り消される（製品の期限 3 秒で切れる）と、同じプロセスの references がすべて
    `this._token.cancel is not a function` で失敗し、T-06 はセッションの間ずっと `structural` になる（BUG-0090）。
    T-06 の真値を製品の期限で聞く書き方だと、試験もこれで落ちる。

### 直し方

- `tests/contract_fixtures/_rig.py`:
  - `require_exact_lsp(rig, what)`: skip は、型解決の LSP がこの環境に **入っていない**（起動コマンドが見つからない）ときだけ。
    理由はそのとおり（`no TypeScript LSP installed (typescript-language-server / vtsls not found): F-TS exact postconditions unverified (...)`）。
    入っているのに ready にならなければ fail（F-PY で basedpyright が起きなかったのを skip で隠さない）。
  - `ask_exact(rig, tool, what=..., **args)`: 製品の期限のまま聞き、exact でなければ、LSP が ready のままか確かめてから、期限を延ばして
    （`patient_lsp`）最大 3 回聞き直す。それでも exact でなければ **fail**（各回の水準・エンジン・`lsp_status`・警告・所要を書く）。
    聞き直して exact になったときは pytest の警告を出す（検査はしたが、製品の期限で降格したことは隠さない）。
  - `patient_lsp()`: その間だけ `features.LSP_TIMEOUT_*`（120 秒）と `_lsp_exact.EXACT_BUDGET_S`（180 秒）を延ばす。exact の答えの中身は
    期限に依らない（LSP の答えそのもの。期限で変わるのは exact で答えきれたかだけ）ので、事後条件は弱めていない。試験が LSP に直に聞く
    真値（T-04 の hover、T-06 の references、T-07 の callHierarchy、T-11 の参照数）もこの中で聞く（製品の期限で切れると試験が落ち、
    その時間切れが LSP の連続時間切れ〈3 回で unavailable〉に数えられる）。
- `tests/contract_fixtures/test_fx_read_T00_T08.py`:
  - T-04 の resolve_symbols、T-07 の depth 1 の答えを `ask_exact` で得る。
  - T-06 は最初の答えを `ask_exact(..., product_first=False)`（最初から延ばした期限）で得て、すぐ真値を取る（BUG-0090 のため、製品の
    期限で references が 1 度取り消されると、以降は exact も真値も得られない）。以降の T-06 の問い合わせは PaaD の exact 行から答える
    （`lsp-cache`）ので、事後条件 2〜6 も経路が揺れない。T-07 の depth 3（事後条件 1・3・4・6）は今までどおり製品の期限で聞く
    （どちらの経路で答えても同じ検査。BUG-0074）。
  - exact の事後条件（T-06#1・T-07#2）を試験の最後に移し、skip が T-06#5・T-07#5 を外さないようにした。docstring の「skip はしない
    （`skips = "fail"`）」を事実（`skips = "report"`、skip は TypeScript の LSP が無いときの exact の事後条件だけ）に直した。
- `tests/contract_fixtures/test_fx_edit_T10_T13.py`: T-11・T-13 の skip を `require_exact_lsp` にした。T-11 の真値（参照数）を `patient_lsp` で聞く。
- `src/lucianke/tools/_lsp_exact.py`: `CallEdgeSource` の `budget_s` の既定を、定義時ではなく作るときの `EXACT_BUDGET_S` にした
  （`budget_s: float = EXACT_BUDGET_S` は定義時に値が固定され、試験から延ばせなかった）。既定値 10 秒と挙動は変えていない。
  製品の予算は変えていない（変える根拠になる数字は BUG-0091 に書いた。どう直すかは直す役が決める）。

### 確認

- 降格を強制（`EXACT_BUDGET_S=0`・`LSP_TIMEOUT_REFERENCES_MS=1`・`LSP_TIMEOUT_HOVER_MS=1`・`LSP_TIMEOUT_DEFINITION_MS=1` を monkeypatch。
  負荷で使い切ったときと同じ降格）: F-PY の T-04・T-06・T-07・T-11（事後条件 1〜3）は 4 passed（直す前は T-06・T-07 が「no TypeScript
  LSP」で skip）。T-07 は聞き直しの警告（`exact only after a patient re-ask; #1 (product budgets, 0.0s): confidence=structural ...`）が出る。
- 延ばした期限でも exact にならないとき（`PATIENT_TARGETS` の予算も 0 にした）: T-07[F-PY] は FAILED（4 回の試行の内訳つき）。skip しない。
- `tests/contract_fixtures` 全体（`uv run pytest -q -p no:cacheprovider -rfs tests/contract_fixtures`。`c7f2b7e` に載せて。同じ PC で
  他の役の試験も走っていた）:
  1. 単独: `40 passed, 5 skipped`（402 秒）。
  2. `tests/unit tests/contract` を並行に流して負荷をかけた: `40 passed, 5 skipped`（589 秒）。
  3. `tests/contract_fixtures` を 2 本同時に流した: 2 本とも `40 passed, 5 skipped`（574 秒・544 秒）。
  4 本とも skip は F-TS の 5 件（`no TypeScript LSP installed (typescript-language-server / vtsls not found)`、T-04#4・T-06#1・T-07#2・
  T-11#2-3・T-13#1-3）だけで、F-PY の exact の事後条件（T-04#4・T-06#1・T-07#2・T-11・T-13）は 4 本とも検査して通った。F-PY の skip・
  失敗は 0。この 4 本では聞き直しの警告は出なかった（T-07 の depth 1 は depth 3 の後なので LSP が温まっている。T-06 は最初から延ばした期限）。
- `uv run ruff check`（引数なし）: All checks passed。

### 見つけた別の不具合（票）

- BUG-0090: basedpyright の references の要求が 1 度取り消されると、同じプロセスの references がすべて失敗し、T-06 がセッションの間
  `structural` になる（警告は「LSP が利用できない」、`lsp_status` は `ready`）。
- BUG-0091: 冷えた LSP での F-PY の T-07 depth 3 が、負荷なしでも 1 回目に予算 10 秒（または要求の期限 5 秒）に届いて降格する。
  降格の警告は使い切った予算ではなく「5000 ms」と書く。

### 確認（追記: 載せ直した後）

- `8c10a2a` に載せ直し、BUG-0086 の直しも足して `tests/contract_fixtures` 全体を単独 1 本・2 本同時 1 組（計 3 本）: F-PY の exact の
  事後条件（T-04#4・T-06#1・T-07#2・T-11・T-13）は 3 本とも通った。skip は 3 本とも F-TS の 5 件（TypeScript の LSP が無い）と、F-TS には
  当てはまらない BUG-0079 の試験 1 件だけ。別の 1 件（T-20、DB ファイルの SHA-256 が試験の間に変わる）が 3 本中 2 本で落ちた → 票 BUG-0092。
