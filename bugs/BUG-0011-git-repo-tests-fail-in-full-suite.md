# BUG-0011 全体実行のとき tests/unit/test_git_repo.py が実行ごとに別のテストで落ちる

- 報告: 2026-10-29 14:31 / 見た人: BUG-0005 の担当（`uv run pytest -q tests/unit` の全体実行中）
- 見たこと:
  - 最初の全体実行（2060 passed / 27 failed）で、`tests/unit/test_git_repo.py` のテストが落ちた。落ちるテストは実行ごとに違った。
  - 同じファイルを単独で実行すると 132 passed。
  - 全体実行は、同じ作業ツリーで 20 本ほどの役が並行して作業・テストしている最中だった。
- 再現手順: `uv run pytest -q tests/unit` を、別のプロセスで同時に pytest を走らせながら数回。
- 調査はしていない（直す役の仕事）。
