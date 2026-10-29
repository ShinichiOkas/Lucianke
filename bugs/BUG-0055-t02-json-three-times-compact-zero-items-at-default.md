# BUG-0055 get_file_skeleton の json が compact の約 3 倍で、既定の max_tokens（2000）では中規模のファイルが 1 件も入らない

- 報告: 2026-10-29 / 見た人: BUG-0014・BUG-0017 を仕上げた役（BUG-0030 の役の観察「json で urn.py 1 件が約 9,009 トークン、compact は約 1,000」の確認）
- 症状: 期待は json でも compact と同程度の量で骨格が返ること（仕様書 §5.7 は応答テキスト全体を数える）。実際は json の `data` が骨格の全項目を平らに持ち、同じファイルで compact の約 3 倍のトークンになる。既定の `max_tokens`（T-02 は 2000）では json が 0 件になる。これは数え方の誤りではない（BUG-0017 の直しのあと、`used_tokens_est` は JSON 本文を数え直した値に一致している）。
- 再現手順（`src/lucianke/core/urn.py`、387 行・骨格 147 行、`get_file_skeleton {"paths":["src/lucianke/core/urn.py"]}`）:
  - `max_tokens: 50000`: compact は本文 8,548 文字・2,948 トークン（ヘッダ `~2948/50000tok`）、json は本文 34,294 文字・9,227 トークン（`budget.used_tokens_est` 9,227）。文字数で 4.0 倍、トークンで 3.1 倍。直す前の作業ツリー（`main`）では json の `used_tokens_est` が 2,739 で本文は 9,129（BUG-0017）。
  - json の `data.items[0].symbols` が 36,306 文字（42 個の最上位 + children）。1 シンボルあたり `urn`・`scip_symbol`（null）・`path`・`name`・`qualified_name`・`kind`・`modifiers`・`range{start_line,end_line}`・`selection_range{line,start_col,end_col}`・`signature`（null）・`return_type`（null）・`doc_summary`・`confidence_level`・`children`（[]）・`fold_lines`（0）・`decorators`（[]）・`raises`（[]）を全部持つ。null と空配列も省かない。
  - 既定の `max_tokens`（2000）: compact は 1,545 トークンで返る（`- truncated: 0 members omitted (doc, urn omitted)`）。json は 0 件で、警告 `no record fits in max_tokens=2000: the next record needs ~9387 tokens; raise max_tokens to ~9511 or more`。
  - `max_tokens: 3000`: json は 17 個の最上位シンボル・`truncated_members` 54・2,365 トークン（json の縮退の最小段）。compact の最小段（1,545）より大きい。
- 頻度: 骨格の json 呼び出しの全部。`tests/contract/test_perf_reuse.py` など、json の既定の予算で PAY_PY（`tests/fixtures/poly/src/shop/services/payment.py`）を呼ぶテストが 0 件の応答になっていた（`max_tokens: 20000` に上げた）。
- 備考: T-02 の縮退（`Record.shrink`）は compact の行で段を選ぶ。json の `data` は段が同じでも compact の 3 倍あるので、`Packer._shrink` が `ask` を二分探索して収まる段を探しているが、段の最小でも json が既定の予算を超える。
- 調査はしていない（直す役の仕事）。
