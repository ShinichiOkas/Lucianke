# BUG-0010 LspClient.to_lsp_position / to_lsp_range が丸め（clamped）を無視して値を返す

- 報告: 2026-10-29 / 見た人: docsync-3（設計書 §7.5.3 を実装と突き合わせていて）
- 見たこと:
  - `src/lucianke/lsp/client.py` の `to_lsp_position`（モジュール関数と `LspClient` のメソッド）は `line_index.to_unit(pos, encoding)` が `clamped=True` を返しても、`debug` ログを 1 行出して丸めた列を返す。`to_lsp_range` も同じ。
  - 設計書 §7.5.3 手順 3 と S-11（`docs/spikes/S-11.md` 設計への反映 4）は「送る列は文字境界だけ（MUST）。丸めが出たら送らず降格」。ruff は文字の途中の列を増分 `didChange` で受けるとパニックして終了する（S-11 の実測）。
  - 現在、`client.py` の外から `to_lsp_position` / `to_lsp_range` を呼ぶ箇所は無い（`rg "to_lsp_position|to_lsp_range" src` で確認）。`lsp/features.py` は自前の `_lsp_position`（`clamped` なら `E_LSP_UNAVAILABLE` / `position_clamped`）を使っている。
- 再現手順: 絵文字とかなを含む 1 行の `LineIndex` を作り、行末を越える `Position(0, 99)` で `to_lsp_position(idx, Position(0, 99), Unit.UTF16)` を呼ぶ（`to_unit` は `clamped=True` を返す）。例外にならず、行末に丸めた列が返る。
- 調査はしていない（直す役の仕事）。設計書は「丸めは受信側だけ許す」と書いた。

## 原因
- `lsp/client.py` の `to_lsp_position`（モジュール関数）が `line_index.to_unit(...)` の `clamped` を見て `debug` ログを 1 行出すだけで、丸めた列をそのまま返していた。`to_lsp_range` と `LspClient.to_lsp_position` / `to_lsp_range` は同じ関数を呼ぶだけなので同じ穴だった。設計書 §7.5.3 手順 3・S-11 の「送る列は文字境界だけ（MUST）」を満たす実装が `lsp/features.py` の `_lsp_position`（自前で `to_unit` を呼び、`clamped` なら `E_LSP_UNAVAILABLE` / `position_clamped`）にしか無く、送信側の変換が 2 か所に分かれていた。現状は `client.py` の外から呼ぶ箇所が無かったので実害はまだ出ていないが、将来の呼び手（編集側・診断側など）が `client.to_lsp_position` を使うと、文字の途中の列で ruff がパニックする経路が開いていた。

## 直し方
- 送信側の変換を `client.py` の 1 か所に集めた。`to_lsp_position` / `to_lsp_range`（モジュール関数と `LspClient` のメソッド）は、丸めが要る位置のとき丸めた値を返さず `LuciankeError(E_LSP_UNAVAILABLE, details.reason="position_clamped")` を上げる（メッセージは features と同じ「… is not on a character boundary; the request was not sent」）。`to_lsp_range` は start / end のどちらが丸めでも同じ例外。範囲外の行は従来どおり `ValueError`。受信側（`from_lsp_position` / `from_lsp_range`）の丸めは設計書どおり許す。
- 例外の `details` に `server` / `language` を載せたい呼び手のため、キーワード引数 `details=` を足した（`reason` は常に `position_clamped`）。
- `lsp/features.py` の `_lsp_position` は自前の `to_unit` と丸めの判定をやめ、`client.to_lsp_position(index, pos, details=_server_details(handle))` を呼ぶだけにした（`ValueError` → `E_INVALID_ARGUMENT` の変換だけが残る）。`_lsp_range` は従来どおり `_lsp_position` を 2 回。`details`（`reason` / `server` / `language`）は直す前と同じ。
- テスト: `tests/unit/test_lsp_client.py` に追加（絵文字とかなの行 `a😀かな`、行末を越える `Position(0, 99)` が UTF-16・UTF-8・コードポイントのすべてで `E_LSP_UNAVAILABLE` / `position_clamped`、境界の列は全て通り期待値どおり〈UTF-16: 0,1,3,4,5 / UTF-8: 0,1,5,8,11〉、行末ちょうどは通り +1 は拒否、`details` の合流、範囲外の行は `ValueError`、`to_lsp_range` は片端でも拒否、`LspClient` のメソッドも両単位で拒否、受信側の丸めは許す）。既存の `test_lsp_features.py::test_fake_clamped_position_is_not_sent`（definition / hover / code_actions が丸めを送らない）がそのまま通る。

## 確認
- 再現手順どおり、絵文字とかなの行に `Position(0, 99)` で `to_lsp_position(idx, Position(0, 99), Unit.UTF16)` を呼ぶと、直す前は行末の列が返り、直した後は `E_LSP_UNAVAILABLE`（`details.reason == "position_clamped"`）が上がることを単体テストで確認（UTF-8 でも同じ）。
- `tests/unit/test_lsp_client.py`・`test_lsp_features.py`・`test_import_graph.py` を 3 回続けて実行し、1 回目・3 回目は 101 passed・1 skipped。2 回目は実サーバー（pyright）を使う `test_real_*` が `client.request` のタイムアウトで落ちた（負荷が高い間の既知のゆらぎで BUG-0012 に記録済み。位置の変換は通って要求は送られている）。その後に落ちたテストだけを再実行しても落ちるテストは入れ替わる（負荷依存）。位置まわりのテスト（fake サーバーの `test_fake_*`）は毎回 green。
- `uv run ruff check src/lucianke/lsp tests/unit/test_lsp_client.py tests/unit/test_lsp_features.py` は clean。
