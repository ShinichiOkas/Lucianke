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

## 原因・直し方・確認（2026-10-31）

### 原因

- **揺れの元は、T-07 がどちらの経路で答えたか。** T-07 の exact 経路は、1 回の問い合わせで LSP に使う時間の合計を `EXACT_BUDGET_S`（10 秒。各ノードのファイルの最初の診断を待つ時間を含む）に限り、使い切ると木ごと `structural`（索引の `relations`）に降格する。負荷の高い全体テストや、取得直後の 1 回目の単独実行は予算を使い切って降格し、温まった単独実行は exact で答える。
- **2 つの経路は辺の集合が違う。** 索引の `relations` には `URL.__init__ → URL.copy_with`（`httpx/_urls.py:119` の `url._uri_reference.copy_with(**kwargs)`、`guard = !(isinstance(url, str)) && isinstance(url, URL)`、`structural`）の行がある。LSP（basedpyright）の incoming calls（21 件）には 119 行は無い（`ParseResult.copy_with` の呼び出し）。この辺は structural の層の誤り（票 BUG-0079）。だから 119 行の辺は降格したときだけ応答に出る。
- **落ちたのは試験の側の誤り。** 事後条件 3 の検査は `when` を `&& || and or not` で分けて `.strip("()")` するだけで、`!(…)` を外さずに `!(isinstance(url,str` を探していた。`!(条件)` は仕様どおりの書き方（`else`・前の `elif` の条件の否定。単体テスト `tests/unit/test_syntax_guards.py::test_py_elif_chain` が `!(a) && b` を固定）。
- **書き方は exact と structural で同じ。** どちらも `extract_relations` → `syntax/guards.guard_for` で作る（exact は LSP の位置に合う `SiteInfo.guard` を付ける）。書き方の不一致ではない。
- **もう 1 つの潜在の揺れ（同じ検査の窓）。** 窓が「呼び出しの前の 40 行」だったので、外側の `if` が 40 行より上にある辺（ky の `source/utils/merge.ts` の 320〜465 行の 23 か所など）や、条件が呼び出しの後ろの行にある複数行の三項（httpx の `httpx/_main.py:137`）では、正しい `when` でも落ちる。どの辺が返るかで結果が変わる。

### 証拠

- 単独で 3 回（取得直後から）: 1 回目は同じ assert で落ち、2・3 回目は通った。
- 温まった状態で T-07（`URL.copy_with`、incoming、depth 3）の応答を 10 回記録: すべて `conf=exact`・辺 56 本・119 行の辺なし。同じときの索引の `relations` には上の 119 行の行がある。
- LSP の予算を 0 秒にして（`CallEdgeSource.__init__` の `budget_s` を monkeypatch。負荷で使い切ったときと同じ降格）直す前の試験を流す → 3 回とも同じ assert（`('!(isinstance(url, str)) && isinstance(url, URL)', 119)`）で落ちる（毎回再現）。

### 直し方

- `tests/contract_fixtures/test_fx_read_T00_T08.py` の事後条件 3:
  - `guard_sources`: 仕様書 T-07 処理規約 4 の書き方で読む。括弧の外の ` && ` で項に分け、`!(…)`・`(…)` を外し、`on: 型` は型（`*` は `except`、TS の型なしは `catch`）、末尾の `…` は 120 文字の省略として扱う。残りをソーステキストとして探す。
  - `guard_window`: 探す範囲を、呼び出し箇所を含む最も外側の関数の本文にした（guard_for が遡る範囲を含む。関数の外ならファイル全体）。
  - 読み方の確かめ `test_t07_guard_sources_reads_the_when_grammar`（rig なし）を足した。
- 仕様書 §11 T-07 処理規約 4 に、否定の書き方（言語によらず `!(条件)`、`elif` は前の条件の否定と自分の条件を連ねる、例 `!(a) && b`）・`(条件)` の囲み・exact と structural で同じことを明記し、事後条件 3 を「ソーステキストと連ねる記号だけから」にした。理由: 呼ぶ側の AI が言語ごとの否定の書き方を知らずに、呼び出しが実行される条件をそのまま読めるようにする。実装の書き方は変えていない（今の書き方が AI に最も読みやすい: 条件を省かず、記号は 1 種類）。

### 確認

- F-PY・F-TS の全ファイルのすべての呼び出しの guard を guard_for で作り、新しい検査に通した（どちらの経路がどの辺を返しても通るか）: httpx 718 件（否定 266・`on:` 49・省略 65）、ky 692 件（否定 202・`on:` 33・`case`/`default` 37・省略 17）→ 失敗 0。前の 40 行の窓では 33 件落ちる。
- LSP の予算 0 秒（降格を強制）: 直した試験は 3 回とも事後条件 3 を通る（その後の事後条件 2 は降格のため「no TypeScript LSP」の理由で skip。票 BUG-0080）。
- 単独 3 回（`tests/contract_fixtures` の全体と並行に流して負荷をかけた）: 3 回とも `1 passed, 1 skipped`（F-TS の skip は TypeScript の LSP が無いため。従来どおり）。
- `tests/contract_fixtures` 全体 3 回（`uv run pytest -q -p no:cacheprovider -rfs tests/contract_fixtures`。同じ PC で他の役の全体テストも走っていた）: 3 回とも `40 passed, 5 skipped`（213 秒・207 秒・824 秒。3 回目は負荷が高い）。skip は 3 回とも F-TS の 5 件（TypeScript の LSP が無い。各 `[1]`）だけで、F-PY の skip・失敗は無い。
- `uv run ruff check`（引数なし）: All checks passed。T-07 の単体・契約（`tests/unit/test_syntax_guards.py`・`tests/contract/test_T06_T07*.py`・`test_T07_*.py`）: 98 passed, 2 skipped（TypeScript の LSP が無い）。

### 見つけた別の不具合（票）

- BUG-0079: 構造層の CALLS が `url._uri_reference.copy_with(...)` を `URL.copy_with` に結び付ける（LSP は結び付けない）。
- BUG-0080: F-PY の契約テストで、LSP が ready なのに応答が降格すると、exact の事後条件（T-06#1・T-07#2）を「no TypeScript LSP」の理由で skip する。
