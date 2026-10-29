# BUG-0050 tree-sitter の node を保持したまま check_fragment を呼ぶと、1 回だけアクセス違反（Windows）で落ちた

- 報告: 2026-10-29 15:35 / 見た人: BUG-0045 を直す役（使い捨ての検証スクリプトで）
- 見たこと:
  - tree-sitter の node（構文木のノード）を変数に保持したまま `edit/syntax_check.py` の `check_fragment` を呼ぶ使い捨てのスクリプトで、Windows のアクセス違反（segfault）でプロセスが落ちた。1 回だけ。
  - 同じ形を簡略にしたものは 3 回とも通った。最小の再現は作れていない。
  - 参考: BUG-0003 は py-tree-sitter 0.26.0 の `Point.row` / `.column` が int の参照を余計に手放す不具合だった（c298734 で `point[0]` に替えて回避）。
- 再現手順: 未確定。
- 調査はしていない（直す役の仕事）。
