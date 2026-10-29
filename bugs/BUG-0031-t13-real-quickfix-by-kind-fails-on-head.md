# BUG-0031 test_real_quickfix_by_kind_is_ambiguous_and_lists_candidate_ids が HEAD でも落ちる

- 報告: 2026-10-29 15:03 / 見た人: BUG-0016 を直す役（HEAD に自分の変更だけを載せた隔離ワークツリーで、周辺の契約テストを回したとき）
- 見たこと:
  - `tests/contract/test_T13_code_action.py::test_real_quickfix_by_kind_is_ambiguous_and_lists_candidate_ids`（実の ruff）が落ちた。
  - BUG-0016 の変更を載せない HEAD でも同じく落ちた。
  - 失敗の出力は報告に無い。
- 再現手順: HEAD の使い捨て作業ツリーで `.venv/Scripts/python.exe -m pytest -q tests/contract/test_T13_code_action.py -k quickfix_by_kind`。
- 調査はしていない（直す役の仕事）。
