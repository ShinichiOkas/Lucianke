# BUG-0107 exact の答え（LSP ready）で、名前の一致の本番の呼び出しが件数だけになり位置が見えない（構文木の答えより情報が減る）

- 報告: 2026-10-31 / 見た人: 親（dogfood。`main` = `13a728f`、Lucianke 自身を stdio で索引、basedpyright が ready になるまで待って同じ問い合わせ）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

`find_references_and_implementations {"target": {"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer.pack"}, "include_tests": false}`

- LSP indexing 中（構文木の答え）:
  ```
  # find_references_and_implementations ok conf=heuristic lsp=indexing ...
  references: 1 files, 2 sites (tests: 0; by name: 2)
  src/lucianke/mcp/fit.py:102:23  [call] in pack_and_render  page = packer.pack(...)  conf=heuristic
  src/lucianke/mcp/fit.py:142:23  ...  conf=heuristic
  ```
- basedpyright ready（exact の答え、2.74 秒）:
  ```
  # find_references_and_implementations ok conf=exact lsp=ready ...
  ! 2 call site(s) of .pack() whose receiver type is unknown to the syntax tree are not in this exact answer (the LSP could not type the receiver, e.g. Any); Packer.pack is the only method named 'pack' in the workspace (besides 7 in ...
  references: 0 files, 0 sites (tests: 0)
  ```
  位置（`fit.py:102`・`:142`）は compact の本文に出ない（警告の件数だけ）。

## 利用者（呼ぶ側の AI）として困ること

- より正確なはずの exact の答えで、本番の呼び出し元が「0 sites」に見える。構文木の答えでは位置まで見えていたのに、LSP が温まると見えなくなる。
- exact の一覧に推定を混ぜない方針（BUG-0071）は正しいが、別の欄として位置を出さないと、影響範囲を見落とす。

## 原因・直し方・確認（2026-10-31）

### 原因

- BUG-0071 で、`exact` の答え（LSP・PaaD の `exact` 行）には名前で結んだ行を混ぜないと決め、LSP の答えに無い「名前で結べる位置」は
  `warnings[]` の件数と先頭 5 件・`data.unattributed_calls[]`（`reason: not_in_exact_answer`。先頭 20 件）に書くだけにしていた
  （`t06._name_refs` の `exact=True`、`t07._exact_name_note`）。compact の本文は `references:` の件数と LSP の行だけなので、
  `Any` の受け手（`fit.py` の `packer: Any`）は LSP も型を決められず、本番の呼び出し元が「0 sites」に見えた。
- 記録（`Record`）はすべて json の `data[records_key]` に入り、精度の集計（`_resolution_meta`）にも数えられるので、位置を記録に
  すると `references[]` が純粋でなくなり（事後条件「`exact` 水準のとき `references[]` は LSP の結果と等しい」）、応答全体が
  `heuristic` の降格（`! DEGRADED`）に見える。位置を出す欄が無かった。

### 直し方（決めたこと）

- **補足の欄** を作った。`Record.section`（任意。`core/records.py`）: json はそのレコードの `data` を `data[records_key]` ではなく
  `data[section]` に置き（`render/json_out.py`。そのページに欄のレコードがあるときだけ）、`_resolution_meta` の精度の集計に
  数えない（`envelope.build_meta`）。compact の行・予算・ページネーションは本体と同じ（並びの順に詰め、続きはカーソルで）。
- **T-06**: `exact` の答えの `references[]` は LSP の答えのまま。BUG-0071 / BUG-0077 の段の判定で候補が対象 1 つだけ（組み込み型と
  同名でない）なのに LSP の答えに無い位置（呼び出しと属性の読み書き）を、本体の後ろに補足の欄 `name_matched` として並べる。
  - compact: `- not in exact answer (matched by name; receiver type unknown to the LSP): N sites` の `note`（欄の最初のレコードの
    先頭の行。ページの末尾に見出しだけが残らない）と、行末に `conf=heuristic` の `ref-line`。見出しの kv 行は
    `references: 0 files, 0 sites (tests: 0) + 2 by name, not confirmed by the LSP (tests: 0)`（どのページにも出る）。
  - json: `data.name_matched[]`（`references[]` と同じ形で `confidence_level: heuristic`・`match: "name"`）と `totals.name_matched`。
    `data.unattributed_calls[]` / `unattributed_refs[]` の `reason: not_in_exact_answer` は無くした（同じ位置を二重に書かない。
    `unattributed_*` は「結べなかったもの」だけ）。`warnings[]` は件数と根拠（`data.name_matched` を指す）。
  - `include_tests`・`scope_paths`・`use_kinds` は欄にも効く。同名が同じ近さに複数なら今までどおり何も書かない（LSP が正）。
    `dot` / `mermaid` の図には欄の辺も `conf=heuristic` で描く。
- **T-07**: `exact` の木には混ぜず、根の incoming で名前で根に結べるのに LSP の答えに無い呼び出しの呼び出し元を、incoming の木の
  直後に補足の欄 `name_matched` として並べる（深さ 1 だけ。その先は辿らない。`max_children` まで）。compact は
  `- not in exact tree (matched by name; receiver type unknown to the LSP): N callers, M sites` と `<- 呼び出し元 @path:line  conf=heuristic`、
  json は `data.name_matched[]`（`nodes[]` と同じ形）と省いた数 `data.name_matched_truncated`。`edges[]` には入れない。
  `unattributed_calls[]` の `not_in_exact_answer` は無くした。
- **json は `data.name_matched[]` にした理由**（`unattributed_calls` を全部の位置に広げる案と比べて）: `unattributed_calls` は
  `data_static` なのでページネーションも予算の詰め込みもできず、大きいと `max_tokens` を超える。`references[]` / `nodes[]` と同じ形
  なら呼ぶ側の AI は同じ処理で読め、欄の名前で「LSP が確かめていない」ことが分かる。
- **精度の集計に数えない理由**: 印つきの補足を添えただけで応答全体が `conf=heuristic`・`! DEGRADED` に見えると、AI は LSP の答え
  まで疑い、`exact` の事後条件の検査（`confidence_level == exact` で `references[]` を LSP と突き合わせる）も外れる。欄の各要素が
  自分の `confidence_level`（compact は行末の `conf=`）を持つ。
- 仕様書: §5.5 に補足の欄の集計の規約、T-06 処理規約 7・json の表（`totals.name_matched`・`name_matched[]`・`reason` から
  `not_in_exact_answer` を外す）・精度と降格、T-07 処理規約 9・json の表（`name_matched[]`・`name_matched_truncated`）・精度と降格
  （理由を 1 行添えた）。T-06 処理規約 9・T-07 処理規約 11 は処理規約 7 / 9 に従うだけなので文言は変えていない。設計書の
  `Record` の定義に `section`。

### 確認

Lucianke 自身（作業ツリーを in-process に索引。basedpyright が ready になり `budget.py` の最初の診断が来るまで待つ。
`src/lucianke/mcp/budget.py#Packer.pack`、`include_tests: false`）:

修正前（票の実測。`13a728f`）:

```
# find_references_and_implementations ok conf=exact lsp=ready ...
! 2 call site(s) of .pack() whose receiver type is unknown to the syntax tree are not in this exact answer (...)
references: 0 files, 0 sites (tests: 0)
```

修正後:

```
# find_references_and_implementations ok conf=exact lsp=ready rev=WORKING_TREE ws=lucianke ~245/2000tok
! 2 call site(s) of .pack() are not in this exact answer because the LSP could not type the receiver (e.g. Any); Packer.pack is the only method named 'pack' in the workspace (besides 7 in test files), so they are listed after the exact references under 'not in exact answer', marked conf=heuristic (data.name_matched)
target: [Method] lk://lucianke/src/lucianke/mcp/budget.py#Packer.pack
references: 0 files, 0 sites (tests: 0) + 2 by name, not confirmed by the LSP (tests: 0)
- not in exact answer (matched by name; receiver type unknown to the LSP): 2 sites
src/lucianke/mcp/fit.py:102:23  [call] in pack_and_render  page = packer.pack(out, max_tokens=call.max_tokens, offset=offset, cursor_factory=cursor_factory)  conf=heuristic
src/lucianke/mcp/fit.py:142:23  [call] in pack_and_render  page = packer.pack(  conf=heuristic
```

json（2 回目は PaaD の `exact` 行 `lsp-cache+tree-sitter`）: `_resolution_meta.confidence_level: exact`、`references: []`、
`name_matched: [fit.py:102 heuristic name, fit.py:142 heuristic name]`、`totals: {files: 0, sites: 0, tests: 0, name_matched: 2}`、
`unattributed_calls` なし。

T-07（incoming 深さ 1、`include_tests: false`）:

```
# query_call_hierarchy ok conf=exact lsp=ready rev=WORKING_TREE ws=lucianke ~172/2000tok
! incoming: 2 call site(s) of .pack() are not in this exact tree because the LSP could not type the receiver (e.g. Any); ...
root: [Method] lk://lucianke/src/lucianke/mcp/budget.py#Packer.pack direction=incoming depth=1
- not in exact tree (matched by name; receiver type unknown to the LSP): 1 callers, 2 sites
<- pack_and_render @src/lucianke/mcp/fit.py:102  when: fmt not in FITTED_FORMATS  conf=heuristic
```

テスト（決定的）:

- `tests/contract/test_T06_T07_exact_name_supplement.py`（新規 8 件。偽の LSP〔`test_T06_T07_exact_marks.FAKE_SERVER`〕が型の分かる
  `Packer().pack([1])` だけを答え、`packer: Any` の 2 箇所とテストの注釈なしの 1 箇所が LSP の答えに無い）: json の `references` は
  LSP の 1 件のまま・`name_matched` に 3 件（`heuristic`・`match: "name"`・`is_test`）・`totals.name_matched`・`_resolution_meta` は
  `exact`／`mixed: false`／降格なし、compact の見出し（`+ 3 by name, not confirmed by the LSP (tests: 1)`）・欄の見出しと
  `conf=heuristic` の行・本体の後ろの順・文法、`include_tests: false` で欄から外れる、予算を全体より 40 トークン小さくすると
  2 ページ以上に分かれ各ページ予算内・`exact`・本体 → 欄の順で重複なし（json）、LSP なし（`structural`）の答えは今までどおり
  （`references` に印つき・欄なし）、T-07 の木は LSP の 1 件・欄に 3 件・`edges` は木だけ・compact の見出しと行、
  `include_tests: false`、`dot` の図で欄の辺が破線、`structural` の木は今までどおり。
- `tests/contract/test_T06_T07_name_calls.py`: `not_in_exact_answer` を見ていた 2 件を、補足の欄の中身（5 箇所の位置・囲み・
  `is_test`・`snippet`）・`include_tests` / `scope_paths` / `use_kinds`・同名 2 つ（`Runner.run`）なら欄も警告も無い・T-07 の欄
  （`max_children` で切ると見出しに `first 2 callers shown`）の 5 件に置き換えた。
- `tests/unit/test_mcp_render.py`: `Record.section` が json の別のキーに入り本体の配列に入らない・集計に数えない・欄の無いページに
  キーが無い・本体 0 件のページはエンジンの水準（1 件）。
- `uv run ruff check`: クリーン。
- T-06 / T-07 の契約（`test_T06_T07*.py`〔name_calls・name_scopes・receivers・reexports・untyped_refs・inherited・import_roots・
  exact_marks・exact_budget・lsp・lsp_postconditions〕・`test_T06_ts_outside_tsconfig.py`・`test_T07_*.py`）と単体
  （`test_core_records.py`・`test_mcp_budget*.py`・`test_mcp_render.py`・`test_mcp_response_budget.py`・`test_name_calls.py`）:
  `382 passed, 6 skipped`（`50b712a` に載せ直した後は `384 passed, 6 skipped`）。`tests/contract_fixtures`: `45 passed, 6 skipped`（載せ直した後も同じ）（skip は TS の LSP が無い PC の F-TS の exact）。
- 見つけた別の不具合: **BUG-0110**（compact で全体より少し小さい予算だと 1 件も返らず、今より小さい `max_tokens` を勧める。
  `structural` の答えでも同じ。補足の欄の compact のページネーションの試験はこのため json だけで確かめた）。
