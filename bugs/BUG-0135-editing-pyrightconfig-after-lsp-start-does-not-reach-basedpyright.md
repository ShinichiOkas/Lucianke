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

## 原因・直し方・確認（2026-10-31）

### 原因

- 監視の経路そのもの（`IndexerSink` → `LspPool.notify_watched` → `workspace/didChangeWatchedFiles`）は、索引に載る
  `pyrightconfig.json` / `pyproject.toml` の変更を届けていた。basedpyright 1.40.2 はそれで根の設定を読み直す（`include` を狭めた・
  `pyproject.toml` に節を足した: 下の実測）。足りなかったのは Lucianke の側の 3 つ:
  1. **解析の対象を起動のときのままにしていた**（`LspHandle.analysis_scope`。BUG-0132 で「サーバーは起動の後の編集を受け取らない」と
     見て据え置いた）。サーバーは狭めた対象で探すのに、T-06・T-07 は起動のときの対象（狭めていない）で開かせる候補を決め、`tests/` を
     開かせず routes.py の 2 箇所だけを `exact` で返した。
  2. **設定ファイルの明示の判断（`configFilePath`。BUG-0132）を起動のときのままにしていた**。basedpyright は `pyproject.toml` を明示して
     いる間は根に作った `pyrightconfig.json` を読まない。明示していないまま根の `pyrightconfig.json` を消すと、`didChangeWatchedFiles` を
     受けて親のディレクトリの設定を採り、ワークスペースを解析の対象から外す（`include: ["libs"]` の親の下で参照が 0 件。BUG-0132 の症状に戻る）。
  3. **索引に載らない設定ファイル（`.gitignore` の対象など）の変更は LSP に知らせていなかった**（`index_file` が何もしなかった変更は送らない）。

| 場面（実機の basedpyright 1.40.2、F-POLY の複製、`process_charge` の参照のファイル） | 何も送らない | `didChangeWatchedFiles` だけ | 設定を差し替えて `didChangeConfiguration` |
|---|---|---|---|
| A: 根の `pyrightconfig.json` の `include` を `[src, tests]` → `[src]` | 2 ファイル（届かない） | routes.py だけ（読み直した） | routes.py だけ |
| B: 明示した `pyproject.toml` に `[tool.basedpyright] include = ["src"]` を足す | 2 ファイル | routes.py だけ | routes.py だけ |
| C: 明示した `pyproject.toml` のまま根に `pyrightconfig.json`（`include: ["src"]`）を作る | 2 ファイル | 2 ファイル（読まない） | routes.py だけ（明示を外すと読む） |
| D: `include: ["libs"]` の親の下で根の `pyrightconfig.json`（`{}`）を消す | 2 ファイル | **0 件**（親の設定を採った） | 2 ファイル（`pyproject.toml` を明示し直す） |

### 直し方（決めたこと）

- **根の設定の変更で読み直す**（`LspPool.notify_watched` → `_reload_pyright_config`）: 変更の中に根の `pyrightconfig.json` /
  `pyproject.toml`（`servers.is_pyright_root_config`。大文字小文字を区別しないワークスペースでは畳む）があれば、送る前に Python の型解決の
  ハンドルの明示の判断（新しい `LspHandle.pinned_config`）と解析の対象（`analysis_scope`。`pyright_scope(refresh=True)` で覚えを見ずに
  読む）を読み直す（スレッドで。読んでいる間に起動し直したハンドルは触らない）。
- **明示が変わったら `didChangeConfiguration`**: `LspClient.set_settings` で設定を差し替えて `workspace/didChangeConfiguration`
  （`settings: null`。サーバーが `workspace/configuration` を引き直し、根の設定を読み直す）を送る。そのハンドルには設定ファイルの変更を
  `didChangeWatchedFiles` に入れない（D で、古い明示のまま一度読み直して親の設定を採る間を作らない）。明示が変わらなければ今までどおり
  `didChangeWatchedFiles` だけ（A・B はそれで読み直す）。起動中（`starting`）のハンドルは覚えを直すだけ。ruff（`lint`）には送らない。
  起動し直す（`reindex_scope: "lsp"` 相当）案は採らない: 型サーバーの準備の間（数秒〜20 秒）T-06・T-07 が `structural` に降格するより、
  動いているサーバーに読み直させる方が呼ぶ側の AI にとって有用。
- **索引に載らない設定ファイルも知らせる**（`IndexerSink`。`servers.is_lsp_config`: 根の Python の 2 つと、どの深さでも
  `tsconfig.json` / `jsconfig.json` / `tsconfig.<名前>.json`）: 内容の `sha256` を覚え、前に知らせたものと違えば作成・変更・削除を
  知らせる（起動の後の最初の変更は前を知らないので知らせる）。バーストの差分索引の後は根の Python の設定も見て回る。
- **TypeScript**: `tsconfig.json` の変更は索引の経路で TS の LSP にも `didChangeWatchedFiles` が届いていた（`.gitignore` の対象も上で
  届く）。Lucianke は `tsconfig` から覚えるもの（解析の対象など）を持たない（tsserver 系は `tsconfig` の外も含めて名前を含むファイルを
  開かせる。BUG-0047）ので、読み直すものは無い。この PC に TS の LSP が無く、実機では確かめていない。
- 仕様書: §12.3「Python の設定の根」の行（起動のときだけ読むの文を、根の設定の変更の扱いに置き換えた）、§12.4 に 7（索引に載らない
  設定ファイル）。設計書: §7.3.6 の 5、§7.5.1（Python の備考）、§7.5.6（対象の覚え方）、`notify_watched` の API の注記。

### 確認

- 端から端まで（使い捨て。実物の `RuntimeHub` の監視 → `IndexerSink` → `LspPool` → 実機の basedpyright。F-POLY の複製、
  `pyrightconfig.json` は `.gitignore` の対象）: `pyproject.toml` を明示して起こした後に `pyrightconfig.json`（`include: ["src"]`）を
  書くと、0.06 秒で明示が外れ（`pinned_config: None`）・Lucianke の見る対象が狭まり（`narrowed: True`）、サーバーの参照も routes.py だけに
  なった（直しの前は索引に載らないので何も届かない）。

試験（決定的）:

- 新しい `tests/contract/test_lsp_config_change_bug0135.py`（実機の basedpyright。無ければ skip。変更は `IndexerSink.changed` / `removed`
  に渡し、サーバーが読み直した合図は開いている文書の診断の再発行〔1 秒の静止まで〕）。直しの前はどれも赤（使い捨ての写しで確認）、直した後は
  緑（3 回続けて 5 本とも約 20 秒）:
  - 起動の後に `include: ["src"]` に狭めても、T-06 が `tests/test_payment.py` を含む 3 箇所（`pyrightconfig.json`・`pyproject.toml` の
    節の両方）、T-07 incoming が `test_charge_ok` を含む 3 ノードを `exact` で返す。
  - `include: ["libs"]` の親の下で根の `pyrightconfig.json` を消しても、T-07 incoming（`LimitTable.get`）が予算の内に `exact` の
    `_validate_limit`（直しの前はサーバーが診断を出し直さないまま、親の設定を採っていた）。
  - 明示した `pyproject.toml` の上に作った `pyrightconfig.json` の `reportAssignmentType: "none"` が診断に効く（直しの前は読まれない）。
- 新しい `tests/unit/test_lsp_config_notify_bug0135.py`（偽のサーバーが受けた通知と、引き直した設定を書き残す）: 作ると明示を外して
  `didChangeConfiguration` が `didChangeWatchedFiles` より先・設定ファイルは入れない・引き直した設定に `configFilePath` が無い、消すと
  明示し直す、明示が変わらない変更は `didChangeWatchedFiles` だけで対象は読み直す、ほかのファイル（`src/pyrightconfig.json` など）では
  読み直さない、ruff には `didChangeWatchedFiles` だけ。`is_pyright_root_config` / `is_lsp_config` の判定。
- `tests/unit/test_tools_runtime.py` に 3 本: `.gitignore` の対象の `pyrightconfig.json`・`web/tsconfig.json` の作成・変更・削除を一度ずつ
  知らせ、同じ内容・ほかの無視されたファイルは知らせない。索引に載る `pyproject.toml` は索引の経路で一度だけ。バーストの走査で無視された根の
  設定の作成を知らせる（前を知らない無いファイルは知らせない）。

関係する unit・contract（LSP のプール・クライアント・機能・文書・先に開かせる候補、監視・実行時の束・遅延検証、T-06・T-07 の LSP・PaaD・
予算・補足の欄・import の根・TS の tsconfig の外〔TS の LSP が無く skip〕、T-04 の LSP、BUG-0132・BUG-0135 の試験）と見張り 4 本
（`test_import_graph`・`test_server_launch_guard`・`test_stdout_purity`・`test_leak_guard`）が green。`uv run ruff check` クリーン。

### 残ったこと

- `extends` の先の設定ファイルの変更では、Lucianke の見る対象を読み直さない（根の設定を触るか、`reindex_scope: "lsp"` まで）。
  basedpyright が `extends` の先の変更を読み直すかも確かめていない。
- TypeScript の `tsconfig.json` の変更が TS の LSP に効くことは、この PC に TS の LSP が無く実機で確かめていない（Lucianke の側に読み直す
  状態は無い）。
- Lucianke の編集ツールが TOML / JSON を書けるようになったら（Tier 3 の編集は M2）、その書き込みは監視のこだまとして捨てられ、TOML / JSON の
  LSP が無いので basedpyright に届かない。そのときは編集トランザクションからも `notify_watched` を送ること。
