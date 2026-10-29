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

## 原因
- `tools/t05_get_symbol_bodies.py` は結線の最初の縦切りで「コードのシンボルだけ」を前提に作られていた。`_resolve` は `cell:` などのセレクタ接頭辞を見ると一律に `E_UNSUPPORTED`（`selector_not_supported`）を返し、`.cob` や未知の拡張子は `analyze_at` -> `analyze_file` が文法の無い言語を `E_UNSUPPORTED`（`language_not_supported`）で断った。部品（`syntax/adapters/ipynb.py::select` と `syntax/heuristic.py::block_window` / `lexical_find`）は単体試験だけで、T-05 に結線されていなかった。
- 仕様書 T-05 の事後条件 3・4 を確かめる試験は `xfail(strict=True)` で名乗りだけ置かれていたため、`bench/postcondition_map.py` では「対応あり」と出ていた（BUG-0040 の突き合わせで発覚）。

## 直し方
- `t05_get_symbol_bodies.py` に「コード以外の対象」の節を足し、ハンドラが `_resolve` の前に `_non_code` を呼ぶ（`None` ならコードの経路。従来の挙動・BUG-0016 の続き・BUG-0018 の revision は変えない）。種類の振り分けは T-02（`_skeletons.file_skeleton`）と同じ順序で、`revision` の断面から `read_checked` で読む。
  - `cell:<n>`（`.ipynb`）: `ipynb.to_virtual_python` / `ipynb.select` で `cells[n].source` の連結だけを `code` に返す（`outputs` は読まない）。行は T-02 と同じ仮想 Python ファイルの行。コードセルはコードと同じ `Record.shrink` / `resume` で `next_cursor` から続きを取れる（`_record` の行の取り先を `_Body` に一般化。`code` は全部返すとき原文のまま）。Markdown / raw セルは仮想ファイルの行を占めないので全文を 1 レコードで返し、行番号は付けない（`cell.in_virtual_file: false`）。`cell:0`・構文の誤りは `E_INVALID_ARGUMENT`、範囲外は `E_TARGET_NOT_FOUND`、`.ipynb` 以外への `cell:` は `E_UNSUPPORTED`（`selector_not_applicable`）、壊れた JSON は `E_UNSUPPORTED`（`invalid_data_file`）。エンジンは `parser:ipynb`、Tier 2。
  - Tier 4（文法を結線していない言語・未知の拡張子。データファイルと Markdown は従来どおり断る）: `lexical_find`（単語境界。定義らしい一致が先）の一致行を `block_window` で前後 15 行（最大 31 行）の窓にし、`windows[]` に最大 3 つ。先に採った窓に入る一致は別の窓にしない。水準は `lexical_fallback`、エンジンは `internal_ripgrep_word_boundary`、Tier 4。`! DEGRADED`・`is_degraded` はパイプラインがレコードの水準から付ける（ツールは付けない）。`path` + `line` と、T-03・T-06 が返す `lex:NAME@Ln` / `ctags:NAME@Ln` の URN は、その行を中心とする 1 窓。一致が無いと `E_TARGET_NOT_FOUND`。
  - 仕様書の `heuristic`（ctags / ブロック推定の終了行まで）の枝は結線していない（契約試験が COBOL の `COMPUTE-TAX` に `lexical_fallback` を期待しているため）。
- 試験: `tests/contract/test_T05_bodies.py` の事後条件 3・4 の `xfail` を外した。足した試験: セルの誤り・Markdown セル・コードセルの仮想行・大きいセルの続き（全ページの連結が `source` と一致）、窓の compact（`! DEGRADED`）・`line`・`lex:` URN・最大 3 窓で重ならないこと。

## 確認
- `tests/contract/test_T05_bodies.py`（42 件）・`test_revision_reads.py`・`tests/unit/test_import_graph.py` を HEAD に自分の差分だけを載せた使い捨ての作業ツリーで 3 回続けて green（70 件）。`ruff check` も通る。
- `python -c` でパイプライン経由に `cell:1`（Markdown）・`cell:3`・`cell:4`、`legacy/calc.cob` の `COMPUTE-TAX`・`line: 10`・`lex:COMPUTE-TAX@L9` を呼び、compact が `tests/contract/compact_grammar.py` の検証を通ることと、`! DEGRADED: confidence=lexical_fallback engine=internal_ripgrep_word_boundary lang=cobol` が付くことを確認した。
- 範囲外で見たこと: `xpath:` / `jsonpath:` / `jq:` / `toml:` / `yaml:` は今も `E_UNSUPPORTED`（`selector_not_supported`）。BUG-0056 に起票。
- コミット: d9f53cb
