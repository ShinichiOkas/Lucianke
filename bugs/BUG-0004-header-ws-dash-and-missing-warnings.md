# BUG-0004 応答ヘッダが ws=- のまま、バッチの失敗件数が warnings[] に出ない

- 報告: 2026-10-29 14:05 / 見た人: 縦切りの結線の担当、親（実機の stdio クライアントで確認）
- 症状 1: `get_file_skeleton` / `get_symbol_bodies` を `workspace_id` 省略で呼ぶと、compact のヘッダが `ws=-` になる（例: `# get_symbol_bodies ok conf=structural lsp=n/a rev=WORKING_TREE ws=- ~130/800tok`）。
  - 期待: 仕様書 §5.4.2・§5.2 — 解決後のワークスペース ID（このリポジトリなら `Lucianke` 由来の ID）が出る。
  - 担当の観察: `CallInfo.workspace_id` は引数の値のまま（引数なしなら null）。
- 症状 2: バッチ入力の一部が失敗したとき、失敗項目の件数が `warnings[]` に出ない（`! …` 行と `items[].ok` だけ）。
  - 期待: 仕様書 §5.4.4「1 件以上成功していれば応答全体は ok: true とし、失敗項目の件数を warnings[] に書く」。
  - 担当の観察: `ToolOutput` に warnings の欄が無い。
- 再現手順: `uv run --directory S:/work/develop/Lucianke lucianke` を MCP クライアントで起動し、`get_file_skeleton` に `{"paths": ["src/lucianke/core/urn.py", "no/such/file.py"], "format": "json"}`。
- 調査はしていない（直す役の仕事）。

## 原因
- 症状 1: パイプラインは `CallInfo.workspace_id` に引数の値（省略なら `None`）をそのまま入れていた。ワークスペースを解決するのはハンドラの中（`ToolServices.workspace()`）だけで、その結果がヘッダを描く側へ戻る道が無かった。設計書 §7.1.2 手順 3（ハンドラの前に workspace_id を解決する）が実装されていなかった。
- 症状 2: `ToolOutput` に `warnings` の欄が無く、T-02・T-05 は一部失敗を `! …` 行と `items[].ok` にしか出せなかった（§5.4.4 の「失敗項目の件数を warnings[] に書く」が未実装）。

## 直し方
- 症状 1: 設計書 §7.1.2 のとおり、パイプラインがハンドラの前に解決する道を選んだ。`Services.resolve_workspace_id(workspace_id)` を足し（基底は引数をそのまま返す。`ToolServices` は `workspace(workspace_id).workspace_id` を返す）、パイプラインがカーソル復号の前に呼ぶ。解決後の ID で `args.workspace_id`・`CallInfo.workspace_id` を差し替えるので、ヘッダの `ws=`・カーソルの結び付け・ハンドラ（`ctx.call` と `args`）が同じ値を見る。解決できない ID は `E_WORKSPACE_NOT_CONFIGURED` の `isError` 結果（ハンドラは呼ばない）。
- 症状 2: `ToolOutput.warnings: Sequence[str] = ()` を足し、パイプラインがハンドラの後で自分の警告の後ろへ足して `RenderInput.warnings` に流す。T-02・T-05 は一部失敗のとき `batch_warnings(失敗数, 全数)`（`tools/_services.py`）で「N of M items failed (…)」を入れる。全件成功は空、全件失敗は従来どおり `ok: false`。
- テスト: `tests/unit/test_mcp_pipeline.py`（解決後の ID がヘッダ・カーソル・ハンドラに出る、解決失敗はエラー結果、ハンドラの警告が compact の `! ` 行と json の `warnings[]` に出る）、`tests/contract/test_T02_skeleton.py`・`test_T05_bodies.py`（`ws=poly`、失敗件数の警告）、`tests/e2e/test_vertical_slice_stdio.py`（実機の stdio で `ws=- ` でないこと、再現手順どおりの `warnings`）。

## 確認
- 再現手順どおり stdio クライアントから `get_file_skeleton` に `{"paths": ["src/lucianke/core/urn.py", "no/such/file.py"], "format": "json"}` を送り、`ok: true`・`warnings` が 1 件（`1 of 2 items failed …`）、compact のヘッダの `ws=` が `-` でないことを e2e で確認。
- `tests/unit/test_mcp_pipeline.py`・`tests/contract/test_T02_skeleton.py`・`test_T05_bodies.py`・`tests/e2e/test_vertical_slice_stdio.py` を 3 回続けて green（88 件）。
