# BUG-0003 syntax/skeleton.py の build_file_record が本物のファイルで落ちる（NameError とアクセス違反）

- 報告: 2026-10-29 14:05 / 見た人: 縦切りの結線の担当
- 症状:
  - `src/lucianke/core/urn.py` を `analyze_file` で解析した `ParsedFile` と `symbols.extract` の結果で `build_file_record` を呼ぶと、最初は `NameError: _one_line`（`_read_info` の中）。
  - 少しして `-X faulthandler` で Windows のアクセス違反（Segmentation fault、exit 139）が `_collect_raises`（skeleton.py の 241 行付近）で起きた。
  - この呼び出しを MCP サーバーの中で使うと、クライアント側は `Connection closed` になる。
- 期待: 仕様書 §11 T-02 の骨格が返る。
- 現状の回避: T-02 は `src/lucianke/tools/_skeleton_fallback.py` の最小実装で動いている（`decorators:`、`raises:`、`refs:` は未実装）。
- 再現手順: `PYTHONUTF8=1 uv run python -X faulthandler <スクリプト>`。スクリプトは「`src/lucianke/core/urn.py` を読み、`syntax.parsers` で解析し、`symbols.extract` の結果と `SkeletonOptions()` で `syntax.skeleton.build_file_record` を呼ぶ」だけ（担当のスクリプトは作業ツリーの外にあり回収していない）。
- 備考: 同じ時間帯に、別の役が Compact の行組み立てを `core/compact_lines.py` に移し、`skeleton.py` の複製を消している（d245475）。
- 調査はしていない（直す役の仕事）。
