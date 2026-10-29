# BUG-0010 LspClient.to_lsp_position / to_lsp_range が丸め（clamped）を無視して値を返す

- 報告: 2026-10-29 / 見た人: docsync-3（設計書 §7.5.3 を実装と突き合わせていて）
- 見たこと:
  - `src/lucianke/lsp/client.py` の `to_lsp_position`（モジュール関数と `LspClient` のメソッド）は `line_index.to_unit(pos, encoding)` が `clamped=True` を返しても、`debug` ログを 1 行出して丸めた列を返す。`to_lsp_range` も同じ。
  - 設計書 §7.5.3 手順 3 と S-11（`docs/spikes/S-11.md` 設計への反映 4）は「送る列は文字境界だけ（MUST）。丸めが出たら送らず降格」。ruff は文字の途中の列を増分 `didChange` で受けるとパニックして終了する（S-11 の実測）。
  - 現在、`client.py` の外から `to_lsp_position` / `to_lsp_range` を呼ぶ箇所は無い（`rg "to_lsp_position|to_lsp_range" src` で確認）。`lsp/features.py` は自前の `_lsp_position`（`clamped` なら `E_LSP_UNAVAILABLE` / `position_clamped`）を使っている。
- 再現手順: 絵文字とかなを含む 1 行の `LineIndex` を作り、行末を越える `Position(0, 99)` で `to_lsp_position(idx, Position(0, 99), Unit.UTF16)` を呼ぶ（`to_unit` は `clamped=True` を返す）。例外にならず、行末に丸めた列が返る。
- 調査はしていない（直す役の仕事）。設計書は「丸めは受信側だけ許す」と書いた。
