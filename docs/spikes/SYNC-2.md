# SYNC-2 スパイクの残りの結論と構築の担当の報告の反映（S-4・S-5・S-6・S-8・S-11・S-12）

日付 2026-10-29。設計書 = `docs/Lucianke アーキテクチャ設計書.md`、仕様書 = `docs/Lucianke仕様書.md`。節番号は変えていない。新しい事実には `（S-nn で確認）` または `（実装で確定 2026-10-29）` を添えた。第 1 回は [SYNC-1.md](SYNC-1.md)。

## 反映した点（表の 1 行 = 1 点。計 41 点）

| 出どころ | 点 | 設計書 | 仕様書 |
|---|---|---|---|
| S-4 | basedpyright は `basedpyright-langserver --stdio`（Node 不要）、インタープリタは `workspace/configuration` の `python` だけ、`locale: "en"` | §7.5.1（表・定義） | — |
| S-4 | `LspServerDef` に `locale`・`config_for`、環境変数はサーバー名単位 | §7.5.1 | （§12.3 は `<LANG>` のまま。実装は両方） |
| S-4 | Python の `codeAction` は ruff が供給元（basedpyright は `# pyright: ignore` のみ）、`resolve` は ruff のため | §7.5.1 表、§7.5.6 | — |
| S-4・S-5 | 受信 URI の正規化（`unquote` → 先頭 `/` → `normcase(realpath)`） | §7.5.3 手順 4、§7.5.5 2(e) | — |
| S-4・S-5 | `WorkspaceEdit` は `changes` と `documentChanges` の両形、`version: null`、サーバーごとの癖の表 | §7.5.1 表、§7.5.6 | — |
| S-4・S-5 | 版待ち: `>=`、依存元は `didChangeWatchedFiles` ＋ 300 ms 静止、空診断は「きれい」でない、版なしサーバーは発行を数える ＋ 猶予 2 s | §7.5.5 手順 2 | — |
| S-4 | `ready` の契機、`didOpen` 直後の要求は約 0.6 s 待たされる（ADR-6 (b) は変更不要） | §7.5.2 | — |
| S-4 | `didChangeWatchedFiles` が必須、参照元を開く必要は無い | §7.7.6 手順 2・3 | — |
| S-4・S-5・S-11 | 終了: `shutdown` → `exit` で全部終わる、シム経由は木ごと kill | §7.5.2 | — |
| S-5 | TS の既定を typescript-language-server に入れ替え、vtsls は代替。tsserver の探索と渡し方 | §7.5.1 | — |
| S-5 | サーバーからの要求への返答（`workspace/configuration` ほか） | §7.5.1 | — |
| S-11 | `position_encoding_default` を削除、規則は「応答に無ければ UTF-16、未知も UTF-16」、契約テスト、`lsp.position_encodings`（MAY） | §7.5.1、§7.5.3 | — |
| S-11 | 文字境界の列だけを送る（MUST）、丸めが出たら降格、ruff の panic | §7.5.3 手順 3 | — |
| S-6 | `step=10` 必須、`debounce` の意味、フィルタ（`.git/`・`.lucianke-tmp-*`・IgnoreFilterManager）、`wait_for` で包まない | §7.3.6 手順 1 | §12.4 手順 2 |
| S-6 | 種別は信用せずパスの集合に畳む（旧「追加 + 削除 = 削除」は誤り） | §7.3.6 手順 2 | §12.4 手順 2 |
| S-6 | バースト検知: 100 件以上の窓を 0.3 秒静まるまで合算し 500 超で `rescan` | §7.3.6 手順 3 | §12.4 手順 4 |
| S-6 | ディレクトリの移動・改名は子のイベントが来ない（列挙 ＋ 前方一致で削除） | §7.3.6 手順 4、受入 | §12.4 手順 2 |
| S-6 | 開始失敗は `OSError`、`force_polling` は使わない、ネットワークドライブは未検証、起動順 | §7.3.6 手順 6・起動順 | §12.4 手順 6 |
| S-8 | `resolve_revision` は許可リスト方式、3 つの例外を `E_REVISION_NOT_FOUND`、短縮 SHA の最適化は不要 | §7.9.1 | — |
| S-8 | `Repo` を保持しない（`with Repo`）、`open()` は `Repo(root)`、`read_blob` の `None` の条件、`STAGED` の競合 | §7.9.1 | — |
| S-8 | `status` は `optional_locks=False` を MUST、機械検査の順 | §7.9.1 手順 7 | — |
| S-8 | `Walker` は使わない（F-PY 規模で 13 秒超）、履歴専用の `git` 子プロセス案（推奨・未決） | §7.9.2 | — |
| S-8 | 改名検出は `rename_threshold=50`、大きな差分は 1 件ずれうる | §7.9.2 | — |
| S-8 | OD-20 に S-8 の結果（M1 は (a) 確定、M2 の履歴だけ未決） | — | §17 OD-20 |
| S-12 | 推定式を `lean_wide`（8 項の係数と式、誤差、速度、限界、テスト）に | §7.2.1 | §5.7 |
| S-4〜S-12 | §12.1 の結果欄 6 行（S-4・S-5・S-6・S-11 合格、S-8 部分合格、S-12 現行不合格）と本文の注、§12.2 タスク 1 | §12.1、§12.2 | — |
| S-4〜S-12 | §13: R-4・R-7 を縮小、R-12（トークン推定の過小）を追加 | §13 | — |
| 構築 | Compact の例を検査器に通る形へ: `[Node]`・`[Edge]`・`[Tag]` に `@L`、`(25 lines)` を削除、`resolved:` を桁 0 | — | §11 T-02・T-05・T-10・T-13 |
| 構築 | 同上の追加分: T-14 の見出し行を `query:` の kv 行に。規約 8・9 を §6.1 に追記 | §7.2.4 | §6.1、T-14 |
| 構築 | エンジン選択: 仕様書 §8.2 の Tier 4 を §7.8.2 に揃える（T-06 実装は `unavailable`、T-07 は grep、T-08 は 0 件、T-11 は字句置換） | §7.8.2 | §8.2 |
| 構築 | `standby` の扱い（契機になる能力は `indexing` 列、ならない能力は `unavailable` 列で `lsp_status=standby`）、Tier 3 は `structural`・`n/a` | §7.8.2 | §8.2 |
| 構築 | パスの封じ込め: `reason` に `secret_file`・`invalid_chars`、Windows の拒否対象、`is_excluded` の規則、拡張子表は `workspace/languages.py` | §7.3.2、§7.3.3 | — |
| 構築 | Index: 読み取りプールは `mode=ro` ＋ `query_only`、作り直しは接続内で全 `DROP`（`edit_seq` は引き継ぐ）、`WRITE_QUEUE_MAX=1000`、`workspace_hash` 16 桁 | §6.2、§7.6.1、§7.6.5、§7.3.1、§8.1 | §4.5 |
| 構築 | 公開クエリ: 4 段の防御、`temp_grep_matches` は `query_only` の前、`PRAGMA` は `data_version` と `query_only` の読み取りだけ | §7.6.4 | §13.2、T-20 規約 3 |
| 構築 | 子プロセス: `CTRL_BREAK_EVENT` は GUI ホスト配下で届かない → LSP は `shutdown`/`exit` を先に（Job Object は SYNC-1 で反映済み） | ADR-12、§8.7、§9.5 | — |
| 構築 | 構文検証: エラーノードは窓の中の個数（盲点を明記）、期限切れの解析は `failed`、`details.errors[]` は `{line, message}` | §7.7.4 | （§5.8 は元から `{line, message}`） |
| 構築 | 編集の計画: `overlapping_edits`・`edit_outside_workspace` は `E_INVALID_ARGUMENT`、`repeated_document_edit` は `E_UNSUPPORTED` | §7.7.2 | — |
| 構築 | 境界規則: Doc コメントは空行を挟んでも含める、通常コメントは空行無し ＋ その行に他のコードが無いとき、`pass`/`...` は置換対象、`blank_lines_around` は Python のトップレベルだけ 2 | §7.7.7 | §10.8 |
| 構築 | URN の `/arity` は衝突グループ内でアリティが 2 種類以上あるときだけ | — | §7.2 |
| 構築 | MCP の入口: `ToolSpec`・`CommonArgs`・`ToolContext` は `tools/_base.py`（`registry.py` が再公開）、Compact の行組み立ては `core/compact_lines.py`、トークン推定は `core/tokens.py` | §4.1、§4.2、§4.3、§7.1.1、§7.2.4 | — |
| 親の追加 | (1) `fold` に `nodes omitted`（XML の深さ超過、T-02 の 4 と整合）。(2) `[Tag] failure ×3` などの `@Ls-e` は最初の開始から最後の終了まで。(3) T-20 の JSON の `rows` は列名をキーにしたオブジェクトの配列（重複は `_2`）。(4) T-19 の compact は `table: x (N rows)` の kv 行 ＋ `sig:`・`doc:` | — | §6.1（EBNF・規約 9）、T-20 json、T-19（規約 8・例） |

## 反映しなかった点と理由

- **S-4 の `position_encoding_default`（basedpyright=utf-16、ruff=utf-8）**: S-11 が後から覆した（持たないほうが安全）。S-11 を採り、設計書 §7.5.1 に「S-4 の提案を S-11 が覆した」と明記した。
- **S-8 の「`git` 子プロセスを履歴専用に許可」**: 方針の判断（仕様書 §4.2 は外部 `git` を起動する (c) を方針と相反としている）。設計書 §7.9.2 と OD-20 には「推奨・未決」として事実と案だけを書き、決めていない。師匠の判断が要る。
- **S-12 の `core/tokens.py` の置き換え、S-4/S-5 の `servers.py` 反映、S-8 の `git/repo.py` 実装**: コードは対象外。設計書に目標を書いた。S-12 の実装は構築側で進行中（`core/tokens.py` が作業ツリーで変更中）。
- **仕様書 §12.3 の `LUCIANKE_LSP_<LANG>_CMD`**: 実装は言語単位とサーバー名単位の両方を持つ（サーバー名が先）。仕様書は言語単位のままで矛盾しないので、設計書 §7.5.1 にだけサーバー名単位を書いた。
- **仕様書 §10.3 の表 4（構文検証）の「編集範囲に新たに生じたら」**: 窓の中の個数で比べる実装は設計書 §7.7.4 に書いた。仕様書は結果の規約として十分なので変えていない。
- **S-5 の「§6.5 終了手順: Windows のシム起動では強制終了を木ごとに」**: §7.5.2 の「終了」と ADR-12（Job Object）に書いた。§6.5 の本文は変えていない（重複を避けた）。
- **構築の報告のうち、Windows の読み取り専用ファイル（`E_MUTATION_BLOCKED`/`read_only_file`）と再試行（winerror 5・32、10〜160 ms、全体 1 s）**: SYNC-1 で設計書 §7.7.5・仕様書 §10.6 に反映済みで、重複させなかった。Job Object と `ProcessRegistry`、同じく SYNC-1 で反映済み。
- **§5.4.2 の例の `...`**: 省略記号であり、検査器の対象外とした（仕様書 §6.1 の規約 9 に注記）。

## 判断した点（事後報告）

- 設計書 §7.7.4 の「期限切れの解析は `failed`」は、`SyntaxCheckResult.status == "failed"`（検証できなかった = 通さない）と読んだ。メッセージは `syntax could not be verified: parse deadline exceeded`。
- 設計書 §7.3.2 の Windows の拒否対象の `reason` は、すべて `invalid_chars`（`paths.py` の `invalid_chars` の分岐で確認）。
- T-14 の見出し行 `flow=data hops<=8 ...` は検査器に通らなかったため `query:` の kv 行にした（親の追加項目ではない。ほかに T-13 の `  resolved:` も直した）。
- S-4 の URI 正規化の `unquote` 後の先頭 `/` 除去は、Windows のドライブ付きパスにだけ効く（S-4 の型どおり）。

## 未反映

- **検査器 `tests/contract/compact_grammar.py` は `fold` の `nodes omitted` を受けない**（`RE_SYMBOL` の `lines omitted` だけ）。仕様書側は親の依頼どおり EBNF を直した。検査器の更新が要る（コード）。
- rust-analyzer・gopls・clangd の位置単位と診断（S-11/S-5 とも未測定）、macOS / Linux の LSP（S-4）、ネットワークドライブの監視（S-6）。
- S-5 の「開いていないファイルの診断を TS の 2 サーバーが再発行するか」（§7.7.6 手順 2 に未確認と明記）。
- S-8 の pygit2 の実測（M2 のベンチ）と、`IgnoreFilterManager` が追跡済みファイルを扱う挙動（index は見ない。今回未確認）。
- S-2 項目 8 の BOM（SYNC-1 から持ち越し）。S-11 の「`lsp.position_encodings` の設定項目」は、仕様書 §12.7 の設定一覧にまだ載せていない（MAY のため）。
