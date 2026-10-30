# BUG-0065 全体テストで T-02（skeleton）系の 7 件が落ちる（`KeyError: 'children'`・`'signature'` が無い・F-PY で 2 ファイル目が返らない）

- 報告: 2026-10-31 / 見た人: BUG-0063・BUG-0064 を直した役（全体テストを流したとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 症状

`uv run pytest -q -p no:cacheprovider`（作業ツリー = `87d9451` + BUG-0063/0064 の未コミット差分。langs extra 同期済み・`bench/.cache` に httpx・ky あり）:
`7 failed, 3774 passed, 32 skipped in 989.66s`。落ちたのは次の 7 件だけ。

```
FAILED tests/contract_fixtures/test_fx_read_T00_T08.py::test_t02_postcondition_1_2_5_skeleton_matches_the_source[F-PY]
FAILED tests/unit/test_syntax_skeleton.py::test_fold_lines_and_data_agree - KeyError: 'children'
FAILED tests/unit/test_syntax_skeleton.py::test_json_item_shape - AssertionError
FAILED tests/unit/test_syntax_skeleton.py::test_include_private_false_drops_private
FAILED tests/unit/test_syntax_skeleton.py::test_raises_do_not_leak_across_nested_definitions
FAILED tests/unit/test_syntax_skeleton.py::test_signature_and_doc_text_come_from_source[src/shop/services/payment.py-python]
FAILED tests/unit/test_syntax_skeleton.py::test_signature_and_doc_text_come_from_source[web/src/payment.ts-typescript]
```

## 実測（エラーの行）

- `tests/unit/test_syntax_skeleton.py:74`（`flatten`）: `out.extend(flatten(s["children"]))` → `KeyError: 'children'`（5 件がこれ。対象の記号は `PaymentRequest.orderId` など子を持たない記号）
- `test_json_item_shape`: `AssertionError: assert 'signature' in {'urn': 'lk://app/src/shop/services/payment.py#PaymentRequest', 'path': ..., 'name': 'PaymentRequest', 'qualified_name': 'PaymentRequest', ...}`
- `test_t02_postcondition_1_2_5_skeleton_matches_the_source[F-PY]`（`get_file_skeleton`、`max_tokens=6000`）:
  `assert [i["path"] for i in items] == list(t.skeleton)` → 返った `items` は `['httpx/_urls.py']` の 1 件だけ。期待は `httpx/_urls.py` と `httpx/_api.py` の 2 件（`Right contains one more item: 'httpx/_api.py'`）

## 手がかり（見たことだけ）

- 落ちた 7 件はすべて T-02 / skeleton の json の形に関するもの。BUG-0063/0064 の差分（`core/limits.py`・`mcp/budget.py`・`mcp/pipeline.py`・`mcp/registry.py`・`tools/_base.py`・`mcp/render/graph.py`）は skeleton のコードを触っていない
- 直近に T-02 の json を変えたコミット: `1e1a9aa BUG-0055: drop null/empty fields from T-02 json symbol output to reduce token bloat`

## 再現

```powershell
uv run pytest -q tests/unit/test_syntax_skeleton.py
uv run pytest -q "tests/contract_fixtures/test_fx_read_T00_T08.py::test_t02_postcondition_1_2_5_skeleton_matches_the_source"
```

## 原因・直し方・確認（2026-10-31）

- 原因（7 件とも同じ 1 つ）: `1e1a9aa`（BUG-0055）が `syntax/skeleton.py` の `_symbol_json` で、値が null・空のキー（`scip_symbol`・`selection_range`・`signature`・`return_type`・`doc_summary`・`modifiers`・`children`・`fold_lines`=0・`decorators`・`raises`）を出さないようにした。確かめ: HEAD（`b1ce8ac`）の `skeleton.py` だけを `1e1a9aa^` の版に戻すと、再現の 2 コマンドが **25 passed**（他のファイルは HEAD のまま。BUG-0063/0064 は関係しない）。
  - 単体 6 件: 子の無い記号に `children` が無い（`flatten` の `KeyError`）。`PaymentRequest`（dataclass。シグネチャ無し）に `signature` が無い。
  - F-PY の 1 件（2 ファイル目が返らない）も同じ原因で、予算の詰め方の結果。json の量（`used_tokens_est`。単独で `max_tokens=50000` で呼んだ値）が `httpx/_urls.py` 6,751 → 5,993、`httpx/_api.py` 2,199 → 2,078 に減った。省く前は `_urls.py` が単独で 6,000 に入らないので縮退（`truncated_members` 50）し、`_api.py` も全部入って 2 件（応答全体 2,637 トークン）。省いた後は `_urls.py` が縮退せずに丸ごと入り（応答全体 5,591）、残りに `_api.py` の最小の縮退も入らず、次ページに回った（1 件・`next_cursor` あり）。
- どちらが正しいか: 実装（`1e1a9aa`）が仕様に合っていない。テストが仕様どおり。
  - 仕様書 §6.2 は SymbolRecord を `data` 内のシンボルの共通型とし、例に `"scip_symbol": null` を書く（null のキーも出す形）。§11 T-02 の json の `symbols[]` は「SymbolRecord に `children[]`・`fold_lines`・`decorators[]`・`raises[]` … を加えたもの」。null・空のキーを省いてよいという規約は仕様書に無い（§6.1 は compact の規約）。`SymbolRecord.to_json()` も §6.2 の例にあるキーを null でも出す（例に無い `declared_in` だけ省く）。
  - BUG-0055 の前提「json でも compact と同程度の量」は仕様に無い。§5.7 は「同じ内容でも JSON は compact の数倍になるので、同じ `max_tokens` で入る件数は json のほうが少ない」と書き、入らないときは §5.6-4（0 件・`warnings` に必要な `max_tokens`）で返す。
- 直し方: `_symbol_json` を `1e1a9aa` の前の形に戻した。SymbolRecord のキーと、T-02 で加える `doc_summary`・`children`・`fold_lines`・`decorators`・`raises` を、値が null・空でも必ず出す（理由をコメントに書いた）。テストは緩めていない。
- 回帰テスト: `tests/unit/test_syntax_skeleton.py::test_json_symbols_keep_null_and_empty_keys`（Python・TypeScript の 2 件）。全記号（入れ子も、縮退版も）が §6.2 のキー + T-02 で加えるキーをすべて持ち、子の無い `Field` / `Property` が `children: []`・`decorators: []`・`raises: []` を持ち、`signature: null` の記号があることを確かめる。`1e1a9aa` の版では 2 件とも落ちることを確かめた。
- 確認（2026-10-31、作業ツリー = `b1ce8ac` + この直し。langs extra 同期済み・`bench/.cache` に httpx・ky あり）:
  - `uv run ruff check src tests` → All checks passed
  - `uv run pytest -q tests/unit/test_syntax_skeleton.py` → 25 passed（直す前は 6 failed）
  - `uv run pytest -q tests/contract_fixtures/test_fx_read_T00_T08.py tests/e2e/test_vertical_slice_stdio.py tests/e2e/test_m1_6_session.py` → 26 passed・3 skipped（skip は F-TS の exact 水準 3 件〈TypeScript の LSP が無い。BUG-0057〉で T-02 ではない。T-02 の事後条件 1・2・5 は F-PY・F-TS とも passed）
  - `tests/unit/test_mcp_*.py`・`tests/contract/test_T02_skeleton.py`・`tests/contract/test_T02_data_files.py`・`tests/contract/test_perf_reuse.py`・`tests/contract/test_compact_grammar.py`・`tests/contract/test_revision_reads.py`・`tests/contract/test_T10_delete.py`・骨格と adapter の単体 → 597 passed
- 戻ったもの（BUG-0055 の症状）: json の量は省く前に戻る。`src/lucianke/core/urn.py` の実測: json `max_tokens=50000` で 10,203 トークン（compact 3,449）、`max_tokens=3000` で縮退して 2,531（`truncated_members` 54）、既定の 2,000 では 0 件と `warnings`「no record fits in max_tokens=2000 … raise max_tokens to ~10203 or more」。§5.6-4・§5.7 どおりの応答。
- 師匠の判断が要ること（推奨つき）: json で null・空のキーを省いてトークンを減らしたいなら、仕様書 §6.2 の改訂（全ツール一律）として決めて、T-02・T-06・T-07 とテストを一緒に直す。推奨は「省かない（今の仕様のまま）」。json は形の安定を求める受け手のための形式で、トークンを節約する既定の形式は compact。
- 別の票: T-06・T-07 の json も BUG-0054（`0db3922`）で SymbolRecord の null・空のキーを省いている（BUG-0066）。T-02 の json で `max_tokens` を上げると返るファイル数が減る（BUG-0067。F-PY の契約テストが 6,000 で通るのはこの詰め方の結果でもある）。
