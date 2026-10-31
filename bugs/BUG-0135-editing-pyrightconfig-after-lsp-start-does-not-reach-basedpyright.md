# BUG-0135 LSP の起動の後にワークスペースの根の `pyrightconfig.json` を書き換えても basedpyright に届かない（解析の対象が起動のときのまま）

見たことだけを書く（Lucianke の側の対処は調べていない）。2026-10-31、BUG-0132 の直す役が見た。

## 症状
- F-POLY の複製（`tmp_path`。リポジトリの外）の根に `pyrightconfig.json`（`{"include": ["src"]}`）を置いて、Lucianke の `LspPool` で
  basedpyright 1.40.2 を起こし、`src/shop/services/payment.py` の `PaymentProcessor.process_charge` の `textDocument/references` を
  `features.references` で直接送った（`include_declaration: false`）。
- その後 `pyrightconfig.json` を `{"include": ["src", "tests"]}` に書き換えた:

| 時点 | references の答えのファイル |
|---|---|
| 書き換える前 | `src/shop/api/routes.py` |
| 書き換えて 5 秒後（Lucianke からは何も送らない） | `src/shop/api/routes.py`（`tests/test_payment.py` が入らない） |
| `LspPool.notify_watched(ws, [("pyrightconfig.json", CHANGED)])`（`workspace/didChangeWatchedFiles`）を送って 5 秒後 | `src/shop/api/routes.py`・`tests/test_payment.py` |

- `tools/_runtime.py` の `IndexerSink` は、索引が取り込んだファイル（`index_file` が `stats` を返したもの）の変更だけを `notify_watched` で
  LSP に送る（コードを読んだだけ。`pyrightconfig.json` / `pyproject.toml` の変更がこの経路に乗るかは確かめていない）。

## 再現
1. 上のとおり、根に `pyrightconfig.json` を持つワークスペースで Python の LSP を起こし、参照を 1 回聞く。
2. `pyrightconfig.json` の `include` / `exclude` を書き換え、もう一度聞く（`reindex_scope: "lsp"` で起動し直すまで答えが変わらない）。

## 影響しうる使い方
LSP が動いている間に、利用者（または AI）が `pyrightconfig.json`・`pyproject.toml` の `[tool.basedpyright]` / `[tool.pyright]` を編集したとき。
根に `pyrightconfig.json` が無く `pyproject.toml` を設定ファイルとして明示した起動（BUG-0132）の後に `pyrightconfig.json` を作ったときも
同じかもしれない（見ていない）。

## 仕様書
§12.3（「Python の設定の根」の行。根の設定は起動のときに読む）・§12.4。関連: BUG-0132（T-06・T-07 が開かせる候補は、LSP の起動のときに
覚えた解析の対象〔`LspHandle.analysis_scope`〕で決める）。
