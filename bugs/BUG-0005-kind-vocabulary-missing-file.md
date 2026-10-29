# BUG-0005 仕様書 §6.1 の kind 語彙に File があるのに core.model.Kind に無い

- 報告: 2026-10-29 14:13 / 見た人: core 移設の担当（全体テスト実行時）、親（再現）
- 症状: `uv run pytest -q tests/unit/test_core_model.py` で `test_kind_vocabulary_matches_spec_document` が落ちる。
  - `AssertionError: ... Extra items in the left set: 'File'`（仕様書の `kind = ...;` 規則に `"File"` があり、`Kind` の値に無い）
- 併せて見たこと: 同じファイルの `test_kind_parse_is_case_insensitive` は `Kind.parse("File")` が `ValueError` になることを期待している（133 行）。
- 再現手順: `uv run pytest -q tests/unit/test_core_model.py` → 1 failed, 42 passed。
- 調査はしていない（直す役の仕事）。

## 原因
- 仕様書 §6.1 の `kind` 規則へ `"File"` を足したのは意図した改版（コミット 2fbb15b「スパイクの結論を仕様書に反映」、§6.1 の File）。同じコミットで §6.1 の規則 7（`File` は T-18 の Tier 3 / Tier 4 のファイルの変更要素 `modified_symbols[].kind` に使う応答上の要素で、`symbols` テーブルには格納しない）と、§6.2 の `symbols.kind` の行（「ただし `File` は応答上の要素で、`symbols` には格納しない」）も足されている。T-18 の処理規約 4 と `lexical_fallback` の行（ファイル単位・`kind: "File"`）がそれを使う。設計書の `Kind` の節（§5、422 行）も「File を含む」と書いてある。仕様書が正で、`core/model.py` の `Kind` と、それを写したテストの期待値が改版に追従していなかった（`Kind.parse("File")` が `ValueError` になることをテストが期待していた）。
- 仕様書側が誤りだとする証拠は無い。

## 直し方
- `Kind.FILE = "File"` を足した（`src/lucianke/core/model.py`。docstring に「応答上の要素で `symbols` には格納しない」を追記）。`Kind.parse("file")`（T-03 の小文字形）も通る。
- `tests/unit/test_core_model.py`: `SPEC_KINDS` に `File` を足し、`len(Kind) == 26` に。`test_kind_parse_is_case_insensitive` は `Kind.parse("File")` / `"file"` が `Kind.FILE` になること、未知の語（`"Klass"`）が `ValueError` になることに直した。
- `tests/contract/compact_grammar.py`（Compact 検査器の独自の `KINDS`）にも `File` を足した（仕様書の文法どおりに `[File]` の見出し行を通すため）。
- `Kind` を使う側への影響を確かめた: `core/compact_lines.py` の `KINDS`（`Kind` から導出）は自動で `File` を含む。`index/queries.py` の `Kind.parse`・`_kind_clause` と T-03 の `symbol_kinds`（仕様書 §5 は「§6.1 の kind の小文字形」）は `file` を受け付けるが、`symbols` に `File` の行は無いので 0 件になるだけ。`syntax/*` と `index/ingest.py` は個別の `Kind.X` しか使わず影響なし。`index/schema.sql` の `symbols.kind` に CHECK 制約は無い。

## 確認
- `uv run pytest -q tests/unit/test_core_model.py`: 全件 green（以前は `test_kind_vocabulary_matches_spec_document` が 1 failed）。
- `tests/unit/test_core_compact_lines.py`・`tests/contract/test_compact_grammar.py`・`test_T02_skeleton.py`・`test_T05_bodies.py` も green。
