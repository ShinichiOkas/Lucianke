# BUG-0005 仕様書 §6.1 の kind 語彙に File があるのに core.model.Kind に無い

- 報告: 2026-10-29 14:13 / 見た人: core 移設の担当（全体テスト実行時）、親（再現）
- 症状: `uv run pytest -q tests/unit/test_core_model.py` で `test_kind_vocabulary_matches_spec_document` が落ちる。
  - `AssertionError: ... Extra items in the left set: 'File'`（仕様書の `kind = ...;` 規則に `"File"` があり、`Kind` の値に無い）
- 併せて見たこと: 同じファイルの `test_kind_parse_is_case_insensitive` は `Kind.parse("File")` が `ValueError` になることを期待している（133 行）。
- 再現手順: `uv run pytest -q tests/unit/test_core_model.py` → 1 failed, 42 passed。
- 調査はしていない（直す役の仕事）。
