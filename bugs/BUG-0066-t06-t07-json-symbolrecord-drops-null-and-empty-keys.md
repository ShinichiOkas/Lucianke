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
