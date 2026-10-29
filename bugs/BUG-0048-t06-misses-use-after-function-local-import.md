# BUG-0048 T-06 が関数内 import で束縛した名前の使用（pytest.raises(LuciankeError)）を参照として返さない

- 報告: 2026-10-29 / 見た人: dogfood-2（実 stdio クライアント、使い捨て作業ツリー 42640ac〔HEAD〕、このリポジトリ自身を `set_roots`）
- 症状: `find_references_and_implementations {target: {path: "src/lucianke/core/errors.py", symbol_name: "LuciankeError"}, query_type: "references", include_declaration: true}` の参照に、`tests/unit/test_engine_indexer.py:239`（`    with pytest.raises(LuciankeError):`）が含まれない。同じファイルの 237 行（関数内の `    from lucianke.core.errors import LuciankeError`）は含まれる。
  - 実測: 返り 473 件（`lsp=unavailable`・`confidence=structural`）。`ast` で `src/`・`tests/` の `Name` の識別子 `LuciankeError` を集めた 473 件と比べ、`ast` にあって返りに無いのは `(tests/unit/test_engine_indexer.py, 239)` の 1 件のみ（返りにあって `ast` に無いのは宣言行 1 件）。239 行の `LuciankeError` は 237 行の関数内 import で束縛された名前の使用。
  - 同じ調査で `resolve_target`（5 件 vs `ast` 4 件＋宣言）、`confine`（72 件 vs 71 件＋宣言）は漏れ 0。
- 期待: 関数内 import で束縛した名前の使用も参照として返る（import 行が返るのだから、使用箇所も同じ対象に解決される）。
- 再現手順: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> self`（`bench/dogfood/out/obs2_findings_self.json`）。
- 調査はしていない（直す役の仕事）。
