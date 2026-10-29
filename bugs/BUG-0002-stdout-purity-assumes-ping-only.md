# BUG-0002 stdout 純度の不変条件テストが、ツールの追加で落ちる

- 報告: 2026-10-29 14:00 / 見た人: グラフ出力の担当、骨格の担当（全体 pytest を実行した）
- 症状: `tests/invariants/test_stdout_purity.py` の 2 件が落ちる。サーバーに `get_file_skeleton` が登録され、`tools/list` の結果が `["ping"]` だけではなくなったため（骨格の担当の観察）。
- 再現手順: 縦切りの結線（T-02 の登録）が入った作業ツリーで `uv run pytest -q tests/invariants/test_stdout_purity.py`。
- 調査はしていない（直す役の仕事）。
