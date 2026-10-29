# BUG-0049 get_symbol_bodies が `cell:<n>`（.ipynb）と `lexical_fallback` の窓を返せない（E_UNSUPPORTED）。仕様書 T-05 の事後条件 3・4 をツール経由で確かめられない

- 報告: 2026-10-29 / 見た人: BUG-0040 を直す役（事後条件の対応表の突き合わせ）/ 見たことだけ（調査・修正はしていない）
- 見たこと（F-POLY `tests/fixtures/poly` をルートに、パイプライン経由で `get_symbol_bodies` を `format: json` で呼んだ）:
  - `targets: [{"path": "notebooks/eda.ipynb", "symbol_name": "cell:3"}]` -> `isError`、`error.code = E_UNSUPPORTED`、message `selector cell: is not supported in this build (code symbols only)`（`details.reason = selector_not_supported`）。
  - `targets: [{"path": "legacy/calc.cob", "symbol_name": "CALC"}]` -> `E_UNSUPPORTED`、message `legacy/calc.cob: language 'cobol' has no structural support in this build (...)`。`conf/rules.custom`（未知の拡張子）も同じ。窓（`windows[]`）も `! DEGRADED` も返らない。
  - 部品は在る: `src/lucianke/syntax/adapters/ipynb.py::select`（`cell:<n>`、単体試験 `tests/unit/test_syntax_ipynb.py`）と `src/lucianke/syntax/heuristic.py::block_window`（最大 31 行、`tests/unit/test_syntax_heuristic.py`）。`src/lucianke/tools/t05_get_symbol_bodies.py` の `_resolve` が `_SELECTOR_PREFIXES`（`cell:` を含む）を見ると `E_UNSUPPORTED` を返す。
- 仕様書: §11 T-05 処理規約 3〜5（セレクタ、`cell:<n>` は `cells[n].source` の連結だけ、`lexical_fallback` は前後 15 行〈最大 31 行〉の窓を `windows[]` に最大 3 つ。先頭に `! DEGRADED`）、事後条件 3（`cell:<n>` の `code` は `cells[n].source` の連結と一致し `outputs` 内の文字列を含まない）・4（`lexical_fallback` の各窓は `end_line - start_line + 1 ≤ 31` で応答は `is_degraded: true`）。マイルストーンは M1。
- 影響: M1 の 14 ツールの事後条件のうち T-05 の 3・4 は、ツール経由では満たされていない（`bench/postcondition_map.py` の対応表では、下の xfail の試験が名乗っているため「対応あり」と出る。実際は未実装）。
- 再現: `.venv/Scripts/python.exe -m pytest tests/contract/test_T05_bodies.py -k "t05_postcondition_3 or t05_postcondition_4" -rx`（xfail が 2 件）。
- 直す役への手がかり: `tests/contract/test_T05_bodies.py` の 2 件が仕様書どおりの本物のアサーションを持ち、`@pytest.mark.xfail(strict=True, reason="BUG-0049 ...")` が付いている。直したら XPASS（strict なので赤）になるので、xfail の印を外すこと。
