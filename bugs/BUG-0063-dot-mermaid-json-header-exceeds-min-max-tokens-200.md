# BUG-0063 dot/mermaid/json: ヘッダ(accuracy_warning + head_kv + fixed part)だけで min max_tokens=200 を超える

- 報告: 2026-10-30 / 見た人: BUG-0053・BUG-0054 を直した役
- 症状: BUG-0053・BUG-0054 の修正後も、max_tokens=200 の dot/mermaid/json 応答で `used_tokens_est > 200` になるケースが残る。
  前置き（ヘッダ行・DEGRADED 行・accuracy_warning・head_kv）だけで 200 を超えるためで、グラフを空にしても・レコードを 0 件にしても解消しない。
- 実測（`tests/fixtures/poly`、`PaymentProcessor.process_charge`、`direction=both`、`depth=5`、`max_tokens=200`）:
  - `format: "dot"`: 226 トークン（うち accuracy_warning 行が約 80 トークン、head_kv の root 行が約 30 トークン）
  - `format: "json"`: 365 トークン（うち `_resolution_meta.accuracy_warning` が 113 トークン、`data.root` が 133 トークン）
  - `format: "mermaid"`: 同様（計測省略）
- 再現条件: LSP なし（structural）で accuracy_warning が長い日本語文字列になるとき、かつ max_tokens=200 の最小予算
- 関連: BUG-0053（解消：グラフが max_tokens に応じて切り詰められるようになった）、BUG-0054（部分解消：null/空フィールド省略）
- 残る問題の構造:
  - dot/mermaid の accuracy_warning（`ACCURACY_WARNING` in `t07_query_call_hierarchy.py`）約 80 トークン
  - json の `_resolution_meta.accuracy_warning` 約 113 トークン、`data.root`（SymbolRecord.to_json）約 133 トークン
  - 仕様書 §5.7「used_tokens_est <= max_tokens」vs §5.2「max_tokens の最小は 200」の矛盾
- 調査はしていない（直す役の仕事）
