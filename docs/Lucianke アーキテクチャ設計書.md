# Lucianke アーキテクチャ設計書

| 項目 | 値 |
|---|---|
| 版 | 1.0（Draft） |
| 日付 | 2026-10-29 |
| 上位文書 | 『Lucianke 仕様書』1.0 Draft（`docs/Lucianke仕様書.md`）。要求・ツール仕様・受入基準は仕様書が正本であり、本書はそれを **どう作るか** を定める。矛盾があれば仕様書を正とし、本書を直す |
| 確定した前提 | 本体ライセンス MIT（師匠 2026-10-29）。実装言語 Python（仕様書 OD-1 の推奨で進行。異論なし）。配布は `uv tool install` / `pipx`（OD-2 推奨） |
| 読者 | 構築者（M1 を作る役）、受入検査を設計する役、方針を決める師匠 |

---

# 0. 文書について

## 0.1 位置づけと範囲

仕様書は「何を・どこまで・どう確かめるか」を定めた。本書は、その要求を満たす **構造・責務分担・実行モデル・主要な設計決定** を定める。コードの行レベルの設計（関数名や例外クラスの網羅）は書かない。構築者が迷う箇所、間違えると作り直しになる箇所、受入検査の設計に必要な箇所だけを規範にする。

本書が答える問い:

- どのプロセス・スレッド・接続が何を担い、どこで直列化するか（§6）。
- モジュールをどう切り、どの方向にだけ依存してよいか（§4）。
- ツールの応答を「予算内・レコードを切らない・精度メタデータつき」で作る共通の仕組みは何か（§7.2）。
- 編集・LSP・索引・ファイル監視が互いにどう噛み合うか（§7、§8）。
- M1 で最初に何を確かめ、何から作るか（§12）。

## 0.2 規範語

仕様書 §0.2 と同じ。MUST / SHOULD / MAY を使う。規範語の無い文は説明である。

## 0.3 章構成

| 章 | 内容 |
|---|---|
| 1 | 前提と制約（仕様書からの入力） |
| 2 | 全体像（コンテキストとコンテナ） |
| 3 | アーキテクチャ決定記録（ADR） |
| 4 | パッケージ構成とモジュール責務 |
| 5 | 中核ドメインモデル |
| 6 | 実行モデル（並行・ロック・接続・プロセス） |
| 7 | コンポーネント詳細設計 |
| 8 | 主要シーケンス |
| 9 | 横断関心事 |
| 10 | 品質戦略（テスト・ベンチ・CI） |
| 11 | パッケージングと配布 |
| 12 | 技術検証（スパイク）と M1 の着手順 |
| 13 | リスクと対策 |
| 付録 A | 候補パッケージの実態（一次情報） |
| 付録 B | 仕様書の要求 → 実現モジュール対応 |

---

# 1. 前提と制約

## 1.1 仕様書から受け取る制約

| 由来 | 制約 | 設計への影響 |
|---|---|---|
| NFR-PERF-1 | Fast Path p95 < 50 ms（ウォームアップ後、サーバー内時間） | 要求経路で LSP 往復を待たない設計（キャッシュと PaaD から答え、LSP は背景で更新）。Python のオーバーヘッドを測る（§12 S-9） |
| NFR-PERF-3 | 外部変更の反映 500 ms 以内 | 監視 → 差分再解析 → DB 更新 → LSP 通知を 1 本の非同期パイプラインにする（§7.6） |
| NFR-SAFE-4 / 第 10 章 | 編集の原子性、陳腐化検知、ジャーナル | 編集トランザクションを状態機械として 1 か所に実装（§7.7） |
| NFR-OPS-1 | `stdout` は JSON-RPC のみ | 起動直後に fd 1 を退避し、以後の fd 1 への書き込みを stderr へ向ける（ADR-3） |
| NFR-OPS-2 | 子プロセスを残さない | 子プロセスの台帳と終了手順を 1 か所に（§6.5） |
| NFR-OPS-3 | LSP 起動中もブロックしない | エンジン選択で「今答えられる最高水準」を返す（§7.8） |
| NFR-PORT-1 / 2 | 外部 DB・GPU 不要、Windows / macOS / Linux | SQLite と純 Python / wheel 配布の依存のみ。Windows を第一級で扱う（§9.5） |
| 第 13 章 | パス封じ込め、DB 読み取り専用、LLM 不使用、外部通信なし | 封じ込めはパス正規化を 1 関数に集約（§7.3）。公開 DB 接続は authorizer・`query_only`・文の許可リストの 3 重（§7.6） |
| 第 16 章 L-1〜L-9 | MIT、依存は寛容のみ、GPL 系は同梱しない | 依存は付録 A と仕様書付録 D の範囲。CI にライセンス検査（§10.5） |
| 仕様書 §5.6 / §5.7 | 予算内応答、レコードを切らない、トークン近似 | 出力を「レコード列 → パッカー → レンダラ」の共通経路にする（ADR-4、§7.2） |
| 仕様書 §5.5 / §8 | 精度メタデータ、ラダー | レコード単位で精度を持ち、パッカーが集計する（ADR-5） |

## 1.2 本書で固定する技術前提

| 項目 | 決定 | 根拠 |
|---|---|---|
| Python | 3.12 以上を要求（`requires-python = ">=3.12"`）。CI は 3.12 と 3.13 | `tomllib`（3.11+）、`asyncio` の成熟、型構文。3.11 を切る理由は保守対象を減らすため |
| 非同期基盤 | `asyncio`（MCP SDK が anyio 上で動くため、anyio 互換の書き方をする） | |
| 配布形態 | 純 Python パッケージ（`src/` レイアウト）。ネイティブ依存はすべて wheel で供給される既存パッケージに限る | NFR-PORT-1、L-2 |
| 新規のネイティブコード | 書かない（M1〜M3）。性能が足りない箇所が測定で判明したときに OD-1 (c) を再検討 | |
| ログ | 標準ライブラリ `logging` + JSON Lines フォーマッタ | 依存を増やさない |
| 設定 | 環境変数 > `.lucianke.toml`（読むだけ）> 自動検出（仕様書 §12.7） | |

## 1.3 スコープ外

- M2・M3 のコンポーネント（Git 時系列、データフロー、契約抽出、SMT）は **境界と置き場所** だけを定め、内部設計は各マイルストーン着手時に本書を改版して足す。
- IDE 拡張。HTTP 系トランスポートは提供しない（仕様書 §5.1、OD-22）。Lucianke は常にホストから子プロセスとして起動される stdio のコマンド実行型である。

---

# 2. 全体像

## 2.1 コンテキスト

```
┌──────────────────────────────┐   stdio (JSON-RPC 2.0)   ┌──────────────────────────────┐
│ コーディングエージェントのホスト │ ◄──────────────────────► │ lucianke serve（1 プロセス）   │
│ (Claude Code / Cline / Cursor) │                          │                              │
└──────────────────────────────┘                          │  ┌─ 子プロセス（言語ごと）─┐  │
                                                          │  │ pyright / ruff / vtsls │  │
        ┌─────────────────┐  読む・監視する・書く（編集のみ） │  │ rust-analyzer / gopls  │  │
        │ ワークスペース    │ ◄──────────────────────────────┤  └────────────────────────┘  │
        │ (作業ツリー, .git) │                                │  任意: ctags / rg（PATH 上）    │
        └─────────────────┘                                └──────────────┬───────────────┘
                                                                          │ 読み書き
                                                          ┌───────────────▼───────────────┐
                                                          │ <cache_root>/<ws_hash>/        │
                                                          │  paad.sqlite (WAL) / journal/  │
                                                          │  trees/ (任意) / logs/         │
                                                          └───────────────────────────────┘
```

- Lucianke はネットワークに出ない。外部との接点は stdio、ワークスペースのファイル、キャッシュディレクトリ、子プロセスの 4 つだけ。
- LSP サーバーは利用者の環境にあるものを起動する。Lucianke は同梱しない（L-4、仕様書 §12.3）。

## 2.2 プロセス内の構成（コンテナ図）

```
lucianke serve
│
├─ Transport        stdio 読み書き（退避した fd 1 を使う）。JSON-RPC の枠組みは MCP SDK
├─ McpAdapter       tools/list・tools/call・roots を SDK からツールレジストリへ橋渡し
├─ ToolPipeline     検証 → 文脈解決 → ハンドラ → パッカー → レンダラ → エンベロープ（全ツール共通）
├─ Services         ツールが使う内部 API 群（下の 7 つ）。ツールはサービスにだけ依存する
│   ├─ WorkspaceContext   ルート・環境・言語判定・封じ込め・設定
│   ├─ SyntaxEngine       構文木キャッシュ・骨格・シンボル・境界・ガード・アダプタ・構文パターン
│   ├─ LspPool            言語別 LSP の生涯・文書同期・診断・要求
│   ├─ Index (PaaD)       書き込みキュー・読み取りプール・公開クエリエンジン
│   ├─ EditTransaction    第 10 章の 8 段
│   ├─ EngineSelector     ラダー（どのエンジンで・どの精度で答えるか）
│   └─ GitLayer (M2)      リビジョン・仮想ツリー・履歴・共変更
├─ Watcher          ファイル監視イベント → Indexer
├─ IndexWriter      SQLite への唯一の書き手（専用スレッド）
└─ Lifecycle        起動・終了・子プロセス台帳・シグナル
```

---

# 3. アーキテクチャ決定記録（ADR）

各決定は「文脈 → 決定 → 捨てた案 → 帰結」の順。番号は以後の章から参照する。

### ADR-1 Python 3.12+、`src/` レイアウト、純 Python パッケージ

- 文脈: 仕様書 OD-1 の推奨。師匠の主力言語。候補ライブラリはすべて wheel で入る。
- 決定: `src/lucianke/` に本体、`tests/`、`bench/` を分ける。ネイティブコードは書かない。
- 捨てた案: Rust 本体（配布は楽だが M1 の速度を落とす）。Python + 自前 Rust 拡張（まだ必要が示されていない）。
- 帰結: p95 < 50 ms は Python のまま達成する設計が要る（ADR-6、ADR-7）。測って足りなければ OD-1 (c)。

### ADR-2 MCP SDK は低水準 API を使い、ツールのスキーマとエンベロープは自前で持つ

- 文脈: 仕様書は共通パラメータ、`additionalProperties: false`、`isError` 付きの通常結果、`structuredContent` の併用、`roots/list` 購読を求める。高水準の自動スキーマ生成では既定値や禁止プロパティの制御が効きにくい。
- 決定: SDK の低水準サーバー（`tools/list` / `tools/call` のハンドラ登録）を使い、入力スキーマは pydantic モデル（`extra="forbid"`）から生成して Lucianke 側のレジストリが保持する。結果の `content` と `structuredContent` と `isError` はパイプラインが組み立てる。
- 捨てた案: 高水準の関数デコレータ方式（速いが、エンベロープを全ツールに強制しにくい）。
- 帰結: SDK の該当 API の存在と挙動をスパイク S-1 で確かめる。SDK の版は固定する。

### ADR-3 `stdout` の退避と差し替えをプロセス起動の最初に行う

- 文脈: NFR-OPS-1。依存ライブラリや C 拡張が fd 1 に書く可能性を排除できない。
- 決定: 起動直後に `real_stdout = os.dup(1)` で退避し、`os.dup2(2, 1)` で fd 1 を stderr に重ねる。MCP のトランスポートには退避した fd から作ったバイナリストリームを渡す。`sys.stdout` も stderr 向けのラッパに差し替える。
- 捨てた案: `sys.stdout` の差し替えだけ（C レベルの書き込みを防げない）。
- 帰結: トランスポートに任意のストリームを渡せることをスパイク S-1 で確認。できなければ `sys.stdout` を退避 fd のテキストラッパに戻す形で代替する。

### ADR-4 出力は「レコード列 → パッカー → レンダラ」の 1 本道

- 文脈: 仕様書 §5.6（レコードを切らない、要約と `next_cursor`）、仕様書 §5.7（予算）、仕様書 §6（compact / json / dot / mermaid）。21 ツールがそれぞれ実装すると必ず揺れる。
- 決定: ツールハンドラは **優先順位つきのレコード列** を返すだけにする。パッカーが予算内に詰め、カーソルと要約を作り、レンダラが形式に直す。ハンドラは出力形式を知らない。
- 捨てた案: ハンドラが文字列を組み立てる（予算超過時の切り方がツールごとに違ってしまう）。
- 帰結: 全ツールの事後条件「`used_tokens_est ≤ max_tokens`」はパッカーの単体テストで一括して担保できる。

### ADR-5 精度（confidence）はレコードが持ち、メタデータは集計で作る

- 文脈: 仕様書 §5.5 の `mixed` / `confidence_breakdown`、仕様書 §8.3 の「ツール固有の最高水準」。
- 決定: 各レコードが `confidence` を持つ。パッカーが最低水準・内訳・`mixed` を計算し、ツールが宣言した「最高水準」と比べて `is_degraded` を決める。警告文はエンジン選択の結果から雛形で生成する。
- 帰結: ツールは精度の集計コードを書かない。

### ADR-6 要求経路で LSP を待たない。LSP は PaaD を更新する供給者

- 文脈: NFR-PERF-1 と NFR-OPS-3。LSP の往復は数十〜数百 ms になりうる。
- 決定: 読み取りツールはまず PaaD と構文木キャッシュから答える。LSP の結果が必要で未取得なら、(a) 短いタイムアウト（仕様書 §12.3 の値）で待って `exact` を返すか、(b) 待たずに `structural` で返すかを **ツールのエンジン選択ポリシー** で決める。既定は (a) を `definition` / `references` に、(b) を骨格・検索に適用する。LSP の `publishDiagnostics` と参照結果は到着し次第 PaaD に書く。
- 捨てた案: 常に LSP を同期で呼ぶ（p95 を守れない）。常に待たない（`exact` をいつまでも返せない）。
- 帰結: `relations` には `structural` 行と `exact` 行が共存し、同じ (source, target, kind) で `exact` が上書きする（仕様書 §9.4）。

### ADR-7 SQLite は「単一の書き手スレッド + 読み取り接続プール + 公開用の隔離接続」

- 文脈: 仕様書 §4.4、§9.5、§13.2。Python の `sqlite3` 接続はスレッドをまたいで共有しない。
- 決定: 書き込みは `IndexWriter` スレッドのキューに積み、1 接続で順に実行する。読み取りは小さな接続プール（既定 8）から借りて `asyncio.to_thread` で使う。`query_project_db` 用は別の読み取り専用接続（`mode=ro`、`query_only`、authorizer、progress handler）。
- 捨てた案: 接続を 1 本にして全部直列化（読み取りが書き込みに詰まる）。ORM（スキーマは手書き DDL が正本）。
- 帰結: `data_version`（§7.6）は書き手が更新し、読み手は参照だけ。

### ADR-8 カーソルはワークスペースの `data_version` を抱え、変われば失効

- 文脈: 仕様書 §5.6「基になるデータが変わっていれば `E_CURSOR_EXPIRED`」。
- 決定: カーソルは `{tool, args_hash, offset, data_version}` を base64url にしたもの。`data_version` は索引の書き込みトランザクションごとに増える（診断の更新では増やさない）。読み取り時に一致しなければ失効。
- 捨てた案: 影響ファイルの集合を持つ（正確だが複雑。M1 では保守的な全体版で足りる）。
- 帰結: 無関係なファイルの変更でもカーソルが失効しうる。仕様の範囲内（失効は常に安全側）。LSP 由来の `relations` 書き込み（§7.5.6）も `data_version` を進めるため、LSP の索引が進んでいる間はカーソルの失効が増える。ファイル変更時はそのファイルを `source_file_id` とする `relations` 行を `exact` も含めて全削除し `structural` を入れ直すので、編集直後は一時的に `structural` に下がり、LSP の再取得で `exact` に戻る。

### ADR-9 言語とフォーマットは `LanguageSupport` / `FormatAdapter` のプラグイン表で足す

- 文脈: Tier 1 が 10 言語、Tier 2〜4 が多数。言語ごとの分岐を各ツールに書くと保守できない。
- 決定: 言語 ID をキーに、文法・クエリ・境界規則・テストパス規則・LSP 定義・インデント既定をまとめた `LanguageSupport` を登録する。Tier 3〜4 は `FormatAdapter`（骨格・部分取得・置換）を登録する。ツールはこの表を通してだけ言語を知る。
- 帰結: 新言語の追加は表の 1 行と `.scm` クエリの追加で済む。

### ADR-10 降格はエンジン選択関数に集約する

- 文脈: 仕様書 §8.4 のラダーと警告文。
- 決定: `EngineSelector.select(file, capability)` が `(engine, confidence, lsp_status, warning)` を返す。ツールはこれに従ってエンジンを呼ぶだけで、ラダーの判断を書かない。
- 帰結: 降格の挙動を変えるときは 1 か所。

### ADR-11 編集は単一のトランザクション実装を全編集ツールが使う

- 文脈: 仕様書第 10 章。
- 決定: `EditTransaction.run(plan_builder)` が 8 段を実行する。編集ツールは **計画（hunk 列）を作る関数** だけを渡す。前提検証・構文検証・原子的書き込み・ジャーナル・再索引・診断差分はトランザクションが担う。
- 帰結: EP-1〜EP-9 はトランザクションのテストで一括して担保する。

### ADR-12 子プロセスはシェルを介さず、台帳で一元管理する

- 文脈: 仕様書 §12.5、§13.3。
- 決定: すべての子プロセスは `ProcessRegistry` 経由で起動し（argv 配列、最小の環境変数、作業ディレクトリ = ルート）、終了手順はこの台帳を舐める。Windows では新しいプロセスグループで起動し、`CTRL_BREAK` と `terminate` → `kill` の順に落とす。
- 帰結: NFR-OPS-2 の検査は台帳が空であることの確認になる。

### ADR-13 Tree-sitter の解析はスレッドプール、構文木はバイト列と組で LRU キャッシュ

- 文脈: 構文木のノードは元のバイト列が無いとテキストを取れない。Parser はスレッド安全でない。
- 決定: スレッドごとに Parser を持つプール。キャッシュのキーは `(workspace_id, path, sha256)`、値は `(source_bytes, tree, line_index)`。上限はファイル数とバイト数の両方（仕様書 §12.8）。
- 帰結: 同じ版のファイルは 1 回しか解析しない。編集後は新しい `sha256` で再解析（増分解析は MAY）。

### ADR-14 位置の単位変換は LSP クライアントの境界でだけ行う

- 文脈: 仕様書 §5.10、§12.2。
- 決定: Lucianke 内部の位置は「0 始まりの行、0 始まりのコードポイント列」を使い、外部出力の直前で 1 始まりに、LSP との送受信で UTF-16 または UTF-8 単位に変換する。変換には行ごとのテキストを使い、`LineIndex` がバイト・コードポイント・UTF-16 の相互変換表を持つ。
- 帰結: 変換の誤りは `LineIndex` の単体テスト（非 ASCII・サロゲートペア・結合文字）で捕まえる。

### ADR-15 M1 で LSP を同期で使うのは 5 要求だけ

- 文脈: 範囲を絞らないと LSP 連携が肥大する。
- 決定: M1 の LSP 要求は `initialize`、`textDocument/definition`、`textDocument/references`、`callHierarchy/*`、`textDocument/rename`（`prepareRename` 含む）、`textDocument/codeAction`（`resolve` 含む）、`textDocument/hover`（型展開用）、および通知（`didOpen` / `didChange` / `didClose` / `didChangeWatchedFiles`、受信 `publishDiagnostics` / `$/progress`）。`workspace/symbol` と `typeHierarchy` は M2。
- 帰結: `search_workspace_symbols` は PaaD（FTS5）で答え、LSP を使わない。

---

# 4. パッケージ構成とモジュール責務

## 4.1 ディレクトリ

```
Lucianke/
├─ pyproject.toml            # ビルド・依存・extras・エントリポイント
├─ LICENSE                   # MIT
├─ THIRD_PARTY_NOTICES.md    # 生成物（§10.5）
├─ src/lucianke/
│  ├─ __init__.py            # __version__
│  ├─ __main__.py            # `lucianke` CLI: serve | index | bench | doctor
│  ├─ app.py                 # 合成ルート: 設定読込 → サービス生成 → 起動・終了
│  ├─ bootstrap_io.py        # ADR-3: fd 1 の退避と差し替え（最初に import される）
│  ├─ config.py              # 設定モデル（環境変数 / .lucianke.toml / 既定）と cache_root
│  ├─ logging_setup.py       # stderr + JSON Lines ファイル、ローテーション
│  ├─ lifecycle.py           # 起動順、終了手順、ProcessRegistry、シグナル
│  │
│  ├─ core/                  # 依存なしの中核型。どこからでも import 可、core は他を import しない
│  │  ├─ model.py            #   Urn, TargetRef, Range, Position, SymbolRecord, Kind, Confidence, Tier, Capability
│  │  ├─ urn.py              #   URN の構文解析・整形・緩和（仕様書第 7 章）
│  │  ├─ errors.py           #   LuciankeError(code, message, details, candidates)
│  │  ├─ records.py          #   Record, RecordGroup, Summary（ADR-4）
│  │  ├─ textpos.py          #   LineIndex（ADR-14）、改行・BOM・インデントの検出
│  │  └─ limits.py            #   時間・件数の上限定数（§6.4）。仕様書 §12 の値をここにだけ置く
│  │
│  ├─ mcp/                   # MCP との境界
│  │  ├─ server.py           #   SDK の低水準サーバーを組み立て、tools/list・tools/call・roots を接続
│  │  ├─ registry.py         #   ToolSpec（名前・pydantic 入力・既定 max_tokens・graph 可否・writes・最高水準・M）
│  │  ├─ pipeline.py         #   検証 → 文脈解決 → ハンドラ → パッカー → レンダラ → エンベロープ
│  │  ├─ envelope.py         #   ResponseEnvelope / ResolutionMeta の組み立て（json・compact のヘッダ）
│  │  ├─ budget.py           #   TokenEstimator、Packer、CursorCodec
│  │  └─ render/             #   compact.py（§6.1 の文法）、json_out.py、graph.py（dot / mermaid）
│  │
│  ├─ workspace/
│  │  ├─ context.py          #   WorkspaceContext: ルート登録、マーカー検出、Python 環境、workspace_id
│  │  ├─ paths.py            #   封じ込め（正規化・実体パス・ルート配下検査）。唯一のパス検証点
│  │  ├─ languages.py        #   拡張子・シバン・モードライン → language_id、tier
│  │  ├─ watcher.py          #   watchfiles ループ → デバウンス → Indexer へ
│  │  └─ settings_file.py    #   .lucianke.toml の読み込み（書かない）
│  │
│  ├─ syntax/
│  │  ├─ support.py          #   LanguageSupport / FormatAdapter の Protocol と登録表（ADR-9）
│  │  ├─ parsers.py          #   Parser プール、TreeCache（ADR-13）
│  │  ├─ queries/            #   言語ごとの .scm（tags / skeleton / imports / guards）
│  │  ├─ symbols.py          #   構文木 → SymbolRecord 列（修飾名・種別・修飾子・範囲・doc 要約）
│  │  ├─ skeleton.py         #   骨格レコードの生成（折りたたみ・予算優先順位の付与）
│  │  ├─ boundaries.py       #   定義ノードの範囲規則（仕様書 §10.8）
│  │  ├─ guards.py           #   呼び出し箇所のガード条件抽出
│  │  ├─ imports.py          #   import 文 → モジュールパス → ワークスペース内ファイル（structural 解決）
│  │  ├─ structural.py       #   ast-grep-py の薄いラッパ（検索・書き換え案）
│  │  ├─ heuristic.py        #   Tier 4: ctags（あれば）/ ブロック推定 / 単語境界 grep
│  │  └─ adapters/           #   ipynb.py, xml_.py, json_.py, yaml_.py, toml_.py, dot_.py, mermaid.py
│  │
│  ├─ lsp/
│  │  ├─ servers.py          #   LspServerDef（言語 → コマンド探索・初期化オプション・位置単位の既定）
│  │  ├─ client.py           #   JSON-RPC over pipes、要求・通知、タイムアウト、位置変換の境界（ADR-14）
│  │  ├─ pool.py             #   言語 × ワークスペースごとの状態機械（standby/starting/indexing/ready/unavailable）
│  │  ├─ documents.py        #   didOpen/didChange/didClose、版番号、LRU 200
│  │  ├─ diagnostics.py      #   publishDiagnostics の受信・版待ち（DiagnosticsWaiter）・Index への反映
│  │  └─ features.py         #   definition / references / call_hierarchy / rename / code_actions / hover の呼び出しと PaaD への書き込み
│  │
│  ├─ index/                 # PaaD
│  │  ├─ schema.sql          #   仕様書 §9.2 の DDL（正本はそのコピー。差分はテストで検出）
│  │  ├─ connection.py       #   接続の開き方（WAL、pragmas、ro URI）、ReaderPool
│  │  ├─ writer.py           #   IndexWriter スレッド、ジョブキュー、data_version
│  │  ├─ ingest.py           #   ファイル単位の取り込み（files/symbols/relations/diagnostics の差分更新）
│  │  ├─ queries.py          #   ツールが使うパラメータ化クエリ（名前つき）
│  │  ├─ public.py           #   query_project_db 用: 文の検査、authorizer、progress handler、行数上限
│  │  └─ migrate.py          #   スキーマ版管理
│  │
│  ├─ edit/
│  │  ├─ transaction.py      #   EditTransaction（8 段の状態機械、ADR-11）
│  │  ├─ plan.py             #   Hunk、Plan、WorkspaceEdit → Plan 変換
│  │  ├─ normalize.py        #   new_code の正規化（仕様書 §10.9）
│  │  ├─ syntax_check.py     #   メモリ上適用 → 再解析 → 新規エラーノード判定
│  │  ├─ writer.py           #   原子的書き込み、ジャーナル、改行・BOM・モードの保持、Windows の再試行
│  │  ├─ verify.py           #   診断差分（仕様書 §10.7）
│  │  └─ lock.py             #   ワークスペース単位の書き込みロック
│  │
│  ├─ engine/
│  │  └─ selector.py         #   EngineSelector（ADR-10）、警告文の雛形
│  │
│  ├─ git/                   # M2。M1 ではリビジョン解決と blob 取得（T-02/T-05 の revision 対応）だけ
│  │  ├─ repo.py             #   dulwich ラッパ: 参照解決、blob、ツリー差分、log --follow 相当
│  │  ├─ vtree.py            #   revision 指定時の仮想ツリー
│  │  ├─ history.py          #   シンボル単位の変更検出（M2）
│  │  └─ cochange.py         #   共変更結合の集計（M2）
│  │
│  ├─ verify/                # M2（paths）/ M3（contracts）。境界のみ定義（§7.10）
│  │  ├─ paths.py             #   M2: 経路探索（再帰 CTE）
│  │  ├─ dataflow.py          #   M2: 手続き内 Def-Use → dataflows
│  │  ├─ contracts/extract.py #   M3: 決定論的契約抽出
│  │  ├─ exprlang.py          #   M3: 制約式の構文解析と SMT-LIB 化
│  │  ├─ ssa.py               #   M3: 関数本体の SSA 化
│  │  └─ solver.py            #   M3: SMT ソルバ呼び出し（タイムアウト付き）
│  │
│  ├─ tools/                 # 21 ツール。1 ファイル 1 ツール。薄い: 引数モデル + レコード生成
│  │  ├─ _base.py            #   ToolContext、共通ヘルパ（TargetRef 解決の呼び出し、ファイル列挙）
│  │  ├─ t00_configure_workspace.py … t20_query_project_db.py
│  │  └─ __init__.py         #   レジストリへの登録
│  │
│  └─ cli/                   # doctor（環境診断）、index（事前索引）、bench の入口
│
├─ tests/
│  ├─ unit/                  # core, budget, textpos, boundaries, normalize, writer, urn …
│  ├─ contract/              # 仕様書 §11 の事後条件を T-nn ごとに
│  ├─ fixtures/              # F-POLY（合成）と小さな Python / TS プロジェクト
│  ├─ e2e/                   # 実 LSP（pyright / vtsls）と実 MCP クライアントで
│  └─ invariants/            # stdout 純度、子プロセス 0、sha256 不変
└─ bench/
   ├─ fixtures.toml          # F-PY / F-TS の SHA
   ├─ scenarios/             # S1〜S4 の台本（従来手法と Lucianke）
   └─ run_bench.py
```

## 4.2 依存の方向（MUST）

```
tools ─► services (workspace, syntax, lsp, index, edit, engine, git) ─► core
mcp   ─► tools（レジストリ経由）, core
app / lifecycle ─► すべて（合成ルート）
```

- `core` は他のどのパッケージも import しない（MUST）。
- `tools` は `mcp` を import しない（ツールは MCP を知らない）。`mcp.render` も知らない。
- サービス同士の依存は次の向きに限る: `edit → syntax, index, lsp, workspace`、`lsp → index, workspace`、`syntax → workspace`、`engine → lsp, syntax, workspace`、`git → workspace`、`index → (なし。core のみ)`。逆向きが必要になったらイベント（コールバック登録）で解く。
- 依存方向の検査は `tests/unit/test_import_graph.py` で機械的に行う（import 文の静的走査）。

## 4.3 モジュール責務の要点

| モジュール | 担う | 担わない |
|---|---|---|
| `mcp/pipeline.py` | 全ツール共通の前後処理。`LuciankeError` の捕捉とエンベロープ化。`timing_ms` | ツール固有の判断 |
| `mcp/registry.py` | `ToolSpec` の表。`tools/list` の JSON Schema 生成（pydantic の `model_json_schema` に共通パラメータを合成） | 実行 |
| `mcp/budget.py` | トークン推定、予算内詰め込み、要約、カーソル | レコードの内容 |
| `mcp/render/compact.py` | `Record.lines` を §6.1 の文法で連結し、ヘッダ・警告・トレーラを付ける | 行の中身の生成（ツールと syntax が作る） |
| `workspace/paths.py` | 相対化・正規化・実体解決・ルート配下検査。失敗は `E_INVALID_ARGUMENT` | ファイルの読み書き |
| `syntax/symbols.py` | 構文木 → `SymbolRecord`。修飾名・種別・範囲・選択範囲・修飾子・doc 要約・`body_sha256` | DB 書き込み |
| `index/ingest.py` | 1 ファイルの差分取り込みを 1 トランザクションで。`data_version` を進める | 構文解析 |
| `lsp/pool.py` | 状態機械・再起動・バックオフ・待機解放 | 要求の意味 |
| `lsp/features.py` | 各要求の呼び出し、結果の `SymbolRecord` / `relations` 化、PaaD への書き込み要求 | 直列化形式 |
| `edit/transaction.py` | 8 段の順序と失敗時の不変条件 | hunk の内容 |
| `engine/selector.py` | ラダーと警告文 | エンジンの実行 |
| `tools/t*.py` | 引数モデル、サービス呼び出しの順序、レコードの優先順位付け、ツール固有の kv 行 | 予算・形式・精度集計・エラーの直列化 |

---

# 5. 中核ドメインモデル

`core/model.py` の主要型。すべて不変（`frozen=True` の dataclass）で、ツール・サービス間の受け渡しに使う。

```python
class Confidence(StrEnum):            # 仕様書 §8.3。順序比較できる（exact が最高）
    EXACT = "exact"; STRUCTURAL = "structural"; HEURISTIC = "heuristic"; LEXICAL = "lexical_fallback"

class Tier(IntEnum): T1 = 1; T2 = 2; T3 = 3; T4 = 4

class Capability(StrEnum):            # エンジン選択の単位（ADR-10）
    SKELETON, SEARCH, DEFINITION, BODY, REFERENCES, IMPLEMENTATIONS, CALL_HIERARCHY,
    DIAGNOSTICS, RENAME, CODE_ACTION, STRUCTURAL_EDIT, SYMBOL_EDIT

@dataclass(frozen=True)
class Position:  line: int; col: int          # 0 始まり・コードポイント（内部表現。ADR-14）
@dataclass(frozen=True)
class Range:     start: Position; end: Position

@dataclass(frozen=True)
class Urn:                                     # 仕様書第 7 章
    workspace_id: str; path: str; selector: Selector | None
    # Selector = CodeSel(qualified, arity, sighash) | DataSel(kind, expr) | CellSel(n, qualified) | TagSel(name, line) | LexSel(name, line)

@dataclass(frozen=True)
class TargetRef:                               # 仕様書 §5.3
    urn: str | None = None; path: str | None = None; symbol_name: str | None = None
    line: int | None = None; character: int | None = None; variable_name: str | None = None

@dataclass(frozen=True)
class SymbolRecord:                            # 仕様書 §6.2 と同じ形
    urn: Urn; path: str; name: str; qualified_name: str; kind: Kind
    modifiers: tuple[str, ...]; range: Range; selection: Range | None
    signature: str | None; return_type: str | None; doc_summary: str | None
    declared_in: str; confidence: Confidence; scip_symbol: str | None = None

@dataclass(frozen=True)
class FileInfo:
    file_id: int; workspace_id: str; path: str; language: str; tier: Tier
    sha256: str; line_count: int; byte_size: int; is_test: bool; confidence: Confidence

@dataclass(frozen=True)
class EngineChoice:                            # ADR-10 の結果
    engine: str; confidence: Confidence; lsp_status: str; lsp_progress: int | None; warning: str | None
```

`core/records.py`（ADR-4）:

```python
@dataclass(frozen=True)
class Record:
    kind: str                 # "file-block" | "symbol" | "ref" | "diag" | "tree" | "path" | "code" | "kv" | "note" | "row"
    key: str                  # 安定キー（ページ跨ぎの重複排除・並び替えに使う）
    priority: int             # 小さいほど先に詰める。ツールが付ける
    lines: tuple[str, ...]    # compact 表現（§6.1 の 1 ブロック）。レンダラは連結するだけ
    data: Mapping[str, Any]   # json 表現（data 配列の 1 要素）
    confidence: Confidence
    group: str | None = None  # 要約の単位（例: ファイルパス）
    tokens: int | None = None # パッカーが推定して埋める
    shrink: Callable[[int], "Record | None"] | None = None
                              # 任意。残り予算で収まる縮退版（T-02 の doc:→urn:→非公開→入れ子の順の省略）を返す。
                              # 収められなければ None。パッカーは「入らないレコード」にこれがあれば一度だけ呼ぶ

@dataclass(frozen=True)
class ToolOutput:
    records: Sequence[Record]           # 優先順位順に並べて渡す
    head_kv: Sequence[tuple[str, str]]  # compact のヘッダ直後に出す kv 行（target:, rows: など）。常に含める
    data_static: Mapping[str, Any]      # json の data のうち、レコード配列以外（totals, target, summary など）
    max_confidence: Confidence          # このツールが到達しうる最高水準（ADR-5）
    engine: EngineChoice                # 代表のエンジン選択（lsp_status, warning の元）
    page_unit: str                      # 1 レコードの意味（事後条件の文言と一致させる）
    summarize: Callable[[Sequence[Record]], str] | None = None   # 残りの要約文（# summary: 行）
```

`core/errors.py`:

```python
class LuciankeError(Exception):
    code: ErrorCode          # 仕様書 §5.8 の 14 種
    message: str
    details: Mapping[str, Any]
    candidates: Sequence[Mapping[str, Any]]   # E_TARGET_AMBIGUOUS 用
    partial: ToolOutput | None               # E_TIMEOUT の部分結果
```

---

# 6. 実行モデル

## 6.1 スレッドとタスク

| 実行単位 | 数 | 役割 | 備考 |
|---|---|---|---|
| メインスレッド / asyncio ループ | 1 | MCP の読み書き、ツールの非同期ハンドラ、LSP クライアントの I/O、監視イベントの受け取り | ブロックする処理を置かない（MUST） |
| 読み取り用スレッドプール | 既定 8 | SQLite 読み取り、Tree-sitter 解析、ファイル読み込み、ハッシュ計算 | `asyncio.to_thread` |
| `IndexWriter` スレッド | 1 | SQLite の全書き込み | ジョブキュー。1 ジョブ = 1 トランザクション |
| 編集用の直列区間 | ワークスペースごとに 1 | `EditTransaction` の段 3〜7 | `asyncio.Lock`。待ち上限 5,000 ms |
| 子プロセス | 言語 × ワークスペースごと | LSP | 標準入出力をパイプで非同期に読む |

- CPU を使う処理（解析・ハッシュ・大きな JSON 生成）はループ上で行わない。p95 を守る鍵は「ループを空けること」である。
- 初回索引（数千ファイル）はプールで並列に解析し、書き込みはキューで直列化する。**初回索引が重く時間がかかることは許容する**（師匠 2026-10-29。仕様書 §14.4）。索引中もツールは降格応答で答え続ける設計（ADR-6、§7.8）であり、初回の所要を縮める工夫（プロセスプール、`lucianke index` の事前実行）は M1 では必須にしない。

## 6.2 SQLite 接続（ADR-7）

| 接続 | 開き方 | 使い手 | 備考 |
|---|---|---|---|
| 書き込み | 通常。`journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=5000` | `IndexWriter` のみ | 起動時にスキーマ版を確認・移行 |
| 読み取りプール | 同じファイルを通常モードで開くが、アプリ側で書き込み文を発行しない。`busy_timeout=1000` | ツール、`EngineSelector`、`EditTransaction` の読み | プールから借りて返す。スレッド境界を越えて保持しない |
| 公開クエリ | `file:...?mode=ro` の URI、`PRAGMA query_only=ON`、`set_authorizer`、`set_progress_handler`、行数上限 | `query_project_db` | 失敗が他接続に波及しないよう独立。1 要求ごとに新しく開いてもよい（MAY。開く費用を測る） |

- WAL のチェックポイントは `IndexWriter` が暇なときと終了時に行う。
- `data_version`: `_meta` 内部テーブルの整数。`files` / `symbols` / `relations` を変える書き込みトランザクションの最後に `+1`（LSP 由来の `relations` 更新を含む）。`diagnostics` だけの更新では進めない。読み手は `SELECT` で読む。カーソルとキャッシュの無効化に使う（ADR-8）。
- 内部テーブルは `_meta`（`schema_version`, `data_version`, `edit_seq`）と `_hover_cache`（LSP hover の結果。キー: `urn` + `sha256`）。名前が `_` で始まるものは公開クエリから見えない（仕様書 §9.1）。

## 6.3 ロックと順序

| ロック | 範囲 | 守るもの |
|---|---|---|
| ワークスペース書き込みロック | `EditTransaction` の段 3〜7 | 同時編集、編集中の読み取り（読み取りは解放を待つ。仕様書 §5.9） |
| `TreeCache` の LRU ロック | 取り出し・挿入 | 二重解析の抑止（同じキーの解析中は待つ） |
| `LspPool` の状態遷移ロック | 言語ごと | 二重起動 |
| `IndexWriter` のキュー | 常時 | 書き込み順序（投入順） |

デッドロックを避ける順序: 書き込みロック → `TreeCache` → LSP 要求。逆順に取らない（MUST）。`IndexWriter` はロックを取らない（キューに積むだけ）。例外として、編集トランザクションの段 2（計画）で LSP に `rename` / `codeAction` を要求するのはロック取得前であり、その結果（`WorkspaceEdit`）が古くなっていないことは段 3 の前提検証（`sha256` 比較）が守る（§7.7.1）。

## 6.4 時間の上限

仕様書 §12.3・§12.8 の値をそのまま定数表 `core/limits.py` に置き、散在させない。超過時の挙動（降格して続行 / `E_TIMEOUT` に部分結果）は各ツールの `ToolSpec` に書く。

## 6.5 起動と終了（`lifecycle.py`）

起動順:

1. `bootstrap_io`（fd 1 の退避・差し替え）→ 2. ログ → 3. 設定 → 4. キャッシュディレクトリ → 5. `IndexWriter` 起動とスキーマ移行 → 6. MCP サーバーの `initialize` 受付 → 7. `roots/list` → `WorkspaceContext` 登録 → 8. 差分索引ジョブ投入（背景）→ 9. 監視開始。

LSP はここでは起動しない。

終了順（`stdin` EOF / SIGTERM / SIGINT / Windows の `SIGBREAK`）:

1. 新規 `tools/call` を `E_UNSUPPORTED`（`details.reason: "shutting_down"`）で拒否 → 2. 実行中の編集トランザクションの完了またはロールバックを最大 5 s 待つ → 3. 監視停止 → 4. `LspPool.shutdown_all()`（`shutdown` → `exit` → 500 ms → `terminate` → `kill`）→ 5. `ProcessRegistry` に残った子を `kill` → 6. `IndexWriter` のキューを空にしてチェックポイント → 7. 終了コード 0。

- Windows では `loop.add_signal_handler` が使えないため、`signal.signal` でフラグを立て、ループ側の監視タスクが拾う。
- 親プロセスの異常終了は `stdin` の EOF で検知する（MCP SDK のトランスポート終了）。

---

# 7. コンポーネント詳細設計

## 7.1 MCP アダプタとツールパイプライン

### 7.1.1 `ToolSpec`（`mcp/registry.py`）

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str                          # "get_file_skeleton"
    tid: str                           # "T-02"
    args_model: type[BaseModel]        # extra="forbid"。共通パラメータは CommonArgs を継承
    handler: Callable[[ToolContext, BaseModel], Awaitable[ToolOutput]]
    default_max_tokens: int
    allows_graph: bool                 # dot / mermaid 可否（仕様書 §6.4）
    writes: bool                       # カテゴリ D
    max_confidence: Confidence         # ツール固有の最高水準（§8.3）
    milestone: str                     # "M1" | "M2" | "M3"
    accepts_revision: bool             # False なら revision を無視して warnings
    page_unit: str
```

- `tools/list` は `args_model.model_json_schema()` に共通パラメータの定義を合成し、`additionalProperties: false` を付けて返す。
- 未実装のマイルストーンのツールも `tools/list` には載せる（エージェントが存在を知れる）。呼ばれたら `E_UNSUPPORTED`（`details.reason: "not_in_milestone"`, `details.milestone`）。

### 7.1.2 パイプライン（`mcp/pipeline.py`）

```
tools/call(name, arguments)
  1. spec = registry[name]                      無ければ JSON-RPC エラー（プロトコル違反）
  2. args = spec.args_model.model_validate(arguments)   失敗 → E_INVALID_ARGUMENT（details.param）
  3. ctx  = ToolContext(workspace, revision, format, budget, cursor, request_id, started_at)
       - workspace_id の解決（T-00 手順 7 の順序）  → E_WORKSPACE_NOT_CONFIGURED
       - revision の解決（WORKING_TREE / STAGED / HEAD / 名前 / SHA）→ E_REVISION_NOT_FOUND
       - format の可否（allows_graph）、max_tokens ≥ 200、writes なら revision == WORKING_TREE
       - cursor があれば復号し data_version を照合 → E_CURSOR_EXPIRED
  4. out  = await spec.handler(ctx, args)         LuciankeError はここで捕捉
  5. page = Packer.pack(out, ctx.budget, ctx.cursor_offset)   レコードを詰め、残りを要約、next_cursor
  6. meta = ResolutionMeta.from(page, out.max_confidence, out.engine)
  7. text = Renderer[ctx.format].render(spec, out, page, meta)
  8. result = CallToolResult(content=[text], structuredContent=(json のとき), isError=False)
  例外: LuciankeError → envelope(ok=false, error, meta) を同じ経路で描画し isError=True
        その他の例外 → ログに stack、E_INTERNAL（details.log_ref）
```

- `timing_ms` は 1 の直前から 8 の直前まで。
- ハンドラは `ctx.reader()`（読み取り接続）、`ctx.services`（各サービス）、`ctx.resolve_target(ref)`（仕様書 §5.3）を使う。

### 7.1.3 `ToolContext.resolve_target`（仕様書 §5.3）

1. `urn` → `index.queries.symbol_by_urn`。無ければ緩和照合（`~sighash`・`/arity` を外す）→ 1 件なら `warnings` に記録、複数なら曖昧。
2. `path + symbol_name` → セレクタ接頭辞（`xpath:` 等）ならその場で `DataTarget` を返す。そうでなければ `qualified_name` の末尾一致を `symbols` で検索。0 件 → `E_TARGET_NOT_FOUND`、2 件以上 → `E_TARGET_AMBIGUOUS`（候補 10 件）。
3. `path + line (+character)` → `symbols` で範囲を含む最内の行。
4. `revision` が `WORKING_TREE` 以外なら、`symbols` ではなく仮想ツリーの一時索引（§7.9）を引く。
5. 監視がポーリングモード（§7.3.6）のときは、解決前に対象ファイルの `sha256` を `files` と照合し、違っていれば先に再取り込みを同期で行う（遅延検証）。
6. ファイル内容の取得は常に `ToolContext.file_source(rel) -> bytes` を通す。`revision` が `WORKING_TREE` なら作業ツリー、それ以外なら仮想ツリー（§7.9.1）から読む。ツールが `open()` を直接呼ぶことはない（MUST）。

## 7.2 出力パイプライン

### 7.2.1 `TokenEstimator`

- 文字種別の係数で推定する: ASCII 英数字と空白は 1/4 トークン、ASCII 記号は 1/2、全角・漢字・かなは 1/1.5、その他は 1/2（係数は `tokens.toml` に置き、ベンチで `o200k_base` と比べて ±10% に調整する。§10.4）。
- 1 レコードの推定は `lines` の合計。ヘッダ・警告・トレーラも同じ推定器で数える。

### 7.2.2 `Packer`

```
pack(out, budget, offset):
  reserved = est(header) + est(head_kv) + est(worst-case trailer)
  remaining = budget - reserved
  included = []
  for rec in out.records[offset:]:
      t = est(rec)
      if t > remaining and rec.shrink is not None:
          rec = rec.shrink(remaining)              # 縮退版を一度だけ試す（T-02 のファイル内省略）
          t = est(rec) if rec is not None else None
      if rec is None or t > remaining: break
      included.append(rec); remaining -= t
  rest = out.records[offset + len(included):]
  if rest: next_cursor = CursorCodec.encode(tool, args_hash, offset + len(included), data_version)
           summary = out.summarize(rest) or default_summary(rest)   # group ごとの件数
  meta: min confidence, breakdown, mixed  ← included から集計
  return Page(included, next_cursor, summary, total_estimate=len(out.records))
```

- 1 件も入らないときは `included = []`、`next_cursor` あり、`warnings` に理由（仕様書 §5.6 の 4）。
- `records` は遅延列でもよい（巨大な参照一覧をすべて具体化しないため、ツールは生成器を渡してよい。`total_estimate` は別途 `COUNT` で与える）。

### 7.2.3 `CursorCodec`

`base64url(json({"t": tool, "a": sha1(args 正規化 JSON)[:12], "o": offset, "v": data_version, "w": workspace_id}))`。復号時に `t`・`a`・`w` が一致しなければ `E_INVALID_ARGUMENT`、`v` が現在と異なれば `E_CURSOR_EXPIRED`。

### 7.2.4 レンダラ

| 形式 | 入力 | 出力 |
|---|---|---|
| compact | ヘッダ（`# <tool> ok conf= lsp= rev= ws= ~used/max tok`）、`! ` 警告（降格時）、`head_kv`、`Record.lines` の連結、`# summary:`、`# more:` | 文字列 |
| json | `ResponseEnvelope`（`data = data_static + {records の配列名: [rec.data]}`） | 文字列 + `structuredContent` |
| dot / mermaid | `out.data_static["edges"]` と `records` のうち `kind in {"tree", "path", "ref", "row"}` | 文字列。ヘッダはコメント |

- `ToolOutput.head_kv` は常に予算内に含める（短い kv 行だけを置く）。編集ツールの `diff` のように長くなりうるものは `Record(kind="code")` として渡し、パッカーの切り詰め対象にする。
- compact の各行種（`symbol-block`, `ref-line`, `diag-line`, `tree-line` …）を組み立てる関数は `mcp/render/compact.py` に **行種ごとに 1 つ** 置き、ツールと `syntax` はそれを呼んで `Record.lines` を作る。文法（仕様書 §6.1）に適合するかは、この関数群の単体テストで EBNF 由来のパーサに通して検査する（§10.2）。

---
## 7.3 Workspace Context（workspace/）

ワークスペースの境界・環境・言語・設定・外部変更の検知を担う。他のサービスは「どのファイルが対象で、どの言語で、どこまで触ってよいか」をすべてここから受け取る。

### 7.3.1 ルートとワークスペース ID

```python
@dataclass(frozen=True)
class Workspace:
    workspace_id: str            # 仕様書 §7.2 の文字集合 [A-Za-z0-9._-]+
    root: Path                   # realpath 済みの絶対パス
    workspace_hash: str          # sha1(normcase(root))[:12]。<cache_root>/<workspace_hash>/ に使う
    sub_projects: tuple[SubProject, ...]   # (相対パス, マーカー名)
    python_env: PythonEnv | None
    exclude_globs: tuple[str, ...]
    default_base_ref: str
    settings: Settings           # §7.3.5 の合成結果

class WorkspaceContext:
    def register_roots(self, paths: Sequence[str], *, source: Literal["roots_list", "tool", "auto"]) -> list[Workspace]
    def resolve(self, workspace_id: str | None, hint_path: str | None) -> Workspace   # T-00 手順 7 の順序
    def active(self) -> Workspace
    def set_active(self, workspace_id: str) -> None
    def list(self) -> list[Workspace]
```

手順（`register_roots`）:

1. 各パスを realpath に正規化し、存在しないディレクトリが 1 つでもあれば全体を `E_INVALID_ARGUMENT` にして登録を変えない（T-00 の規約）。
2. `workspaces` テーブルに同じ `root_path` の行があれば、その `workspace_id` を再利用する（セッションをまたいで URN を安定させるため。MUST）。無ければルートのディレクトリ名から ID を作り、衝突したら `-2`, `-3` を付す。
3. ルート直下から深さ 6 までマーカー（`pyproject.toml`, `uv.lock`, `poetry.lock`, `package.json`, `Cargo.toml`, `go.mod`, `.git`）を走査し `sub_projects` に記録する。サブプロジェクトは独立したワークスペースにはしない（登録されたルートだけがワークスペース）。
4. `IndexWriter` に `workspaces` 行の upsert を積み、初回の差分索引ジョブ（§7.6.5）を投入して `job_id` を返す。

`resolve` の順序は T-00 手順 7 のとおり: ①引数のパスを含む登録済みルート → ②アクティブ → ③パスから上方向に最も近いマーカーを持つディレクトリ（自動登録し `warnings`）→ いずれも無ければ `E_WORKSPACE_NOT_CONFIGURED`（`details.known_workspaces[]`）。

受入で確かめる点:

- 同じルートを 2 セッション連続で登録したとき `workspace_id` が同一である。
- 存在しないパスを含む `set_roots` の前後で `workspaces` テーブルの行集合が同一である。

### 7.3.2 パスの封じ込め（paths.py）

仕様書 §13.1 の唯一の実装点。**ファイルに触るすべての経路がここを通る**（MUST）。`FileInfo.path` と URN の `path` は必ずこの関数の出力である。

```python
def confine(ws: Workspace, user_path: str, *, for_write: bool = False) -> RelPath:
    # 1. NUL・制御文字を拒否
    # 2. 区切りを '/' に統一。絶対パスならそのまま、相対なら ws.root と結合
    # 3. os.path.realpath でシンボリックリンクと '..' を解決
    # 4. normcase（Windows / macOS の既定は大文字小文字を区別しない）した上で、
    #    結果が ws.root と等しいか、ws.root + sep で始まることを検査
    # 5. for_write のとき: '.git/' 配下、exclude_globs 一致、秘密ファイル（§7.3.3）を拒否（仕様書 §13.4）
    # 6. ws.root からの相対パス（POSIX 区切り）を返す
```

失敗はすべて `E_INVALID_ARGUMENT`（`details.param` に引数名、`details.reason` に `outside_root` / `symlink_escape` / `git_dir` / `excluded`）。LSP から返る `file://` URI も受信時に `confine` を通し、ルート外なら外部シンボル（`lk://_ext/...`、§7.4.3）として扱う。

受入で確かめる点:

- `../../etc/passwd`、ルート外を指すシンボリックリンク、別ドライブの絶対パスが `E_INVALID_ARGUMENT` になる。
- ルート配下の絶対パス（Windows ではドライブ文字の大小が違っても）が相対パスに正規化される。

### 7.3.3 言語判定と Tier

```python
def detect(rel: RelPath, head: bytes) -> Detected:   # Detected(language_id, tier, index_content: bool)
```

1. 先頭 8 KB に NUL があれば `language_id="binary"`, `tier=4`, `index_content=False`。
2. 拡張子 → `syntax/support.py` の登録表（`LanguageSupport.extensions` と `FormatAdapter.extensions`）。
3. 拡張子で決まらなければシバン（`#!…python`, `node`, `sh`）、モードライン（`-*- mode: … -*-`, `vim: ft=`）。
4. `.gitattributes` の `linguist-language` を読んでもよい（MAY）。
5. いずれでも決まらなければ `unknown`、`tier=4`。
6. 秘密らしきファイル（仕様書 §13.5 のパターン）は言語を判定したうえで `index_content=False`。`files` 行は作るが本文は読まない。

`is_test(rel)` は仕様書 §9.3 の既定規則（`test/`, `tests/`, `*_test.*`, `*.spec.*`, `*.test.*`, `test_*.py`）に `LanguageSupport.test_path_globs` を加えたもの。T-06 の `include_tests` と T-09 はこの列だけを見る。

### 7.3.4 Python 環境の検出

T-00 手順 3 の順で候補を試し、最初に実行できたものを採る: 明示指定 → `<root>/.venv/`（Windows は `Scripts/python.exe`、他は `bin/python`）→ `uv.lock` があるときの `.venv` → `poetry`（PATH にあれば `poetry env info -p`）→ `CONDA_PREFIX` → PATH 上の `python`。外部コマンドはすべて `ProcessRegistry`（ADR-12）経由、上限 3,000 ms、失敗は次の候補へ。結果 `PythonEnv(interpreter, version, manager)` は `workspaces.python_env` に保存し、Python 系 LSP の初期化（§7.5.1）に渡す。

### 7.3.5 設定の優先順位

`Settings` は pydantic モデル。合成順は仕様書 §12.7: ツールでの明示 > 環境変数 > `.lucianke.toml` > 自動検出。`.lucianke.toml` は読むだけ（MUST NOT 書く）。解析できなければ無視して T-00 `status` の `warnings` に出す。`exclude_globs` / `python_env` / `default_base_ref` は `workspaces` 行に保存し、次回起動時の初期値にする。

### 7.3.6 ファイル監視（watcher.py）

```python
class Watcher:
    async def run(self, ws: Workspace, sink: IngestSink, stop: asyncio.Event) -> None
    mode: Literal["native", "polling"]
    async def drain(self, paths: Sequence[RelPath] | None = None) -> None   # 未処理イベントを同期で処理し切る（EditTransaction 段 3、§7.7.1）
    def expect(self, path: RelPath, sha256_after: str) -> None             # 自分の書き込みイベントを 1 回だけ無視する（§7.7.1 Reindexed）
```

1. `watchfiles.awatch(ws.root, debounce=WATCH_DEBOUNCE_MS, watch_filter=…)` を用いる想定（S-6 で Windows の挙動を確認）。フィルタは `.gitignore`（`dulwich.ignore` を用いる想定。S-8 で確認。使えなければ fnmatch の自前サブセット）と `exclude_globs`、`.git/` 配下。
2. 1 回のデバウンス窓のイベントをパスごとに畳む（追加 + 削除 = 削除、変更の連続 = 1 回）。
3. 窓内のパス数が `WATCH_BURST_FILES`（500）を超えたら個別処理をやめ、`sink.rescan(ws)`（§7.6.5 と同じ差分索引）に切り替える。
4. 各パスについて `confine` → `stat` → `sha256`（スレッドプール）→ `files.sha256` と比較 → 変化があれば `sink.changed(path)`、削除なら `sink.removed(path)`。`sha256` が同じなら何もしない（エディタの保存で mtime だけ変わる場合を吸収）。
5. `IndexWriter` の取り込みが完了したら、`LspPool.notify_watched(changes)`（`workspace/didChangeWatchedFiles`）と、開いている文書には `documents.on_external_change(path)`（全文 `didChange`）を送る。`TreeCache` の該当エントリは新しい `sha256` で自然に置き換わる。
6. 監視が開始できない（ネットワークドライブなど）ときは `mode="polling"` にし、背景のポーリングは行わず、要求時に対象ファイルの `sha256` を照合する遅延検証（`ToolContext.resolve_target` と `EditTransaction` 段 3）に切り替える。T-00 `status` に `watcher: "polling"` を出す。
7. NFR-PERF-3 の測定のため、イベント受信・DB コミット・LSP 通知の各時刻を `debug` ログに残す。
8. `drain(paths)` はデバウンス窓に溜まっているイベントのうち指定パス（省略時は全部）を即座に処理し、取り込みの完了まで待つ。`EditTransaction` が前提検証の直前に呼ぶ。`expect(path, sha256_after)` は、直後に届く自分の書き込みイベントを `sha256` が一致する場合に 1 回だけ捨てる（二重取り込みの抑止）。

受入で確かめる点:

- 外部プロセスが 1 ファイルを書き換えたとき、`files.sha256` の更新と `didChangeWatchedFiles` の送信が `INDEX_REFLECT_MS`（500）以内に完了する（p95、100 回）。
- `.gitignore` に一致するファイルを作っても `files` に行が増えない。
- 501 ファイルを同時に変えたとき、ログに `rescan` への切替が 1 回記録され、個別取り込みが走らない。

---

## 7.4 Syntax Engine（syntax/）

構文木に基づく一切（解析・骨格・シンボル・境界・ガード・import 解決・構文パターン・非コード形式・未知言語の推定）を担う。LSP も DB も知らない。入力はバイト列と言語 ID、出力は `core` の型。

### 7.4.1 LanguageSupport / FormatAdapter（ADR-9）

```python
class LanguageSupport(Protocol):                 # Tier 1〜2 のコード
    language_id: str
    tier: Tier
    extensions: tuple[str, ...]
    def grammar(self) -> "tree_sitter.Language"   # 例: Language(tree_sitter_python.language())（S-2）
    def queries(self) -> LanguageQueries          # tags / skeleton / imports / guards / doc の .scm（遅延コンパイル）
    def symbol_kind(self, node) -> Kind
    def definition_range(self, node, parsed: ParsedFile) -> Range     # §7.4.5。既定実装は boundaries.py
    def is_private(self, name: str, node) -> bool
    indent_default: IndentSpec                    # Python 4sp、TS/JS 2sp、Rust 4sp、Go tab
    test_path_globs: tuple[str, ...]
    lsp_servers: tuple[LspServerDef, ...]         # §7.5.1

class FormatAdapter(Protocol):                   # Tier 3〜4、および .ipynb / DOT / Mermaid
    language_id: str
    tier: Tier
    extensions: tuple[str, ...]
    def skeleton(self, source: bytes, opts: SkeletonOptions) -> list[Record]
    def select(self, source: bytes, selector: Selector) -> list[Match]        # xpath: / jsonpath: / jq: / toml: / yaml: / cell:
    def replace(self, source: bytes, selector: Selector, new: bytes) -> bytes  # M2

SUPPORT: dict[str, LanguageSupport]; ADAPTERS: dict[str, FormatAdapter]
def get_support(language_id) -> LanguageSupport | FormatAdapter | None
```

M1 で登録するもの:

| 区分 | 言語 | 根拠 |
|---|---|---|
| Tier 1 必須 | `python`（`.py`, `.pyi`）、`typescript` / `tsx`、`javascript` | 仕様書 OD-7。受入フィクスチャはこの 2 系統 |
| Tier 1 best effort | `rust`, `go`, `c`, `cpp`, `c_sharp` | PyPI に wheel がある（付録 A）。LSP は見つかれば使う |
| Tier 1 保留 | `kotlin`, `swift` | PyPI 配布の有無を S-2 で確認。無ければ M1 では未登録（Tier 4 扱い） |
| Tier 2〜3 | `ipynb`, `json`, `jsonl`, `yaml`, `toml`, `xml`（`.svg` 含む）, `markdown`, `rst`, `dot`, `mermaid` | §8.6 の M1 範囲（B2 読み取り、B3 出力、B4 骨格・部分取得） |

`typescript` / `cpp` / `json` / `xml` / `toml` の文法パッケージは最終公開が 2024 年で止まっている（付録 A）。`tree-sitter` 0.26 の ABI 範囲で読み込めるかを S-2 で確かめ、読めない文法は登録表から外して Tier 4 に落とす（起動時に `grammar()` を試し、失敗をログと T-00 `status` に出す）。

### 7.4.2 Parser プールと TreeCache（ADR-13）

```python
@dataclass(frozen=True)
class ParsedFile:
    language_id: str; sha256: str; source: bytes; tree: "tree_sitter.Tree"; line_index: LineIndex
    has_errors: bool
    def error_ranges(self) -> list[Range]        # ERROR / MISSING ノード

class TreeCache:
    def get_or_parse(self, ws: Workspace, rel: RelPath, sha256: str, load: Callable[[], bytes]) -> ParsedFile
    def peek(self, ws, rel, sha256) -> ParsedFile | None
    def evict(self, ws, rel) -> None
```

1. `Parser` はスレッド安全でないため、スレッドローカルに言語ごとの `Parser` を持つ。解析は常にスレッドプールで行う（ループ上で行わない。MUST）。
2. キーは `(workspace_id, rel, sha256)`。同じキーの解析が進行中なら 2 本目は待つ（二重解析の抑止）。
3. 上限は `TREE_CACHE_MAX_FILES`（2,000）と `TREE_CACHE_MAX_BYTES`（512 MiB）の両方。LRU で古いものから落とす。
4. `MAX_FILE_BYTES`（5 MB）を超えるファイルは解析しない。Tier 3 ならアダプタのストリーミング骨格、それ以外は `files` 行だけ（`lexical_fallback`）。
5. 構文エラーがあっても木は保持する（`has_errors`）。`error_ranges()` は Indexer が `source: "tree-sitter"` の診断にする。
6. 増分解析（`tree.edit` → 再解析）は MAY。M1 は全解析。

受入で確かめる点:

- 同じ `sha256` のファイルを 2 回要求したとき、2 回目は解析関数が呼ばれない（カウンタで検査）。
- 上限を超えたとき、キャッシュのファイル数とバイト数が上限以下に戻る。

### 7.4.3 シンボル抽出（symbols.py）と URN の付与

```python
def extract(parsed: ParsedFile, support: LanguageSupport, ws: Workspace, rel: RelPath) -> list[SymbolRecord]
```

1. `tags.scm` の捕捉（`@definition.<kind>` と `@name`）を走査し、定義ノードごとに `Kind` を決める。
2. 修飾名は包含する定義（クラス・名前空間・`impl`・ネストした関数）を外側から `.` で連ねる。モジュール名は含めない（URN の `path` が担う。仕様書 §7.2）。ラムダ・無名関数はシンボルにしない。
3. `range` は §7.4.5 の `definition_range`。`selection` は名前ノードの範囲。
4. `signature` は引数リストと戻り値注釈のテキストを空白正規化したもの。`return_type` は注釈があれば抽出。`doc_summary` は `doc.scm` が指す docstring / Doc コメントの最初の文（200 文字で省略）。
5. `modifiers` は `pub / priv / prot / static / async / abstract / export`（仕様書 §6.1）。`is_private` は `LanguageSupport` に委ねる（Python は `_` 始まり）。
6. `declared_in` は `.pyi` なら `stub`、それ以外は `source`（`cython` / `pyo3` / `proto` は M2 の B1b / B6）。
7. `body_sha256` は本体ノード（関数なら `block` / `body`）のバイト列の SHA-256。シグネチャだけ変わったか本体だけ変わったかの判定（T-17 / T-18）に使う。
8. **URN**: `lk://<workspace_id>/<rel>#<qualified>`。同一ファイル内で `qualified` が重複したら、重複した側だけに `/<arity>` を付け、なお重複なら `~<sighash>`（空白正規化したシグネチャの SHA-256 先頭 8 桁）を付ける（仕様書 §7.2。重複しない限り付けない。MUST NOT 常時付与）。シグネチャまで同一の再定義（Python で同名関数を 2 度書いた場合）は、**後の定義が素の URN を取り、先の定義には `~` + SHA-256(シグネチャ + 開始行)[:8] を付けて `warnings` に記録する**（仕様書に無い規則。親に報告）。
9. 外部シンボル（ワークスペース外の定義。LSP の結果から生じる）は `lk://_ext/<パッケージ>[@<版>]#<qualified>`。パッケージ名は `site-packages/` または `node_modules/` 直下のディレクトリ名、版は `*.dist-info` / `package.json` から取れれば付ける（MAY）。
10. 抽出結果の `confidence` は `structural`。LSP の hover / definition で確認した行は `features.py` が `exact` に上げる（§7.5.6）。

受入で確かめる点:

- フィクスチャの各言語ファイルについて、抽出した `range` が Tree-sitter の対応ノード範囲と一致する（T-02 の事後条件）。
- オーバーロードの無いファイルでは、どの URN にも `/` と `~` が現れない。
- 同一ファイルを 2 回抽出した結果（URN の集合と `body_sha256`）が一致する。

### 7.4.4 骨格（skeleton.py）

```python
def build(parsed: ParsedFile, symbols: list[SymbolRecord], support, opts: SkeletonOptions) -> Record   # 1 ファイル = 1 レコード
```

1. ファイルブロックのヘッダ行（`=== <path> (<N> lines[, <M> cells], skeleton: <K> lines, sha=<12hex>) ===`）、`imports:` 行（`imports.scm`）、トップレベルから入れ子へ `symbol-block` を組み立てる。本体は `skeleton.scm` の `@body` 捕捉の行数で `{ ...N lines omitted }` に畳む。
2. 行の生成は `mcp/render/compact.py` の行種関数を呼ぶ（§7.2.4）。ツールと骨格は文法を直接書かない。
3. **予算内での縮退**: T-02 手順 8（`doc:` → `urn:` → 非公開 → 入れ子の順に落とし、`- truncated: N members omitted`）を実現するため、ファイルブロックのレコードは `shrink(remaining_tokens) -> Record` を持つ。パッカーは「入らないレコード」に `shrink` があればそれを呼び、縮めても入らなければ次ページへ回す。`Record` に任意フィールド `shrink` を足す必要がある（親の §7.2 への突き合わせ点）。
4. Tier 3 の骨格はアダプタの `skeleton()` が同じ `Record` を返す。

### 7.4.5 定義ノードの境界（boundaries.py）

仕様書 §10.8 の規則の唯一の実装。`symbols.py`（`range`）、T-05（`context_lines: 0` の返却範囲）、T-10（置換・削除範囲）、T-18（差分の範囲）がすべてこれを使う（MUST。T-10 規約 1 の「T-05 と一致」はこれで担保する）。

```python
def definition_range(node, parsed: ParsedFile, cfg: BoundaryConfig) -> Range
def insertion_point(parent_node, parsed, cfg, where: Literal["start", "end"]) -> tuple[Position, IndentSpec]
def blank_lines_around(kind: Kind, scope: Literal["module", "class"], cfg) -> int
```

`BoundaryConfig` は言語ごとの表（`LanguageSupport` が返す）: デコレータ・属性のノード型（Python `decorated_definition`、Rust `attribute_item`、TS `decorator`、C# `attribute_list`）、Doc コメントの判定（JSDoc `/** */`、Rustdoc `///` `//!`、Python は本体先頭の文字列式）、通常コメントを含める条件（直前に空行が無い）、末尾同一行コメント、`export` / 可視性修飾子の含め方、`pass` だけの本体の扱い。

手順: 定義ノードから出発 → 親が「装飾つき定義」ならそれに置き換える → 直前の兄弟を遡り、Doc コメントは常に、通常コメントは空行を挟まない限り範囲に含める → 末尾は定義ノードの終端行の同一行コメントまで → 直後の空行は含めない。

受入で確かめる点:

- 言語ごとのフィクスチャ（デコレータつき・Doc コメントつき・空行で隔てたコメントつき）で、期待する開始行・終了行と一致する。
- `symbols.range` と T-05（`context_lines: 0`）の `range_returned` が全シンボルで一致する。

### 7.4.6 ガード条件（guards.py）と import 解決（imports.py）

`guard_for(call_node, parsed, support) -> str | None`: 呼び出し箇所から囲みの関数本体まで祖先を遡り、`guards.scm` が指す条件（`if` / `elif` / `else`（否定で表す）/ `match`・`case` / `try`・`except`（`on: 例外型`）/ `while` / 三項 / 短絡 `and`・`or`）の式テキストを `&&` で連ね、120 文字で省略する（T-07 手順 4）。結果は `relations.guard_condition` に書く。

`resolve_import(parsed, import_node, ws) -> RelPath | None`（`structural` 水準の名前解決）:

1. Python: 絶対 import はルートと `src/` 配下、相対 import は当該ファイルからの相対で、`<mod>.py` / `<mod>/__init__.py` / `<mod>.pyi` の順に探す。
2. TypeScript / JavaScript: 相対パスに `.ts/.tsx/.js/.jsx/.mjs` と `/index.*` を補う。`tsconfig.json` の `paths` は M2（MAY）。
3. 解決できたら `relations(IMPORTS, structural)`。解決できなければ外部として `lk://_ext/<名前>` を `target_urn` にする。
4. 呼び出し関係の `structural` 行は、呼び出し式の識別子を「同一ファイルの定義 → import で取り込んだ名前 → 解決先ファイルの公開シンボル」の順で引いて作る。同名の別シンボルを区別しないため `confidence_level = structural`。

### 7.4.7 構文パターン（structural.py）

```python
def search(language_id: str, pattern: str, files: Sequence[RelPath], constraints: Mapping[str, str] | None, max_matches: int, deadline: Deadline) -> list[Match]
def rewrite(match: Match, template: str) -> str
```

`ast-grep-py` を用いる想定。パターンと `rewrite` を対象言語で解析できなければ `E_SYNTAX_INVALID`（`details.param`）。書き換え API（メタ変数の展開・置換テキストの生成）が `ast-grep-py` に無ければ、`Match.meta_vars` の範囲からテンプレート展開を自前で行う（S-3 で確認）。照合はスレッドプールで走らせ、`STRUCTURAL_SEARCH_MS`（5,000）を超えたら `E_TIMEOUT` に部分結果。結果は T-12 が `edit/plan.py` の `Plan` に変換する（書き込みは `EditTransaction`）。

### 7.4.8 Tier 3 アダプタと Tier 4 ヒューリスティック

| アダプタ | 骨格 | 部分取得（`select`） | 備考 |
|---|---|---|---|
| `ipynb` | 全セルを順に連結した **仮想 Python ファイル** を作る。code セルは `source` そのまま、markdown / raw セルは `# %% [markdown]` の行と `# ` を前置した本文に変換して行数を占める（行番号が単調になり、Python 文法で解析できる）。`CellMap(cell_index → 仮想行範囲)` を保持し、`[Cell] <n> <kind> (exec_count: k) @L<s>-<e>` と、code セル内のシンボルを入れ子で出す。`outputs` / `metadata` は読まない（MUST） | `cell:<n>` は `cells[n].source` の連結のみ | 標準ライブラリ `json` で読む（`nbformat` は不要。親に報告）。書き戻し（M2）は該当セルの `source` だけを差し替え、他のキーは元の順序・インデントで再直列化する |
| `xml` | タグ階層を深さ `max_depth`（既定 4）まで、同名兄弟は `×N`、属性名一覧を `type:` に。5 MB 超は `xml.etree.ElementTree.iterparse` で要素数だけ数える | `xpath:` は `lxml` が入っていれば XPath 1.0、無ければ ElementTree のサブセット（それで評価できない式は `E_UNSUPPORTED` と `details.hint: "pip install lucianke[xml]"`） | `lxml` は extras `[xml]`（§11） |
| `json` / `jsonl` | キー階層と配列要素の推論型（`array<object{id:int,…}>`）。JSONL は先頭 100 レコードから推論 | `jsonpath:` は `jsonpath-ng`。`jq:` は M2（仕様書 §8.6） | 行番号は自前の軽量トークナイザで付ける（`json` 標準ライブラリは位置を返さない） |
| `yaml` | `yaml.compose`（PyYAML）のノードの `start_mark.line` で範囲を付ける | `yaml:` はキー経路 | `safe` 系のみ使う（MUST） |
| `toml` | 範囲は `tree-sitter-toml`、値は `tomllib` | `toml:` はキー経路 | |
| `dot` / `mermaid` | `tree-sitter-dot` でノード・エッジ・サブグラフ数。Mermaid は行ベースの自前パーサで種別と件数のみ（入力の構造解析は M2 の B3） | なし | |
| `markdown` / `rst` | 見出しの階層（`[Section]`）と Mermaid ブロックの位置 | なし | `rst` は `tree-sitter-rst` |

Tier 4（`heuristic.py`）:

1. **ctags**: 起動時に一度だけ PATH の `ctags --version` を試し、Universal Ctags なら使う。子プロセス（ADR-12）で JSON 出力（フラグは実機で確認）を読み、`kind` と行を `[Function]` / `[Section]` にする。**同梱しない**（GPL。仕様書第 16 章 L-4）。無くても次の手順で動く（MUST）。
2. **ブロック推定**: 定義らしい行（`def|function|fn|sub|proc|class|procedure|section` と `<name>\s*[:=(]`）を開始行とし、終了行はインデント（次に同じ以下のインデントで非空白の行の直前）または括弧の対応で推定する。`confidence = heuristic`。
3. **字句一致**: 単語境界つきの正規表現を Python の `re` で走査する（`ripgrep` が PATH にあれば使ってもよいが依存しない。MAY）。`REGEX_SCAN_MS`（200。T-03）／`DEGRADED_GREP_MS`（2,000。T-06・T-07）で打ち切り、`E_TIMEOUT` に部分結果。`confidence = lexical_fallback`。

受入で確かめる点:

- `.ipynb` の骨格・本体の出力文字列に、その Notebook の `outputs` 配列内の文字列が 1 つも含まれない。
- ctags を PATH から外した環境でも Tier 4 ファイルの骨格が `heuristic` で返る。
- 5 MB 超の XML に対し `get_file_skeleton` が `MAX_FILE_BYTES` 超でも構文木を作らずに応答する（メモリ上限の検査）。

---

## 7.5 LSP Pool（lsp/）

言語別 LSP の生涯・文書同期・診断・要求を担う。要求経路の中では「今すぐ答えられるか」だけを判断し、待つかどうかはエンジン選択（ADR-6、ADR-10）が決める。

### 7.5.1 サーバー定義（servers.py）

```python
@dataclass(frozen=True)
class LspServerDef:
    name: str                               # "basedpyright" / "ruff" / "vtsls" …
    language_ids: tuple[str, ...]
    role: Literal["types", "lint"]          # 同一言語に 2 役を並列してよい（仕様書 §12.3）
    discover: tuple[Candidate, ...]         # 探索順: LUCIANKE_LSP_<LANG>_CMD → .venv/bin|Scripts, node_modules/.bin → PATH
    args: tuple[str, ...]                   # 例: ("--stdio",)
    init_options: Callable[[Workspace], dict]
    position_encoding_default: Literal["utf-16", "utf-8"]
    requests_m1: frozenset[str]             # ADR-15 の範囲でこのサーバーに送ってよいもの
```

M1 の定義（起動コマンド・初期化オプション・位置単位は **S-4 / S-5 / S-11 で実機確認してから固定する**。ここは想定）:

| 言語 | 既定 | 代替 | 備考 |
|---|---|---|---|
| Python（types） | `basedpyright`（PyPI の `basedpyright` は `nodejs-wheel-binaries` を依存に持ち、Node を別途入れずに動く見込み。付録 A） | `pyright`（`nodeenv` 依存で Node を取りに行く） | インタープリタは `initializationOptions` / `workspace/configuration` で渡す（`python.pythonPath` 相当。S-4） |
| Python（lint） | `ruff server` | — | QuickFix / `source.organizeImports` の供給元 |
| TypeScript / JavaScript | `vtsls --stdio` | `typescript-language-server --stdio` | どちらも Node が要る。`node_modules/.bin` → PATH。tsserver の所在を初期化で渡す（S-5） |
| Rust / Go / C,C++ / C# / Kotlin / Swift | `rust-analyzer` / `gopls` / `clangd` / `csharp-ls` / `kotlin-language-server` / `sourcekit-lsp` | — | best effort。PATH に無ければ `unavailable` |

`initialize` では `positionEncodings: ["utf-8", "utf-16"]` を提示し、応答の `positionEncoding` を `LspClient` に設定する。交渉できないサーバーは UTF-16（仕様書 §12.2）。

### 7.5.2 状態機械（pool.py）

```python
class LspPool:
    def get(self, language_id: str, ws: Workspace, *, start_if_needed: bool = True) -> LspHandle | None   # ブロックしない
    def status(self, language_id, ws) -> LspStatus          # (state, progress, server_name, pid, reason)
    async def ensure_started(self, language_id, ws) -> None # standby なら起動を投げて即戻る（待たない。§8.3）
    async def notify_watched(self, changes) -> None          # workspace/didChangeWatchedFiles を稼働中の全 LSP へ
    async def shutdown_all(self, grace_ms: int = 500) -> None # 終了手順（§6.5）: shutdown → exit → terminate → kill
    def reset(self, language_id | None, ws) -> None          # reindex_scope: "lsp"
    async def shutdown_all(self) -> None
```

| 状態 | 入る契機 | 動作 | 出る契機 |
|---|---|---|---|
| `standby` | 初期状態。30 分無要求（MAY） | 何もしない | 当該言語への最初の要求 → `starting` |
| `starting` | 要求 | コマンド探索 → `ProcessRegistry.spawn` → `initialize` / `initialized` を送る。要求元には降格応答（待たない） | `initialize` 成功 → `indexing`。失敗 → 再試行判定 |
| `indexing` | 初期化直後 | `$/progress` / `window/workDoneProgress` を `progress` に反映。取れないサーバーは最初の `publishDiagnostics` 受信で `ready` | 進捗完了 → `ready` |
| `ready` | 索引完了 | 要求を通す | クラッシュ / 無応答（連続 3 回のタイムアウト）→ 再試行判定。`reset` → `standby` |
| `unavailable` | 再試行上限 | `status.reason` に理由（`not_found` / `crashed_3x` / `init_failed`）。T-00 `status` に出す | `reset` → `standby` |

再試行は `LSP_RESTART_BACKOFF_S = (1, 2, 4)`、`LSP_RESTART_MAX = 3`。状態遷移は言語ごとの `asyncio.Lock` の下で行い、二重起動を防ぐ。`EngineSelector` は `status()` だけを見る。

受入で確かめる点:

- LSP を PATH から外した状態で Python のツールを呼ぶと、応答は `structural`、`lsp_status: "unavailable"`、エラーにならない。
- LSP プロセスを外部から kill したとき、ログに再起動が 3 回記録され、4 回目以降は `unavailable` のまま。
- `shutdown_all()` 完了から 1 秒以内に `ProcessRegistry` が空になる。

### 7.5.3 JSON-RPC クライアントと位置変換（client.py、ADR-14）

```python
class LspClient:
    async def request(self, method: str, params: Any, *, timeout_ms: int) -> Any    # 超過で asyncio.TimeoutError → 呼び手が降格
    def notify(self, method: str, params: Any) -> None
    def on_notification(self, method: str, handler) -> None
    def on_request(self, method: str, handler) -> None     # client/registerCapability, workspace/configuration, window/workDoneProgress/create
    async def cancel(self, request_id: int) -> None        # $/cancelRequest
    encoding: Literal["utf-16", "utf-8"]
```

1. フレーミングは `Content-Length` ヘッダ。読み取りタスク 1 本が応答を `id → Future` に配り、通知をハンドラへ回す。子の `stderr` は別タスクでログへ（`debug`）。`window/logMessage` / `showMessage` もログへ。
2. タイムアウトは `core/limits.py` の定数: `LSP_TIMEOUT_DEFINITION_MS=3000`、`LSP_TIMEOUT_REFERENCES_MS=3000`、`LSP_TIMEOUT_HOVER_MS=3000`、`LSP_TIMEOUT_CALL_HIERARCHY_MS=5000`、`LSP_TIMEOUT_CODE_ACTION_MS=5000`、`LSP_TIMEOUT_RENAME_MS=10000`。超過時は `$/cancelRequest` を送ってから呼び手に返す。
3. **位置変換はこの境界だけ**（MUST）。`to_lsp(pos: Position, line_text) -> {line, character}` と `from_lsp(...)` が `core/textpos.LineIndex` でコードポイント ↔ UTF-16 ↔ バイトを変換する。`Range` / `Location` / `TextEdit` を越境させる関数はここに閉じる。
4. URI: 送信は realpath の絶対パスから `file://` を作る（Windows は `file:///S:/...`）。受信は `confine` を通し、ルート外は外部シンボル扱い。

### 7.5.4 文書同期（documents.py）

```python
class DocumentSync:
    async def ensure_open(self, path: RelPath) -> int                   # 版 1 で didOpen。開いていれば現在の版
    async def on_edit(self, path, new_text: str) -> int                 # 全文 didChange。版 +1。送信時刻を記録
    async def on_external_change(self, path) -> int | None              # 開いていれば再読込して didChange
    async def close(self, path) -> None
```

開いている文書は `LSP_OPEN_DOCS_MAX`（200）の LRU。要求で初めて触れたファイルを開き、超えた分は `didClose`。`didChange` は全文（増分は MAY）。`revision != WORKING_TREE` の内容は開かない（LSP は作業ツリーしか知らない）。

### 7.5.5 診断の受信と版待ち（diagnostics.py）

```python
class DiagnosticsStore:
    def on_publish(self, uri, version: int | None, items: list, received_at: float) -> None
    async def wait_for(self, path: RelPath, min_version: int, timeout_ms: int) -> bool   # 揃えば True
```

1. 受信した診断は `IndexWriter` に「`(file_id, source)` 単位の全置換」として積む（仕様書 §9.4）。`source` は LSP 名（`basedpyright`, `ruff`, `vtsls` …）。`enclosing_urn` は `symbols` の範囲包含で引く。`data_version` は進めない（ADR-8）。
2. `wait_for` は `version >= min_version` の `publishDiagnostics` 到着を待つ。`version` を付けないサーバーに対しては、当該文書の `didChange` 送信時刻より後に受信した最初の `publishDiagnostics` を応答とみなす（SHOULD）。
3. Tree-sitter の構文エラーは LSP ではなく Indexer が `source: "tree-sitter"` で入れる（§7.6.2）。同一位置・同一メッセージの重複は T-08 が LSP 側を優先する。

### 7.5.6 機能呼び出しと PaaD への反映（features.py、ADR-6、ADR-15）

```python
class LspFeatures:
    async def definition(self, path, pos) -> list[Location] | None       # None = 今は答えられない（降格）
    async def references(self, path, pos, include_decl) -> list[Location] | None
    async def call_hierarchy(self, path, pos, direction) -> list[CallItem] | None
    async def hover_signature(self, path, pos) -> str | None
    async def rename(self, path, pos, new_name) -> Plan                  # prepareRename → rename → WorkspaceEdit → Plan
    async def code_actions(self, path, range, only, diagnostics) -> list[CodeAction]
    async def resolve_code_action(self, action) -> CodeAction            # edit が無ければ codeAction/resolve
```

| 能力 | LSP が `ready` | `indexing` / `starting` | `standby` / `unavailable` |
|---|---|---|---|
| DEFINITION / REFERENCES / CALL_HIERARCHY / HOVER | タイムアウト内で待ち `exact` | 待たずに `None`（起動は契機にする）→ 呼び手は `structural` | `None` → `structural` |
| RENAME / CODE_ACTION | 実行 | `E_LSP_UNAVAILABLE`（`details.reason: "indexing"`） | `E_LSP_UNAVAILABLE` |
| SKELETON / SEARCH / BODY | 使わない | 使わない | 使わない |

PaaD への反映:

1. `definition` / `references` / `callHierarchy` の結果は `Location` → `symbols` の範囲包含で URN に写し（ワークスペース外は `_ext`）、`relations` に `exact` で書く。同じ `(source_urn, target_urn, relation_kind)` の `structural` 行は置き換える（仕様書 §9.4）。この書き込みは `data_version` を進める（結果集合が増えうるため。ADR-8 の規則どおり。親に報告）。
2. `hover` の型文字列は内部テーブル `_hover_cache(urn, file_sha256, text)` に置き、T-04 の `signature_expanded` に使う。公開しない（`_` 接頭辞）。
3. `rename` / `codeAction` の `WorkspaceEdit` は `edit/plan.py` の `Plan`（hunk 列）に変換して返すだけで、**書き込みは `EditTransaction`** が行う（ADR-11）。ファイルの作成・削除を含む `WorkspaceEdit` は `E_UNSUPPORTED`。

受入で確かめる点:

- LSP が `indexing` の間に T-04 を呼んだとき、応答は `timing_ms < 50`（LSP を待っていない）かつ `lsp_status: "indexing"`。
- `ready` 後に T-06 を呼ぶと、`relations` の当該 `target_urn` の行が `exact` に置き換わり、`structural` の重複行が残らない。

---

## 7.6 Indexer と PaaD（index/）

仕様書第 9 章のスキーマを唯一の書き手で更新し、読み手には一貫した断面を見せる。`index/` は `core` 以外に依存しない（§4.2）。

### 7.6.1 IndexWriter とジョブ（ADR-7）

```python
@dataclass(frozen=True)
class WriteJob:
    fn: Callable[[sqlite3.Connection], Any]
    bumps_version: bool          # files / symbols / relations を変えるなら True。diagnostics だけなら False
    label: str                   # ログ用（"ingest:src/a.py", "diag:basedpyright:src/a.py"）
    coalesce_key: str | None     # 同じキーの未実行ジョブは最後の 1 つに畳む
    priority: int = 0            # EditTransaction の再索引は 0、通常は 10

class IndexWriter:
    def submit(self, job: WriteJob) -> concurrent.futures.Future
    async def submit_async(self, job: WriteJob) -> Any     # 満杯（WRITE_QUEUE_MAX=10000）なら待つ
```

1. 専用スレッドがキューから取り、`BEGIN IMMEDIATE` → `fn(conn)` → `bumps_version` なら `UPDATE _meta SET data_version = data_version + 1` → `COMMIT`。例外は `ROLLBACK` し、`label` 付きでログ、`Future` に例外を載せる。半更新の状態は読み手に見えない（MUST）。
2. 接続は書き込み専用の 1 本（`journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=5000`）。
3. チェックポイントは、キューが空になり前回から 1,000 コミット以上たったとき `PASSIVE`、終了時に `TRUNCATE`。
4. 起動時の差分索引では、1 トランザクションに最大 50 ファイルをまとめ、`data_version` はトランザクションごとに 1 回だけ進める。

### 7.6.2 取り込みパイプライン（ingest.py）と data_version（ADR-8）

```python
async def ingest_file(ws: Workspace, rel: RelPath, *, reason: str) -> None
async def remove_file(ws, rel) -> None
async def rescan(ws) -> JobHandle          # §7.6.5 と同じ差分索引
```

1 ファイルの手順:

1. `confine` → `stat` → 読み込み（スレッド）→ `sha256`。`files.sha256` と同じなら終了。
2. `detect`（§7.3.3）。`index_content=False` またはバイナリなら `files` 行だけ upsert（`confidence_level = lexical_fallback`）。
3. `MAX_FILE_BYTES` 以下なら `TreeCache.get_or_parse` → `symbols.extract` → `imports.resolve` と呼び出し候補から `structural` の `relations` を作る。Tree-sitter のエラーノードは `source: "tree-sitter"` の診断行にする。
4. `WriteJob`（`bumps_version=True`, `coalesce_key=f"ingest:{ws}:{rel}"`）として次を 1 トランザクションで行う:
   - `files` を upsert（`sha256`, `line_count`, `byte_size`, `language`, `tier`, `is_test`, `confidence_level`, `indexed_at`）。
   - `symbols` を URN で突き合わせ、新規は INSERT、変化（`range`, `signature`, `body_sha256`, `doc_summary` …）は UPDATE、消えた URN は DELETE（`relations` の `source_file_id` は `ON DELETE CASCADE`。`target_urn` が消えた URN を指す行は FK が無いので明示的に DELETE する）。
   - `relations` のうち `source_file_id = このファイル` の行を **水準を問わず全削除** し、`structural` 行を入れ直す。`exact` 行は次に LSP が答えたときに再取得される（編集直後に `structural` へ一時的に下がることを受け入れる。親に報告）。
   - `diagnostics` の `(file_id, 'tree-sitter')` を全置換。
   - `symbols_fts` は `symbols` への INSERT / UPDATE / DELETE トリガで追従させる（`schema.sql` にトリガを足す。仕様書 §9.2 の DDL への追記。親に報告）。
5. 完了後、Watcher 経由なら LSP への通知（§7.3.6 手順 5）。

並列度: 解析はスレッドプール（`READER_POOL_SIZE`=8 と同じ数）、書き込みはキューで直列。バースト（`WATCH_BURST_FILES` 超）と起動時は `rescan` に切り替える。

受入で確かめる点:

- ファイルを 1 回編集したとき `data_version` がちょうど 1 増える。診断だけが届いたときは増えない。
- シンボルを 1 つ削除して再取り込みしたあと、その URN を `target_urn` に持つ `relations` 行が 0 件である。
- 取り込み中に読み取り接続から `symbols` を読んでも、同一ファイルの新旧行が混在して見えない（トランザクション境界の検査）。

### 7.6.3 読み取りクエリ（queries.py）

ツールが使うクエリは名前つき関数に閉じ、SQL 文字列をツールに書かない（MUST）。すべてパラメータ化し、`ReaderPool` から借りた接続でスレッド上で実行し、`core` の型で返す。

| 関数 | 用途 | 備考 |
|---|---|---|
| `symbol_by_urn(urn)`, `symbols_by_suffix(ws, suffix, limit)`, `symbol_at(ws, rel, pos)` | TargetRef 解決（§7.1.3） | `idx_symbols_qname` / `idx_symbols_file` |
| `file_by_path(ws, rel)`, `files_in_scope(ws, globs, include_tests)` | 対象の列挙 | |
| `skeleton_rows(file_id)` | T-02 / T-01 | 親子は `parent_urn` で組み立てる |
| `search_fts(ws, query, kinds, limit)`, `search_prefix / exact / regex` | T-03 | `regex` は索引を使わず `REGEX_SCAN_MS` で打ち切る |
| `references_to(urn, use_kinds, include_tests, offset, limit)`, `count_references_to(urn, …)` | T-06 | 1 参照 = 1 行。`offset` はカーソルの `o` |
| `callers(urn, depth, max_children)`, `callees(…)` | T-07 / T-09 | 再帰 CTE。循環は経路配列で検出 |
| `diagnostics_for(file_ids, min_severity, sources)` | T-08 | |
| `repo_map(ws, scope, ranking, per_file)` | T-01 | `pagerank_score` は `relations` から Indexer が背景で計算して `files` / `symbols` に書く（空なら `alpha`） |
| `data_version(ws)` | パイプライン・カーソル | |

性能の目安は「10 万シンボルで各関数 10 ms 以下」。S-9 のベンチで確かめ、超えるものは索引を足す。

### 7.6.4 公開クエリエンジン（public.py、仕様書 §13.2）

```python
class PublicQueryEngine:
    async def run(self, ws, sql: str, params: Mapping[str, Any], *, limit: int, timeout_ms: int,
                  grep: GrepSpec | None, explain: bool) -> QueryResult
```

手順:

1. 長さ ≤ `QUERY_LENGTH_MAX`（64 KB）。コメントを除いた先頭トークンが `SELECT` または `WITH` でなければ `E_QUERY_REJECTED(not_select)`。禁止語（`ATTACH`, `DETACH`, `PRAGMA`, `VACUUM`, `load_extension`, `readfile`, `writefile`, `fts5` の書き込みコマンド）を含めば `E_QUERY_REJECTED(forbidden_keyword)`。
2. 接続を `file:<paad.sqlite>?mode=ro` の URI で開き、`PRAGMA query_only = ON`、`set_authorizer(authorize)`、`set_progress_handler(check_deadline, 1000)` を設定する（Python 標準の `sqlite3` にこれらがある想定。S-7 で確認。**WAL の DB を `mode=ro` で開くには `-shm` への書き込みが要る**ため、同じディレクトリに書き込み権限があることと、`immutable=1` を使わないことを S-7 の確認項目に入れる）。
3. `grep` があれば、接続ローカルの `CREATE TEMP TABLE temp_grep_matches(path TEXT, line INTEGER, snippet TEXT, confidence_level TEXT)` を作り、単語境界つき正規表現で走査した結果（上限 `GREP_MATCHES_MAX`=10,000）を入れる。この間だけ authorizer のフラグ `setup_phase=True` で TEMP への INSERT を許す。
4. `explain=True` なら `EXPLAIN QUERY PLAN` を実行して `plan[]` を返し、本体は実行しない。
5. `params` の名前と `:name` の一致を検査（`E_INVALID_ARGUMENT`）。`execute(sql, params)`。Python の `sqlite3` は複文を拒否するので、その例外は `E_QUERY_REJECTED(multiple_statements)` に写す。構文エラーは `E_QUERY_REJECTED(syntax_error)` に `details.sqlite_message`。
6. `fetchmany(limit + 1)`。`limit + 1` 行目があれば `truncated=True`。期限超過は progress handler が中断し `E_TIMEOUT`（取得済みの行を部分結果に）。
7. 接続を閉じる（1 要求 1 接続。開く費用が大きければプール化。S-9）。

authorizer の判定表（`sqlite3` の定数名）:

| 操作 | 判定 |
|---|---|
| `SQLITE_SELECT`, `SQLITE_RECURSIVE` | 許可 |
| `SQLITE_READ(table, column)` | `table` が公開 10 テーブル・`symbols_fts`・`temp_grep_matches` なら許可。それ以外（`_` 始まりの内部テーブル、`sqlite_master` を含む）は拒否 |
| `SQLITE_FUNCTION(name)` | 組み込みの集計・文字列・日付・JSON・FTS5 関数と Lucianke 登録の読み取り専用関数なら許可。`load_extension`, `readfile`, `writefile`, `edit`, `fts5` の書き込み系は拒否 |
| `SQLITE_CREATE_TEMP_TABLE`, `SQLITE_INSERT(temp_grep_matches)` | `setup_phase` のときだけ許可 |
| それ以外（INSERT / UPDATE / DELETE / CREATE / DROP / ALTER / ATTACH / DETACH / PRAGMA / TRANSACTION …） | 拒否 |

拒否は `E_QUERY_REJECTED(non_public_object)` または `(forbidden_keyword)` に写す。`mode=ro`・`query_only`・authorizer の 3 重は **どれか 1 つが欠けても他が効く**（MUST。テストでは 1 つずつ外して書き込み文が通らないことを確かめる）。

受入で確かめる点:

- `INSERT` / `UPDATE` / `DELETE` / `CREATE` / `DROP` / `ALTER` / `ATTACH` / `PRAGMA` を先頭に持つ文、複文、`_meta` を参照する文が `E_QUERY_REJECTED` で返り、DB ファイルの `sha256` が変わらない。
- 無限再帰の CTE が `timeout_ms` 以内に `E_TIMEOUT` で返る。
- 3 重防御のうち任意の 1 つを無効化したビルドでも、書き込み文が拒否される。

### 7.6.5 スキーマ版管理と起動時の差分索引

- `_meta(schema_version INTEGER, data_version INTEGER, edit_seq INTEGER, lucianke_version TEXT, created_at TEXT)`。`schema.sql` の先頭コメントに `schema_version` を書き、`migrate.py` が前方向の移行を順に適用する。移行が用意されていない古い版は DB を作り直す（PaaD はキャッシュであり、再索引で復元できる。MUST）。
- `schema.sql` と仕様書 §9.2 の DDL の差分はテスト（`tests/unit/test_schema_matches_spec.py`）で検出する。追加したトリガと `_` 内部テーブルは許容リストに書く。
- 起動時の差分索引: `.gitignore` と `exclude_globs` を尊重してファイルを列挙 → `files` の `(path, byte_size, mtime)` と比べ、変わったものだけ `sha256` → 違えば `ingest_file`、無くなったものは `remove_file`、新しいものは `ingest_file`。背景ジョブとして進捗を T-00 の `jobs[]` に出し、索引中もツールは答える（`warnings` に「index in progress N%」）。`files.indexed_at` は取り込み時刻。

受入で確かめる点:

- 2 回目の起動で、変更の無いワークスペースに対して `ingest_file` が 1 回も呼ばれない（ログのカウンタ）。
- `schema_version` を 1 つ古くした DB で起動すると、移行または再構築が走り、起動後の `schema_version` が最新になる。

---
## 7.7 Edit Transaction（edit/、ADR-11）

カテゴリ D の 4 ツールは、`EditTransaction` に **計画を作る関数** を渡すだけで、仕様書第 10 章の 8 段はトランザクションが実行する。

```python
PlanBuilder = Callable[[PlanContext], Awaitable[Plan]]

class EditTransaction:
    async def run(
        self, ctx: ToolContext, plan_builder: PlanBuilder, *,
        dry_run: bool,
        allow_heuristic_mutation: bool,
        expected_shas: Mapping[str, str] | None,   # path → sha256（64 桁、または 12 桁以上の接頭辞）
        diagnostics_wait_ms: int,                   # 0〜10000。0 は待たずに pending
    ) -> EditResult
```

`PlanContext` はトランザクションが `plan_builder` に渡す読み取り専用の文脈: 解決済み `TargetRef`、読み取り接続、対象ファイルの `(bytes, LineIndex, tree)`（`TreeCache` 経由）、`LanguageSupport`、`EngineChoice`。`plan_builder` はファイルを書かない（MUST）。

### 7.7.1 状態機械と不変条件

| 状態 | 入口条件 | 行うこと | 失敗 → コード | ファイル |
|---|---|---|---|---|
| **Resolved** | 呼び出し | `revision == WORKING_TREE` を確認。対象ファイルを読み `resolved_shas` と `LineIndex` を作る。精度水準（`EngineSelector`）を記録。編集前の診断スナップショット（§7.7.6）を取る | `E_INVALID_ARGUMENT`（revision）、`E_TARGET_NOT_FOUND`、`E_TARGET_AMBIGUOUS` | 不変 |
| **Planned** | Resolved | `plan_builder(PlanContext)` を呼び `Plan` を得る。hunk の重なりを検査 | `E_LSP_UNAVAILABLE`、`E_UNSUPPORTED`、`E_SYNTAX_INVALID`（pattern / rewrite）、`E_MUTATION_BLOCKED`（境界が一意に決まらない等。ツールが判定） | 不変 |
| **（ロック取得）** | Planned | `edit/lock.py` のワークスペースロックを取る。待ち上限 `EDIT_LOCK_WAIT_MS = 5000` | `E_TIMEOUT` | 不変 |
| **PreconditionsVerified** | ロック内 | ⑴ `expected_shas` を現在の sha と照合（接頭辞一致可）。⑵ `Watcher.drain()` で未処理イベントを処理し、`resolved_shas` と再照合。⑶ 降格インターロック（仕様書 §10.5）: 降格ファイルで `allow_heuristic_mutation` が無ければ `dry_run = True` に切り替え `warnings` に理由 | `E_PRECONDITION_FAILED`（`details.path / expected / actual`） | 不変 |
| **SyntaxChecked** | PreconditionsVerified | メモリ上で適用し再解析（§7.7.4）。恒等編集（適用後バイト列 == 元）を検出 | `E_SYNTAX_INVALID` | 不変 |
| **DryRunDone** | SyntaxChecked ∧ `dry_run` | 統一 diff、`syntax_check`、予測診断（MAY）を作って応答。ロック解放 | — | 不変 |
| **Journaled** | SyntaxChecked ∧ ¬`dry_run` ∧ 非恒等 | `edit_id` を採番し、全対象ファイルの書き込み前内容を退避（§7.7.5） | `E_INTERNAL` | 不変 |
| **Written** | Journaled | 直前に `resolved_shas` を再照合 → 一時ファイル → `fsync` → 改名（全ファイル）。改名を伴うときはファイル改名も | `E_PRECONDITION_FAILED`（直前照合）、`E_INTERNAL`（`details.rolled_back: true`） | **全部書く or 何も書かない** |
| **Reindexed** | Written | `TreeCache` の旧キー無効化、§7.7.4 で得た構文木を新キーで登録、`index/ingest.py` に同期で取り込ませ `data_version` を進める、`lsp/documents.py` に `didChange`（未オープンなら `didOpen`）、`Watcher.expect(path, sha_after)` で自分の書き込みイベントを抑止。**ロック解放** | ログのみ（応答は続行。`warnings` に記録） | 新内容 |
| **Diagnosed** | Reindexed | `DiagnosticsWaiter` で待ち、差分を計算（§7.7.6）。ロック外で行う | `status: "pending"` で応答 | 新内容 |
| **Done** | | `EditResult` を返す | | |

恒等編集（SyntaxChecked で検出）は Journaled 以降を飛ばし、`applied: true`、`edit_id: null`、`files_changed[].hunks = 0`、空の `diff`、`warnings` に `no-op` を入れて応答する。ファイルの更新時刻も変えない（監視の無駄撃ちを避ける）。

ロックは Planned の直後に取り、Reindexed の直後に放す（MUST）。診断待ち（最大 10 s）をロック内で行わないのは、他の編集と読み取りを止めないため（仕様書 §5.9 は「読み取りは書き込みの解放を待つ」なので、待ちが長いとすべてのツールが詰まる）。

不変条件と事後条件の対応:

| 不変条件 | 内容 | 担保する EP |
|---|---|---|
| I-1 | ファイルが変わるのは Written の中だけ。それ以外の状態・失敗ではバイト列が不変 | EP-2、EP-3、EP-8 |
| I-2 | Written は全ファイル成功か、全ファイルをジャーナルから戻すかのどちらか | EP-9、NFR-SAFE-4 |
| I-3 | Written に入る直前の sha は `resolved_shas` と一致する（ロック内で再読・再計算） | EP-4 |
| I-4 | `applied: true` なら `files_changed[].sha256_after` はディスクの sha と一致する（改名後に再計算して入れる） | EP-1 |
| I-5 | SyntaxChecked を通っていない計画は書かない | EP-5 |
| I-6 | 恒等編集は書かない | EP-6 |
| I-7 | `new_diagnostics.status: "complete"` のとき、`added` / `resolved` は `diagnostics` テーブルの現在行と整合する（同じ経路で書かれるため） | EP-7 |

`EditResult`:

```python
@dataclass(frozen=True)
class FileChange:
    path: str; sha256_before: str; sha256_after: str; hunks: int; renamed_from: str | None = None

@dataclass(frozen=True)
class EditResult:
    applied: bool
    edit_id: str | None
    files_changed: tuple[FileChange, ...]
    diff: str                                   # unified diff（dry_run でも作る）
    syntax_check: Literal["ok", "skipped"]
    new_diagnostics: DiagDelta                  # §7.7.6
    target_urns_after: tuple[str, ...]          # 再索引後の対象 URN（T-10 の target_urn、T-11 の urn_map の元）
    confidence: Confidence
    warnings: tuple[str, ...]
```

### 7.7.2 Plan と Hunk（plan.py）

```python
@dataclass(frozen=True)
class Hunk:
    path: str                 # ルート相対
    range: Range              # 適用前の座標（0 始まり・コードポイント）。挿入は start == end
    new_text: str             # 正規化済み。改行は "\n"。writer がファイルの改行に合わせる
    label: str | None = None  # 表示用（"replace_body", "rename" など）

@dataclass(frozen=True)
class Plan:
    hunks: tuple[Hunk, ...]
    renames: tuple[tuple[str, str], ...]        # (old_path, new_path)。T-11 / T-13 の WorkspaceEdit 由来のみ
    touched_paths: tuple[str, ...]              # hunks の path と renames の両側の和集合
    resolved_shas: Mapping[str, str]            # Resolved で読んだ sha（touched_paths 全部）
    target_urns: tuple[str, ...]                # 編集対象。再索引後の対応付けに使う
    expects_syntax: bool                        # False なら syntax_check は "skipped"（Tier 4）
```

規約:

- 同一ファイル内の hunk は **後ろから** 適用する（`range.start` の降順）。先に適用した hunk が前方の座標を動かさないため、すべての hunk を適用前座標のまま書ける。
- hunk の範囲が重なっていれば計画は不正。`E_UNSUPPORTED`（`details.reason: "overlapping_edits"`）とし、ログに計画を残す。
- 位置の変換は `LineIndex`（`core/textpos.py`）だけが行う。LSP の位置（UTF-16 または交渉した UTF-8 単位）→ コードポイント（`LineIndex.from_lsp`）、コードポイント → バイトオフセット（`LineIndex.to_byte`。Tree-sitter 用）。
- `WorkspaceEdit` → `Plan` の変換（`plan.from_workspace_edit`）:
  1. `documentChanges` があればそれを、無ければ `changes` を使う。
  2. `TextDocumentEdit` の URI を `workspace/paths.py` でルート相対に直す。ルート外なら `E_UNSUPPORTED`（`details.reason: "edit_outside_workspace"`）。
  3. `textDocument.version` が付いていて `lsp/documents.py` の現在版と違えば `E_PRECONDITION_FAILED`（`details.reason: "lsp_document_version_mismatch"`）。
  4. `RenameFile` は `renames` に、`CreateFile` / `DeleteFile` は `E_UNSUPPORTED`。
  5. `TextEdit` の `range` を `LineIndex.from_lsp` で変換し `Hunk` にする。`newText` は正規化しない（LSP が返した本文をそのまま使う。改行だけ `"\n"` に揃える）。

### 7.7.3 new_code の正規化（normalize.py）

`normalize(new_code, *, file_indent: IndentSpec, base_indent: str, operation, language) -> str`。仕様書 §10.9 を手順にする。

1. 改行を `"\n"` に統一し、末尾の空行は 1 行に詰める（本文は `"\n"` 1 つで終わる）。
2. 空行を除く全行の先頭空白の最長共通接頭辞を除く（文字列として比較。接頭辞にタブとスペースが混ざっていれば次の 3 で拒否される）。
3. `new_code` 内のインデント単位を推定する: 行頭空白の増分を集計し、タブかスペース幅（2 / 4 / 8）の多数派を採る。**タブで始まる行とスペースで始まる行が両方あれば** `E_INVALID_ARGUMENT`（`details.param: "new_code"`, `details.reason: "mixed_indent"`）。行の途中のタブは対象外。
4. 対象ファイルのインデント単位 `file_indent` は `TreeCache` の項目が持つ（`textpos.detect_indent` が同じ方法でファイル全体から推定。推定できなければ `LanguageSupport.indent_default`）。
5. 各行の先頭インデントを「深さ（単位数）」に直し、`file_indent` の単位 × 深さに置き換える。
6. `base_indent`（空行以外の全行に前置する文字列）: `replace_body` / `insert_before` / `insert_after` は元の定義ノードの先頭行の先頭空白をそのまま使う。`insert_member` は既存メンバの先頭行の先頭空白、メンバが無ければ親の先頭空白 + 1 単位。
7. 挿入時の空行: `insert_before` / `insert_after` は前後に言語の規定数（Python はモジュール直下 2・クラス内 1、他言語 1）を置く。既に隣にある空行は数に入れ、過剰に増やさない。`insert_member` は既存メンバの末尾との間に 1 行。`delete_symbol` は削除後に連続する空行が規定数を超える分を詰める。
8. `replace_body` で `new_code` が空文字なら `E_INVALID_ARGUMENT`（`details.hint: "削除は operation: delete_symbol を使う"`）。

結果は `"\n"` 区切り。改行コード・BOM への変換は writer が行う（§7.7.5）。

### 7.7.4 構文検証（syntax_check.py）

`check(original: bytes, hunks, support: LanguageSupport | None, *, had_errors_before: bool) -> SyntaxCheckResult`。

1. 対象ファイルの文法が無い（Tier 4）、または `Plan.expects_syntax == False` なら `"skipped"` を返す。Tier 3 のセレクタ置換（M2）はそのフォーマットのパーサ（`json` / `tomllib` / PyYAML / `xml.etree`）で読めることを確かめ、読めなければ `E_SYNTAX_INVALID`。
2. BOM を外したテキストに hunk を後ろから適用し、適用後のテキストと **適用後座標での編集範囲** を得る（`LineIndex.shift` で前方の hunk による行ずれを加算）。
3. `syntax/parsers.py` のプールから Parser を借り、**`TreeCache` を経由せず** 一時解析する。得られた構文木と新しいバイト列は `SyntaxCheckResult` に抱え、Written 成功後に Reindexed が新しいキー `(ws, path, sha_after)` で `TreeCache` に登録する（二重解析を避ける）。
4. エラーノードの集合を取る: 走査して `is_error` または `is_missing` のノードを `(start_line, end_line, kind, node_type)` で集める。編集前の集合は `TreeCache` にある元の木から同じ方法で取る。
5. 判定: `had_errors_before == False` ならファイル全体で新集合が空であること。そうでなければ、編集範囲（適用後座標）の前後 1 行を含む区間に、編集前の集合に無いエラーノードが無いこと。
6. 違反は `E_SYNTAX_INVALID`。`details.errors[]` は `{path, line, col, kind: "ERROR" | "MISSING", node_type, snippet}` を最大 10 件（`snippet` は該当行 200 文字）。`operation == "replace_body"` で `new_code` の先頭行が定義の開始語（`def` / `class` / `async def` / `function` / `fn` / `pub fn` / `impl` / `struct` / `enum` / `interface` / `type` / `export`）で始まらないときは `details.hint` に「`replace_body` はシグネチャを含む定義全体を渡す」を入れる。
7. 恒等判定: 適用後のバイト列（改行・BOM を戻した形）が元と同一なら `identical: true`。

### 7.7.5 原子的書き込みとジャーナル（writer.py）

ジャーナルの配置:

```
<cache_root>/<ws_hash>/journal/<edit_id>/
  manifest.json       # {edit_id, tool, started_at, state: "journaled"|"committed"|"rolled_back", files: [{path, sha256_before, mode, newline, bom, trailing_newline, size}], renames: [...]}
  pre/<path>          # 書き込み前の内容（ルート相対パスをそのまま配下に展開）
```

- `edit_id` は `"ed_" + 4 桁以上のゼロ埋め連番`。連番は `_meta.edit_seq` を起動時に読んでプロセス内で採番し、Journaled で `IndexWriter` に永続化を依頼する。
- 保持は直近 50 件。51 件目を作るときに最も古い `edit_id` のディレクトリを消す。

書き込み手順（`write_all(plan, new_contents: Mapping[path, str]) -> Mapping[path, sha256]`）:

1. **直前照合**: 各対象ファイルをディスクから読み直して sha を計算し、`resolved_shas` と比べる。不一致なら何も書かずに `E_PRECONDITION_FAILED`。読んだバイト列から改行コード（CRLF / LF / CR の多数派。混在時は先頭行）、BOM の有無、末尾改行の有無、モード（`st_mode`）を記録する（Resolved で記録済みの値と一致するはず。違えば同じく前提不一致）。
2. **符号化**: `"\n"` 区切りの本文を元の改行コードに変換し、BOM があれば先頭に付け、末尾改行は元に合わせる（元が改行で終わっていれば 1 つ、終わっていなければ無し）。UTF-8 で書く（元が UTF-8 以外なら Resolved の時点で `E_UNSUPPORTED`、`details.reason: "non_utf8_file"`）。
3. **段階 A（一時ファイル）**: 各ファイルについて同じディレクトリに `.lucianke-tmp-<edit_id>-<乱数 6 桁>` を作り、書き込み → `flush` → `os.fsync`。POSIX ではモードを元に合わせる（`os.chmod`）。Windows では読み取り専用属性を元に合わせる。
4. **段階 B（改名）**: すべての一時ファイルが書けてから、`os.replace(tmp, path)` を順に行う。`renames` はテキストの改名が終わったあとに `os.replace(old, new)`。
5. **Windows の共有違反**: `os.replace` が `PermissionError`（共有違反・アクセス拒否）を返したら 20 ms 待って再試行、最大 5 回（S-10 で挙動を確認）。それでも失敗なら 6 へ。
6. **ロールバック**: 段階 B で失敗したら、改名済みのファイルを `pre/` から同じ手順（一時ファイル → 改名）で戻し、残った一時ファイルを消し、`manifest.state = "rolled_back"` とする。`E_INTERNAL`（`details.rolled_back: true`, `details.log_ref`）。戻し自体に失敗したら `details.rolled_back: false` と `details.journal: <manifest のパス>` を返し、ログに `error` で残す。
7. **確定**: 全改名が終わったら各ファイルの sha を再計算して返し、`manifest.state = "committed"`。
8. POSIX ではディレクトリの `fsync` を行ってもよい（MAY）。

### 7.7.6 診断差分（verify.py）

仕様書 §10.7 の実装。

1. **編集前スナップショット**（Resolved で取る）: 対象ファイルと **直接の参照元ファイル** について `diagnostics` を読む。参照元は `relations` で `target_urn ∈ Plan.target_urns` かつ `source_file_id ≠ 対象` の `source_file_id` を、関係行数の多い順に最大 20 ファイル。それぞれの行を同一性キー `(file, source, code, message, enclosing_urn)` で辞書にする。行番号は使わない。
2. **対象ファイルの開き直し**: Reindexed で `lsp/documents.py` が対象ファイルに `didChange`（版 `v_n`）を送る。参照元ファイルは LSP が開いていなければ `didOpen` する（LSP は開いている文書にしか診断を再発行しないことが多いため。S-4 / S-5 で確認）。
3. **待ち**: `DiagnosticsWaiter.wait(paths, min_versions, timeout_ms=diagnostics_wait_ms)`。各パスについて「版 ≥ `v_n` の `publishDiagnostics` を受信した」ことを条件とする。版が付かない LSP では「`didChange` 送信時刻より後の受信」を条件にする。参照元は「`didOpen` / 最後の `didChange` 以後の受信」。`diagnostics_wait_ms == 0` なら待たず 5 へ。
4. **受信側**: `lsp/diagnostics.py` は受信のたびに `diagnostics` テーブルの当該ファイル行を全置換する（`IndexWriter`）。待ちの完了は、この書き込みが終わった後に通知する（EP-7: 直後の `get_diagnostics` と整合させるため）。
5. **差分**: 揃ったファイルについて、現在の `diagnostics` 行を同じキーで辞書にし、`added = after − before`、`resolved = before − after`。`added[]` / `resolved[]` の要素は T-08 の診断オブジェクトと同形（行番号は現在値）。
6. **状態**: 対象ファイルと参照元がすべて揃えば `"complete"`、1 つでも揃わなければ `"pending"`（揃った分だけ入れ、`checked_files[]` に `{path, fresh: bool}`）。`dry_run` で予測診断を行わなかったときは `"not_computed"`。
7. **LSP 不在**（`unavailable` / `standby` で起動不能 / Tier 2 の一部）: `tree-sitter` 由来の構文エラー診断（§7.7.4 の集合を T-08 の形にしたもの）だけで差分を取り、`checked_files` は対象ファイルのみ、`confidence = STRUCTURAL`。
8. **予測診断**（`dry_run` の MAY）: M1 では行わず常に `"not_computed"`。M2 以降、LSP が書き込みなしのオーバーレイ（`didChange` のみ）に対応することを確かめたうえで足す。

```python
@dataclass(frozen=True)
class DiagDelta:
    status: Literal["complete", "pending", "not_computed"]
    added: tuple[Mapping[str, Any], ...]
    resolved: tuple[Mapping[str, Any], ...]
    checked_files: tuple[Mapping[str, Any], ...]   # {path, fresh}
```

### 7.7.7 編集ツールからの使い方（T-10〜T-13 の plan_builder）

| ツール | `plan_builder` が作るもの | 固有の前段 | 再索引後の対応付け |
|---|---|---|---|
| T-10 `edit_symbol_body` | `replace_body`: `syntax/boundaries.py` の定義範囲（デコレータ・属性・直前 Doc コメント込み）を 1 hunk で置換。`insert_before` / `insert_after`: 定義範囲の直前・直後（行頭 / 行末の改行後）に空の範囲で挿入。`insert_member`: 親の最後のメンバの定義範囲末尾（無ければ本体先頭。Python の `pass` だけの本体は `pass` 文を置換）。`delete_symbol`: 定義範囲 + 行末の改行 + 規定数を超える後続空行 | `new_code` を §7.7.3 で正規化。`insert_member` の対象が本体を持たなければ `E_INVALID_ARGUMENT`。降格ファイルで終了行の候補が 2 つ以上なら `E_MUTATION_BLOCKED` | 再索引後、hunk の適用後開始行（デコレータを除いた最初の行）から始まるシンボルを `symbols` で引き、その URN を `target_urn`、範囲を `new_range` にする。`delete_symbol` は `None` |
| T-11 `rename_symbol` | `prepareRename` → `rename(newName)` → `WorkspaceEdit` → `plan.from_workspace_edit` | LSP が `ready` でなければ `E_LSP_UNAVAILABLE`。`new_name` が識別子でなければ `E_INVALID_ARGUMENT`。降格モード（字句置換）では対象ファイル内に `new_name` が単語として既出なら `E_MUTATION_BLOCKED` | Resolved で対象ファイル群の `symbols` を `(path, selection 開始位置)` つきで控え、再索引後に `LineIndex.shift`（前方 hunk の行・列ずれ）で位置を写像して突き合わせ、`qualified_name` が変わった全シンボルの旧 URN → 新 URN を `urn_map` にする |
| T-12 `structural_search_replace` | `syntax/structural.py` の一致ごとに `(range, replacement)` → hunk。`rewrite` 無しなら計画を作らず読み取りのみ | `pattern` / `rewrite` の解析失敗は `E_SYNTAX_INVALID`。一致数が `max_matches` を超えたら計画を作らず `applied: false`。`dry_run` 既定 `true` | 不要（`matches[]` を返す） |
| T-13 `apply_code_action` | `codeAction`（または保持中の ID）→ 必要なら `codeAction/resolve` → `edit` → `plan.from_workspace_edit` | ID の発行後に sha が変わっていれば `E_PRECONDITION_FAILED`（`code_action_id_expired`）。`command` だけのアクションは `E_UNSUPPORTED` | 不要。`target_diagnostic_resolved` は `DiagDelta.resolved` に ID の診断キーが含まれるかで決める |

責務の線引き:

| 責務 | `plan_builder`（ツール） | `EditTransaction` |
|---|---|---|
| 対象の解決と曖昧さの判定 | （`PlanContext` に解決済みで渡る） | ○ |
| 範囲の決定（境界規則） | ○（`boundaries.py` を呼ぶ） | |
| 本文の正規化 | ○（`normalize.py` を呼ぶ） | |
| LSP との往復（rename / codeAction） | ○ | |
| ロック、前提検証、インターロック、構文検証、ジャーナル、書き込み、再索引、診断差分、`diff` 生成、`EditResult` | | ○ |
| ツール固有の出力項目（`new_range`、`urn_map`、`matches[]`、`target_diagnostic_resolved`） | ○（`EditResult` から組み立てる） | |

## 7.8 Engine Selector（engine/selector.py、ADR-10）

### 7.8.1 入力と出力

```python
class WaitPolicy(StrEnum):
    WAIT_SHORT = "wait_short"   # LSP が ready なら要求を送り、LSP_TIMEOUT_* 内で待つ。超えたら降格
    NO_WAIT = "no_wait"         # LSP に同期要求を送らない。PaaD と構文木で答える

class EngineSelector:
    async def select(self, file: FileInfo, cap: Capability, *,
                     wait_policy: WaitPolicy | None = None) -> EngineChoice
```

参照するもの: `syntax/support.py` の登録表（文法の有無、`FormatAdapter` の有無、LSP 定義の有無）、`LspPool.status(language, workspace_id)`（`standby` / `starting` / `indexing` / `ready` / `unavailable` と進捗）、`files` 行の `tier` と `confidence`。

副作用は 1 つだけ: LSP 由来の `Capability` で状態が `standby` なら `LspPool.ensure_started(language, workspace_id)` を投げる（待たない）。これが仕様書 §12.3 の遅延起動の契機である。

### 7.8.2 判定表（Capability × ファイル状態 → エンジン・精度）

列は「文法あり」を LSP の状態で 3 つに分け、最後に「文法なし（Tier 4）」。セルは `engine / confidence`。`→` は `WAIT_SHORT` でタイムアウトしたときの降格先。

| Capability | 文法あり・LSP `ready` | 文法あり・LSP `starting` / `indexing` | 文法あり・LSP `unavailable` / 定義なし | 文法なし（Tier 4） |
|---|---|---|---|---|
| SKELETON | `tree-sitter` / exact（推論型の補完は `symbols.return_type` に既にある値だけ。同期 hover はしない） | `tree-sitter` / structural | `tree-sitter` / structural | `ctags` → `block-heuristic` / heuristic。何も取れなければ `internal_ripgrep_word_boundary` / lexical |
| SEARCH | `fts5` / exact | `fts5` / structural | `fts5` / structural | `fts5`（ctags 行）/ heuristic → grep / lexical |
| BODY | `tree-sitter` / exact | `tree-sitter` / structural | `tree-sitter` / structural | `ctags+block` / heuristic → grep ±15 行 / lexical |
| DEFINITION | `lsp` / exact → `tree-sitter+imports` / structural | `tree-sitter+imports` / structural | `tree-sitter+imports` / structural | `ctags` / heuristic → grep 定義パターン / lexical |
| REFERENCES | `lsp` / exact → `relations` / structural | `relations` / structural | `relations` / structural | grep 単語境界 / lexical |
| IMPLEMENTATIONS | `lsp` / exact → `relations(IMPLEMENTS, INHERITS)` / structural | 同左 structural | 同左 structural | `(unavailable)` |
| CALL_HIERARCHY | `lsp` / exact → `relations(CALLS)` / structural | `relations(CALLS)` / structural | `relations(CALLS)` / structural | grep `name(` 1 段 / lexical |
| DIAGNOSTICS | `lsp-cache+tree-sitter` / exact（受信済みキャッシュのみ。同期要求なし） | `tree-sitter` / structural | `tree-sitter` / structural | なし（0 件） |
| RENAME | `lsp` / exact | `lsp_unavailable`（ツールは `E_LSP_UNAVAILABLE`） | `lsp_unavailable` | `lexical-replace` / lexical（インターロック下） |
| CODE_ACTION | `lsp` / exact | `lsp_unavailable` | `lsp_unavailable` | `lsp_unavailable` |
| STRUCTURAL_EDIT | `ast-grep` / structural | `ast-grep` / structural | `ast-grep` / structural | 対象外（`warnings` に除外件数） |
| SYMBOL_EDIT | `tree-sitter` / exact（診断差分に LSP が効く） | `tree-sitter` / structural | `tree-sitter` / structural | `ctags+block` / heuristic → grep / lexical（インターロック下） |

- `indexing` の列では `EngineChoice.lsp_status = "indexing"`、`lsp_progress` に進捗を入れる。`starting` も `"indexing"` と表示し進捗は `null`。
- Tier 2 のアダプタ（`.ipynb`、SQL、IDL）は「文法あり」として扱い、LSP の列は当該言語の LSP（`.ipynb` は Python）に従う。Tier 3 のアダプタは常に `structural`。
- 判定はファイル単位。1 回の応答で複数ファイルが異なる列に落ちれば `mixed`（パッカーが集計。ADR-5）。

### 7.8.3 LSP を待つか待たないか（ADR-6 のポリシー表）

| Capability | 既定の `wait_policy` | 上限（`core/limits.py`） | 理由 |
|---|---|---|---|
| DEFINITION | `WAIT_SHORT` | `LSP_TIMEOUT_DEFINITION_MS = 3000` | 型解決済みの定義は構文木で代替できない |
| REFERENCES / IMPLEMENTATIONS | `WAIT_SHORT` | `LSP_TIMEOUT_REFERENCES_MS = 3000` | 同上。結果は `relations` に `exact` で書き戻し、次回は PaaD から答える |
| CALL_HIERARCHY | `WAIT_SHORT` | `LSP_TIMEOUT_CALL_HIERARCHY_MS = 5000` | 同上 |
| RENAME | `WAIT_SHORT` | `LSP_TIMEOUT_RENAME_MS = 10000` | 待たなければ提供できない |
| CODE_ACTION | `WAIT_SHORT` | `LSP_TIMEOUT_CODE_ACTION_MS = 5000` | 同上 |
| SKELETON / SEARCH / BODY | `NO_WAIT` | — | 構文木と PaaD で足りる |
| DIAGNOSTICS | `NO_WAIT` | — | 受信済みキャッシュを返す。編集ツールの診断待ちは `DiagnosticsWaiter` が別に担う |
| STRUCTURAL_EDIT / SYMBOL_EDIT | `NO_WAIT` | — | 境界と構文検証は構文木 |

`WAIT_SHORT` で上限を超えたら、その要求に限り右隣の列へ降格し、`warning` に `lsp_timeout` の雛形を入れる。LSP プロセスは落とさない（タイムアウトは `LspPool` が数え、連続 3 回で `unavailable` に遷移させる）。

### 7.8.4 警告文の雛形と is_degraded

- `is_degraded = choice.confidence < spec.max_confidence`（`ToolSpec.max_confidence`。T-12 は `STRUCTURAL`、T-00 / T-19 は常に `EXACT` で降格なし、T-20 は行から集計）。
- `accuracy_warning` は `is_degraded` のときだけ非 null。雛形は `engine/selector.py` に置き、`reason` で選ぶ:

| `reason` | 水準 | 雛形（日本語） |
|---|---|---|
| `lsp_indexing` | structural | 「LSP が起動・索引中（{progress}%）のため構文木で解決しました。型・オーバーロード・再エクスポート越しの参照は区別されていません。完了後に呼び直すと `exact` になります。」 |
| `lsp_unavailable` | structural | 仕様書 §8.4 の `structural` 雛形 + 「`configure_workspace status` で LSP の状態を確認してください。」 |
| `lsp_timeout` | structural | 「LSP の応答が {timeout} ms 内に得られなかったため構文木で解決しました。」 |
| `no_grammar_ctags` | heuristic | 仕様書 §8.4 の `heuristic` 雛形 |
| `no_grammar_lexical` | lexical_fallback | 仕様書 §8.4 の `lexical_fallback` 雛形 |
| `mixed` | （最低水準） | 上の雛形に「{n} 件のファイルがこの水準です（内訳は `confidence_breakdown`）。」を足す |

compact の `! DEGRADED: confidence=… engine=… lang=…` 行と 2 行目の `! Warning:` は `mcp/envelope.py` が `EngineChoice` から組み立てる。ツールは書かない。

## 7.9 Git Layer（git/）

### 7.9.1 M1 の範囲: リビジョン解決と仮想ツリー（vtree.py、仕様書 §8.5）

dulwich（Apache-2.0 側を選択。第 16 章、OD-20）を用いる想定で、M1 で必要なのは次の 3 つだけ（能力は S-8 で確認）。

```python
class GitRepo:                               # git/repo.py
    @classmethod
    def open(cls, root: Path) -> GitRepo | None      # .git が無ければ None（Git 無しでも Lucianke は動く）
    def resolve_revision(self, rev: str) -> ResolvedRevision
    def read_blob(self, commit_sha: str, path: str) -> bytes | None
    def read_staged_blob(self, path: str) -> bytes | None
    def list_tree(self, commit_sha: str, prefix: str = "") -> Iterator[TreeEntry]

@dataclass(frozen=True)
class ResolvedRevision:
    kind: Literal["WORKING_TREE", "STAGED", "COMMIT"]
    sha: str | None                           # COMMIT のとき 40 桁
    display: str                              # ヘッダの rev= に出す（入力をそのまま）
```

`resolve_revision` が受けるもの: `WORKING_TREE`、`STAGED`、`HEAD`、`HEAD~N` / `HEAD^`、ブランチ名（`refs/heads/`）、タグ名（`refs/tags/`、注釈付きタグは剥がす）、リモート追跡名（`refs/remotes/`）、40 桁 SHA、4 桁以上の一意な短縮 SHA。それ以外の構文（`@{...}`、範囲 `a..b`、`:path`）は `E_REVISION_NOT_FOUND`（`details.hint: "対応する指定: WORKING_TREE, STAGED, HEAD, HEAD~N, ブランチ, タグ, SHA"`）。短縮 SHA の一意性確認がオブジェクト数に比例して遅い場合は、`git_commits`（M2 で埋まる）を先に引き、無ければ走査する（S-8）。

`VirtualTree`（要求の間だけ生きる）:

1. `VirtualTree.open(ws, resolved)` はメモリ上の SQLite（`:memory:`）に `index/schema.sql` と同じ DDL を作る。永続 DB には触れない（MUST）。
2. `source(path) -> bytes` は `read_blob` / `read_staged_blob` で取り、`TreeCache` にはキー `(ws, path, sha256(blob))` で載せる（内容ハッシュなので作業ツリーの同内容と自然に共有される）。
3. 要求されたパスだけを解析して `symbols` に入れる（遅延）。T-05 の `symbol_name` 解決は、この一時 `symbols` に対して §7.1.3 の規則を適用する。
4. `ToolContext.file_source(path)` は `revision` が `WORKING_TREE` なら作業ツリー、それ以外なら `VirtualTree` を見る。T-02 / T-05 はこの 1 つの入口だけを使い、`revision` を意識しない。
5. 応答のヘッダは `rev=<display>`、`engine_used = "tree-sitter@<短縮 SHA>"`、最高水準 `structural`（LSP を使わない）、`accuracy_warning` に「過去断面のため LSP を使わず構文木で解析した」。
6. パス名は `workspace/paths.py` の封じ込めを通す。Git のツリー項目に `..` や絶対パスが含まれていれば拒否する。
7. 検査: 任意の `revision` 指定の読み取りの前後で、作業ツリー全ファイルの sha と `.git` 配下の sha が変わらない（M2-5 の受入をこの形で機械化する。M1 でも T-02 / T-05 に対して走らせる）。

M1 で `revision` を受けるのは T-02 と T-05 だけ（仕様書 §8.5）。他の読み取りツールは `E_UNSUPPORTED`（`details.reason: "revision_not_supported_until_M2"`）。

### 7.9.2 M2 の境界: 履歴・共変更（history.py、cochange.py）

内部設計は M2 着手時に本書を改版して足す。ここでは境界と不変の規約だけを置く。

- `history.symbol_changes(repo, path, *, since, max_commits) -> list[SymbolChange]`: ファイル名変更を追跡した履歴（`git log --follow` 相当。dulwich の walker と改名検出の能力は S-8）を新しい順に辿り、各コミットの前後の blob を `SyntaxEngine` で `SymbolRecord` 集合にして、`(qualified_name, kind)` で突き合わせ、`body_sha256` の異同と正規化シグネチャの異同で `ADDED` / `DELETED` / `MODIFIED_SIG` / `MODIFIED_BODY` / `RENAMED` を決める（仕様書 T-17 処理規約 3）。結果は `git_symbol_changes` に `IndexWriter` 経由で追記し、2 回目以降は索引済み範囲を飛ばす。
- `cochange.aggregate(repo, *, window=CO_CHANGE_WINDOW)`（`CO_CHANGE_WINDOW = 500`）: 直近 `window` コミットの変更ファイル集合から対 `(a, b)` の共起回数と `a` の変更回数を数え、`git_co_changes` を全置換する。シンボル単位は `git_symbol_changes` から同様に。`HEAD` の移動（`.git/HEAD` と `refs/` の監視、または要求時の照合）で増分更新する。
- Git 層は作業ツリーを書かない（MUST）。シェルの `git` を起動しない（SHOULD NOT）。
- `git_commits` の取り込みは `cochange` / `history` の前段として `repo.walk(max=window)` で行う。

## 7.10 Verification Core（verify/）の境界

M2 / M3 の内部設計は着手時に足す。置き場所、入口、守る規約だけを定める。

| モジュール | M | 入口 | 中身の要点 |
|---|---|---|---|
| `verify/paths.py` | M2 | `find_paths(reader, source, sink, *, flow_type, max_hops, max_paths, filter, timeout_ms) -> PathSearchResult` | `relations(CALLS)` と `dataflows` に対する再帰 CTE（SQLite）。Datalog は OD-3 のとおり保留。結果順は決定的（ホップ数 → パス・行の辞書順） |
| `verify/dataflow.py` | M2 | `extract_intraprocedural(tree, support) -> list[DataflowRow]` | 手続き内の Def-Use 連鎖を構文木から取り `dataflows` に書く（`structural`）。手続き間は `exact` 水準（LSP 解決済みの `relations`）があるときだけ連結 |
| `verify/contracts/extract.py` | M3 | `extract(file_source, support, *, symbol, sources, include_smt) -> list[Contract]` | 型・ガード節・構造化 Doc コメントは `LanguageSupport` が持つクエリで、OpenAPI / JSON Schema は PyYAML / `json`、Gherkin は自前の行パーサ。`contract_id` は仕様書 T-15 の決定論的ハッシュ |
| `verify/contracts/exprlang.py` | M3 | `parse(expr: str) -> Expr`、`to_smtlib(Expr) -> str`、`to_z3(Expr, env) -> z3.ExprRef` | 仕様書 T-16 の制約式文法。LL(1) の手書きパーサ |
| `verify/ssa.py` | M3 | `lower(tree, support, *, unroll) -> SsaFunction` | 対象構文は仕様書 T-16 処理規約 4 の範囲。範囲外は `unsupported_construct` |
| `verify/solver.py` | M3 | `check(ssa, pre, post, inv, *, implicit_safety, timeout_ms, seed) -> VerifyResult` | z3-solver（MIT）。乱数シード固定、`timeout_ms` をソルバに渡す |

規約:

- `verify/` は `mcp` / `tools` を import しない。入力は読み取り接続とファイル内容、出力はレコードとテーブル行だけ。
- 外部 LLM API を呼ばない、ネットワークに出ない（MUST。仕様書 §13.7）。`verify/` 配下に HTTP クライアントの import が無いことを `test_import_graph` で検査する。
- 重い依存（z3-solver、sqlglot）は extras `lucianke[verify]` に置き、未導入なら T-14〜T-16 は `E_UNSUPPORTED`（`details.reason: "extra_not_installed"`）。

---

# 8. 主要シーケンス

各シーケンスは「番号付き手順（関与モジュール）」と失敗分岐で書く。`timing_ms` の範囲は NFR-PERF-1 / NFR-PERF-2 の判定に使う区間を示す。

## 8.1 起動と差分索引

```
host ──spawn──► lucianke serve
 1. bootstrap_io          fd 1 を退避、fd 1 → stderr（ADR-3）
 2. logging_setup         stderr + JSON Lines ファイル
 3. config                環境変数 / .lucianke.toml / 既定
 4. cache dir             <cache_root>/<ws_hash>/ を用意（ws_hash = sha1(実体パス)[:12]）
 5. index.writer          IndexWriter 起動、schema 版確認・移行、_meta（data_version, edit_seq）読込
 6. mcp.server            initialize 受付 → tools/list 提供可能
 7. roots/list            WorkspaceContext.register（マーカー走査、Python 環境検出、workspace_id）
 8. ingest job（背景）    ファイル列挙（.gitignore + exclude_globs）→ files.sha256 と比較 → 差分だけ解析（プール）→ ingest（writer）
 9. workspace.watcher     監視開始
```

- 8 は要求の受付を止めない。索引が終わるまで `search_workspace_symbols` などは部分的な結果を返し、`configure_workspace status` の `jobs[]` に進捗が出る。
- LSP はここでは起動しない（最初の該当要求で §8.3）。
- 失敗分岐: キャッシュディレクトリに書けない → 一時ディレクトリに切り替えて `warnings` と `warn` ログ。スキーマ移行に失敗 → DB を作り直して全索引（ログに理由）。`.git` が無い → `GitRepo.open` が `None`、Git 系は `E_UNSUPPORTED`。
- 計測: `startup_ms`（1〜6）、`initial_index_ms`（8 の完了）をログに残す。合否には使わない。

## 8.2 読み取りツール（get_file_skeleton）の往復

```
host ─tools/call(get_file_skeleton, {paths:[a.py, b.py]})─► mcp.server
  ┌─ timing_ms（NFR-PERF-1 の区間）──────────────────────────────────────────┐
  │ 1. mcp.pipeline      spec を引く → args 検証（pydantic）→ ToolContext（workspace, revision, format, budget）
  │ 2. tools.t02         各 path:
  │      2a. workspace.paths   封じ込め → ルート相対
  │      2b. index.queries     files 行（language, tier, sha256）。無ければ項目エラー E_TARGET_NOT_FOUND
  │      2c. syntax.parsers    TreeCache.get_or_parse(ws, path, sha, load)。ヒットなら解析なし。ミスなら to_thread で読込 + 解析
  │      2d. engine.selector   select(file, SKELETON, NO_WAIT) → EngineChoice
  │      2e. syntax.skeleton   構文木 → Record 列（優先順位: 最上位 > doc > urn > 非公開 > 入れ子）
  │ 3. mcp.budget        Packer.pack（予算、next_cursor、summary、精度集計）
  │ 4. mcp.render        compact / json / …
  │ 5. mcp.envelope      ヘッダ、! DEGRADED、timing_ms
  └──────────────────────────────────────────────────────────────────────────┘
host ◄─CallToolResult(content, structuredContent?, isError=false)─┘
```

- NFR-PERF-1 は 2c がキャッシュヒットする定常状態で測る（仕様書 §14.4 のウォームアップ後）。ミス時の時間は別に記録する。
- 失敗分岐: 一部の path が無い → `data.items[].error`、全体は `ok: true` と `warnings`（仕様書 §5.4.4）。全件失敗 → 最初の項目のコードで `ok: false`。`max_tokens` が小さく 1 件も入らない → レコード 0 件 + `next_cursor` + `warnings`。

## 8.3 LSP の遅延起動と降格応答（resolve_symbols）

1 回目（LSP `standby`）:

```
 1. mcp.pipeline       検証・文脈
 2. tools.t04          target を ctx.resolve_target → symbols 行（structural で索引済み）
 3. engine.selector    select(file, DEFINITION, WAIT_SHORT)
      LspPool.status(python) == standby
      → LspPool.ensure_started(python, ws)   ← 待たない。ProcessRegistry 経由で spawn、initialize を送る
      → EngineChoice(engine="tree-sitter+imports", confidence=STRUCTURAL, lsp_status="indexing", progress=None,
                     warning=reason:lsp_indexing)
 4. syntax.imports     import 文 → モジュールパス → ワークスペース内ファイル → 宣言行（structural）
 5. Packer / render    ヘッダ lsp=indexing、! DEGRADED 行つきで応答
```

背景（応答後）:

```
 lsp.pool    starting → initialize 応答 → initialized → indexing（$/progress を購読）→ 最初の publishDiagnostics または進捗完了 → ready
 lsp.documents  要求で触れたファイルを didOpen（LRU 200）
 lsp.diagnostics  publishDiagnostics 受信 → diagnostics テーブル全置換（writer）
```

2 回目（LSP `ready`）:

```
  ┌─ timing_ms（NFR-PERF-1 の区間。LSP 往復を含む）──────────────────────────┐
  │ 3'. engine.selector  select(...) → EngineChoice(engine="pyright+tree-sitter", confidence=EXACT, lsp_status="ready")
  │ 4'. lsp.features     definition(path, pos)（LSP_TIMEOUT_DEFINITION_MS=3000）→ 位置を LineIndex で変換 → 定義 SymbolRecord
  │                      hover で展開済み型（signature_expanded）。結果を relations / symbols に exact で書き戻す（writer、待たない）
  │ 5'. Packer / render  conf=exact で応答
  └──────────────────────────────────────────────────────────────────────────┘
```

- タイムアウト: 4' が `LSP_TIMEOUT_DEFINITION_MS` を超えたら、その要求だけ 4 の経路（structural）で答え、`warning = lsp_timeout`。`LspPool` はタイムアウトを数え、連続 3 回で `unavailable` へ。
- 2 回目の区間に LSP 往復が入るため、NFR-PERF-1 の達成は LSP の応答速度に依存する。緩和: 一度 `exact` で書き戻した定義・参照は次回 PaaD から答える（`relations` / `symbols` の `exact` 行があれば LSP を呼ばない）。これはベンチマーク S-9 の計測対象（親が §13 のリスクに載せる）。
- 失敗分岐: LSP の実行ファイルが見つからない → `status = unavailable`、以後は structural 固定、`configure_workspace status` に理由。起動してもクラッシュ → 1 s / 2 s / 4 s のバックオフで 3 回まで再起動。

## 8.4 編集（edit_symbol_body）の往復

```
host ─tools/call(edit_symbol_body, {target, operation, new_code, expected_file_sha256})─►
  ┌─ timing_ms（NFR-PERF-1 の区間は diagnostics_wait_ms=0 で測る: 1〜7 と応答）──┐
  │ 1. Resolved      edit.transaction  target 解決、bytes/LineIndex/tree（TreeCache）、resolved_shas、EngineChoice、
  │                                    診断スナップショット（index.queries: diagnostics + 直接参照元 ≤ 20 ファイル）
  │ 2. Planned       tools.t10         boundaries.definition_range → normalize(new_code) → Plan（1 hunk）
  │ ── lock ─────────edit.lock         ワークスペースロック取得（≤ 5000 ms、超過 E_TIMEOUT）
  │ 3. Preconditions edit.transaction  expected_file_sha256 照合 → Watcher.drain → resolved_shas 再照合 → インターロック
  │ 4. SyntaxChecked edit.syntax_check メモリ上適用 → 一時解析 → 新規 ERROR/MISSING 判定 → 恒等判定
  │ 5. (dry_run)     → diff を作って応答、ロック解放、終了
  │ 6. Journaled     edit.writer       edit_id 採番、pre/ へ退避、manifest(journaled)
  │ 7a. Written      edit.writer       直前照合 → 一時ファイル → fsync → os.replace（Windows 再試行）→ manifest(committed)
  │ 7b. Reindexed    syntax.parsers    TreeCache: 旧キー無効化、4 の木を新キーで登録
  │                  index.ingest      ファイル取り込み（同期で完了を待つ）→ data_version++
  │                  lsp.documents     didChange(v_n)（未オープンなら didOpen）
  │                  workspace.watcher expect(path, sha_after)
  │ ── unlock ───────edit.lock
  └────────────────────────────────────────────────────────────────────────────┘
  8. Diagnosed       edit.verify       参照元を didOpen → DiagnosticsWaiter.wait(paths, v_n, diagnostics_wait_ms)
                                        → diagnostics 現在行 − スナップショット → DiagDelta(status)
  9. 応答            tools.t10 → Packer → render（applied:, files_changed:, syntax_check:, new_diagnostics: と diag-line）
```

- 失敗分岐と不変条件: 3 で不一致 → `E_PRECONDITION_FAILED`（I-1）。4 で違反 → `E_SYNTAX_INVALID`（I-5）。7a の改名途中で失敗 → ロールバック → `E_INTERNAL(rolled_back)`（I-2）。8 で揃わない → `status: "pending"`、エージェントは `get_diagnostics` で後追い。
- 恒等編集は 4 で検出して 6〜7 を飛ばす（I-6）。
- 外部プロセスが 1〜7a の間にファイルを書いた場合、3 または 7a の照合で検知する（I-3、EP-4）。監視イベントの到着が遅れていても 7a の直前照合はディスクを読むので取りこぼさない。

## 8.5 外部変更の反映

```
外部（エージェントの別ツール / エディタ / git checkout）がファイルを書く
 1. workspace.watcher   watchfiles のイベント（.gitignore と exclude_globs で除外）
 2. デバウンス           パスごとに 50 ms まとめる
 3. 自分の書き込みか     Watcher.expect に (path, sha) が登録済みなら消費して終わり（§8.4 の 7b で登録）
 4. 読込 + sha256        to_thread。files.sha256 と同じなら終わり（内容が変わっていない保存）
 5. 解析                 syntax.parsers（プール）→ 構文木。解析エラーがあっても続行（tree-sitter 診断になる）
 6. 取り込み             index.ingest（writer）: files / symbols / relations(IMPORTS, 構文木由来) / diagnostics(tree-sitter) を 1 トランザクションで差分更新 → data_version++
 7. キャッシュ            TreeCache: 旧キー無効化、新キー登録
 8. LSP へ通知           lsp.pool: 起動中の該当言語に workspace/didChangeWatchedFiles。文書が開いていれば didChange（全文）
 9. 診断の到着（非同期） lsp.diagnostics → diagnostics 全置換（writer）
10. 失効                 data_version が進んだので、以後の cursor は E_CURSOR_EXPIRED（§8.6）
```

- 削除: `files` 行を消す（`ON DELETE CASCADE` で `symbols` / `relations` / `diagnostics` が落ちる）、`didChangeWatchedFiles(Deleted)`、開いていれば `didClose`。
- 1 バーストで 500 ファイル超（ブランチ切り替え）: 個別処理を止めて §8.1 の手順 8 の差分索引を 1 回走らせる。
- 監視が使えない環境: `WorkspaceContext.watcher_mode = "polling"`。要求時に対象ファイルの sha を照合し、違えば 4〜8 をその場で行う。
- NFR-PERF-3 の区間は 1 の受信から 6 の `data_version++` と 8 の送信まで（p95 ≤ 500 ms）。

## 8.6 カーソル継続と失効

```
1 回目: tools/call(find_references_and_implementations, {target, max_tokens: 800})
  Packer  → records[0:k] を詰め、残りがあるので CursorCodec.encode({t, a: sha1(args)[:12], o: k, v: data_version, w: ws})
  応答    → # more: cursor=<token> remaining≈N

2 回目: tools/call(同ツール, {同じ引数, cursor: <token>})
  pipeline → decode → t / a / w が一致しなければ E_INVALID_ARGUMENT（details.param: "cursor"）
           → v != 現在の data_version なら E_CURSOR_EXPIRED（details.reason: "data_changed"）
           → 一致なら ctx.cursor_offset = o
  handler  → 同じクエリを同じ順序で再実行（ORDER BY に完全なタイブレークを付ける。MUST）
  Packer   → records[o:] から詰める
```

- 決定性の規約: 同じ `(args, data_version)` に対して handler は同じ順序のレコード列を返さなければならない（MUST）。これが満たされないとページ間で重複・欠落が出る（T-06 事後条件「同じ `(path, line, col)` は 1 度しか現れない」）。
- `data_version` は索引の書き込み（§8.4 の 7b、§8.5 の 6）で進み、診断の全置換（§8.5 の 9）では進まない（ADR-8）。したがって LSP の診断が後から届いても参照一覧のカーソルは生きている。

## 8.7 終了

```
契機: stdin EOF（ホスト終了）/ SIGTERM / SIGINT / Windows: SIGBREAK・親プロセス終了
 1. lifecycle       受付停止: 以後の tools/call は E_UNSUPPORTED（details.reason: "shutting_down"）
 2. edit.lock       実行中の編集トランザクションの完了またはロールバックを最大 5 s 待つ（Written の途中なら完了させる。Journaled 以前なら中断）
 3. workspace.watcher  停止
 4. lsp.pool        各 LSP に shutdown → exit → 500 ms 待ち → terminate → kill（ProcessRegistry の台帳順）
 5. lifecycle       ProcessRegistry に残った子（ctags / rg など）を kill
 6. index.writer    キューを空にする → WAL チェックポイント → 接続を閉じる
 7. 終了コード 0
```

- Windows では `loop.add_signal_handler` が使えないため、`signal.signal` でフラグを立て、ループ上の監視タスクが 1 を始める。`CTRL_BREAK_EVENT` を LSP の子プロセスに送るのは、子を新しいプロセスグループで起動している場合だけ（ADR-12）。
- 受入（NFR-OPS-2）: 7 の 1 秒後に、起動した子プロセスの PID がすべて存在しない。`ProcessRegistry` は起動時刻と PID を JSON Lines ログに残し、テストはそれを読んで確かめる。
- `stdin` EOF の検知は MCP SDK のトランスポート終了に乗る（S-1 で、EOF 後にサーバーのタスクが戻ることを確認）。

---
# 9. 横断関心事

## 9.1 エラー処理

| 層 | 規約 |
|---|---|
| サービス | 仕様書 §5.8 のコードを持つ `LuciankeError` を送出する。それ以外の例外は送出元のバグとみなす |
| パイプライン | `LuciankeError` → エンベロープ（`ok: false`、`isError: true`）。その他の例外 → スタックをログ（`log_ref` 付き）→ `E_INTERNAL` |
| 部分結果 | `E_TIMEOUT` は `LuciankeError.partial` に `ToolOutput` を載せ、パイプラインが `data` に詰める（`details.partial: true`） |
| バッチ項目 | 仕様書 §5.4.4。項目の失敗はレコードに `ok: false` と `error` を入れて続行する。全件失敗のときだけツール全体を失敗にする |
| LSP の失敗 | `features.py` が捕捉し、`EngineChoice` を 1 段下げて続行（ADR-10）。`rename` / `codeAction` のように代替が無い操作だけ `E_LSP_UNAVAILABLE` |
| 書き込みの失敗 | `EditTransaction` がロールバックし、`E_INTERNAL`（`details.rolled_back: true`）。部分書き込みを残さない（EP-9） |

## 9.2 ログ

- `logging` の JSON Lines フォーマッタを `logging_setup.py` に置く。出力先は `stderr`（人向け 1 行）とファイル（JSON Lines、日次ローテーション、7 日）。
- 必ず残すフィールド: `ts`, `level`, `event`, `request_id`, `tool`, `workspace_id`, `timing_ms`, `confidence`, `error_code`, `edit_id`, `lsp.language`, `lsp.state`。
- 残さないもの: ファイル本文、`new_code`、秘密らしき値。`debug` でもスニペットは 200 文字まで（仕様書 §12.6）。
- 受入: ログファイルの各行が JSON として解析でき、`event` と `request_id` を持つ。

## 9.3 設定

| 層 | 実装 |
|---|---|
| 既定 | `config.Settings` の既定値（`core/limits.py` の定数を参照） |
| `.lucianke.toml` | ルート直下。`settings_file.py` が読む。書かない。キー: `exclude_globs`, `python_interpreter`, `lsp.<lang>.cmd`, `test_globs`, `co_change_window`, `max_file_bytes` |
| 環境変数 | `LUCIANKE_CACHE_DIR`, `LUCIANKE_LOG_LEVEL`, `LUCIANKE_LSP_<LANG>_CMD`, `LUCIANKE_DISABLE_LSP`, `LUCIANKE_MAX_FILE_BYTES` |
| ツール | `configure_workspace` の設定は `workspaces` 行に保存し、次回起動でも使う |

優先順位: ツール > 環境変数 > `.lucianke.toml` > 既定（仕様書 §12.7）。`Settings` は不変で、変更は `WorkspaceContext` が新しい `Settings` を作って差し替える。

## 9.4 セキュリティの実装点

| 仕様書 | 実装点 |
|---|---|
| 仕様書 §13.1 パス封じ込め | `workspace/paths.py` の `confine(ws, raw) -> RelPath`。すべてのファイルアクセスはこの戻り値だけを受け取る型（`RelPath`）にする。生の `str` を受け取る I/O 関数を置かない |
| 仕様書 §13.2 DB 読み取り専用 | `index/public.py`（3 重防御）。`tests/invariants/test_public_db_readonly.py` が書き込み文の全パターンを投げて拒否と DB ハッシュ不変を確かめる |
| 仕様書 §13.3 子プロセス | `lifecycle.ProcessRegistry.spawn(argv, cwd, env)`。`shell=True` を使う箇所をコードベースから静的に禁止（lint） |
| 仕様書 §13.5 秘密ファイル | `languages.py` が秘密パターンに一致したファイルを `tier=4, language="secret"` にし、`FormatAdapter` を割り当てない。骨格・本体は `E_UNSUPPORTED`（`details.reason: "secret_file"`） |
| 仕様書 §13.6 入力検証 | pydantic（`extra="forbid"`）、正規表現は `re` のタイムアウトが無いため、長さ上限（1,000 文字）と `regex` パターンの事前検査（ネストした量化子の拒否）＋スレッドでの評価時間監視（`REGEX_TIMEOUT_MS`）で代替する |
| 仕様書 §13.7 外部通信禁止 | 依存のうち実行時に外へ出るものを入れない（L-6）。`tests/invariants/test_no_network.py` が全ツールを実行中に `socket.socket` をモンキーパッチして外向き接続が無いことを確かめる |

## 9.5 OS 差の扱い（Windows を第一級に）

| 事項 | 扱い |
|---|---|
| パス | 内部は常に `/` 区切りの相対パス。大文字小文字を区別しないファイルシステムでは、実体パスの大文字小文字を正とし、`confine` で正規化する |
| 改行 | ファイルの多数派を検出して保持（`core/textpos.py`）。`git` の `autocrlf` に依存しない |
| 原子的改名 | `os.replace`。Windows で共有違反なら再試行（S-10） |
| シグナル | `SIGINT` / `SIGTERM`（POSIX）、`SIGBREAK` と `stdin` EOF（Windows）。`loop.add_signal_handler` が無い環境では `signal.signal` + フラグ |
| 子プロセス | Windows では `CREATE_NEW_PROCESS_GROUP`。終了は `terminate` → `kill`。POSIX では `SIGTERM` → `SIGKILL` |
| ファイル監視 | `watchfiles`（Rust 製バックエンド。Windows 対応の記述あり。S-6 で挙動確認）。利用不可ならポーリング（仕様書 §12.4 の 6） |
| 実行ビット | POSIX のみ保持。Windows では無視 |
| Python 環境 | `.venv/Scripts/python.exe`（Windows）と `.venv/bin/python`（POSIX）の両方を探索 |
| キャッシュ場所 | `%LOCALAPPDATA%\lucianke\cache`、`~/Library/Caches/lucianke`、`~/.cache/lucianke`（`platformdirs` は使わず自前で 3 分岐。依存を増やさない） |

## 9.6 文字コードとエンコーディング

- ファイルは UTF-8 として読む。BOM があれば記録して保持する。UTF-8 として読めないファイルは `files.language = "binary"`、`tier = 4` とし、骨格・本体は `E_UNSUPPORTED`。
- Tree-sitter はバイト列で解析し、ノード範囲はバイトオフセットで返る。`LineIndex` がバイト → コードポイントに変換する（ADR-14）。
- JSON 出力は `ensure_ascii=False`。

---

# 10. 品質戦略

## 10.1 テスト階層

| 階層 | 対象 | 速さ | 実 LSP |
|---|---|---|---|
| unit | `core`、`budget`（パッカー・推定器・カーソル）、`textpos`、`urn`、`boundaries`、`normalize`、`writer`、`paths`、`languages` | 秒 | 不要 |
| contract | 仕様書 §11 の事後条件を T-nn ごとに。フィクスチャ F-POLY と小さな Python / TS プロジェクト。LSP は偽物（録画した応答を返す `FakeLsp`） | 分 | 不要 |
| e2e | 実 LSP（basedpyright、ruff、vtsls）と実 MCP クライアント（SDK のクライアント）で、`.mcp.json` に書くのと同じ起動方法 | 分〜十分 | 要 |
| invariants | `stdout` 純度、子プロセス 0、読み取りツールでの `sha256` 不変、公開 DB の読み取り専用、外向き接続なし | 全テストに横断して適用 | |
| bench | 仕様書第 14 章 | 別実行 | 要 |

- `FakeLsp` は実 LSP との対話を一度録画して JSON に保存し、以後はそれを再生する。録画の更新は e2e と同じ環境で行う。
- Windows / macOS / Linux の 3 OS で unit・contract・invariants を回す（CI）。e2e は各 OS で基本シナリオだけ。

## 10.2 契約テストの作り方

- 仕様書 §11 の各ツールの「事後条件」を 1 項目 1 テストに写す。テスト名は `test_T02_postcondition_3` のように仕様書の番号を含める。
- Compact DSL の文法適合は、仕様書 §6.1 の EBNF から起こした小さなパーサ（`tests/contract/compact_grammar.py`）に全ツールの compact 出力を通して検査する。文法とパーサの乖離は仕様書の改版で直す。
- 事後条件が「LSP の結果と等しい」と言うものは、`FakeLsp` の録画と比較する。

## 10.3 編集の不変条件テスト

- EP-1〜EP-9 を `tests/unit/test_edit_transaction.py` で。外部変更の競合（EP-4）は、段 1 と段 6 の間に差し込むフックでファイルを書き換えて再現する。
- 恒等編集（EP-6）はフィクスチャの全シンボルに対して実行し、バイト一致を確かめる（CRLF・BOM・末尾改行なしのファイルを含める）。

## 10.4 ベンチマーク

- `bench/run_bench.py` が仕様書 §14.3〜14.6 を実行し、`bench/results/<日時>-<OS>.json` を書く。
- トークン推定の較正（S-12）は bench の中で行う。基準のトークナイザは bench の開発依存にだけ入れ、製品の依存には入れない（L-6）。
- 結果の要約は `.pair-agent/acceptance/` に置く。

## 10.5 CI

| ジョブ | 内容 |
|---|---|
| lint | `ruff check`、`ruff format --check`、import 方向の検査（§4.2）、`shell=True` 禁止 |
| typecheck | basedpyright（strict ではなく standard から始め、`core` と `edit` は strict） |
| test | unit + contract + invariants（3 OS × Python 3.12 / 3.13） |
| e2e | 1 OS ずつ基本シナリオ |
| license | `pip-licenses` で依存木の SPDX を許可リストと照合（仕様書第 16 章 L-8）。`THIRD_PARTY_NOTICES.md` を生成して差分が無いことを確認 |
| schema | `index/schema.sql` と仕様書 §9.2 の DDL の一致。内部テーブル（`_meta`, `_hover_cache`）と `symbols_fts` の追従トリガは許容リストで除外 |

## 10.6 受入検査の役への引き渡し

- 受入検査は `tests/contract` と `bench` を実行し、結果を `.pair-agent/acceptance/` に残す。人手の操作を要する項目を作らない（仕様書 §14.1）。
- M1 の出口条件（仕様書 §15.2）を 1 項目 1 スクリプトに対応づけた表を `bench/exit_criteria.toml` に置く。

---

# 11. パッケージングと配布

## 11.1 `pyproject.toml` の骨子

```toml
[project]
name = "lucianke"
requires-python = ">=3.12"
license = "MIT"
dependencies = [
  "mcp",                 # MCP SDK（版は S-1 のあと固定）
  "pydantic",
  "tree-sitter",
  "tree-sitter-python", "tree-sitter-typescript", "tree-sitter-javascript",
  "tree-sitter-json", "tree-sitter-yaml", "tree-sitter-toml", "tree-sitter-xml", "tree-sitter-markdown",
  "watchfiles",
  "PyYAML",
  "jsonpath-ng",
  "dulwich",             # M1 から必須: revision 解決と blob 取得（T-02 / T-05）、.gitignore の評価。Apache-2.0 側を選ぶ（S-8）
]

[project.optional-dependencies]
langs   = ["tree-sitter-rust", "tree-sitter-go", "tree-sitter-c", "tree-sitter-cpp", "tree-sitter-c-sharp", "tree-sitter-rst", "tree-sitter-sql"]
python  = ["basedpyright", "ruff"]          # Python 系 LSP を pip で入れたい人向け（Node 同梱）
edit    = ["ast-grep-py"]                   # M2: structural_search_replace
verify  = ["z3-solver", "sqlglot"]          # M3 / M2
xml     = ["lxml"]                          # XPath が必要なとき
all     = ["lucianke[langs,python,edit,verify,xml]"]

[project.scripts]
lucianke = "lucianke.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- 依存の版は M1 着手時に固定し、`uv.lock` をリポジトリに置く。
- `jq`（PyPI）は M2 の `jq:` セレクタを採用する場合にだけ `[xml]` 相当の extras に加える（仕様書 §16.2.1）。

## 11.2 導入と起動

```jsonc
// .mcp.json（Claude Code の例）
{
  "mcpServers": {
    "lucianke": {
      "command": "lucianke",
      "args": ["serve"],
      "env": { "LUCIANKE_LOG_LEVEL": "info" }
    }
  }
}
```

- `uv tool install lucianke[all]` または `pipx install lucianke[all]`。
- `lucianke doctor` が、PATH 上の LSP・Node・ctags・git の有無、キャッシュディレクトリの書き込み可否、`stdout` 隔離の自己診断を表示する（`stderr` へ）。
- `lucianke index <root>` が事前索引を行い、初回の `serve` を速くする（任意）。

## 11.3 版管理

- セマンティックバージョニング。ツールの入出力の互換性を壊す変更はメジャーを上げる。
- `tools/list` の各ツールに `description` 末尾で `lucianke/<version>` を出す（エージェントが版を知れる）。

---

# 12. 技術検証（スパイク）と M1 の着手順

## 12.1 スパイク一覧

各スパイクは「何を確かめるか・合格条件・不合格時の代替」を持つ。結果は `docs/spikes/S-nn.md` に 1 画面で残し、本書の該当箇所を改版する。

| ID | 確かめること | 合格条件 | 不合格時の代替 |
|---|---|---|---|
| S-1 | `mcp` 2.x の低水準サーバーで、`tools/list` のスキーマを自前 JSON で返せるか、`tools/call` の結果に `structuredContent` と `isError` を載せられるか、`roots/list` を要求できるか、トランスポートに任意のバイナリストリーム（退避 fd）を渡せるか。あわせて、HTTP 系の依存（starlette / uvicorn 等）が extras で外せるか、外せないなら stdio 起動時に import されないか | 4 点とも可能で、SDK クライアントから往復できる | 高水準 API へ切替（スキーマは後処理で `additionalProperties: false` を付ける）。ストリームが渡せなければ `sys.stdout` を退避 fd のラッパに戻す |
| S-2 | `tree-sitter` 0.26 の API（`Language(mod.language())`、`Query` / `QueryCursor`）と、2024 年で更新が止まった文法（typescript / cpp / json / xml / toml）の ABI 互換 | 対象 7 文法で解析・タグクエリが通る | 互換しない文法は版を下げるか、`tree-sitter-language-pack` を避けて個別に再ビルド（ライセンス再確認） |
| S-3 | `ast-grep-py` でパターン検索と書き換え案（メタ変数展開）が得られるか | `print($MSG)` → `logger.info($MSG)` の一致と置換テキストが取れる | M2 まで保留。自前の Tree-sitter クエリで検索のみ |
| S-4 | `basedpyright` を pip で入れ、`basedpyright-langserver --stdio` 相当で起動し、`initialize` → `didOpen` → `publishDiagnostics` → `definition` が通るか。`positionEncodings` に `utf-8` を返すか | Windows / macOS / Linux で通る | `pyright` + Node を利用者の環境に求める |
| S-5 | `vtsls` または `typescript-language-server` の探索・起動・`definition` / `references` / `rename` | 1 つ以上で通る | TS は best effort に格下げ（OD-7 の改版） |
| S-6 | `watchfiles` の Windows での検知遅延・デバウンス・ネットワークドライブの挙動 | 単一ファイル変更をイベントまで 100 ms 以内で受ける | ポーリング（仕様書 §12.4 の 6） |
| S-7 | `sqlite3` の `set_authorizer` / `set_progress_handler` / `mode=ro` URI が WAL の DB で動くか（`-shm` の扱い含む）、`query_only` が書き込みを拒否するか | 書き込み文が 3 重のどの層でも拒否される | `mode=ro` を外して `query_only` + authorizer の 2 重にする（その旨を仕様書に反映） |
| S-8 | `dulwich` で参照解決・blob 取得・ツリー差分・改名追跡が F-PY 規模で何 ms か | `HEAD~3` の blob 取得が 50 ms 以内、`log --follow` 相当が 500 ms 以内 | `pygit2`（L-3 の手続き）または `git` 子プロセス |
| S-9 | Python + SDK の往復オーバーヘッド: 何もしないツールと `get_file_skeleton`（キャッシュ済み）の p95 | 何もしないツールで p95 < 10 ms、骨格で p95 < 50 ms | JSON 直列化・pydantic 検証の最適化、ダメなら OD-1 (c) |
| S-10 | Windows で `os.replace` の対象をエディタが開いているときの挙動と再試行の効果 | 5 回以内の再試行で成功するか、失敗を検知してロールバックできる | 一時ファイルに書いて内容をコピーする方式（原子性は劣る。仕様書に注記） |
| S-11 | basedpyright / vtsls / rust-analyzer / gopls / clangd の `positionEncodings` 交渉結果 | 各サーバーの単位が分かり、`LineIndex` の変換で非 ASCII 行の位置が一致する | UTF-16 固定 |
| S-12 | トークン推定の係数を `o200k_base` と比べて較正 | 仕様書 §14 のコーパスで誤差 ±10% | 係数表を文字種別にさらに分ける |

## 12.2 M1 の着手順（最初の 12 タスク）

依存の無いものは並列に進める。各タスクは「成果物」と「確かめ方」を持つ。

| # | タスク | 成果物 | 確かめ方 | 依存 |
|---|---|---|---|---|
| 1 | S-1、S-2、S-7、S-9 のスパイク | `docs/spikes/` | 各合格条件 | なし（並列） |
| 2 | リポジトリ骨格: `pyproject.toml`、`src/lucianke` の空パッケージ、`core/`、CI（lint / test / license） | ビルドとテストが空で通る | CI 緑、`pip-licenses` 緑 | なし |
| 3 | `core`（model / urn / errors / records / textpos / limits） | 単体テスト | `urn` の往復、`LineIndex` の非 ASCII | 2 |
| 4 | `mcp`（server / registry / pipeline / envelope / budget / render.compact / render.json） | 何もしないツール `ping`（開発時だけ登録し `tools/list` に載せない）が SDK クライアントから往復 | S-9 の計測、compact 文法パーサ | 1, 3 |
| 5 | `workspace`（context / paths / languages / settings_file） | T-00 `configure_workspace`（`status` / `set_roots` / `set_excludes`） | T-00 の事後条件 | 3 |
| 6 | `index`（schema / connection / writer / ingest / queries） | 起動時の差分索引が F-POLY で通る | `data_version` の増加、行数 | 3, 5 |
| 7 | `syntax`（parsers / symbols / skeleton / boundaries / support: Python, TS/JS, JSON, YAML, TOML, XML, Markdown） | T-01 / T-02 / T-03 / T-05 | 各事後条件、`sha=` | 6 |
| 8 | `lsp`（servers / client / pool / documents / diagnostics / features: definition, references, hover） | T-04 / T-06 / T-08 | S-4 の環境で事後条件。`FakeLsp` の録画 | 6, 7 |
| 9 | `edit`（transaction / plan / normalize / syntax_check / writer / verify / lock） | T-10 | EP-1〜EP-9 | 7, 8 |
| 10 | `lsp.features` の rename / codeAction と T-11 / T-13 | T-11 / T-13 | 事後条件 | 9 |
| 11 | `index.public` と T-19 / T-20、`engine/selector` の全ツール適用、T-07（callHierarchy） | T-07 / T-19 / T-20 | 読み取り専用の不変条件、降格の警告 | 8 |
| 12 | `watcher`、`lifecycle`（終了手順）、`doctor`、e2e（Claude Code の `.mcp.json`）、bench S1 / S2 / S4 | M1 出口条件 M1-1〜M1-8 | `bench/exit_criteria.toml` | 全部 |

- タスク 4 の時点で「Claude Code から ping が返る」状態にし、以後は常に接続できる状態を保つ（各タスクの終わりに e2e の基本シナリオを回す）。
- T-12（M2）・T-09・T-14〜T-18（M2）・T-15 / T-16（M3）は `tools/list` に載せ、`E_UNSUPPORTED` を返す実装だけを M1 で入れる。

---

# 13. リスクと対策

| # | リスク | 兆候 | 対策 |
|---|---|---|---|
| R-1 | Python の往復オーバーヘッドで NFR-PERF-1 を満たせない | S-9 で p95 > 50 ms | 直列化と検証の削減、レコードの遅延生成、骨格の事前計算。最後に OD-1 (c) |
| R-2 | `mcp` SDK 2.x の API が想定と違う | S-1 不合格 | ADR-2 の代替。SDK の版を固定し、境界を `mcp/server.py` に閉じ込めてあるので影響は 1 ファイル |
| R-3 | 更新が止まった Tree-sitter 文法が 0.26 と合わない | S-2 不合格 | 版の固定、個別再ビルド、または当該言語を `structural` 不可の Tier 4 扱いに一時格下げ |
| R-4 | LSP サーバーごとの差（位置単位、`publishDiagnostics` の `version` 無し、`callHierarchy` 未対応） | e2e で位置ずれ・診断待ちのタイムアウト | `servers.py` にサーバー別の癖を集約。`FakeLsp` の録画で回帰を固定 |
| R-5 | Windows での共有違反・改行・大文字小文字 | EP-6 / EP-9 が Windows でだけ落ちる | CI を 3 OS で回す。`writer.py` の再試行とロールバック |
| R-6 | GIL により初回索引が遅い | 1 万ファイルの索引に分単位 | 許容する方針（師匠 2026-10-29）。索引中も降格応答で答え続けることと、`configure_workspace status` の進捗表示を優先する。プロセスプールや `lucianke index` の事前実行は、利用者が困ったときの手段として M2 以降 |
| R-7 | `dulwich` の性能 | S-8 不合格 | OD-20 の (b) または (c) |
| R-8 | ライセンスの許可リスト外が依存木に紛れ込む | CI の license ジョブが赤 | L-8 の検査を必須ジョブに。extras ごとに検査 |
| R-9 | 仕様書と設計の乖離 | 契約テストが仕様書の番号と合わない | 事後条件 → テスト名の対応を機械的に検査（§10.2） |
| R-10 | キャッシュディレクトリの肥大 | ディスク警告 | ジャーナル 50 件、ログ 7 日、構文木キャッシュの上限、`lucianke doctor` に使用量表示 |
| R-11 | `exact` の応答に LSP 往復が含まれ、NFR-PERF-1 が LSP の速度に左右される（§8.3 の 2 回目の呼び出し） | `definition` / `references` の p95 が LSP 待ちで 50 ms を超える | 結果を `relations` / `_hover_cache` に書いて 2 回目以降は PaaD から答える。S-9 で「LSP 待ちを含む p95」と「含まない p95」を分けて測り、仕様書 §14.4 の母集団定義（`exact` で応答した呼び出し）と照らす。超えるなら `WAIT_SHORT` の上限を下げて `structural` を先に返す |

---

# 付録 A. 候補パッケージの実態（一次情報・2026-10-29）

PyPI JSON API（`https://pypi.org/pypi/<name>/json`）から取得した。ライセンスは仕様書付録 D を正とし、ここでは **版・対応 Python・wheel の有無・依存** を記録する。版は M1 着手時に固定し直す（§11.1）。

## A.1 表

| 名前 | 最新版 | 公開日 | requires_python | wheel | 種別 | 主要依存 / 備考 |
|---|---|---|---|---|---|---|
| mcp | 2.3.0 | 2026-10-02 | >=3.10 | any | 純 Python | anyio, httpx2, jsonschema, mcp-types, pydantic>=2.12, starlette, uvicorn, pyjwt[crypto], opentelemetry-api, pywin32（Windows）。**依存が重い**（§A.3） |
| tree-sitter | 0.26.0 | 2026-06-30 | >=3.10 | win / mac / linux（cp310〜） | ネイティブ | `Language(mod.language())` 形式 |
| tree-sitter-python | 0.25.0 | 2025-09-11 | >=3.10 | abi3（win_amd64 / win_arm64 含む） | ネイティブ | |
| tree-sitter-typescript | 0.23.2 | **2024-11-11** | >=3.9 | abi3 | ネイティブ | 更新停止気味。S-2 で ABI 互換を確認 |
| tree-sitter-javascript | 0.25.0 | 2025-09-01 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-rust | 0.24.2 | 2026-03-27 | >=3.9 | abi3 | ネイティブ | |
| tree-sitter-go | 0.25.0 | 2025-08-29 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-c | 0.24.2 | 2026-04-22 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-cpp | 0.23.4 | **2024-11-11** | >=3.9 | abi3 | ネイティブ | 更新停止気味。S-2 |
| tree-sitter-c-sharp | 0.23.5 | 2026-04-14 | >=3.9 | abi3 | ネイティブ | |
| tree-sitter-json | 0.24.8 | **2024-11-11** | >=3.9 | abi3 | ネイティブ | 更新停止気味。S-2 |
| tree-sitter-yaml | 0.7.2 | 2025-10-07 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-toml | 0.7.0 | **2024-12-03** | >=3.9 | abi3 | ネイティブ | 更新停止気味。S-2 |
| tree-sitter-xml | 0.7.0 | **2024-11-13** | >=3.9 | abi3 | ネイティブ | 更新停止気味。S-2 |
| tree-sitter-markdown | 0.5.4 | 2026-10-24 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-rst | 0.2.0 | 2025-02-24 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-sql | 0.3.11 | 2025-10-01 | >=3.10 | abi3 | ネイティブ | M2 |
| ast-grep-py | 0.51.0 | 2026-10-15 | >=3.8 | win / mac / linux | ネイティブ | M2。rewrite API の有無は description に記述なし（S-3） |
| z3-solver | 5.3.2.0 | 2026-10-25 | なし | py3-none-win / mac / linux | ネイティブ | M3 |
| dulwich | 1.2.20 | 2026-10-26 | >=3.10 | any + win / mac / linux | 純 Python + 任意の C 拡張 | urllib3, typing_extensions。S-8 |
| watchfiles | 1.3.0 | 2026-09-21 | >=3.10 | abi3（win32 / win_amd64 / win_arm64 含む） | ネイティブ | anyio。Windows 対応の記述あり。S-6 |
| sqlglot | 30.25.0 | 2026-10-26 | >=3.9 | any | 純 Python | M2 |
| nbformat | 5.11.1 | 2026-08-17 | >=3.10 | any | 純 Python | fastjsonschema, jsonschema, jupyter-core, traitlets。`.ipynb` は標準の `json` で読めるため **採用しない**（依存を増やさない。仕様書 B2 は JSON 構造の直接操作で足りる） |
| PyYAML | 6.0.3 | 2025-09-29 | >=3.8 | win / mac / linux | ネイティブ（純 Python 動作も可） | |
| jsonpath-ng | 1.14.0 | 2026-10-26 | >=3.11 | any | 純 Python | |
| lxml | 6.1.3 | 2026-09-03 | >=3.8 | win / mac / linux | ネイティブ | 任意（extras `xml`） |
| pydantic | 2.14.1 | 2026-10-11 | >=3.10 | any | 純 Python（pydantic-core はネイティブ） | mcp が要求するため必ず入る |
| basedpyright | 1.40.2 | 2026-10-04 | >=3.10 | any | 純 Python | `nodejs-wheel-binaries>=20.13.1` に依存し **Node.js を pip で同梱**。Python 系 LSP の既定候補（S-4） |
| pyright | 1.1.414 | 2026-09-10 | >=3.7 | any | 純 Python | `nodeenv` 依存（実行時に Node を取得。L-6 に抵触しうるため既定にしない） |
| ruff | 0.17.3 | 2026-10-27 | >=3.7 | py3-none-win / mac / linux | ネイティブ | `ruff server` |
| pip-licenses | 5.5.5 | 2026-03-28 | >=3.9 | any | 純 Python | CI の license ジョブ |
| hatchling | 1.33.0 | 2026-10-28 | >=3.10 | any | 純 Python | ビルドバックエンド |
| uv | 0.13.6 | 2026-10-27 | >=3.8 | py3-none-win / mac / linux | ネイティブ | 導入経路 |

## A.2 追加確認の結果

1. `tree-sitter` 0.26.0 の言語読み込みは `Language(tree_sitter_python.language())` 形式（PyPI の description のサンプル）。
2. `basedpyright` は `requires_dist` に `nodejs-wheel-binaries>=20.13.1` を持つ。Node.js は pip の依存として入る。
3. `mcp` 2.3.0 の description に `structuredContent` と `roots` の記述は無い（クライアント側の `result.structured_content` の例のみ）。サーバー側の対応は S-1 で確かめる。
4. `watchfiles` の description に "Binaries are available for most architectures on Linux, MacOS and Windows" の記述あり。
5. `ast-grep-py` の description に rewrite / replace の記述は無い。S-3 で確かめる。

## A.3 設計への含意

- **Windows wheel が無いパッケージは無い。** NFR-PORT-2 の障害にならない。
- **`requires_python` の下限は 3.12 で全部満たす**（最も厳しいのは jsonpath-ng の >=3.11）。
- **`mcp` 2.x は依存が重い**（HTTP サーバー系・OpenTelemetry・JWT・Windows では pywin32）。起動時間と import 時間に効く。S-9 でコールドスタートも測り、必要なら stdio だけに必要な部分を遅延 import する。依存木のライセンス検査（L-8）はこの重さも対象にする。
- **Tree-sitter の 5 文法（typescript / cpp / json / xml / toml）は 2024 年で更新が止まっている。** 本体 0.26 との ABI 互換を S-2 で確かめ、合わなければ版を固定するか個別に対処する（R-3）。
- **basedpyright を Python 系 LSP の既定にする理由**: pip だけで Node ごと入り、Windows の利用者に Node の別途導入を求めない。`pyright`（PyPI）は実行時に Node を取得する方式で、仕様書 L-6 の趣旨に反する。
- **nbformat は採らない。** `.ipynb` は JSON であり、標準の `json` で読み書きできる。依存を 4 つ増やす価値が無い。
- **`z3-solver` は requires_python が無指定。** M3 で採用時に動作確認する。


---

# 付録 B. 仕様書の要求 → 実現モジュール

| 要求 | 実現する主なモジュール | 受入の置き場所 |
|---|---|---|
| FR-WS-1 | `workspace/context.py`, `workspace/languages.py`, `mcp/server.py`（roots） | contract T-00 |
| FR-WS-2 | `tools/t00_configure_workspace.py`, `workspace/context.py`, `index/writer.py` | contract T-00 |
| FR-WS-3 | `git/repo.py`, `git/vtree.py`, `mcp/pipeline.py`（revision 解決） | contract T-02 / T-05 |
| FR-EXP-1 | `tools/t01_get_repo_map.py`, `index/queries.py`（pagerank） | contract T-01 |
| FR-EXP-2 | `tools/t02_get_file_skeleton.py`, `syntax/skeleton.py`, `syntax/adapters/*` | contract T-02 |
| FR-EXP-3 | `tools/t03_search_workspace_symbols.py`, `index/queries.py`（FTS5） | contract T-03 |
| FR-NAV-1 | `tools/t04_resolve_symbols.py`, `lsp/features.py`（definition, hover） | contract T-04 |
| FR-NAV-2 | `tools/t05_get_symbol_bodies.py`, `syntax/boundaries.py`, `syntax/adapters/*` | contract T-05 |
| FR-NAV-3 | `tools/t06_find_references_and_implementations.py`, `lsp/features.py`（references） | contract T-06 |
| FR-NAV-4 | `tools/t07_query_call_hierarchy.py`, `lsp/features.py`（callHierarchy）, `syntax/guards.py` | contract T-07 |
| FR-DIAG-1 | `tools/t08_get_diagnostics.py`, `lsp/diagnostics.py`, `index/ingest.py`（構文エラー） | contract T-08 |
| FR-DIAG-2 | `tools/t09_analyze_change_impact.py`, `git/cochange.py`（M2） | contract T-09 |
| FR-EDIT-1 | `tools/t10_edit_symbol_body.py`, `edit/*` | contract T-10, unit edit |
| FR-EDIT-2 | `tools/t11_rename_symbol.py`, `lsp/features.py`（rename）, `edit/plan.py` | contract T-11 |
| FR-EDIT-3 | `tools/t12_structural_search_replace.py`, `syntax/structural.py`（M2） | contract T-12 |
| FR-EDIT-4 | `tools/t13_apply_code_action.py`, `lsp/features.py`（codeAction） | contract T-13 |
| FR-EDIT-5 | `edit/verify.py`, `lsp/diagnostics.py` | unit edit（EP-7） |
| FR-EDIT-6 | `edit/transaction.py`（段 3 のインターロック） | unit edit（EP-8） |
| FR-EDIT-7 | `edit/transaction.py`, `edit/writer.py` | unit edit（EP-3, EP-4, EP-9） |
| FR-VER-1 | `verify/paths.py`（M2） | contract T-14 |
| FR-VER-2 | `verify/contracts/*`（M3） | contract T-15 |
| FR-VER-3 | `verify/ssa.py`, `verify/solver.py`（M3） | contract T-16 |
| FR-GIT-1 | `git/history.py`, `git/cochange.py`（M2） | contract T-17 |
| FR-GIT-2 | `git/history.py`, `tools/t18_analyze_git_diff_symbols.py`（M2） | contract T-18 |
| FR-DB-1 | `tools/t19_get_project_db_schema.py`, `index/schema.sql` | contract T-19 |
| FR-DB-2 | `tools/t20_query_project_db.py`, `index/public.py` | contract T-20, invariants |
| FR-FMT-1 | `mcp/render/compact.py` | contract（文法パーサ） |
| FR-FMT-2 | `mcp/budget.py` | unit budget |
| FR-FMT-3 | `mcp/envelope.py`, `mcp/budget.py`（集計） | contract 全ツール |
| FR-FMT-4 | `mcp/render/graph.py` | contract T-06 / T-07 / T-09 / T-14 / T-18 / T-20 |
| FR-LANG-1 | `workspace/languages.py`, `syntax/support.py` | unit languages |
| FR-LANG-2 | `engine/selector.py` | unit selector |
| FR-LANG-3 | `syntax/adapters/*`, `syntax/support.py`（B1a） | contract T-02 / T-05 |
| NFR-PERF-1 | ADR-6、§6.1、`mcp/pipeline.py` の `timing_ms` | bench（仕様書 §14.4）|
| NFR-PERF-2 | `verify/*`, `git/*`（M2 / M3） | bench（仕様書 §14.4）|
| NFR-PERF-3 | `workspace/watcher.py`, `index/ingest.py`, `lsp/documents.py` | bench（仕様書 §14.6）|
| NFR-TOK-1 | `mcp/budget.py`, `syntax/skeleton.py` | bench（仕様書 §14.3）|
| NFR-SAFE-1 | `edit/syntax_check.py` | bench（仕様書 §14.5）|
| NFR-SAFE-2 | `index/public.py` | invariants |
| NFR-SAFE-3 | `workspace/paths.py` | unit paths, invariants |
| NFR-SAFE-4 | `edit/writer.py` | unit edit（EP-9） |
| NFR-OPS-1 | `bootstrap_io.py` | invariants（stdout 純度） |
| NFR-OPS-2 | `lifecycle.py`（ProcessRegistry） | invariants（子プロセス 0） |
| NFR-OPS-3 | `engine/selector.py`, `lsp/pool.py` | contract T-04 / T-08（indexing 中） |
| NFR-OPS-4 | `core/textpos.py`, `lsp/client.py` | unit textpos |
| NFR-PORT-1 | `pyproject.toml`（§11.1） | CI license / build |
| NFR-PORT-2 | §9.5 | CI 3 OS |
