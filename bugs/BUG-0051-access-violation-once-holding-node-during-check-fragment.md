# BUG-0051 tree-sitter の node を保持したまま check_fragment を呼ぶと、1 回だけアクセス違反（Windows）で落ちた

- 報告: 2026-10-29 15:35 / 見た人: BUG-0045 を直す役（使い捨ての検証スクリプトで）
- 見たこと:
  - tree-sitter の node（構文木のノード）を変数に保持したまま `edit/syntax_check.py` の `check_fragment` を呼ぶ使い捨てのスクリプトで、Windows のアクセス違反（segfault）でプロセスが落ちた。1 回だけ。
  - 同じ形を簡略にしたものは 3 回とも通った。最小の再現は作れていない。
  - 参考: BUG-0003 は py-tree-sitter 0.26.0 の `Point.row` / `.column` が int の参照を余計に手放す不具合だった（c298734 で `point[0]` に替えて回避）。
- 再現手順: 未確定。
- 調査はしていない（直す役の仕事）。

## 原因
- **`check_fragment` も `Point` も無関係。py-tree-sitter 0.26.0 の `Node.text` が、`read_callback` で解析した木に対して解放後の領域を読む。** Lucianke の解析は常に `read_callback`（`syntax.parsers._parse_with_deadline`）なので、`Node.text` を呼ぶ処理（`syntax/boundaries.py`・`syntax/imports.py`・`syntax/relations.py`・`tools/_t11_scope.py`）はどれも当たりうる。
- 見つけ方（15 分の調査）: 木の API を 1 つずつ数千回ずつ当てた（`start_point[0]`・`[1]`・`end_point`・`children`・`named_children`・`type`・`parent`・`next_sibling`・`start_byte`・`end_byte`・`range`・`child_by_field_name` など 28 種）。単独ではどれも落ちない（対照: `start_point.row` だけが BUG-0003 どおり落ちる）。`check_fragment` を呼ばなくても、保持した node の `text` を読みつつ別の木の `children` を取り直し続けるだけで 10 回前後の反復で落ちる。
- 最小の組み合わせ（Lucianke 無し・`tree_sitter` と `tree_sitter_python` だけ）: 3 本の Python ファイルを `parser.parse(read)`（`read(offset, point)` が毎回 `data[offset:offset+16384]` を新しく返す）で解析 → 反復ごとに木の全 node を `children` で集め直し、先頭から 1/N の 300 個の `n.text` を読む。300 反復を 10 回走らせて **9/10 で落ちる**。同じ木を `parser.parse(data)`（`bytes`）で解析すると 0/5。
- コールバックの戻り値のつなぎとめ方だけを変えた比較（各 10 回・300 反復）: 毎回新しい 16 KiB を返す = **9/10 で落ちる** / 直近 1 個を保持 = 0/10 / 直近 4 個を保持 = 0/10 / オフセットごとに保持 = 0/10 / 300 バイトの小さい塊を返す = 0/5（pymalloc の領域は OS に返らないので落ちない）。よって `Node.text` が戻り値の `bytes` を解放してから読んでいる（解放後の読み取り）。大きい塊は解放で OS に返ることがあり、木の走査で確保・解放が続くと、あるとき落ちる。報告の「1 回だけ・簡略にした形は 3 回通る」と合う。
- 上流（py-tree-sitter）への報告は師匠の判断なので、していない。上の最小の組み合わせが報告の材料になる。

## 直し方
- `syntax/parsers.py` の `read`（`_parse_with_deadline`）が、直近に返した塊を `last` に保持する（木はこの関数を保持するので、木と同じ寿命で最大 16 KiB 残る）。`Node.text` の呼び出し側は 1 行も変えない（`imports.py`・`relations.py` は他の役が編集中だった）。
- 将来 `Node.text` をやめる場合: 呼び手は `parsed.source[node.start_byte:node.end_byte]` で同じ値が取れる（`skeleton._text` が既にそうしている）。いまは塊の保持で足りる。

## 確認
- 再現の台本: `tests/unit/test_syntax_ts_refcount_stress.py`（子プロセス + `-X faulthandler`）。`parse_source` で解析した 3 つの実ファイルの node を保持 → `check_fragment`（Python・TypeScript・Rust の正常・壊れた断片 6 種）→ 保持した node の `start_point`・`end_point`・`text`・`children`・`named_children`・`type`・`parent`・`next_sibling` を読む → 同じ Parser で別の木を作って走査、を反復する。`gc.collect()` を挟む版と挟まない版。既定 60 反復（約 7 秒）、`LUCIANKE_STRESS=1` で 3,000 反復。
- 直す前: 2 版とも 10 回前後の反復で終了コード 0xC0000005（`n.text` の行で落ちる）。
- 直したあと: 既定の 2 版が green を 3 回続けて確認。600 反復（gc あり）も完走。`tests/unit/test_syntax_parsers.py`・`test_edit_syntax_check.py` も green。
- 備考: `start_point.row` / `.column` の属性読みが `src/` に残っていないことは、BUG-0003 の `test_src_never_reads_point_row_or_column_attributes` が検査している（`yaml_.py` の `e.column` は PyYAML の `Mark` で別物）。
