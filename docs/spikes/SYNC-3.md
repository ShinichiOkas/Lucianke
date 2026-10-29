# SYNC-3 実装との差の反映（LSP プール・索引器・core の移設）

日付 2026-10-29。設計書 = `docs/Lucianke アーキテクチャ設計書.md`、仕様書 = `docs/Lucianke仕様書.md`（本回は触っていない）。節番号は変えていない。実装で決まった点には `（実装で確定 2026-10-29）` を添えた。第 1 回は [SYNC-1.md](SYNC-1.md)、第 2 回は [SYNC-2.md](SYNC-2.md)。

SYNC-2（docsync-2、e0d5418）が S-4・S-5・S-6・S-8・S-11・S-12 の結論を先に設計書へ入れたので、本回は**重複させず**、実装との差だけを足した。S-11 の `position_encoding_default` の削除、`Unit(caps.get("positionEncoding","utf-16"))` の規則、文字境界の列だけを送る、vtsls の `definition` が宣言文全体、`workspace_hash` の 16 桁、core への移設の記述は SYNC-2 以前に入っており、確認だけした（下記「既に入っていたもの」）。

## 反映した点（表の 1 行 = 1 点）

| 節 | 何を | 根拠 |
|---|---|---|
| §7.5.1 | `LspServerDef` を実装のフィールドに: `language_id`（単数。TypeScript 系は言語 ID ごとに 1 件）、`argv_candidates`、`initialization_options`、`settings`（設定木）、`lsp_language_id`、`best_effort`、`locale`、`requests_m1`、`tsserver_via`、`needs_workspace_tsserver`。初版の `config_for` / `init_options: Callable` は `build_settings` / `build_initialization_options` と `LspClient.set_settings` に置き換わった | `lsp/servers.py`、S-4・S-5、522770b |
| §7.5.1 | 探索は `shutil.which` を使わず自前（Windows の `which` はカレントを先に見る）。指定 venv を `.venv` より先。環境変数の値はそのまま argv | `lsp/servers.py` の `find_command` |
| §7.5.1 | `Unit` に無い値は UTF-16 + 警告、ただし実装は `utf-32` を `Unit.CODEPOINT` として受ける（提示していないので来ない。S-11 の推奨との差を明記）。`lsp.position_encodings` と `tests/lsp_contract` は**未実装・未作成**と明記 | `lsp/client.py`、S-11 反映 1〜3 |
| §7.5.2 | `LspPool` の実際の API: `role` 引数、`handles`、`wait_ready`、`sweep_idle`、`async reset`（停止の回収を待つ）、`notify_watched(ws, changes) -> float`（`sent_at`）、`shutdown_all` の重複を削除。`on_diagnostics(path, diags, version, source)` の 4 引数（`source` はサーバー名） | `lsp/pool.py`（66bc682 ほか）、8b の報告 |
| §7.5.2 | 要求のタイムアウトはプールから見えず、機能呼び出し側が `LspHandle.record_ok()` / `record_timeout()` を呼ぶ。`unavailable` の理由に `timeout_3x` | `lsp/pool.py`、`lsp/features.py` |
| §7.5.2 | 監督タスク 1 本 + 同期の `standby` 遷移（言語ごとの `Lock` は不要）、`ready` の合図 3 つ（全 `end` 揃い / 最初の診断 / `ready_fallback_s` 15 s・`indexing_max_s` 300 s）、`stable_after_s` 60 s、`not_found` は即 `unavailable` | `lsp/pool.py` |
| §7.5.3 | `LspClient` の実際の API（`start` / `initialize` / `request` / `set_settings` / `shutdown`、`negotiated_encoding: Unit`）。`cancel` は公開しない（`request` が `$/cancelRequest` を送る）。位置変換は `LineIndex.to_unit` / `from_unit`。受信は丸めてよく、送信側の降格は `features._lsp_position` が行う（`LspClient.to_lsp_position` は丸めるだけ → BUG-0010） | `lsp/client.py`、`lsp/features.py`、S-11 |
| §7.5.4 | `DocumentSync` を同期 API に: `did_open` / `did_change` / `sync` / `on_external_change` / `did_close` / `version_of` / `sent_at_of` / `touch`。LRU は最も古い文書から閉じ、開こうとしている文書は閉じない | `lsp/documents.py` |
| §7.5.5 | `DiagnosticsStore` → `DiagnosticsHub`（サーバー 1 つ分）。`wait_for(path, *, min_version, sent_at, timeout_ms, settle_ms, min_publishes)`、`is_provisional`、`settle_all`。ハブ自身は index に書かず、プールが `source` を足した 4 引数で呼び手に渡す。`DiagnosticsWaiter`（§7.7.6）は `wait_for` の上の薄い層 | `lsp/diagnostics.py`、8b の報告 |
| §7.5.6 | `LspFeatures` クラス → `lsp/features.py` の async 関数群（`definition` / `references` / `hover` / `prepare_call_hierarchy` / `incoming_calls` / `outgoing_calls` / `rename` / `code_actions` / `execute_command` / `to_exact_relations`）。`ready` でないとき `None` ではなく `E_LSP_UNAVAILABLE`（`details.reason`）を投げ、読み取り系は呼び手が降格。`WorkspaceEdit` は dict のまま返し、`applyEdit` は `execute_command` が捕まえる | `lsp/features.py`（ba55dbd） |
| §4.1 | `engine/indexer.py` を木に追加。`diagnostics.py` の説明を `DiagnosticsHub` に。`ingest.py` は `FileFacts` を受けて呼び手のトランザクション内で動く。`connection.py` の `workspace_hash` は `core/hashing.py` の再公開 | `engine/indexer.py`、`index/connection.py` |
| §4.3 | `index/ingest.py` の行を修正し、`engine/indexer.py` の行を追加 | 同上 |
| §7.6.2 | 見出しを「`engine/indexer.py` と `index/ingest.py`」に。取り込みの API を実装の形に（`Indexer.index_file` / `remove` / `scan` / 背景ジョブ、`ingest_file` / `remove_file` / `upsert_workspace` / `upsert_exact_relations` / `replace_lsp_diagnostics`） | 5a0a33f、c61cd6d、11a の報告 |
| §7.6.2 | 1 トランザクション最大 50 ファイル（`INGEST_BATCH_FILES`）、`data_version` はトランザクションごとに +1、書き込みは 1 バッチだけ先行 | `engine/indexer.py` |
| §7.6.2 | import 表から作った関係の後始末 `_settle_relations`: `symbols` に無い目標（`#` を含む）の行を消し、`target_file_id` を埋める。外部シンボルは `ingest_file` の `_ensure_external` が `symbols` に行を作るので残る | `engine/indexer.py` |
| §7.6.2・§7.4.6 項 3 | **未解決の import は `lk://_ext/<パッケージ>#<名前>` の外部扱いにする（決定）**。T-06・T-07 が外部ノードを要るため。現状の実装は捨てている（`_relations` が目標の無い関係を落とす）ので、別の役（7j）が設計書に合わせて直す。両節に差を明記 | 親の決定 2026-10-29 |
| §7.6.2 | Tier 3 の `FormatAdapter` は M1 ではシンボルを返さず `files` 行だけ（`lexical_fallback`）。`symbols(...)` を持つアダプタはそのシンボルも入れる（任意） | `engine/indexer.py` の `_analyze_adapter` |

## 既に入っていたもの（確認だけ。重複させなかった）

| 項目 | 場所 | 入れた回 |
|---|---|---|
| `workspace_hash` は 16 桁（`WORKSPACE_HASH_LEN`）。`[:12]` が残る 2 か所（§7.2.3、§8.6）は `CursorCodec` の `sha1(args)[:12]` で別物（`mcp/budget.py` も 12） | §4.1 `hashing.py`、§7.3.1 | SYNC-2（c3d2480 の実装） |
| `core/compact_lines.py`・`core/tokens.py`・`core/hashing.py` の新設と `mcp/render/compact.py`・`mcp/budget.py` の再公開 | §4.1、§4.3、§7.2.4 | SYNC-2 |
| S-11: `position_encoding_default` の削除、`Unit(...)` の 1 規則、提示は `["utf-8","utf-16"]`、`lsp.position_encodings`（MAY）、文字境界の列だけを送る、vtsls の `definition` が宣言文全体 | §7.5.1、§7.5.3 | SYNC-2（e0d5418） |
| S-4・S-5・S-6・S-8・S-12 の結論、§12.1 の結果欄、§13 の R-4・R-7 | §7.3.6、§7.5、§7.9、§7.2.1、§12.1、§13 | SYNC-2（e0d5418） |
| T-20 の JSON `rows` が列名キーのオブジェクト、T-19 の compact が `table:` の kv 行 | 仕様書 T-19・T-20 | SYNC-2（仕様書側。設計書は無関係） |

S-1〜S-12 の報告書（`docs/spikes/S-*.md`）の「設計への反映」を一通り見直した。設計書に未反映の結論は、下記「未決」と SYNC-2 の「未反映」に挙げたもの以外に見つからなかった。

## 仕様書への申し送り（仕様書は触っていない）

1. **§12.2（位置の単位、L3054 付近）**: S-11 の規則を 1 文足す提案。「`positionEncoding` が応答に無ければ UTF-16。サーバーごとの上書きは持たない。LSP へ送る列は文字境界だけで、丸めが要るときは要求を送らず降格する（ruff は文字の途中の列でパニックする）」。実測は 4 サーバー（basedpyright・vtsls・typescript-language-server は UTF-16、ruff は utf-8）。現文の「交渉できない LSP は UTF-16 とみなす」と矛盾はしない。
2. **§12.7（設定一覧）**: `lsp.position_encodings`（MAY、既定 `["utf-8","utf-16"]`、`["utf-16"]` に絞る逃げ道）を足す。設計書では未実装と明記してあるので、足すなら「未実装」の印とともに。
3. **§12.3（発見、L3064 付近）**: 環境変数はサーバー名単位 `LUCIANKE_LSP_<サーバー名>_CMD` が先、言語単位 `LUCIANKE_LSP_<LANG>_CMD`（lint は `…_LINT_CMD`）が後。TypeScript の既定は typescript-language-server、代替は vtsls（SYNC-2 で判断済みの「矛盾なし」は維持。書くなら追記）。
4. **§9.3（外部シンボル）は変更不要**: 既に `lk://_ext/<パッケージ名>[@<版>]#…`、`is_external = 1` と書いてある。未解決 import を外部扱いにする決定とは整合する。T-06・T-07 の事後条件に「解決できない import の呼び先が `is_external` の葉として現れる」を足すと、7j の受入にそのまま使える（推奨）。
5. **`status.reason` の列挙**: 仕様書に LSP の理由（`not_found` / `crashed_3x` / `init_failed`）の列挙は見当たらないので、`timeout_3x` を足す先は無い。T-00 の `status` の説明に列挙を置くなら 4 つにする。
6. T-20 の JSON `rows`、T-19 の compact の `table:` 行、S-11 の「期待する単位は契約テスト」は SYNC-2 で入った、または設計書だけの話。**追加の申し送りは無い**。

## 未決として残したもの

- **S-8 の履歴専用 `git` 子プロセス**（設計書 §7.9.2、仕様書 OD-20）: SYNC-2 のとおり推奨・未決。師匠の判断が要る。
- **実装の宿題（設計書に目標を書いた）**: `lsp.position_encodings` の設定、`tests/lsp_contract`（期待する単位の契約テスト）、未解決 import の外部扱い（7j）、`LspClient.to_lsp_position` / `to_lsp_range` の丸めの扱い（BUG-0010。呼び手が無いので害は未顕在）。
- **未測定**: rust-analyzer・gopls・clangd の位置単位と診断、macOS / Linux の LSP、ネットワークドライブの監視（SYNC-2 と同じ）。
- **突き合わせなかった**: `workspace/watcher.py` と §7.3.6、`git/repo.py` と §7.9.1 の実装との差（SYNC-2 が S-6・S-8 の結論を入れた。実装との照合は本回の範囲外）。
- 本回の元になった `features.py` は、書き始めた時点で未コミットだった（ba55dbd で入った）。以後の変更で §7.5.6 の関数名がずれる可能性がある。
