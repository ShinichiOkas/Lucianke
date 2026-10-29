# BUG-0009 test_outline_uses_ctags_through_a_child_process が並列実行で 2〜3 割落ちる

- 報告: 2026-10-29 14:21 / 見た人: BUG-0001 の担当（並列での全体実行中）
- 見たこと:
  - `tests/unit/test_syntax_heuristic.py::test_outline_uses_ctags_through_a_child_process` が、並列実行（同時に複数の pytest）で 2〜3 割の確率で落ちる。
  - 担当の観察: テストは `tempfile.gettempdir()` 直下の `lucianke-ctags-*` を数えている。同時に走る別のプロセスの一時物を拾っている疑い。
- 再現手順: `uv run pytest -q tests/unit/test_syntax_heuristic.py` を 6 本同時に走らせる。
- 調査はしていない（直す役の仕事）。
