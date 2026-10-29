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

## 原因
- **NameError（`_one_line`）**: d245475 が `skeleton.py` の `_one_line` を消して `core/compact_lines.one_line` に移した。その作業の途中（定義を消し、`_read_info` の呼び出し側をまだ `one_line` に直していない間）の作業ツリーを、縦切りの担当が実行した。d245475 のコミット自体では `_read_info` も `one_line` に直っており、HEAD に `_one_line` の参照は残っていない（`grep` で確認）。コミット後は再現しない一過性のもの。
- **アクセス違反（`_collect_raises`、237 行）**: **py-tree-sitter 0.26.0 の `Point.row` / `Point.column`（`Node.start_point.row` など）の属性ゲッターが、タプルの持つ int への参照を 1 つ余計に手放す**（借用参照を返している）。256 以下の int は不滅（Python 3.12）なので無害だが、行・列が 256 を超えると int が早く解放され、ヒープが壊れて、あとの木の走査（`named_children` など）でアクセス違反になる。`skeleton.py` が `name.start_point.row`・`body.start_point.row` を読んでいた（`compact_lines.py` は 276 行目付近の関数があるため再現した。`urn.py` なども行が 256 を超える）。`parse_source` の `read_callback`（S-02 の `progress_callback` とは別物）・木の持ち方（Tree / Parser / Language の寿命）は無関係と確かめた（Parser・Language を保持しても、bytes で解析しても同じ）。
  - 最小再現（Lucianke 無し）: 400 行のソースを解析し、`n = tree.root_node.named_children[-1]; p = n.start_point; v = p.row; del v` のあと `sys.getrefcount(p[0])` が 1 に落ちる（正しくは 2）。続けて木を走査すると落ちる。
  - 同じ形のコードが `syntax/symbols.py`（`_doc_comment` の `first_row` / `prev.end_point.row`、562 行付近）にもあった。

## 直し方
- `Node.start_point` / `end_point` の行・列は **`point[0]` / `point[1]`**（タプル添字。安全）で読む。`skeleton.py` の 4 か所と `symbols.py` の 2 か所を直した（c298734）。`skeleton.py` に理由のコメントを足した。
- 再発防止: `tests/unit/test_syntax_skeleton_realfiles.py` に `src/` の `(start|end)_point.(row|column)` の属性読みを禁じる検査を入れた。
- T-02 の呼び先を `build_file_record`（`syntax.skeleton`）に差し替え、`tools/_skeleton_fallback.py` を削除した。`decorators:`・`raises:` が出る。折りたたみ行数は仕様どおり「範囲 − デコレータ行 − シグネチャ行」になり、`process_charge`（デコレータあり）は 14 → 13（契約テストの期待を 13 に直した）。

## 確認
- `tests/unit/test_syntax_skeleton_realfiles.py`: `src/` の全 `.py` を子プロセス（`-X faulthandler`）で `parse_source` → `symbols.extract` → `build_file_record`（既定・urns=all・public のみ・縮退 3 段）に通し、終了コード 0、標準エラー空、全行が `compact_grammar.validate` に適合。直す前は `compact_lines.py` で終了コード 0xC0000005。
- `tests/contract/test_T02_skeleton.py`（20）・`tests/e2e/test_vertical_slice_stdio.py`・`tests/unit/test_syntax_skeleton.py`・`test_syntax_symbols.py`・`test_import_graph.py` が green。
