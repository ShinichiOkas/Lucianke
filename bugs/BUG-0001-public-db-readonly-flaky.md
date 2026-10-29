# BUG-0001 公開 DB の読み取り専用テストが全体実行のときだけ落ちる（揺れ）

- 報告: 2026-10-29 14:00 / 見た人: シンボル抽出の担当（全体 pytest を実行した）
- 症状: `uv run pytest -q` の全体実行で `tests/invariants/test_public_db_readonly.py` が 5 件落ちる。
  - delete-multi_drop、snapshot の delete・wal、authorizer-pragma_journal_delete、wal-multi_drop
- 対照: `uv run pytest -q tests/invariants/test_public_db_readonly.py` の単独実行では 190 passed。
- 再現手順: リポジトリ直下で全体実行 → 上の 5 件が落ちる。単独実行 → 落ちない。
- 調査はしていない（直す役の仕事）。
