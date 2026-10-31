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
| NFR-PERF-1 | Fast Path p95 < 50 ms（ウォームアップ後、サーバー内時間） | 要求経路で LSP 往復を待たない設計（キャッシュと PaaD から答え、LSP は背景で更新）。Python のオーバーヘッドは S-9 で測り、往復は何もしないツールで p95 1.08 ms と十分小さい（§12.1。T-02 の p95 は実装後に測る） |
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
│ コーディングエージェントのホスト │ ◄──────────────────────► │ lucianke（1 プロセス）         │
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
lucianke（ホストが起動する 1 プロセス。寿命はホストのセッションと同じ。ADR-16）
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
- 決定: SDK の低水準サーバー（v2 は `Server(name, on_list_tools=…, on_call_tool=…)` のコンストラクタ引数方式で、ハンドラは `(ctx, params)`。v1 のデコレータではない。S-1 で確認）を使い、入力スキーマは pydantic モデル（`extra="forbid"`）から生成して Lucianke 側のレジストリが保持する。結果の `content` と `structuredContent` と `isError` はパイプラインが組み立てる。
- 捨てた案: 高水準の関数デコレータ方式（速いが、エンベロープを全ツールに強制しにくい）。
- 帰結: スパイク S-1 で確認した（合格。[`docs/spikes/S-01.md`](spikes/S-01.md)）。決定は変えず、次の 4 点を前提に加える。
  1. 自前の JSON Schema（`additionalProperties: false` 込み）は `tools/list` にそのまま通り、`tools/call` は `CallToolResult(content, structured_content, is_error=True)` を例外なしで返せる。未知のツール名だけが `raise MCPError(INVALID_PARAMS)`（`-32602`）になる（S-1 で確認）。
  2. **SDK は引数をスキーマで検証せず、ハンドラから漏れた例外も捕捉しない**（漏れると `error.code=0` で例外文面がそのままクライアントへ出る）。検証と例外捕捉は §7.1.2 のパイプラインが全責任を持ち、最外周で必ず捕捉する（S-1 で確認）。
  3. `Server.middleware` は SDK が暫定 API と明記しているので製品コードでは使わない。`timing_ms` は §7.1.2 の自前計測とする（S-1・S-9 で確認）。
  4. SDK の版は固定する: `mcp>=2.3,<2.4`（§11.1。S-1 は 2.3.0 で実施）。HTTP 系の依存（starlette・uvicorn・httpx2 等）は `mcp` の無条件依存で extras では外せず、低水準 `Server` が import するので stdio でも読み込まれる。遅延 import は SDK 内部の import 構造への依存が強く効果も約 −190 ms にとどまるので採用しない（付録 A.3。S-1 で確認）。

### ADR-3 `stdout` の退避と差し替えをプロセス起動の最初に行う

- 文脈: NFR-OPS-1。依存ライブラリや C 拡張が fd 1 に書く可能性を排除できない。
- 決定: 起動直後に `real_stdout = os.dup(1)` で退避し、`os.dup2(2, 1)` で fd 1 を stderr に重ねる。fd 0 も NUL に差し替える（子プロセスに入力を横取りさせない）。MCP のトランスポートには、退避した fd を `io.TextIOWrapper(os.fdopen(fd, "wb", closefd=False), encoding="utf-8", newline="\n")` で包み、`anyio.wrap_file` して `stdio_server(stdin=…, stdout=…)` に **明示的に** 渡す（SDK が受け取るのはバイナリではなくテキストストリーム `anyio.AsyncFile[str]`。`newline="\n"` を忘れると Windows で行末が CRLF になる）。`sys.stdout` も stderr 向けのラッパに差し替える。**手動で退避したあとに、引数なしの `stdio_server()` を呼んではならない**（fd 0 が NUL なので SDK が入力 EOF と見て即終了し、応答が 0 行になる）。
- 捨てた案: `sys.stdout` の差し替えだけ（C レベルの書き込みを防げない）。
- 帰結: スパイク S-1 で確認した（合格）。トランスポートに退避 fd のストリームを渡せる。ツール内の `print`・`os.write(1)`・`sys.__stdout__`・UCRT `puts`・`WriteFile(GetStdHandle)`・stdout 継承の子プロセスの 6 経路はすべて stderr へ出て、`stdout` は純粋（全行 JSON-RPC、LF のみ）だった。代替案（`sys.stdout` を退避 fd のラッパに戻す）は不要。`SetStdHandle` は CPython 3.12 + UCRT の環境では不要と実測した（`dup2` が Win32 の標準ハンドルも付け替える。入れても害は無く数行）。SDK 2.3 の引数なし `stdio_server()` も同様の退避を自前で行うが、行末が CRLF になり、退避が import 後になるため使わない。stdin を閉じると 117〜154 ms で終了コード 0（ADR-16）。

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
- 決定: カーソルの中身は `{tool, args_hash, offset, data_version, workspace_id}`。`data_version` は索引の書き込みトランザクションごとに増える（診断の更新では増やさない）。読み取り時に一致しなければ失効。中身はサーバーのセッション限定の表（ADR-16 の軽い対応表）に持ち、応答に出すのは短い鍵（`c` + 10 進 9 桁）だけ（BUG-0115、2026-10-31。理由: 中身を base64url の JSON にして渡していた間は、カーソル 1 つが約 70 トークンで、毎ページの `# more:` 行が参照 3〜4 行ぶんの呼ぶ側の文脈を使い、件数の少ない答えは compact で分けて読めなかった）。
- 捨てた案: 影響ファイルの集合を持つ（正確だが複雑。M1 では保守的な全体版で足りる）。
- 帰結: 無関係なファイルの変更でもカーソルが失効しうる。仕様の範囲内（失効は常に安全側）。LSP 由来の `relations` 書き込み（§7.5.6 の `exact` への書き戻し）は `data_version` を**進めない**（`bumps_version=False`。進めると、そのツール自身が直前に返した `next_cursor` が次のページで `E_CURSOR_EXPIRED` になる。パイプラインは `data_version` をハンドラの前に引くため。書き戻しは同じ問い合わせの答えを `structural` から `exact` に置き換えるだけで、結果集合は変わらない。1d8700d。実装で確定 2026-10-29）。ファイル変更時はそのファイルを `source_file_id` とする `relations` 行を `exact` も含めて全削除し `structural` を入れ直すので、編集直後は一時的に `structural` に下がり、LSP の再取得で `exact` に戻る。

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
- 決定: すべての子プロセスは `ProcessRegistry` 経由で起動し（argv 配列、最小の環境変数、作業ディレクトリ = ルート）、終了手順はこの台帳を舐める。Windows では新しいプロセスグループで起動したうえで **Job Object**（`KILL_ON_JOB_CLOSE`）に入れ、子孫（`npx` や `.cmd` の中継、venv のランチャーが挟む孫）までジョブごと落とす。Lucianke が異常終了してジョブのハンドルが閉じたときも OS が全部落とす。穏やかな終了の合図として `CTRL_BREAK_EVENT` を送ってもよいが、**GUI ホスト配下（コンソールを持たない親）では届かない**ので当てにしない。LSP には `terminate` の前に LSP の `shutdown` → `exit` を送る（§6.5、§7.5.2）。順序は（LSP なら `shutdown` → `exit`）→ `terminate` → `kill`（実装で確定 2026-10-29）。ジョブに入れられなかった場合（権限など）はリーダーだけを `kill` する縮退になる。POSIX は `start_new_session=True` で起動し、`killpg` で落とす。台帳は `core/procs.py` に置く（core 構築で実装）。
- 帰結: NFR-OPS-2 の検査は台帳が空であることの確認になる。台帳（`ProcessRegistry`）はスレッド安全ではなく、イベントループのスレッドからだけ使う。

### ADR-13 Tree-sitter の解析は期限つき `read_callback`、構文木はバイト列と組で LRU キャッシュ

- 文脈: Parser はスレッド安全でない。`parse(bytes)` は解析の全体にわたって GIL を握るため、スレッドプールで走らせても並列化せず（4 スレッドで直列比 0.80〜0.98 倍）、イベントループも止まる（2,000 行で約 8 ms、5 MB で約 750 ms）。`progress_callback` は Windows でアクセス違反（0xC0000005）でプロセスが落ちる。構文木はソースの約 20〜26 倍のメモリを食う（python 20.3 倍、json 25.3 倍、markdown 25.9 倍）。`Node.text` は Tree がソースを保持するので取れるが、`line_index` のためにソースのバイト列は別に持つ（以上 S-2 で確認）。
- 決定: スレッドごと・言語ごとに Parser を 1 個持つ（共有しても壊れなかったのは GIL による直列化のおかげにすぎず、当てにしない。`Language` / `Query` / `Tree` / `Node` はスレッド間で共有してよく、`QueryCursor` は呼び出しごとに作る）。解析は常に `parse_with_deadline`（`read_callback` で 16 KiB 刻み。期限後に `b""` を返して打ち切り、`timed_out` なら木は捨てる）で行い、プールのスレッドで走らせる。キャッシュのキーは `(workspace_id, path, sha256)`、値は `(source_bytes, tree, line_index)`。上限はファイル数と、**推定木メモリ（ソース長 × 25）** の合計の両方（仕様書 §12.8）。
- 捨てた案: `parse(bytes)` をスレッドプールで呼ぶだけ（上記の理由で並列化もループの保護もしない）。`progress_callback` による打ち切り（落ちる）。例外を投げて打ち切る方式（`SystemError` になることがあり不安定）。
- 帰結: 同じ版のファイルは 1 回しか解析しない。`read_callback` 版は速度が `bytes` 版と同等（pdb.py 1,987 行で中央値 8.5 ms 対 8.2〜8.9 ms）で、他スレッド（ループ）の停止が最長約 20 ms に縮む。ただし並列にはならない。並列度が要るなら別プロセス（4 本で約 4 倍）であり、これが R-6 の「プロセスプールは M2 以降」の根拠になる。編集後は新しい `sha256` で再解析（増分解析は MAY。全解析が約 8 ms なので M1 は不要）。（S-2 で確認）

### ADR-14 位置の単位変換は LSP クライアントの境界でだけ行う

- 文脈: 仕様書 §5.10、§12.2。
- 決定: Lucianke 内部の位置は「0 始まりの行、0 始まりのコードポイント列」を使い、外部出力の直前で 1 始まりに、LSP との送受信で UTF-16 または UTF-8 単位に変換する。変換には行ごとのテキストを使い、`LineIndex` がバイト・コードポイント・UTF-16 の相互変換表を持つ。
- 帰結: 変換の誤りは `LineIndex` の単体テスト（非 ASCII・サロゲートペア・結合文字）で捕まえる。Tree-sitter の列は **行頭からのバイト数** なので、コードポイント列への変換は `syntax` の出口（`Range` を作る 1 か所）に置く。`LineIndex` は `\n` だけで行を切る（Tree-sitter と一致。CRLF の `\r` は行末に残り、単独の `\r` は改行にならない）。LSP との境界でだけ、単独 `\r` と BOM（UTF-8 BOM 付きは 1 行目の列が 3 ずれる）の扱いを合わせる。ast-grep の位置は 0 始まりのコードポイントで内部表現と同じなので、変換は要らない（S-2・S-3 で確認）。

### ADR-15 M1 で LSP を同期で使うのは 5 要求だけ

- 文脈: 範囲を絞らないと LSP 連携が肥大する。
- 決定: M1 の LSP 要求は `initialize`、`textDocument/definition`、`textDocument/references`、`callHierarchy/*`、`textDocument/rename`（`prepareRename` 含む）、`textDocument/codeAction`（`resolve` 含む）、`textDocument/hover`（型展開用）、および通知（`didOpen` / `didChange` / `didClose` / `didChangeWatchedFiles`、受信 `publishDiagnostics` / `$/progress`）。`workspace/symbol` と `typeHierarchy` は M2。
- 帰結: `search_workspace_symbols` は PaaD（FTS5）で答え、LSP を使わない。

### ADR-16 プロセスの寿命はセッション。永続状態はディスクに、プロセス内は復元できるキャッシュだけ

- 文脈: 師匠 2026-10-29「serve としてサーバプロセスにする必要もないのでは？ ツールとしては状態を持たないですよね？」。MCP の stdio は、ホストがサーバーを 1 回起動してセッションの間つなぎ続ける形であり、呼び出しごとにコマンドを起動する形は無い。一方で、ツールの意味はステートレスであるべきで、Lucianke を「状態を抱えるサーバー」にしてはならない。
- 決定:
  1. Lucianke はホストが起動する **子プロセス** で、寿命はホストのセッションと同じ（`stdin` の EOF で終わる）。常駐デーモン・複数ホストで共有するサービスにしない。入口は `lucianke`（引数なし）。`serve` という名は使わない。
  2. **ツールの意味は各呼び出しで閉じる**: 結果は作業ツリーと PaaD の内容から決まり、前の呼び出しに依存しない。
  3. **永続する状態は PaaD（SQLite）とジャーナルだけ** で、どちらもディスク上にある。プロセスが死んでも次の起動で使える。PaaD はキャッシュであり、消えても再索引で作り直せる。
  4. **プロセス内の状態は性能のためのキャッシュ** （温まった LSP 子プロセス、構文木キャッシュ、hover キャッシュ）と、**セッション限定の軽い対応表** （ページネーションの `cursor`、`code_action_id`、`edit_seq` の採番位置）だけ。失っても `E_CURSOR_EXPIRED` / `E_TARGET_NOT_FOUND` で再取得でき、ファイルや DB は壊れない。
- 捨てた案: 呼び出しごとのコマンド起動（MCP に無い。LSP を毎回起こすと秒単位になり、NFR-PERF-1 を満たせない）。常駐デーモン（導入・終了・多重起動・権限の問題を増やす）。
- 帰結: プロセスが生きている唯一の実利は「LSP と構文木を温めておくこと」であり、それは NFR-PERF-1 のための手段にすぎない。受入に「再起動耐性」（§10.1）を加える。LSP の温め直しは初回の重さとして現れるが、それは許容する（仕様書 §14.4）。

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
│  ├─ __main__.py            # `lucianke` CLI。引数なしで stdio の MCP 入口（ホストが起動）。補助サブコマンド index | doctor | bench
│  ├─ app.py                 # 合成ルート: 設定読込 → サービス生成 → 起動・終了
│  ├─ bootstrap_io.py        # ADR-3: fd 1 の退避と差し替え（最初に import される）
│  ├─ config.py              # 設定モデル（環境変数 / .lucianke.toml / 既定）と cache_root
│  ├─ logging_setup.py       # stderr + JSON Lines ファイル、ローテーション
│  ├─ lifecycle.py           # 起動順、終了手順、シグナル（子プロセス台帳 ProcessRegistry は core/procs.py）
│  │
│  ├─ core/                  # 依存なしの中核型。どこからでも import 可、core は他を import しない
│  │  ├─ model.py            #   TargetRef, Range, Position, SymbolRecord, Kind, Confidence, Tier, Capability（Urn は urn.py）
│  │  ├─ urn.py              #   Urn と Selector（CodeSel / DataSel / CellSel / TagSel / LexSel）の型、URN の構文解析・整形・緩和（仕様書第 7 章）
│  │  ├─ errors.py           #   LuciankeError(code, message, details, candidates)
│  │  ├─ procs.py            #   ProcessRegistry（ADR-12）: 子プロセスの起動・台帳・終了、Windows の Job Object、minimal_env
│  │  ├─ records.py          #   Record, RecordGroup, Summary（ADR-4）
│  │  ├─ textpos.py          #   LineIndex（ADR-14）、改行・BOM・インデントの検出
│  │  ├─ hashing.py          #   sha256 / sha1 のヘルパ。workspace_hash は sha1 の先頭 16 桁
│  │  ├─ tokens.py           #   TokenEstimator（§7.2.1）。mcp/budget.py が再公開する（syntax が mcp を import できないため core に置く）
│  │  ├─ compact_lines.py    #   Compact DSL の行種ごとの組み立て関数（§7.2.4）。同じ理由で core に置く
│  │  └─ limits.py            #   時間・件数の上限定数（§6.4）。仕様書 §12 の値をここにだけ置く
│  │
│  ├─ mcp/                   # MCP との境界
│  │  ├─ server.py           #   SDK の低水準サーバーを組み立て、tools/list・tools/call・roots を接続
│  │  ├─ registry.py         #   ToolSpec・CommonArgs・ToolContext の再公開と tools/list の組み立て。定義は tools/_base.py（tools は mcp を import しないため）
│  │  ├─ pipeline.py         #   検証 → 文脈解決 → ハンドラ → パッカー → レンダラ → エンベロープ
│  │  ├─ envelope.py         #   ResponseEnvelope / ResolutionMeta の組み立て（json・compact のヘッダ）
│  │  ├─ budget.py           #   Packer、CursorCodec（TokenEstimator は core/tokens.py の再公開）
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
│  │  ├─ documents.py        #   didOpen/didChange/didClose、版番号、LRU 200。DocumentRouter（言語の全サーバーへ sync・診断の待ちを合わせる。§7.5.4）
│  │  ├─ diagnostics.py      #   DiagnosticsHub: publishDiagnostics の受信・版待ち（wait_for。編集層の DiagnosticsWaiter の土台）。Index への書き込みは呼び手（on_diagnostics）
│  │  └─ features.py         #   definition / references / call_hierarchy / rename / code_actions / hover の呼び出しと PaaD への書き込み
│  │
│  ├─ index/                 # PaaD
│  │  ├─ schema.sql          #   仕様書 §9.2 の DDL（正本はそのコピー。差分はテストで検出）
│  │  ├─ connection.py       #   接続の開き方（WAL、pragmas、ro URI）、ReaderPool。WORKSPACE_HASH_LEN と workspace_hash は core/hashing.py の再公開
│  │  ├─ writer.py           #   IndexWriter スレッド、ジョブキュー、data_version
│  │  ├─ ingest.py           #   ファイル単位の取り込み（files/symbols/relations/diagnostics の差分更新）。解析結果は FileFacts で受け取り、呼び手のトランザクション内で実行（index は syntax/lsp を import しない）
│  │  ├─ queries.py          #   ツールが使うパラメータ化クエリ（名前つき）
│  │  ├─ rank.py             #   参照グラフ上の PageRank・fan_in・ファイルのスコア（_symbol_rank、files.pagerank_score。T-01・T-03 の並びの元）
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
│  │  ├─ selector.py         #   EngineSelector（ADR-10）、警告文の雛形
│  │  ├─ indexer.py          #   Indexer: 走査・解析（スレッド）・バッチ化・背景ジョブ・_settle_relations。DB への書き込みは index/ingest.py へ渡す（§7.6.2。実装で確定 2026-10-29）
│  │  ├─ analysis_pool.py    #   走査の解析の子プロセス（spawn。解析の要るファイルが多い走査だけ。§6.1。BUG-0127）
│  │  ├─ reresolve.py        #   後から作られた・消えたファイルを import している側の relations を解き直す（_import_probes。§7.6.2。BUG-0025）
│  │  └─ rank_scheduler.py   #   PageRank の再計算のデバウンス（最後の更新の約 1 秒後に 1 回。§7.6.2）
│  │
│  ├─ git/                   # M2。M1 ではリビジョン解決と blob 取得（T-02/T-05 の revision 対応）だけ
│  │  ├─ repo.py             #   dulwich ラッパ: 参照解決、blob、ツリー差分、log --follow 相当
│  │  ├─ vtree.py            #   revision 指定時の仮想ツリー
│  │  ├─ history.py          #   シンボル単位の変更検出（M2）。履歴の走査は読み取り専用の `git` 子プロセス、無ければ dulwich（OD-20）
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
│  │  ├─ _base.py            #   ToolSpec、CommonArgs、ToolContext（mcp/registry.py が再公開）、共通ヘルパ（TargetRef 解決の呼び出し、ファイル列挙）
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
- `tools` は `mcp` を import しない（ツールは MCP を知らない）。`mcp.render` も知らない。このため `ToolSpec`・`CommonArgs`・`ToolContext` は `tools/_base.py` に置き、`mcp/registry.py` が再公開する。同じ理由（`syntax` が `mcp` を import できない）で、Compact の行組み立て（`core/compact_lines.py`）とトークン推定（`core/tokens.py`）は `core` に置く（実装で確定 2026-10-29）。
- サービス同士の依存は次の向きに限る: `edit → syntax, index, lsp, workspace`、`lsp → index, workspace`、`syntax → workspace`、`engine → lsp, syntax, workspace`、`git → workspace`、`index → (なし。core のみ)`。逆向きが必要になったらイベント（コールバック登録）で解く。
- 依存方向の検査は `tests/unit/test_import_graph.py` で機械的に行う（import 文の静的走査）。

## 4.3 モジュール責務の要点

| モジュール | 担う | 担わない |
|---|---|---|
| `mcp/pipeline.py` | 全ツール共通の前後処理。`LuciankeError` の捕捉とエンベロープ化。`timing_ms` | ツール固有の判断 |
| `mcp/registry.py` | `ToolSpec` の表。`tools/list` の JSON Schema 生成（pydantic の `model_json_schema` に共通パラメータを合成） | 実行 |
| `mcp/budget.py` | 予算内詰め込み、要約、カーソル（トークン推定は `core/tokens.py` の再公開） | レコードの内容 |
| `mcp/render/compact.py` | `Record.lines` を §6.1 の文法で連結し、ヘッダ・警告・トレーラを付ける | 行の中身の生成（ツールと syntax が作る） |
| `workspace/paths.py` | 相対化・正規化・実体解決・ルート配下検査。失敗は `E_INVALID_ARGUMENT` | ファイルの読み書き |
| `syntax/symbols.py` | 構文木 → `SymbolRecord`。修飾名・種別・範囲・選択範囲・修飾子・doc 要約・`body_sha256` | DB 書き込み |
| `index/ingest.py` | `FileFacts` を受けて 1 ファイルの差分取り込みを（呼び手が開いた）トランザクションの中で。`ingest_file` / `remove_file` / `upsert_workspace` / `upsert_exact_relations` / `replace_lsp_diagnostics`。`data_version` は `IndexWriter` が進める | 構文解析、走査 |
| `engine/indexer.py` | ワークスペースの走査（`.gitignore` と `exclude_globs`）、1 ファイルの解析（`syntax`）→ `FileFacts`（走査では子プロセスにも並べる。`engine/analysis_pool.py`。§6.1）、バッチ化（`INGEST_BATCH_FILES` = 50）、背景ジョブ（T-00 `reindex`）、取り込み後の `_settle_relations`、import 元の解き直し（`reresolve.py`）、PageRank の再計算（`rank_scheduler.py`） | DB の SQL（`ingest.py` と `IndexWriter` に任せる） |
| `lsp/pool.py` | 状態機械・再起動・バックオフ・待機解放 | 要求の意味 |
| `lsp/features.py` | 各要求の呼び出し、結果の `SymbolRecord` / `relations` 化、PaaD への書き込み要求 | 直列化形式 |
| `edit/transaction.py` | 8 段の順序と失敗時の不変条件 | hunk の内容 |
| `engine/selector.py` | ラダーと警告文 | エンジンの実行 |
| `tools/t*.py` | 引数モデル、サービス呼び出しの順序、レコードの優先順位付け、ツール固有の kv 行 | 予算・形式・精度集計・エラーの直列化 |

---

# 5. 中核ドメインモデル

`core/model.py` の主要型（`Urn` と `Selector` は `core/urn.py` にあり、`model.py` はそこから import する）。すべて不変（`frozen=True` の dataclass）で、ツール・サービス間の受け渡しに使う。

```python
class Confidence(StrEnum):            # 仕様書 §8.3。順序比較できる（exact が最高）
    EXACT = "exact"; STRUCTURAL = "structural"; HEURISTIC = "heuristic"; LEXICAL = "lexical_fallback"

class Tier(IntEnum): T1 = 1; T2 = 2; T3 = 3; T4 = 4

class Capability(StrEnum):            # エンジン選択の単位（ADR-10）
    SKELETON, SEARCH, DEFINITION, BODY, REFERENCES, IMPLEMENTATIONS, CALL_HIERARCHY,
    DIAGNOSTICS, RENAME, CODE_ACTION, STRUCTURAL_EDIT, SYMBOL_EDIT

class Kind(StrEnum):                  # 仕様書 §6.1 の kind 語彙。値は外部表現（先頭大文字）。File を含む
    MODULE, CLASS, INTERFACE, STRUCT, ENUM, TRAIT, TYPE, FUNCTION, METHOD, CONSTRUCTOR, FIELD, PROPERTY,
    VARIABLE, CONSTANT, TABLE, COLUMN, ENDPOINT, MESSAGE, SERVICE, NODE, EDGE, CELL, TAG, KEY, SECTION,
    FILE                              # ファイル単位の 1 要素（T-18 の Tier 3 / Tier 4）。symbols 表には入れない

@dataclass(frozen=True)
class Position:  line: int; col: int          # 0 始まり・コードポイント（内部表現。ADR-14）
@dataclass(frozen=True)
class Range:     start: Position; end: Position

@dataclass(frozen=True)
class Urn:                                     # 仕様書第 7 章。core/urn.py
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
                              # 任意。残り予算で収まる縮退版（T-02 の doc:→urn:→非公開→入れ子の順に 1 件ずつの省略）を返す。
                              # 収められなければ None。パッカーは「入らないレコード」にこれがあれば一度だけ呼ぶ
    shrink_json: Callable[[int], "Record | None"] | None = None
                              # 任意。shrink と同じ鎖で、json で描いた大きさ（data の JSON + 区切りの ,）が予算以下の最も
                              # 浅い版。パッカーは json のときこれを先に試し、数え直して収まれば使う（BUG-0078）
    section: str | None = None
                              # 任意。答えの本体の外の補足の欄の名前（T-06 / T-07 の exact の答えの name_matched。BUG-0107）。
                              # json は data[records_key] ではなく data[section] に置き、_resolution_meta の精度の集計に数えない。
                              # compact の行・予算・ページネーションは本体と同じ（並びの順に詰める）
    attached: Mapping[str, tuple[Mapping, ...]] | None = None
                              # 任意。このレコードに付随する行（T-07 の json の edges: ノードごとの辺。BUG-0136）。
                              # json は入ったレコードの付随行だけを data[キー] に連ねる（同一の行は最初の 1 件だけ）。
                              # 付随行もレコードの大きさに数える（core.records.json_record_text）。木全体の辺を data_static に
                              # 置くと、ノードを 1 件も詰める前に前置きとして予算を食い、ノード 0 件・予算超過になる

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
| 読み取り用スレッドプール | 既定 8 | SQLite 読み取り、Tree-sitter 解析（`read_callback` 方式。ADR-13）、ファイル読み込み、ハッシュ計算 | `asyncio.to_thread`。Tree-sitter 解析と ast-grep の照合は GIL を握るので、プールでも**並列化しない**（直列比 0.80〜0.98 倍。S-2・S-3 で確認） |
| `IndexWriter` スレッド | 1 | SQLite の全書き込み | ジョブキュー。1 ジョブ = 1 トランザクション |
| 走査の解析の子プロセス（`engine/analysis_pool.py`） | 走査の間だけ。既定は論理 CPU の半分（最大 8） | 索引の走査のファイルの解析（`Indexer._analyze_sync`: 構文解析・シンボル・関係の抽出。DB に触れない） | `spawn` の `ProcessPoolExecutor`。解析の要るファイルが `POOL_MIN_FILES`（64）を超えた走査だけ起こし、走査の終わりに畳む。`LUCIANKE_INDEX_WORKERS` で数を変える（0 で使わない）。BUG-0127 |
| 編集用の直列区間 | ワークスペースごとに 1 | `EditTransaction` の段 3〜7 | `asyncio.Lock`。待ち上限 5,000 ms |
| 子プロセス | 言語 × ワークスペースごと | LSP | 標準入出力をパイプで非同期に読む |

- CPU を使う処理（解析・ハッシュ・大きな JSON 生成）はループ上で行わない。p95 を守る鍵は「ループを空けること」である。ただし Tree-sitter の `parse(bytes)` は、スレッドで走らせても解析の全体にわたって GIL を握ってループを止める（5 MB で約 750 ms）。このため解析は常に `read_callback` 方式（16 KiB 刻み）で行い、ループの停止を最長約 20 ms に抑える（ADR-13。S-2 で確認）。
- **前景の優先**（`core/foreground.py`、BUG-0112。実装で確定 2026-10-31）: 索引の取り込みは CPU を使う Python のコードで GIL を握る。同じプロセスのツールの呼び出しが小さな I/O（`stat`・SQLite の 1 文・Git の状態）を重ねると、I/O のたびに GIL を取り戻すのに切り替え間隔ずつ待たされ（convoy）、大きなワークスペースの初回の索引の間 `configure_workspace status` が 1〜4 秒から 2 分以上に延びていた。パイプラインはツールの呼び出しを `FOREGROUND.active()` で囲み、背景の索引（走査のタスクと、そこから `asyncio.to_thread` で起こした解析のスレッド。`background()` の印が文脈で伝わる）は区切りごと（ファイルの解析の前・解析の 16 KiB の塊ごと・抽出の繰り返しの 1 回ごと・整えのジョブの間・PageRank の反復ごと）に、呼び出しが始まってから `PRIORITY_WINDOW_S`（10 秒）の間は短く眠って譲る（解析の期限は眠った分だけ延ばす）。1 回に譲るのは `MAX_YIELD_S`（2 秒）までで、前景が続いていれば背景は `RUN_SLICE_S`（0.1 秒）進んでからまた譲る（呼ぶ側が絶え間なく呼び続けても索引は止まらない）。書き手スレッドのジョブの中では譲らない（前景の書き込みがそのジョブの後ろで待っているかもしれない）ので、背景の書き込みは短いジョブに割る。窓を過ぎた呼び出し（LSP を待つ T-06 など）では止めない。印の無い文脈（ツールが同じ抽出の関数を前景で呼ぶ）では何もしない。
- 初回索引（数千ファイル）は、解析の要るファイルが `POOL_MIN_FILES`（64）を超えたら残りを**子プロセスに並べて解析し**（`engine/analysis_pool.py`。スレッドでは GIL のため並列化しない。S-2 で確認）、書き込みはキューで直列化する（実装で確定 2026-10-31、BUG-0127）。子は `Indexer.for_analysis(ws)`（書き手・読み手を持たない解析だけの索引器）で `_analyze_sync(rel, loaded)` を走らせ、`_Prepared`（`FileFacts`）を pickle で返す。親は読み込み（`sha256` の照合）を自分で済ませてから子に渡し、結果は**列挙の順に**受け取って 50 ファイルのバッチに積む（`file_id` の振り方・バッチの切れ目・取り込みの順は同じプロセスで解析したときと同じで、索引は行ごとに同じ）。子 1 つあたり 8 ファイルまで先に渡す。ctags の外形（Tier 4）は子が `None` を返し、親がイベントループで作る。子が落ちた（`BrokenProcessPool`）・起こせない・結果を渡せないときは、そのファイルから同じプロセスで解析する。小さな差分の走査は子を起こさない（Windows で起こすのに 1〜2 秒）。子の標準出力は `bootstrap_io` が fd 1 を stderr に重ねた後に起こすので stderr に行く（ADR-3）。exe は `__main__` の `freeze_support` が子の起動（`--multiprocessing-fork`）を受ける。理由: 4,000 ファイル級（Mnemo 4,222 ファイル）で初回の索引が 213〜350 秒かかり、呼ぶ側の時間の上限（120 秒）を超えていた。解析は単独で 125 秒、書き込みは単独で 57〜82 秒かかり、同じプロセスで重ねると書き手が SQLite の文ごとに GIL を取り戻すのを待たされて（convoy）、順に流すより遅かった（246 秒）。子に分けると親の GIL を握るのは書き手と結果の受け取りだけになる。索引中もツールは降格応答で答え続ける（ADR-6、§7.8）。

## 6.2 SQLite 接続（ADR-7）

| 接続 | 開き方 | 使い手 | 備考 |
|---|---|---|---|
| 書き込み | 通常。`journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=5000`、`cache_size=-131072`（128 MiB。BUG-0127） | `IndexWriter` のみ | 起動時にスキーマ版を確認・移行 |
| 読み取りプール | 同じファイルを `mode=ro` の URI で開き、`PRAGMA query_only=ON` を設定する（読み取り側が誤って書けない構造にする。実装で確定 2026-10-29。旧版は「通常モードで開き、アプリ側で書き込み文を発行しない」）。`busy_timeout=1000` | ツール、`EngineSelector`、`EditTransaction` の読み | プールから借りて返す。スレッド境界を越えて保持しない |
| 公開クエリ | `file:...?mode=ro` の URI、`PRAGMA query_only=ON`、`set_authorizer`、`set_progress_handler`、行数上限 | `query_project_db` | 失敗が他接続に波及しないよう独立。1 要求 1 接続で足りる（`open_public` + 1 クエリ + `close` が p50 1.3 ms、書き手が連続コミット中でも p95 約 10 ms。S-7 で確認）。`isolation_level=None`（自動コミット）で開き、読み取りトランザクションを持ち越さない（`wal_checkpoint(TRUNCATE)` を妨げる） |

- WAL のチェックポイントは `IndexWriter` が暇なときと終了時に行う。
- **作り直し**（`reindex`、移行できない古い `schema_version`）は DB ファイルを削除せず、書き込み接続の中で全オブジェクトを `DROP` して流し直す（§7.6.5。Windows では読み取り接続が開いている DB ファイルを消せない。実装で確定 2026-10-29）。
- `data_version`: `_meta` 内部テーブルの整数。`files` / `symbols` / `relations` を変える書き込みトランザクションの最後に `+1`（`WriteJob.bumps_version=True`）。`diagnostics` だけの更新、LSP の結果を `relations` へ `exact` で書き戻す更新（`upsert_exact_relations`。§7.5.6）、PageRank の再計算（§7.6.2）は進めない。読み手は `SELECT` で読む。カーソルとキャッシュの無効化に使う（ADR-8）。
- 内部テーブルは `_meta`（`schema_version`, `data_version`, `edit_seq`）と `_hover_cache`（LSP hover の結果。キー: `urn` + 定義ファイルの `sha256`。§7.5.6）、`_symbol_rank`（シンボル単位の PageRank と fan_in。schema v2。`index/rank.py`）、`_import_probes`（import の解決で調べたパスの語幹。schema v3。`engine/reresolve.py`）、`_exact_marks`（LSP の exact の集合が完全だった目印。schema v4。ADR-16）、`_name_calls`（受け手の型が分からない属性呼び出し `x.name(...)` の位置と名前。schema v5。索引器が書き、T-06 / T-07 が `tools/_name_calls.py` で名前の一意性から結ぶ。v4 からの移行は表を足し、Python / TS / JS のファイルの `sha256` を空にして次の差分走査で取り込み直させる。BUG-0071）、`_member_refs`（受け手のクラスに無い〔基底クラスから継承した〕メンバーへの参照と、いま `relations` に作ってある先。schema v6。`index/inherited.py`。v5 からの移行は v5 と同じく表を足してコードのファイルを取り込み直させる。BUG-0075。schema v7 で `via_super` を足した: `super().m()` の行を取り込みが直接入れ、整えが受け手のクラスの次から探す。v6 からの移行は列を足してコードのファイルを取り込み直させる。BUG-0081。schema v8 は表を変えず、v7 からの移行でコードのファイルを取り込み直させる: 型の分からない名前の受け手 `x.save()` を同じファイルの唯一の `.save` に結んだ誤った行を消す。BUG-0085。schema v9 で `_name_calls.use_kind`〔`call` / `r` / `w` / `rw`。型の分からない受け手の属性の読み書きも名前の一致に任せる。BUG-0095〕と、`_attr_defs`〔インスタンス属性の定義 `self.x = ...` / `this.x = ...`。名前の一致の候補。BUG-0095〕、`_var_types`〔モジュール直下の変数の構文から分かる型。import した変数の `settings.reload()` を型のメンバーに結ぶ。BUG-0094〕を足した。v8 からの移行は列と表を足してコードのファイルを取り込み直させる。schema v10 で `_reexports`〔Python のモジュール直下の import が束ねた名前とその先・`from x import *` の先・`__all__`。整えが再び公開した名前を定義元までたどる。BUG-0100〕と `_reexport_deps`〔たどるときに引いた `(ファイル, 名前)`。そのファイルの公開する名前が変わったら依っていたファイルを取り込み直す〕を足した。v9 からの移行は表を足して Python のファイルを取り込み直させる〔名前空間パッケージの読みを外部にしない。BUG-0101〕。schema v11 は表を変えず、`_reexports` に TS / JS のモジュールが公開する名前〔`import`: `export { a as b } from` などの別の定義を公開する名前の束ね先、`star`: `export * from` の先、`export`: 公開する名前。BUG-0104〕と、Python のリテラルでない `__all__` の印〔`dynamic_all`。BUG-0105〕を書く。v10 からの移行は TS / JS のファイル・`from x import *` を持つ Python のファイル・`__all__` を書くがリテラルの名前の無い Python のファイルを取り込み直させる。schema v12 で `_type_aliases`〔TS の型の別名の構成。`op`: `and`（別名そのもの・交差）/ `or`（合併）、`kind`: `type`（構成の型の URN）/ `member`（別名自身の型リテラルのメンバー `<別名>.<名前>`）/ `opaque`（中身の分からない構成）。整えが型の別名を注釈にした受け手のメンバーを構成から探す。BUG-0108〕を足した。v11 からの移行は表を足して TS / JS のファイルを取り込み直させる〔既定の import の名前を既定の export に結ぶ。BUG-0109〕。schema v13 は表を変えず、`_type_aliases` に型注釈つきのメンバー〔インターフェース・型リテラルのプロパティ、クラスのフィールド〕の型の構成も書く〔`alias_urn` がメンバー。受け手 `r: Opts['retry']`・`o.retry.limit` の型。中身の分からない構成しか無いメンバーは書かない。BUG-0113〕。TS の型リテラルのメンバーがシンボルになった〔BUG-0114〕ので、v12 からの移行は TS / JS のファイルを取り込み直させる）。名前が `_` で始まるものは公開クエリから見えない（仕様書 §9.1）。公開 DDL（仕様書 §9.2）に列や表を足さないための表で、`schema.sql` に写しを置く。

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

1. `bootstrap_io`（fd 1 の退避・差し替え）→ 2. ログ → 3. 設定 → 4. キャッシュディレクトリ → 5. `IndexWriter` 起動とスキーマ移行 → 6. MCP サーバーの `initialize` 受付 → 7. （取れたら）`roots/list` → `WorkspaceContext` 登録 → 8. 差分索引ジョブ投入（背景）→ 9. 監視開始。

LSP はここでは起動しない。

`roots/list` は **取れたら** 使う前提にする（S-1 で確認）。
- 要求は `initialize` の処理中ではなく `notifications/initialized` のハンドラ内で出し、タイムアウトは 5 s（`initialize` の処理中に待つとデッドロックする。通知ハンドラは別タスクなので待てる）。`notifications/roots/list_changed` は来たら同じ手順で取り直す。
- ホストが roots 能力を宣言していない場合（`ctx.session.client_capabilities.roots is None`）は要求しない。2026-07-28 版の MCP では `initialized` と roots の `list_changed` が仕様から消え、サーバー起点の `roots/list` もできない（代わりに `tools/call` の結果へ `InputRequiredResult` を載せてクライアントに再呼び出しさせる方式。SDK 2.3 は roots を非推奨とし、呼ぶたび `MCPDeprecationWarning` が出る）。この方式は、ホストが 2026-07-28 を話し始めた時点で M2 以降の課題として扱う。`server/discover` のハンドラを外して旧版に固定する案は不可（`Method not found` を返した時点で接続が 2026-07-28 に確定し、続く `initialize` が拒否される）。Lucianke は両方の版を受ける前提で作る。
- **取れないとき**（roots 能力なし・2026-07-28 版・タイムアウト）は、カレントディレクトリをルート候補にし、T-00 `set_roots` で登録できる。`roots/list` に頼らなくても動く（仕様書 §5.1）。
- コールドスタート（プロセス起動 → `initialize` 応答）は中央値約 1.0 s（959〜1030 ms、n=7）で、SDK の import が約 890 ms を占める。初回の重さとして許容する（仕様書 §14.4。S-1 で確認）。

Lucianke はホストが起動する子プロセスであり、寿命はホストのセッションと同じ（`stdin` の EOF で終わる）。常駐デーモンや複数ホストで共有するサービスにはしない（ADR-16）。

終了順（`stdin` EOF / SIGTERM / SIGINT / Windows の `SIGBREAK`）:

1. 新規 `tools/call` を `E_UNSUPPORTED`（`details.reason: "shutting_down"`）で拒否 → 2. 実行中の編集トランザクションの完了またはロールバックを最大 5 s 待つ → 3. 監視停止 → 4. `LspPool.shutdown_all()`（`shutdown` → `exit` → 500 ms → `terminate` → `kill`）→ 5. `ProcessRegistry` に残った子を `kill` → 6. `IndexWriter` のキューを空にしてチェックポイント → 7. 終了コード 0。

- Windows では `loop.add_signal_handler` が使えないため、`signal.signal` でフラグを立て、ループ側の監視タスクが拾う。
- 親プロセスの異常終了は `stdin` の EOF で検知する（MCP SDK のトランスポート終了。S-1 で確認: stdin を閉じると 117〜154 ms で終了コード 0）。

---

# 7. コンポーネント詳細設計

## 7.1 MCP アダプタとツールパイプライン

### 7.1.1 `ToolSpec`（`tools/_base.py`。`mcp/registry.py` が再公開）

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
  1. spec = registry[name]                      無ければ JSON-RPC エラー（`MCPError(INVALID_PARAMS)`、-32602。これだけがプロトコル違反）
  2. args = spec.args_model.model_validate(arguments)   失敗 → E_INVALID_ARGUMENT（details.param。isError の結果。JSON-RPC エラーにしない）
  3. ctx  = ToolContext(workspace, revision, format, budget, cursor, request_id, started_at)
       - workspace_id の解決（T-00 手順 7 の順序）  → E_WORKSPACE_NOT_CONFIGURED
       - revision の解決（WORKING_TREE / STAGED / HEAD / 名前 / SHA）→ E_REVISION_NOT_FOUND
       - format の可否（allows_graph）、max_tokens > 0（固定の下限なし。BUG-0063）、writes なら revision == WORKING_TREE
       - cursor があれば復号し data_version を照合 → E_CURSOR_EXPIRED
  4. out  = await spec.handler(ctx, args)         LuciankeError はここで捕捉
  5. page = Packer.pack(out, ctx.budget, ctx.cursor_offset)   レコードを詰め、残りを要約、next_cursor
  6. meta = ResolutionMeta.from(page, out.max_confidence, out.engine)
  7. text = Renderer[ctx.format].render(spec, out, page, meta)
  8. result = CallToolResult(content=[text], structuredContent=(json のとき), isError=False)
  例外: LuciankeError → envelope(ok=false, error, meta) を同じ経路で描画し isError=True
        その他の例外 → ログに stack、E_INTERNAL（details.log_ref）
```

- `timing_ms` は 1 の直前から 8 の直前まで。パイプライン内の自前計測とし、SDK の `Server.middleware`（暫定 API）は使わない（S-1・S-9 で確認）。
- **SDK は引数をスキーマで検証せず、ハンドラから漏れた例外も捕捉しない**（漏れると `error.code=0` で例外文面がそのままクライアントへ出る）。したがって 2 の検証と、`LuciankeError` 以外の例外の捕捉（`E_INTERNAL`）は、このパイプラインが最外周で全責任を持つ（S-1 で確認）。結果は `types.CallToolResult(content=[TextContent], structured_content=…, is_error=…)` で返す。
- 往復の予算: SDK クライアントから見た何もしないツールの往復は p95 1.08 ms（2026-07-28 版でも 1.89 ms）、サーバー内の SDK 処理は約 45 µs、pydantic の検証は 3 µs 前後で、8 KB の応答も差が出ない。往復に p99 で 2.5 ms を見込めば、NFR-PERF-1（p95 < 50 ms）のうちハンドラに 47 ms 以上を割り当てられる（S-9 で確認）。
- ハンドラは `ctx.reader()`（読み取り接続）、`ctx.services`（各サービス）、`ctx.resolve_target(ref)`（仕様書 §5.3）を使う。

### 7.1.3 `ToolContext.resolve_target`（仕様書 §5.3）

1. `urn` → `index.queries.symbol_by_urn`。無ければ緩和照合（`~sighash`・`/arity` を外す）→ 1 件なら `warnings` に記録、複数なら曖昧。
2. `path + symbol_name` → セレクタ接頭辞（`xpath:` 等）ならその場で `DataTarget` を返す。そうでなければ `qualified_name` の末尾一致を `symbols` で検索。0 件 → `E_TARGET_NOT_FOUND`、2 件以上 → `E_TARGET_AMBIGUOUS`（候補 10 件）。
3. `path + line (+character)` → `symbols` で範囲を含む最内の行。
4. `revision` が `WORKING_TREE` 以外なら、`symbols` ではなく仮想ツリーの一時索引（§7.9）を引く。
5. 監視がポーリングモード（§7.3.6）のときは、解決前に対象ファイルの `sha256` を `files` と照合し、違っていれば先に再取り込みを同期で行う（遅延検証。BUG-0096、実装で確定 2026-10-31）。`resolve_target` は同期（読み取り接続の中）なので、ハンドラが解決の前に `tools/_building.py` の `ensure_indexed` / `ensure_target_indexed` / `ensure_targets_indexed`（T-04・T-06・T-07・T-08 が呼ぶ。走査の途中の先取りと同じ入口）を呼び、その中で `WiredServices.revalidate(ws, rel)` が `Watcher.drain([rel])` を呼び手の文脈で行う（`sha256` が索引と同じなら何もせず、違えば `sink.changed` で取り込み直し、消えていれば `removed`。LSP への通知も同じ道）。`native` のときは何もしない。対象のファイル 1 つだけを照合するので、参照元・呼び出し元など **対象以外のファイルは古いことがある**。呼ぶ側の AI がそれと分かるよう、`polling` の間は索引を読むツール（`ToolSpec.reads_index`）の応答に `warnings[]` を 1 つ添える（`file watching unavailable (polling): the target file is checked against the disk on each call, but other files may be stale until the next reindex`。パイプラインが `building_warning` と同じ場所で付ける）。
6. ファイル内容の取得は常に `ToolContext.file_source(rel) -> bytes` を通す。`revision` が `WORKING_TREE` なら作業ツリー、それ以外なら仮想ツリー（§7.9.1）から読む。ツールが `open()` を直接呼ぶことはない（MUST）。

## 7.2 出力パイプライン

### 7.2.1 `TokenEstimator`

- **`lean_wide` 式**（文字種別と連の数の係数）で推定する（S-12 で `o200k_base` と比べて較正し、27e0821 で `core/tokens.py` に採用済み）。旧版の「ASCII 英数字と空白 1/4、ASCII 記号 1/2、全角・漢字・かな 1/1.5、その他 1/2」は、日本語 Markdown で −16〜−17%、compact 出力で −11%、base64 などの塊で −43〜−63% と**過小**だった（係数だけ当て直しても通らない。ASCII は文字数ではなく語の数、空白と記号は連でトークンになる）。トークン数 = 切り上げ(Σ 係数 × 数)。係数は 1/100 刻みの有理数で、整数演算にできる（分母 100）。置き場は `core/tokens.py` の `TOKEN_COEFFICIENTS`（`Fraction` の表。`tokens.toml` は設けない。旧名 `TOKENS_PER_CHAR` は別名で残す。実装で確定 2026-10-29）。ベンチで `o200k_base` と比べて ±10% に保つ（§10.4）:
  - `blob_chars` 0.63: `[A-Za-z0-9+/=]{40,}` の文字数（base64・SHA-1/256 の hex）。**先に数えて本文から除く**。
  - `letters_runs` 0.85: `[A-Za-z]+` の個数。`digits_chars` 0.90: `[0-9]` の文字数。`symbol_runs` 0.87: ASCII 記号 `` [!-/:-@\[-`{-~]+ `` の連の個数。
  - `space_runs_multi` 0.92: `[ \t]{2,}` の個数（インデント）。単一の空白は 0（語に吸収される）。`nl_chars` 0.60: `\r` と `\n` の文字数。
  - `wide` 0.95: 仮名・漢字・全角記号の文字数（U+3000–30FF、U+31F0–31FF、U+3400–4DBF、U+4E00–9FFF、U+F900–FAFF、U+FF00–FFEF、U+20000–2FA1F）。`other` 0.46: 上記のどれでもない文字の個数（ASCII 以外の記号・制御文字・ハングルなど）。
  - 誤差（ファイル単位の 2 分割交差検証の保持側）は、種類別の合計で ±3.3% 以内（標本が 1 つの base64 だけ標本内で −8.9%）。安全側にしたければ全係数を ×1.03 にしてよい（MAY。推定が実測より小さいと実測が `max_tokens` を超えるため）。**採用では見送り**（実装で確定 2026-10-29）: 合成 base64 が −8.3% で ±10% に近いので、M1 のベンチで再較正し、TS・Go・Rust・韓国語を標本に加える。ASCII だけの文字列は `wide` の走査を省く（`text.isascii()`）。1 MB で旧版の約 2.3〜2.8 倍（150〜200 ms）だが、パッカーは予算までしか数えない（8 KB で約 1.5 ms）ので許容する。
  - 限界: 当てはめの標本は本リポジトリ（日本語コメントの Python が多い）に偏り、TS・Go・Rust・韓国語は未検証。`bench/run_bench.py` で較正を再実行し、種類別の合計誤差が ±10% を超えたら失敗にする。単体テスト（`tests/unit`）は tiktoken なしで、固定の文字列と基準トークン数との比が ±10% に入ることを確かめ、陽性対照に旧版の係数が落ちることも置く。実装は `tests/unit/test_core_tokens_calibration.py` が、`tests/fixtures/tokens/` に凍結した標本（`bench/tokens/freeze_corpus.py` が S-12 のコーパスから種類ごとに切り出して写した本文と、`manifest.json` に記録した `o200k_base` 基準・SHA-256）で種類ごとの合計・平均誤差 ±10% を検査し（結果〔2026-10-31 の凍結〕: compact −0.9%、日本語 md −1.4%、spikes −1.0%、poly コード −1.1%、ipynb −3.6%、py_src −0.4%、py_tests +0.1%、合成 base64 −8.3%・hex +7.7%）、旧式が落ちる陽性対照を置く。リポジトリの生のファイルは読まない（生のファイルを `bench/tokens/result.json` の文字数と照合していた頃は、書き換わった種類が skip になり検証が抜けた。BUG-0068）。凍結した本文が記録と食い違う・種類が欠けるときは skip せず失敗にする。速度は 1 MB で py_src 126 ms、日本語 md 148 ms、compact 188 ms。再現は `bench/tokens/calibrate.py`。
- 1 レコードの推定は `lines` の各行 + 行末の改行の合計（実装で確定 2026-10-29: 改行の係数が 0.25 から 0.6 に上がったので、パッカーは各行の終端の改行も数える。レコード別の和が応答全体の推定以上になる）。縮退（`Record.shrink`）には終端の改行の分（1）を引いた予算を渡す。ツール側の縮退の見積もり（`tools/_services.py` の `estimate_tokens`）もパッカーと同じ推定器を使う。ヘッダ・警告・トレーラも同じ推定器で数える。

### 7.2.2 `Packer`

```
pack(out, budget, offset):
  avail(m) = budget - reserved_for(m)              # reserved_for(m) = 先頭 m 件のページの前置き（ヘッダ・降格の警告行・head_kv・json の封筒。呼び手が数える。BUG-0116）
  # 鎖「r0 の縮退版 → r0 丸ごと → r1 の縮退版 → r1 丸ごと → …」のうち、トレーラ込みで avail に収まる最も先の形（BUG-0067）
  for m in (先頭 m-1 件を丸ごと入れた本文 <= avail の最大の m から 1 まで):
      room = avail(m) - Σ est(r0..r[m-2]) - trailer(m)  # trailer = "# more:" 行 + 要約の行の min(全長, avail(m) // 4)
      if est(r[m-1]) <= room: included = r0..r[m-1]; break
      small = r[m-1].shrink(room) if r[m-1].shrink else None   # 縮退版（T-02 のファイル内省略）はページの最後の 1 件
      if small: included = r0..r[m-2] + [small]; break         # 縮退版の後ろには詰めない
  rest = out.records[offset + len(included):]
  unseen = max(0, out.total_estimate - len(out.records))       # ツールが具体化していない続き（BUG-0008。下の total_estimate の意味）
  if rest or unseen: next_cursor = cursor_factory(offset + len(included))   # 短い鍵（§7.2.3。表に入れるのは応答に出した分だけ）
           summary = out.summarize(rest) or default_summary(rest)   # group ごとの件数
  meta: min confidence, breakdown, mixed  ← included から集計
  return Page(included, next_cursor, summary, total_estimate=max(len(out.records), out.total_estimate))
```

- **応答全体の予算（BUG-0014・BUG-0017。`mcp/fit.py` の `pack_and_render`）**: パッカーが詰めるのはレコードとトレーラだけで、ヘッダ・警告・降格の警告行・`head_kv`（compact）や封筒・`_resolution_meta`・`data_static`（json）は `reserved_tokens` で呼び手が渡す。パイプラインは、**レコード 0 件のページを同じレンダラで実際に描いた全文**（`used_tokens_est` と `timing_ms` は最大桁数）を数えて `reserved_tokens` にする。降格の警告行・ヘッダの `conf=`・`_resolution_meta` はページに入ったレコードの精度で出るので、精度の集計が変わる位置で段に分け（ふつう 1〜2 段。最大で精度の水準の数）、段ごとに「その段の最後の形（先頭から段の終わりまで）の集計」で 0 件のページを描いて数え、前の段までの最大を取った値を `reserved_for(m)`（先頭から m 件のページの前置き）としてパッカーに渡す（BUG-0116、2026-10-31）。値は m について減らず予算に依らないので、鎖の各形が自分の件数の前置きで「収まる」を判定しても単調性は保たれる（各形の判定は予算について単調で、ページはその中で最も先の形）。理由: 以前は全レコードの集計で 1 つだけ数えたので、後ろに精度の低いレコード（COBOL のファイルなど）が 1 つでもあると、そのページに出ない注意文の分を毎ページ取り分けた（T-01 の compact `max_tokens=260` で前置き約 30 に 106、約 78 トークンが使われずに返った）。0 件のページの前置き（`reserved_tokens`）はレコード 0 件の集計（`out.engine.confidence`）で数える。詰めたあと全文を数え直し、`max_tokens` を超えていれば超過分を `reserved_tokens` に足して詰め直す（`returned` の桁数など見積もりの誤差を吸収する）。最後に `used_tokens_est` を数え直しの値に置き換えて描き直す（数字は 1 文字ずつ数えるので、桁数が変わらなければ 1 回で収束する）。数え直しは `timing_ms` を最大桁数にした全文で行う（json の `timing_ms` は全文に入る。同じ入力なら呼び出しごとの処理時間にかかわらず同じ値になり、実際の全文以上の上限になる。差は最大 4 トークン）。この結果、描画後の全文の数え直し ≤ `used_tokens_est` ≤ `max_tokens`（compact は一致。json は上の差だけ上）が全応答で成り立つ。dot / mermaid は対象外（`data_static["graph"]` を丸ごと描くため。別の票）。
- **形式ごとの見積もり**: `Packer.pack` は `record_text(rec)`（1 レコードが描かれるときの本文）と `trailer_text(summary, cursor, remaining)`（続きがあるときに増える本文）を受ける。既定は compact（`Record.lines` + 行末の改行、`# summary:` / `# more:` の 2 行）。json は `render/json_out.py` の `record_text`（`data` の JSON + 区切りの `,`）と `trailer_text`（`next_cursor` の文字列だけ。json に要約の行は無い）。`Record.shrink` は compact の行で収める約束なので、別の形式で数えて収まらなければ、`ask`（compact の行の予算）を二分探索して json で収まる最大の縮退版を探す（最大 16 回。json の大きさは compact の行に比例しない固定分があるため、比例で縮めても収束しない）。`pack(..., form="json")` で `Record.shrink_json` があるレコード（T-02 の骨格）は、先にそれで json の予算に収まる版を求め、数え直して収まればそれを使う（版ごとに JSON を作って数える二分探索を省く。BUG-0078）。
- **予算に対する単調性（BUG-0067、仕様書 §5.6-5）**: 上の鎖は予算に依らないので、`max_tokens` を上げても件数は減らず、どのレコードも小さくならない。以前は縮退版の後ろにも詰め（予算を上げて先頭が丸ごと入ると後ろが次ページに回った）、要約の分を丸ごと先に取り分けて 1 件も入らないときだけ省いていた（要約つきで 1 件入る予算に上げると件数が減った）。要約の取り分は avail の 1/4 まで（予算 1 トークンにつき高々 1 トークンしか増えない）で、長い要約は「…」で切る。`tests/unit/test_mcp_budget_monotone.py` が乱数で確かめる。
- **要約は先頭の 1 件とカーソルを押し出さない（BUG-0110）**: 取り分は `min(avail // 4, max(0, avail − F))`。F は先頭のレコードの最も小さい形と `# more:` 行（要約なし）が入るのに要る本文で、予算に依らない: 縮退できなければ丸ごと + `# more:`、縮退できれば `min(丸ごと + # more:, 縮退版が返る最小の ask + 1 + 再開位置が最大のときの # more:)`（縮退版は ask に収まる最大の版を返すので、縮退版が返る最小の ask が最も小さい形。途中で切れた版は続きのカーソルが長いので、再開位置の上限で数えた `# more:` を使う）。どちらの項も予算 1 トークンにつき高々 1 トークンしか増えないので `avail − 取り分` は予算について減らず、鎖の各形の「収まる」と縮退版の大きさは単調のまま。以前は 1/4 を必ず取り分けたので、F ≤ avail < F + 要約の帯で 0 件になった（compact の既定の形式。カーソルの行は約 60〜80 トークンで、1 行約 20 トークンの参照より重い）。F は取り分が 1/4 より小さくなりうるとき（`avail − 丸ごとの F < avail // 4` で先頭が縮退できる）だけ求める: まず「1/4 を取っても最も小さい形が入るか」を縮退 1 回で見て、入らないときだけ帯の中（幅は avail の 1/4）を二分探索する。要約の分がある形を初めて数えるときに 1 度だけ求める（続きの無い 1 件だけの答えでは縮退の呼び出しを増やさない）。縮退版でページを閉じるときの要約（BUG-0067 の取り分）はこれまでどおり残る。`tests/unit/test_mcp_budget_fit.py` が本物のレンダラと `pack_and_render` を通した予算の掃引で確かめる（パッカー単体・json だけの試験では見えなかった）。
- 1 件も入らないときは `included = []`、`next_cursor` なし（進まないカーソルは出さない。BUG-0015）、`warnings` に理由と必要な `max_tokens` の目安（仕様書 §5.6 の 4）。トレーラは描かれないので `used` に数えない。目安は `reserved + min(先頭を丸ごと + # more:, 残りの全部)`（残りの全部は具体化していない続きが無いときだけ。トレーラなしで全部入る予算）で、`Page.need_max_tokens` にも入れる。`pack_and_render` はその値で詰めて描き直した全文を数え、警告なしに収まる（1 件以上入る）まで上げてから（最大 6 回）、パッカーに `min_need` として渡して警告を書かせる（BUG-0110。以前の目安 `reserved + 先頭` は `# more:` 行を数えず、呼んだ値より小さかった）。`next_cursor` を返すページは必ず 1 件以上を含み、オフセットが進む。
- `records` は遅延列でもよい（巨大な参照一覧をすべて具体化しないため、ツールは生成器を渡してよい。`total_estimate` は別途 `COUNT` で与える）。
  - **`total_estimate` の意味（BUG-0008 で確定）**: 並び全体の件数。`records` は全体の先頭 `len(records)` 件（具体化した接頭辞）で、`total_estimate > len(records)` なら差（`unseen`）は具体化していない続きである。パッカーは `unseen` も「続き」に数える: 渡された分が全部入っても `next_cursor`（オフセットは `len(records)` 以降の入らなかった先頭）と `# more: … remaining≈N`（N = 入らなかった分 + `unseen`）を出し、トレーラの分も予算に入れる（最後の 1 件にも要る）。`total_estimate` が `None` または `len(records)` 以下なら従来どおり（`records` が全体）。
  - 次のページ: ツールはカーソルのオフセット `o` を `ctx.cursor_offset` で受け取り、`o + limit` 件までを渡し直す（T-03 の方式。`limit` はその 1 ページの最大件数）。パッカーは `o` から詰める。ツールは `o` 以降を具体化しなければならない（`o ≥ len(records)` はツールの誤りで、パッカーは進まないカーソルを出さず空のページを返す）。要約は `summarize(rest)` が具体化した残りだけを受け取る（`rest` が空で `unseen` だけのときは既定の `N more`、`unseen` は group 不明として `others` に数える）。

### 7.2.3 `CursorCodec`

カーソルの文字列は短い鍵 `c` + 10 進 9 桁（`c482139071`。o200k で約 3 トークン。`c` は JSON で数値と取り違えないため）。中身 `CursorState{tool, args_hash = sha1(cursor・max_tokens・format を除く引数の正規化 JSON)[:12], offset, data_version, workspace_id}` は `CursorCodec` の表（メモリ・`OrderedDict`。`CURSOR_TABLE_MAX` = 4,096 件。発行と照合で新しい側へ移し、超えたら最も長く使われていないものから捨てる）に持つ。鍵は中身の SHA-256 から決める（同じ中身なら同じ鍵、プロセスをまたいでも同じ。応答が決定的）。表の別の中身とぶつかったときだけ塩を足して引き直す（最大 32 回。空間 10 億に表 4,096 件なので事実上起きない）。`peek` は表に入れずに鍵を返し（パッカーが見積もりで作るカーソル。続きの形ごと・再開位置の上限など 1 回の呼び出しで数個）、`encode` は表に入れる。パイプラインは `cursor_factory` に `peek` を渡し、描き終えて応答に出した `next_cursor` だけを `encode` で発行する（見積もりで作った分で表の上限を使わない）。照合（`decode`）: 鍵の形でなければ `E_INVALID_ARGUMENT`（`details.param: "cursor"`）、表に無ければ `E_CURSOR_EXPIRED`（`details.reason: "unknown_cursor"`。再起動・捨てた・発行していない）、`tool`・`args_hash`・`workspace_id` が一致しなければ `E_INVALID_ARGUMENT`（失効より先に見る）、`data_version` が今と違えば `E_CURSOR_EXPIRED`（`"data_changed"`）。表はセッション限定の軽い対応表（ADR-16 決定 4）で、ファイルや DB には書かない（再起動耐性は「失効して先頭から再取得できる」）。理由（BUG-0115、2026-10-31）: 以前は中身を `base64url(json(...))` にして渡していた（無署名・中身が読める）ので、カーソル 1 つが約 70 トークン（`find_references_and_implementations` で 112 文字）あり、`# more:` 行は約 82 トークンで参照 1 行（約 22）の約 3.7 行ぶんだった。件数の少ない答えは「1 件 + カーソル」が全体より大きく、全体より小さい予算では compact で 1 件も返らなかった。

### 7.2.4 レンダラ

| 形式 | 入力 | 出力 |
|---|---|---|
| compact | ヘッダ（`# <tool> ok conf= lsp= rev= ws= ~used/max tok`）、`! ` 警告（降格時）、`head_kv`、`Record.lines` の連結、`# summary:`、`# more:` | 文字列 |
| json | `ResponseEnvelope`（`data = data_static + {records の配列名: [rec.data]}`） | 文字列 + `structuredContent` |
| dot / mermaid | `out.data_static["edges"]` と `records` のうち `kind in {"tree", "path", "ref", "row"}` | 文字列。ヘッダはコメント |

- `ToolOutput.head_kv` は常に予算内に含める（短い kv 行だけを置く）。編集ツールの `diff` のように長くなりうるものは `Record(kind="code")` として渡し、パッカーの切り詰め対象にする。
- compact の各行種（`symbol-block`, `ref-line`, `diag-line`, `tree-line` …）を組み立てる関数は `core/compact_lines.py`（`mcp/render/compact.py` は連結とヘッダ・トレーラだけ）に **行種ごとに 1 つ** 置き、ツールと `syntax` はそれを呼んで `Record.lines` を作る。文法（仕様書 §6.1）に適合するかは、この関数群の単体テストで EBNF 由来のパーサに通して検査する（§10.2。検査器は `tests/contract/compact_grammar.py`。仕様書 §11 の例も通す）。

---
## 7.3 Workspace Context（workspace/）

ワークスペースの境界・環境・言語・設定・外部変更の検知を担う。他のサービスは「どのファイルが対象で、どの言語で、どこまで触ってよいか」をすべてここから受け取る。

### 7.3.1 ルートとワークスペース ID

```python
@dataclass(frozen=True)
class Workspace:
    workspace_id: str            # 仕様書 §7.2 の文字集合 [A-Za-z0-9._-]+
    root: Path                   # realpath 済みの絶対パス
    workspace_hash: str          # sha1(normcase(root)) の先頭 16 桁（core/hashing.py。実装で確定 2026-10-29）。<cache_root>/<workspace_hash>/ に使う
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
    # 1. NUL・制御文字を拒否。Windows では下記の拒否対象も（reason: invalid_chars）
    # 2. 区切りを '/' に統一。絶対パスならそのまま、相対なら ws.root と結合
    # 3. os.path.realpath でシンボリックリンクと '..' を解決
    # 4. normcase（Windows / macOS の既定は大文字小文字を区別しない）した上で、
    #    結果が ws.root と等しいか、ws.root + sep で始まることを検査
    # 5. for_write のとき: '.git/' 配下、exclude_globs 一致、秘密ファイル（§7.3.3）を拒否（仕様書 §13.4）
    # 6. ws.root からの相対パス（POSIX 区切り）を返す
```

失敗はすべて `E_INVALID_ARGUMENT`（`details.param` に引数名、`details.reason` に `outside_root` / `symlink_escape` / `git_dir` / `excluded` / `secret_file` / `invalid_chars`。後ろの 2 つは実装で確定 2026-10-29）。

- **Windows で拒否するもの**（`invalid_chars`）: NTFS の代替データストリーム（`a.py:s`）、予約名（`CON`・`NUL`・`COM1` など。拡張子が付いていても）、末尾のドットと空白（Win32 が黙って削る名前）、ドライブ相対（`C:foo`）、UNC（`\\server\share`）。ドライブ付きの絶対パスはルート配下であれば通る。
- **`is_excluded(ws, rel)`** は `exclude_globs` を gitignore 風に評価する: `*` は `/` を越えず、`**` は `/` を越える。`/` を含まない名前（`node_modules`、`*.min.js`）は任意の階層の名前に一致し、末尾が `/` のものはディレクトリにだけ一致する。一致するディレクトリの配下も除外される。大文字小文字を区別しないファイルシステムでは区別しない（実装で確定 2026-10-29）。LSP から返る `file://` URI も受信時に `confine` を通し、ルート外なら外部シンボル（`lk://_ext/...`、§7.4.3）として扱う。

受入で確かめる点:

- `../../etc/passwd`、ルート外を指すシンボリックリンク、別ドライブの絶対パスが `E_INVALID_ARGUMENT` になる。
- ルート配下の絶対パス（Windows ではドライブ文字の大小が違っても）が相対パスに正規化される。

### 7.3.3 言語判定と Tier

```python
def detect(rel: RelPath, head: bytes) -> Detected:   # Detected(language_id, tier, index_content: bool)
```

1. 先頭 8 KB に NUL があれば `language_id="binary"`, `tier=4`, `index_content=False`。
2. 拡張子 → `workspace/languages.py` の拡張子表（`syntax/support.py` の登録表 〔`LanguageSupport.extensions` と `FormatAdapter.extensions`〕 と同じ内容。`workspace` は `syntax` を import しない〔§4.2〕ので、表はここが持つ。実装で確定 2026-10-29）。
3. 拡張子で決まらなければシバン（`#!…python`, `node`, `sh`）、モードライン（`-*- mode: … -*-`, `vim: ft=`）。
4. `.gitattributes` の `linguist-language` を読んでもよい（MAY）。
5. いずれでも決まらなければ `unknown`、`tier=4`。
6. 秘密らしきファイル（仕様書 §13.5 のパターン）は言語を判定したうえで `index_content=False`。`files` 行は作るが本文は読まない。

`is_test(rel)` は仕様書 §9.3 の既定規則（`test/`, `tests/`, `*_test.*`, `*.spec.*`, `*.test.*`, `test_*.py`）に `LanguageSupport.test_path_globs` を加えたもの。T-06 の `include_tests` と T-09 はこの列だけを見る。

### 7.3.4 Python 環境の検出

T-00 手順 3 の順で候補を試し、最初に実行できたものを採る: 明示指定 → `<root>/.venv/` → `<root>/venv/` → `<root>/env/` → `<root>/.env/`（Windows は `Scripts/python.exe`、他は `bin/python`。`.venv` 以外は `pyvenv.cfg` があるときだけ。`venv` は `python -m venv venv` の既定の名前、`.env` は普通 dotenv のファイルなので `pyvenv.cfg` で見分ける。BUG-0111。`manager` は隣の `uv.lock` → `uv`、`poetry.lock` → `poetry`、無ければ `venv`）→ `VIRTUAL_ENV`（根の中を指すときだけ。根の外は Lucianke 自身の venv でありうる）→ `uv.lock` があるときの `.venv` → `poetry`（PATH にあれば `poetry env info -p`）→ `CONDA_PREFIX` → PATH 上の `python`。外部コマンドはすべて `ProcessRegistry`（ADR-12）経由、上限 3,000 ms、失敗は次の候補へ。結果 `PythonEnv(interpreter, version, manager)` は `workspaces.python_env` に保存し、Python 系 LSP の初期化（§7.5.1）に渡す。

### 7.3.5 設定の優先順位

`Settings` は pydantic モデル。合成順は仕様書 §12.7: ツールでの明示 > 環境変数 > `.lucianke.toml` > 自動検出。`.lucianke.toml` は読むだけ（MUST NOT 書く）。解析できなければ無視して T-00 `status` の `warnings` に出す。`exclude_globs` / `python_env` / `default_base_ref` は `workspaces` 行に保存し、次回起動時の初期値にする。

### 7.3.6 ファイル監視（watcher.py）

```python
class Watcher:
    async def run(self, ws: Workspace, sink: IngestSink, stop: asyncio.Event) -> None
    mode: Literal["native", "polling"]
    async def drain(self, paths: Sequence[RelPath] | None = None) -> None   # 未処理イベントを同期で処理し切る（EditTransaction 段 3、§7.7.1）
    def expect(self, path: RelPath, sha256_after: str) -> None             # 自分の書き込みのイベントを、内容が一致する間無視する（§7.7.1 Reindexed。索引に取り込めたファイルだけ）
    def forget(self, path: RelPath) -> None                                # そのパスの期待を消す（取り込めなかったファイル。こだまを sink に届ける。BUG-0087）
```

1. `watchfiles.awatch(ws.root, debounce=WATCH_DEBOUNCE_MS, step=WATCH_STEP_MS, watch_filter=…, stop_event=stop)` を用いる（S-6 で Windows の挙動を確認）。**`step=10` が必須**: 既定の `step=50` では単発変更の p95 が 100.6 ms で NFR-PERF-3 の境界に当たり、`step=10` なら p50 18.8 / p95 19.9 ms（遅延はほぼ 1〜2×`step`）。`WATCH_STEP_MS = 10`、`WATCH_DEBOUNCE_MS = 1000`。`debounce` の意味は「パスが増え続ける間にグループ化する最大時間」で、単発の遅延には効かない（同じファイルを 3 ms ごとに書き続けても 21 ms 前後ごとに配信される）。フィルタは Python 側で全イベントに対して呼ばれる（Rust 側に除外手段は無い。2,000 件を含む 2.2 s の操作で支障なし）: `.git/` 配下（`.git/HEAD` だけ通してブランチ切替を直接拾う案は MAY）、EditTransaction の一時名 `.lucianke-tmp-*`（これで `expect()` が対象にするのは最終パスのイベントだけになる）、`.gitignore`（`dulwich.ignore.IgnoreFilterManager`。S-8 で確認。1,000 パスで 50 ms 以下。ディレクトリは末尾 `/` を付けて渡す。fnmatch の自前サブセットは要らない）、`exclude_globs`。`awatch` を `wait_for` で包んで期限を付けてはならない（キャンセルで監視が終わる）。期限が要るときは下の手順 3 のようにキューを挟む。
2. **イベントの種別は信用しない**。種別は `added` / `modified` / `deleted` の 3 つだけで、改名は `added` + `deleted`、既存ファイルへの `os.replace`（EditTransaction の書き込み）では a.py が存在するのに `deleted a.py` も届く。旧版の「追加 + 削除 = 削除」の畳み込みは**誤り**（S-6 で確認）。種別は捨て、1 回の配信のイベントを **パスの集合** に畳む。
3. **バースト検知**: 1 回の窓のパス数を 500 と比べても成立しない（`debounce=50` の窓は最大約 170 件、`debounce=1000` でも 700 ファイルの `git checkout` が [1271, 152] に割れる）。窓が `WATCH_BURST_HINT`（100 件）以上のときだけ、`WATCH_BURST_QUIET_S`（0.3 秒）静まるまで続く窓を合算し、合算が `WATCH_BURST_FILES`（500）を超えたら個別処理をやめ、`sink.rescan(ws)`（§7.6.5 と同じ差分索引）に切り替える。小さい窓は待たず即処理するので、単発の遅延は増えない。型は `spikes/s06_watch/pattern.py`（700 ファイルで `rescan` 1 回・個別処理 0 回）。
4. 各パスについて `confine` → `stat` → 存在すれば `sha256`（スレッドプール）→ `files.sha256` と比較 → 変化があれば `sink.changed(path)`、不在なら `sink.removed(path)`。`sha256` が同じなら何もしない（エディタの保存で mtime だけ変わる場合を吸収）。**ディレクトリ**: 中身のあるディレクトリを外から移し入れる・改名する・移し出すとき、子のイベントは届かない（`added pkg` / `added new` + `deleted old` / `deleted pkg` だけ）。パスがディレクトリなら `os.walk` で子を列挙して取り込み、パスが不在なら `files.path` の前方一致 `パス + '/'` の行もすべて `removed` にする（`git checkout`・ブランチ切替で消えた・移された配下の取りこぼし防止。S-6 で確認）。
5. `IndexWriter` の取り込みが完了したら、`LspPool.notify_watched(changes)`（`workspace/didChangeWatchedFiles`）と、開いている文書には `documents.on_external_change(path)`（全文 `didChange`）を送る。`TreeCache` の該当エントリは新しい `sha256` で自然に置き換わる。LSP の設定ファイル（`servers.is_lsp_config`: 根の `pyrightconfig.json` / `pyproject.toml`、どの深さでも `tsconfig.json` / `jsconfig.json` / `tsconfig.<名前>.json`）は、索引に載らない（`.gitignore` の対象など）ときも、`IndexerSink` が覚えた前に知らせた `sha256` と違えば `notify_watched` する（前を知らない起動の後の最初の変更は知らせる。`rescan` の後は根の Python の設定も見て回る。BUG-0135）。
6. 監視が開始できない（存在しないパスは `FileNotFoundError` で表れるので `try/except OSError` で受ける。ネットワークドライブなど）ときは `mode="polling"` にし、`force_polling=True` は使わない（0.5 秒おきの書き換え 8 回のうち 3 回が 3 秒待っても届かなかった。S-6）。ネットワークドライブは環境が無く未検証。背景のポーリングは行わず、要求時に対象ファイルの `sha256` を照合する遅延検証（`ToolContext.resolve_target` と `EditTransaction` 段 3）に切り替える。T-00 `status` に `watcher: "polling"` を出す（compact の `watcher:` 行と `data.workspaces[].watcher`。監視が動いていれば `native`。複数のワークスペースで状態が割れるときの compact の行は `id=mode` を並べる）。`EditTransaction` 段 3 は、`polling` のときだけ前提検証の前に `watcher.drain(paths)`（`hunk_paths` / `touched_paths` / `expect` 対象）で対象のファイルを今のディスクに合わせる（`native` は従来どおり `drain()`。BUG-0096）。
7. NFR-PERF-3 の測定のため、イベント受信・DB コミット・LSP 通知の各時刻を `debug` ログに残す。
8. `drain(paths)` はデバウンス窓に溜まっているイベントのうち指定パス（省略時は全部）を即座に処理し、取り込みの完了まで待つ。`EditTransaction` が前提検証の直前に呼ぶ。`expect(path, sha256_after)` は、自分の書き込みのイベントを `sha256` が一致する間（`WATCH_EXPECT_TTL_S` まで）捨て、一致しない内容を見たら期待を消費してそのイベントを渡す（二重取り込みの抑止。1 回の書き込みは複数のイベントで届き〈`os.replace` は deleted と added〉、負荷の高いときは別々の窓に割れるので、1 回で消費すると 2 つ目が二重取り込みになる。BUG-0083）。**`expect` を呼ぶのは、`EditTransaction` がそのファイルを索引に取り込み終えたときだけ**: 取り込みが失敗した（または索引が無い）ファイルは `forget(path)` で期待を消し、こだまを捨てずに `sink.changed` で取り込み直させる（期待があるとディスクの内容は `sha256_after` のままなので、こだまが全部捨てられ、索引が編集前の内容のまま残る。BUG-0087）。

受入で確かめる点:

- 外部プロセスが 1 ファイルを書き換えたとき、`files.sha256` の更新と `didChangeWatchedFiles` の送信が `INDEX_REFLECT_MS`（500）以内に完了する（p95、100 回）。
- `.gitignore` に一致するファイルを作っても `files` に行が増えない。
- 501 ファイルを同時に変えたとき、ログに `rescan` への切替が 1 回記録され、個別取り込みが走らない。
- `os.replace` で既存ファイルを置き換えたとき（`deleted` と `added` が同時に届く）、`files` の行は消えず、`sha256` が更新される。
- 中身のあるディレクトリを外から移し入れたとき、配下のファイルがすべて `files` に入る。ディレクトリを移し出したとき、配下の行がすべて消える。

起動順: `awatch` を `create_task` してイベントループを 1〜2 回譲ってから差分索引（§7.6.5）を始めれば、起動時の隙間は生じない（開始通知は無いが、2 回譲った後に書いたファイルは 23 ms で届き、取りこぼしは無かった）。`stop.set()` から終了までは 1〜48 ms（S-6 で確認）。

---

## 7.4 Syntax Engine（syntax/）

構文木に基づく一切（解析・骨格・シンボル・境界・ガード・import 解決・構文パターン・非コード形式・未知言語の推定）を担う。LSP も DB も知らない。入力はバイト列と言語 ID、出力は `core` の型。

### 7.4.1 LanguageSupport / FormatAdapter（ADR-9）

```python
class LanguageSupport(Protocol):                 # Tier 1〜2 のコード
    language_id: str
    tier: Tier
    extensions: tuple[str, ...]
    def grammar(self) -> "tree_sitter.Language"   # Language(getattr(importlib.import_module(m), fn)())。lang_id → (モジュール, 関数名) の表で持つ（S-2）
    def queries(self) -> LanguageQueries          # tags / skeleton / imports / guards / doc の .scm（遅延コンパイル。Lucianke が自前で持つ。下記）
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
    def skeleton_record(self, source: bytes, opts: SkeletonOptions, *, rel: str, sha256: str,
                        estimate: Callable[[str], int] | None = None,
                        python_symbols: Callable[[bytes], Sequence[Any]] | None = None) -> Record   # T-02 の 1 ファイルぶん（file-block）。壊れたファイルは ValueError
    def symbols(self, source: bytes, *, rel: str, workspace_id: str) -> list[SymbolRecord]          # 索引に出すシンボル。壊れたファイル・持たない形式は空
    def select(self, source: bytes, selector: str) -> list[Match]        # selector は式の文字列（"jsonpath:$.a"）: xpath: / jsonpath: / jq: / toml: / yaml: / cell:
    def replace(self, source: bytes, selector: str, new: bytes) -> bytes  # M2

SUPPORT: dict[str, LanguageSupport]; ADAPTERS: dict[str, FormatAdapter]
def get_support(language_id) -> LanguageSupport | FormatAdapter | None
```

実装で確定（2026-10-29、a9e9818）: `selector` は `Selector` 型ではなく式の文字列（URN の `DataSel` なら `f"{sel.kind}:{sel.expr}"`）。`skeleton_record` は T-02 の入口で、`skeleton` が持てない `rel` と `sha256` を受ける（壊れたファイルは `ValueError` を投げ、呼び手が `E_UNSUPPORTED`〔`details.reason: "invalid_data_file"`〕にする。`E_SYNTAX_INVALID` は編集の検証用）。`symbols` は索引の入口で、**Tier 3 の索引は最上位の節だけをシンボルにする**: JSON / YAML / TOML は最上位のキー（`Key`）、XML は根と直下の子（`Tag`）。128 KiB（`MAX_SYMBOL_SOURCE_BYTES`）超、JSONL、根が配列、ipynb、壊れたファイルは `files` 行だけ（シンボルなし）。1 ファイル 500 件（`MAX_INDEX_SYMBOLS`）まで。URN は `lk://<ws>/<path>#jsonpath:$.service` / `#yaml:retry` / `#toml:project` / `#xpath:/testsuites/testsuite`（同じアダプタの `select` が解ける式）。

M1 で登録するもの:

| 区分 | 言語 | 根拠 |
|---|---|---|
| Tier 1 必須 | `python`（`.py`, `.pyi`）、`typescript` / `tsx`、`javascript` | 仕様書 OD-7。受入フィクスチャはこの 2 系統 |
| Tier 1 best effort | `rust`, `go`, `c`, `cpp`, `c_sharp` | PyPI に wheel がある（付録 A）。LSP は見つかれば使う |
| Tier 1 保留 | `kotlin`, `swift` | tree-sitter の PyPI 配布は S-2 の確認対象外で未確認。M1 では未登録（Tier 4 扱い）のまま。構文パターン検索だけなら ast-grep が文法を内蔵している（S-3 で確認） |
| Tier 2〜3 | `ipynb`, `json`, `jsonl`, `yaml`, `toml`, `xml`（`.svg` 含む）, `markdown`, `rst`, `dot`, `mermaid` | §8.6 の M1 範囲（B2 読み取り、B3 出力、B4 骨格・部分取得） |

`typescript` / `cpp` / `json` / `xml` / `toml` の文法パッケージは最終公開が 2024 年で止まっている（付録 A）。S-2 の結果（合格。[`docs/spikes/S-02.md`](spikes/S-02.md)）:

- 登録表の 18 言語（`python` / `typescript` / `tsx` / `javascript` / `json` / `yaml` / `toml` / `xml` / `dtd` / `markdown` / `markdown_inline` / `rust` / `go` / `c` / `cpp` / `c_sharp` / `rst` / `sql`）はすべて読み込み・解析・クエリができ、ABI は 14 または 15 で、`tree-sitter` 0.26 の範囲（13〜15）に収まる。**登録表から外す文法は無い**。ABI 14 は 9 言語（上の 5 パッケージ + `yaml`・`rst`）で、ABI 15 版が出ていない。
- `typescript` / `tsx`、`xml` / `dtd`、`markdown` / `markdown_inline` は 1 パッケージ 2 言語なので、`lang_id → (モジュール, 関数名)` の表で持つ（例: `("tree_sitter_typescript", "language_tsx")`）。
- 起動時検査は `MIN_COMPATIBLE_LANGUAGE_VERSION <= lang.abi_version <= LANGUAGE_VERSION` で足りる。将来の更新で読めなくなった文法は、従来どおり登録表から外して Tier 4 に落とし、失敗をログと T-00 `status` に出す。ABI 14 の文法は `Language.name` と `semantic_version` が `None` なので使わない。
- `tree-sitter>=0.26,<0.27` に固定し、ロック更新時に S-2 の `run_grammars.py` を回す（CI の煙試験にする）。
- **パッケージ同梱の `TAGS_QUERY` は当てにしない**（`typescript` は TS 固有構文のみで JS の `TAGS_QUERY` との連結が要る、`c_sharp` は `None`、データ形式・文書の 9 言語には無い）。`.scm` は Lucianke が自前で持つ（`syntax/queries/`）。タグは `QueryCursor.matches()` で取る（`captures()` は `@name` と `@definition.*` の対応が失われる）。`parse(src, None)` と `captures(node, None)` は `TypeError` になるので、`old_tree` と述語は省略する。

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

1. `Parser` はスレッド安全でないため、スレッドローカルに言語ごとの `Parser` を持つ（共有しても壊れなかったのは GIL による直列化のおかげで、当てにしない）。`Language` / `Query` / `Tree` / `Node` はスレッド間で共有してよく、`QueryCursor` は呼び出しごとに作る。解析は常にプールのスレッドで行い（ループ上で行わない。MUST）、`parse(bytes)` ではなく **`parse_with_deadline`**（`read_callback` で 16 KiB 刻み。期限〔`STRUCTURAL_SEARCH_MS` 相当〕後に `b""` を返して打ち切り、`timed_out` なら木は捨てる）を使う。理由は ADR-13: `parse(bytes)` は解析の全体にわたって GIL を握り（スレッドプールは並列化せず、2,000 行で約 8 ms、5 MB で約 750 ms ループが止まる）、`read_callback` 版は速度が同等で他スレッドの停止が最長約 20 ms に縮む。`progress_callback` は使わない（Windows でアクセス違反）。例外を投げて打ち切る方式も使わない（不安定）。並列度が要るなら別プロセス（S-2 で確認）。
2. キーは `(workspace_id, rel, sha256)`。同じキーの解析が進行中なら 2 本目は待つ（二重解析の抑止）。
3. 上限は `TREE_CACHE_MAX_FILES`（2,000）と `TREE_CACHE_MAX_BYTES`（512 MiB）の両方。LRU で古いものから落とす。**構文木はソースの約 20〜26 倍のメモリを食う**ので、`TREE_CACHE_MAX_BYTES` で数えるのはソースのバイト数ではなく推定木メモリ（ソース長 × 25。係数は `core/limits.py` に置く）とする（ソースで数えると実メモリは 10 GiB 超になる。S-2 で確認）。
4. `MAX_FILE_BYTES`（5 MB）を超えるファイルは解析しない。Tier 3 ならアダプタのストリーミング骨格、それ以外は `files` 行だけ（`lexical_fallback`）。
5. 構文エラーがあっても木は保持する（`has_errors`）。`error_ranges()`（実体は `error_spans`: `is_error` / `is_missing` を、ERROR の中へは降りずに列挙する）は Indexer が `source: "tree-sitter"` の診断にする。Python は構文が壊れると位置の粗い 1 個の大きな ERROR になるため、位置は参考にとどめる（§7.7.4。S-2 で確認）。
6. 増分解析（`tree.copy()` → `tree.edit(...)` → 再解析）は MAY。M1 は全解析（約 8 ms）で足りる（増分は約 1.85 ms で全解析と同一結果。`edit` は呼んだ木を直接書き換えるので `copy()` してから）。`Node.text` は Tree がソースを保持するので取れるが、`line_index` のため `ParsedFile.source` は残す（S-2 で確認）。

受入で確かめる点:

- 同じ `sha256` のファイルを 2 回要求したとき、2 回目は解析関数が呼ばれない（カウンタで検査）。
- 上限を超えたとき、キャッシュのファイル数とバイト数が上限以下に戻る。

### 7.4.3 シンボル抽出（symbols.py）と URN の付与

```python
def extract(parsed: ParsedFile, support: LanguageSupport, ws: Workspace, rel: RelPath) -> list[SymbolRecord]
```

1. `tags.scm` の捕捉（`@definition.<kind>` と `@name`）を走査し、定義ノードごとに `Kind` を決める。
2. 修飾名は包含する定義（クラス・名前空間・`impl`・ネストした関数）を外側から `.` で連ねる。モジュール名は含めない（URN の `path` が担う。仕様書 §7.2）。ラムダ・無名関数はシンボルにしない。 TS の型リテラルのメンバー（`type KyOptions = { retry?: … }`）は、型の別名の値・メンバーの型注釈（インターフェース・型リテラルのプロパティ・クラスのフィールド・getter）から括弧・合併・交差・メンバーを持ち越す標準の型（`Partial<{…}>` など）の第 1 型引数だけを通って着く型リテラルのものを `Property` / `Method`（修飾子 `pub`、修飾名 `KyOptions.retry`・入れ子は `KyOptions.inline.before`）にする（`_Ctx._type_member_position`。BUG-0114、2026-10-31。理由: インターフェースのメンバーと同じく T-02〜T-06 の対象にする。関数型の引数・配列の要素などの型リテラルは持ち主の型のメンバーではない）。合併の別々の型リテラルの同名のメンバーは衝突の規則で最後が素のURN になるが、再定義の警告は出さない。素の名前の解決（`syntax/relations._resolve_bare`）は、TS の型の別名とメンバーの中の型リテラルのメンバーを素の名前の範囲にしない（`readonly stop: typeof stop` の `stop` は import した値）。`const x = { … } as const` のオブジェクトリテラルのメンバーはシンボルにしない（型の分かる受け手から結ぶには変数の型の仕組みが別に要る。名前の一致の候補が増えるだけになる）。
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
3. **予算内での縮退**: T-02 手順 8（`doc:` 行 → `urn:` 行 → 非公開 → 入れ子の順に 1 件ずつ落とし、`- truncated: N members omitted (…): <入れ物> <件数>, …; whole file: max_tokens>=M …`）を実現するため、ファイルブロックのレコードは `shrink(remaining_tokens) -> Record` と `shrink_json` を持つ。段 t（1 件ずつ落とした数）の大きさは、行と JSON の部品（シンボルごとに覚えた行・JSON の `units`。`core.tokens.TokenEstimator.units` は連結しても足し算で求まる）の和で数え、段を二分探索する（調べる位置は範囲と各位置の判定だけで決まるので、結果は予算について単調。BUG-0078）。縮退版の `data`（json）は読まれたときに作る（compact の応答では作らない）。データファイル・Markdown・Tier 4 のアウトライン（`syntax/adapters/_common.build_record`）も同じ作りで、段は表示しているノードを `_bfs_rank`（浅い順 → 文書順の連番。親は子より小さい）の逆に 1 件ずつ落とした数 t（最上位は残す。最後に最上位だけの版から注を縮める 2 段を足し、下限を以前の大きさに保つ）、`_prune` が先頭 `V − t` 件の木を作って落とした子孫の数を親の `omitted` に足し、入れ物ごとの件数を `truncated_by_container` に書く。compact は行の本文、json は `data` の JSON で大きさを数えて二分探索する（BUG-0084。`Record.shrink_json` は既定の推定器のときだけ）。パッカーは「入らないレコード」に `shrink` があればそれを呼んでページの最後の 1 件にし（後ろは次ページ。予算に対する単調性のため。仕様書 §5.6-5、BUG-0067）、縮めても入らなければ次ページへ回す。`Record` に任意フィールド `shrink` を足す必要がある（親の §7.2 への突き合わせ点）。
4. Tier 3 の骨格はアダプタの `skeleton()` が同じ `Record` を返す。
5. 親子は行の範囲の包含で決める（`_build_tree`）。同じ行の範囲は兄弟だが、修飾名が親の修飾名 + `.` で始まるものは子にする（1 行の型リテラルの別名 `type U = { a: string }` の `U.a`。最上位に並べると縮退で落とせない。BUG-0114、2026-10-31）。

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

1. Python: 絶対 import は `python_import_roots(from_rel, exists, pytest_imports)` の根の順、相対 import は当該ファイルからの相対で、`<mod>.py` / `<mod>/__init__.py` / `<mod>.pyi` の順に探す。根（BUG-0082、2026-10-31）: いちばん内側のプロジェクト（ファイルを含むテストフィクスチャのルート〔`workspace.languages.fixture_roots`〕、無ければワークスペースのルート）について、(a) テストのパスなら pytest の prepend が `sys.path` に足すディレクトリ（`__init__.py` の無い最初の祖先＝`resolve_package_path` の親、続いて祖先の `conftest.py` の同じもの。近いものから。`--import-mode=importlib` なら無し）、(b) そのプロジェクトの pytest の設定の `pythonpath`（`workspace.pyenv.detect_pytest_imports`。pytest 9 の `CONFIG_LOADERS_BY_NAME` の順で最初の設定ファイル）、(c) プロジェクトと `<プロジェクト>/src`。外側のプロジェクトは (b)(c) を後ろに足す。(a)(b) は `ImportRoot(stdlib=False)` で、`sys.stdlib_module_names` と `pytest`・`_pytest`・`pluggy` はそこで解かない（pytest が根を足す時点で標準ライブラリの多くは `sys.modules` にある。実測: pytest 9.2 で最上位 301 個中 159 個が読み込み済み）。索引器は使った設定の指紋を語幹 `?pytest:<プロジェクト>#<指紋>` として `_import_probes` に書き、設定ファイルの取り込み・削除・差分走査で指紋が変わったファイルだけを解き直す（§7.6.2）。`exists` は `make_resolver` の中で覚えて使い回す。理由: テストの土台やフィクスチャのクラスの参照・呼び出し元が外部に結ばれて 0 件に見えると、それを変える AI が影響を見落とす。**名前空間パッケージ**（BUG-0101、2026-10-31）: どの根にもファイル（`<mod>.py`・`<mod>/__init__.py`・`.pyi`）が無く、根の順に `<根>/<mod>` が `__init__.py(i)` を持たないディレクトリなら、ワークスペースの名前空間パッケージ（`ImportResolver.namespace_dir`。標準ライブラリの名前は除く）。それを束ねた名前（`import nsp.tools.fmt` の `nsp`・`import nsp.tools as nt`・`from nsp import tools`。`namespace_name_map`）とそのディレクトリ（`nsp.tools`。`dotted_module_path` が `NAMESPACE_PACKAGE` と答える）の読みは**参照を作らず外部にもしない**。その下のファイル（`nt.fmt.render()`）はワークスペースのモジュール、ワークスペースに無い先（`nsp.ext`。別の場所にある部分かもしれない）は今までどおり外部。`import nsp.tools.fmt` の import 文の行は import したモジュールのファイルへの `IMPORTS`（先頭の `nsp` にはファイルが無い）。調べたディレクトリは語幹 `?dir:<ディレクトリ>` として `_import_probes` に書き、ファイルが作られた・消えたら祖先のディレクトリの `?dir:` でも引く（§7.6.2）。理由: ワークスペースのディレクトリを確かな（`structural`）外部パッケージ `lk://_ext/nsp` への参照にすると、外部の依存があるように見える誤った答えになる。ディレクトリにはシンボルもファイルも無く T-06 の対象にならないので、参照の行を作っても使われない。
2. TypeScript / JavaScript: 相対パスに `.ts/.tsx/.js/.jsx/.mjs` と `/index.*` を補う。`tsconfig.json` の `paths` は M2（MAY）。
3. 解決できたら `relations(IMPORTS, structural)`。解決できなければ外部として `lk://_ext/<パッケージ>#<名前>` を `target_urn` にする（決定 2026-10-29。T-06 / T-07 が外部ノードを要るため。現状の `engine/indexer.py` は捨てている → §7.6.2 の「実装との差」）。
4. 呼び出し関係の `structural` 行は、呼び出し式の識別子を「同一ファイルの定義 → import で取り込んだ名前 → 解決先ファイルの公開シンボル」の順で引いて作る。同名の別シンボルを区別しないため `confidence_level = structural`。

### 7.4.7 構文パターン（structural.py）

```python
def search(language_id: str, pattern: str, files: Sequence[RelPath], constraints: Mapping[str, str] | None, max_matches: int, deadline: Deadline) -> list[Match]
def rewrite(match: Match, template: str) -> str
```

`ast-grep-py` 0.51 を用いる。パターンと `rewrite` を対象言語で解析できなければ `E_SYNTAX_INVALID`（`details.param`）。照合はスレッドプールで走らせ、`STRUCTURAL_SEARCH_MS`（5,000）を超えたら `E_TIMEOUT` に部分結果。結果は T-12 が `edit/plan.py` の `Plan` に変換する（書き込みは `EditTransaction`）。S-3 の結果（合格。[`docs/spikes/S-03.md`](spikes/S-03.md)）:

- **書き換え**: `node.replace(new_text) -> Edit` と `root.commit_edits([Edit…])` はあるが、**`replace` はメタ変数を展開せず**文字列をそのまま挿入する。よって `rewrite` のテンプレート展開は自前で行う（`$X` は `get_match` のテキスト、`$$$X` は先頭ノードの開始〜末尾ノードの終了のソース断片で展開し、区切りと空白を保つ。複数行はインデントを挿入先に合わせる。パターンに無い変数の参照は `TemplateError`）。展開済みの文字列を `replace` に渡し、適用は `commit_edits` か `Match` の `start_index` / `end_index` による文字列連結で行う。
- **入れ子の一致**は外側と内側の両方が返る（`foo(foo(1))` に `foo($X)`）。重なる hunk を作らないため、`outermost` を **必ず** 通してから hunk にする（MUST）。
- **言語表**: `lang_id → ast-grep の言語名` の登録表で持つ。**非対応は `toml` / `xml` / `dtd` / `rst` / `sql` / `markdown_inline`**（`E_UNSUPPORTED`）。`c_sharp` は ast-grep では `csharp`。未対応名で `SgRoot` を作ると `PanicException`（`BaseException` の子で `Exception` ではない）と stderr 出力が出るので、**登録表で事前に弾く**。
- **不正パターン**は例外にならず 0 件を返す。例外（`RuntimeError: cannot get matcher`）になるのは、パターンが単独の AST ルートにならないとき（→ `E_SYNTAX_INVALID`）。一致 0 件のときだけ Tree-sitter でパターンを解析し、ERROR なら応答の `warnings` に「パターンが解析できない可能性」を添える（`E_SYNTAX_INVALID` にはしない。S-3 の報告では `details.hint`）（ERROR だけで断定すると、Rust・Java・C# の正しいパターンを誤判定する）。Go・C の式パターンは `{"context": …, "selector": …}` 形式で包む必要がある（`LanguageSupport` に「パターン用の包み」を持たせる案。M2 で T-12 の対象言語を決めるときに確定）。
- **位置**: ast-grep の行・列は 0 始まりのコードポイントで、Lucianke の内部表現と同じ（変換は要らない。ADR-14）。入力は `str` なので、UTF-8 として読めないファイルの書き換えは拒否する。
- **予算**: ast-grep は `TreeCache` の木を使えず自前で再解析する（約 10 ms / 2,000 行）ので、5 秒で約 400 ファイル。GIL を解放しないのでスレッドプールでは速くならない（並列化するなら別プロセス）。ファイル数が多い検索には、パターン中のリテラルを `bytes.find` で調べてから渡す前ふるいが効くはず（未測定の推奨）。

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
@dataclass(frozen=True, slots=True, eq=False)
class LspServerDef:
    language_id: str                        # workspace.languages の言語 ID。TypeScript 系は typescript / tsx / javascript ごとに 1 件（実装で確定 2026-10-29）
    name: str                               # "basedpyright" / "ruff" / "typescript-language-server" / "vtsls" …
    argv_candidates: tuple[tuple[str, ...], ...]   # 起動コマンドの候補 (実行ファイル名, 引数...)。先に見つかったものを使う
    initialization_options: Mapping[str, Any] | None = None   # initialize の initializationOptions
    settings: Mapping[str, Any] = {}        # workspace/configuration の応答の元になる設定木（"." 区切りのセクション名で引く）
    role: Literal["types", "lint"] = "types"   # 同一言語に 2 役を並列してよい（仕様書 §12.3）
    lsp_language_id: str | None = None      # didOpen の languageId（tsx は "typescriptreact"）
    best_effort: bool = False               # 実機未確認のサーバー。PATH に無ければ unavailable で済ませる
    locale: str | None = None               # initialize の locale。basedpyright は "en"（診断を英語に固定。既定は OS のロケール＝日本語）
    requests_m1: frozenset[str] = frozenset()   # ADR-15 の範囲でこのサーバーに送ってよい LSP の method
    tsserver_via: Literal["initialization_options", "settings"] | None = None   # tsserver の所在の渡し方（S-5）
    needs_workspace_tsserver: bool = False  # tsserver.js が無いと initialize が失敗する（tsls）
    # position_encoding_default は持たない（S-11: 応答に positionEncoding が無ければ全部 UTF-16 で足りる）
```

初版の想定（`language_ids` / `discover` / `args` / `init_options: Callable` / `config_for: Callable`）から、実装で上のとおりに変えた（実装で確定 2026-10-29）。探索の候補は `argv_candidates`、`initialize` の `initializationOptions` は静的な `initialization_options` に、実行時にしか決まらない値（Python のインタープリタ、tsserver の所在）は `build_initialization_options(defn, tsserver_path=…)` と `build_settings(defn, python_path=…, tsserver_path=…)` が重ねる。`config_for(ws, section)` に当たるのは、`build_settings` の結果を `LspClient.set_settings` に渡しておくと `LspClient` が `workspace/configuration` の section を引いて返す仕組み。

M1 の定義（Python と TypeScript / JavaScript の起動コマンド・初期化・位置単位は S-4 / S-5 / S-11 で実機確認して固定した。ほかの言語は想定のまま）:

| 言語 | 既定 | 代替 | 備考 |
|---|---|---|---|
| Python（types） | `basedpyright-langserver --stdio`（起動口は `<.venv>/Scripts/basedpyright-langserver.exe`（POSIX は `bin/`）。PyPI の `basedpyright` が `nodejs-wheel-binaries` を同梱するので Node の別途導入は不要。PATH を System32 だけにしても起動する。S-4 で確認） | `pyright`（`nodeenv` 依存で Node を取りに行く） | インタープリタは **`workspace/configuration` の `python` セクション**（`{"pythonPath": …}`）への応答だけで渡す。`initializationOptions` の `pythonPath` は効かない（S-4 で確認）。**設定の根**: 根に `pyrightconfig.json` が無く `pyproject.toml` があるときは、その `pyproject.toml` を `analysis.configFilePath`（`basedpyright` と `python` の両方の節。basedpyright は `basedpyright.analysis` があれば `python.analysis` を読まない）で明示する（`servers.pinned_pyright_config`。起動のときと、根の設定ファイルの変更を監視が拾ったときに引く）。明示しないと、節の無い `pyproject.toml` は無いものとして親のディレクトリの設定を探して採る（basedpyright 1.40.2 の `_getExtendedConfigurations`。明示したときは探さない）。根に `pyrightconfig.json` があれば明示しない。どちらも無ければサーバーは親を探さない（言語サーバーとして起動したときは根しか見ない）。仕様書 §12.3（実装で確定 2026-10-31、BUG-0132）。**根の設定の変更**（BUG-0135）: basedpyright は起動の後の根の設定の変更を `workspace/didChangeWatchedFiles` で読み直す（`include` を狭めた・`pyproject.toml` に節を足した。実機で確認）。ただし `configFilePath` で `pyproject.toml` を明示している間は根に作った `pyrightconfig.json` を読まず、明示しないまま根の `pyrightconfig.json` を消すと親の設定を採る。`LspPool.notify_watched` は根の設定ファイルの変更を見たら、送る前に明示の判断（`LspHandle.pinned_config`）と解析の対象（`LspHandle.analysis_scope`）を読み直し（`_reload_pyright_config`。読むのはスレッドで、読んでいる間に起動し直したハンドルは触らない）、明示が変わったら `LspClient.set_settings` で設定を差し替えて `workspace/didChangeConfiguration`（`settings: null`。サーバーは `workspace/configuration` を引き直して根の設定を読み直す）を送る。そのハンドルには設定ファイルの変更を `didChangeWatchedFiles` に入れない（古い明示のまま一度読み直させない）。起動中（`starting`）のハンドルは覚えを直すだけ（`initialize` の前に通知を送らない）。ruff（`lint`）には送らない。`locale: "en"` を渡す。`positionEncoding` は応答に無い → UTF-16（S-11）。`initialize` 応答まで 377 ms、`didChange` から診断まで約 0.3 s |
| Python（lint） | `ruff server` | — | **QuickFix / `source.organizeImports` / `source.fixAll` の供給元は ruff**（basedpyright の `codeAction` は `# pyright: ignore` の追加だけで、`source.organizeImports` は返さない。S-4 で確認）。起動 12 ms、`positionEncoding: "utf-8"` を返す、診断は `didChange` ごとに版つきで届く（0.5 ms） |
| TypeScript / JavaScript | `typescript-language-server --stdio`（tsls） | `vtsls --stdio` | **既定は tsls**（S-5 で確認: 診断が常に最新で空配列も届く、診断が約 110 ms 速い、`definition` の範囲が名前になる）。vtsls は TypeScript を同梱するので、tsserver が見つからず tsls の `initialize` が失敗するとき（ワークスペースに TypeScript が無い、または npm の `typescript@7` のように `lib/tsserver.js` が無い）の代替にする。どちらも Node が要る。`positionEncoding` は応答に無い → UTF-16（S-11） |
| Rust / Go / C,C++ / C# / Kotlin / Swift | `rust-analyzer` / `gopls` / `clangd` / `csharp-ls` / `kotlin-language-server` / `sourcekit-lsp` | — | best effort。PATH に無ければ `unavailable` |

`initialize` では `positionEncodings: ["utf-8", "utf-16"]` を提示し、応答の `positionEncoding` を `LspClient` に設定する。応答に無い（交渉できない）サーバーは UTF-16（仕様書 §12.2）。未知の値も UTF-16 に倒して警告ログに出す（実装は `Unit` の値〔`utf-8` / `utf-16` / `utf-32`〕を受け、それ以外を倒す。`utf-32` は提示していないので来ない。S-11 の推奨は `utf-32` も倒す案だったが、提示しない以上どちらでも実害は無い）。**サーバーごとの既定値は持たない**: 旧版（と S-4 の提案）の `position_encoding_default` は削除した。実測した 4 サーバーのうち、basedpyright・vtsls・tsls は応答に無く UTF-16 で、`utf-8` を採るのは ruff だけ。上書きを持つと誤った値が黙って位置を壊す穴になる（S-11 で確認）。サーバーの更新で単位が変わったら落ちるよう、期待する単位を契約テスト（サーバー未導入なら skip）に持たせる。`utf-8` の経路は将来のサーバー（rust-analyzer・clangd など）でも通るので ruff で常時検査する。提示を `["utf-16"]` に絞る設定（`lsp.position_encodings`）を逃げ道に用意する（MAY。**未実装**: 今は `lsp/client.py` の `default_client_capabilities()` に固定。期待する単位の契約テスト `tests/lsp_contract` も未作成）。

**探索順と tsserver の所在**（S-4・S-5 で確認）:

- サーバー: `LUCIANKE_LSP_<サーバー名>_CMD` → `LUCIANKE_LSP_<LANG>_CMD`（lint は `…_LINT_CMD`）→ ワークスペースの仮想環境（`<root>/.venv/`、`pyvenv.cfg` のある `venv/` / `env/` / `.env/`。§7.3.4 と同じ候補・`workspace_venv_dirs`）の `Scripts|bin/` → `<root>/node_modules/.bin/` → PATH。探索は自前で行い `shutil.which` は使わない（Windows の `which` はカレントディレクトリを先に見るので、ワークスペース直下に置かれた実行ファイルを掴みかねない。`.exe` / `.cmd` / `.bat` を探し、`create_subprocess_exec` にそのまま渡す）。呼び手が Python 環境〔`venv`〕を渡したときは、その `Scripts|bin` を `.venv` より先に見る。環境変数の値はそのまま argv になる（存在確認はしない。起動に失敗すれば `init_failed`）。TypeScript は tsls → vtsls の順。
- tsserver: 明示指定 → `<root>/node_modules/typescript/lib/tsserver.js`（存在確認。TypeScript 7 には無い）→ 無ければ vtsls に切り替える。tsls へは `initializationOptions.tsserver.path`、vtsls へは `workspace/configuration` の応答（`{"vtsls": {"autoUseWorkspaceTsdk": true}}`。返さないと同梱の TypeScript を使う）で渡す。

**サーバーからの要求への返答**（`on_request`。S-4・S-5 で確認）: `workspace/configuration`（basedpyright は section `python` と `basedpyright`、vtsls は section `""`、tsls は `formattingOptions`。MUST。`LspClient.set_settings` に渡した設定木から返す）、`window/workDoneProgress/create`・`client/registerCapability`・`client/unregisterCapability`（`None`）、`workspace/applyEdit`（M1 では適用しない: `{"applied": false}`。書くのは `EditTransaction` だけ。tsls の organizeImports は `only: source.removeUnusedImports.ts` で `edit` つきが返るのでそれを使い、`_typescript.organizeImports` 経由の `applyEdit` は使わない）、未知のメソッドは `-32601`。

**サーバーごとの癖**（実測。`servers.py` に集約する）:

| 事項 | basedpyright | ruff | vtsls | tsls |
|---|---|---|---|---|
| `rename` の `WorkspaceEdit` | `documentChanges`（`version: null`） | — | `changes` | `changes` |
| `codeAction` の `edit` | `changes`（`# pyright: ignore` のみ） | `documentChanges` | `changes` | `documentChanges` |
| organizeImports | `workspace/executeCommand basedpyright.organizeimports`（未広告）だけ | `only: source.organizeImports` → `codeAction/resolve` で `edit` | `only: source.organizeImports` → `resolve` で `edit` | `only: source.removeUnusedImports.ts`（kind に `.ts` 接尾辞） |
| `definition` の範囲 | 識別子 | — | **宣言全体**（名前の範囲は hover / documentSymbol で取る） | 識別子 |
| `publishDiagnostics.version` | あり | あり | **無し** | **無し** |
| 診断の更新 | 連続した変更は最後の版だけ | 版ごと | 誤りを消しても古い診断が届くことがある | 常に最新 |
| 受信 URI | 小文字ドライブ＋`%3A` | 送った形のまま | 小文字ドライブ＋`%3A` | 同左 |
| `didOpen` 直後の要求 | 約 0.6 s 待たされて正しい結果（`null` は返さない） | — | — | — |

`requests_m1`: basedpyright に `definition / references / hover / prepareRename / rename / callHierarchy`、ruff に `codeAction / codeAction/resolve`、vtsls と tsls に `definition / references / hover / prepareRename / rename / callHierarchy / codeAction`（vtsls の organizeImports は `codeAction/resolve` も）。`rename` は `changes` に頼らず両形を受ける（§7.5.6）。

### 7.5.2 状態機械（pool.py）

```python
class LspPool:   # 言語 × ワークスペース × 役（types / lint）ごとに 1 つの LspHandle を持つ
    def __init__(self, registry: ProcessRegistry, *, on_diagnostics: OnDiagnostics | None = None, …)
        # OnDiagnostics = Callable[[RelPath, list[dict], int | None, str, Workspace], Any]  → on_diagnostics(path, diags, version, source, ws)
        # 4 引数目の source はサーバー名（"basedpyright" / "ruff" …）。§7.5.5 の (file_id, source) 単位の全置換に要る
        # 5 引数目の ws はそのハンドルのワークスペース（複数ルートでも診断を送り元の DB へ書ける。fccee0a。5 引数を受けない呼び手は RuntimeHub.on_diagnostics を直す）
    def get(self, language_id, ws, *, role="types", start_if_needed=True) -> LspHandle | None   # ready のときだけ返す。ブロックしない
    def status(self, language_id, ws, role="types") -> PoolStatus       # (state, progress, server_name, pid, reason)
    def handles(self, ws=None, language_id=None) -> list[LspHandle]
    async def ensure_started(self, language_id, ws) -> None            # standby なら起動を投げて即戻る（待たない。§8.3）
    async def wait_ready(self, language_id, ws, timeout_ms, *, role="types") -> bool
    async def notify_watched(self, ws, changes: Iterable[tuple[RelPath, int]]) -> float
        # workspace/didChangeWatchedFiles を稼働中（indexing / ready）の全 LSP へ。送信前の perf_counter を返す（DiagnosticsHub.wait_for の sent_at にそのまま渡せる）
        # 根の pyrightconfig.json / pyproject.toml の変更なら、先に Python の型解決のハンドルの analysis_scope と pinned_config を読み直し、明示が変わったら
        # 設定を差し替えて workspace/didChangeConfiguration を送る（そのハンドルには設定ファイルを didChangeWatchedFiles に入れない。§7.5.1。BUG-0135）
    def router(self, ws) -> DocumentRouter                             # ws の DocumentRouter（同じ workspace_id なら同じもの。§7.5.4。8a6f40f）
    async def rebind(self, ws) -> list[LspHandle]                      # 設定変更（set_python_env・set_roots …）を既存のハンドルに取り込み、再起動したハンドルを返す。ハンドルが無ければ何もしない
    async def reset(self, language_id: str | None, ws) -> None         # reindex_scope: "lsp"。停止の回収を待つので async
    async def shutdown_all(self, grace_ms: int | None = None) -> None  # 終了手順（§6.5）: shutdown → exit → terminate（Windows は Job Object で孫ごと）。以後は起動しない
    async def sweep_idle(self, now=None, idle_s=LSP_IDLE_SHUTDOWN_S) -> list[LspHandle]   # MAY: 30 分無要求の稼働中を止めて standby へ

class LspHandle:   # 状態の読み取りと、機能呼び出しが使う client / documents / diagnostics
    started_at: str | None                  # 直近に起動を始めた時刻（UTC・ISO 8601・ミリ秒）。再起動のたびに更新。T-00 の lsp_daemons[].started_at
    async def rebind(self, ws) -> bool      # 新しい Workspace に差し替える（documents / diagnostics の ws も）。再起動したら True
    def record_ok(self) -> None             # 要求が時間内に答えた: 連続タイムアウトの数え直し
    def record_timeout(self) -> bool        # 連続 3 回に達したらプロセスを落として unavailable（timeout_3x）にし True を返す
    def record_broken(self, method, detail) -> LspState | None   # その method に以後答えられない壊れ方（LspServerDef.restart_on_errors）: その場で starting（restarting）にして起動し直す。上限なら unavailable（broken_3x）。BUG-0090
```

| 状態 | 入る契機 | 動作 | 出る契機 |
|---|---|---|---|
| `standby` | 初期状態。30 分無要求（MAY） | 何もしない | 当該言語への最初の要求 → `starting`。索引の走査の列挙が `EAGER_LSP_MIN_FILES`（200）以上なら、型サーバーのある言語のうちファイルの最も多い 1 言語の `types` の役は列挙の直後に `starting`（`tools/_runtime.warm_type_server`。`Indexer.on_listed` が列挙を渡す。BUG-0127） |
| `starting` | 要求 | コマンド探索 → `ProcessRegistry.spawn` → `initialize` / `initialized` を送る。要求元には降格応答（待たない） | `initialize` 成功 → `indexing`。失敗 → 再試行判定 |
| `indexing` | 初期化直後 | `$/progress` / `window/workDoneProgress` を `progress` に反映。取れないサーバーは最初の `publishDiagnostics` 受信で `ready`（basedpyright は単一文書では `$/progress` を出さない。`ready` は「`initialize` 応答後、最初に開いた文書の実診断を受信」または「`$/progress end`」の早い方。S-4 で確認） | 進捗完了 → `ready` |
| `ready` | 索引完了 | 要求を通す | クラッシュ / 無応答（連続 3 回のタイムアウト）→ 再試行判定。壊れ方の印（`record_broken`）→ `starting`（`reason: restarting`）。`reset` → `standby` |
| `unavailable` | 再試行上限 | `status.reason` に理由（`not_found` / `crashed_3x` / `init_failed` / `timeout_3x` / `broken_3x`）。T-00 `status` に出す | `reset` → `standby` |

再試行は `LSP_RESTART_BACKOFF_S = (1, 2, 4)`、`LSP_RESTART_MAX = 3`。起動後 `stable_after_s`（60 s）以上動いていたクラッシュは数え直す。コマンドが見つからなければバックオフせず即 `unavailable`（`not_found`）。`EngineSelector` は `status()` だけを見る。

実装との差（実装で確定 2026-10-29）:

- 状態遷移は、ハンドルごとの監督タスク 1 本が行う。`standby` からの遷移は `await` なしの同期で 1 回だけなので、言語ごとのロックは要らず二重起動しない。
- `indexing` → `ready` の合図は次の 3 つの早いもの: (1) `$/progress` の `begin` を見たサーバーは**すべての `end` が揃ったとき**、(2) 進捗中でなければ**最初の `publishDiagnostics`**、(3) どちらも来なければ `ready_fallback_s`（15 s）後（進捗が続いていても `indexing_max_s`〔300 s〕で打ち切り）。
- **要求のタイムアウトはプールから見えない**。機能呼び出し側（`lsp/features.py`）が、時間内に答えたら `LspHandle.record_ok()`、`TimeoutError` なら `record_timeout()` を呼ぶ（連続 3 回で `timeout_3x`。降格は呼び手が `engine.selector.on_lsp_timeout` で行う）。例外: 1 問い合わせの予算つきの exact の要求（T-07 の根の要求と T-06 の参照検索）は予算の残りを期限にし（`prepare_call_hierarchy` / `incoming_calls` / `outgoing_calls` / `references` の `timeout_ms`。T-06 は `_lsp_exact.ExactBudget`）、その残りが要求 1 つの期限より短かったときの時間切れは数えない（`strike=False`。予算が尽きただけで無応答ではない）。予算の内に遅れて答えた要求は `record_ok`（BUG-0091。仕様書 §12.3）。
- `reset` は停止の回収を待つので `async`。`unavailable` も解く（次の要求で再起動）。`shutdown_all` は 1 つだけ（初版の本書は 2 つ書いていた）。
- `LspHandle.documents`（`DocumentSync`）と `.diagnostics`（`DiagnosticsHub`）はハンドルと同じ寿命で、再起動のたびに中身だけ空になる（呼び手が参照を持ち続けてよい）。外へ出す `LspStatus` では `starting` を `indexing` に含める。
- **`rebind`（8a6f40f・c84b7a0、BUG-0023）**: 設定が変わったとき（`set_python_env`・`set_roots`・除外・`default_base_ref`）、`LspPool.rebind(ws)` が `workspace_id` の同じ既存のハンドルすべてに `LspHandle.rebind(ws)` を呼ぶ。`ws` と `documents.ws` / `diagnostics.ws`（URI の変換に使う）を差し替え、`on_diagnostics` の 5 引数目も新しい `ws` になる。**再起動するのは、ルートが変わったとき（言語を問わない）と、Python 系でインタープリタが変わったとき**だけ（basedpyright はインタープリタを `workspace/configuration` の応答でしか受け取らず、起動時の値で環境を作る〔§7.5.1〕ため、`set_python_env` の後は `started_at` が呼び出し時刻より後になる〔仕様書 T-00 事後条件 3〕）。除外などは LSP の設定に出ない（`build_settings` が使うのはインタープリタと tsserver だけ）ので再起動も `workspace/didChangeConfiguration` も要らない。再起動はクラッシュではないので `restarts` は増えない。`unavailable` は再起動せず `standby` に戻す（`reset` と同じ。次の要求で、新しい venv にサーバーが入っていれば起動する）。`standby` は差し替えだけ。**再起動の責任は `rebind` にある**（c613fb4、BUG-0035）: T-00 の `set_python_env` のジョブは、`workspace_changed` が返した「`rebind` が再起動したハンドル」をもう一度止めない（1 回の呼び出しで二重に起動し直さない）。ジョブが受け持つのは `rebind` が起こさなかった分だけ: 同じインタープリタを指定し直したとき（`pip install` 後の取り直し。動いていても止めて起動し直す）と、まだ起動していない・`unavailable` から戻した `standby` を起動するとき。
- **`started_at`**: ハンドルが `starting` に入るたびにプールの壁時計（UTC）で ISO 8601（ミリ秒）に更新し、停止で `None` に戻す。
- **壊れたプロセスの起動し直し（BUG-0090、実装で確定 2026-10-31）**: プロセスは生きているのに、ある method に以後答えられない壊れ方がある（basedpyright 1.40.2 = pyright 1.1.414 は、references の取り消しが処理の始まる前に届くと、そのプロセスの references に以後ずっと `this._token.cancel is not a function` を返す。§7.5.3）。`LspServerDef.restart_on_errors` にそのエラー文の印を持ち、`lsp/features.py` の `_call` がエラー応答に印を見つけたら `LspHandle.record_broken(method, detail)` を呼ぶ。ハンドルは **その場で（`await` なしで）** `starting`（`reason: restarting`）になり、以後の要求の経路も `status`（T-00 の `lsp_daemons[]`、各ツールの `lsp_status` / `lsp_sources[]`）も `ready` と言わない。続けて `_stop` → `_begin` で起動し直す（クラッシュと同じ `restarts` を数え、起動から `stable_after_s` 以上動いていれば数え直す）。`restart_max` 回に達していれば起動し直さず `unavailable`（`broken_3x`）。その要求は `E_LSP_UNAVAILABLE`（`details.reason: "restarting"`、上限のときは `"unavailable"`）で降格する。要求の経路は起動を待たない（§8.3）ので、その場で聞き直さず、再起動の完了後の呼び出しが `exact` になる。
- **`_stop` の重なり**: 別の `_stop`（`record_broken` / `record_timeout` の停止と `shutdown_all` など）がすでに取り消した監督タスクは取り消し直さず、終わるのを待つだけにする。取り消し直すと監督タスクの `finally` が待つ `client.shutdown` の途中に `CancelledError` が入り、`ProcessRegistry.terminate` に届かず子プロセスが残る（BUG-0090 の試験で実測）。

`didOpen` 直後（解析中）の `definition` はサーバー内で約 0.6 s 待たされて正しい結果が返る（`null` は返さない。3 秒の `LSP_TIMEOUT_*` に収まる）ので、`indexing` 中の要求を `None` で返す設計（ADR-6 (b)）は安全側で変更不要（S-4 で確認）。

**終了**（S-4・S-5・S-11 で確認）: `shutdown` → `exit` で、起動口 `.exe` → python → python → `node.exe` の 4 段（basedpyright）も、tsserver の子（vtsls・tsls）も 1 秒以内に終わる。`ProcessRegistry` は起動口の PID を持てばよい。固まった子の `kill` と、**`.bin/vtsls.cmd` のようなシム経由で `cmd.exe` だけを殺すと `node` が残る**（パイプを握り続け `proc.wait()` も返らない）問題があるので、強制終了は木ごと（Job Object〔ADR-12〕か `taskkill /T /F`）にする。basedpyright の子 `node` が 1.5 秒後に残ったことが 10 回中 1 回あった（後から消える見込み）。

受入で確かめる点:

- LSP を PATH から外した状態で Python のツールを呼ぶと、応答は `structural`、`lsp_status: "unavailable"`、エラーにならない。
- LSP プロセスを外部から kill したとき、ログに再起動が 3 回記録され、4 回目以降は `unavailable` のまま。
- `shutdown_all()` 完了から 1 秒以内に `ProcessRegistry` が空になる。

### 7.5.3 JSON-RPC クライアントと位置変換（client.py、ADR-14）

```python
class LspClient:
    async def start(self, argv, *, cwd, env=None, registry: ProcessRegistry) -> None
    async def initialize(self, *, root_uri, initialization_options=None, capabilities=None, locale=None, timeout_ms=60_000) -> dict
        # initialize → initialized。応答の positionEncoding を negotiated_encoding に入れる
    async def request(self, method: str, params: Any, *, timeout_ms: int | float) -> Any
        # 超過で $/cancelRequest を送ってから TimeoutError（呼び手が降格）。エラー応答は LspError、切断は ConnectionError
        # no_cancel_methods（LspClient(name, no_cancel_methods=LspServerDef.no_cancel_methods)）の method には送らない（BUG-0090）
    def notify(self, method: str, params: Any = None) -> None
    def on_notification(self, method: str, handler) -> None
    def on_request(self, method: str, handler) -> None     # client/registerCapability, workspace/configuration, window/workDoneProgress/create
    def set_settings(self, settings: Mapping[str, Any]) -> None   # workspace/configuration の応答の元（LspServerDef.settings + build_settings）
    async def shutdown(self, grace_ms: int = 500) -> None
    negotiated_encoding: Unit        # core.textpos.Unit。Unit(caps.get("positionEncoding", "utf-16"))。無い・未知なら UTF-16（S-11）
    # to_lsp_position / from_lsp_position / to_lsp_range / from_lsp_range: negotiated_encoding で LineIndex を呼ぶ薄い包み
```

1. フレーミングは `Content-Length` ヘッダ。読み取りタスク 1 本が応答を `id → Future` に配り、通知をハンドラへ回す。子の `stderr` は別タスクでログへ（`debug`）。`window/logMessage` / `showMessage` もログへ。
2. タイムアウトは `core/limits.py` の定数: `LSP_TIMEOUT_DEFINITION_MS=3000`、`LSP_TIMEOUT_REFERENCES_MS=3000`、`LSP_TIMEOUT_HOVER_MS=3000`、`LSP_TIMEOUT_CALL_HIERARCHY_MS=5000`、`LSP_TIMEOUT_CODE_ACTION_MS=5000`、`LSP_TIMEOUT_RENAME_MS=10000`。超過時は `$/cancelRequest` を送ってから呼び手に返す。**例外（BUG-0090、実装で確定 2026-10-31）**: `LspServerDef.no_cancel_methods` の method（basedpyright / pyright の `textDocument/references`・`textDocument/implementation`・`workspace/executeCommand`）には送らない。pyright の `onReferences` などは `CancelAfter` の source を `_pending*CancellationSource` に残し、要求の token が処理の始まる前（キューで待つ間・`window/workDoneProgress/create` の往復を待つ間）に取り消されていると、その source の token が vscode-jsonrpc の凍結された `CancellationToken.Cancelled`（`cancel` を持たない）になる。次の同じ種類の要求は最初にその source の `cancel()` を呼んで TypeError（`this._token.cancel is not a function`）で落ち、欄を戻す前に落ちるのでプロセスが続く間ずっと失敗する。送らなければ token は取り消されず、次の同じ種類の要求がサーバー自身の健全な source で前の要求を止める。遅れて届いた応答は client が捨てる（LSP 仕様の上で `$/cancelRequest` は任意の通知）。それでも壊れたときの回復は §7.5.2 の `record_broken`。
3. **位置変換はこの境界だけ**（MUST）。`core/textpos.LineIndex` の `to_unit(pos, unit)`（コードポイント → 列）と `from_unit(line, col, unit)`（列 → コードポイント）が、`negotiated_encoding` の単位（UTF-8 / UTF-16）で変換する（バイトオフセットは Tree-sitter 用に `position_to_offset`）。`LineIndex` は `didOpen` / `didChange` に送った本文そのものから作る。`Range` / `Location` / `TextEdit` を越境させる関数はここに閉じる。**サーバーへは文字境界の列だけを送る**（MUST）。`to_unit` の丸め（`clamped=True`）が出たら要求を送らず降格応答にする（UTF-16 の 3 サーバーは境界でない列だと黙って別のシンボルや `null` を返し、ruff は文字の途中を指す増分 `didChange` でパニックして終了する〔exit 101〕。S-11 で確認）。`didChange` は全文なので、位置を送るのは definition・references・hover・rename・codeAction の range だけ。増分 `didChange` を入れる（MAY）ときは境界検査を必須にする。`LineIndex` は 4 サーバーの全件で一致した（S-11）。**実装の差**: `LspClient.to_lsp_position` は丸めて返し `debug` ログに残すだけで、受信側は丸めてよい（行末超過・文書末超過・文字の途中は直前の境界へ。TypeScript の `2147483647` もこれで足りる）。送信側の降格は `lsp/features.py` の `_lsp_position` が `to_unit` を直接呼び、`clamped` なら `E_LSP_UNAVAILABLE(details.reason="position_clamped")` を投げて行う。
4. URI: 送信は realpath の絶対パスから `file://` を作る（Windows は `file:///S:/...`）。受信は先に正規化してから `confine` を通し、ルート外は外部シンボル扱い。正規化は `unquote` → 先頭の `/` を落とす（`/s:/work` → `s:/work`）→ `os.path.normcase(realpath)`。basedpyright・vtsls・tsls は小文字ドライブ＋`%3A`（`file:///s%3A/work/...`）で返す（S-4・S-5 で確認）。

### 7.5.4 文書同期（documents.py）

```python
class DocumentSync:   # LSP サーバー 1 つ分の「開いている文書」の台帳。送信は notify 関数（LspClient.notify）を渡される。すべて同期関数
    def did_open(self, path: RelPath, text: str, language_id: str) -> int     # 版 1 で didOpen
    def did_change(self, path, text: str) -> int                             # 全文 didChange。版 +1。送信の直前に送信時刻を記録
    def sync(self, path, text, language_id) -> int                           # 未オープンなら didOpen、本文が変わっていれば didChange、同じなら何もしない
    def on_external_change(self, path, text) -> int | None                   # 開いていれば全文 didChange
    def did_close(self, path) -> bool
    def version_of(self, path) -> int | None
    def sent_at_of(self, path) -> float | None                               # perf_counter。DiagnosticsHub.wait_for(sent_at=…) に渡す
    def touch(self, path) -> None                                            # 要求で触れた文書を新しい側へ（LRU）
```

開いている文書は `LSP_OPEN_DOCS_MAX`（200）の LRU。要求で初めて触れたファイルを開き、超えたら使われたのが最も古い文書から `didClose`（いま開こうとしている文書は閉じない）。送信に失敗（`ConnectionError`）したら状態を変えずに送出し、サーバーの再起動では台帳ごと作り直す。ファイル内容は呼び手が渡す（`text`）。`didChange` は全文（増分は MAY）。`revision != WORKING_TREE` の内容は開かない（LSP は作業ツリーしか知らない）。

**`DocumentRouter`（`lsp/documents.py`、`LspPool.router(ws)`。8a6f40f・c84b7a0、BUG-0023。実装で確定 2026-10-29）**: 1 言語に複数のサーバー（Python は `basedpyright`〔`types`〕と `ruff`〔`lint`〕、§7.5.1）が動くので、`EditTransaction` が求める `documents`（`sync(path, text, language_id)`・`sent_at_of(path)`・`is_open(path)`）と `diagnostics_hub`（`wait_for`）の口を、ワークスペース 1 つにつき 1 つの `DocumentRouter` が持つ。サーバー 1 つ分の `DocumentSync` / `DiagnosticsHub` をそのまま渡すと、書き込み後の `didChange` と診断の待ちが片方のサーバーにしか届かず、`diagnostics` の差分が狂う（BUG-0023）。`LspPool.router(ws)` は同じ `workspace_id` なら同じものを返す。

- **`sync`**: その言語の `ready` なハンドルすべて（`types` が先、`lint` が後）の `DocumentSync` に同期する。`didOpen` の `languageId` はハンドルの定義の `document_language_id`（`c_sharp` → `csharp`）に直す。サーバーごとに版の数え方が違うので、返す版は代表（`types` 優先）の版にすぎず、**サーバーごとの `(版, 送信時刻)` を記録**して `wait_for` が使う。一部への送信が `ConnectionError` でも残りには送る。動いているサーバーが無い、または全部失敗したら `ConnectionError`（トランザクションは警告にして続ける）。**起動はしない**（`ready` でないハンドルは対象外）。
- **`is_open` / `sent_at_of`**: どれかのハンドルが開いていれば真 / 開いているハンドルの送信時刻の**最小**（`wait_for(sent_at=)` の下限を甘い側にして、後から開いたサーバーの診断を取りこぼさない）。
- **`diagnostics_hub.wait_for`**: その文書を開いている `ready` のサーバーすべてを**同時に**待ち、すべて揃ったときだけ `True`（1 つでも遅れたら pending。診断は index から全 `source` ぶんを読むので、1 つでも古いと差分が狂う）。待ちの時間はサーバー数で増えない（`timeout_ms` を共有する）。版つきの待ち（`min_version` あり＝編集した文書）は**記録したサーバーごとの版と送信時刻**で待つ（呼び手の `min_version` は代表の版なので他のサーバーには使わない。記録の無いサーバーは版なしで待つ）。**版なしの待ち（`min_version=None`＝LSP に見せただけの依存元）は `lint` のサーバーを待たない**（ruff は 1 ファイル内の検査で、依存元は内容が変わらないので再発行しない。待つと必ず時間切れになる）。`lint` しか開いていなければ全部待つ。
- **版の検査**（`edit/workspace_edit.py` の `DocumentVersions`）は**`WorkspaceEdit` を出したサーバーの** `DocumentSync` に対して行う（ルーターは `version_of` を持たない）。T-08 など個別のハンドルの診断を読む呼び手は `handles_for` / `hubs_for`。
- **結線（7b4fa68）**: `WiredServices.edit_transaction` は、プールが `router(ws)` を持てば**口を常に `DocumentRouter`** にする。T-13 が `lsp=` に QuickFix を供給した `LspHandle` を渡しても、`lsp` は「LSP で検証する」手がかり（LSP の状態を `READY` にする）にとどまり、`documents` / `diagnostics_hub` は 1 サーバー分でなくルーターになる。`lsp` が無いときの LSP の状態は言語ごとにプールの `status`。ルーターを出せないプール（偽物）のときだけ、渡されたハンドルの 1 サーバー。

### 7.5.5 診断の受信と版待ち（diagnostics.py）

```python
class DiagnosticsHub:   # LSP サーバー 1 つ分。attach(client) で textDocument/publishDiagnostics に結ぶ
    def on_publish(self, params) -> None        # ファイルごとに最新の (version | None, received_at, diagnostics, seq) を持ち、on_diagnostics(path, diags, version) を呼ぶ
    def get(self, path) -> DiagnosticsEntry | None
    def is_provisional(self, path) -> bool      # 最初の受信が空で、まだ信用できない（(c)）
    def settle_all(self) -> None                # $/progress end: 仮の空の診断を信用に切り替える
    async def wait_for(self, path, *, min_version: int | None, sent_at: float, timeout_ms, settle_ms=0, min_publishes=1) -> bool   # 揃えば True
        # sent_at は DocumentSync.sent_at_of。min_publishes は版を付けないサーバーで発行を数える (d)。settle_ms は依存元の静止 (b)
```

1. 受信した診断は `IndexWriter` に「`(file_id, source)` 単位の全置換」として積む（仕様書 §9.4）。`source` は LSP 名（`basedpyright`, `ruff`, `vtsls` …）。**`DiagnosticsHub` 自身は index に書かない**: 受信のたびに `on_diagnostics(path, diags, version)` を呼ぶだけで、`LspPool` がサーバー名とハンドルのワークスペースを足した 5 引数 `on_diagnostics(path, diags, version, source, ws)` を呼び手（Index 側）に渡す（実装で確定 2026-10-29。`ws` は fccee0a で追加。複数ルートでも送り元のワークスペースの DB へ書ける）。`on_diagnostics` が返す awaitable か `concurrent.futures.Future`（`IndexWriter.submit` の戻り）は、**書き込みが済むまで `wait_for` を「揃った」にしない**（§7.7.6 手順 4、EP-7。Future を捨てるとこの順序の保証が外れる。書き込みが失敗したら次の受信まで揃わず、タイムアウトで pending。BUG-0007、706f253）。`wait_for` は編集層の `DiagnosticsWaiter`（§7.7.6）の土台。`enclosing_urn` は `symbols` の範囲包含で引く。`data_version` は進めない（ADR-8）。
2. `wait_for` の条件（S-4・S-5 で確認）:
   - (a) 自分が `didChange` した文書は `version >= min_version`（`==` ではなく `>=`）。basedpyright は連続した変更を畳んで最後の版だけ、ruff は版ごとに届く。診断が変わらない変更（コメントだけ）でも basedpyright は毎回 1 件届く。想定の待ち時間は basedpyright が `didChange` から約 0.3 s、ruff が 1 ms 未満。
   - (b) 依存元の文書は、`version` が自分の旧い版のまま再送されるので、`workspace/didChangeWatchedFiles` 送信後に受信した最初の診断＋ 300 ms の静止で「揃った」とする。
   - (c) 複数文書を続けて `didOpen` した直後の**空の診断は「きれい」と見なさない**（basedpyright は同じ版 1 の空の診断を先に送り、実診断は約 75 ms 後に `$/progress end` と同時に来る）。`$/progress end` または最初の受信から 300 ms の静止まで `unknown` とする。
   - (d) **`version` を付けないサーバー**（vtsls・tsls）は、publish を受けた＝現行とは言えない（vtsls は誤りを消したあとも古い診断〔n=1〕を返し、次の変更で正常化する。3 回の別実行で再現）。変更ごとの発行を数えて追い越しを検出する。診断の無いファイルを開いても何も届かない vtsls に備え、「届かない＝0 件」とみなす猶予時間（設計値: 2 s）を持つ。tsls は常に最新で、空配列も届く。
   - (e) 受信 URI は §7.5.3 の正規化を通してから `DiagnosticsHub` に入れる。
3. Tree-sitter の構文エラーは LSP ではなく Indexer が `source: "tree-sitter"` で入れる（§7.6.2）。同一位置・同一メッセージの重複は T-08 が LSP 側を優先する。

### 7.5.6 機能呼び出しと PaaD への反映（features.py、ADR-6、ADR-15）

```python
# lsp/features.py: LspHandle と作業ツリーの RelPath を受ける async 関数群（実装で確定 2026-10-29。クラスではない）
async def definition(handle, rel, pos) -> list[Location]
async def references(handle, rel, pos, *, include_declaration=False) -> list[RefLocation]
async def hover(handle, rel, pos) -> str | None                      # 署名の文字列（markdown なら最初のコードフェンス）
async def prepare_call_hierarchy(handle, rel, pos) -> list[CallItem]
async def incoming_calls(handle, item) -> list[CallEdge]; async def outgoing_calls(handle, item) -> list[CallEdge]
async def rename(handle, rel, pos, new_name) -> dict                 # prepareRename → rename → WorkspaceEdit（dict のまま。Plan への変換は edit.plan.from_workspace_edit）
async def code_actions(handle, rel, rng, *, only=None, diagnostics=()) -> list[dict]   # edit が無ければ codeAction/resolve
async def execute_command(handle, command, arguments=None) -> CommandOutcome            # tsls の _typescript.organizeImports など。applyEdit を捕まえる
def to_exact_relations(source_urn, refs, *, locate, relation_kind="CALLS", …) -> list[RelationFact]   # confidence=exact。位置 → URN は呼び手が locate で渡す
```

実装の差: `ready` でないとき、各関数は `None` ではなく `LuciankeError(E_LSP_UNAVAILABLE, details.reason="indexing" | "standby" | "unavailable" | "disconnected")` を投げる。読み取り系（definition・references・hover・callHierarchy）は呼び手が握って `structural` に降格し、`rename` / `code_actions` は下の表どおりそのまま上へ伝える。位置は送らずに降格する（`position_clamped`。§7.5.3）。時間切れは `TimeoutError` を上げ、`record_timeout()` / `record_ok()` を呼んだうえで呼び手が降格する。ワークスペース外の URI は `RelPath` にできないので `external_uri` として返す。`WorkspaceEdit` は `edit` を import できないので dict のまま返す。サーバーが `workspace/applyEdit` を**要求として**送ってくる経路（tsls の `_typescript.organizeImports`。S-5）は `execute_command` が受けて捕まえる（`applied: true` と答えるが、書き込みは `EditTransaction` の責任）。

| 能力 | LSP が `ready` | `indexing` / `starting` | `standby` / `unavailable` |
|---|---|---|---|
| DEFINITION / REFERENCES / CALL_HIERARCHY / HOVER | タイムアウト内で待ち `exact` | 待たずに `None`（起動は契機にする）→ 呼び手は `structural` | `None` → `structural` |
| RENAME / CODE_ACTION | 実行 | `E_LSP_UNAVAILABLE`（`details.reason: "indexing"`） | `E_LSP_UNAVAILABLE` |
| SKELETON / SEARCH / BODY | 使わない | 使わない | 使わない |

PaaD への反映:

1. `definition` / `references` / `callHierarchy` の結果は `Location` → `symbols` の範囲包含で URN に写し（ワークスペース外は `_ext`）、`relations` に `exact` で書く。同じ `(source_urn, target_urn, relation_kind)` の `structural` 行は置き換える（仕様書 §9.4）。**この書き込みは `data_version` を進めない（`bumps_version=False`。実装で確定 2026-10-29、1d8700d）**。進めると、T-06・T-07 自身が直前に返した `next_cursor` が次のページで `E_CURSOR_EXPIRED` になる（パイプラインは `data_version` をハンドラの前に引く）。書き戻しは同じ問い合わせの答えを `structural` から `exact` に置き換えるだけで、そのツールの結果集合は変わらない。以前の版（本書の初版）は「進める」と書いていた。

   **T-06・T-07 の `exact` 経路（`tools/_lsp_exact.py`。実装で確定 2026-10-29、1d8700d）**: ツールは次の順で答える。
   - **PaaD から（2 回目以降）**: 以前 LSP で取って `relations` に `exact` で書いた集合が**まだ新しい**なら、LSP を呼ばずその `exact` 行だけで答える（LSP が止まっていても `exact`）。新しさの目印はプロセス内の `MEMO`（問い合わせごとに `(files_fingerprint, exact 行の指紋)`）。ファイルが 1 つでも変わる（索引の `sha256` が変わる）か、別の書き手が同じ行を書き換えると目印は外れる。ファイルの再取り込みはそのファイルの `exact` 行を消して `structural` を入れ直すので、行の有無だけを見ると「LSP でしか見つからない参照」が編集後に黙って落ちる。目印はその穴を塞ぐ条件である。
   - **LSP から（1 回目）**: `ready` の LSP に聞き、`to_exact_relations` で `RelationFact` にして `IndexWriter` へ**1 回のジョブで**書き戻す（`upsert_exact_relations` は `(元, 先, 種別)` ごとの置換なので、同じ組を別ジョブで書くと互いに消す）。書けて読み戻した指紋が一致し、書いている間にファイルが変わっていなかったときだけ `MEMO` に目印を置く。使用種別・囲みのシンボル・ガード条件は LSP からは来ないので、参照箇所のファイルを構文木にかけて位置から引く（索引器と同じ分類。引けない位置は `REFERENCES` / `r` で囲みはファイルの URN）。
   - **0 件の答えは信用しない**: 起動直後の LSP は `ready` でも解析が済まず 0 件を返すことがある（basedpyright の実測）。索引に参照・呼び出しがあるのに LSP が 0 件と答えたときは、内部では `E_LSP_UNAVAILABLE`（`details.reason: "indexing"`）として扱って `structural` に降格し（ツールの応答はエラーにならない）、目印も置かない（呼び直すと `exact` になる。未完了の LSP の空振りを固定しない）。
   - **LSP が探さないファイルを先に開かせる（`prime_set`。BUG-0047・BUG-0132）**: T-06 の参照検索と T-07 の incoming は、聞く前に、参照元になりうるのに LSP が開かれるまで探さないファイルを `didOpen` する（索引の Tier 1〜2 で対象の名前を単語境界で含み、未オープンのもの。テスト優先、1 回に `PRIME_MAX` = 100 件まで、超えた分は `warnings`）。tsserver 系は同系のファイルすべて（`tsconfig.json` の `include` の外は開くまでどのプロジェクトにも入らない）。Python は basedpyright の解析の対象（`lsp/pyright_scope.py`）の外のファイルだけ: basedpyright は対象の内なら未オープンの文書も探すが、外は開いた文書しか探さない（実機で確認: `include: ["src"]` の F-POLY で `tests/test_payment.py` を開くと参照にも incomingCalls にも出る）。対象は根の `pyrightconfig.json`（JSONC）→ 根の `pyproject.toml` の `[tool.basedpyright]` / `[tool.pyright]` の `include` / `exclude`（`extends` を辿り、最も派生した設定の値が勝つ。パスは書いた設定のディレクトリから）。`include` が無ければ根、`exclude` にはサーバーの既定（`**/node_modules`・`**/__pycache__`・`**/.*`・`**/__editable__.*`）を足す。照合は pyright の `getFileSpec` と同じ正規表現（`**` は 0 以上の階層、末尾は `($|/)`、Windows は大文字小文字を区別しない）。仮想環境の自動の除外は見ない（内とみなす = 開かせない側）。設定が読めないときは判断せず開かせない。利用者の設定が `include` / `exclude` を書いていない（既定の除外だけ。外はドットのディレクトリ・`node_modules`・`__pycache__`・編集可能インストールの影の木だけ）ときも開かせず、ファイルの一覧も引かない（ふつうのワークスペースの答えの速さを変えない）。対象は LSP の起動のときに引いてハンドル（`LspHandle.analysis_scope`）に覚え、それで決める（Lucianke の見る対象をサーバーが読んだものに揃える。ディスクの設定を要求のたびには読まない）。根の設定の変更をサーバーに届けたとき（`LspPool.notify_watched`。§7.5.1）に引き直す（BUG-0135）。開かせた後の答えは LSP が見たものそのままなので `exact` のまま（実装で確定 2026-10-31、BUG-0132）。
   - **降格**: LSP が `ready` でない・時間切れ・`E_LSP_UNAVAILABLE` なら `structural`（`relations` の全行。今までどおり）。
   - **T-07 はノードごと（BUG-0091。実装で確定 2026-10-31）**: 木を深さごとに聞く（`CallEdgeSource.prefetch`）。根の要求は要求 1 つの期限ではなく予算の残り（＋下の裏の待ちの `EXACT_BACKGROUND_S`）まで待ち（`_root_limits`。連続時間切れに数えるのは期限以上待っても答えなかったときだけ）。1 問い合わせの予算 `EXACT_BUDGET_S`（10 秒）を使い切る・要求の期限（`LSP_TIMEOUT_CALL_HIERARCHY_MS`）で切れる・LSP が使えなくなると、以降のノードは LSP に聞かず、子を `relations` の行で補う（答えた分は `exact` のまま。根の子を取れなければ木ごと `structural`）。予算で打ち切ったとき、すでに LSP に送っていた要求は取り消さず、答えを裏で待って `relations` に書き戻し目印を置く（`CallEdgeSource._detach` / `_late`。根は予算の残り＋`EXACT_BACKGROUND_S`〔60 秒〕、深いノードは要求 1 つの期限まで。プロセス全体で `INFLIGHT_MAX`〔8〕ノードまで、超えた分と送る前の要求は今までどおり取り消す。LSP が落ちる・起動し直すと要求は切断で終わる）。同じノードを同じファイルの状態（`files_fingerprint`）で聞く次の問い合わせは、要求を送り直さずその答えを予算の内で待つ（`_INFLIGHT` / `_join`）。理由（BUG-0123。実装で確定 2026-10-31）: basedpyright は `$/cancelRequest` を受けても計算中の要求を止めない。以前は取り消して遅れて届いた答えを捨てていたので、冷えた LSP で 17〜22 秒かかる根の `incomingCalls`（Mnemo の能力ベンチ T11）は毎回やり直しになり、呼び直しの要求もその後ろに並んで予算を使い切り、続く別の問い合わせ（T12）の要求まで遅らせていた。警告は切れた上限の種類と実際の値・`exact` のノード数と深さ・次にすること（`CallEdgeSource.degradation_warning` / `cut_note`。セレクターの `lsp_timeout` 雛形は要求の期限しか知らないので T-07 の時間切れには使わない）。LSP への要求は 1 本ずつ（`EXACT_LANES = 1`。basedpyright は要求を順に処理するので、重ねると待ち行列の分だけ要求の期限に近づく）。深いノードの項目は親の答え（incoming の `from` / outgoing の `to`）をそのまま使い、`prepareCallHierarchy` を送らない（索引のシンボルと同じファイル・同じ名前の位置のときだけ）。最初の診断を待つのは根のファイルだけ（「LSP が解析を済ませた」合図は LSP 全体の状態なので 1 度で足りる。T-06 と同じ）。根の子が PaaD から来た呼び直しで深いノードを聞くときも、そのノードのファイルではなく根のファイルで待つ（`_settle_path`。BUG-0123: ノードのファイルごとに 0.7〜1.6 秒待ち、basedpyright がそのファイルを検査するあいだ他の要求も遅れていた）。根の子をその合図の後に聞いたときだけ、木のノードに目印を置く。呼び出し箇所の構文木は要求の枠の外でファイルごとに並行。
   - **範囲**: T-06 の実装検索（`implementations`）と型階層（`type_hierarchy`）に LSP 版は無い。`exact` の応答でも索引の継承関係（`INHERITS` / `IMPLEMENTS`）からの推定のままで、`warnings` に書く。T-07 の外部（標準ライブラリ等）の呼び先は、LSP が返したファイルのパスから推定した `lk://_ext/` の URN の葉にする（最善努力）。
2. `hover` の型文字列は内部テーブル `_hover_cache(urn, sha256, hover_text, cached_at)`（主キーは **`(urn, sha256)`**。`sha256` は定義ファイルの内容の SHA-256）に置き、T-04 の `signature_expanded` に使う。公開しない（`_` 接頭辞）。行があれば LSP を呼ばず（起動も促さず）`exact` で答える（R-11）ので、定義ファイルの内容が変わると鍵が外れて LSP に聞き直す（同じ `urn` の古い `sha256` の行は書くときに消す）。書き込みは `data_version` を進めない（内部表。76a2aca。実装で確定 2026-10-29）。
3. `rename` / `codeAction` の `WorkspaceEdit` は `edit/plan.py` の `Plan`（hunk 列）に変換して返すだけで、**書き込みは `EditTransaction`** が行う（ADR-11）。ファイルの作成・削除を含む `WorkspaceEdit` は `E_UNSUPPORTED`（実測ではどのサーバーも `resourceOperations` は 0 件）。`changes` と `documentChanges` の**両方の形**を変換する（サーバーごとの形は §7.5.1 の表。`textDocument.version` は `null`＝版検査なし。S-4・S-5 で確認）。開いたのが定義側 1 ファイルだけでも、basedpyright の `rename` は呼び元の編集も返す。
- **Python の `codeAction`（quickfix・organizeImports・fixAll）は ruff に送る**（PaaD への反映ではなく送り先の規約）。`code_actions` は Python では ruff に送り、basedpyright には送らない。`resolve_code_action` は ruff のためにある（`data` あり・`edit` 無しが返り、`codeAction/resolve` で `edit` が付く）。TypeScript の organizeImports は経路が違う（§7.5.1 の表）ため、サーバー定義ごとに持つ。

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
    async def submit_async(self, job: WriteJob) -> Any     # 満杯（WRITE_QUEUE_MAX=1000。実装で確定 2026-10-29）なら待つ
```

1. 専用スレッドがキューから取り、`BEGIN IMMEDIATE` → `fn(conn)` → `bumps_version` なら `UPDATE _meta SET data_version = data_version + 1` → `COMMIT`。例外は `ROLLBACK` し、`label` 付きでログ、`Future` に例外を載せる。半更新の状態は読み手に見えない（MUST）。
2. 接続は書き込み専用の 1 本（`journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=5000`、`cache_size=-131072`）。ページキャッシュの上限（`WRITER_CACHE_KIB` = 128 MiB）は、長い URN の索引（`symbols` の `urn`・`name`、`relations` の `target_urn`）へばらばらの位置に差し込む初回の索引のため。既定の 2 MiB ではページの読み直しが続き、4,222 ファイルの書き込みが 61〜82 秒 → 38〜42 秒（上限なので、小さな DB では使った分しか取らない。実装で確定 2026-10-31、BUG-0127）。取り込み（`ingest_file`）は文の数も減らす: シンボルの INSERT と、そのシンボルを先に指していた行の `target_file_id` の UPDATE は `executemany`、関係の目標の持ち主の `file_id` と外部シンボルの行の有無は `IN (…)` でファイルごとに 1 文で引く（以前はシンボル・目標ごとに 1 文ずつ、初回の索引で約 40 万文。合わせて 25〜27 秒）。
3. チェックポイントは、キューが空になり前回から 1,000 コミット以上たったとき `PASSIVE`、終了時に `TRUNCATE`。
4. 起動時の差分索引では、1 トランザクションに最大 50 ファイルをまとめ、`data_version` はトランザクションごとに 1 回だけ進める。

### 7.6.2 取り込みパイプライン（engine/indexer.py と index/ingest.py）と data_version（ADR-8）

```python
# engine/indexer.py（実装で確定 2026-10-29。当初の ingest_file(ws, rel) / rescan(ws) を分けた）
INGEST_BATCH_FILES = 50                       # 起動時の差分索引は 1 トランザクションに最大 50 ファイル。data_version はトランザクションごとに +1
class Indexer:                                # ワークスペース 1 つ分
    async def index_file(self, rel) -> IngestStats | None      # 1 ファイルの再取り込み。sha256 が同じなら何もしない。消えた・除外になったなら remove
    async def remove(self, rel) -> bool
    async def scan(self, *, progress_cb=None) -> ScanReport    # 起動時の差分索引と全体の再索引（§7.6.5 と同じ処理）。進捗 0〜100 は単調増加
    def start_scan_job(self) -> str; def job_status(job_id); def jobs(); async def wait_job(job_id)   # 背景ジョブ（T-00 reindex、status の jobs[]）

# index/ingest.py（書き手スレッドのトランザクションの中で呼ぶ。外では RuntimeError）
def ingest_file(conn, facts: FileFacts, *, now=None) -> IngestStats    # files / symbols / relations / diagnostics の差分更新
def remove_file(conn, workspace_id, path) -> bool
def upsert_workspace(conn, ws_row) -> None
def upsert_exact_relations(conn, rows: Sequence[RelationFact]) -> int  # LSP の結果（§7.5.6 の反映 1）
def replace_lsp_diagnostics(conn, …)                                   # (file_id, source) 単位の全置換（§7.5.5 の 1）
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

実装との差（実装で確定 2026-10-29）:

- **分担**: `Indexer`（`engine/indexer.py`）が走査・解析・バッチ化を担い、`index/ingest.py` は解析結果 `FileFacts`（`RelationFact` / `DiagnosticFact` を含む）を受けて DB を更新するだけ。`index` が `syntax` / `lsp` を import しないための分け方（§4.2）。
- **バッチ**: 起動時の差分索引は 1 トランザクションに最大 `INGEST_BATCH_FILES`（50）ファイル。走査中は 1 バッチの書き込みを待たず次のバッチの解析へ進む（書き込みは常に 1 バッチだけ先行）。`data_version` はトランザクションごとに 1 回だけ進む。
- **圧縮済みの JS / TS**（BUG-0119、2026-10-31）: `_prepare` は、JS / TS のうち名前が `MINIFIED_SUFFIXES`（`.min.js` / `.min.mjs` / `.min.cjs`）のもの、または `MINIFIED_MIN_BYTES`（32 KB）以上で 1 行の平均が `MINIFIED_AVG_LINE_BYTES`（300 バイト）以上のものを、構文木を作らず `files` 行だけ（`lexical_fallback`）にする。理由: Mnemo の基準のワークスペースの `plotly.min.js`（4.56 MB）1 本が 46 秒・シンボル 6,869・関係 41,160（名前は `e`・`t` など）で、全体の索引の約 2 割を使い、名前の検索・名前の一致の候補を汚していた。ツールが直接そのファイルを開く（T-02 など）ときは今までどおり解析する。
- **入れ子の Git の作業ツリー**（BUG-0118、2026-10-31）: 列挙（`Indexer._walk`）は、根以外で `.git`（ファイル・ディレクトリのどちらでも）を持つディレクトリに降りない。1 ファイルの取り込み（監視・先取り。`_is_skipped`）も祖先にそれがあれば対象外。根の `.gitmodules` の `path` に挙がるサブモジュールは今までどおり辿る。理由: Lucianke の主の作業ツリーの `.claude/worktrees/` に並行の役の作業ツリーが 33 あり、`.py` は外 454 本・中 15,048 本だった（外部のクライアントの 1 呼び出しの上限 120 秒を超えた）。
- **走査の段**（BUG-0112。実装で確定 2026-10-31）: 列挙（進み 0〜2）→ 消えたファイルの削除（〜5）→ ファイルごとの解析と取り込み（5〜95。`building()` は `files_done/files_total`）→ 関係の整え（95〜98）→ 差分のときだけ取り込まなかった import 元の解き直し → PageRank（99）→ 100。全ファイルの後の段は `ScanProgress.phase` と `JobStatus.phase`（`settling relations k/n`・`re-resolving importers`・`ranking symbols`・`settling files ingested ahead`）で出し、読むツールの進みの警告と T-00 の `jobs[].phase` に載せる（止まって見えないように）。整えは `SETTLE_BATCH_FILES`（50）ファイルずつの短いジョブに割る: (1) ファイルごとの整え（`_settle_files`: 再び公開した名前・オーバーロード・継承したメンバーの退避・無い目標の削除・`target_file_id`）を全バッチで、(2) 退避した行を基底から結ぶ（`inherited.resolve_member_refs`）を全バッチで。(2) は (1) が全ファイルを終えてから。(2) を割っても結ぶ行の和集合は同じで（各バッチのファイルの退避行と、そのクラス・派生クラス・変数を受け手にする退避行）、2 回結ばれる行は同じ答えになる（割らない整えと同じ索引になることをフィクスチャで確かめている）。理由: 1 ジョブで全ファイルを整えると書き手を長くふさぎ、その間の先取り・編集の書き込みが待たされる。1 ファイルの取り込み（`index_file` / `remove`）は今までどおり 1 トランザクションで整える。
- **大きな行・深い入れ子の費用**（BUG-0112）: 位置の変換（`core/textpos.LineIndex`）は `SHAPE_MIN_BYTES`（256）以上の行の形（ASCII か・複数バイト文字と 4 バイト文字の位置）を 1 回だけ調べて覚え、二分探索で答える（変換のたびに行全体を切り出してデコードしない）。関係の抽出の祖先の走査（囲みのスコープ・読み飛ばす文の中か）はノードごとに覚えて使い回す（`Node.parent` は根から下りて探すので、参照ごとに根まで登ると深さの 2 乗になる）。囲みの定義の掃引は、次の定義を積む前に、それより前に終わった定義を降ろす（並んだ数千の定義が積み上がると 参照の数 × 定義の数）。照合（`QueryCursor.matches`）は `QUERY_WINDOW_BYTES`（256 KB）より大きいソースをバイト範囲ごとに行い（`syntax/parsers.iter_matches`）、範囲の間で前景に譲る（1 回の `matches` は照合と結果の組み立ての全体で GIL を握り、4.5 MB で 70 万件・数秒、その間イベントループも止まっていた。範囲をまたぐ一致は両方の範囲で返るので、受け手は同じノードを 2 度受けても結果が変わらない使い方にする）。理由: 圧縮した JS の 1 行 数 MB（`plotly.min.js` 4.5 MB）で、変換の回数 × 行の長さ・参照の数 × 深さの 2 乗になり、1 ファイルの取り込みが数十分から数時間かかって、その間ほかのファイルの取り込みも進まなかった。
- **import 表から作った関係の後始末（`_settle_relations`）**: import の解決は定義元の存在を確かめずに URN を作る（走査の順序に依存させないため）。そこで、取り込み後に同じトランザクションの中で (1) `symbols` に無いシンボルを指す目標（`#` を含む URN）の行を消し、(2) `target_file_id` を目標のシンボルの持ち主で埋める（目標のファイルが後から取り込まれた場合に備える）。外部シンボル（`lk://_ext/`）は `ingest_file` が `symbols` に最小の行（`is_external`）を作る（`_ensure_external`）ので消えない。
- **継承したメンバーへの参照（`index/inherited.py`、BUG-0075。実装で確定 2026-10-31）**: 抽出器は受け手の型が分かる属性アクセスの先を `<受け手のクラス>.name` まで決める（同じファイルの基底だけで決まるなら C3 の順でその場で結ぶ。`core/mro.py`）。基底が別ファイルだとその URN は `symbols` に無いので、`_settle_relations` は消す前に、目標の「クラスの部分」がワークスペースのクラス・インターフェースである `CALLS` / `REFERENCES` の行を内部表 `_member_refs` に退避し、最後に受け手のクラスの C3 線形化（`INHERITS` 行を書いた順。外部の基底は葉。`object`・`Generic`・`Protocol`・`ABC` 以外の外部の基底・クラスでない基底に先に当たったら止める）で最初に見つかったメンバーを先にした行（`structural`）を作る。結び直す範囲は、整えるファイルの退避行と、整えるファイルのクラスとその推移的な派生クラス（`INHERITS` を逆にたどる再帰 CTE）を受け手にする退避行。基底のメンバーの増減・上書き・基底の並びの変更はそのクラスのファイルの取り込み → 整えで結び直り、`mid` を import しない `use.py` の `s.helper()` も取り込み直さずに追従する（直接の派生クラスのファイルは基底を import しているので、基底の URN が消えたときは import 元の解き直しで一緒に整う）。前に結んだ先は `_member_refs.resolved_urn` に覚え、変わったら行を差し替える。`super().m()`（`super(C, self).m()`・TS / JS の `super.m()`）は抽出器が `RelationCandidate.super_of`（`C` の URN）にし、取り込みが `_member_refs` に `via_super = 1` で直接入れる。整えは `C` の線形化の **次のクラスから** 探す（`C` 自身の上書きには結ばない。BUG-0081）。先に当たった外部・クラスでない基底のために結べなかった行は `index/inherited.blocked_member_refs` が引き、T-06 / T-07 の `structural` の答えが対象のクラスがその基底の後ろにあるときだけ理由つきで書く（`tools/_blocked_refs.py`）。`a.b.m()` の受け手は `a.b` で、抽出器は `a.b` の型を同じファイルのクラスの属性の型（注釈・property の戻り値・構築の呼び出し。`_attr_type`）から決め、分からなければ名前だけの呼び出し（`_name_calls`）にする（BUG-0079）。受け手が名前（`x.m()`）でも同じで、型が構文から分かる（注釈〔前方参照の文字列を含む〕・`x = Foo()` / `new Foo()`・`self` / `cls` / `this`・モジュールの名前）ときだけそのメンバーに結び、分からなければ同じファイルの同名の末尾一致にも import した同名にも当てず名前だけの呼び出しにする（`_resolve_member` に末尾一致の当て方は残っていない。BUG-0085）。点つきのモジュールのパスの受け手（`import pkg.helpers` の `pkg.helpers.flush()`）は、索引器が import 表とファイルの在り処からサブモジュールのファイルに解いて（`syntax/imports.dotted_module_path`。調べたパスは `_import_probes` の語幹に残す）モジュールの関数に結ぶ（BUG-0093）。import した変数の受け手（`from conf import settings` の `settings.reload()` → `conf.py#settings.reload`）は、`_settle_relations` が目標の「クラスの部分」がモジュール直下の変数・定数なら `_member_refs` に退避し（`class_urn` は変数）、定義元の変数の型（`_var_types`。抽出器がモジュール直下の `x = Foo()`・`x: Foo = ...` から書く）の C3 線形化で結ぶ。型が外部なら外部のメンバー、分からなければその位置を `_name_calls` に書いて名前の一致に任せる。変数のファイルと、型のクラス（とその基底）のファイルを整えるたびに結び直す。目標の「クラスの部分」がシンボルでないが、そのファイルは索引にある（`__init__.py` が再び公開した名前で、定義元までたどれないものなど）行も、消さずに `_name_calls` に移す（BUG-0094）。**再び公開した名前**（`index/reexports.py`、BUG-0100。実装で確定 2026-10-31）: `_settle_relations` は退避の前に、整えるファイルの `symbols` に無いワークスペースの目標（呼び出し・参照・`INHERITS`・型の使用・import の行すべて）と `_var_types` の型を、`_reexports`（取り込みが Python のファイルごとに書く。モジュール直下の import が束ねた名前 → 先、`from x import *` のワークスペースの先、文字列のリテラルだけの `__all__`）でたどった先に書き換える（`pkg/__init__.py#flush` → `from .helpers import flush` → `pkg/helpers.py#flush`。`P#A.rest` の `A` を `P` が束ねた先に置き換えて続け、束ねていなければ `P` の `*` の先を後ろから見て `A` を公開しているもの〔`__all__` があれば載っている、無ければ `_` で始まらず定義・束ね・`*` で持つ〕。シンボル・外部・`P#A` がシンボル〔残りは継承したメンバー・変数の型として続く退避で決まる〕で止める。段は 5〔T-04 と同じ〕まで、循環なら元のまま）。別名・絶対 import・何段の連鎖・普通のモジュールの再公開・外部の再公開（`lk://_ext/threading#Lock`）も同じ。たどるときに引いた `(ファイル, 名前)` は `_reexport_deps` に覚え（2 段目からは着いた先がシンボルでも）、そのファイルの「公開する名前」（最上位のシンボル・束ねた名前の先・`*` の先・`__all__`）が変わったら依っていたファイルを取り込み直す（`engine/reresolve.py`）。理由: パッケージは公開 API を `__init__.py` で再び公開するのが普通で、そこから import した呼び出し元が捨てられると、ライブラリの関数の実際の呼び出し元の大半が T-06 / T-07 から消える。**TS / JS のバレル**（BUG-0104、2026-10-31）: 取り込みが TS / JS のファイルごとに `_reexports` に公開する名前を書く（`syntax/imports.js_module_exports`。`export { a, b as c } from './m'`・`export * as ns from './m'`・import した名前の `export { a }`・同じファイルの定義の別名・既定の export〔`default` → そのローカルの定義〕は `import`、`export * from './m'` は `star`、公開する名前は`export`）。たどり方は Python と同じで、`*` の先は TS / JS の規則（`m` が公開する名前〔`export`〕か、`m` がさらに `export *` で公開する名前。既定の export は公開しない。`_` の規則・`__all__` は無い）。`./dir` は今までどおり `./dir/index.ts` などに解く。理由: バレル（`index.ts`）は TS のパッケージの公開 API の普通の書き方で、TS の LSP が無いとバレル経由の呼び出し元の大半が見えなかった。**TS / JS の既定の import**（BUG-0109、2026-10-31）: 抽出器は `import main from './helpers'` の `main` を `helpers.ts#default` に解き（前は解かずに捨てていた）、整えが `_reexports` の既定の export の束ね先（`default` → `export default function main` の `main`・`export default h` の `h`・`export { x as default }` の `x`）まで上と同じくたどる。バレルの `export { default } from`・`export { default as foo } from`・`import * as m` の `m.default()` も同じ。たどった先が `P#default` のままで `default` という名前のシンボルも無い TS / JS のモジュール（無名の `export default () => …`・`export default { … }`・既定の export が無い）なら、呼び出し・参照・型の使用・import 文の行の先をモジュール `P`（ファイルの URN）にする（`ReexportResolver.anonymous_default`。`INHERITS` / `IMPLEMENTS` は今までどおり消す）。既定の export の変化は `export_changes` の束ねた名前 `default` の変化として引く。理由: 既定の import は TS / JS の普通の書き方で、LSP が無いと呼び出し元が import 文の行ごと消えていた。無名の既定の export は結ぶ先のシンボルが無いので、黙って捨てずにモジュールを指す。**TS の型の別名の受け手**（BUG-0108、2026-10-31）: 抽出器は `type A = …`（シンボルになったもの）の構成を内部表 `_type_aliases` に書く（`syntax/relations._Extractor.type_aliases`。値の名前の型・交差・合併を書いた順に平らにする。型リテラルのメンバーは `member`、`Partial<B>` などメンバーを持ち越す標準の型は `B`、総称の型 `B<T>` は `B`、別名の型引数・関数型・条件型・添字のシグネチャ・`string`・合併の中の交差は `opaque`、`null` / `undefined` / `void` / `never` は書かない）。同じファイルの別名を注釈にした受け手も`<別名>.name` にする（`_name_ref` が TS の `Type` を URN で返す）。整えは目標の「クラスの部分」が別名（`kind = 'Type'`）なら`_member_refs` に退避し（`_receiver_class`）、`MemberResolver.lookup_alias` で構成から探す: `and` は書いた順に最初に持つ構成（クラス・インターフェースは C3、別名はさらにその構成。別名自身のメンバーはシンボルがあればそれ、無ければ結ばない〔`inline`〕）、どれにも無ければ外部の型の構成のメンバー、中身の分からない構成があれば名前だけ（`_name_calls`）。`or` は持つ構成がちょうど 1 つで中身の分からない構成（外部を含む）が無いときだけ結び、それ以外は名前だけ。C3 の線形化の途中の別名（`interface Options extends KyOptions`）も同じく構成から探す（分からなければ今までどおりそこで止める）。構成の型がバレル・既定の import の名前なら、`redirect_reexports` が `_type_aliases.target_urn` も定義元に書き換える。結び直す範囲の再帰 CTE は、整えるファイルの別名と、構成に派生クラス・別名を持つ別名も足す（`_type_aliases.target_urn` を逆にたどる）。理由: 型の別名は TS の型の普通の書き方で、別名を注釈にした受け手のメンバーの読みは行にも名前の一致にも残らず、注釈の無い受け手より見えなかった。v13 から TS の型リテラルのメンバーはシンボル（BUG-0114。§7.4.3）で、別名自身のメンバーの読みはそれに結び、`and` では別名自身のメンバーをほかの構成より先に見る（抽出器が `this.#options.headers` を `InternalOptions.headers` に直接結ぶのと揃える）。`or` では同じシンボル（合併の別々の型リテラルの同名のメンバーは素の URN を指す）を 1 つに数える。**TS のメンバーの型の受け手**（BUG-0113、2026-10-31）: 抽出器は TS の注釈を `_TsType`（名前・添字の道・標準の配列・`Promise` の中身）に読み、先頭の名前に縮めない（前の `_js_ann_name` は最初の `type_identifier` を返していた: `Opts['retry']` も `Opts[]` も `Opts`、`Error & {…} | undefined` も `Error`、`express.Request` は `Request`）。添字の型 `Opts['retry']` の受け手の URN は `<Opts>.retry` で、`r.limit` の先は `<Opts>.retry.limit`。属性の連鎖 `o.retry.limit` も、`o` の型が別ファイルの型・同じファイルのインターフェースなら `o.retry` の型を `<型>.retry` とする（Python は今までどおり分からない）。配列・タプルは標準の `Array`（`ReadonlyArray`）、`Foo | null` は `Foo`、`Promise<T>` は `await` で `T`、`Partial<Foo>` などは `Foo`、`x as Foo` は `Foo`、それ以外の合併・交差・型リテラル・型引数は分からない。抽出器は型注釈つきのメンバー（`property_signature`・`public_field_definition`）の型の構成も `_type_aliases` に書く（`alias_urn` がメンバー。別名と同じ `_alias_part`。配列は `lk://_ext/globals#Array`、添字の型は `type` の `<B>.k`）。整え: 目標がシンボル（型リテラルの入れ子のメンバー）ならそのまま。無ければ `_receiver_class` が「クラスの部分」をメンバー（`Property` / `Field` / `Method`）としても退避し、`MemberResolver.lookup` がメンバーの型の構成を `lookup_alias` で探す（メソッド・構成の無いメンバーは名前だけ）。「クラスの部分」がシンボルでない（`Options.hooks` の `hooks` が基底の別名から継承したメンバー）ときは、いちばん長い受け手になれるシンボルの途中（`Options`）を `class_urn` にし、`name` を点つきの道（`hooks.beforeRequest`）にする（`_lookup_path` が 1 段ずつ型の分かるメンバーをたどり、決まらなければ名前だけ。`_name_calls` の名前は道の最後）。別名の構成の `type` の先がメンバー・シンボルでない道でも同じくたどる（`_in_type`）。結び直す範囲の再帰 CTE は、整えるファイルの型の分かるメンバーも種に入れ、`_type_aliases` を逆にたどったメンバーの持ち主（`symbols.parent_urn`）も足す（メンバーの型だけの編集は import している側を取り込み直さないので、道の受け手は持ち主から結び直す）。理由: 注釈の綴りの最初の名前に縮めると、`Opts['retry']` の `r.limit` を `Opts.limit` に、`errors: Error[]` の `errors.push()` を `Error.push` に `structural` で結んでいた（誤った確かな行は取りこぼしより悪い）。ky（F-TS）で 29 箇所の先が正しく変わった。**import する側の `from x import *`**（BUG-0105、2026-10-31）: `from x import *` を持つ Python のファイルで、同じファイル・import 表・組み込み名のどれでも決まらなかった素の名前（と受け手・型・基底に使う名前）は、抽出器が `<このファイル>#name` を仮の目標にし（`syntax/relations.resolve_star`）、整えが `*` の先を後ろから見て公開しているもの（上と同じ規則）に書き換える。どの `*` の先も公開していなければ、素の名前は今までどおり捨て、受け手の名前は名前だけ（`_name_calls`）、変数の型は消す。`__all__` がリテラルでない `*` の先（`dynamic_all`）は公開する名前が構文から決まらないので、公開名の規則で見つけた名前を `heuristic` で結ぶ（名前だけで決めた。`ReexportResolver.resolve_detail`。再び公開する側の `*` も同じ）。理由: `*` で持ち込んだ名前の呼び出し元が黙って消えるより、`*` の先が公開する定義に結ぶ方が呼ぶ側の AI の役に立ち、決まらないときは水準で知らせる。型の分からない受け手の属性の読み書きは `_name_calls` に `use_kind` つきで残し、T-06 が同名のクラスのメンバーと `_attr_defs` のインスタンス属性を候補に名前で判定する。外部の型のメンバーと同名（索引にある `lk://_ext/<pkg>#<Class>.<名前>` を型の分かる受け手が使っている。兄弟のモジュールは除く。`index/queries.external_members_named`）なら結ばない（T-07 は呼び出しだけ。BUG-0095。理由: 黙って 0 件に見せない）。
- **インスタンス属性への参照（`index/inherited.py`、BUG-0122。実装で確定 2026-10-31）**: 受け手の型が分かる `a.skipped`・`self.skipped` で、線形化のどこにもメンバー（シンボル）が無ければ、整えが `_attr_defs` から、先に当たって探すのをやめた基底より前で最初にその名前のインスタンス属性を持ち基底のものにまとまらないクラスの `<クラス>.skipped` に `structural` で結ぶ（同じファイルのクラスの受け手も抽出器が整えに回す）。先はシンボルでないので、`relations` に「シンボルでない先」の行ができる例外はこれだけで、インスタンス属性が消えたら（取り込み・削除）`ingest.py` がそれを指す行を消す。T-06 は `resolve_target(instance_attrs=True)` でインスタンス属性を対象にできる。どれも無い名前は `_member_refs` に結べないまま残し、受け手の型が対象のクラスの基底なら `blocked_member_refs` が `base_receiver` で返す。
- **未解決の import は外部扱いにする（決定 2026-10-29）**: 解決できない import（標準ライブラリ・`site-packages` / `node_modules` の第三者・解決失敗）は、**捨てずに** `lk://_ext/<パッケージ>#<名前>`（`<パッケージ>` は import 先の最上位モジュール名。§7.4.3 項 9）を `target_urn` にして `relations` に残す。理由: T-06 / T-07 が外部ノードを必要とする（外部の呼び先を返す）。**現状の実装は捨てている**（`engine/indexer.py` の `_relations` が解決できない名前の関係を落とす）。実装は別の役（7j）が本書に合わせて直す。
- **import 元の解き直し（`engine/reresolve.py`、edb72e4、BUG-0025・BUG-0034。実装で確定 2026-10-29）**: import の解決は、定義元のファイルの有無を**取り込んだ時点のファイルシステム**で決める（走査の順序に依存させないため、定義元の存在は確かめずに URN を作る）。そのままだと、`a.py` を取り込んだ後で `b.py` が作られても `a.py` の行は `lk://_ext/pkg#b.helper`（外部）のまま、相対 import なら行そのものが無いまま残り、逆に `b.py` が消えると指す先が宙に浮く。そこで次のようにする。
  - 取り込むとき、`resolve_module` が調べたパス（当たった・外れた両方）の**語幹**（パスから拡張子と `__init__` / `index` を除いたもの。`pkg/b.py` も `pkg/b/__init__.py` も `pkg/b`）を内部表 **`_import_probes(file_id, stem)`**（schema v3、`file_id` は `ON DELETE CASCADE`、`stem` に索引）に書く。`FileFacts.import_stems` を `ingest_file` がファイルごとに全置換する。
  - ファイル `P` が**作られた・消えた**とき、`stem_of(P)` を持つ `_import_probes` の行から import している側を引き、**その範囲だけ**を解析し直して取り込み直す（全体の再走査はしない）。`index_file` / `remove` では `P` の取り込み・削除と**同じトランザクション**に入れる（`prepare_importers` で書き込みの前に事実を作り、`ingest_importers` で書く。`data_version` は 1 回だけ進む）。`scan` は最後に、この走査で取り込まなかった側を別のトランザクション（`data_version` は進めない。`_settle_relations` と同じ扱い）で解き直す。取り込み直した側は、同じ内容でも `exact` の行を失い `structural` に戻る（ADR-8。次に LSP が答えたとき戻る）。
  - 語幹の一致は「`P` を調べたパスと同じ語幹を持つ」という意味で、実際に `P` に解決されるとは限らない（大目に引く）。取り込み直した結果が前と同じなら行は変わらない。
  - 既存ファイルの編集でも、**シンボルの集合が変わった**（URN が足された・消えた）ときは同じ扱いにする（afb6717、BUG-0034）。import 元の行は、定義元に目標のシンボルがまだ無いと `_settle_relations` に捨てられ（`_ext` にもならない）、後から足されても元に戻らないため。**本文だけの編集（集合が同じ）では import 元に触れない**（編集のたびに解析し直す費用と `exact` の行を失う副作用を避ける）。
  - **再び公開した名前と名前空間パッケージ**（BUG-0100・BUG-0101、2026-10-31）: ファイルの取り込み・削除で、その「公開する名前」が変わった（`index/reexports.export_changes`。最上位のシンボルの増減・束ねた名前の先〔同じ名前は後のもの〕・`*` の先の並び・`__all__` の有無と名前・`__all__` がリテラルかどうか・TS / JS の公開する名前〔`export`〕の増減。削除はすべての名前。BUG-0104・BUG-0105）ら、その名前に依っていたファイル（`_reexport_deps`。`reexports.dependents`）も同じトランザクションで取り込み直し、取り込み直した側の公開する名前も変われば続けて引く（5 段まで）。連鎖の途中のファイルもすべて覚えているので、`pkg/sub/__init__.py` の再公開の先を変えても `pkg` 経由で import した側が 1 回で結び直る。公開する名前が変わらない編集（本文・コメント）では触れない。名前空間パッケージか調べたディレクトリは語幹 `?dir:<ディレクトリ>` で書き、ファイルが作られた・消えたら祖先のディレクトリの `?dir:` でも引く（ディレクトリの最初・最後のファイルで名前空間パッケージができる・無くなる）。
  - **pytest の設定**（BUG-0082、2026-10-31）: Python の絶対 import の根はプロジェクト（ワークスペースのルート・テストフィクスチャのルート）の pytest の設定（`pythonpath`・`--import-mode`）でも変わる（§7.4.6 項 1）。取り込むとき、使った設定の指紋を語幹 `?pytest:<プロジェクト>#<指紋>`（パスの語幹とは重ならない）として同じ表に書く。設定ファイル（`PYTEST_CONFIG_NAMES`: `pytest.toml`・`.pytest.toml`・`pytest.ini`・`.pytest.ini`・`pyproject.toml`・`tox.ini`・`setup.cfg`）が取り込まれた（中身が変わった）・作られた・消えたら、索引器の覚えた設定を捨てて読み直し、**いまと違う指紋で解いたファイルだけ**を同じ手順で解き直す（`stale_config_importers`。pytest に関係ない鍵だけの編集では何もしない。`exact` を失う副作用を最小にする）。指紋は索引に残るので、サーバーが止まっている間の編集も次の差分走査が設定ファイルを取り込み直したときに拾う。走査の始まりでは覚えた設定をすべて捨てる。
- **PageRank（`index/rank.py`、809eff4。実装で確定 2026-10-29）**: T-01 の `ranking: "pagerank"` と T-03 の同順位の並びの元。グラフは `relations` のうち `CALLS` / `REFERENCES` / `IMPORTS` / `INHERITS` / `IMPLEMENTS` の行で、**ワークスペース内のシンボル**（`is_external = 0`）を指すものだけを辺にする（`lk://_ext/` を辺にしないのは `Decimal` や `Exception` が上位を占めないため）。始点は参照箇所を含むシンボル、またはモジュール直下のコード（セレクタの無い URN）。自己ループは捨て、`(始点, 終点)` の組は種別・参照箇所の数に関わらず 1 本（重み 1）。テストファイルからの辺も数える（索引上の重要度は `include_tests` に依らない。T-01 が表示から外すだけ）。減衰係数 0.85、出次数 0 の頂点の質量は全頂点に均等配分、初期値は一様、最大 200 反復で L1 差が 1e-10 未満なら止める。頂点は URN の辞書順、辺も整列してから足すので**決定的**（同じ入力なら浮動小数の加算順まで同じ）。numpy は使わない（10 万シンボル・40 万辺で約 2.5 秒）。**シンボルのスコア**はワークスペース内のシンボルの PageRank をその最大値で割った値（最大 1.0、小数 12 桁）を**内部表 `_symbol_rank(urn, workspace_id, score, fan_in)`**（schema v2。`fan_in` は別ファイルからの辺の本数）に、**ファイルのスコア**は公開シンボル（定義は仕様書 T-01 処理規約 2。祖先に `priv` / `prot` があるものは除く。`PUBLIC_SYMBOL_SQL` の再帰 CTE、BUG-0124）の上位 5 件の和を最大のファイルを 1.0 とした値で `files.pagerank_score` に書く。T-01 の `max_symbols_per_file` は `_symbol_rank` から要求の件数で計算し直す。辺が 1 本も無いときは何も書かず、`files.pagerank_score` と `_symbol_rank` を空にする。T-01 はこれを「グラフ未準備」として `alpha` に降格する。計算は読み取り接続のスレッドで行い（書き手を止めない）、書き込みは短いジョブ 1 つで、**`data_version` は進めない**（並び順の付加情報で、カーソルの断面を変えない）。
- **PageRank の再計算のデバウンス（`engine/rank_scheduler.py`、bdacc4a・a4b1229）**: `Indexer.scan` は全体の取り込みの後に 1 回計算する。監視や編集ツールによる `index_file` / `remove` は 1 ファイルずつ来るので、そのたびに全体を計算し直すと重い。`RankScheduler.touch()` が「最後の更新から約 1 秒（`RANK_DEBOUNCE_S`）、次の更新が来なければ 1 回だけ再計算」を予約する（連続 10 回で再計算 1 回）。再計算の最中の更新は、終わった後にもう一度（さらに 1 秒待ってから）計算する（途中の取り消しはしない。書き込みジョブを中途半端に止めない）。`aclose()` で待ちも再計算も取り消す。再計算は走査のジョブの終わりを待つ（走査の最後の計算を古い結果で上書きしない）。失敗は握って記録する（順位は付加情報で、取り込みの成否に影響させない）。待ち（`sleep`）は差し替えられる。
- **Tier 3**: `FormatAdapter` は既定ではシンボルを返さず `files` 行だけを作る（`confidence = lexical_fallback`）。`symbols(source, *, rel, workspace_id)` が非空を返すアダプタ（JSON / YAML / TOML / XML。最上位の節だけ。§7.4.1）はそのシンボルも入れる（持てば `structural`）。
- 秘密ファイル・バイナリ・`MAX_FILE_BYTES` 超過・タグクエリの無い言語は、`files` 行だけ（本文は解析しない）か Tier 4 の `heuristic.outline` に回す。

並列度: 1 ファイルの解析（監視・先取り・編集）はスレッドプール（`READER_POOL_SIZE`=8 と同じ数）で行うが、Tree-sitter の解析は GIL を握るため並列化しない（ADR-13。S-2 で確認）。走査は解析の要るファイルが 64 を超えたら残りを子プロセスに並べる（§6.1。BUG-0127）。書き込みはキューで直列。バースト（`WATCH_BURST_FILES` 超）と起動時は `rescan` に切り替える。

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
| `repo_map(ws, scope, ranking, per_file)` | T-01 | `files.pagerank_score` と `_symbol_rank` は `relations` から Indexer が背景で計算して書く（§7.6.2。`index/rank.py` の `repo_map_files`。空なら `alpha`） |
| `data_version(ws)` | パイプライン・カーソル | |

性能の目安は「10 万シンボルで各関数 10 ms 以下」。S-9 のベンチで確かめ、超えるものは索引を足す。

### 7.6.4 公開クエリエンジン（public.py、仕様書 §13.2）

```python
class PublicQueryEngine:
    async def run(self, ws, sql: str, params: Mapping[str, Any], *, limit: int, timeout_ms: int,
                  grep: GrepSpec | None, explain: bool) -> QueryResult
```

手順:

1. 長さ ≤ `QUERY_LENGTH_MAX`（64 KB）。**末尾の `;` とコメントを除いてから**、先頭トークンが `SELECT` または `WITH` でなければ `E_QUERY_REJECTED(not_select)`（`SELECT 1;;` は複文扱い。素の `EXPLAIN` もここで拒否し、`explain=True` のときだけアプリが `EXPLAIN QUERY PLAN ` を前置する）。禁止語（`ATTACH`, `DETACH`, `PRAGMA`, `VACUUM`, `load_extension`, `readfile`, `writefile`, `fts5` の書き込みコマンド）を含めば `E_QUERY_REJECTED(forbidden_keyword)`。**この検査は省かない**（下の注記）。
2. 接続を `Path.as_uri()` に `?mode=ro` を付けた URI（`file:<paad.sqlite>?mode=ro`）で開く（`uri=True`、`isolation_level=None`、`timeout=1.0`。空白・日本語・`#` を含むパスも開ける）。**設定は次の順で行う**: (a) 手順 3 の `temp_grep_matches` の作成と投入（`grep` があるときだけ）→ (b) `PRAGMA query_only = ON` → (b2) `PRAGMA cache_size = -65536`（64 MiB）と `PRAGMA mmap_size = 268435456`（既定の 2 MiB のキャッシュでは、シンボルを主キーで引く結合・集計が 5 倍遅い。90 MB の索引で 9.8 秒 → 1.7 秒。BUG-0125。1 要求 1 接続なので確保は読んだ分だけで、閉じれば返る）→ (c) `setlimit(SQLITE_LIMIT_LENGTH, 16 MiB)` と `setlimit(SQLITE_LIMIT_SQL_LENGTH, 64 KiB)` → (d) 非公開名の写し `hidden`（`sqlite_master` と `sqlite_temp_master` の名前から公開名を除いたもの）→ (e) `set_authorizer(authorize)` → (f) `set_progress_handler(check_deadline, 1000)`。Python 標準の `sqlite3` にこれらが揃っていることを S-7 で確認した（合格。[`docs/spikes/S-07.md`](spikes/S-07.md)）。WAL の DB を別接続から `mode=ro` で読めることも確認した（書き手が開いている／閉じているの両方。書き手が 3 秒連続コミット中に新規接続 239 回でエラー 0）。**`immutable=1` は書き手の新しい行を見ないので使わない**。読み手は `-shm` を作る／開くため、キャッシュディレクトリに書き込み権限が要る（ディレクトリが作成不可で `-shm` が無いと `unable to open database file`）。`IndexWriter` は起動時にキャッシュディレクトリの書き込み権限を確認する。
3. `grep` があれば（2 の (a)）、接続ローカルの `CREATE TEMP TABLE temp_grep_matches(path TEXT, line INTEGER, snippet TEXT, confidence_level TEXT)` を作り、単語境界つき正規表現で走査した結果（上限 `GREP_MATCHES_MAX`=10,000）を入れる。`query_only=ON` の下では TEMP TABLE も作れない（`attempt to write a readonly database`）ので、**作成と投入を `query_only` と authorizer の設定より前に済ませる**（1 万行の投入込みで 23 ms。済ませたあとは読めて、書き込みは拒否される）。したがって authorizer に `setup_phase` フラグは要らない。
4. `explain=True` なら `EXPLAIN QUERY PLAN` を実行して `plan[]` を返し、本体は実行しない。
5. `params` の名前と `:name` の一致を検査（`E_INVALID_ARGUMENT`）。`execute(sql, params)`。Python の `sqlite3` は複文を拒否する（`ProgrammingError: You can only execute one statement at a time.`）ので、その例外は `E_QUERY_REJECTED(multiple_statements)` に写す。構文エラーは `E_QUERY_REJECTED(syntax_error)` に `details.sqlite_message`。`params` に余分なキーがあっても `sqlite3` は黙って受理するので、名前の突き合わせはアプリ側で行う。
6. `fetchmany(limit + 1)`。`limit + 1` 行目があれば `truncated=True`。期限超過は progress handler が中断し `E_TIMEOUT`（取得済みの行を部分結果に）。期限は、引数が無ければ索引ファイルの大きさ 1 MB あたり 60 ms（下限 300 ms・上限 10 秒。`default_timeout_ms`）、引数の上限は 30 秒（`QUERY_TIMEOUT_MAX_MS`。呼ぶ側の MCP の上限より短く。BUG-0125）。打ち切ったら、同じ接続で `EXPLAIN QUERY PLAN`（実行はしない）を引いて、`details` に `max_timeout_ms`・`can_extend`・`suggested_timeout_ms`（4 倍。上限まで）・`full_scans[]`（索引を使わず全件を読む表）・`hint`（書き直しの手がかり）を足す（`_advise_timeout`）。期限は **`time.perf_counter()`** で判定する（`time.monotonic()` は Windows の CPython 3.12 で分解能 15.6 ms あり、300 ms の期限が 310 ms にずれる）。中断は `OperationalError: interrupted` で、`guard.timed_out` により `E_TIMEOUT` に分ける。中断後も同じ接続を再利用でき、期限 300 ms に対し 300.05 ms で止まる。**再帰 CTE の深さ上限は SQLite に機構が無く実装できない**ので設けず、時間（`timeout_ms`、既定 300 ms）で止める（決定的な上限が要るなら progress handler で命令数を数える。MAY）。progress handler は SQL の命令単位でしか効かず、`hex(zeroblob(4e8))` のような単発の C 関数は止められない（1.2 秒・1.1 GB）ので、巨大値は 2 の `SQLITE_LIMIT_LENGTH`（16 MiB）で 0 ms の即エラーにする（12 MiB の `doc_full` は読め、20 MiB は `string or blob too big`）。初期の試走で p95≈300 ms の停止が出て原因は未特定なので、期限の設計には 1 要求ごとに最大 300 ms の余裕を見る（S-7 で確認）。
7. 接続を閉じる（1 要求 1 接続で足りる。開く費用は p50 1.3 ms。S-7 で確認）。接続は自動コミットで使い、使い終わったら `BEGIN` を持ち越さない（読み取りトランザクションを保持した接続は `wal_checkpoint(TRUNCATE)` を妨げる）。

authorizer の判定表（`sqlite3` の定数名）:

| 操作 | 判定 |
|---|---|
| `SQLITE_SELECT`, `SQLITE_RECURSIVE` | 許可 |
| `SQLITE_READ(table, column)` | `table` が公開 10 テーブル・`symbols_fts` とその影テーブル（`symbols_fts_config` / `_data` / `_docsize` / `_idx`。FTS5 の内部クエリにも authorizer が呼ばれる）・`json_each` / `json_tree`・`temp_grep_matches` なら許可。列を読まない参照（`count(*)`、CTE・サブクエリの別名）は `READ(名, '', None)` で届くので、DB に実在する非公開名（`_` 始まり、`sqlite_`、`pragma_`、起動時の `sqlite_master` の写し）だけ拒否し、それ以外は別名として通す（副作用: `WITH _meta AS (SELECT 1) SELECT count(*) FROM _meta` も拒否される。許容）。それ以外（`_` 始まりの内部テーブル、`sqlite_master` を含む）は拒否 |
| `SQLITE_FUNCTION(name)` | 組み込みの集計・文字列・日付・JSON・FTS5 関数と Lucianke 登録の読み取り専用関数なら許可。`load_extension`, `readfile`, `writefile`, `edit`, `fts5` の書き込み系は拒否 |
| `SQLITE_PRAGMA(data_version)`・`SQLITE_PRAGMA(query_only)`（引数なしの読み取り） | 許可（`data_version` は FTS5 の `MATCH` が内部で読む。拒否すると `authorization denied` で失敗する。`query_only` は読み取りだけ。利用者が書いた `PRAGMA` は手順 1 の禁止語検査で先に拒否される。実装で確定 2026-10-29） |
| それ以外（INSERT / UPDATE / DELETE / CREATE / DROP / ALTER / ATTACH / DETACH / PRAGMA（`data_version` と `query_only` の読み取りを除く）/ TRANSACTION …） | 拒否。`ATTACH`（24）は `SQLITE_FORMAT` と数値が衝突するので名前で扱う |

FTS5 の内部読み取りと利用者の `SELECT * FROM symbols_fts_data` は authorizer で区別できない（内容は公開列から導出した索引なので害は無い）。

拒否は `E_QUERY_REJECTED(non_public_object)` または `(forbidden_keyword)` に写す。例外は `sqlite3.DatabaseError` 1 本で受け（`OperationalError` はその子）、`guard.denied`（`non_public_object` / `forbidden_keyword`）と `guard.timed_out`（`E_TIMEOUT`）で分ける。`ProgrammingError` は `multiple_statements`、または bind 不足の `E_INVALID_ARGUMENT`。

実装は静的検査（手順 1）を含めて **4 段**（静的検査 → `mode=ro` → `query_only` → authorizer）で守る。このうち `mode=ro`・`query_only`・authorizer の 3 重は、**DB 本体への書き込みについて、どれか 1 つが欠けても他が効く**（MUST。テストでは 1 つずつ外して書き込み文が通らないことを確かめる。S-7 で 25 種の書き込み系の文 × 層の 8 通りを DB ファイルの前後比較で確認）。ただし副作用は別で、次を既知として明記する: ① `ATTACH` は `mode=ro` + `query_only` でも**任意パスに 0 バイトのファイルを作り**（`mode=ro` だけなら ATTACH 先へ書ける）、② `query_only` 単独は `PRAGMA journal_mode` で DB を変更でき、`PRAGMA query_only=OFF` で自分を外せる（`mode=ro` は外しても拒否のまま）。副作用を止められるのは authorizer と手順 1 の禁止語検査だけなので、**禁止語検査は省かない**。テストには「層を 1 つ外す」ことに加え、「authorizer を外した状態では `ATTACH` が 0 バイトのファイルを作る」ことを既知の挙動として書く。

受入で確かめる点:

- `INSERT` / `UPDATE` / `DELETE` / `CREATE` / `DROP` / `ALTER` / `ATTACH` / `PRAGMA` を先頭に持つ文、複文、`_meta` を参照する文が `E_QUERY_REJECTED` で返り、DB ファイルの `sha256` が変わらない。
- 無限再帰の CTE が `timeout_ms` 以内に `E_TIMEOUT` で返る。
- 3 重防御のうち任意の 1 つを無効化したビルドでも、DB 本体への書き込み文が拒否される（`ATTACH` による空ファイルの作成は authorizer と禁止語検査で止める。上の注記）。

### 7.6.5 スキーマ版管理と起動時の差分索引

- `_meta(schema_version INTEGER, data_version INTEGER, edit_seq INTEGER, lucianke_version TEXT, created_at TEXT)`。`schema.sql` の先頭コメントに `schema_version`（現在 3）を書き、`migrate.py` が前方向の移行を順に適用する（v1→v2: `_symbol_rank` を足す。v2→v3: `_import_probes` を足す。値は各ファイルの次の取り込みで入る）。移行が用意されていない古い版は DB を作り直す（PaaD はキャッシュであり、再索引で復元できる。MUST）。**作り直しは DB ファイルの削除ではなく、書き込み接続の中で全オブジェクト（テーブル・ビュー・トリガ・仮想テーブル・索引）を `DROP` して `schema.sql` を流し直す**（Windows では読み取り接続が開いている DB ファイルを削除・改名できないため）。`_meta.edit_seq` は作り直しをまたいで引き継ぐ（`edit_id` を再利用しない。実装で確定 2026-10-29）。
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
| **Reindexed** | Written | `TreeCache` の旧キー無効化、§7.7.4 で得た構文木を新キーで登録、`index/ingest.py` に同期で取り込ませ `data_version` を進める、`lsp/documents.py` に `didChange`（未オープンなら `didOpen`）、取り込めたファイルは `Watcher.expect(path, sha_after)` で自分の書き込みイベントを抑止し、取り込めなかったファイルは `Watcher.forget(path)`（こだまを監視の通常の道で取り込ませる。BUG-0087）。**ロック解放** | ログのみ（応答は続行。`warnings` に `reindex failed for …`） | 新内容 |
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
- hunk の範囲が重なっていれば計画は不正。`E_INVALID_ARGUMENT`（`details.reason: "overlapping_edits"`）とし、ログに計画を残す（実装で確定 2026-10-29。旧版は `E_UNSUPPORTED` と書いていた。重なりは計画の誤りで、機能が無いのではないため）。
- 位置の変換は `LineIndex`（`core/textpos.py`）だけが行う。LSP の位置（UTF-16 または交渉した UTF-8 単位）→ コードポイント（`LineIndex.from_lsp`）、コードポイント → バイトオフセット（`LineIndex.to_byte`。Tree-sitter 用）。
- `WorkspaceEdit` → `Plan` の変換（`plan.from_workspace_edit`）:
  1. `documentChanges` があればそれを、無ければ `changes` を使う。
  2. `TextDocumentEdit` の URI を `workspace/paths.py` でルート相対に直す。ルート外なら `E_INVALID_ARGUMENT`（`details.reason: "edit_outside_workspace"`。実装で確定 2026-10-29。旧版は `E_UNSUPPORTED`）。同じ文書への `TextDocumentEdit` が 2 つ以上あるときは `E_UNSUPPORTED`（`details.reason: "repeated_document_edit"`。同じ文書への編集が複数あると座標が逐次〔前の編集を適用した後の座標〕になり、適用前座標の hunk にできないため。改名後の URI への編集は旧パスの文書として数える）。
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
5. 判定: `had_errors_before == False` ならファイル全体で新集合が空であること。そうでなければ、**編集範囲（適用後座標。前後 1 行を含む窓）の中のエラーノードの個数**が編集前の同じ窓の個数を超えないこと。エラーノードの比較は位置の一致ではなく窓の中の個数で行う（Python は構文が壊れると位置の粗い 1 個の大きな ERROR になり、位置では比べられない。S-2 で確認。窓の外のエラー〔もともとあった別の箇所の壊れ〕は問わない。窓は編集範囲と実際に変わった行を覆う区間の前後 1 行。実装で確定 2026-10-29）。**既知の盲点**: 窓の中の既存の壊れを直しつつ、同じ窓の中に別の壊れを 1 つ足すと、個数が変わらず見逃す（`had_errors_before == False` の場合はファイル全体で検査するのでこの盲点は無い）。
   - 解析が期限（`parse` の期限。ADR-13）を超えたときは、検証できなかったものとして結果を `failed` にする（構文が正しいとは見なさず、書き込みへ進まない。実装で確定 2026-10-29）。
6. 違反は `E_SYNTAX_INVALID`。`details.errors[]` は `{line, message}` を最大 10 件（`line` は適用後の 1 始まりの行、`message` は `syntax error: missing "<ノード型>"`（MISSING）または `syntax error near '<該当行の先頭の抜粋>'`（ERROR）。仕様書 §5.8 は元から `{line, message}`。実装で確定 2026-10-29。旧版は `{path, line, col, kind, node_type, snippet}` の 6 項目だった）。`operation == "replace_body"` で `new_code` の先頭行が定義の開始語（`def` / `class` / `async def` / `function` / `fn` / `pub fn` / `impl` / `struct` / `enum` / `interface` / `type` / `export`）で始まらないときは `details.hint` に「`replace_body` はシグネチャを含む定義全体を渡す」を入れる。
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
3. **段階 A（一時ファイル）**: 各ファイルについて同じディレクトリに `.lucianke-tmp-<edit_id>-<乱数 6 桁>` を作り、**バイナリで**（`open(path, "wb")`。テキストモードの `"w"` は `\n` を `\r\n` に変える）書き込み → `flush` → `os.fsync(f.fileno())` → **close してから**段階 B へ進む（tmp を開いたままだと改名が winerror=32 で失敗する）。POSIX ではモードを元に合わせる（`os.chmod`）。Windows では tmp に読み取り専用属性を付けない（改名で target に移る）。対象が読み取り専用属性を持つ場合は、Resolved 段で `st_file_attributes & FILE_ATTRIBUTE_READONLY` を検査して `E_MUTATION_BLOCKED`（`details.reason: "read_only_file"`）とし、何も書かずに返す（利用者が付けた保護を黙って外さない。読み取り専用は再試行では直らず、ハンドルを開かれている場合と同じ winerror=5 になる）。
4. **段階 B（改名）**: すべての一時ファイルが書けてから、`os.replace(tmp, path)` を順に行う。`renames` はテキストの改名が終わったあとに `os.replace(old, new)`。
5. **Windows の共有違反**: `os.replace` が `PermissionError` を返し、かつ `e.winerror in {5, 32}` のとき（5 = アクセス拒否: target のハンドル保持、32 = 共有違反: tmp 側のハンドル保持。Windows のみ。他の `OSError` は即失敗）、**10, 20, 40, 80, 160 ms**（合計 310 ms）の間隔で再試行する（初回＋再試行 5 回の最大 6 試行。費用は失敗時だけで、書き込みロックの保持が最大 310 ms 延びる）。複数ファイルの改名全体では上限 1 s で打ち切ってロールバックする。それでも失敗なら 6 へ。20 ms × 5 回（約 100 ms）では、相手が 50 ms 保持するなら成功するが 90 ms では 3/5 に落ちる。10〜160 ms の間隔なら 200 ms の保持まで 5/5 で成功する。`os.replace` は、相手が共有 DELETE を許していても、ハンドルが 1 つでも在れば失敗する（S-10 で確認）。
6. **ロールバック**: 段階 B で失敗したら、改名済みのファイルを `pre/` から同じ手順（一時ファイル → 改名）で戻し、残った一時ファイルを消し、`manifest.state = "rolled_back"` とする。`E_INTERNAL`（`details.rolled_back: true`, `details.log_ref`）。戻し自体に失敗したら `details.rolled_back: false` と `details.journal: <manifest のパス>` を返し、ログに `error` で残す。
7. **確定**: 全改名が終わったら各ファイルの sha を再計算して返し、`manifest.state = "committed"`。
8. POSIX ではディレクトリの `fsync` を行ってもよい（MAY）。Windows では行わない（ディレクトリを `os.open` で開けず `PermissionError` になる。`CreateFileW` + `FlushFileBuffers` なら可能だが、得られる保証が小さい。S-10 で確認）。

- 任意（MAY）: 再試行を使い切っても失敗し、相手が共有 DELETE を許す型（Node.js 系のエディタなど）なら、POSIX 意味論の改名（`SetFileInformationByHandle(FileRenameInfoEx, REPLACE_IF_EXISTS | POSIX_SEMANTICS)`、Windows 10 1709 以降）を 1 回だけ試す。`os.replace` はこの相手に勝てない。M1 では入れない。
- 改名方式で**保持されないもの**: mtime は tmp の書き込み時刻になり、NTFS の作成時刻・明示 ACE・代替データストリームは失われる（tmp が親から継承した ACL になる）。保持するのは改行コード・BOM・末尾改行・（POSIX の）実行ビット（仕様書 §10.6）。
- テスト（§10.1 unit の `writer`）: `CreateFileW` の共有 0 でハンドルを保持して `Timer` で閉じる（S-10 の `run.py` 項目 8 と同じ型）ことで、再試行成功・再試行切れ・target 無傷を決定的に再現できる。Windows 以外は skip。

### 7.7.6 診断差分（verify.py）

仕様書 §10.7 の実装。

1. **編集前スナップショット**（Resolved で取る）: 対象ファイルと **直接の参照元ファイル** について `diagnostics` を読む。参照元は `relations` で `target_urn ∈ Plan.target_urns` かつ `source_file_id ≠ 対象` の `source_file_id` を、関係行数の多い順に最大 20 ファイル。それぞれの行を同一性キー `(file, source, code, message, enclosing_urn)` で辞書にする。行番号は使わない。
   **読む前に基準を待つ**（BUG-0098）: 書き込みをする呼び出し（`dry_run`・インターロックの強制 dry_run・`diagnostics_wait_ms == 0` を除く）では、各ファイルのいまの内容を LSP に見せ（`documents.sync`。同じ内容なら何も送らない。参照元もここで開く）、サーバーごとに「その版の診断を受信し、表に書き終えた」まで並べて待つ（上限は `diagnostics_wait_ms`。書き込み後の待ちとは別枠。出ていれば即戻る。ロックの外）。結果は `Snapshot.baseline = {path: {サーバー名: 揃ったか}}`。サーバー名は `diagnostics.source` と同じ `server_name` で、口が `wait_sources`（`lsp/documents.RoutedDiagnostics`。`wait_for` と同じ待ちをサーバーごとの結果で返す）を持てばサーバーごと、持たなければ `{"*": 揃ったか}`（`"*"` = そのファイルの LSP すべて）。LSP に見せられなかったファイルは `{"*": False}`、表を読めなかったら `Snapshot.failed`（どのファイルも基準が不明）。理由: 負荷や起動直後に LSP がまだ診断を出していないと、表にその行が無く、編集後の前からある診断を `added` と答えていた（`complete`・`exact`。呼び手の AI は自分の編集が壊したと読む）。
2. **対象ファイルの開き直し**: Reindexed で `lsp/documents.py` が対象ファイルに `didChange`（版 `v_n`）を送り、`workspace/didChangeWatchedFiles`（`type: 2`）を送る。**`didChangeWatchedFiles` が必須**: basedpyright はディスクの書き換えだけでは依存元の診断を更新せず（3 秒待っても無し）、送ると 40 ms で再発行する（S-4 で確認）。参照元ファイルの診断を取りたいときは、LSP が開いていなければ `didOpen` する（`diagnosticMode: openFilesOnly` では開いた文書の診断しか来ない）。`references` / `rename` / `callHierarchy` のために参照元を開く必要は無い（未オープンの文書も対象になる）。**ただし TypeScript の 2 サーバー（tsserver）は例外**: `tsconfig.json` の `include` に入らないファイル（ky の `test/` など）は、開かれるまでどのプロジェクトにも入らず結果に出ない（BUG-0047）。T-06 の `exact` 経路は、対象の名前を単語境界で含む tsserver 系のファイル（索引の Tier 1〜2、LSP が未オープンのもの、テスト優先、1 回に 100 件まで）を先に `didOpen` してから `references` を聞く（`tools/_lsp_exact.prime_candidates` / `lsp/features.open_documents`）。上限を超えた分は `warnings[]` に書く。開いていないファイルの診断を TypeScript の 2 サーバーが再発行するかは未確認（S-5）。
3. **待ち**: `DiagnosticsWaiter.wait(paths, min_versions, timeout_ms=diagnostics_wait_ms)`。各パスについて「版 ≥ `v_n` の `publishDiagnostics` を受信した」ことを条件とする。版が付かない LSP では「`didChange` 送信時刻より後の受信」を条件にする。参照元は `didChangeWatchedFiles` 送信後に受信した最初の診断＋ 300 ms の静止（§7.5.5 の 2 (b)。版が旧いまま届くため版では判定できない）。`diagnostics_wait_ms == 0` なら待たず 5 へ。
4. **受信側**: `lsp/diagnostics.py` は受信のたびに `diagnostics` テーブルの当該ファイル行を全置換する（`IndexWriter`）。待ちの完了は、この書き込みが終わった後に通知する（EP-7: 直後の `get_diagnostics` と整合させるため）。
5. **差分**: 揃ったファイルについて、現在の `diagnostics` 行を同じキーで辞書にし、`added = after − before`、`resolved = before − after`。`added[]` / `resolved[]` の要素は T-08 の診断オブジェクトと同形（行番号は現在値）。**基準が不明なサーバー**（編集の前後どちらかで待ったサーバーのうち、編集前に揃わなかったもの。`"*"` は索引の source〔`tree-sitter`・`lucianke-*`〕以外すべて）の行は比べず、編集後の行を `present_after[]` に入れる（`resolved` も判定しない）。前後どちらでも待たなかった source（索引の行・止まったサーバーの古い行）は前後で同じなので、ふつうに比べる。編集の前に無かったファイルの基準は空で確か。
6. **状態**: 対象ファイルと参照元がすべて揃えば `"complete"`、1 つでも揃わなければ `"pending"`（揃った分だけ入れ、`checked_files[]` に `{path, fresh: bool}`）。揃っていても基準が不明なサーバーがあれば `"partial"`（`pending` が優先）で、そのファイルの `checked_files[]` に `unknown_baseline: [サーバー名]`、`warnings` にサーバー名と次にすること（`present_after` の位置と diff を見比べる・`get_diagnostics`）を書く（`edit/verify.compute_delta`・`edit/transaction._unknown_baseline`）。`dry_run` で予測診断を行わなかったときは `"not_computed"`。
7. **LSP 不在**（`unavailable` / `standby` で起動不能 / Tier 2 の一部）: `tree-sitter` 由来の構文エラー診断（§7.7.4 の集合を T-08 の形にしたもの）だけで差分を取り、`checked_files` は対象ファイルのみ、`confidence = STRUCTURAL`。
8. **予測診断**（`dry_run` の MAY）: M1 では行わず常に `"not_computed"`。M2 以降、LSP が書き込みなしのオーバーレイ（`didChange` のみ）に対応することを確かめたうえで足す。

```python
@dataclass(frozen=True)
class DiagDelta:
    status: Literal["complete", "partial", "pending", "not_computed"]
    added: tuple[Mapping[str, Any], ...]
    resolved: tuple[Mapping[str, Any], ...]
    checked_files: tuple[Mapping[str, Any], ...]   # {path, fresh[, unknown_baseline]}
    confidence: Confidence
    warnings: tuple[str, ...]
    present_after: tuple[Mapping[str, Any], ...]   # 基準が不明なサーバーの編集後の診断（BUG-0098）
```

### 7.7.7 編集ツールからの使い方（T-10〜T-13 の plan_builder）

| ツール | `plan_builder` が作るもの | 固有の前段 | 再索引後の対応付け |
|---|---|---|---|
| T-10 `edit_symbol_body` | `replace_body`: `syntax/boundaries.py` の定義範囲（デコレータ・属性・直前 Doc コメント込み）を 1 hunk で置換。`insert_before` / `insert_after`: 定義範囲の直前・直後（行頭 / 行末の改行後）に空の範囲で挿入。`insert_member`: 親の最後のメンバの定義範囲末尾（無ければ本体先頭。Python の `pass` / `...` だけの本体はその文を置換）。`delete_symbol`: 定義範囲 + 行末の改行 + 規定数を超える後続空行 | `new_code` を §7.7.3 で正規化。`insert_member` の対象が本体を持たなければ `E_INVALID_ARGUMENT`。降格ファイルで終了行の候補が 2 つ以上なら `E_MUTATION_BLOCKED` | 再索引後、hunk の適用後開始行（デコレータを除いた最初の行）から始まるシンボルを `symbols` で引き、その URN を `target_urn`、範囲を `new_range` にする。`delete_symbol` は `None` |
| T-11 `rename_symbol` | `prepareRename` → `rename(newName)` → `WorkspaceEdit` → `plan.from_workspace_edit` | LSP が `ready` でなければ `E_LSP_UNAVAILABLE`。`new_name` が識別子でなければ `E_INVALID_ARGUMENT`。降格モード（字句置換）では対象ファイル内に `new_name` が単語として既出なら `E_MUTATION_BLOCKED` | Resolved で対象ファイル群の `symbols` を `(path, selection 開始位置)` つきで控え、再索引後に `LineIndex.shift`（前方 hunk の行・列ずれ）で位置を写像して突き合わせ、`qualified_name` が変わった全シンボルの旧 URN → 新 URN を `urn_map` にする |
| T-12 `structural_search_replace` | `syntax/structural.py` の一致ごとに `(range, replacement)` → hunk。`rewrite` 無しなら計画を作らず読み取りのみ | `pattern` / `rewrite` の解析失敗は `E_SYNTAX_INVALID`。一致数が `max_matches` を超えたら計画を作らず `applied: false`。`dry_run` 既定 `true` | 不要（`matches[]` を返す） |
| T-13 `apply_code_action` | `codeAction`（または保持中の ID）→ 必要なら `codeAction/resolve` → `edit` → `plan.from_workspace_edit` | ID の発行後に sha が変わっていれば `E_PRECONDITION_FAILED`（`code_action_id_expired`）。`command` だけのアクションは `E_UNSUPPORTED`。種別 `quickfix` で、文脈の診断の待ち（`CONTEXT_DIAGNOSTICS_WAIT_MS` = 2,000 ms）の間にいまの内容の診断を出さなかったサーバーがあり候補が 1 件以下なら `E_TIMEOUT`（`diagnostics_pending`・`server` / `servers`・`retry_after_ms` = 1,000。1 件は ID つきで `candidates[]`、自動では適用しない）、2 件以上は `E_TARGET_AMBIGUOUS` に `details.diagnostics_pending`（BUG-0099。0 件・1 件は確かめられていない） | 不要。`target_diagnostic_resolved` は `DiagDelta.resolved` に ID の診断キーが含まれるかで決める |

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
- Tier 2 のアダプタ（`.ipynb`、SQL、IDL）は「文法あり」として扱い、LSP の列は当該言語の LSP（`.ipynb` は Python）に従う。Tier 3 のアダプタは **LSP の状態によらず** 常に `structural` で、`lsp_status` は `n/a`（実装で確定 2026-10-29）。
- 表に `standby` の列は無い。LSP が `standby` のとき、遅延起動の契機になる能力（`ready` 列のエンジンが `lsp` または `lsp-cache` のもの: DEFINITION・REFERENCES・IMPLEMENTATIONS・CALL_HIERARCHY・DIAGNOSTICS・RENAME・CODE_ACTION）は **`indexing` の列** で答え、`lsp_status` は `indexing`（起動を投げる）。契機にならない能力（SKELETON・SEARCH・BODY・STRUCTURAL_EDIT・SYMBOL_EDIT）は **`unavailable` の列のエンジン** で答え、`lsp_status` は `standby` のまま（LSP を起こさない。実装で確定 2026-10-29）。
- Tier 4 の列の IMPLEMENTATIONS は `unavailable`（提供しない）、DIAGNOSTICS は 0 件、CALL_HIERARCHY は grep 1 段、RENAME は字句置換（インターロック下）。仕様書 §8.2 の表はこれに揃えてある。
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

- `is_degraded = choice.confidence < spec.max_confidence`（`ToolSpec.max_confidence`。T-12 は `STRUCTURAL`、T-00 / T-19 は常に `EXACT` で降格なし、T-20 は行から集計。T-04 は `EXACT`〔LSP の hover で `exact` に上げる。76a2aca〕。T-01・T-02・T-03・T-05 の最高水準は `STRUCTURAL`）。
- `accuracy_warning` は `is_degraded` のときだけ非 null。雛形は `engine/selector.py` に置き、`reason` で選ぶ:

| `reason` | 水準 | 雛形（日本語） |
|---|---|---|
| `lsp_indexing` | structural | 「LSP が起動・索引中（{progress}%）のため構文木で解決しました。型・オーバーロード・再エクスポート越しの参照は区別されていません。完了後に呼び直すと `exact` になります。」 |
| `lsp_unavailable` | structural | 仕様書 §8.4 の `structural` 雛形 + 「`configure_workspace status` で LSP の状態を確認してください。」 |
| `lsp_timeout` | structural | 「LSP の応答が {timeout} ms 内に得られなかったため構文木で解決しました。」 |
| `position_clamped`（理由別の警告ではなく `engine/selector.py` の定数 `POSITION_CLAMPED_WARNING`） | structural | 「解決する位置が文字の境界に当たらないため LSP に要求を送らず構文木で解決しました。型・オーバーロード・再エクスポート越しの参照は区別されていません。」LSP は動いているので `lsp_status` は `ready` のまま、`lsp_unavailable` の案内（`configure_workspace status`）は付けない（9259466。実装で確定 2026-10-29） |
| `lsp_error`（`engine/selector.py` の `lsp_error_warning`。`details` の `server` / `method` / `lsp_message` から組む） | structural | 「LSP（{server}）が {method} の要求にエラーを返した（{エラー文}）ため構文木で解決しました。型・オーバーロード・再エクスポート越しの参照は区別されていません。」LSP は動いているので `lsp_status` は `ready` のまま、「利用できない」とは言わない（以前は `lsp_unavailable` の雛形で、`ready` なのに「LSP が利用できない」と書いていた。BUG-0090、実装で確定 2026-10-31） |
| `restarting`（`engine/selector.py` の `lsp_restarting_warning`） | structural | 「LSP（{server}）が {method} に以後答えられない状態になった（{エラー文}）ため、LSP を再起動しています。今回は構文木で解決しました。型・オーバーロード・再エクスポート越しの参照は区別されていません。再起動の完了後に呼び直すと `exact` になります。」`lsp_status` は `indexing`（§7.5.2 の `record_broken`。BUG-0090、実装で確定 2026-10-31） |
| `no_grammar_ctags` | heuristic | 仕様書 §8.4 の `heuristic` 雛形 |
| `no_grammar_lexical` | lexical_fallback | 仕様書 §8.4 の `lexical_fallback` 雛形 |
| `mixed` | （最低水準） | 上の雛形に「{n} 件のファイルがこの水準です（内訳は `confidence_breakdown`）。」を足す |

compact の `! DEGRADED: confidence=… engine=… lang=…` 行と 2 行目の `! Warning:` は `mcp/envelope.py` が `EngineChoice` から組み立てる。ツールは書かない。

## 7.9 Git Layer（git/）

### 7.9.1 M1 の範囲: リビジョン解決と仮想ツリー（vtree.py、仕様書 §8.5）

dulwich（Apache-2.0 側を選択。第 16 章、OD-20）を M1 の第一候補として維持する（S-8: M1 の用途である参照解決・blob・`STAGED`・`.gitignore`・不変性は合格で 1〜5 ms）。M1 で必要なのは次の 3 つだけ。

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

`resolve_revision` が受けるもの: `WORKING_TREE`、`STAGED`、`HEAD`、`HEAD~N` / `HEAD^`、ブランチ名（`refs/heads/`）、タグ名（`refs/tags/`、注釈付きタグは剥がす）、リモート追跡名（`refs/remotes/`）、40 桁 SHA、4 桁以上の一意な短縮 SHA。それ以外の構文（`@{...}`、範囲 `a..b`、`:path`）は `E_REVISION_NOT_FOUND`（`details.hint: "対応する指定: WORKING_TREE, STAGED, HEAD, HEAD~N, ブランチ, タグ, SHA"`）。

実装は**許可リスト方式**にする（S-8 で確認）: 前置検査で `@{…}`・`a..b`・`:path`・`^{}`・空白や `:?*[\` を含む名前を拒否し（dulwich の `parse_object` は `HEAD@{1}`・`HEAD:README.md`・`^{}` を受理してしまい、短縮 SHA を基点にした `a9477ab~1` は解せない）、基点を `parse_commit` で解決してから `~N` / `^N` を `parse_object` で辿る。`KeyError`（無い名前・3 桁以下の SHA）・`ValueError`（`HEAD~9999`・親が足りない・タグがツリーを指す）・`objectspec.AmbiguousShortId`（`Exception` 直下で `KeyError` ではない）の 3 つを `E_REVISION_NOT_FOUND` に束ねる（曖昧は `details.reason: "ambiguous"`）。4 桁以上の短縮 SHA の解決は 15,757 オブジェクトの規模（pallets/click）でも約 5 ms なので、`git_commits` を先に引く最適化は M1 では要らない。

`GitRepo` は **`Repo` を保持しない**。要求ごとに `with Repo(root)` で開閉する（開き直しは +1〜3 ms。pack を開いたままだと、利用者の `git gc` / `repack` が Windows で `winerror=32` で失敗する）。`open()` は `Repo.discover` ではなく `Repo(root)` を使い、`NotGitRepository` を捕まえて `None` を返す（モノレポのサブディレクトリを作業ルートにするときのパス前置は M2）。`read_blob` は `lookup_path` の `KeyError` / `NotTreeError` / `SubmoduleEncountered`、および結果が `Tree`（ディレクトリ）のときに `None` を返す（ツリーのパスは UTF-8 の `bytes`）。`read_staged_blob` は `repo.open_index()[path]` を読むだけで（ロックも書き込みもしない）、競合中の `ConflictedIndexEntry` は `this` を採り、未追跡は `None`。

`VirtualTree`（要求の間だけ生きる）:

1. `VirtualTree.open(ws, resolved)` はメモリ上の SQLite（`:memory:`）に `index/schema.sql` と同じ DDL を作る。永続 DB には触れない（MUST）。
2. `source(path) -> bytes` は `read_blob` / `read_staged_blob` で取り、`TreeCache` にはキー `(ws, path, sha256(blob))` で載せる（内容ハッシュなので作業ツリーの同内容と自然に共有される）。
3. 要求されたパスだけを解析して `symbols` に入れる（遅延）。T-05 の `symbol_name` 解決は、この一時 `symbols` に対して §7.1.3 の規則を適用する。
4. `ToolContext.file_source(path)` は `revision` が `WORKING_TREE` なら作業ツリー、それ以外なら `VirtualTree` を見る。T-02 / T-05 はこの 1 つの入口だけを使い、`revision` を意識しない。**実装との差（実装で確定 2026-10-29、de291be、BUG-0018）**: ツールの入口は `tools/_rev.py`（`open_revision`: `revision` を解決して `RevisionView` にする。作業ツリーのときは Git を一切開かず、dulwich の import〔約 130 ms〕を作業ツリー以外のときだけ遅延 import にする。`read_checked`: 断面から 1 ファイルを読み、封じ込め → 読み込み → 大きさ・秘密・バイナリの検査〔`E_TARGET_NOT_FOUND` / `E_UNSUPPORTED`〕を作業ツリーと同じ規約で行う。`analyze_at`: 作業ツリーは `analyze_file` にそのまま任せ、過去断面は読んだバイト列を同じ手順で構文木にかける〔`TreeCache` のキーは内容の SHA-256 を含むので作業ツリーの同じ内容の木と共有される〕）。T-02 の種類の振り分けは `tools/_skeletons.py`（データ形式 → コード → Markdown・rst の見出し `[Section]` → Tier 4 の `block_outline`。`ctags` は起動しない）。**M1 では 1 と 3 のメモリ上の SQLite は作らない**: 過去断面の解析結果は `FileAnalysis.symbols`（メモリ上のリスト）で `symbol_name` を解決し、PaaD にも一時テーブルにも書かない（`:memory:` の一時 `symbols` は M2 の `query_project_db` の `revision` 対応で足す）。Git のリポジトリでないワークスペースでの `WORKING_TREE` 以外は `E_REVISION_NOT_FOUND`（`details.reason: "no_git_repository"`）。
5. 応答のヘッダは `rev=<display>`、`engine_used = "tree-sitter@<短縮 SHA>"`、最高水準 `structural`（LSP を使わない）、`accuracy_warning` に「過去断面のため LSP を使わず構文木で解析した」。
6. パス名は `workspace/paths.py` の封じ込めを通す。Git のツリー項目に `..` や絶対パスが含まれていれば拒否する。
7. 検査: 任意の `revision` 指定の読み取りの前後で、作業ツリー全ファイルの sha と `.git` 配下の sha が変わらない（M2-5 の受入をこの形で機械化する。M1 でも T-02 / T-05 に対して走らせる）。`porcelain.status` を使うときは **`optional_locks=False` を MUST** とする（既定の `True` は、stat だけが変わったファイルがあると `.git/index` を書き換える。S-8 で確認）。機械検査は `git status` を先に 1 回流してから `index` の sha を測る（`git status` 自体も index を更新するため）。

M1 で `revision` を受けるのは T-02 と T-05 だけ（仕様書 §8.5）。他の読み取りツール（T-03・T-04・T-06・T-07・T-08）は、`tools/_rev.py` の `reject_unsupported_revision` が先に `revision` を解決し、存在しない参照・不正な構文は `E_REVISION_NOT_FOUND`、解決できた参照は `E_UNSUPPORTED`（`details.reason: "revision_not_supported_until_M2"`）を返す。

### 7.9.2 M2 の境界: 履歴・共変更（history.py、cochange.py）

内部設計は M2 着手時に本書を改版して足す。ここでは境界と不変の規約だけを置く。

- `history.symbol_changes(repo, path, *, since, max_commits) -> list[SymbolChange]`: ファイル名変更を追跡した履歴（`git log --follow` 相当。**dulwich の `Walker` は使わない**: F-PY 規模の pallets/click で `src/click/core.py`〔git は 431 件、改名 2 回〕に 13〜17 秒かかり、件数も git と食い違う〔`git log --follow` は 106 ms〕。自前の path 限定 walk〔親との `(mode, sha)` 比較〕でも無制限で 1.8 秒、`max_commits=500` に区切って 392 ms。S-8 で確認）を新しい順に辿り、各コミットの前後の blob を `SyntaxEngine` で `SymbolRecord` 集合にして、`(qualified_name, kind)` で突き合わせ、`body_sha256` の異同と正規化シグネチャの異同で `ADDED` / `DELETED` / `MODIFIED_SIG` / `MODIFIED_BODY` / `RENAMED` を決める（仕様書 T-17 処理規約 3）。結果は `git_symbol_changes` に `IndexWriter` 経由で追記し、2 回目以降は索引済み範囲を飛ばす。
- `cochange.aggregate(repo, *, window=CO_CHANGE_WINDOW)`（`CO_CHANGE_WINDOW = 500`）: 直近 `window` コミットの変更ファイル集合から対 `(a, b)` の共起回数と `a` の変更回数を数え、`git_co_changes` を全置換する。シンボル単位は `git_symbol_changes` から同様に。`HEAD` の移動（`.git/HEAD` と `refs/` の監視、または要求時の照合）で増分更新する。
- Git 層は作業ツリーを書かない（MUST）。シェルの `git` は起動しない（SHOULD NOT）。**例外は履歴専用の読み取り専用 `git` 子プロセスだけ（OD-20 で決定。師匠 2026-10-29「推奨: 読み取り専用で使うことを認る」）**: `git log --follow --format=%H -n <max_commits> -- <path>` と `git diff-tree -r -M50% --name-status <a> <b>` を、`LC_ALL=C`・`core/procs` 経由で起動する。書き込み系のコマンドは起動しない（MUST NOT）。`git` が無い環境（PATH に無い）では、自前の path 限定 walk を `max_commits=500` に区切る形に戻る（dulwich のオブジェクトを辿る。`Walker` そのものは使わない。git と件数・順序が完全には一致しない）。どちらの経路でも `max_commits` を T-17 の引数にする。M1 は dulwich だけで、`git` 子プロセスは使わない。`git` は利用者の環境のものを別プロセスとして起動するだけで、同梱もリンクもしない（第 16 章 L-4〔GPL は同梱・リンクしない。利用者の環境にあるものをサブプロセスで起動するのは可〕に適合する）。
- 改名検出は `RenameDetector(store, rename_threshold=50)`（git 既定の 50%。dulwich の既定 60 は厳しく、click の R053 を D+A にする）。100 ファイルを超える大きな差分は 300 ms 級で、改名の判定が git と 1 件ずれうる（T-18 の `RENAMED` は本体ハッシュ一致が主なので影響は小さい）。
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
host ──spawn──► lucianke
 1. bootstrap_io          fd 1 を退避、fd 1 → stderr（ADR-3）
 2. logging_setup         stderr + JSON Lines ファイル
 3. config                環境変数 / .lucianke.toml / 既定
 4. cache dir             <cache_root>/<ws_hash>/ を用意（ws_hash = sha1(normcase(実体パス)) の先頭 16 桁）
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
- 失敗分岐: 一部の path が無い → `data.items[].error`、全体は `ok: true` と `warnings`（仕様書 §5.4.4）。全件失敗 → 最初の項目のコードで `ok: false`。`max_tokens` が小さく 1 件も入らない → レコード 0 件 + `warnings`（`next_cursor` なし。BUG-0015、仕様書 §5.6 の 4）。

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
  │                  workspace.watcher expect(path, sha_after)（取り込めたときだけ。失敗なら forget(path)）
  │ ── unlock ───────edit.lock
  └────────────────────────────────────────────────────────────────────────────┘
  8. Diagnosed       edit.verify       参照元を didOpen（DocumentRouter 経由）→ DiagnosticsWaiter.wait(paths, v_n, diagnostics_wait_ms)
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
 3. 自分の書き込みか     Watcher.expect に (path, sha) が登録済みで内容が一致すれば捨てて終わり（一致しなければ期待を消費して取り込む。§8.4 の 7b で登録）
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
  Packer  → records[0:k] を詰め、残りがあるので CursorCodec.encode({tool, args_hash: sha1(args)[:12], offset: k, data_version, workspace_id})
            （中身は表に入れ、応答には短い鍵だけ。§7.2.3）
  応答    → # more: cursor=<token> remaining≈N

2 回目: tools/call(同ツール, {同じ引数, cursor: <token>})
  pipeline → decode → 表に無ければ E_CURSOR_EXPIRED（details.reason: "unknown_cursor"）
           → tool / args_hash / workspace_id が一致しなければ E_INVALID_ARGUMENT（details.param: "cursor"）
           → data_version が現在と違えば E_CURSOR_EXPIRED（details.reason: "data_changed"）
           → 一致なら ctx.cursor_offset = offset
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

- Windows では `loop.add_signal_handler` が使えないため、`signal.signal` でフラグを立て、ループ上の監視タスクが 1 を始める。`CTRL_BREAK_EVENT` を LSP の子プロセスに送れるのは、子を新しいプロセスグループで起動している場合だけで、GUI ホスト配下では届かない。当てにせず、LSP には `terminate` の前に `shutdown` → `exit` を送る（ADR-12）。
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
| 環境変数 | `LUCIANKE_CACHE_DIR`, `LUCIANKE_LOG_LEVEL`, `LUCIANKE_INDEX_WORKERS`（走査の解析の子プロセスの数。`engine/analysis_pool.default_workers` が読む）, `LUCIANKE_LSP_<LANG>_CMD`, `LUCIANKE_DISABLE_LSP`, `LUCIANKE_MAX_FILE_BYTES` |
| ツール | `configure_workspace` の設定は `workspaces` 行に保存し、次回起動でも使う |

優先順位: ツール > 環境変数 > `.lucianke.toml` > 既定（仕様書 §12.7）。`Settings` は不変で、変更は `WorkspaceContext` が新しい `Settings` を作って差し替える。

## 9.4 セキュリティの実装点

| 仕様書 | 実装点 |
|---|---|
| 仕様書 §13.1 パス封じ込め | `workspace/paths.py` の `confine(ws, raw) -> RelPath`。すべてのファイルアクセスはこの戻り値だけを受け取る型（`RelPath`）にする。生の `str` を受け取る I/O 関数を置かない |
| 仕様書 §13.2 DB 読み取り専用 | `index/public.py`（3 重防御）。`tests/invariants/test_public_db_readonly.py` が書き込み文の全パターンを投げて拒否と DB ハッシュ不変を確かめる |
| 仕様書 §13.3 子プロセス | `core/procs.py` の `ProcessRegistry.spawn(argv, cwd, env)`。`shell=True` を使う箇所をコードベースから静的に禁止（lint） |
| 仕様書 §13.5 秘密ファイル | `languages.py` が秘密パターンに一致したファイルを `tier=4, language="secret"` にし、`FormatAdapter` を割り当てない。骨格・本体は `E_UNSUPPORTED`（`details.reason: "secret_file"`） |
| 仕様書 §13.6 入力検証 | pydantic（`extra="forbid"`）、正規表現は `re` のタイムアウトが無いため、長さ上限（1,000 文字）と `regex` パターンの事前検査（ネストした量化子の拒否）＋スレッドでの評価時間監視（`REGEX_TIMEOUT_MS`）で代替する |
| 仕様書 §13.7 外部通信禁止 | 依存のうち実行時に外へ出るものを入れない（L-6）。`tests/invariants/test_no_network.py` が全ツールを実行中に `socket.socket` をモンキーパッチして外向き接続が無いことを確かめる |

## 9.5 OS 差の扱い（Windows を第一級に）

| 事項 | 扱い |
|---|---|
| パス | 内部は常に `/` 区切りの相対パス。大文字小文字を区別しないファイルシステムでは、実体パスの大文字小文字を正とし、`confine` で正規化する |
| 改行 | ファイルの多数派を検出して保持（`core/textpos.py`）。`git` の `autocrlf` に依存しない |
| 原子的改名 | `os.replace`。Windows で `winerror` 5・32 なら 10〜160 ms の指数間隔で再試行（合計 310 ms）。読み取り専用属性の対象は書かずに `E_MUTATION_BLOCKED`（§7.7.5。S-10 で確認） |
| シグナル | `SIGINT` / `SIGTERM`（POSIX）、`SIGBREAK` と `stdin` EOF（Windows）。`loop.add_signal_handler` が無い環境では `signal.signal` + フラグ |
| 子プロセス | Windows では `CREATE_NEW_PROCESS_GROUP` で起動して Job Object（`KILL_ON_JOB_CLOSE`）に入れ、孫まで落とす。終了は（LSP は `shutdown` → `exit` のあと）`terminate` → `kill`。`CTRL_BREAK_EVENT` は GUI ホスト配下で届かないので当てにしない。POSIX では `start_new_session=True` で起動し、`killpg` の `SIGTERM` → `SIGKILL`（ADR-12） |
| ファイル監視 | `watchfiles`（Rust 製バックエンド。Windows 対応の記述あり。S-6 で挙動確認）。利用不可ならポーリング（仕様書 §12.4 の 6） |
| 実行ビット | POSIX のみ保持。Windows では無視 |
| Python 環境 | 仮想環境（`.venv` / `venv` / `env` / `.env`）の `Scripts/python.exe`（Windows）と `bin/python`（POSIX）を実行環境の側で探索 |
| キャッシュ場所 | `%LOCALAPPDATA%\lucianke\cache`、`~/Library/Caches/lucianke`、`~/.cache/lucianke`（`platformdirs` は使わず自前で 3 分岐。依存を増やさない） |

## 9.6 文字コードとエンコーディング

- ファイルは UTF-8 として読む。BOM があれば記録して保持する。UTF-8 として読めないファイルは `files.language = "binary"`、`tier = 4` とし、骨格・本体は `E_UNSUPPORTED`。
- Tree-sitter はバイト列で解析し、ノード範囲はバイトオフセットで返る。`LineIndex` がバイト → コードポイントに変換する（ADR-14）。列は行頭からのバイト数で、行は `\n` だけで切る（S-2 で確認）。
- JSON 出力は `ensure_ascii=False`。

---

# 10. 品質戦略

## 10.1 テスト階層

| 階層 | 対象 | 速さ | 実 LSP |
|---|---|---|---|
| unit | `core`、`budget`（パッカー・推定器・カーソル）、`textpos`、`urn`、`boundaries`、`normalize`、`writer`、`paths`、`languages` | 秒 | 不要 |
| contract | 仕様書 §11 の事後条件を T-nn ごとに。フィクスチャ F-POLY と小さな Python / TS プロジェクト。LSP は偽物（録画した応答を返す `FakeLsp`） | 分 | 不要 |
| e2e | 実 LSP（basedpyright、ruff、vtsls）と実 MCP クライアント（SDK のクライアント）で、`.mcp.json` に書くのと同じ起動方法 | 分〜十分 | 要 |
| invariants | `stdout` 純度、子プロセス 0、読み取りツールでの `sha256` 不変、公開 DB の読み取り専用、外向き接続なし、**再起動耐性**（プロセスを kill して再起動しても、同じ呼び出しが同じ結果を返す。cursor と code_action_id は失効して再取得できる。ADR-16） | 全テストに横断して適用 | |
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
| license | `pip-licenses` で依存木の SPDX を許可リストと照合（仕様書第 16 章 L-8）。`python tools/gen_notices.py --check` で `THIRD_PARTY_NOTICES.md` が最新であること、`uv build` の後に `python tools/check_dist.py` で配布物の中身を確認（下記。ec55f1b・f748e04。実装で確定 2026-10-29） |
| schema | `index/schema.sql` と仕様書 §9.2 の DDL の一致。内部テーブル（`_meta`, `_hover_cache`, `_symbol_rank`, `_import_probes`）と `symbols_fts` の追従トリガは許容リストで除外 |

**`THIRD_PARTY_NOTICES.md` の生成と配布物の検査（実装で確定 2026-10-29）**:

- `tools/gen_notices.py` が `THIRD_PARTY_NOTICES.md` を `uv.lock` の依存木から生成する（手で書かない。仕様書 §16.3）。対象は本体の `dependencies` と extras（`all` を除く）ごとの推移的閉包で、dev 依存は含めない（57 パッケージ）。依存の条件（`sys_platform` / Python の版）は Windows・Linux・macOS × Python 3.12〜3.14 のどれかで要るものをすべて含め、どの OS で生成しても同じ内容になる。ライセンス情報は導入済みの配布情報から `pip-licenses` で読む（ネットワーク不要）。この環境に入らない依存（例: Windows 専用の pywin32）は表の値だけを出す（`STATIC_ENTRIES`）。デュアルライセンスの選択は検査器の `DUAL_CHOICES` を使う（dulwich は Apache-2.0 側。OD-20）。`--check` は生成物が古ければ 1 を返す。単体テスト `tests/unit/test_notices_up_to_date.py` も同じことを検査する。
- `pyproject.toml` の `license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md"]` で wheel の `*.dist-info/licenses/` に入る。sdist は**許可リスト方式**（`[tool.hatch.build.targets.sdist]`）にして、`.claude`・`.pair-agent`・`.mcp.json` などの作業用ファイルを配らない。
- `tools/check_dist.py`（`dist/` または `--build`）が、wheel に `LICENSE` と `THIRD_PARTY_NOTICES.md` が入りリポジトリのものと一致すること（古い生成物を配らない）、`METADATA` に `License-Expression: MIT` と両方の `License-File` があること、sdist の直下に両ファイルがあり作業用ファイルが無いことを確かめる（`tests/unit/test_dist_contents.py`）。CI の license ジョブに接続済み。

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
  "mcp>=2.3,<2.4",                 # MCP SDK。低水準 API の形と暫定 API を避ける前提が 2.3 系の実測に依る（S-1）
  "pydantic",
  "tree-sitter>=0.26,<0.27",   # ABI 14 の文法が 9 言語あり、0.26 の範囲（13〜15）に収まる（S-2）
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

- 依存の版は M1 着手時に固定し、`uv.lock` をリポジトリに置く。`mcp` は `>=2.3,<2.4`、`tree-sitter` は `>=0.26,<0.27` に絞る（S-1・S-2）。ロック更新時は S-2 の `run_grammars.py`（18 言語の読み込みと ABI）を CI の煙試験として回す。
- `jq`（PyPI）は M2 の `jq:` セレクタを採用する場合にだけ `[xml]` 相当の extras に加える（仕様書 §16.2.1）。

## 11.2 導入と起動

```jsonc
// .mcp.json（Claude Code の例。利用者が `uv tool install` / `pipx install` したとき）
{
  "mcpServers": {
    "lucianke": {
      "command": "lucianke",
      "args": [],
      "env": { "LUCIANKE_LOG_LEVEL": "info" }
    }
  }
}

// .mcp.json（リポジトリで開発するとき。実装で確定 2026-10-29、BUG-0030・1734b29・251eeb3。「.」は cf4497c・BUG-0126）
{
  "mcpServers": {
    "lucianke": {
      "command": "uv",
      "args": ["run", "--directory", ".", "python", "-P", "-m", "lucianke"]
    }
  }
}
```

- **`--directory` は「.」**（ホストがプロジェクトの根で起こす）。マシン固有の絶対パスを書かない。別 PC の `S:/work/develop/Lucianke` が入っていた頃は、S: の無い PC で `uv` が引数を読んだすぐ後に `error: 指定されたパスが見つかりません。 (os error 3)`（文脈なし）で落ち、Lucianke の Python まで届かずにつながらなかった（BUG-0126。`tests/unit/test_server_launch_guard.py` が「.」を見張る）。ホストは起動するときに `.mcp.json` を読むので、変えたら起動し直す。clone した直後・新しい worktree では先に `uv sync` を流す（最初の `uv run` は依存を入れるだけで 15 秒を超え、Claude Code の接続の上限 30 秒に届いて `CONNECT_TIMEOUT` になった。2026-10-31 実測）。つながるかは `claude mcp list` で確かめる。

- `uv tool install lucianke[all]` または `pipx install lucianke[all]`。
- **開発時の起動は `python -P -m lucianke`**（コンソールスクリプトの `lucianke.exe` を使わない）。`uv run` は起動のたびに環境を同期し、`pyproject.toml` が変わっていると editable の `lucianke.exe` を入れ直そうとするが、Windows では**動いている exe は消せない**（`os error 32`）ので、Lucianke を MCP でつないだままだと全員の `uv run` が落ちる（BUG-0030）。`python -m lucianke` なら握るのは `python.exe` で、同期は python のバージョンが変わらない限り触らない。`-P` は cwd（解析対象のワークスペース。任意の Python ファイルを含む）を `sys.path` に足さないため（足すとワークスペースの `mcp.py` などが SDK を隠しうる）。テスト・ベンチも同じ形で起こす（`tests/_server_cmd.py` の `server_argv`、`bench/_stdio.py`。`tests/unit/test_server_launch_guard.py` が `uv run … lucianke` に戻っていないかを見張る）。
- `lucianke doctor`（`cli/doctor.py`、`__main__` のサブコマンド。1a7c487。実装で確定 2026-10-29）が環境を自己診断し、**結果は `stderr` だけ**に表で出す（`stdout` は MCP 専用。診断コマンドでも 1 バイトも書かず、`bootstrap_io` の隔離の後で走らせる）。項目（グループ順）: `runtime`（Python 3.12 以上。必須）、`dependencies`（実行に要る依存は必須、extras の欠けは warn。メタデータの `Requires-Dist` から読み、無ければ固定の一覧）、`stdout`（子プロセスで `bootstrap_io` を呼び、重い import・`print`・`os.write(1, …)` がすべて `stderr` へ出て、退避した fd のトランスポートだけが本物の `stdout` に届くことを確かめる。必須）、`cache`（キャッシュディレクトリの書き込み可否は必須、使用量は 5 GiB 超で warn、ディスクの空きが 1 GiB 未満で warn。R-10）、`tools`（Node・ctags〔Universal Ctags だけ〕・git の有無。任意。git は dulwich で直読するので不要）、`lsp`（PATH・`.venv`・`node_modules/.bin`・環境変数の上書きにある言語サーバー。任意。入れ方の手がかりを添える）。終了コードは必須項目の欠け（`fail`）が 1 つでもあれば 1、無ければ 0、`--strict` なら warn も 1、引数の誤りは 2。`--json` は同じ内容を 1 行の JSON で `stderr` に出す。`--no-selftest` は `stdout` 隔離の子プロセス検査を省く。
- `lucianke index <root>` が事前索引を行い、初回起動を速くする（任意。初回索引の重さは許容する方針なので必須ではない）。**未実装**（`__main__` は `doctor` だけ。`index` と `bench` は M1 の後半）。

## 11.3 版管理

- セマンティックバージョニング。ツールの入出力の互換性を壊す変更はメジャーを上げる。
- `tools/list` の各ツールに `description` 末尾で `lucianke/<version>` を出す（エージェントが版を知れる）。

---

# 12. 技術検証（スパイク）と M1 の着手順

## 12.1 スパイク一覧

各スパイクは「何を確かめるか・合格条件・不合格時の代替」を持つ。結果は `docs/spikes/S-nn.md` に 1 画面で残し、本書の該当箇所を改版する。

| ID | 確かめること | 合格条件 | 不合格時の代替 | 結果 |
|---|---|---|---|---|
| S-1 | `mcp` 2.x の低水準サーバーで、`tools/list` のスキーマを自前 JSON で返せるか、`tools/call` の結果に `structuredContent` と `isError` を載せられるか、`roots/list` を要求できるか、トランスポートに任意のバイナリストリーム（退避 fd）を渡せるか。あわせて、HTTP 系の依存（starlette / uvicorn 等）が extras で外せるか、外せないなら stdio 起動時に import されないか | 4 点とも可能で、SDK クライアントから往復できる | 高水準 API へ切替（スキーマは後処理で `additionalProperties: false` を付ける）。ストリームが渡せなければ `sys.stdout` を退避 fd のラッパに戻す | **合格**（[docs/spikes/S-01.md](spikes/S-01.md)）。4 点とも可で、SDK クライアントから往復できた。HTTP 系の依存は extras で外せず stdio でも import される（起動 +約 190 ms、コールドスタート約 1.0 s）。ADR-2 は維持、ADR-3 は文言を修正 |
| S-2 | `tree-sitter` 0.26 の API（`Language(mod.language())`、`Query` / `QueryCursor`）と、2024 年で更新が止まった文法（typescript / cpp / json / xml / toml）の ABI 互換 | 対象 7 文法で解析・タグクエリが通る | 互換しない文法は版を下げるか、`tree-sitter-language-pack` を避けて個別に再ビルド（ライセンス再確認） | **合格**（[docs/spikes/S-02.md](spikes/S-02.md)）。対象 7 文法を含む全 18 言語で解析・タグクエリが通る（ABI 14 / 15）。GIL とメモリ量（ソースの約 20〜26 倍）が前提と違い、ADR-13・§7.4.2 を改版 |
| S-3 | `ast-grep-py` でパターン検索と書き換え案（メタ変数展開）が得られるか | `print($MSG)` → `logger.info($MSG)` の一致と置換テキストが取れる | M2 まで保留。自前の Tree-sitter クエリで検索のみ | **合格**（[docs/spikes/S-03.md](spikes/S-03.md)）。`print($MSG)` → `logger.info($MSG)` の一致と置換テキストが Python・TS・TSX・JS で取れた。`replace` はメタ変数を展開しないので展開は自前（代替は不要） |
| S-4 | `basedpyright` を pip で入れ、`basedpyright-langserver --stdio` 相当で起動し、`initialize` → `didOpen` → `publishDiagnostics` → `definition` が通るか。`positionEncodings` に `utf-8` を返すか | Windows / macOS / Linux で通る | `pyright` + Node を利用者の環境に求める | **合格**（[docs/spikes/S-04.md](spikes/S-04.md)）。Windows 11 のみ実測（macOS / Linux は CI で確認）。basedpyright は起動 377 ms で `initialize`・診断・`definition`・`references`・`hover`・`rename`・`callHierarchy` が通り、ruff は診断・quickfix・organizeImports が通る。`positionEncoding` は basedpyright が返さず UTF-16、ruff が `utf-8`。インタープリタは `workspace/configuration` の `python` セクションだけで渡せる。§7.5・§7.7.6 を改版 |
| S-5 | `vtsls` または `typescript-language-server` の探索・起動・`definition` / `references` / `rename` | 1 つ以上で通る | TS は best effort に格下げ（OD-7 の改版） | **合格**（[docs/spikes/S-05.md](spikes/S-05.md)）。vtsls・typescript-language-server の両方で `definition` / `references` / `rename` が通る。診断は typescript-language-server が常に最新で空配列も届くため、**既定を typescript-language-server に入れ替え**、vtsls は tsserver が見つからないときの代替にした（OD-7 の改版は不要）。どちらも `publishDiagnostics.version` を返さず、vtsls は古い診断を返すことがある。§7.5・§7.7.6 を改版 |
| S-6 | `watchfiles` の Windows での検知遅延・デバウンス・ネットワークドライブの挙動 | 単一ファイル変更をイベントまで 100 ms 以内で受ける | ポーリング（仕様書 §12.4 の 6） | **合格（`step=10` を指定した場合）**（[docs/spikes/S-06.md](spikes/S-06.md)）。単発変更が p50 18.8 / p95 19.9 ms で届く（既定の `step=50` では p95 100.6 ms で境界）。バースト検知・イベント種別・ディレクトリ移動の扱いが想定と違い、§7.3.6 を改版。ネットワークドライブは未検証 |
| S-7 | `sqlite3` の `set_authorizer` / `set_progress_handler` / `mode=ro` URI が WAL の DB で動くか（`-shm` の扱い含む）、`query_only` が書き込みを拒否するか | 書き込み文が 3 重のどの層でも拒否される | `mode=ro` を外して `query_only` + authorizer の 2 重にする（その旨を仕様書に反映） | **合格**（[docs/spikes/S-07.md](spikes/S-07.md)）。DB 本体への書き込み文は 3 層のどの 1 層でも拒否される。ただし `ATTACH` の空ファイル作成と `PRAGMA journal_mode` は authorizer と禁止語検査でのみ止まる。§7.6.4 を改版 |
| S-8 | `dulwich` で参照解決・blob 取得・ツリー差分・改名追跡が F-PY 規模で何 ms か | `HEAD~3` の blob 取得が 50 ms 以内、`log --follow` 相当が 500 ms 以内 | `pygit2`（L-3 の手続き）または `git` 子プロセス | **部分合格**（[docs/spikes/S-08.md](spikes/S-08.md)）。M1 の範囲（参照解決・blob・`STAGED`・`.gitignore`・不変性）は合格で、`HEAD~3` の blob は 2.2 ms。**`log --follow` 相当は F-PY 規模で不合格**（dulwich の `Walker` が 13〜16 秒、自前の path 限定 walk でも 1.8 秒。`git log --follow` は 106 ms）。M1 は dulwich 維持、M2 の履歴は「履歴専用の `git` 子プロセス」（読み取り専用）に**決定**（OD-20、師匠 2026-10-29）。§7.9 を改版 |
| S-9 | Python + SDK の往復オーバーヘッド: 何もしないツールと `get_file_skeleton`（キャッシュ済み）の p95 | 何もしないツールで p95 < 10 ms、骨格で p95 < 50 ms | JSON 直列化・pydantic 検証の最適化、ダメなら OD-1 (c) | **合格**（[docs/spikes/S-09.md](spikes/S-09.md)）。何もしないツールの往復 p95 1.08 ms（条件 10 ms）。骨格（T-02）の p95 < 50 ms は実装後に測る |
| S-10 | Windows で `os.replace` の対象をエディタが開いているときの挙動と再試行の効果 | 5 回以内の再試行で成功するか、失敗を検知してロールバックできる | 一時ファイルに書いて内容をコピーする方式（原子性は劣る。仕様書に注記） | **合格**（[docs/spikes/S-10.md](spikes/S-10.md)）。失敗は `PermissionError`（winerror 5・32）で確実に検知でき target は無傷。50 ms の保持は 4 回目の試行で成功。再試行の条件と間隔を §7.7.5 で改版 |
| S-11 | basedpyright / vtsls / rust-analyzer / gopls / clangd の `positionEncodings` 交渉結果 | 各サーバーの単位が分かり、`LineIndex` の変換で非 ASCII 行の位置が一致する | UTF-16 固定 | **合格**（[docs/spikes/S-11.md](spikes/S-11.md)）。basedpyright・vtsls・typescript-language-server は応答に `positionEncoding` が無く UTF-16、ruff だけが `utf-8` を返す。`LineIndex` の変換は 4 サーバーで全件一致。`position_encoding_default` を削除し、文字境界の列だけを送る規則を足した。rust-analyzer・gopls・clangd は未導入で未測定 |
| S-12 | トークン推定の係数を `o200k_base` と比べて較正 | 仕様書 §14 のコーパスで誤差 ±10% | 係数表を文字種別にさらに分ける | **現行の係数は不合格 → `lean_wide` に替えて合格**（[docs/spikes/S-12.md](spikes/S-12.md)）。日本語 Markdown が −16〜−17%、compact 出力が −11%、base64 などが −43〜−63% で、いずれも過小。文字種別に分けるだけでは足りず、連の数を数える `lean_wide`（8 項）で種類別の合計誤差が ±3.3% 以内（交差検証の保持側）。§7.2.1 を改版。`core/tokens.py` は 27e0821 で置き換え済み（回帰テスト `tests/unit/test_core_tokens_calibration.py` が全種類合格） |

結果の欄は S-1〜S-12 のすべてを埋めた（反映の一覧は [docs/spikes/SYNC-1.md](spikes/SYNC-1.md)〔S-1・S-2・S-3・S-7・S-9・S-10〕と [docs/spikes/SYNC-2.md](spikes/SYNC-2.md)〔S-4・S-5・S-6・S-8・S-11・S-12 と構築の担当の報告〕）。

## 12.2 M1 の着手順（最初の 12 タスク）

依存の無いものは並列に進める。各タスクは「成果物」と「確かめ方」を持つ。

| # | タスク | 成果物 | 確かめ方 | 依存 |
|---|---|---|---|---|
| 1 | 12 本のスパイク（実施済み。結果は §12.1） | `docs/spikes/` | 各合格条件 | なし（並列） |
| 2 | リポジトリ骨格: `pyproject.toml`、`src/lucianke` の空パッケージ、`core/`、CI（lint / test / license） | ビルドとテストが空で通る | CI 緑、`pip-licenses` 緑 | なし |
| 3 | `core`（model / urn / errors / records / textpos / limits） | 単体テスト | `urn` の往復、`LineIndex` の非 ASCII | 2 |
| 4 | `mcp`（server / registry / pipeline / envelope / budget / render.compact / render.json） | 何もしないツール `ping`（開発時だけ登録し `tools/list` に載せない）が SDK クライアントから往復 | S-9 の型（`spikes/s01_mcp/bench.py`）を `bench/` へ移して回帰の基準にする（実ツールでも同じ 500 回計測の p95 を CI に記録）、compact 文法パーサ | 1, 3 |
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

| # | リスク | 兆候 | 対策 | 状態 |
|---|---|---|---|---|
| R-1 | Python の往復オーバーヘッドで NFR-PERF-1 を満たせない | S-9 で p95 > 50 ms | 直列化と検証の削減、レコードの遅延生成、骨格の事前計算。最後に OD-1 (c) | **縮小**（S-9: 何もしないツールの往復は p95 1.08 ms で、JSON 直列化・pydantic 検証の最適化は不要。T-02 の p95 は実装後に測る） |
| R-2 | `mcp` SDK 2.x の API が想定と違う | S-1 不合格 | ADR-2 の代替。SDK の版を固定し、境界を `mcp/server.py` に閉じ込めてあるので影響は 1 ファイル | **解消**（S-1 合格。維持する前提は ADR-2） |
| R-3 | 更新が止まった Tree-sitter 文法が 0.26 と合わない | S-2 不合格 | 版の固定、個別再ビルド、または当該言語を `structural` 不可の Tier 4 扱いに一時格下げ | **解消**（S-2: 読めない文法は無かった。18 言語すべて読み込み・クエリ可） |
| R-4 | LSP サーバーごとの差（位置単位、`publishDiagnostics` の `version` 無し、`callHierarchy` 未対応） | e2e で位置ずれ・診断待ちのタイムアウト | `servers.py` にサーバー別の癖を集約。`FakeLsp` の録画で回帰を固定 | **縮小**（S-4・S-5・S-11: 位置単位は 4 サーバーで実測し `LineIndex` が全件一致。`version` が無い・古い診断を返すサーバー〔tsls・vtsls〕は §7.5.5 の規則で吸収。`callHierarchy` は basedpyright・tsls・vtsls で通る。残り: rust-analyzer・gopls・clangd の単位と診断は未測定） |
| R-5 | Windows での共有違反・改行・大文字小文字 | EP-6 / EP-9 が Windows でだけ落ちる | CI を 3 OS で回す。`writer.py` の再試行とロールバック | **縮小**（S-10: 失敗は winerror 5・32 で確実に検知でき target は無傷。310 ms の再試行で 200 ms までの保持に勝てる。3 OS の CI は継続） |
| R-6 | GIL により初回索引が遅い | 1 万ファイルの索引に分単位 | 索引中も降格応答で答え続けることと、`configure_workspace status` の進捗表示を優先する。走査の解析を子プロセスに並べ、書き手のページキャッシュと取り込みのまとめ書きで書き込みを縮めた（§6.1・§7.6.1。BUG-0127。4,222 ファイルで索引のジョブの完了 350 → 57〜62 秒、型サーバーの準備 371 → 62 秒）。`lucianke index` の事前実行は M2 以降 | 軽減（BUG-0127） |
| R-7 | `dulwich` の性能 | S-8 不合格 | OD-20: M2 の履歴は (a) ＋ 履歴専用の `git` 子プロセス（決定済み） | **縮小**（S-8: M1 の用途は dulwich で合格〔1〜5 ms〕。M2 の履歴〔`log --follow`〕は F-PY 規模で `Walker` が不合格のため、OD-20 を「M1 は (a)、M2 の履歴は (a) ＋ 履歴専用の `git` 子プロセス〔読み取り専用〕」に**決定**〔師匠 2026-10-29「推奨: 読み取り専用で使うことを認る」〕。(b) pygit2 は採らない。残り: 子プロセスの経路は M2 着手時に実装し、F-PY で p95 を測る） |
| R-8 | ライセンスの許可リスト外が依存木に紛れ込む | CI の license ジョブが赤 | L-8 の検査を必須ジョブに。extras ごとに検査 | 継続 |
| R-9 | 仕様書と設計の乖離 | 契約テストが仕様書の番号と合わない | 事後条件 → テスト名の対応を機械的に検査（§10.2） | 継続 |
| R-10 | キャッシュディレクトリの肥大 | ディスク警告 | ジャーナル 50 件、ログ 7 日、構文木キャッシュの上限、`lucianke doctor` に使用量表示 | 継続 |
| R-11 | `exact` の応答に LSP 往復が含まれ、NFR-PERF-1 が LSP の速度に左右される（§8.3 の 2 回目の呼び出し） | `definition` / `references` の p95 が LSP 待ちで 50 ms を超える | 結果を `relations` / `_hover_cache` に書いて 2 回目以降は PaaD から答える。S-9 で「LSP 待ちを含む p95」と「含まない p95」を分けて測り、仕様書 §14.4 の母集団定義（`exact` で応答した呼び出し）と照らす。超えるなら `WAIT_SHORT` の上限を下げて `structural` を先に返す | 継続（S-9 の往復は LSP 待ちを含まない値。LSP 待ちを含む p95 は実装後に測る） |
| R-12 | トークン推定が実測より小さく、応答が `max_tokens` を超える（NFR-TOK の予算が守れない） | S-12 の種類別の合計誤差が ±10% を超える。`bench/run_bench.py` の較正が落ちる | `lean_wide`（§7.2.1）に替え、ベンチで較正を再実行する。必要なら係数を ×1.03 にして誤差を + 側へ寄せる | **縮小**（S-12: 旧係数は日本語で −16〜−17%、base64 で −43〜−63% と過小。`lean_wide` で ±3.3% 以内。27e0821 で採用済み〔回帰テストで種類ごと ±10%〕。残り: TS・Go・Rust・韓国語は未検証。合成 base64 が −8.3% で限界に近く、×1.03 は見送り） |

---

# 付録 A. 候補パッケージの実態（一次情報・2026-10-29）

PyPI JSON API（`https://pypi.org/pypi/<name>/json`）から取得した。ライセンスは仕様書付録 D を正とし、ここでは **版・対応 Python・wheel の有無・依存** を記録する。版は M1 着手時に固定し直す（§11.1）。

## A.1 表

| 名前 | 最新版 | 公開日 | requires_python | wheel | 種別 | 主要依存 / 備考 |
|---|---|---|---|---|---|---|
| mcp | 2.3.0 | 2026-10-02 | >=3.10 | any | 純 Python | anyio, httpx2, jsonschema, mcp-types, pydantic>=2.12, starlette, uvicorn, pyjwt[crypto], opentelemetry-api, pywin32（Windows）。**依存が重い**（無条件依存で extras では外せない。§A.3） |
| tree-sitter | 0.26.0 | 2026-06-30 | >=3.10 | win / mac / linux（cp310〜） | ネイティブ | `Language(mod.language())` 形式 |
| tree-sitter-python | 0.25.0 | 2025-09-11 | >=3.10 | abi3（win_amd64 / win_arm64 含む） | ネイティブ | |
| tree-sitter-typescript | 0.23.2 | **2024-11-11** | >=3.9 | abi3 | ネイティブ | 更新停止気味。ABI 14 で本体の範囲に収まる（S-2 で確認） |
| tree-sitter-javascript | 0.25.0 | 2025-09-01 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-rust | 0.24.2 | 2026-03-27 | >=3.9 | abi3 | ネイティブ | |
| tree-sitter-go | 0.25.0 | 2025-08-29 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-c | 0.24.2 | 2026-04-22 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-cpp | 0.23.4 | **2024-11-11** | >=3.9 | abi3 | ネイティブ | 更新停止気味。ABI 14 で互換（S-2 で確認） |
| tree-sitter-c-sharp | 0.23.5 | 2026-04-14 | >=3.9 | abi3 | ネイティブ | |
| tree-sitter-json | 0.24.8 | **2024-11-11** | >=3.9 | abi3 | ネイティブ | 更新停止気味。ABI 14 で互換（S-2 で確認） |
| tree-sitter-yaml | 0.7.2 | 2025-10-07 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-toml | 0.7.0 | **2024-12-03** | >=3.9 | abi3 | ネイティブ | 更新停止気味。ABI 14 で互換（S-2 で確認） |
| tree-sitter-xml | 0.7.0 | **2024-11-13** | >=3.9 | abi3 | ネイティブ | 更新停止気味。ABI 14 で互換（S-2 で確認） |
| tree-sitter-markdown | 0.5.4 | 2026-10-24 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-rst | 0.2.0 | 2025-02-24 | >=3.10 | abi3 | ネイティブ | |
| tree-sitter-sql | 0.3.11 | 2025-10-01 | >=3.10 | abi3 | ネイティブ | M2 |
| ast-grep-py | 0.51.0 | 2026-10-15 | >=3.8 | win / mac / linux | ネイティブ | M2。`replace` / `commit_edits` はあるがメタ変数を展開しない（S-3 で確認） |
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
3. `mcp` 2.3.0 の description に `structuredContent` と `roots` の記述は無い（クライアント側の `result.structured_content` の例のみ）。サーバー側の対応は S-1 で確かめ、`structuredContent`・`isError`・`roots/list` とも可と分かった（S-1 で確認）。
4. `watchfiles` の description に "Binaries are available for most architectures on Linux, MacOS and Windows" の記述あり。
5. `ast-grep-py` の description に rewrite / replace の記述は無い。S-3 で確かめ、`replace` と `commit_edits` はあるが、`replace` はメタ変数を展開しないと分かった（S-3 で確認）。

## A.3 設計への含意

- **Windows wheel が無いパッケージは無い。** NFR-PORT-2 の障害にならない。
- **`requires_python` の下限は 3.12 で全部満たす**（最も厳しいのは jsonpath-ng の >=3.11）。
- **`mcp` 2.x は依存が重い**（HTTP サーバー系・OpenTelemetry・JWT・Windows では pywin32）。起動時間と import 時間に効く。コールドスタートは S-1 で測った: 中央値 980 ms（959〜1030 ms、n=7。素の `python -c pass` は 57 ms）で、import 全体が約 890 ms（累積で重なりあり: `mcp_types` 約 440、pydantic 約 120、starlette 約 95、sse_starlette 約 70、uvicorn 約 65、httpx2 約 45）。HTTP 系 9 パッケージを空スタブにすると 980 → 793 ms（−190 ms）で stdio は動くが、SDK 内部の import 構造への依存が強いので遅延 import は採用しない。初回の重さとして許容する（仕様書 §14.4）。重くなったら SDK 側への提案を先に検討する。依存木のライセンス検査（L-8）はこの重さも対象にする。
- **Tree-sitter の 5 文法（typescript / cpp / json / xml / toml）は 2024 年で更新が止まっている。** 本体 0.26 との ABI 互換を S-2 で確かめた結果、5 パッケージとも ABI 14 で範囲（13〜15）に収まり、読めない文法は無かった（R-3 は解消。S-2 で確認）。`tree-sitter>=0.26,<0.27` に固定してロック更新時に回帰を確かめる（§11.1）。
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
| NFR-OPS-2 | `core/procs.py`（ProcessRegistry）, `lifecycle.py`（終了手順） | invariants（子プロセス 0） |
| NFR-OPS-3 | `engine/selector.py`, `lsp/pool.py` | contract T-04 / T-08（indexing 中） |
| NFR-OPS-4 | `core/textpos.py`, `lsp/client.py` | unit textpos |
| NFR-PORT-1 | `pyproject.toml`（§11.1） | CI license / build |
| NFR-PORT-2 | §9.5 | CI 3 OS |
