# BUG-0008 limit を超えても next_cursor が出ない（パッカーが total_estimate を見ていない）

- 報告: 2026-10-29 14:20 / 見た人: T-03・T-04 の担当（14a）
- 見たこと:
  - `search_workspace_symbols`（T-03）で一致が `limit` を超えるとき、応答に `next_cursor` が出ない。`returned ≤ limit` と `total_estimate` は満たしている。
  - 担当の観察: パッカー（`src/lucianke/mcp/budget.py`）は `ToolOutput.total_estimate` を見ず、`len(records)` から残り（remaining）を決めている。T-03 は `limit` 件までしかレコードを渡さないので、残りが 0 に見える。
- 期待: 仕様書 §5.4（ページング）— 残りがあれば `next_cursor` を返す。
- 再現手順: `tests/contract/test_T03_T04.py` の F-POLY で、`limit` を一致件数より小さくして T-03 を呼ぶ。
- 調査はしていない（直す役の仕事）。

## 原因
- 担当の観察のとおり。`Packer.pack` は残りを `len(out.records)` だけで決め、`ToolOutput.total_estimate` はページの `total_estimate` に写すだけで「続き」に数えていなかった。T-03 は `offset + limit` 件までしかレコードを渡さない（`limit` で打ち切った遅延列。設計書 §7.2.2「records は遅延列でもよい。total_estimate は別途 COUNT で与える」）ので、渡された分が全部入ると「残り 0・カーソル無し」と判定された（手順 1 の「トレーラ無しで全部入るか」で即 return）。予算で切れたページだけカーソルが出ていたので、`max_tokens` を絞る既存テストでは気づかれなかった。
- ツール側（T-03）は正しかった: カーソルの `o` を `ctx.cursor_offset` で受け取り、`o + limit` 件を渡し直し、`total_estimate` に総数を入れている。カーソルの符号化（`CursorCodec`）も正しく、直しは要らない。

## 直し方
- `mcp/budget.py`（`Packer.pack`）: `unseen = max(0, total_estimate - len(records))`（`total_estimate` が `None` または `len(records)` 以下なら 0）を「具体化していない続き」として扱う。
  - `unseen > 0` なら「トレーラ無しで全部入る」の早期 return を通らず、貪欲詰めでは最後の 1 件にもトレーラの見込みを引く。全部入っても `next_cursor`（オフセット = 入った最後の次）と `# more: … remaining≈N`（N = 入らなかった分 + `unseen`）を出し、トレーラの分は予算（`used_tokens_est <= max_tokens`）に数える。
  - `Page.remaining` は `入らなかった分 + unseen`、`Page.total_estimate` は `len(records) + unseen`。`# summary:` は `default_summary(rest, unseen)`（`unseen` は group 不明なので `others` に数える。`rest` が空なら `N more`）。ツールの `summarize` は具体化した残りだけを受け取る。
  - `total_estimate` が無いときは従来どおり。
- 設計書 §7.2.2 に `total_estimate` の意味（先頭の接頭辞と総数）と次ページの取り方を追記。`ToolOutput.total_estimate` のコメントも同じに。
- 既存テスト `test_pack_total_estimate_from_tool_wins` は旧挙動（`total_estimate` を見ない）を固定していたので、`remaining == total_estimate - 入った件数` に直した。
- テスト: `tests/unit/test_mcp_budget_cursor.py`（新規 19 件: 全部入っても遅延列ならカーソルが出る・オフセットが予算の切れ目から再開する・`total_estimate` が `len(records)` 以下・無しは従来どおり・トレーラが予算に入る・`limit` 窓で辿ると重複も欠落も無い〈乱数 80 ケース〉）、`tests/contract/test_T03_T04.py::test_t03_over_limit_issues_next_cursor_and_pages_do_not_overlap`（`limit=2` から辿って全件が重複なく揃う・compact の `# more:`）。

## 確認
- 直す前: 新規テスト 19 件中 13 件が失敗（`next_cursor` が `None`、`remaining` が 0）。
- 直した後: `tests/unit/test_mcp_budget_cursor.py`・`test_mcp_budget.py`・`test_mcp_pipeline.py`・`test_mcp_render.py`・`test_import_graph.py`・`tests/contract/test_T03_T04.py` が green（234 件）。`uv run pytest -q tests/unit tests/contract` 全体は 2391 件 green・他の役の作業中の 7 件が赤（`test_lsp_features` の実機 basedpyright 2 件、`tests/contract/test_T08_diagnostics.py` 5 件。いずれもパッカーとは無関係で、未コミットの T-08 / LSP の作業）。
