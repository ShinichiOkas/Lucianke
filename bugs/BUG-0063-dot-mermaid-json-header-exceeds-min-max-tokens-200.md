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

## 原因・直し方・確認（2026-10-31）

- 原因: 仕様 §5.2 の固定下限 200 が、前置きの量（accuracy_warning・`head_kv`・json の封筒）と無関係に決め打ちだった。前置きが 200 を超える呼び出しでは、どう詰めても §5.7 を満たせない。
- 直し方（師匠の判断 2026-10-30・(A)）: 固定下限を廃止し、下限は呼び出しごとの実測値にした。`max_tokens` は正の整数ならそのまま受け付け（`tools/_base.py` の `gt=0`・スキーマの `exclusiveMinimum: 0`）、前置きすら入らなければ出口が必要な `max_tokens` を `warnings` で返す（json の 0 件経路の穴は BUG-0064）。`core/limits.py` の `MIN_MAX_TOKENS` は `REGISTRY_MIN_DEFAULT_MAX_TOKENS`（登録時の床 1）と `BUDGET_FLOOR_FOR_PAGING`（テスト用 200）に分けた。仕様書 §5.2・設計書を更新。
- 確認: `bench/_budget_probe2.py` 2 件 green（dot・mermaid・json とも `is_error=False`。予算 600 の dot・mermaid は収まる）。単体 `tests/unit/test_mcp_pipeline.py` の非正・小さい正値・スキーマの検査を更新。
