# BUG-0066 T-06・T-07 の json で SymbolRecord の null・空のキーが消える（§6.2 の共通型と形が違う）

- 報告: 2026-10-31 / 見た人: BUG-0065 を直した役（T-02 の同じ形の直しのときにコードを読んだ）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 症状（コードで見たこと）

- `src/lucianke/tools/t06_find_references_and_implementations.py`（`0db3922` BUG-0054 の変更）: `data.target` と `data.implementations[]` を
  `{k: v for k, v in sym.to_json().items() if v is not None and v != []}` で作っている（関数 `_compact`。コメント「BUG-0054: drop null/empty fields from SymbolRecord json …」）。
- `src/lucianke/tools/t07_query_call_hierarchy.py`（同じコミット）: `data.root` を同じ式で作っている（`_root_json`）。
- この式では、値が null の `scip_symbol`・`selection_range`・`signature`・`return_type`・`doc_summary` と、空の `modifiers` のキーが json に出ない。

## 仕様との違い（見たこと）

- 仕様書 §6.2: 「`data` 内のシンボル表現は次の SymbolRecord を共通型とする」。例に `"scip_symbol": null` を書いている（null のキーも出す形）。null・空のキーを省いてよいという規約は仕様書に見当たらない。
- 同じ省き方をしていた T-02（`1e1a9aa` BUG-0055）は、`s["children"]` で辿るテストが落ちて、BUG-0065 で省かない形に戻した。T-06・T-07 は、キーがあることを確かめるテストが無いので落ちていない（全体テストは T-02 の 7 件以外 green だった）。

## 手がかり

- BUG-0065 の票の「師匠の判断が要ること」: json で null・空のキーを省くなら仕様書 §6.2 の改訂（全ツール一律）として決める。推奨は「省かない」。この票はその判断に従って、T-06・T-07 を T-02 にそろえるか、全ツールを省く形にそろえるかのどちらか。

## 原因・直し方・確認（2026-10-31）

- 原因: `0db3922`（BUG-0054）が、json の固定部分を最小の `max_tokens=200` に収めるために、T-06 の `data.target`・`data.implementations[]`（`_compact`）と T-07 の `data.root`（`_root_json`）で `SymbolRecord.to_json()` の値が null・空のキーを捨てた。票の見たとおり、`scip_symbol`・`selection_range`・`signature`・`return_type`・`doc_summary` が null のとき、`modifiers` が空のときにキーが消えていた。キーがあることを確かめるテストが無かったので、全体テストは落ちなかった。
- どちらが正しいか: 実装（`0db3922`）が仕様に合っていない。仕様書 §6.2 は SymbolRecord を `data` 内のシンボルの共通型とし、例に `"scip_symbol": null` を書く（null のキーも出す形）。§11 T-06 の `target`・`implementations[]`、T-07 の `root` の型は「SymbolRecord」。null・空のキーを省いてよいという規約は仕様書に無い。T-02 は BUG-0065（`e021064`）で同じ形に戻した。
- 省く理由はもう無い: BUG-0063 で固定の 200 の下限をやめ、前置きだけで予算を超えるときは 0 件と `warnings`（必要な `max_tokens`）で返す（BUG-0064。§5.6-4）。json の量が数トークン増えても、その応答の形で受け手は呼び直せる。
- 直し方: T-06・T-07 とも `SymbolRecord.to_json()` をそのまま使う形に戻した（`_compact` と `_root_json` を消した。理由をコメントに書いた）。BUG-0054 の変更は `0db3922` ではこの 2 箇所だけで、ほかに null・空を捨てる箇所は T-06・T-07 に無い（`type_hierarchy` の `supertypes[]`・`subtypes[]` は元から `to_json()` そのまま）。テストは緩めていない。
- 回帰テスト: `tests/contract/test_T06_T07.py::test_t06_t07_json_symbol_records_keep_null_and_empty_keys`。一時のワークスペース（`Base.run` と、それを上書きする `Impl.run`）を索引して、T-06 の `data.target`（メソッドとクラス）・`data.implementations[]`（`Impl.run`）と T-07 の `data.root` が §6.2 のキー（`urn`・`scip_symbol`・`path`・`name`・`qualified_name`・`kind`・`modifiers`・`range`・`selection_range`・`signature`・`return_type`・`doc_summary`・`confidence_level`。ほかは `declared_in` だけ許す）をすべて持ち、`scip_symbol: null`・`return_type: null`・`doc_summary: null` のキーがあることを確かめる。直す前のソースでは落ちる（`scip_symbol`・`return_type`・`doc_summary` が無い）ことを確かめた。
- 確認（2026-10-31、作業ツリー = `c149e0d` + この直し）:
  - `uv run ruff check src tests` → All checks passed
  - `uv run pytest -q tests/contract/test_T06_T07.py -k keep_null` → 直す前 1 failed、直した後 1 passed
  - `uv run pytest -q tests/unit/test_mcp_*.py tests/contract_fixtures/test_fx_read_T00_T08.py tests/contract/test_T06_T07*.py tests/contract/test_T06_ts_outside_tsconfig.py tests/contract/test_T07_builtins_hidden.py tests/contract/test_T07_ts_outside_tsconfig.py` → 374 passed・9 skipped（skip は TypeScript の LSP が無い 8 件と F-TS〈ky〉の未取得 1 件。環境由来）
  - `PYTHONUTF8=1 uv run pytest bench/_budget_probe2.py -s -q` → 2 passed。json の `max_tokens=200` は 0 件と `raise max_tokens to ~N`、N で呼び直すと収まる。N は T-07 365 → **370**、T-06 384 → **389**（キーが戻った分だけ増えた）。dot・mermaid の値は変わらない。
- 戻ったもの（BUG-0054 の症状）: json の前置きが SymbolRecord 1 件あたり数トークン増える（上の N で T-07・T-06 とも +5）。§5.6-4・§5.7 どおりの応答。トークンを節約する既定の形式は compact。
