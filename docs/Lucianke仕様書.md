# Lucianke 仕様書

**Lucid + Ananke: 透明な支配の美学** — コーディングエージェントのための決定論的コード知識 MCP サーバー

| 項目 | 値 |
|---|---|
| 版 | 1.0（Draft） |
| 日付 | 2026-10-29 |
| 状態 | 実装前の規範文書。師匠の検収で Draft を外す |
| 系譜 | 概念文書『決定論的プロジェクト知識DB・LSP統合クエリ＆論理解析MCPサーバー完全仕様書 (v3.0)』（`docs/` 同梱）を足掛かりに起こした。本書は実装・受入の正本であり、概念文書は背景・動機の参照先として残す |
| 旧称 | 概念文書での内部名 DLSR-MCP は本書で **Lucianke** に統一した。以後 DLSR-MCP は使わない |

---

# 0. 文書について

## 0.1 位置づけ

本書は Lucianke の **何を・どこまで・どう確かめるか** を定める。読者は、実装する役（構築者）、受入検査を設計・実施する役、そして方針を決める師匠である。概念文書が「なぜ作るか」と「どんな姿か」を広く描いたのに対し、本書は矛盾と曖昧さを取り除き、各機能に識別子と検証可能な受入基準を与える。

概念文書から本書で改めた点は付録 B に一覧する。主なもの:

- 版表記（v2.0 / v2.2 / v3.0）と名称（DLSR-MCP）の混在を解消した。
- ツールは 21 本（カテゴリ 0 と A〜G）に確定し、番号を `T-00`〜`T-20` で固定した。概念文書で F より前に置かれていた G を番号順に戻した。
- すべてのツールに共通するエンベロープ、精度メタデータ、エラー分類、ページネーション、Compact DSL の文法、URN の形式を規範として新設した。概念文書には例はあったが文法と失敗時の形が無かった。
- 編集系ツールに散在していた安全規約を第 10 章「決定論的編集プロトコル」に集約し、外部変更との競合（陳腐化書き込み）の検知を加えた。
- KPI を測定手順つきの受入基準に書き直した（第 14 章）。
- ロードマップの期間表記（週）を外し、マイルストーンの出口条件に置き換えた（第 15 章）。
- 実装言語とライブラリの選定を規範から外し、未決事項として推奨つきで列挙した（第 17 章）。
- ライセンス方針を新設し、候補ライブラリ・文法・外部ツールのライセンスを一次情報で確認して採否を定めた（第 16 章、付録 D）。本体は MIT を推奨する。

## 0.2 規範語

本書では RFC 2119 に倣い、次の語を使う。

| 表記 | 意味 |
|---|---|
| しなければならない（MUST）/ してはならない（MUST NOT） | 受入検査の不合格条件。例外なし |
| すべきである（SHOULD）/ すべきでない（SHOULD NOT） | 原則。外すときは理由を実装ノートに残す |
| してもよい（MAY） | 任意。実装の裁量 |

規範語の無い文は説明であり、受入の対象ではない。数値目標はすべて第 14 章の測定手順で判定する。

## 0.3 章構成と読み方

| 章 | 内容 | 主な読者 |
|---|---|---|
| 1 | 概要・課題・スコープ・中核原則・KPI | 全員 |
| 2 | 用語 | 全員 |
| 3 | 要求一覧（FR / NFR、受入基準） | 受入検査設計者 |
| 4 | アーキテクチャ | 構築者 |
| 5〜7 | 共通プロトコル・出力形式・URN（全ツールの前提） | 構築者・受入 |
| 8 | 言語ティアとフォールバック | 構築者 |
| 9 | プロジェクト知識 DB（PaaD）スキーマ | 構築者 |
| 10 | 決定論的編集プロトコル | 構築者・受入 |
| 11 | ツール仕様（21 本） | 構築者・受入 |
| 12〜13 | 運用・安全 | 構築者 |
| 14 | 性能目標と測定手順 | 受入 |
| 15 | マイルストーン | 師匠・構築者 |
| 16 | ライセンス方針と依存ライブラリの確認 | 師匠・構築者 |
| 17 | 未決事項（推奨つき） | 師匠 |
| 付録 A〜D | サンプルクエリ・概念文書からの変更点・参考文献・依存ライセンス調査 | |

---

# 1. 概要

## 1.1 目的

Lucianke は、コーディングエージェント（Claude Code、Cline、Cursor など）に `stdio` の MCP サーバーとして接続し、コードベースの探索・編集・検証を **決定論的に、最小トークンで** 提供する。

- **Lucid（透明性）**: ファイル全文を読ませず、構造の骨格だけを透かして見せる。ノイズを削いだ結果、エージェントの文脈窓は本質的な推論に使われる。
- **Ananke（必然性）**: 解析と編集を確率に委ねない。コンパイラ精度のシンボル解決、AST 境界での編集、ソルバによる検証で「逃れられない正解」を導く。

## 1.2 解決する課題

エージェントのトークンと時間を浪費させている運用上の 4 課題を対象とする。

| # | 課題 | 現象 | Lucianke の答え |
|---|---|---|---|
| 1 | 全文読み込みと grep の濫用 | 関数 1 つのインターフェースを知るために数百〜数千行を文脈に入れる | 骨格ファースト探索（`get_file_skeleton` → `get_symbol_bodies`）と Compact DSL |
| 2 | 行番号・文字列一致パッチの脆さ | 行ずれ・インデント不一致で適用が失敗し、再読込と再生成を繰り返す | シンボル単位の AST 境界編集と LSP `WorkspaceEdit`（第 10 章） |
| 3 | フィードバックループの遅さ | 編集のたびにビルドやテストをシェルから起動し、起動待ちとログで文脈を圧迫する | 常駐 LSP からの即時診断と、編集応答への診断差分同梱（Edit-and-Verify） |
| 4 | 往復回数の多さ | 1 回の呼び出しで 1 シンボルしか扱えず、結合はエージェントの文脈内で行われる | バッチ入力と、プロジェクト知識 DB 上でのサーバー側 JOIN（PaaD） |

## 1.3 スコープ

### 1.3.1 含むもの

- MCP `stdio` サーバー本体と、第 11 章の 21 ツール。
- Tree-sitter による構文木、言語別 LSP の多重化、SQLite 上のプロジェクト知識 DB（PaaD）、Git 履歴の取り込み。
- 第 8 章の 4 ティア（完全解析から未知言語の字句フォールバックまで）と精度メタデータ。
- 第 10 章の決定論的編集プロトコル。
- 決定論的（LLM 不使用）な契約抽出と、境界付き SMT 検証。
- 第 14 章のベンチマーク手順（受入の道具として本体と同じリポジトリで保守する）。

### 1.3.2 含まないもの

- サーバー内部での LLM・重量級 NLP の実行。自然言語の仕様を論理式に変えるのはホスト側エージェントの責務である。
- 外部 DB サーバー（Neo4j 等）、GPU、ネットワーク上のインデックスサービスへの依存。
- IDE 拡張や GUI。Lucianke はエージェント向けの機械インターフェースに徹する。
- 任意コードの実行（テストランナー・ビルドの起動）。エージェントは既存のシェルツールで行う。
- `stdio` 以外のトランスポート。HTTP 系は提供しない（§5.1）。

## 1.4 中核原則

以降の章はこの 6 原則を具体化したものである。

1. **骨格ファースト（Skeleton-First）**: 未知のファイルには全文ではなく骨格を返す。本体は求められた単位だけ切り出す。
2. **既定でバッチ（Batching by Default）**: 主要な読み取りツールは配列を受け付け、1 回の呼び出しで複数の対象を解決する。
3. **編集と検証の一体化（Edit-and-Verify）**: 編集ツールの応答には、その編集で増減した診断が含まれる。エージェントは別の呼び出しをせずに自己修正へ進める。
4. **予算内応答（Token Budget Enforcement）**: すべての応答は `max_tokens` を超えない。超える分は切り捨てではなく要約と継続カーソルに変える。
5. **プロジェクトは DB である（Project-as-a-Database）**: AST・型・参照・診断・Git 履歴・契約を一つの正規化スキーマに同期し、定型ツールはその上のビューとして、非定型の問いは読み取り専用 SQL として答える。
6. **優雅な降格と精度の明示（Graceful Degradation & Confidence Signaling）**: 未対応言語や LSP 未起動でも止まらず、段階的に降格して答える。ただし精度水準を必ず応答に刻み、降格状態での編集には安全装置をかける。

## 1.5 KPI 要約

数値はすべて第 14 章のベンチマーク手順で測る。ここでの値は受入の閾値である。

| KPI | 閾値 | 測定 |
|---|---|---|
| 探索・影響調査に要するトークン削減率（従来手法比） | 75% 以上 | §14.3 |
| Fast Path ツールの応答レイテンシ（ウォームアップ後） | p95 < 50 ms | §14.4 |
| Verification Path ツールの応答レイテンシ | p95 < 500 ms | §14.4 |
| 編集後の構文妥当性 | 99.9% 以上 | §14.5 |
| 外部変更の検知から DB 反映まで（単一ファイル） | 500 ms 以内 | §14.6 |

概念文書の第 8 章にあった「90% 削減」「作業時間 80% 短縮」といった値は、本書では **仮説** として扱い、ベンチマークが確かめる対象とする（§14.7）。

---

# 2. 用語定義

| 用語 | 定義 |
|---|---|
| ワークスペース | 解析対象となるプロジェクトルート 1 つ。モノレポでは複数登録できる。識別子は `workspace_id`（§7.2） |
| リビジョン（revision） | 解析対象の断面。`WORKING_TREE`（作業ツリー）、`STAGED`、`HEAD`、ブランチ・タグ・コミット SHA |
| シンボル | 言語が名前で参照できる構造単位（クラス・関数・メソッド・変数・型など）と、Tier 2〜4 で同等に扱う要素（テーブル・エンドポイント・セル・タグ・キー・ctags エントリ） |
| URN | Lucianke が付与するシンボルの安定識別子。`lk://` で始まる（第 7 章） |
| TargetRef | ツールにシンボルを指定する共通オブジェクト（§5.3） |
| 骨格（Skeleton） | 関数本体を折りたたみ、インポート・型・シグネチャ・Doc 要約だけを残したファイル表現 |
| Compact DSL | 既定の出力テキスト形式（§6.1） |
| 精度水準（confidence_level） | `exact` / `structural` / `heuristic` / `lexical_fallback` の 4 段階（§8.3） |
| 降格（degraded） | 精度水準が `exact` でない状態。応答に警告が付く |
| ティア（Tier） | 言語・フォーマットごとの解析能力の等級 1〜4（第 8 章） |
| Fast Path | ミリ秒級で答える読み取り・編集の経路。LLM もソルバも使わない |
| Verification Path | オンデマンドの経路抽出・契約抽出・SMT 検証の経路 |
| PaaD（Project-as-a-Database） | プロジェクトの全ファクトを 1 つの SQLite スキーマに同期し、クエリで引く設計（第 9 章） |
| Edit-and-Verify | 編集ツールが編集直後の診断差分を同じ応答に含める規約（§10.7） |
| 診断（diagnostic） | LSP または Tree-sitter が報告する構文・型・Lint の指摘 |
| Code Action | LSP が提供する自動修正・リファクタリング操作。`code_action_id` で参照する |
| 契約（contract） | 事前条件・事後条件・不変条件・例外条件。型・ガード節・Doc コメント・スキーマから決定論的に抽出する |
| 共変更結合（co-change coupling） | Git 履歴上で同じコミットに含まれる頻度から求めた、2 つの実体の結びつき（`coupling_ratio`） |
| マイルストーン（M1〜M3） | 第 15 章の段階。期間ではなく出口条件で定義する |

---

# 3. 要求一覧

要求は機能要求（FR）と非機能要求（NFR）に分け、識別子で参照する。「受入」列は、その要求を満たしたと判定する根拠の所在を示す。ツール固有の事後条件は第 11 章の各ツールに置く。

## 3.1 機能要求（FR）

| ID | 要求 | 受入 |
|---|---|---|
| FR-WS-1 | 起動時に MCP `roots/list`（取れたとき。取れなければ cwd と T-00 `set_roots`。§5.1）とマーカーファイル（`pyproject.toml`, `uv.lock`, `poetry.lock`, `package.json`, `Cargo.toml`, `go.mod`, `.git`）からワークスペース境界と Python 実行環境（`.venv`, `uv`, `poetry`, `conda`）を自動検出する | T-00 `status` の出力で検出結果が確認できる |
| FR-WS-2 | ルート・Python 環境・除外パターン・基準リビジョンを明示設定し、LSP とインデックスの状態を確認・再構築できる | T-00 |
| FR-WS-3 | `revision` を指定すると、作業ツリーを変えずに Git オブジェクトから当該断面の仮想ツリーを構築して読み取りツールが解析する | 読み取りツール全般。§8.5 |
| FR-EXP-1 | 重要度順（参照グラフ上のスコア）に並べたリポジトリ俯瞰マップを予算内で返す | T-01 |
| FR-EXP-2 | 複数ファイルの骨格を 1 回で返す。コード以外（`.ipynb`, XML, JSON, YAML, TOML, DOT, Mermaid、未知言語）にも骨格の形を定める | T-02 |
| FR-EXP-3 | 名前（部分・ファジー・正規表現）、種別、型シグネチャでワークスペース横断にシンボルを検索する | T-03 |
| FR-NAV-1 | 複数の TargetRef を一括で定義解決し、展開済み型・Doc 全文・URN・関連型の定義を返す | T-04 |
| FR-NAV-2 | シンボルの実装本体を AST 境界で切り出す。Tier 3 のセレクタ（XPath / JSONPath / jq / セル）も同じ入口で受ける | T-05 |
| FR-NAV-3 | 参照箇所（用途種別つき）、実装、型階層を列挙する | T-06 |
| FR-NAV-4 | 呼び出し元・先の階層を深さ指定で返し、各呼び出しを囲むガード条件を添える | T-07 |
| FR-DIAG-1 | ビルドを起動せずに最新の診断と QuickFix 候補を返す。LSP 不在でも構文エラーは返す | T-08 |
| FR-DIAG-2 | シンボル変更の影響（直接破壊候補・推移的依存・関連テスト）に Git 共変更結合を統合して返す | T-09 |
| FR-EDIT-1 | シンボル単位で置換・前後挿入・メンバー追加・削除を行う | T-10、第 10 章 |
| FR-EDIT-2 | LSP の rename でワークスペース全体の定義と参照を一括改名し、URN の対応表を返す | T-11 |
| FR-EDIT-3 | 構文パターン（メタ変数つき）で横断検索し、同じパターンで書き換える | T-12 |
| FR-EDIT-4 | 診断に紐づく QuickFix や種別指定の Code Action を適用する | T-13 |
| FR-EDIT-5 | 編集ツールの応答に、その編集で新規発生・解消した診断の差分を含める | §10.7、T-10〜T-13 |
| FR-EDIT-6 | 精度水準が `heuristic` / `lexical_fallback` のファイルへの編集は、明示フラグが無ければ書き込まず差分だけ返す | §10.5 |
| FR-EDIT-7 | 編集は原子的に書き込み、解決時点から書き込み時点までの外部変更を検知したら何も書かずに失敗する | §10.4、§10.6 |
| FR-VER-1 | 起点から終点への制御フロー・データフロー経路を、通過・非通過フィルタつきで抽出する | T-14 |
| FR-VER-2 | 型・ガード節・構造化 Doc コメント・OpenAPI/JSON Schema・Gherkin から契約を LLM 不使用で抽出し、SMT-LIB 式を併記する | T-15 |
| FR-VER-3 | 契約または与えられた制約式を境界付きで検証し、違反なら具体的な反例を返す | T-16 |
| FR-GIT-1 | シンボル単位の変更履歴（意味的な差分種別つき）と共変更ホットスポットを返す | T-17 |
| FR-GIT-2 | 2 つのリビジョン間の差分をシンボル単位で構造化し、影響と推奨テストを併せて返す | T-18 |
| FR-DB-1 | PaaD の全テーブル定義・行数・精度比率・サンプルクエリを返す | T-19 |
| FR-DB-2 | PaaD に対する読み取り専用 SQL を、バインドパラメータ・タイムアウト・行数上限つきで実行する | T-20 |
| FR-FMT-1 | 既定の出力は Compact DSL であり、§6.1 の文法に適合する | 全ツール |
| FR-FMT-2 | 応答は `max_tokens` を超えず、超過分は要約と `next_cursor` に変換する | 全ツール、§5.6 |
| FR-FMT-3 | 成功・失敗を問わず全応答に `_resolution_meta` を付け、降格時は警告文を含める | 全ツール、§5.5 |
| FR-FMT-4 | グラフ構造を返すツールは `dot` と `mermaid` でも出力できる | §6.4 |
| FR-LANG-1 | 対応言語・フォーマットを 4 ティアに分類し、ティアごとの提供機能を固定する | §8.1〜8.2 |
| FR-LANG-2 | LSP → Tree-sitter → ctags/ヒューリスティック → 字句一致の順に自動降格する | §8.3〜8.4 |
| FR-LANG-3 | Python 周辺（`.pyi`/`.pyx`/PyO3 の連結、`.ipynb` の出力ストリップ、DOT/Mermaid の入出力、XML/JSON/YAML/TOML の骨格と部分取得、SQL と ORM/Pydantic の整合、IDL と実装の連結、Jinja2/rst の参照解決）を Tier 2〜3 として扱う | §8.6 |

## 3.2 非機能要求（NFR）

| ID | 要求 | 閾値 / 判定 | 受入 |
|---|---|---|---|
| NFR-PERF-1 | Fast Path ツール（T-00〜T-08, T-10, T-11, T-13, T-19, T-20）の応答レイテンシ | ウォームアップ後 p95 < 50 ms | §14.4 |
| NFR-PERF-2 | Verification Path ツール（T-09, T-12, T-14〜T-18）の応答レイテンシ | p95 < 500 ms（ソルバのタイムアウトを除く） | §14.4 |
| NFR-PERF-3 | 外部からの単一ファイル変更が DB と LSP に反映されるまで | 500 ms 以内 | §14.6 |
| NFR-TOK-1 | ベンチマーク台本における探索・影響調査のトークン消費 | 従来手法比 75% 以上削減 | §14.3 |
| NFR-SAFE-1 | 編集ツール適用後のファイルが対象言語として構文妥当である割合 | 99.9% 以上（ベンチマーク母集団で） | §14.5 |
| NFR-SAFE-2 | `query_project_db` が DB を変更できないこと | 書き込み文はすべて拒否。接続は読み取り専用 | §13.2 |
| NFR-SAFE-3 | ワークスペースルート外のパスを読まず書かないこと | ルート外指定は `E_INVALID_ARGUMENT` | §13.1 |
| NFR-SAFE-4 | 編集の原子性 | 失敗時に部分的に書き換わったファイルが存在しない | §10.6 |
| NFR-OPS-1 | `stdout` に JSON-RPC 以外のバイトを書かないこと | 起動から終了までの `stdout` が改行区切り JSON のみ | §12.1 |
| NFR-OPS-2 | 親プロセスの終了・シグナル受信後に子プロセス（LSP 等）が残らないこと | 終了後 1 秒以内に子プロセス 0 | §12.5 |
| NFR-OPS-3 | LSP の起動・索引中でもツールが応答すること | 降格応答を返し、起動完了を待ってブロックしない | §12.3 |
| NFR-OPS-4 | 位置情報の正規化 | 外部に出す行・列は §5.10 の規約に一致する | §12.2 |
| NFR-PORT-1 | 配布形態 | 外部 DB サーバー・GPU を要しない。単一パッケージ（または単一バイナリ）で導入できる | §17 |
| NFR-PORT-2 | 対応 OS | Windows / macOS / Linux で同一の挙動 | §14.2 |

---

# 4. アーキテクチャ

## 4.1 層構成

```
  Coding Agent (Claude Code / Cline / Cursor ...)
        │  ▲
        │  │  JSON-RPC 2.0 over stdio  (Compact DSL / token-budgeted payload)
        ▼  │
┌────────────────────────────────────────────────────────────────────┐
│  L1  MCP Interface & Token Optimizer                               │
│      tools/list, tools/call ・ TargetRef 解決 ・ 予算計数 ・ 形式化  │
│      ページネーション ・ エンベロープ ・ 精度メタデータ               │
└──────────────┬────────────────────────────────────┬────────────────┘
               │  Fast Path (p95 < 50 ms)           │  Verification Path (p95 < 500 ms)
               ▼                                    ▼
┌──────────────────────────────────┐  ┌───────────────────────────────┐
│  L2a  Structural & LSP Engine    │  │  L2b  Symbolic Verification    │
│   Tree-sitter 増分構文木          │  │   CFG / Def-Use 抽出           │
│   構文パターン検索・書き換え      │  │   経路探索（再帰 CTE / Datalog）│
│   多重化 LSP クライアントプール   │  │   契約抽出器（ルールベース）    │
│   編集トランザクション（第 10 章）│  │   境界付き SMT 検証（タイムアウト付き）│
└──────────────┬───────────────────┘  └───────────────┬───────────────┘
               │                                      │
               ▼                                      ▼
┌────────────────────────────────────────────────────────────────────┐
│  L3  Project Knowledge DB (PaaD)  — SQLite (WAL + FTS5 + 再帰 CTE)  │
│      workspaces files symbols relations dataflows diagnostics       │
│      contracts git_commits git_symbol_changes git_co_changes        │
└──────────────┬──────────────────────────────┬──────────────────────┘
               │                              │
               ▼                              ▼
┌──────────────────────────────┐  ┌──────────────────────────────────┐
│  L4a  Workspace Context      │  │  L4b  Git Temporal Layer          │
│   roots/list ・ マーカー検出  │  │   .git 直読 ・ シンボル別履歴      │
│   Python 環境 ・ 除外規則     │  │   共変更結合の集計                 │
│   ファイル監視 ・ 仮想ツリー  │  │   revision 断面の仮想ツリー供給    │
└──────────────────────────────┘  └──────────────────────────────────┘
```

## 4.2 コンポーネントの責務

| コンポーネント | 責務 | 置かない責務 |
|---|---|---|
| MCP Interface | スキーマ公開、引数検証、TargetRef 解決、ツールのディスパッチ、エンベロープ生成 | 解析そのもの |
| Token Optimizer | トークン計数、予算内への詰め込み、要約と `next_cursor` の生成、Compact/JSON/DOT/Mermaid への直列化 | 内容の取捨選択の方針（各ツールが優先順位を与える） |
| Tree-sitter Engine | ファイルの構文木の生成・増分更新・キャッシュ、骨格抽出、シンボル範囲の決定、構文エラーノードの検出 | 型推論、クロスファイル解決 |
| Structural Matcher | 構文パターン（メタ変数つき）の検索と書き換え案の生成 | 書き込み（編集トランザクションに委ねる） |
| LSP Pool | 言語別 LSP の遅延起動・初期化・`didOpen`/`didChange`/`didChangeWatchedFiles` の送出、診断の受領、`definition`/`references`/`callHierarchy`/`rename`/`codeAction` の呼び出し、位置エンコーディング変換、子プロセスの回収 | LSP が返した結果の直列化 |
| Edit Transaction | 第 10 章の手順の実行、書き込みロック、原子的書き込み、ジャーナル | 編集内容の生成 |
| Indexer | 構文木・LSP・Git・契約抽出器の出力を PaaD スキーマへ正規化して書き込む。ファイル監視イベントに応じた差分更新 | 外部への公開（ビューは読み取り専用で別に公開する） |
| Query Engine | `query_project_db` の文の検証（読み取り専用）、バインド、タイムアウト、行数制限、結果の直列化 | スキーマ変更 |
| Verification Core | CFG / Def-Use の抽出、経路探索、契約抽出、SSA 変換、SMT 検証 | LLM の呼び出し（禁止） |
| Workspace Context | ルートと環境の検出・設定、除外規則、ファイル監視、`revision` 指定時の仮想ツリー | |
| Git Temporal Layer | コミット・ツリーの直読、シンボル別変更の算出、共変更結合の集計 | シェルの `git` 呼び出し（SHOULD NOT）。例外は M2 の履歴専用の読み取り専用 `git` 子プロセスだけ（§17 OD-20） |

## 4.3 主要な流れ

### 4.3.1 起動

1. `stdout` を JSON-RPC 専用にし、ログを `stderr` とログファイルへ向ける（§12.1）。
2. MCP `initialize` に応じ、`tools/list` を提供できる状態にする。
3. `initialized` の後に、取れれば `roots/list` を要求し（取れなければ cwd と T-00 `set_roots`。§5.1）、マーカーファイルからワークスペースと Python 環境を検出して `workspaces` に登録する。
4. キャッシュ（§4.5）に前回の PaaD があれば開き、ファイルの `sha256` を照合して差分だけ再索引する。無ければ Tree-sitter で全ファイルの骨格を索引する（LSP は起動しない）。
5. ファイル監視を開始する。

LSP は起動時には立ち上げない。該当言語への最初の要求で遅延起動する（§12.3）。

手順 4 の索引（と T-00 `reindex`、§12.4 のバーストの差分索引）の走査が終わるまでも、ツールは待たずに答える。索引の重さでツールの呼び出しが数秒を超えて待たされてはならない（MUST。走査は背景の仕事で、ツールの呼び出しの間は区切りごとに譲る。BUG-0112）。対象のファイルは §5.3 規則 6 のとおりその場で索引する。索引から答えるツール（T-01・T-03・T-04・T-06・T-07・T-08・T-20）は得られた分で答え、`warnings[]` に進みを入れる（MUST。例 `index building: 412/1300 files (31%); results may be incomplete - call again when done`、列挙の前は `listing files`。全ファイルを見た後も関係の整えが終わるまで 99% にとどめ、その間は段を足す: `4222/4222 files (99%), settling relations 12/170`・`ranking symbols` など。BUG-0112）。索引の途中の 0 件の検索は、これで本当の 0 件と見分けられる。走査を終えた後は付けない。理由: 呼ぶ側の AI が「無い」と「まだ索引していない」を応答だけで区別し、呼び直す時期を決められるようにする（BUG-0070）。

### 4.3.2 読み取り要求

1. 引数を検証し、TargetRef を URN に解決する（§5.3）。
2. `revision` が `WORKING_TREE` 以外なら仮想ツリーを用意する（§8.5）。
3. 必要な精度水準を満たすエンジンを選ぶ。LSP が必要で未起動なら起動を非同期に始め、今回は Tree-sitter と PaaD で答える（降格応答）。
4. 結果に優先順位を付け、Token Optimizer が予算内に詰めて直列化する。
5. `_resolution_meta` を付けて返す。

### 4.3.3 編集要求

第 10 章の手順に従う。概略は「解決 → 計画 → 前提検証 → 構文検証 → 原子的書き込み → 再索引 → 診断 → 応答」。

### 4.3.4 外部変更

1. ファイル監視がイベントを受ける（エージェントの別ツールや `git checkout` による変更）。
2. `sha256` を比較し、変わっていれば Tree-sitter で再解析し、`files` / `symbols` / `relations` を差分更新する。
3. 起動中の LSP に `didChangeWatchedFiles`（開いているなら `didChange`）を送る。
4. 発行済みの `cursor` のうち影響を受けるものを失効させる（§5.6）。

## 4.4 プロセスモデル

- Lucianke 本体は 1 プロセス。`stdin` の読み取りと JSON-RPC 応答の書き出しは単一のスレッド（またはタスク）が担い、ツール実行は並行に行う。
- LSP サーバーは言語ごとに 1 子プロセス。`stdout` / `stderr` はパイプで捕捉する（§12.1）。
- 読み取りは並行、編集はワークスペース単位の書き込みロックで直列化する（§5.9）。
- SQLite は WAL モードで 1 接続を書き込み専用（Indexer）、複数接続を読み取り専用（ツール・Query Engine）とする。

## 4.5 永続化とキャッシュ

| 場所 | 内容 | 寿命 |
|---|---|---|
| `<cache_root>/<workspace_hash>/paad.sqlite` | PaaD 本体（WAL） | ワークスペースが存在する限り。`reindex` で再構築 |
| `<cache_root>/<workspace_hash>/trees/` | Tree-sitter 構文木のシリアライズ（任意・MAY） | ファイル `sha256` が変わるまで |
| `<cache_root>/<workspace_hash>/journal/` | 編集トランザクションの書き込み前スナップショット（§10.6） | 直近 N 件（既定 50） |
| `<cache_root>/logs/` | ローテーション付きログ | 既定 7 日 |

`<workspace_hash>` は実体パス（`normcase` 後）の `sha1` の先頭 16 桁。`reindex` と移行できない古いスキーマ版の作り直しは、DB ファイルを削除せず、書き込み接続の中で全オブジェクトを `DROP` して流し直す（Windows では開いている DB ファイルを削除できないため。`edit_seq` は引き継ぐ。実装で確定 2026-10-29）。

`<cache_root>` の既定は OS の慣例に従う（Linux: `~/.cache/lucianke`、macOS: `~/Library/Caches/lucianke`、Windows: `%LOCALAPPDATA%\lucianke\cache`）。環境変数 `LUCIANKE_CACHE_DIR` で上書きできる。**リポジトリ内には何も書かない**（MUST）。

## 4.6 実装言語の中立性と候補スタック

本書の規範はすべて実装言語に依存しない形で書く。概念文書は Rust を推奨し、一方でリポジトリの `requirements.txt` は Python パッケージ `tree-sitter` を挙げている。選定は師匠の判断であり、第 17 章に推奨つきで載せる。参考として、各コンポーネントの候補を示す（規範ではない。能力は採用前に実機で確かめる）。

| コンポーネント | Python 系の候補 | Rust 系の候補 |
|---|---|---|
| MCP SDK | `mcp`（公式 Python SDK） | `rmcp`（公式 Rust SDK） |
| 構文木 | `tree-sitter` + 各言語の `tree-sitter-*` | `tree-sitter` crate |
| 構文パターン | `ast-grep-py` | `ast-grep-core` |
| LSP クライアント | 自前の JSON-RPC クライアント（`asyncio`）または既存ライブラリ | 自前または `lsp-types` + 自前 |
| DB | 標準ライブラリ `sqlite3` | `rusqlite` |
| Git | `dulwich`（Apache-2.0 側を選択。M1 の全用途）。M2 の履歴だけ、利用者の環境の `git` を読み取り専用の子プロセスとして併用する（同梱しない。無ければ dulwich で走査）。`pygit2` は採らない（GPL-2.0-only + リンク例外。第 16 章、§17 OD-20） | `gix`（gitoxide、MIT OR Apache-2.0） |
| ファイル監視 | `watchfiles` | `notify` |
| 経路探索 | SQLite 再帰 CTE（Datalog は未決事項） | 再帰 CTE または組み込み Datalog |
| SMT | `z3-solver` | `z3` crate |
| 字句フォールバック | `ripgrep` 実行ファイルの子プロセス、または自前の正規表現 | `grep-regex` crate |
| ctags | Universal Ctags（GPL-2.0）は利用者の環境にあれば子プロセスとして使う。同梱しない。無くても内蔵のブロック推定で動く | 同左 |
| XML / JSON / YAML / TOML | 標準ライブラリ `xml.etree` / `json` / `tomllib`、`PyYAML`、`jsonpath-ng`。`lxml` は XPath が必要な範囲で | 同等の crate |

候補はすべて第 16 章のライセンス方針を満たすものに限る。採否の根拠は §16.2 と付録 D。

---
# 5. 共通プロトコル規約

本章は全ツールに共通する規約を定める。第 11 章の各ツール仕様は本章を前提とし、共通事項を再掲しない。

## 5.1 MCP との対応

- Lucianke は MCP サーバーとして `tools/list` と `tools/call` を提供する。トランスポートは **`stdio` のみ**（MUST）。ホストが Lucianke をコマンドとして子プロセス起動し、標準入出力で JSON-RPC をやり取りする。HTTP 系トランスポートは提供しない（師匠 2026-10-29「stdio のコマンド実行型。HTTP 型はあまり望ましくない」。必要が生じたら §17 OD-22 として改めて決める）。
- 各ツールは `tools/list` で JSON Schema（入力スキーマ）を公開する。入力スキーマには第 5.2 節の共通パラメータを含めなければならない（MUST）。
- 各ツールは `tools/list` で注記（annotations）を宣言する（MUST）: 書き込まないツールは `readOnlyHint: true`、**全ツール** `openWorldHint: false`（Lucianke は手元の解析とファイルの読み書きだけで外部へ送らない）。宣言が無いと、ホストは read 系のツールにも外部送信の権限を求め、読むだけの周や不在のセッションで使えない（利用者のコーディングエージェントからのフィードバック 2026-10-31）。
- ツール実行の結果は `tools/call` の `content[0]` に `type: "text"` として本章のエンベロープ（第 5.4 節）を格納する。`format: "json"` のときは、同じ JSON を `structuredContent` にも格納してもよい（MAY）。
- ツール実行中の失敗（対象が見つからない、前提条件不一致など）は JSON-RPC エラーではなく、`isError: true` を伴う通常の結果として返す（MUST）。**JSON-RPC エラーにするのは未知のツール名だけ**とする（プロトコル違反）。スキーマに違反する引数（未知のプロパティ・型違反・必須の欠落）は JSON-RPC エラーにせず、`isError: true` の通常の結果で `E_INVALID_ARGUMENT` を返す（エージェントが結果を読んで自己修正できる。設計書 §7.1.2。MCP SDK は引数を検証しないので、検証は Lucianke のパイプラインが行う。S-1 で確認）。
- サーバーは、ホストが roots 能力を宣言していれば、`notifications/initialized` の後に MCP の `roots/list` を要求し（タイムアウト 5 s）、`notifications/roots/list_changed` が来たら取り直す（SHOULD）。**取れなくても動く**こと（MUST）: ホストが roots 能力を宣言しない場合、および roots の `list_changed` とサーバー起点の `roots/list` が仕様から消えた 2026-07-28 版の MCP では、カレントディレクトリをルート候補にし、`configure_workspace set_roots`（T-00）で登録できる（S-1 で確認。設計書 §6.5）。

## 5.2 共通パラメータ

すべてのツールは以下のパラメータを受け付ける。既定値はツールごとに上書きできる（第 11 章に明記）。

| パラメータ | 型 | 既定 | 規約 |
|---|---|---|---|
| `workspace_id` | string | アクティブなワークスペース | 第 7.2 節の識別子。未登録の ID は `E_WORKSPACE_NOT_CONFIGURED` |
| `revision` | string | `"WORKING_TREE"` | `"WORKING_TREE"` \| `"STAGED"` \| `"HEAD"` \| ブランチ名 \| タグ名 \| コミット SHA（短縮可）。解決できなければ `E_REVISION_NOT_FOUND`。**編集系ツール（カテゴリ D）は `"WORKING_TREE"` 以外を受け付けず `E_INVALID_ARGUMENT`** |
| `format` | enum | `"compact"` | `"compact"` \| `"json"` \| `"dot"` \| `"mermaid"`。`dot` / `mermaid` はグラフ出力を持つツール（第 6.4 節）に限る。非対応ツールへの指定は `E_INVALID_ARGUMENT` |
| `max_tokens` | integer | ツール別（第 11 章） | 応答全体の上限。正の整数（0 以下は `E_INVALID_ARGUMENT`）。固定の最小値は置かない（BUG-0063、2026-10-30）: 前置き（ヘッダ・警告・`head_kv`・json の封筒）すら入らない値なら、レコード 0 件・`warnings` に呼び直しに要る `max_tokens` の実測値を入れた成功応答になる（第 5.6 節 4、BUG-0064） |
| `cursor` | string | なし | 第 5.6 節のページネーション継続トークン |

## 5.3 対象指定（TargetRef）

シンボルを指す引数（`target`, `targets[]`, `source`, `sink`）はすべて次の **TargetRef** オブジェクトで統一する。

```json
{ "urn": "lk://app/src/services/auth.py#AuthService.login" }
{ "path": "src/services/auth.py", "symbol_name": "AuthService.login" }
{ "path": "src/services/auth.py", "line": 42, "character": 9 }
```

解決規則:

1. **`urn` 指定**: 第 7 章の URN を完全一致で引く。存在しなければ `E_TARGET_NOT_FOUND`。
2. **`path` + `symbol_name` 指定**: `symbol_name` は `name`、`Outer.name`、`Outer.Inner.name` のいずれでもよく、`qualified_name` の末尾一致で照合する。候補が 2 件以上なら解決せず `E_TARGET_AMBIGUOUS` を返し、`candidates[]` に最大 10 件の URN とシグネチャを列挙する（MUST）。候補 0 件は `E_TARGET_NOT_FOUND`。Tier 3 のデータファイルでは `symbol_name` に第 7.3 節のセレクタ（`xpath:`, `jsonpath:`, `jq:`, `cell:`）を書ける。
3. **`path` + `line`（+ `character`）指定**: その位置を範囲に含む最も内側のシンボルを選ぶ。`character` 省略時はその行の最初の非空白文字位置とする。
4. 3 形式のうち 2 つ以上が同時に与えられた場合は `urn` > `line` > `symbol_name` の優先順位で 1 つだけを使い、残りは無視して `warnings[]` に記録する。
5. `path` はワークスペースルートからの相対パスで、区切りは `/` に正規化する。ルート配下の絶対パスも受け付けて相対化する。**いずれのルートにも含まれないパス、および `..` でルート外へ出るパスは `E_INVALID_ARGUMENT`**（第 13 章の封じ込め）。
6. **索引の途中**: 索引の走査（§4.3.1 手順 4）がまだ届いていないファイルでも、ディスクにあり除外（`.gitignore`・`exclude_globs`）されていなければ、索引に無いことを理由に `E_TARGET_NOT_FOUND` を返してはならない（MUST）。解決の前にそのファイル（`urn` ならその URN のファイル、import をたどる T-04 ならたどった先のファイルも）をその場で索引してから解決する（1 ファイルの解析は数 ms）。その場で索引できなかったときは `E_TIMEOUT`（`details.reason: "index_building"`、`details.retry_after_ms`、`details.files_done` / `details.files_total`）で再試行を促す。ディスクに無い・除外されたファイルは従来どおり `E_TARGET_NOT_FOUND`。理由: 呼ぶ側の AI は not found を「指定の誤り」と読み、索引待ちと知らずに別の探し方へ移る（BUG-0070。仕様は呼ぶ AI への有用性で決める、2026-10-31）。

## 5.4 レスポンスエンベロープ

### 5.4.1 `format: "json"`

```json
{
  "ok": true,
  "tool": "get_file_skeleton",
  "_resolution_meta": { "...": "第 5.5 節" },
  "data": { "...": "ツール固有（第 11 章）" },
  "page": { "next_cursor": null, "returned": 12, "total_estimate": 12 },
  "budget": { "max_tokens": 2000, "used_tokens_est": 1180, "truncated": false },
  "timing_ms": 7,
  "warnings": [],
  "error": null
}
```

| フィールド | 必須 | 規約 |
|---|---|---|
| `ok` | MUST | 成功なら `true`。`false` のとき `error` が非 null で `data` は null |
| `tool` | MUST | 呼ばれたツール名 |
| `_resolution_meta` | MUST | 第 5.5 節。失敗応答にも付ける |
| `data` | MUST | ツール固有の本体 |
| `page` | MUST | `next_cursor` は続きがあるときだけ非 null |
| `budget` | MUST | `used_tokens_est` は第 5.7 節の計数器による推定 |
| `timing_ms` | SHOULD | サーバー内の処理時間（受信から送信直前まで） |
| `warnings` | MUST | 文字列の配列。無ければ空配列 |
| `error` | MUST | 第 5.8 節のエラーオブジェクト、または null |

### 5.4.2 `format: "compact"`

```
# get_file_skeleton ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~1180/2000tok
=== src/services/payment.py (245 lines, skeleton: 28 lines) ===
...
# more: cursor=c482139071 remaining≈34
```

- 1 行目はヘッダ行（MUST）。`tool` と `ok|error` に続けて `conf`（`confidence_level`）、`lsp`（`lsp_status`）、`rev`、`ws`、トークン使用量を `key=value` で並べる。
- 降格（第 5.5 節）があるときはヘッダ直後に `! ` で始まる警告行を置く（MUST）。
- 本文は第 6 章の Compact DSL。
- 続きがあるときは最終行を `# more: cursor=<token>` とする（MUST）。切り詰めた残りの概況を `remaining≈N` と `# summary:` 行で添える（SHOULD）。
- 失敗時はヘッダが `# <tool> error <CODE>` となり、続く `! ` 行にメッセージ、`E_TARGET_AMBIGUOUS` なら候補を 1 行 1 件で列挙する。

### 5.4.3 `format: "dot"` / `"mermaid"`

第 6.4 節。ヘッダ行はコメント構文（`// ` または `%% `）で同じ内容を出す（MUST）。

### 5.4.4 バッチ入力の部分失敗

配列で複数の対象を受けるツール（`paths[]`, `targets[]`）では、項目ごとに成否を持つ。

- `data.items[]` の各要素に `ok` と、失敗時は項目単位の `error`（第 5.8 節と同じ形）を入れる（MUST）。
- 1 件以上成功していれば応答全体は `ok: true` とし、失敗項目の件数を `warnings[]` に書く。
- 全件失敗のときだけ応答全体を `ok: false` とし、`error` には **最初の項目** のエラーを入れ、`details.items[]` に全項目の結果を残す。
- compact では失敗項目をファイルブロックの位置に `! <path>: <CODE> <message>` の 1 行で示す。

## 5.5 精度メタデータ（`_resolution_meta`）

```json
{
  "confidence_level": "exact",
  "is_degraded": false,
  "tier": 1,
  "detected_language": "python",
  "engine_used": "pyright+tree-sitter",
  "lsp_status": "ready",
  "lsp_progress": null,
  "mixed": false,
  "confidence_breakdown": { "exact": 12 },
  "accuracy_warning": null
}
```

| フィールド | 規約 |
|---|---|
| `confidence_level` | `"exact"` \| `"structural"` \| `"heuristic"` \| `"lexical_fallback"`。応答に複数の精度が混在するときは **最も低い** 水準を書く |
| `is_degraded` | `confidence_level` が `exact` 以外なら `true` |
| `tier` | 第 8 章のティア（1〜4）。混在時は最も大きい数 |
| `detected_language` | 言語 ID（`python`, `typescript`, …）。未知なら `"unknown (<拡張子>)"` |
| `engine_used` | 実際に使ったエンジンの識別子（例 `pyright+tree-sitter`, `tree-sitter`, `ctags`, `internal_ripgrep_word_boundary`） |
| `lsp_status` | `"ready"` \| `"indexing"` \| `"standby"` \| `"unavailable"` \| `"n/a"` |
| `lsp_progress` | `indexing` のとき 0〜100 の整数。他は null |
| `mixed` | 複数の精度水準が混在するとき `true` |
| `confidence_breakdown` | 水準ごとの件数 |
| `accuracy_warning` | 降格時に必ず入れる日本語の注意文（MUST）。`exact` のときは null |

降格の定義と各水準の意味は第 8.3 節。

**補足の欄**（T-06 / T-07 の `exact` の答えの `data.name_matched[]`。答えの本体の外に、見出しの付いた別の欄として添える推定）は、`confidence_level`・`mixed`・`confidence_breakdown`・`is_degraded` の集計に数えない。欄の各要素が自分の `confidence_level`（compact は行末の `conf=`）を持つ（実装で確定 2026-10-31、BUG-0107。理由: `exact` の答えに印つきの補足を添えただけで応答全体が `heuristic` の降格〔`! DEGRADED`〕に見えると、呼ぶ側の AI は LSP の答えまで疑い、`exact` の事後条件も確かめられなくなる）。

## 5.6 ページネーションと切り詰め

1. サーバーは `max_tokens` を超えない範囲でレコードを **先頭から順に** 詰める。レコード（1 シンボル、1 参照箇所、1 診断など）を途中で切ってはならない（MUST）。
2. 次のレコードが入らなくなった時点で `next_cursor` を発行し、残りを `page.total_estimate` と `# summary:` 行（ファイル別の残件数）に要約する。ツールが `limit` で打ち切った遅延列を渡し、`page.total_estimate` が渡した件数より大きいときは、渡された分が全部入っても `next_cursor` と `# more: … remaining≈N`（N は入らなかった分 + 渡されなかった分）を出す（BUG-0008、f0f328e）。
3. `cursor` は不透明な短い文字列（実装は `c` + 10 進 9 桁。o200k で約 3 トークン、`# more:` 行全体で約 11 トークン）で、同じセッション・同じツール・同じ引数（`cursor`・`max_tokens`・`format` を除く）・同じワークスペースでのみ有効。中身（オフセット・データ版・引数のハッシュ）はサーバーがセッションのメモリの表に持ち、文字列は表の鍵である（表は直近に発行・使用した 4,096 件まで。古く使われていないものから捨てる）。表に無いカーソル（サーバーの再起動・捨てた・発行していない）と、基になるデータが変わったカーソルは `E_CURSOR_EXPIRED`（先頭から `cursor` なしで呼び直す）。別のツール・引数・ワークスペースのカーソルと、鍵の形でない文字列は `E_INVALID_ARGUMENT`（`details.param: "cursor"`）。理由（BUG-0115、2026-10-31）: 以前は中身を base64url の JSON にして渡していたので、カーソル 1 つが約 70 トークン（112 文字）で、`# more:` 行が参照 3〜4 行ぶんになり、件数の少ない答えは「1 件 + カーソル」が全体より大きく、compact で分けて読めなかった。応答のトークンはすべて呼ぶ側の AI の文脈から出るので、毎ページのカーソルは短いほどよい。
4. ヘッダ・警告行・トレーラもトークン予算に含める。予算が小さすぎてレコードが 1 件も入らない場合（レコードが元から 0 件で前置きだけで予算を超える場合を含む。BUG-0064）は、レコード 0 件・`next_cursor` なし・`warnings` に理由と必要な `max_tokens` の目安を入れて成功応答とする（BUG-0015: オフセットが進まないカーソルは同じ呼び出しの繰り返しになるので出さない。`cursor` は `max_tokens` を含まない引数で結ばれているので、呼び出しに付けたカーソルは `max_tokens` を上げた呼び直しにそのまま使える）。`next_cursor` を返すページは必ず 1 件以上を含む。目安（`raise max_tokens to ~N`）は呼んだ `max_tokens` より大きく、N で同じ引数を呼び直せば警告なしに収まり 1 件以上が返る値とする（MUST。BUG-0110）。N は前置き + 先頭のレコード + `# more:` 行（カーソル）の大きさで、残りが全部トレーラなしでそれより小さく入るなら前置き + 残りの全部。サーバーは N で描いた全文を数え直して確かめる（N ではヘッダの数字の桁が増えうる）。理由: 以前は目安が `# more:` 行（カーソルは base64 で約 60〜80 トークン）を数えず、呼んだ値より小さい値を勧めていた。言われたとおりに呼び直しても同じ 0 件の応答になり、呼ぶ側の AI にページを進める手段が無かった。
5. **予算に対する単調性**（MUST。BUG-0067）: 同じ引数・同じ位置から `max_tokens` だけを上げた応答は、小さい予算の応答より情報が減らない。返るレコードの件数は減らず、各レコードは同じか大きい版（縮退が浅い・丸ごと・切った本文が長い）で返る。このため、丸ごと入らない最初のレコードだけを縮退版（T-02 規約 8 などのツール内の省略。T-05 の途中で切った本文も同じ）にしてそのページの最後の 1 件とし、その後ろには詰めない（残りは `next_cursor` で次ページ）。`# summary:` 行の取り分はレコードに使える予算の 1/4 まで、かつ先頭のレコードの最も小さい形（縮退版。無ければ丸ごと）と `# more:` 行を押し出さない分までとし、それより長い要約は末尾を「…」で切る（ふつうの予算では丸ごと入る。要約は SHOULD なので、1 件も返らなくなるくらいなら要約を削る。BUG-0110: 1/4 を必ず取り分けていた間は、先頭の 1 件とカーソルは入るのに要約の分で 0 件になる予算の帯があり、compact の既定の形式で全体より少し小さい予算ではページが進められなかった）。理由: 主な利用者は MCP で呼ぶ AI エージェントで、予算を上げて呼び直したら前に見えていたものが消える応答は、呼び直しで情報を失わせる（以前は F-PY の T-02 json で `max_tokens` 6000 → 2 件、8000 → 1 件だった）。

## 5.7 トークン計数

- 計数器は `o200k_base` 互換の近似とする（MUST）。実装は厳密なトークナイザの代わりに、ベンチマーク（第 14 章）で誤差 ±10% 以内と確認したヒューリスティック（文字種別と連の数の係数。設計書 §7.2.1。S-12 で確認: 文字数だけの係数は日本語や base64 で 10〜60% 過小になった）を用いてもよい（MAY）。
- `budget.used_tokens_est` には応答テキスト全体（ヘッダ・トレーラ含む）の推定値を入れる。
- `max_tokens` は応答テキスト（`content[0].text`）全体の上限で、`used_tokens_est`（compact のヘッダの `~used`・json の `budget.used_tokens_est`）は **描いた応答テキスト全体を計数器で数え直した値** と一致する（MUST。json は `timing_ms` を最大桁数で数えるので、実際の全文より最大 4 トークン上になりうる）。ヘッダ・警告行・降格の警告行・`head_kv`・トレーラ（compact）、封筒の全キー・`_resolution_meta`・`data` の静的部分・`warnings`（json）もすべて数える。降格の警告行・ヘッダの `conf=`・`_resolution_meta` はそのページに入ったレコードの精度で出るので、前置きはページに出る形で数える（後ろに精度の低いレコードがあっても、そのページに出ない注意文の分を取り分けない。実装で確定 2026-10-31、BUG-0116。理由: 以前は全レコードの精度で前置きを数え、T-01 の compact `max_tokens=260` で約 30 トークンの前置きに 106 を取り分け、予算の約 3 割を使わずに先頭のファイルを縮め要約を切っていた）。
- `format: "json"` では、レコードを `data` の JSON の大きさで数える（compact の行の大きさではない。同じ内容でも JSON は compact の数倍になるので、同じ `max_tokens` で入る件数は json のほうが少ない）。`structuredContent` は `content[0].text` と同じ内容の写しなので、別に数えない。

## 5.8 エラー分類

| コード | 意味 | 再試行の指針 | 付随情報 |
|---|---|---|---|
| `E_INVALID_ARGUMENT` | スキーマに違反する引数（未知のプロパティ・型違反・必須の欠落。JSON-RPC エラーにはしない。§5.1）、およびスキーマは通るが規約違反（非対応 format、小さすぎる max_tokens、ルート外パス、編集系に revision 指定） | 引数を直す | `details.param` |
| `E_WORKSPACE_NOT_CONFIGURED` | `workspace_id` が未登録、または対象パスからルートを解決できない | `configure_workspace` を呼ぶ | `details.known_workspaces[]` |
| `E_REVISION_NOT_FOUND` | `revision` を Git オブジェクトに解決できない | 参照名を直す | |
| `E_TARGET_NOT_FOUND` | TargetRef が 0 件に解決 | 名前を `search_workspace_symbols` で確かめる | `details.searched_in` |
| `E_TARGET_AMBIGUOUS` | TargetRef が 2 件以上に解決 | `candidates[]` の URN で呼び直す | `candidates[] {urn, signature, path, line}`（最大 10） |
| `E_LSP_UNAVAILABLE` | 操作に LSP が必須だが起動できない・応答しない（降格では代替できない操作: `rename_symbol`、`apply_code_action`） | LSP の導入状況を `configure_workspace status` で確認 | `details.language`, `details.reason` |
| `E_PRECONDITION_FAILED` | 編集の前提不一致（`expected_file_sha256` 不一致、解決から書き込みまでの間に外部変更を検知）。**ファイルは変更されていない** | 対象を読み直して再実行 | `details.path`, `details.expected`, `details.actual` |
| `E_SYNTAX_INVALID` | `new_code` / `rewrite` が対象言語として解析できない、または適用結果に構文エラーノードが生じる | コードを直す | `details.errors[] {line, message}` |
| `E_MUTATION_BLOCKED` | 降格ファイルへの編集で、`allow_heuristic_mutation: true` があっても安全に確定できない（推定した境界が一意に決まらない、改名後の名前が対象ファイルに既出。第 10.5 節）、または Windows で対象が読み取り専用属性を持つ（`details.reason: "read_only_file"`。第 10.6 節）。許可フラグが無いだけならエラーにせず dry_run に切り替える | 範囲を `get_symbol_bodies` で確かめ、対象を絞って再実行 | `details.confidence_level`, `details.reason` |
| `E_QUERY_REJECTED` | `query_project_db` に SELECT / WITH 以外、または禁止関数を含む文 | 文を直す | `details.reason` |
| `E_CURSOR_EXPIRED` | `cursor` がサーバーの表に無い（再起動・古いものから捨てた。§5.6-3）、または基データが更新済み | `cursor` なしで先頭から呼び直す | `details.reason`（`unknown_cursor` / `data_changed`） |
| `E_TIMEOUT` | ツール固有の `timeout_ms` または既定の上限を超過。部分結果があれば `data` に入れる。待てば直る状態: 起動の最中で索引がまだ開いていない（`details.reason: "index_opening"`。BUG-0046）、索引の走査の途中で対象のファイルをその場で索引できなかった（`"index_building"`。§5.3 規則 6、BUG-0070）、`apply_code_action` の種別 `quickfix` で LSP がいまの内容の診断をまだ出しておらず候補が確かめられない（`"diagnostics_pending"`。`details.server` / `servers`。T-13 処理規約 3、BUG-0099） | 範囲を狭める。`index_opening` / `index_building` / `diagnostics_pending` は `details.retry_after_ms` の後に同じ呼び出しを再試行する | `details.partial`、`details.reason`、`details.retry_after_ms` |
| `E_UNSUPPORTED` | 対象ティアではその操作を提供しない（例: Tier 3 への `rename_symbol`） | | `details.tier`、`details.reason`（`invalid_data_file`: 構文が壊れたデータファイル〔T-02〕、`revision_not_supported_until_M2`、`not_in_milestone`〔`details.milestone`〕、`file_too_large`、`secret_file`、`binary_file` など） |
| `E_INTERNAL` | 上記以外の内部エラー。スタックはログにのみ出す | | `details.log_ref` |

エラーオブジェクトの形:

```json
{ "code": "E_TARGET_AMBIGUOUS", "message": "symbol_name 'login' は 3 件に一致します", "details": {}, "candidates": [] }
```

## 5.9 冪等性と並行性

- 読み取りツール（カテゴリ 0・A・B・C・E・F・G）は冪等であり、並行して呼び出してよい。
- 編集ツール（カテゴリ D）は冪等ではない。**ワークスペースごとに単一の書き込みロック**で直列化し（MUST）、ロック待ちの上限は 5,000 ms とする。超過は `E_TIMEOUT`。
- 編集中に到着した読み取り要求は、編集前のスナップショットではなく編集確定後の状態を見る（読み取りは書き込みロックの解放を待つ）。

## 5.10 位置・パス・時刻の表記

| 項目 | 規約 |
|---|---|
| 行番号 | 1 始まり |
| 列番号 | 1 始まり、**Unicode コードポイント**単位。LSP の UTF-16 単位との変換はサーバー内部で行い、外に出さない |
| 行範囲 | `@L<start>-<end>`（両端を含む）。1 行なら `@L<n>` |
| パス | ワークスペースルート相対、`/` 区切り。`#`, `%`, 空白は URN 内でパーセントエンコード |
| 時刻 | ISO 8601、タイムゾーン付き |
| SHA | ファイル内容の SHA-256 を小文字 16 進 64 桁。Git のオブジェクト ID と混同しないよう、フィールド名は常に `sha256` / `commit_sha` で区別する |

---

# 6. 出力形式

## 6.1 Compact DSL（既定）

Compact DSL は、LLM が最小トークンで誤読なく読める行指向のテキスト形式である。文法を EBNF で定める（終端記号は二重引用符、`NL` は改行、`indent` は 2 スペースの 0 回以上の繰り返し）。

```ebnf
document     = header , { banner } , body , [ trailer ] ;

header       = "# " , tool , " " , ( "ok" | "error " , code ) , { " " , kv } , [ " ~" , int , "/" , int , "tok" ] , NL ;   (* 成功ヘッダでは ~used/maxtok が必須 *)
kv           = key , "=" , value ;
banner       = "! " , text , NL ;

body         = { block } ;
block        = file-block | symbol-block | ref-line | diag-line
             | tree-line | path-block | code-block | table | note | kv-line | banner ;   (* banner は本文中にも置ける（§5.4.2・§5.4.4）*)

kv-line      = key , ":" , [ " " , text ] , NL ;   (* ツール固有の見出し行: target:, applied:, rows:, trace:, commits: など。値は空でもよい *)

file-block   = "=== " , path , " (" , int , " lines" , [ ", " , int , " cells" ] ,
               [ ", skeleton: " , int , " lines" ] , [ ", sha=" , 12*12 HEXDIG ] , ")" , " ===" , NL ,
               { import-line | symbol-block | note } ;
import-line  = "imports: [" , text , "]" , NL ;   (* text はモジュールごとの項目を "; " で区切る。規約 10 *)

symbol-block = indent , "[" , kind , "] " , [ modifiers , " " ] , name , [ sig-inline ] ,
               " @L" , range , [ " " , fold ] , NL ,
               { indent , "  " , attr-line } ;
kind         = "Module" | "Class" | "Interface" | "Struct" | "Enum" | "Trait" | "Type"
             | "Function" | "Method" | "Constructor" | "Field" | "Property" | "Variable" | "Constant"
             | "Table" | "Column" | "Endpoint" | "Message" | "Service" | "Node" | "Edge"
             | "Cell" | "Tag" | "Key" | "Section" | "File" ;
modifiers    = modifier , { " " , modifier } ;
modifier     = "pub" | "priv" | "prot" | "static" | "async" | "abstract" | "export" ;
sig-inline   = "(" , text , ")" , [ " -> " , text ] ;
fold         = "{ ..." , int , ( " lines" | " nodes" ) , " omitted }" ;   (* nodes は XML の max_depth 超過。T-02 の 4 *)
attr-line    = attr-key , ": " , text , NL ;
attr-key     = "sig" | "doc" | "urn" | "type" | "body" | "when" | "conf" | "scip" | "decorators" | "raises"
             | "refs" | "rank" | "sha" | "declared_in" | "change" ;

ref-line     = indent , path , ":" , int , [ ":" , int ] , "  " , [ "[" , use , "] " ] ,
               [ "in " , name , "  " ] , snippet , NL ;
use          = "r" | "w" | "rw" | "call" | "type" | "import" | "impl" ;

diag-line    = sev , " " , path , ":" , int , [ ":" , int ] , " " , [ "[" , source , ":" , code , "] " ] ,
               message , [ "  fix=" , action-id ] , NL ;
sev          = "E" | "W" | "I" | "H" ;

tree-line    = indent , ( "<- " | "-> " ) , name , " @" , path , ":" , int , [ " (cycle)" ] ,
               [ "  when: " , text ] , [ "  conf=" , level ] , NL ;

path-block   = "path " , int , " (" , int , " hops)" , NL , { indent , step-line } ;
step-line    = int , ". " , name , " @" , path , ":" , int , [ "  var=" , text ] , [ "  when: " , text ] , NL ;

code-block   = "```" , [ lang ] , NL , { [ "@L" , int , ": " ] , text , NL } , "```" , NL ;
table        = "| " , text , { " | " , text } , " |" , NL , { "| " , text , { " | " , text } , " |" , NL } ;
note         = "- " , text , NL ;

trailer      = [ "# summary: " , text , NL ] , [ "# more: cursor=" , token , [ " remaining≈" , int ] , NL ] ;   (* summary が先 *)

range        = int , [ "-" , int ] ;
level        = "exact" | "structural" | "heuristic" | "lexical_fallback" ;
```

規約:

1. `symbol-block` の `name` は、ファイルブロック内ではファイル内修飾名（`Class.method`）、ワークスペース横断の一覧では `path#qualified` とする。
2. **`urn:` 属性行は、URN がその記号の行から導けないときだけ付ける**（導ける URN の行は MUST NOT。応答のトークンは呼ぶ側の文脈を使うため。BUG-0069、2026-10-31）。導き方は文字列の連結だけで、`lk://<ws>/<path>#<qualified>`（`<ws>` はヘッダの `ws=`）:
   - ワークスペース横断の一覧（`search_workspace_symbols`、`resolve_symbols`。規約 1 の `path#qualified`）: `lk://<ws>/` + 記号名。`[Module] <path>` は `lk://<ws>/<path>`（セレクタなし）。
   - ファイルブロック内（`get_file_skeleton`）: `<path>` はファイルブロックの見出しのパス、`<qualified>` は入れ子をたどった名前を `.` でつないだもの（最上位の行は修飾名そのもの。`[Class] PaymentProcessor` の下の `[Method] process_charge` は `PaymentProcessor.process_charge`）。
   - 導けないもの（`urn:` 行で示す）: arity・sighash の付いた URN（オーバーロード・同名の再定義の区別。`#parse/2`、`#charge/2~2797d533`）、`lex:` ・`ctags:` などのセレクタ、パスや名前にパーセントエンコードの要る文字を含むもの、ヘッダの `ws=` と違うワークスペースのもの。
   - `urn:` 行を付ける候補の範囲は `get_file_skeleton` の `include_urns`（既定 `public`）で選ぶ。json の SymbolRecord（§6.2）は常に `urn` を持つ。
   - 記号は、導いた URN でも、TargetRef の `path` + `symbol_name`（行が示す修飾名。§5.3）でも指せる（`urn` / `path` + `symbol_name` / `path` + `line` の 3 形式を受けるのは T-04・T-05・T-06・T-07・T-09・T-10・T-11・T-14・T-16・T-17）。導けない URN は、`path` + `symbol_name` が曖昧になる記号（オーバーロード）なので、`urn:` 行の値か `path` + `line` で指す。
3. `doc:` は Doc コメントの 1 行目要約のみ。全文は `resolve_symbols` の JSON で返す。
4. `conf=` は、応答内で `_resolution_meta.confidence_level` と異なる水準の要素にだけ付ける（混在時の個別表示）。
5. `code-block` 内の `@L<n>: ` 接頭辞は `with_line_numbers: true`（既定）のとき付ける。コード本文はエスケープせずそのまま出す。コード内に ``` が含まれる場合はフェンスを ```` に延長する。
6. 1 行の長さは 200 文字を超えたら末尾を `…` で省略する（`snippet`, `doc`, `when` に適用。`sig` とコードブロックには適用しない）。ただし `search_workspace_symbols` のヒットの `sig-inline` は 160 文字（`SEARCH_SIGNATURE_MAX_CHARS`）を超えたら引数部の末尾を `…` にして `)` で閉じ、戻り値は残す（`(self, out: ToolOutput, *, max_tokens: int, …) -> Page`。長い戻り値も `…` で切る）。json の `signature` は全文（BUG-0069）。
7. `kind` の `File` は、シンボルに分解せずファイル全体を 1 要素として扱う場合（T-18 の Tier 3 / Tier 4 のファイルの変更要素。`modified_symbols[].kind`）に使う。`symbols` テーブルには格納しない。
8. ツール固有の見出し行（`kv-line`: `target:`、`applied:`、`resolved:` など）はインデント 0 に置く。インデントされた `key: text` は `attr-line` で、キーは `attr-key` の語彙に限る。
9. 個数を示す要約行（`[Node] ×23 @L2-80`、`[Tag] testcase ×412 @L3-5118`）も `symbol-block` の形に従い、`@L` と範囲を省かない（範囲 `@Ls-e` は、集約した最初の要素の開始行から最後の要素の終了行まで）。範囲の後ろに `(25 lines)` のような語を置かない（行数は範囲から分かる。折りたたみは `{ ...N lines omitted }` と `{ ...N nodes omitted }` だけ）。§11 の例はこの文法に適合させてあり、検査器 `tests/contract/compact_grammar.py` が EBNF 由来の行種で検査する。なお §5.4.2 の例の `...` は省略記号であり、検査の対象ではない（実装で確定 2026-10-29）。
10. `import-line` はモジュールごとにまとめ、モジュール名を名前ごとに繰り返さない（BUG-0069、2026-10-31）。項目の区切りは `; `、項目は `module`（`import module`。`import a.b as c` は `a.b as c`）か `module: name, name as alias`（`from module import ...`。同じモジュールの文が複数あっても 1 項目にまとめる。`from . import y` は `.: y`、`from x import *` は `x: *`）。JS / TS は `import X, { A as B } from "m"` → `m: X, A as B`、`import * as ns from "m"` → `m: * as ns`、`import "m"` → `m`。並びはモジュールが最初に現れた順。別名（`as`）は省かない。例: `imports: [base64; collections.abc: Callable, Mapping, Sequence; lucianke.core.records: Record, ToolOutput]`。json の `items[].imports[]`（T-02）は従来どおり 1 名前 1 要素（`"Callable from collections.abc"`）。

### 6.1.1 代表例

```
# get_file_skeleton ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~290/2000tok
=== src/services/payment.py (245 lines, skeleton: 13 lines, sha=3f9c1a2b4c5d) ===
imports: [decimal: Decimal; services.repo: OrderRepository; services.gateway: PaymentGateway]
[Class] PaymentRequest @L12-18
  doc: 決済要求。amount > 0 が必須
[Class] PaymentProcessor @L25-170
  [Constructor] __init__(self, repo: OrderRepository, gateway: PaymentGateway) @L28-32 { ...4 lines omitted }
  [Method] pub async process_charge(self, req: PaymentRequest) -> Receipt @L40-115 { ...75 lines omitted }
    doc: 注文に対する決済を実行し領収書を発行する
    raises: PaymentDeclined, LimitExceeded
  [Method] priv _validate_limit(self, amount: Decimal) -> bool @L117-140 { ...23 lines omitted }
[Function] pub parse(data: bytes) -> PaymentRequest @L180-190 { ...9 lines omitted }
  urn: lk://app/src/services/payment.py#parse/1
[Function] pub parse(data: bytes, strict: bool) -> PaymentRequest @L192-210 { ...17 lines omitted }
  urn: lk://app/src/services/payment.py#parse/2
```

`PaymentProcessor.process_charge` の URN は `lk://app/src/services/payment.py#PaymentProcessor.process_charge` と行から導けるので `urn:` 行を付けない。同名の `parse` の 2 つは arity で区別した URN で、行から導けないので付ける（規約 2）。

```
# find_references_and_implementations ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~420/2000tok
target: lk://app/src/services/payment.py#PaymentProcessor.process_charge
implementations: (none; concrete method)
references: 3 files, 5 sites
src/api/routes.py:88:20  [call] in create_order  receipt = await processor.process_charge(req)
src/api/routes.py:131:20  [call] in retry_order  receipt = await processor.process_charge(req)
tests/test_payment.py:44:12  [call] in test_charge_ok  r = await p.process_charge(req)
```

```
# get_diagnostics ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~180/2000tok
E src/services/payment.py:58:16 [pyright:reportArgumentType] "str" は "Decimal" に割り当てできません  fix=qf_7a1c
W src/services/payment.py:9:1 [ruff:F401] `os` imported but unused  fix=qf_02b9
```

## 6.2 `json`

第 5.4.1 節のエンベロープ。`data` の形はツールごとに第 11 章で定める。`data` 内のシンボル表現は次の **SymbolRecord** を共通型とする。

```json
{
  "urn": "lk://app/src/services/payment.py#PaymentProcessor.process_charge",
  "scip_symbol": null,
  "path": "src/services/payment.py",
  "name": "process_charge",
  "qualified_name": "PaymentProcessor.process_charge",
  "kind": "Method",
  "modifiers": ["pub", "async"],
  "range": { "start_line": 40, "end_line": 115 },
  "selection_range": { "line": 40, "start_col": 15, "end_col": 29 },
  "signature": "(self, req: PaymentRequest) -> Receipt",
  "return_type": "Receipt",
  "doc_summary": "注文に対する決済を実行し領収書を発行する",
  "confidence_level": "exact"
}
```

## 6.3 `dot` / `mermaid`

- ノード ID は URN（引用符で囲む）、ラベルは `qualified_name`（`path` を tooltip / 属性に入れる）。
- エッジのラベルは関係種別（`CALLS`, `IMPLEMENTS`, `INHERITS`, `REFERENCES`, `FLOWS_TO`, `CO_CHANGES`）。
- 降格要素はノード属性 `conf="heuristic"`（dot）/ クラス `:::degraded`（mermaid）で区別する。
- ヘッダ行はコメントで出す: dot は `// # <tool> ok ...`、mermaid は `%% # <tool> ok ...`。

## 6.4 グラフ出力に対応するツール

`query_call_hierarchy`、`find_references_and_implementations`、`find_code_paths`、`analyze_change_impact`、`analyze_git_diff_symbols`、`query_project_db`（結果列に `source_urn` と `target_urn` を含むとき）。他のツールへの `dot` / `mermaid` 指定は `E_INVALID_ARGUMENT`。

---

# 7. シンボル識別（URN）

## 7.1 目的

ツール間でシンボルを受け渡す安定した識別子を定める。行番号に依存せず、編集でずれず、ティア 1〜4 のすべての対象（コード・データ・Notebook・未知言語）を一様に指せること。

## 7.2 形式

```ebnf
urn          = "lk://" , workspace-id , "/" , path , [ "#" , selector ] ;
workspace-id = 1*( ALPHA | DIGIT | "-" | "_" | "." ) ;
path         = segment , { "/" , segment } ;                    (* パーセントエンコード済み *)
selector     = code-sel | data-sel | nb-sel | tag-sel | lex-sel ;

code-sel     = qualified , [ "/" , arity ] , [ "~" , sighash ] ;  (* Tier 1〜2 *)
qualified    = ident , { "." , ident } ;
arity        = DIGIT , { DIGIT } ;
sighash      = 8*8 HEXDIG ;

data-sel     = ( "xpath:" | "jsonpath:" | "jq:" | "toml:" | "yaml:" ) , expr ;   (* Tier 3 *)
nb-sel       = "cell:" , DIGIT , { DIGIT } , [ "/" , qualified ] ;              (* .ipynb *)
tag-sel      = "ctags:" , ident , "@L" , DIGIT , { DIGIT } ;                    (* Tier 4 heuristic *)
lex-sel      = "lex:" , ident , "@L" , DIGIT , { DIGIT } ;                      (* Tier 4 lexical *)
```

- `workspace-id` は `configure_workspace` が割り当てる。既定はルートディレクトリ名（英数・`-`・`_`・`.` 以外は `_` に置換）。衝突したら `-2`, `-3` を付す。
- セレクタを省いた URN はファイルそのものを指す。
- `qualified` は言語のスコープ階層（モジュール内のクラス・関数・ネスト）を `.` で連ねる。モジュール名（パッケージパス）は `path` に含まれるので `qualified` には入れない。
- TS の型リテラルのメンバー（`type KyOptions = { retry?: …; tick(): void }`）は持ち主の下に連ねる: 型の別名のメンバーは `KyOptions.retry`、メンバーの型注釈に書いた入れ子の型リテラル（`inline: { before: string[] }`）は `KyOptions.inline.before`、インターフェースのプロパティ・クラスのフィールド・getter の型注釈の型リテラルも同じ（`Opts.nested.inner.leaf`・`Server.opts.z`）。合併の別々の型リテラルの同名のメンバー（`{ kind: 'a' } | { kind: 'b' }`）は下の衝突の規則に従う（最後が素の `Shape.kind`、それより前は `~sighash`。再定義ではないので警告は出さない）（実装で確定 2026-10-31、BUG-0114。理由: TS の設定・引数のオブジェクトは型リテラルで書くことが多く、その各プロパティは AI が使用箇所を最もよく探す対象。インターフェースのメンバーと同じく T-02〜T-06 の対象にする）。
- `/arity` と `~sighash` は **オーバーロードや同名の再定義があるときだけ** 付ける（MUST NOT 常時付与）。実装の規則: 同じ `qualified` の衝突グループの中で **アリティが 2 種類以上あるときだけ** `/arity` を付ける（アリティが 1 種類だけの衝突グループには付けない。実装で確定 2026-10-29）。`sighash` は正規化したシグネチャ文字列の SHA-256 先頭 8 桁。
- シグネチャまで同一の再定義（条件分岐の中で同じ関数を定義し直す場合など）は、ファイル内で **最後** の定義を素の URN とし、それ以外には `~` + SHA-256(正規化シグネチャ + 開始行) の先頭 8 桁を付ける。
- `scip_symbol`（SCIP の標準シンボル文字列）は URN とは別に `symbols.scip_symbol` 列で保持し、相互運用に用いる。

例:

| 対象 | URN |
|---|---|
| Python のメソッド | `lk://app/src/services/payment.py#PaymentProcessor.process_charge` |
| C++ のオーバーロード | `lk://engine/src/math.cpp#clamp/3~9f2a4c10` |
| ファイル | `lk://app/pyproject.toml` |
| TOML のキー | `lk://app/pyproject.toml#toml:project.dependencies` |
| JUnit XML の失敗ケース | `lk://app/reports/junit.xml#xpath:%2F%2Ftestcase%5Bfailure%5D` |
| Notebook の第 3 セル内関数 | `lk://app/notebooks/eda.ipynb#cell:3/load_frame` |
| 未知言語（ctags 推定） | `lk://app/legacy/calc.cob#ctags:COMPUTE-TAX@L120` |
| 未知言語（字句一致） | `lk://app/conf/rules.custom#lex:MAX_RETRY@L8` |

## 7.3 安定性の規約

| 事象 | URN の扱い |
|---|---|
| シンボル本体の編集（名前・親・アリティ不変） | 不変（MUST） |
| 同一ファイル内で行位置が動く | 不変（MUST） |
| `rename_symbol` | 新 URN を発行し、応答に旧→新の対応表を含める（MUST） |
| ファイル移動 | 新 URN。`git_symbol_changes` に `RENAMED` として記録 |
| `tag-sel` / `lex-sel` | 行番号を含むため編集で変わりうる。応答の `accuracy_warning` にその旨を書く |
| `revision` 付き照会 | URN 自体は revision を含まない。どの revision で解決したかはヘッダ `rev=` と `_resolution_meta` が示す |

## 7.4 URN の照合

`urn` で TargetRef を与えられたサーバーは、まず完全一致で `symbols` を引き、見つからなければ **`~sighash` と `/arity` を外した形で再照合** してよい（MAY）。再照合で 1 件に決まれば `warnings[]` に「URN を緩和して解決した」と記録して続行し、複数なら `E_TARGET_AMBIGUOUS` とする。

---
# 8. 対応言語ティアとフォールバック

## 8.1 ティア定義

| ティア | 対象 | 構文エンジン | 意味解決エンジン | 到達しうる最高精度 |
|---|---|---|---|---|
| **Tier 1: 完全決定論解析** | Python（`.py`, `.pyi`）, TypeScript, JavaScript, Rust, Go, C, C++, C#, Kotlin, Swift | Tree-sitter（各言語文法）＋構文パターン検索 | 言語別 LSP（多重化プール、§12.3）。SCIP インデックスの取り込みは任意（§17 OD-4） | `exact` |
| **Tier 2: 言語境界・構造化仕様・クエリ** | Cython（`.pyx`, `.pxd`）, Jupyter（`.ipynb`）, SQL（`.sql` およびコード内 SQL 文字列）, Graphviz DOT（`.dot`, `.gv`）, Mermaid（`.mmd` と Markdown 内ブロック）, OpenAPI, Protobuf（`.proto`）, GraphQL | Tree-sitter 文法または専用パーサ | 専用リンカ（Python ホストコードとの橋渡し、§8.6） | `exact`〜`structural` |
| **Tier 3: 階層データ・マークアップ・テンプレート** | XML（`.xml`, `.svg`, JUnit/coverage 出力）, JSON / JSONL, YAML, TOML, Jinja2（`.j2`）, Markdown, reStructuredText | Tree-sitter または構造パーサ（XPath / JSONPath / jq 相当のセレクタ評価） | スキーマ推論（JSON Schema / XSD / Pydantic との照合、§8.6） | `structural` |
| **Tier 4: 未知・非対応** | 独自 DSL、レガシー言語（COBOL, Fortran など）、設定ファイル、文法の無い任意のテキスト | Universal Ctags ルール＋インデント/括弧ブロック推定 → 単語境界の字句一致 | なし | `heuristic` / `lexical_fallback` |

言語の判定は、拡張子 → シバン行 → モードライン → `.gitattributes` の `linguist-language`（MAY）の順で行い、判定結果を `files.language` と `files.tier` に記録する。

## 8.2 ティア別の提供機能

○ = 提供（MUST）、△ = 提供するが精度は `structural` 以下または一部操作のみ、× = `E_UNSUPPORTED`。

| 機能（ツール） | Tier 1 | Tier 2 | Tier 3 | Tier 4 |
|---|---|---|---|---|
| 骨格（T-02） | ○ | ○ | ○（タグ・キー階層と要素数） | △（ctags / インデントのアウトライン） |
| シンボル検索（T-03） | ○ | ○ | ○（キー・タグ名） | △（ctags 名・字句一致） |
| 定義解決（T-04） | ○ | △ | △（セレクタの存在確認と型推論） | △（定義パターンの推定） |
| 本体取得（T-05） | ○ | ○ | ○（セレクタ） | △（±15 行の窓） |
| 参照・実装（T-06） | ○ | △ | △（キー参照の字句一致） | △（単語境界一致。実装の検索は `unavailable`） |
| 呼び出し階層（T-07） | ○ | △（SQL・IDL は結合関係のみ） | × | △（`name(` の grep 1 段。`lexical_fallback`） |
| 診断（T-08） | ○ | △（構文エラー） | △（構文・スキーマ違反） | △（診断源が無く 0 件を返す。エラーにしない） |
| 変更影響（T-09） | ○ | △ | △（共変更結合のみ） | △（共変更結合のみ） |
| シンボル編集（T-10） | ○ | ○（`.ipynb` はセル内、Cython は関数） | △（セレクタ範囲の置換。M2） | △（インターロック下で dry_run 既定） |
| リネーム（T-11） | ○ | △（LSP があるものだけ） | × | △（字句置換。降格インターロック下） |
| 構文パターン（T-12） | ○ | △（Tree-sitter 文法があるもの） | △（JSON/YAML/TOML） | × |
| Code Action（T-13） | ○ | △ | × | × |
| 経路（T-14） | ○ | △ | × | × |
| 契約抽出・検証（T-15, T-16） | ○ | △（OpenAPI / Gherkin は抽出のみ） | △（JSON Schema は抽出のみ） | × |
| 履歴・差分（T-17, T-18） | ○ | ○ | ○（ファイル単位） | ○（ファイル単位） |
| DB クエリ（T-19, T-20） | ○ | ○ | ○ | ○ |

表の Tier 4 の列（T-06 の実装検索、T-07、T-08、T-11）は、設計書 §7.8.2 の判定表（文法なしの列）に揃えた（実装で確定 2026-10-29。実装は §7.8.2 を正にしている）。Tier 3 は LSP の状態によらず常に `structural` で、ヘッダの `lsp` は `n/a`。

## 8.3 精度水準の定義

| 水準 | 保証すること | 保証しないこと | エンジンの例 |
|---|---|---|---|
| `exact` | 型推論とスコープ解決を経た、コンパイラと同じ答え。同名別シンボルを区別し、クロスファイルの参照を漏らさない（動的ディスパッチと `eval` 相当は除く） | 実行時にしか決まらない呼び出し | LSP（+ Tree-sitter） |
| `structural` | 構文木に基づく答え。シンボルの境界・種別・同一ファイル内のスコープは正確。クロスファイルの名前解決はインポート文の追跡による推定 | 型、オーバーロードの区別、再エクスポートの追跡 | Tree-sitter、専用パーサ |
| `heuristic` | 定義らしき位置（関数・セクションの開始行）の推定と、インデント・括弧によるブロック終端の推定。T-06 / T-07 では、受け手の型が分からない呼び出し（`x.name(...)`）と属性の読み書き（`x.name`・`x.name = v`。T-06 だけ）を、呼び出し箇所から近い段（同じファイル → 箇所を含むテストフィクスチャ → ワークスペース）にその名前のメソッド（属性の読み書きはクラスのメンバーとインスタンス属性）が 1 つしか無いことを根拠に対象へ結んだ参照・呼び出し（箇所そのものは構文木で正確） | 境界の正確さ、スコープ、受け手の型（名前で結んだ行） | ctags、ブロック推定、名前の一意性（`_name_calls`） |
| `lexical_fallback` | 単語境界つきの文字列一致。該当行と前後のスニペット | 同名の区別、コメントや文字列内の一致の除外 | 内部 grep |

応答に複数水準が混在するとき、`_resolution_meta.confidence_level` は最も低い水準、`mixed: true`、内訳は `confidence_breakdown`（§5.5）。

`is_degraded` は「そのツールが到達しうる最高水準より低い」ときに `true` とする。最高水準が `exact` でないツールがある: `structural_search_replace`（T-12）は構文一致が本来の精度なので `structural` で `is_degraded: false`、`configure_workspace`（T-00）・`get_project_db_schema`（T-19）はサーバー自身のメタデータを返すので常に `exact`、`query_project_db`（T-20）は行の `confidence_level` から集計する。Git 時系列（T-17、T-18）の精度は「コミットをまたいだシンボル同定の確からしさ」で定義し、LSP 解決済みかつ各断面で一意なら `exact`、シグネチャハッシュで同定したら `structural` とする。

## 8.4 フォールバック・ラダー

ツールは要求ごとに、対象ファイルの言語と現在のエンジン状態から **到達できる最高水準** を選び、得られなければ 1 段ずつ降りる。止まらないことが規約である（MUST）。

| 段 | 水準 | 進む条件 | 降りる条件 | `engine_used` |
|---|---|---|---|---|
| 1 | `exact` | 当該言語の LSP が `ready` | LSP が未導入・起動中（`indexing`）・無応答（§12.3 のタイムアウト）・当該操作に未対応 | `<lsp名>+tree-sitter` |
| 2 | `structural` | Tree-sitter 文法または専用パーサがある | 文法が無い、または解析エラーノードが対象範囲を覆う | `tree-sitter` / `parser:<名>` |
| 3 | `heuristic` | ctags の言語定義がある、またはインデント/括弧構造が検出できる | 行構造から定義が推定できない | `ctags` / `block-heuristic` |
| 4 | `lexical_fallback` | 常に可能 | — | `internal_ripgrep_word_boundary` |

規約:

1. 降りたとき、`_resolution_meta.is_degraded = true`、`accuracy_warning` に **何が使えず、何が信用できないか** を書く（MUST）。compact ではヘッダ直後に `! DEGRADED ...` を置く。
2. 段 1 が `indexing` で降りた場合は `lsp_status: "indexing"` と `lsp_progress` を添え、エージェントが後で呼び直せるようにする。起動完了を待ってブロックしてはならない（NFR-OPS-3）。
3. 段 4 の検索は単語境界（`\b<name>\b`）を必須とし、定義らしさ（`<name>\s*[:=(]`、言語によらない `def|function|fn|sub|proc|class`）で候補を並べ替える。前後 2 行（T-05 では ±15 行）のスニペットを付ける。
4. 段 3・4 の結果を基にした編集は第 10.5 節のインターロックに従う。
5. 降格はファイル単位で判定する。1 回の応答の中で、ファイルごとに異なる段を使ってよい（`mixed`）。

警告文の雛形（日本語・`accuracy_warning`）:

| 水準 | 雛形 |
|---|---|
| `structural` | 「LSP が利用できないため構文木のみで解決しました。型・オーバーロード・再エクスポート越しの参照は区別されていません。」 |
| `heuristic` | 「言語の文法が無いため ctags とブロック推定で境界を求めました。終了行は推定であり、編集前に `get_symbol_bodies` で範囲を確かめてください。」 |
| `lexical_fallback` | 「未知の言語のため単語境界 grep に降格しました。同名の誤検知と動的参照の見落としが含まれます。」 |

`structural` の雛形の「LSP が利用できない」は、LSP が本当に使えないとき（未導入・`unavailable`・切断）だけに使う。LSP が動いたままその要求にエラーを返したときは「LSP（<サーバー名>）が <method> の要求にエラーを返した（<エラー文>）ため構文木で解決しました。」（`lsp_status` は `ready` のまま）、サーバーが壊れて起動し直しているときは「…以後答えられない状態になった（<エラー文>）ため、LSP を再起動しています。…再起動の完了後に呼び直すと `exact` になります。」（`lsp_status` は `indexing`）と、起きたことをそのまま書く（実装で確定 2026-10-31、BUG-0090。設計書 §7.8.4）。

## 8.5 `revision` 指定時の解析

- `revision` が `WORKING_TREE` 以外のとき、サーバーは Git オブジェクトから当該断面のファイル内容を読み、**作業ツリーに触れずに** メモリ上の仮想ツリーを作って解析する（MUST）。`STAGED` はインデックス（ステージ）の内容を用いる。
- 仮想ツリーでは LSP を使わない。到達できる最高水準は `structural` とし、`_resolution_meta.accuracy_warning` にその旨を書く。`engine_used` は `tree-sitter@<短縮SHA>`（短縮 SHA は 7 桁。`STAGED` は `tree-sitter@staged`）。
- **M1 の実装（実装で確定 2026-10-29、de291be、BUG-0018）**: 入口は `tools/_rev.py`。`open_revision` が `revision` を解決し（作業ツリーのときは Git を一切開かない。Git 層の import は約 130 ms かかるので、作業ツリー以外のときだけ遅延 import する）、`read_checked` が断面から 1 ファイルを読む（封じ込め → 読み込み → 大きさ・秘密・バイナリの検査。作業ツリーでも過去断面でも同じ規約）。過去断面の読み取りは Git オブジェクトとインデックスを読むだけで、作業ツリーにも `.git` にも書かない。ヘッダの `rev=` は利用者が渡した文字列そのまま、`warnings[]` に「`revision` を Git オブジェクトから読み、構文木だけで解析した（LSP なし）」を入れる。Git のリポジトリでないワークスペースで `WORKING_TREE` 以外を渡すと `E_REVISION_NOT_FOUND`（`details.reason: "no_git_repository"`）。
- **過去断面を M1 で扱わないツール**（下表の ×）は、まず `revision` を解決し、存在しない参照・不正な構文は `E_REVISION_NOT_FOUND`、解決できる参照は `E_UNSUPPORTED`（`details.reason: "revision_not_supported_until_M2"`）を返す。
- 仮想ツリーの解析結果は PaaD の永続テーブルには書かず、要求の間だけの一時テーブルに置く。`query_project_db` の `revision` 対応は M2 とする（M1 では `E_UNSUPPORTED`）。
- マイルストーン別の対応:

| ツール | M1 | M2 以降 |
|---|---|---|
| T-02, T-05 | ○ | ○ |
| T-03, T-04, T-06, T-07, T-08（`structural`） | × `E_UNSUPPORTED` | ○ |
| T-17, T-18 | （M2 ツール） | ○（Git 層が供給） |
| T-20 | × | ○ |
| カテゴリ D | × `E_INVALID_ARGUMENT`（常に） | 同左 |

## 8.6 Python 周辺ブリッジ

Python を主力とする現場で文脈を食いつぶす周辺フォーマットを、Tier 2〜3 として同じ入口（骨格・本体・参照）で扱う。各ブリッジの到達水準とマイルストーンを定める。

| # | ブリッジ | 何をするか | 水準 | M |
|---|---|---|---|---|
| B1 | 型スタブ・Cython・ネイティブ拡張の連結 | `.py` から `.pyi` / `.pxd` / `.pyx`、PyO3 の `#[pyfunction]` / `#[pyclass]`、`cffi` の宣言へ URN を連結し、`symbols.declared_in` に由来（`source` / `stub` / `cython` / `pyo3` / `proto`）を記録する。`relations.relation_kind = 'BINDS_NATIVE'` | `exact`（LSP が解決する範囲）〜`structural` | B1a（`.pyi`）M1、B1b（Cython / PyO3）M2 |
| B2 | Jupyter Notebook の仮想 Python 化 | `outputs` と不要な `metadata` を除き、全コードセルの `source` を連結した仮想 Python ファイルとして骨格・本体・編集を提供する。行番号は仮想ファイル上の行。セル境界は `[Cell] cell:3 (code, exec_count: 12) @L40-58`。編集は元の JSON 構造（セル ID、Markdown セル、出力）を保って書き戻す | `structural`（LSP を仮想ファイルに当てれば `exact`、MAY） | M1（読み取り）、M2（編集） |
| B3 | DOT / Mermaid / PlantUML の双方向 | 入力: ノード・エッジ・サブグラフ・属性を `Node` / `Edge` シンボルとして索引し、状態遷移やクラス関係を契約抽出の対象にする。出力: `format: "dot" \| "mermaid"` | `structural` | 出力 M1、入力 M2 |
| B4 | XML / HTML / SVG / JSON / JSONL / YAML / TOML の骨格と部分取得 | 骨格: タグ・キーの階層、属性一覧、配列要素の推論型、要素数。深さ既定 4、同型の兄弟は 1 つに畳んで `×N`。本体: `xpath:` / `jsonpath:` / `jq:` / `toml:` / `yaml:` セレクタで部分取得。巨大ファイル（既定 5 MB 超）はストリーミングで骨格のみ | `structural` | 骨格・部分取得 M1、置換 M2 |
| B5 | SQL と ORM / Pydantic の整合 | `.sql` とコード内 SQL 文字列から参照テーブル・列を `Table` / `Column` シンボルと `READS_TABLE` / `WRITES_TABLE` 関係として索引し、Pydantic `BaseModel` / SQLAlchemy / TypedDict / DataFrame スキーマの列と照合して診断（`source: "lucianke-sql"`）を出す | `structural` | M2 |
| B6 | IDL と実装の連結 | FastAPI のルートデコレータと Pydantic モデルを `Endpoint` シンボルに、`.proto` の `service` / `message` を `Service` / `Message` に索引し、生成コード（`*_pb2.py`）と `EXPOSES_ENDPOINT` / `BINDS_NATIVE` で結ぶ | `structural` | M2 |
| B7 | Jinja2 / reStructuredText の参照解決 | テンプレートのプレースホルダ（`{{ context.user_id }}`）と Sphinx ロール（`:func:`, `:class:`）を `REFERENCES` / `RENDERS_TEMPLATE` 関係として索引し、レンダー元が渡すキーとの不一致を診断する | `structural` | M2 |

M1 の範囲（B1a、B2 読み取り、B3 出力、B4 骨格・部分取得）は推奨であり、師匠の判断で動かせる（§17 OD-8）。B1b の Cython 文法と B7 の Jinja2 文法は、ライセンスを確認できる文法が未選定である（§16.2.3、§17 OD-21）。B4 の `jq:` セレクタは M2 とし、M1 の必須は `jsonpath:` とする（§16.2.1）。

---

# 9. プロジェクト知識 DB（PaaD）スキーマ

## 9.1 原則

1. 構文木・LSP・Git・契約抽出器が得たファクトは、すべて **1 つの SQLite データベース**（WAL モード）に正規化して置く（MUST）。外部 DB サーバーは使わない（NFR-PORT-1）。
2. 公開するのは以下の 10 テーブルである。内部用のテーブル（ジャーナル、カーソル、索引キュー）は名前を `_` で始め、`query_project_db` からは見えない（§13.2）。
3. **すべての公開テーブルに `confidence_level` 列がある**（MUST）。値は §8.3 の 4 水準に `CHECK` で制約する。
4. 定型ツール（カテゴリ A〜F）は、このスキーマ上のパラメータ化クエリとエンジン呼び出しの組み合わせとして実装する。ツールが返す値と `query_project_db` が返す値は同じ行に由来する（整合性の受入基準: T-03 と `SELECT ... FROM symbols` の件数が一致する）。
5. 書き込みは Indexer のみが行い、ファイル単位のトランザクションで更新する。読み取り接続は常に一貫した断面を見る。

## 9.2 DDL

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE workspaces (
  workspace_id        TEXT PRIMARY KEY,
  root_path           TEXT NOT NULL UNIQUE,
  python_env          TEXT,                       -- インタープリタの絶対パス。無ければ NULL
  active_branch       TEXT,
  head_sha            TEXT,
  default_base_ref    TEXT NOT NULL DEFAULT 'HEAD',
  exclude_globs_json  TEXT NOT NULL DEFAULT '[]',
  lsp_status_json     TEXT NOT NULL DEFAULT '{}', -- {"python": {"status": "ready", "server": "pyright", "progress": null}, ...}
  indexed_at          TEXT,                       -- ISO 8601
  confidence_level    TEXT NOT NULL DEFAULT 'exact'
                      CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);

CREATE TABLE files (
  file_id              INTEGER PRIMARY KEY,
  workspace_id         TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
  path                 TEXT NOT NULL,              -- ルート相対・'/' 区切り
  language             TEXT NOT NULL,              -- 'python', 'typescript', ..., 'unknown'
  tier                 INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 4),
  line_count           INTEGER NOT NULL,
  byte_size            INTEGER NOT NULL,
  sha256               TEXT NOT NULL,
  is_test              INTEGER NOT NULL DEFAULT 0, -- パス規則（test/, tests/, *_test.*, *.spec.*, test_*.py）で判定
  pagerank_score       REAL,                       -- インポート・呼び出しグラフ上のスコア
  last_modified_commit TEXT,
  indexed_at           TEXT NOT NULL,
  confidence_level     TEXT NOT NULL
                       CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback')),
  UNIQUE (workspace_id, path)
);
CREATE INDEX idx_files_lang ON files(workspace_id, language);

CREATE TABLE symbols (
  urn              TEXT PRIMARY KEY,               -- 第 7 章
  file_id          INTEGER REFERENCES files(file_id) ON DELETE CASCADE,  -- 外部シンボルは NULL
  is_external      INTEGER NOT NULL DEFAULT 0,     -- ワークスペース外（標準ライブラリ・依存パッケージ）
  parent_urn       TEXT REFERENCES symbols(urn) ON DELETE CASCADE,
  name             TEXT NOT NULL,
  qualified_name   TEXT NOT NULL,                  -- 'PaymentProcessor.process_charge'
  kind             TEXT NOT NULL,                  -- §6.1 の kind 語彙
  modifiers_json   TEXT NOT NULL DEFAULT '[]',
  start_line       INTEGER NOT NULL,
  end_line         INTEGER NOT NULL,
  sel_line         INTEGER,                        -- 名前トークンの位置
  sel_start_col    INTEGER,
  sel_end_col      INTEGER,
  signature        TEXT,
  return_type      TEXT,
  doc_summary      TEXT,
  doc_full         TEXT,
  declared_in      TEXT NOT NULL DEFAULT 'source'
                   CHECK (declared_in IN ('source','stub','cython','pyo3','proto','generated')),
  complexity       INTEGER,                        -- 循環的複雑度（MAY）
  scip_symbol      TEXT,                           -- SCIP 標準シンボル文字列（あれば）
  body_sha256      TEXT,                           -- 本体テキストのハッシュ（意味的差分の判定に使う）
  confidence_level TEXT NOT NULL
                   CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_symbols_file   ON symbols(file_id, start_line);
CREATE INDEX idx_symbols_qname  ON symbols(qualified_name);
CREATE INDEX idx_symbols_name   ON symbols(name);
CREATE INDEX idx_symbols_kind   ON symbols(kind);
CREATE VIRTUAL TABLE symbols_fts USING fts5(
  name, qualified_name, signature, doc_summary,
  content = 'symbols', content_rowid = 'rowid'
);

CREATE TABLE relations (
  relation_id      INTEGER PRIMARY KEY,
  source_urn       TEXT NOT NULL,                  -- symbols.urn（外部は is_external 行）
  target_urn       TEXT NOT NULL,
  relation_kind    TEXT NOT NULL CHECK (relation_kind IN (
                     'CALLS','IMPLEMENTS','INHERITS','REFERENCES','IMPORTS','TYPE_USES',
                     'READS_TABLE','WRITES_TABLE','RENDERS_TEMPLATE','EXPOSES_ENDPOINT','BINDS_NATIVE')),
  source_file_id   INTEGER REFERENCES files(file_id) ON DELETE CASCADE,
  target_file_id   INTEGER REFERENCES files(file_id) ON DELETE SET NULL,
  call_site_line   INTEGER,
  call_site_col    INTEGER,
  use_kind         TEXT CHECK (use_kind IN ('r','w','rw','call','type','import','impl')),
  guard_condition  TEXT,                           -- 呼び出し箇所を囲む条件式の要約
  confidence_level TEXT NOT NULL
                   CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_rel_target ON relations(target_urn, relation_kind);
CREATE INDEX idx_rel_source ON relations(source_urn, relation_kind);
CREATE INDEX idx_rel_sfile  ON relations(source_file_id);

CREATE TABLE dataflows (
  flow_id          INTEGER PRIMARY KEY,
  source_urn       TEXT NOT NULL,
  source_var       TEXT,
  sink_urn         TEXT NOT NULL,
  sink_var         TEXT,
  hop_count        INTEGER NOT NULL,
  passes_sanitizer INTEGER,                        -- 1/0。判定できなければ NULL
  path_json        TEXT NOT NULL,                  -- [{"urn":..., "line":..., "var":..., "guard":...}, ...]
  computed_at      TEXT NOT NULL,
  confidence_level TEXT NOT NULL
                   CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_flows_src  ON dataflows(source_urn);
CREATE INDEX idx_flows_sink ON dataflows(sink_urn);

CREATE TABLE diagnostics (
  diag_id            INTEGER PRIMARY KEY,
  file_id            INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  enclosing_urn      TEXT,                         -- 指摘位置を含む最内のシンボル
  severity           TEXT NOT NULL CHECK (severity IN ('error','warning','information','hint')),
  source             TEXT NOT NULL,                -- 'pyright', 'ruff', 'tsc', 'tree-sitter', 'lucianke-sql', ...
  code               TEXT,
  start_line         INTEGER NOT NULL,
  start_col          INTEGER NOT NULL,
  end_line           INTEGER,
  end_col            INTEGER,
  message            TEXT NOT NULL,
  quickfix_action_id TEXT,                         -- セッション内で有効な不透明 ID
  observed_at        TEXT NOT NULL,
  confidence_level   TEXT NOT NULL
                     CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_diag_file ON diagnostics(file_id, severity);
CREATE INDEX idx_diag_urn  ON diagnostics(enclosing_urn);

CREATE TABLE contracts (
  contract_id        TEXT PRIMARY KEY,             -- SHA-256(target_urn + contract_type + source_kind + 正規化 expression) の先頭 16 桁
  target_urn         TEXT NOT NULL,
  contract_type      TEXT NOT NULL CHECK (contract_type IN ('PRE','POST','INVARIANT','THROWS')),
  source_kind        TEXT NOT NULL CHECK (source_kind IN ('TYPE','GUARD','DOCSTRING','OPENAPI','GHERKIN')),
  expression         TEXT NOT NULL,                -- 人間可読（§11 T-16 の制約式文法）
  parsed_from_text   INTEGER NOT NULL DEFAULT 0,   -- 自然文の定型パターンから式を得たとき 1
  smt_lib            TEXT,                         -- SMT-LIB v2。変換不能なら NULL
  smt_reason         TEXT,                         -- smt_lib が NULL の理由（unparsed_text, unsupported_type, nonlinear, include_smt_false）
  origin_path        TEXT,
  origin_line        INTEGER,
  last_verify_status TEXT CHECK (last_verify_status IN
                       ('VERIFIED','VIOLATED','BOUNDED_VERIFIED','UNKNOWN_OR_TIMEOUT')),
  last_verified_at   TEXT,
  confidence_level   TEXT NOT NULL
                     CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_contracts_target ON contracts(target_urn);

CREATE TABLE git_commits (
  commit_sha        TEXT PRIMARY KEY,
  workspace_id      TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
  author            TEXT,
  author_email      TEXT,
  committed_at      TEXT NOT NULL,                 -- ISO 8601
  message_summary   TEXT NOT NULL,                 -- 1 行目
  is_merge          INTEGER NOT NULL DEFAULT 0,
  parent_shas_json  TEXT NOT NULL,
  confidence_level  TEXT NOT NULL DEFAULT 'exact'
                    CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_commits_time ON git_commits(workspace_id, committed_at DESC);

CREATE TABLE git_symbol_changes (
  commit_sha       TEXT NOT NULL REFERENCES git_commits(commit_sha) ON DELETE CASCADE,
  urn              TEXT NOT NULL,
  path             TEXT NOT NULL,
  file_id          INTEGER REFERENCES files(file_id) ON DELETE SET NULL,  -- 現存しなければ NULL
  change_type      TEXT NOT NULL CHECK (change_type IN ('ADDED','MODIFIED_BODY','MODIFIED_SIG','DELETED','RENAMED')),
  previous_urn     TEXT,                           -- RENAMED のとき旧 URN
  lines_added      INTEGER NOT NULL DEFAULT 0,
  lines_deleted    INTEGER NOT NULL DEFAULT 0,
  confidence_level TEXT NOT NULL
                   CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback')),
  PRIMARY KEY (commit_sha, urn)
);
CREATE INDEX idx_gsc_urn ON git_symbol_changes(urn, commit_sha);

CREATE TABLE git_co_changes (
  entity_a            TEXT NOT NULL,               -- URN（ファイルはセレクタ無しの URN）
  entity_b            TEXT NOT NULL,
  entity_kind         TEXT NOT NULL CHECK (entity_kind IN ('file','symbol')),
  co_change_count     INTEGER NOT NULL,
  a_change_count      INTEGER NOT NULL,
  coupling_ratio      REAL NOT NULL,               -- co_change_count / a_change_count（a が変わったとき b も変わる割合）
  last_co_changed_sha TEXT,
  window_commits      INTEGER NOT NULL,            -- 集計に用いた直近コミット数
  confidence_level    TEXT NOT NULL
                      CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback')),
  PRIMARY KEY (entity_a, entity_b)
);
CREATE INDEX idx_cochange_ratio ON git_co_changes(entity_a, coupling_ratio DESC);
```

## 9.3 列と語彙の補足

| 項目 | 規約 |
|---|---|
| 外部シンボル | ワークスペース外の対象（標準ライブラリ・依存パッケージ）は `is_external = 1`、`file_id = NULL`、URN は `lk://_ext/<パッケージ名>[@<版>]#<qualified>`。`relations.target_urn` から参照されるときに最小の行を作る |
| `files.is_test` | 既定のパス規則で判定し、`configure_workspace` の `exclude_globs` とは独立。T-06 の `include_tests` と T-09 の `affected_test_targets` はこの列を使う |
| `symbols.kind` | §6.1 の `kind` 語彙と同一（ただし `File` は応答上の要素で、`symbols` には格納しない）。追加は本書の改版で行う。TS の型リテラルのメンバー（型の別名・メンバーの型注釈に書いたもの。§7.2）はプロパティが `Property`、メソッドのシグネチャが `Method`、修飾子は `pub`（BUG-0114）。関数型の引数・配列の要素・ほかの総称の型引数・関数の引数と戻り値の型リテラルは持ち主のメンバーではないのでシンボルにしない。`const x = { … } as const` のオブジェクトリテラルのメンバーもシンボルにしない |
| `relations.guard_condition` | 呼び出し箇所を直接囲む `if` / `match` / `try` / 三項式の条件を 1 行に正規化した文字列。無ければ NULL |
| `git_co_changes` の方向 | `(a, b)` と `(b, a)` は別の行。`coupling_ratio` は `a` を変えたコミットのうち `b` も変えた割合 |
| `git_co_changes.window_commits` | 既定 500。`configure_workspace` で変更できる（MAY） |
| 時刻 | すべて ISO 8601、UTC または明示オフセット |

## 9.4 更新規約

| 契機 | 更新するテーブル | 単位 |
|---|---|---|
| 起動時の差分索引 | `files`, `symbols`, `relations`（`IMPORTS` と構文木から取れる関係）、`files.pagerank_score`（走査の最後に 1 回計算） | ファイル |
| LSP の `publishDiagnostics` | `diagnostics`（当該ファイルの行を全置換） | ファイル |
| LSP による参照・呼び出し階層の取得 | `relations`（取得した範囲を `exact` で上書き。`structural` 行は同一 `(source, target, kind)` で置換） | 要求に応じて遅延 |
| ファイル監視イベント | `files`, `symbols`, `relations`, `diagnostics`（構文エラー分）。**ファイルが作られた・消えた**とき（および `symbols` の URN の集合が変わったとき）は、それを `import` している側のファイルの `relations` も同じトランザクションで解き直す（内部表 `_import_probes` で引く。BUG-0025・BUG-0034）。`files.pagerank_score` は最後の更新の約 1 秒後に 1 回だけ計算し直す（`data_version` は進めない） | ファイル |
| 編集トランザクションの確定 | 同上。さらに `cursors` の失効 | ファイル |
| Git 取り込み（M2） | `git_commits`, `git_symbol_changes`, `git_co_changes` | コミット。`HEAD` の移動を監視して増分 |
| T-14 / T-15 / T-16 の実行 | `dataflows`, `contracts`（結果をキャッシュとして保存） | 要求 |
| `configure_workspace reindex` | 範囲に応じて全置換 | ワークスペース |

- ファイル単位の更新は 1 トランザクションで行い、読み取り側が半更新の状態を見ないようにする（MUST）。
- `symbols.urn` が変わらない更新（本体のみの変更）は行を `UPDATE` し、関連する `relations` を保つ。URN が消えた場合は `ON DELETE CASCADE` で従属行を落とす。

## 9.5 公開と保護

- `query_project_db` の接続は読み取り専用で開き、`PRAGMA query_only = ON` を設定する（MUST）。
- SQLite の authorizer で、上の 10 テーブルと `symbols_fts`（FTS5 の影テーブル `symbols_fts_config` / `_data` / `_docsize` / `_idx` を含む）、`json_each` / `json_tree`、および一時テーブル `temp_grep_matches` 以外への参照を拒否する（MUST）。`_` で始まる内部テーブルは見えない。列を読まない参照（`count(*)`、CTE の別名）は、DB に実在する非公開名でなければ通す（S-7 で確認）。
- `ATTACH`、`PRAGMA`（FTS5 が内部で読む `data_version` を除く）、`load_extension`、書き込み文はすべて `E_QUERY_REJECTED`（§13.2）。

---

# 10. 決定論的編集プロトコル

## 10.1 目的と適用範囲

行番号と文字列一致に頼る編集が失敗する原因（行ずれ、インデント不一致、同名別箇所の誤置換、外部変更との競合）を、プロトコルの手順で排除する。本章は **カテゴリ D の 4 ツール（T-10〜T-13）すべて** に適用される（MUST）。各ツールの固有事項は第 11 章に書き、手順の共通部分は本章だけが規範である。

前提:

- `revision` は `WORKING_TREE` のみ。他は `E_INVALID_ARGUMENT`。
- ワークスペース単位の書き込みロック（§5.9）の下で、1 度に 1 トランザクションだけが走る。

## 10.2 用語

| 用語 | 意味 |
|---|---|
| 編集トランザクション | 1 回のツール呼び出しが行う、解決から応答までの一連の処理。識別子 `edit_id`（`ed_` + 連番 4 桁以上） |
| 計画（plan） | 書き換えの集合。要素は hunk |
| hunk | `(path, start_line, start_col, end_line, end_col, new_text)`。位置は適用前の座標 |
| 前提（precondition） | 計画が有効であるために成り立っていなければならない、対象ファイルの内容ハッシュ |
| ジャーナル | 書き込み前のファイル内容の退避（§4.5） |
| 診断差分 | 編集前後の診断集合の差（§10.7） |

## 10.3 手順

編集トランザクションは次の 8 段を **この順で** 実行する（MUST）。途中で失敗したら、それ以降の段は実行せず、ファイルは一切変更されない。

| 段 | 名称 | 内容 | 失敗時 |
|---|---|---|---|
| 1 | 解決 | TargetRef を URN に解決する（§5.3）。対象ファイルごとに現在の `sha256` を読み取り `resolved_sha` として保持する。対象の精度水準を記録する | `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS` |
| 2 | 計画 | hunk の集合を作る。シンボル操作では範囲を §10.8 の境界規則で決める。`rename_symbol` と `apply_code_action` は LSP の `WorkspaceEdit` を hunk に変換する。`new_code` は §10.9 で正規化する | `E_LSP_UNAVAILABLE` / `E_UNSUPPORTED` |
| 3 | 前提検証 | ⑴ 呼び出し側が `expected_file_sha256(s)` を渡していれば、対象ファイルの現在の `sha256` と比較する。⑵ ファイル監視の未処理イベントを処理し、`resolved_sha` と現在の `sha256` を再比較する。⑶ 降格インターロック（§10.5）を判定する | `E_PRECONDITION_FAILED` / インターロック時は dry_run に切替 |
| 4 | 構文検証 | 計画をメモリ上で適用し、Tree-sitter で解析する。編集範囲（前後各 1 行を含む）に `ERROR` / `MISSING` ノードが新たに生じたら失敗。ファイルが編集前にエラーノードを持たなかった場合は、ファイル全体でエラーノード 0 を要求する。文法の無い言語（Tier 4）は検証を飛ばし `syntax_check: "skipped"` とする | `E_SYNTAX_INVALID`（`details.errors[]` に行とメッセージ） |
| 5 | dry_run 分岐 | `dry_run: true`（明示または強制）なら、統一 diff と `syntax_check`、可能なら予測診断を返して終了する。`applied: false` | — |
| 6 | 原子的書き込み | §10.6 | `E_INTERNAL`（ロールバック済み） |
| 7 | 再索引 | 構文木と PaaD の当該ファイル行を更新し、LSP に `didChange`（未オープンなら `didOpen`）を送る。影響するカーソルを失効させる | ログのみ（応答は続行） |
| 8 | 診断 | §10.7 の診断差分を集め、応答を組み立てる | `status: "pending"` で応答 |

## 10.4 陳腐化書き込みの防止（stale-write guard）

ファイル監視が外部の変更を拾うのは **非同期** であり、エージェントが別ツールで同じファイルを書き換えた直後に Lucianke の編集が到着することがある。解決時点（段 1）と書き込み時点（段 6）の間に内容が変わっていれば、計画の座標は無効である。これを二重に防ぐ。

1. **サーバー側の自動検査（MUST）**: 段 1 で取った `resolved_sha` を、段 3 で監視イベントを処理し切ったあとに再計算した `sha256` と比べ、さらに段 6 の直前（ロック保持下）にもう一度比べる。不一致なら `E_PRECONDITION_FAILED` とし、`details` に `path`, `expected`（`resolved_sha`）, `actual` を入れる。
2. **呼び出し側の楽観的前提（SHOULD）**: 読み取りツール（T-02, T-05 など）はファイルヘッダに `sha=` 接頭 12 桁を返す。エージェントはそれを `expected_file_sha256`（T-10, T-13）または `expected_file_sha256s`（T-11, T-12。`path → sha`）で渡せる。値は 64 桁の完全形、または **12 桁以上の接頭辞** を受け付ける。不一致なら `E_PRECONDITION_FAILED`。
3. 失敗時はファイルを変更しない。エージェントは対象を読み直して再実行する。

## 10.5 降格インターロック

対象ファイルの精度水準が `heuristic` または `lexical_fallback` のとき、境界と同名判定が推定であるため、誤った範囲を上書きする危険がある。

| 条件 | 挙動 |
|---|---|
| 降格ファイルへの編集で `allow_heuristic_mutation` が `false`（既定） | `dry_run` を強制する。応答は `applied: false`、統一 diff、`warnings[]` に「降格のため書き込みを抑止した。確認のうえ `allow_heuristic_mutation: true` で再実行せよ」。エラーにはしない |
| `allow_heuristic_mutation: true` かつ `dry_run: false` | 書き込む。`_resolution_meta.is_degraded = true` のまま、`accuracy_warning` に推定境界の注意を書く |
| `structural` 水準（LSP 不在）での `rename_symbol` | 同一ファイル内のスコープ解析によるリネームに限って行ってもよい（MAY）。複数ファイルに及ぶ場合は `E_LSP_UNAVAILABLE` |
| `structural` 水準での T-10 / T-12 | 通常どおり行う。構文木の境界は正確であるため |
| Tier 3 のセレクタ範囲への T-10 `replace_body`（M2） | 対象ノードのバイト範囲だけを置換し、文書の他の部分（キー順・インデント・コメント）のバイト列を変えない（MUST） |

`E_MUTATION_BLOCKED` は、`allow_heuristic_mutation: true` があっても書き込みを安全に確定できない場合にのみ返す（T-10: 推定した定義ノードの終了行の候補が 2 つ以上ある。T-11: 書き換え対象ファイルに `new_name` が単語として既に出現する）。許可フラグが無いだけの場合は、`dry_run: false` を明示していても上の第 1 行のとおり dry_run に切り替え、エラーにはしない。

## 10.6 原子的書き込みとジャーナル

1. 対象ファイルごとに、書き込み前の内容をジャーナル（`<cache_root>/<workspace_hash>/journal/<edit_id>/<path>`）に退避する（MUST）。
2. 新しい内容を **同じディレクトリの一時ファイル** に書き、`fsync` し、元のパスへアトミックに改名する（MUST）。複数ファイルのときは、全ファイルの一時ファイルを書き終えてから改名を始める。
3. 改名の途中で失敗したら、改名済みのファイルをジャーナルから書き戻す（ロールバック）。ロールバック後に `E_INTERNAL` を返し、`details.rolled_back: true` と `details.log_ref` を入れる。
4. 保持するもの: 改行コード（ファイル内の多数派。混在時は先頭行に従う）、末尾改行の有無、BOM、ファイルモード（実行ビット）。一時ファイルの改名でこれらが変わってはならない。ただし改名方式のため、NTFS の作成時刻・明示 ACE・代替データストリームは保持されない（mtime は書き込み時刻になる。S-10 で確認）。Windows で対象が読み取り専用属性を持つときは、属性を黙って外さず、何も書かずに `E_MUTATION_BLOCKED`（`details.reason: "read_only_file"`）を返す。
5. ジャーナルは直近 50 トランザクション分を保持し、古いものから削除する。取り消しツールの追加は未決事項（§17 OD-5）。
6. 事後条件: トランザクションの成否にかかわらず、「一部の hunk だけが適用されたファイル」が存在しない（NFR-SAFE-4）。

## 10.7 Edit-and-Verify（診断差分）

編集ツールの応答には、その編集で変わった診断を含める（FR-EDIT-5）。

| 項目 | 規約 |
|---|---|
| 対象ファイル | 編集したファイルと、その **直接の参照元**（`relations.source_file_id` が編集したシンボルを `target_urn` に持つファイル）。上限 20 ファイル。超える分は `checked_files` に含めず `warnings[]` に件数を書く |
| 比較の基準 | 段 1 の時点の `diagnostics` 行（編集前）と、段 8 で LSP から受けた最新の診断（編集後）。編集前の行は、**各 LSP サーバーがそのファイルのいまの内容の診断を出し終えてから** 読む（書き込みをする呼び出しだけ。待つ上限は `diagnostics_wait_ms`〔書き込み後の待ちとは別枠〕、出ていれば待たない。BUG-0098） |
| 同一性の判定 | `(file, source, code, message, enclosing_urn)` で突き合わせる。**行番号は使わない**（編集でずれるため） |
| `added[]` | 編集後にあって編集前に無い診断 |
| `resolved[]` | 編集前にあって編集後に無い診断 |
| 基準が不明なサーバー | 上限まで待っても編集前の診断を出さなかったサーバー（負荷・起動直後）は、編集前の診断が分からない。そのサーバーの編集後の診断は `added[]` に入れず **`present_after[]`** に入れ、`resolved[]` も判定しない（表に残っていた古い行を「解消」と言わない）。`checked_files[]` の要素に `unknown_baseline: [サーバー名]`（区別できないときは `"*"` = そのファイルの LSP すべて）を足し、`warnings[]` にサーバー名と次にすること（`present_after` の位置と diff の変更箇所を見比べる、`get_diagnostics` で確かめる）を書く。理由: 前からある診断を `added` と答えると、呼び手の AI は自分の編集が壊したと読んで無い誤りを追ったり正しい編集を戻したりする（BUG-0098） |
| 待ち時間 | `diagnostics_wait_ms`（既定 2000、最大 10000）。LSP の `publishDiagnostics` が編集後の版に追いつくまで待つ |
| `status` | 待ち時間内に全対象ファイルの診断が揃い、すべてのサーバーの基準が分かっていれば `"complete"`。揃わなければ `"pending"` とし、揃った分だけ返す。エージェントは `get_diagnostics` で後追いできる。揃っていても基準が不明なサーバーがあれば `"partial"`（`pending` が優先）。`dry_run` で診断予測を行わなかったときは `"not_computed"`。基準が分からないまま `"complete"` は返さない |
| LSP 不在 | Tree-sitter の構文エラー差分だけを返し、`_resolution_meta.confidence_level = "structural"` |

## 10.8 シンボル境界の規則

シンボル操作（T-10）の範囲は、構文木のノード境界に **言語ごとの付帯要素** を含めて決める。

| 要素 | 含める条件 |
|---|---|
| デコレータ / 属性（Python `@x`, Rust `#[x]`, TS `@x`, C# `[X]`） | 常に含める |
| 直前の Doc コメント（JSDoc, Rustdoc `///`, Python の直後 docstring） | 常に含める（Python の docstring は本体の一部）。**Doc コメントとシンボルの間に空行があっても含める**（実装で確定 2026-10-29） |
| 直前の通常コメント | シンボルとの間に空行が無く、**かつそのコメントの行に他のコードが無い**（コードの行末コメントではない）場合のみ含める（実装で確定 2026-10-29） |
| 末尾の同一行コメント | 含める |
| 空行 | 含めない（範囲の核は空行を含まない）。`delete_symbol` は行単位で取り除き、**直前の連続空行**を一緒に取り除く。直前に空行が無い（ファイルや本体の先頭のメンバー）ときだけ**直後**の連続空行を取り除く。これで隣り合う定義の間の空行は削除前と同じ数のまま残る（Python のトップレベルの 2 行、クラス内の 1 行。「直後の空行を 1 行に詰める」から改めた〔cc031cc。実装で確定 2026-10-29〕）。取り除く範囲がファイルの終端に届き、ファイルが最終改行で終わらないときは、手前の行の行末から取り除く（最終改行を勝手に足さない） |
| 可視性修飾子・`async`・`export` | 含める |

`insert_member` の挿入位置は、親（クラス・構造体・モジュール）の最後のメンバーの直後。親にメンバーが無ければ本体の先頭（Python の `pass` または `...` だけの本体は、その文を置換対象にする）。`insert_before` / `insert_after` は上の境界の外側に挿入し、前後に空行を置く。`blank_lines_around`（前後の空行数）は **Python のトップレベルの関数・クラスだけ 2**、それ以外（クラス内のメソッド、他の言語）は 1 とする（実装で確定 2026-10-29）。既存の空行を数えて、前後が合わせて `blank_lines_around` 行になるよう足す（隣に兄弟が居ないとき、つまり本体の先頭・末尾では足さない。96866a5。T-10 処理規約 4(e)）。

## 10.9 新規コードの正規化

`new_code` / `rewrite` の文字列には次の正規化を行い、diff で確認できるようにする。

1. **インデント**: `new_code` の先頭行の共通インデントを 0 に揃えたうえで、挿入先の深さ（親ノードの子の深さ）に合わせて付け直す。タブとスペースは対象ファイルの多数派に従う。幅はファイルから推定し、推定できなければ言語の慣例（Python 4、JS/TS 2、Go はタブ）。
2. **改行コード**: 対象ファイルに合わせる。
3. **末尾**: `new_code` の末尾の空行は 1 行に詰める。
4. **部分断片の検証**: 段 4 の構文検証は、断片単体ではなく **適用後のファイル** を解析して行う。`replace_body` で関数全体を渡さずに本体文だけを渡した場合は `E_SYNTAX_INVALID` とし、`details.hint` に「`operation: replace_body` はシグネチャを含む定義全体を渡す」と書く。

## 10.10 事後条件（受入基準）

| ID | 事後条件 |
|---|---|
| EP-1 | `applied: true` の応答の `files_changed[].sha256_after` は、応答直後にディスクから計算した `sha256` と一致する |
| EP-2 | `applied: false` の応答（dry_run、インターロック、いかなるエラー）のあと、対象ファイルの `sha256` は呼び出し前と一致する |
| EP-3 | `expected_file_sha256` に現在値と異なる値を渡すと `E_PRECONDITION_FAILED` が返り、ファイルは変わらない |
| EP-4 | 解決と書き込みの間に外部プロセスがファイルを書き換えた場合（テストでは監視イベントを遅延させて再現）、`E_PRECONDITION_FAILED` が返り、外部の変更が保たれる |
| EP-5 | `applied: true` のファイルは、Tree-sitter で解析してエラーノードが編集前以下である |
| EP-6 | 同じ内容での `replace_body`（恒等編集）はファイルをバイト単位で変えない（改行・BOM・末尾改行を含む） |
| EP-7 | `new_diagnostics.status` が `"complete"` のとき、`added[]` と `resolved[]` は `get_diagnostics` を直後に呼んだ結果と整合する（`added` はすべて現在の診断に含まれ、`resolved` はいずれも含まれない） |
| EP-8 | 降格ファイル（`heuristic` 以下）への編集は、`allow_heuristic_mutation: true` が無い限り `applied: false` である |
| EP-9 | 複数ファイル編集で改名段階に失敗を注入したとき、全ファイルが書き込み前の内容に戻っている |

## 10.11 エージェント向けの推奨シーケンス（説明）

1. `get_file_skeleton` で対象ファイルの骨格と `sha=` を得る。
2. `get_symbol_bodies` で変更するシンボルの本体を読む。
3. `edit_symbol_body` を `expected_file_sha256` つきで呼ぶ。大きな変更は先に `dry_run: true` で diff を見る。
4. 応答の `new_diagnostics.added[]` が空なら完了。空でなければ `apply_code_action`（`fix=` がある指摘）か再度の `edit_symbol_body` で直す。
5. `analyze_git_diff_symbols` で変更全体を見直し、`recommended_test_suite` を実行する（実行はエージェントのシェルツールで）。

---
# 11. ツール仕様

## 11.0 一覧と読み方

本章は 21 ツールの正本である。各ツールは第 5 章の共通パラメータとエンベロープ、第 6 章の出力形式、第 7 章の URN、第 8 章のフォールバック、そしてカテゴリ D は第 10 章の編集プロトコルを前提とし、ここでは固有事項だけを書く。

各ツールの記述は次の見出しで統一する: **目的 / マイルストーン / 対応要求 / 入力 / 処理規約 / 出力 / エラー / 精度と降格 / 事後条件**。事後条件は受入検査が機械的に判定できる文であり、第 14 章のフィクスチャ上で検査する。入力表の「必須」欄は、MUST（必須）/ MAY（任意）、または「はい / いいえ」で書く。

| # | ツール | カテゴリ | 経路 | M | `max_tokens` 既定 | `dot` / `mermaid` | 書き込み |
|---|---|---|---|---|---|---|---|
| T-00 | `configure_workspace` | 0 ワークスペース管理 | Fast | M1 | 1500 | | 設定のみ |
| T-01 | `get_repo_map` | A 探索・骨格把握 | Fast | M1 | 1500 | | |
| T-02 | `get_file_skeleton` | A | Fast | M1 | 2000 | | |
| T-03 | `search_workspace_symbols` | A | Fast | M1 | 1500 | | |
| T-04 | `resolve_symbols` | B 定義・参照・コード取得 | Fast | M1 | 2000 | | |
| T-05 | `get_symbol_bodies` | B | Fast | M1 | 3000 | | |
| T-06 | `find_references_and_implementations` | B | Fast | M1 | 2000 | ○ | |
| T-07 | `query_call_hierarchy` | B | Fast | M1 | 2000 | ○ | |
| T-08 | `get_diagnostics` | C 診断・影響分析 | Fast | M1 | 2000 | | |
| T-09 | `analyze_change_impact` | C | Verification | M2 | 2500 | ○ | |
| T-10 | `edit_symbol_body` | D 決定論的編集 | Fast | M1 | 1500 | | ○ |
| T-11 | `rename_symbol` | D | Fast | M1 | 1500 | | ○ |
| T-12 | `structural_search_replace` | D | Verification | M2 | 2000 | | ○（`dry_run` 既定 `true`） |
| T-13 | `apply_code_action` | D | Fast | M1 | 1500 | | ○ |
| T-14 | `find_code_paths` | E 仕様抽出・検証 | Verification | M2 | 2500 | ○ | |
| T-15 | `extract_code_contracts` | E | Verification | M3 | 2000 | | |
| T-16 | `verify_contract` | E | Verification | M3 | 2000 | | |
| T-17 | `get_symbol_history` | F Git 時系列 | Verification | M2 | 2000 | | |
| T-18 | `analyze_git_diff_symbols` | F | Verification | M2 | 3000 | ○ | |
| T-19 | `get_project_db_schema` | G DB 直接クエリ | Fast | M1 | 6000 | | |
| T-20 | `query_project_db` | G | Fast | M1 | 2000 | ○（`source_urn` / `target_urn` 列があるとき） | |

「経路」の Fast は NFR-PERF-1（p95 < 50 ms）、Verification は NFR-PERF-2（p95 < 500 ms）の対象を示す。「書き込み」が空のツールはファイルを変更しない（呼び出し前後でワークスペース内の全ファイルの `sha256` が不変であることを、第 14 章の全テストで常時検査する）。

## 11.1 カテゴリ 0: ワークスペース・対象環境管理

### T-00 `configure_workspace`

**目的**: 解析対象のプロジェクトルート、モノレポ内の対象、Python 実行環境、除外規則、Git の比較基準を確認・設定し、LSP ワーカーとキャッシュの稼働状態を返す。セッション開始時と対象切り替え時に呼ぶ。

**マイルストーン**: M1

**対応要求**: FR-WS-1, FR-WS-2, FR-WS-3, FR-FMT-3, NFR-OPS-3

**入力**（`max_tokens` 既定 1500。`revision` は無視し `warnings[]` に記録する）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `action` | enum | いいえ | `"status"` | `"status"` \| `"set_roots"` \| `"set_python_env"` \| `"set_excludes"` \| `"reindex"` |
| `root_paths` | string[] | `set_roots` で必須 | なし | 絶対パス、または MCP `roots/list` のルート相対パス。存在しないパスがあれば全体を `E_INVALID_ARGUMENT` とし、登録を一切変更しない（MUST） |
| `python_interpreter` | string | `set_python_env` で必須 | 自動検出 | Python 実行ファイルまたは仮想環境ディレクトリのパス。実行可能でなければ `E_INVALID_ARGUMENT` |
| `exclude_globs` | string[] | `set_excludes` で必須 | `[]` | `.gitignore` に加えて除外する glob。`set_excludes` は置換（追記ではない） |
| `default_base_ref` | string | いいえ | `"HEAD"` | `analyze_change_impact` と `analyze_git_diff_symbols` の既定比較元 |
| `reindex_scope` | enum | いいえ | `"all"` | `"all"` \| `"symbols"` \| `"git"` \| `"lsp"`。`reindex` でのみ有効 |
| `job_id` | string | いいえ | なし | `status` で特定ジョブの進捗だけを返す |

**処理規約**:

1. `status` は登録済みワークスペース、言語ティア別ファイル数、Python 環境、Git 状態、LSP デーモン状態、キャッシュ統計、実行中・完了済みジョブを返す。副作用を持たない（MUST）。
2. `set_roots` は各パスを正規化して §7.2 の `workspace-id` を割り当て、ルート直下から下方向に `pyproject.toml`, `uv.lock`, `poetry.lock`, `package.json`, `Cargo.toml`, `go.mod`, `.git` を走査してサブプロジェクト境界を記録する。最初に登録したルートをアクティブにする。登録後に初回インデックスを背景ジョブとして起動し、その `job_id` を返す。
3. Python 環境の自動検出順は `python_interpreter` 明示 > ワークスペースの仮想環境（`.venv/` > `venv/` > `env/` > `.env/`。`.venv` 以外は `pyvenv.cfg` のあるディレクトリだけ） > `VIRTUAL_ENV`（ワークスペースの根の中を指すときだけ） > `uv` の管理環境 > `poetry env` > `conda` 環境 > `PATH` 上の `python` とする（`venv/` は `python -m venv venv`〈Python 公式チュートリアルの既定〉の名前で、見落とすと型の LSP が依存の無い別の Python で解析する。BUG-0111）。検出結果は `status` の `python_env.manager` に記録する。
4. `set_python_env` は Python 系 LSP（型解決用と Lint 用）を新しい環境で再起動する。再起動は非同期で、応答には `job_id` を含める。
5. `set_excludes` は除外規則を置換し、`files` テーブルから一致ファイルを除いたうえでシンボル再走査ジョブを起動する。
6. `reindex` はジョブを投入して **100 ms 以内に** `job_id` を返す（MUST）。完了を待ってはならない。進捗は `status`（`job_id` 指定可）の `jobs[]` で `queued` \| `running` \| `done` \| `failed` と 0〜100 の `progress` を返す。
7. 他ツールが `workspace_id` 省略で呼ばれたときの解決は、①引数のパスを含む登録済みルート、②アクティブなワークスペース、③引数のパスから上方向に最も近い手順 2 のマーカーを持つディレクトリ（自動登録し `warnings[]` に記録）、の順とする。いずれも無ければ `E_WORKSPACE_NOT_CONFIGURED`。
8. 本ツールの出力は 1 ワークスペースを 1 レコードとしてページネーションする。

**出力**（compact）:

```
# configure_workspace ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~260/1500tok
- workspaces: 1 (active: app)
| id | root | langs | python | branch | dirty |
| app | S:/work/app | python(T1):42 toml(T3):2 ipynb(T2):3 | .venv/Scripts/python.exe 3.12 (manager: uv) | main | 3 |
- lsp: pyright=ready ruff=ready vtsls=standby
- cache: files=47 symbols=14230 relations=48910 synced=2026-10-29T11:20:03+09:00
- jobs: (none)
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `active_workspace_id` | string | アクティブなワークスペース |
| `workspaces[]` | object[] | `workspace_id`, `root_path`, `languages[] {language, tier, file_count}`, `python_env {interpreter, version, manager}`, `git {branch, head_sha, dirty_files}`, `exclude_globs[]`, `default_base_ref`, `sub_projects[] {path, marker}` |
| `lsp_daemons[]` | object[] | `language`, `server`, `status`（§5.5 の `lsp_status`）, `progress`, `pid`, `started_at` |
| `cache` | object | `files`, `symbols`, `relations`, `last_sync_at` |
| `jobs[]` | object[] | `job_id`, `kind`, `state`, `progress`, `phase`, `started_at`, `finished_at`, `error`。`phase` は走っている索引のジョブの、全ファイルを取り込んだ後の段（`settling relations 12/170`・`re-resolving importers`・`ranking symbols`。それ以外は `null`。compact の `jobs:` 行は `job_0001 reindex running 97% (settling relations 12/170)`。BUG-0112） |

**エラー**:

- `E_INVALID_ARGUMENT`: `action` に必須のパラメータが無い、`root_paths` に存在しないパスを含む、`python_interpreter` が実行可能でない。
- `E_WORKSPACE_NOT_CONFIGURED`: `status` に未登録の `workspace_id` または未知の `job_id` を渡した。
- `E_INTERNAL`: ジョブの投入に失敗した。

**精度と降格**: 本ツールはサーバー自身の状態を返すため `confidence_level` は常に `exact`。LSP が起動できない言語は `lsp_daemons[].status = "unavailable"` と `reason` で示し、エラーにはしない。

**事後条件**:

- `set_roots` 成功後の `status` で、登録した各ルートが `workspaces[]` に現れ、その `workspace_id` は §7.2 の文字集合 `[A-Za-z0-9._-]+` に一致する。
- `reindex` の応答は `timing_ms ≤ 100` で `job_id` を含み、直後の `status` の `jobs[]` に同じ `job_id` が `queued` または `running` で現れる。
- `set_python_env` 成功後の `status` で `python_env.interpreter` が指定値の正規化形と一致し、Python 系 LSP の `started_at` が呼び出し時刻より後である。
- 存在しないパスを含む `set_roots` は `E_INVALID_ARGUMENT` を返し、前後の `status` の `workspaces[]` が同一である。

---

## 11.2 カテゴリ A: 探索・骨格把握

### T-01 `get_repo_map`

**目的**: リポジトリのディレクトリ構造と、参照グラフ上の重要度が高い公開シンボルを、指定トークン予算に収めた俯瞰マップとして返す。未知のリポジトリに入る最初の 1 手。

**マイルストーン**: M1

**対応要求**: FR-EXP-1, FR-FMT-1, FR-FMT-2, FR-FMT-3, FR-LANG-1, NFR-PERF-1

**入力**（`max_tokens` 既定 1500）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `scope_paths` | string[] | いいえ | ワークスペース全体 | ディレクトリまたは glob。ルート外は `E_INVALID_ARGUMENT` |
| `include_signatures` | boolean | いいえ | `true` | シンボル名に加えて引数・戻り値を出す |
| `max_symbols_per_file` | integer | いいえ | `5` | 1〜50 |
| `ranking` | enum | いいえ | `"pagerank"` | `"pagerank"` \| `"fan_in"` \| `"alpha"` |
| `include_tests` | boolean | いいえ | `false` | テストファイル（`tests/`, `test_*.py`, `*_test.go`, `*.spec.*`, `*.test.*`）を含める |

**処理規約**:

1. `files` テーブルから `scope_paths` と除外規則で対象を絞る。
2. `pagerank` は `relations` の `CALLS` / `REFERENCES` / `IMPORTS` / `INHERITS` / `IMPLEMENTS` エッジ上でシンボル単位に計算し、ファイルのスコアは所属する**公開シンボル**上位 `max_symbols_per_file` 件の和とする。`fan_in` は同じ件数の `fan_in`（別ファイルからの辺の本数）の和、`alpha` はパス順。同点はパスの辞書順。`relations` が空（初回インデックス前）または**ワークスペース内のシンボルを指す辺が 1 本も無い**（実装では `_symbol_rank` が空）なら `alpha` に自動降格し `warnings[]` に `ranking=alpha (graph not ready)` を記録する（実装で確定 2026-10-29、458a4cb）。
   - **グラフの定義**（`index/rank.py`、809eff4）: 辺はワークスペース内のシンボル（`is_external = 0`）を指すものだけ（`lk://_ext/` の外部シンボルは辺にしない。`Exception` や `Decimal` が上位を占めないため）。自己ループは捨て、`(始点, 終点)` の組は種別・参照箇所の数に関わらず 1 本。テストファイルからの辺も数える（索引上の重要度は `include_tests` に依らず、`include_tests: false` はテストファイルを表示と順位から外すだけ）。減衰係数 0.85、同じ入力なら同じ値（決定的）。
   - **公開シンボル**（21a の判断。実装で確定 2026-10-29）: 修飾子に `priv` / `prot` を持たず、種別が `Field` / `Property` / `Column` / `Cell` / `Key` / `Tag` でないもの。修飾子が空（ctags 由来など可視性の分からないもの）は公開として扱い、`conf=heuristic` を付ける。データ形式の最上位の節 `Key` / `Tag` と、クラスやテーブルの中身は、親が代表になるので地図に出さない。
   - 表示する `rank` は先頭のファイルを 1.00 とした相対値（`alpha` は 0.00）。
3. 詰める順序は、①概観（ディレクトリ要約行・パッケージ・入口のモジュール・サブパッケージ）、②スコア降順のファイルブロック、③各ブロック内は公開シンボルをスコア降順に `max_symbols_per_file` 件まで、とする。
   - **ディレクトリ要約行**（実装で確定 2026-10-29、458a4cb・3e8d099）は、トップレベルのディレクトリ別のファイル数を多い順（同数はパスの辞書順）に 12 件まで出し、超えた分は `+N more dirs` とする（例: `- src/ (42 files), tests/ (18 files), +3 more dirs`）。ルート直下のファイルはルート（`./`）に数える。
   - **概観**（MUST。BUG-0120、2026-10-31）: ディレクトリ要約行の後ろに、初めてのリポジトリで最初に知りたい「どんなパッケージがあり、入口はどこか」を数行で出す。理由: 被参照の多いファイルの骨格だけでは全体の形が分からない（能力ベンチの T01 で `src/mnemo/api`・`src/mnemo/tools` など主要なパッケージの再現率が 0.556 だった）。行は次の順。
     - `- packages: <根> (N files), …`: **コードのパッケージの根**。トップレベルのディレクトリのうちコードのファイルを持つもの（`.` で始まる隠しディレクトリは除く）で、直下にコードのファイルが無くコードを持つ子が 1 つだけのディレクトリはその子とつなぐ（`src/` → `src/mnemo/`、`web/` → `web/src/`）。並びは重要度（配下のファイルのスコアの和）の降順、同じならファイル数の降順、パスの辞書順。10 件まで、残りは `+N more`。N は配下（再帰）のコードのファイル数。
     - `- entry modules: …`: **入口のモジュール**。`pyproject.toml` の `[project.scripts]`・`[project.gui-scripts]`・`[tool.poetry.scripts]` が指すモジュールのファイル（`pyproject.toml` のあるディレクトリから `mod/sub.py`・`mod/sub/__init__.py`・`src/` の下の同じ形）、次に名前（拡張子を除く）が `main`・`__main__`・`app`・`cli` か `*_entry` のコードのファイルを浅い順（同じ深さはスコアの降順）に、合わせて 8 件まで。
     - `- <根>: <サブパッケージ> (N), …`: **2 段目のサブパッケージ**。根の直下のコードを持つディレクトリ（同じくつなぐ）をフルパスで、根の順・同じ根ではファイル数の降順に（いちばん重要なパッケージの中の形を先に見せる。PageRank は API・ツールのような呼ばれる側でないパッケージを低く見るので 2 段目は大きさで並べる）。
     - コードのファイルは、Tier 3（データ・文書）と `dot`・`mermaid`・`openapi`・`binary`・`secret`・`unknown` を除き、Tier 1・2 で本文を解析しなかったもの（圧縮済みの JS など `lexical_fallback`）も除いたもの。`include_tests` と `scope_paths` の絞り込みの後のファイルで数える。
     - 大きさは 375 トークン（既定の予算 1500 の 1/4）までで、根 → 入口 → サブパッケージの順に入るだけ入れる。**`max_tokens` に依らない**: 予算に比例させると、予算を少し上げたときに概観が 1 項目ぶん伸びて後ろのファイルブロックが 1 つ落ちる帯ができ、§5.6 規則 5（BUG-0067）を破る。
   - 予算が小さく先頭のレコードが丸ごと入らないときは、概観の行を後ろ（サブパッケージ → 入口のモジュール）から外して先頭のファイルブロックを丸ごと残し、次にディレクトリ要約行とパッケージの行を残したままブロックを縮め（規約 4）、最後にその 2 行も外す。json の `tree[]` は `max_tokens ÷ 40` 件（最低 1 件、ファイル数の多い順）まで、`packages[]` / `entry_modules[]` は compact に直して `max_tokens ÷ 10`（375 まで）に収まる分までに絞る（予約分が予算を食い尽くさないため）。
4. 予算が尽きたら残りのファイルをディレクトリ別件数で `# summary:` 行に要約し、`next_cursor` を発行する。1 レコードは 1 ファイルブロック（途中で切らない。入らないブロックは縮退版にする: シグネチャを外す → 重要でない順にシンボルを減らす → ヘッダだけ）。
5. Tier 4 ファイルは ctags 推定のシンボルがあればそれを `conf=heuristic` 付きで載せ、無ければファイル名だけを載せる。

**出力**（compact）:

```
# get_repo_map ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~1480/1500tok
- src/ (42 files), tests/ (18 files), notebooks/ (3 files), conf/ (2 files)
- packages: src/ (40 files), notebooks/ (3 files)
- entry modules: src/main.py
- src/: src/services/ (12), src/api/ (9), src/models/ (8)
=== src/services/payment.py (245 lines) ===
- rank: 1.00 fan_in: 23
[Class] PaymentProcessor @L25-240
  [Method] pub async process_charge(self, req: PaymentRequest) -> Receipt @L40-115
=== src/api/routes.py (310 lines) ===
- rank: 0.92 fan_in: 17
[Function] pub create_order(req: OrderIn) -> OrderOut @L80-120
# summary: 38 more files (src: 30, notebooks: 3, conf: 2, tests: 3)
# more: cursor=c205817364 remaining≈38
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `ranking_used` | string | 実際に使った順位付け |
| `tree[]` | object[] | `dir`, `file_count`, `line_count` |
| `packages[]` | object[] | 概観のパッケージの根: `dir`, `file_count`, `line_count`, `children[]`（`dir`, `file_count`） |
| `entry_modules[]` | string[] | 概観の入口のモジュール（パス） |
| `files[]` | object[] | `path`, `rank`, `fan_in`, `line_count`, `language`, `tier`, `symbols[]`（SymbolRecord の部分集合: `urn`, `name`, `qualified_name`, `kind`, `modifiers`, `range`, `signature`, `confidence_level`） |

**エラー**:

- `E_INVALID_ARGUMENT`: `scope_paths` にルート外のパス、`max_symbols_per_file` が範囲外。
- `E_WORKSPACE_NOT_CONFIGURED`, `E_REVISION_NOT_FOUND`, `E_CURSOR_EXPIRED`: §5.8 のとおり。

**精度と降格**: `exact` と `structural` の差は無い（Tree-sitter の骨格と `relations` で足りる）。`heuristic` のファイルはシンボル名と推定行のみでシグネチャを持たない。`lexical_fallback` のファイルはファイル名と行数のみを載せる。応答全体の `confidence_level` は混在時の最低水準（§5.5）。

**事後条件**:

- `files[]` の `rank` は非増加順に並ぶ。
- 各ファイルブロックのシンボル数は `max_symbols_per_file` 以下である。
- `.gitignore` と `exclude_globs` に一致するパスが `files[]` に現れない。
- 切り詰めたとき `page.total_estimate` は範囲内の対象ファイル総数に等しい。
- `budget.used_tokens_est ≤ max_tokens`。

### T-02 `get_file_skeleton`

**目的**: 1〜20 ファイルについて、import、型・クラス・関数のシグネチャ、Doc コメント 1 行目だけを残し、本体を折りたたんだ骨格を返す。コード以外（Notebook、XML/JSON/YAML/TOML、DOT/Mermaid、未知言語）も同じ形で返す。

**マイルストーン**: M1

**対応要求**: FR-EXP-2, FR-LANG-1, FR-LANG-2, FR-LANG-3, FR-FMT-1, FR-FMT-2, FR-FMT-3, NFR-PERF-1

**入力**（`max_tokens` 既定 2000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `paths` | string[] | はい | なし | 1〜20 件。0 件または 21 件以上は `E_INVALID_ARGUMENT` |
| `include_private` | boolean | いいえ | `true` | 非公開メンバー（`_` 始まり、`private`、非 `export`）を含める |
| `include_doc_summary` | boolean | いいえ | `true` | Doc コメント 1 行目を `doc:` で付ける |
| `include_urns` | enum | いいえ | `"public"` | `"public"` \| `"all"` \| `"none"`。`urn:` 行を付けてよい範囲。付けるのはその範囲のうち、URN が行から導けないもの（§6.1 規約 2）だけ |
| `include_imports` | boolean | いいえ | `true` | `imports:` 行を付ける（モジュールごと。§6.1 規約 10） |
| `max_depth` | integer | いいえ | `4` | データファイル（Tier 3）の階層表示の深さ。1〜10 |

**処理規約**:

1. **バッチ項目の失敗**: 各パスは独立に処理し、結果は `data.items[]` に項目ごとの `ok` / `error` で返す。1 件以上成功すれば全体は `ok: true` とし、失敗項目を `warnings[]` に列挙する。全件失敗なら全体を最初の項目のエラーコードで失敗にする（MUST）。本規約は T-04, T-05 の `targets[]` にも適用する。
2. Tier 1〜2 のコード: Tree-sitter の具象構文木から、モジュール直下とクラス直下の宣言を再帰的に `symbol-block` にする。TS の型リテラルのメンバー（§7.2・§9.3）は型の別名・メンバーの子（入れ子の型リテラルはそのメンバーの子）。親子は行の範囲の包含で決め、同じ行の範囲は兄弟だが、修飾名が親の修飾名 + `.` で始まるものは子にする（1 行の `type U = { a: string }` の `U.a`。最上位に並べると処理規約 8 の縮退で落とせない）（実装で確定 2026-10-31、BUG-0114）。本体は `{ ...N lines omitted }` に畳む。デコレータ・属性は `decorators:`、docstring / Doc コメントの 1 行目は `doc:`、`raise` / `throws` 宣言が構文から取れれば `raises:` に出す。
3. `.ipynb`: JSON を読み、`outputs` と `metadata` を捨て、セルを `[Cell] <n> <code|markdown> (exec_count: <k>) @L<start>-<end>` として並べる。行番号は「全セルの `source` を順に連結した仮想 Python ファイル」上の行とし、以降の T-05 や編集ツールも同じ仮想行を使う。code セル内の宣言は入れ子の `symbol-block`、markdown セルは最初の見出し行だけを `doc:` に出す。
4. XML / HTML / SVG: タグ階層を `[Tag]` で、同名の兄弟は `×N` で集約し、属性名一覧を `type:` に出す。`max_depth` を超える階層は `{ ...N nodes omitted }` に畳む。
5. JSON / JSONL / YAML / TOML: キー階層を `[Key]` で出し、配列は要素数と推論した要素スキーマ（`array<object{id:int,user:object}>`）を `type:` に出す。JSONL は先頭 100 レコードからスキーマを推論し、その旨を `note` に書く。
6. DOT / Mermaid: グラフ種別、ノード数、エッジ数、サブグラフを `[Node]` `[Edge]` `[Section]` で要約する。
7. Tier 4（未知言語）: ctags 互換の規則で取れたエントリ（`syntax/heuristic.py` の `block_outline`）を `[Function]` / `[Class]` / `[Section]` などの入れ子にし、終了行はインデントまたは括弧の対応から推定する。取れれば `heuristic`、何も取れなければ見出しだけの空の骨格で `lexical_fallback` とし、どちらも先頭に `! DEGRADED` 行を置く（MUST。パイプラインがレコードの水準から付ける。§5.4.2）。T-02 の呼び出しでは `ctags` を起動しない（子プロセスと PATH の探索を持ち込まない。索引側の `Indexer` は使う）。`block_outline` の定義らしい行は、`def|function|fn|sub|proc|procedure|class|module` で始まる行、COBOL の段落名、字下げ 0 の `name = …` / `rule NAME = …` で、終了行は括弧の対応・字下げの戻り・`end` から推定する（実装で確定 2026-10-29、de291be）。
   - **Markdown・reStructuredText**（Tier 3）: 見出しの入れ子を `[Section]`（`structural`）にする。節の終わりは次の同位以上の見出しの前（末尾の空行は含めない）。Markdown は ATX（`#`）と Setext（`===` `---`）の見出しを拾い（フェンス付きコードブロックと先頭の YAML front matter の中は見出しにしない）、reStructuredText は下線のみ・上下線つきの見出しを出現順に深さへ割り当てる。
   - 種類の振り分けの順は、データ形式（JSON・JSONL・YAML・TOML・XML・`.ipynb`）→ コード（Python・TypeScript・JavaScript。構文木）→ Markdown・rst → それ以外（Tier 4）。
8. 予算に収まらないとき、ファイル内では `doc:` 行 → `urn:` 行（導けない URN の行。§6.1 規約 2）→ 非公開メンバー → 入れ子の公開メンバー（最上位は残す）の順に、**1 件ずつ** 落とし、収まる最も浅い段を返す（BUG-0078、2026-10-31）。`doc:` 行と `urn:` 行は深い順・同じ深さでは文書の後ろから、非公開メンバーは文書の後ろから、入れ子の公開メンバーは「浅い順 → 文書順」（クラスごとに上から）に残し、その逆順に落とす（親を落とすと子も落ちる）。段の鎖は予算に依らないので、予算を上げても見える行・シンボルは減らない（§5.6-5）。落としたものは `- truncated: N members omitted (<属性>): <入れ物> <件数>, …; whole file: max_tokens>=<M> with this path alone` に書く。`<属性>` は `doc`・`urn`（全部落とした）か `K doc lines`・`K urn lines`（一部）で、`urn:` 行がもともと無ければ `urn` は挙げない。`<入れ物>` は落としたメンバーの親の修飾名（最上位は `(top level)`）で、文書順に 8 件まで（残りは `(others)` にまとめる）。`<M>` はそのファイルだけを丸ごと返す `max_tokens` の目安（丸ごとの大きさ + 前置きの分 200 を 100 単位に切り上げ。形式ごとに数える）。落とした分はカーソルでは辿れない（カーソルは次のファイルへ進む）ので、呼び直しの手を添える。縮退したファイルブロックはそのページの最後の 1 件とし、残りのファイルは次ページに回す（§5.6-5。縮退版でも収まらなければそのファイルから次ページ）。1 レコードは 1 ファイルブロック。理由: 主な利用者の AI は渡した予算を使い切った応答で往復を減らしたい。以前は段が「`urn:` を落とす」の次が「入れ子を全部落とす」だけで、F-PY の `_urls.py` は json の予算 6,000 で最上位だけ（618 トークン、利用率 1 割）と続きのカーソルを返した（今は 53 シンボル中 48 を返し、利用率 99%）。
9. ファイルブロックのヘッダに `sha=` としてファイル内容の SHA-256 の先頭 12 桁を付ける（MUST）。エージェントはこれを編集ツールの `expected_file_sha256` に渡せる（§10.4）。json では `items[].file_sha256` に 64 桁の完全形を入れる。

**出力**（compact。Python の例は §6.1.1。ここでは他形式の形を示す）:

```
# get_file_skeleton ok conf=structural lsp=n/a rev=WORKING_TREE ws=app ~640/2000tok
=== notebooks/eda.ipynb (310 lines, 12 cells, skeleton: 9 lines, sha=4f1e9a0b7c2d) ===
[Cell] 1 markdown @L1-3
  doc: # 売上データの探索
[Cell] 3 code (exec_count: 12) @L10-48
  [Function] load_frame(path: str) -> pd.DataFrame @L12-30 { ...19 lines omitted }
=== reports/junit.xml (5120 lines, skeleton: 4 lines, sha=9b2c7d0e1f3a) ===
[Tag] testsuite @L2-5120
  type: attrs=[name,tests,failures,time]
  [Tag] testcase ×412 @L3-5118
    [Tag] failure ×3 @L61-4977
=== pyproject.toml (61 lines, skeleton: 5 lines, sha=0a1b2c3d4e5f) ===
[Key] project @L1-20
  [Key] dependencies ×14 @L8-20
    type: array<string>
[Key] tool.ruff @L30-45
=== docs/flow.mmd (88 lines, skeleton: 3 lines, sha=6c7d8e9f0a1b) ===
[Section] flowchart TD @L1-88
  [Node] ×23 @L2-80
  [Edge] ×41 @L3-86
```

```
# get_file_skeleton ok conf=heuristic lsp=n/a rev=WORKING_TREE ws=app ~180/2000tok
! DEGRADED: confidence=heuristic engine=ctags lang=unknown(.cob)
! Warning: Tree-sitter grammar unavailable. Symbol ranges are estimated from indentation.
=== legacy/calc.cob (980 lines, skeleton: 3 lines, sha=d4e5f6a7b8c9) ===
[Section] PROCEDURE DIVISION @L120-980
  [Function] COMPUTE-TAX @L140-210
  [Function] WRITE-REPORT @L212-300
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `items[]` | object[] | 1 ファイル 1 項目 |
| `items[].ok` / `items[].error` | boolean / object | 項目の成否と §5.8 のエラー |
| `items[].path`, `language`, `tier` | string, string, integer | |
| `items[].line_count`, `skeleton_line_count` | integer | 元の行数と骨格の行数（Notebook はセル数も `cell_count` に） |
| `items[].file_sha256` | string | ファイル内容の SHA-256（64 桁）。compact ではヘッダの `sha=` に先頭 12 桁 |
| `items[].imports[]` | string[] | 1 名前 1 要素（`"os"`、`"Callable from collections.abc"`）。compact の `imports:` 行はモジュールごとにまとめた形（§6.1 規約 10） |
| `items[].symbols[]` | object[] | SymbolRecord に `children[]`, `fold_lines`, `decorators[]`, `raises[]`, `attrs[]`（Tag）, `inferred_type`（Key）, `count`（×N） を加えたもの |
| `items[].truncated_members` | integer | 予算で落としたメンバー数 |
| `items[].truncated_by_container[]` | object[] | `{container, omitted}`。落としたメンバーの入れ物（親の修飾名、最上位は `"(top level)"`）ごとの件数（子孫も数える）。文書順に 8 件まで、残りは `"(others)"`。丸ごとなら `[]`（規約 8。BUG-0078） |
| `items[].truncated_note` | string \| null | compact の `- truncated:` 行と同じ文（`max_tokens>=` の目安は json の大きさで数え、json はどのシンボルも `urn` を持つので `urn` は挙げない）。丸ごとなら `null` |
| `items[].confidence_level` | string | 項目ごとの精度 |

**エラー**:

- `E_INVALID_ARGUMENT`: `paths` が 0 件または 21 件以上、ルート外のパス、`max_depth` が範囲外。
- `E_TARGET_NOT_FOUND`: 項目のファイルが存在しない（全件なら全体のエラー）。
- `E_UNSUPPORTED`: 項目のファイルが構文の壊れたデータファイル（JSON / YAML / TOML / XML。`details.reason: "invalid_data_file"`、`details.language_id`）、大きすぎる・バイナリ・秘密ファイル（`details.reason`）。項目単位。
- `E_WORKSPACE_NOT_CONFIGURED`, `E_REVISION_NOT_FOUND`, `E_CURSOR_EXPIRED`: §5.8 のとおり。

**精度と降格**: `exact` は `structural` の出力に、注釈の無い戻り値を LSP の推論で `-> <inferred: int>` と補う点だけが異なる。`structural` は宣言どおりのシグネチャと正確な行範囲を返す。`heuristic` はシンボル名と推定行範囲のみでシグネチャ・入れ子・`doc:` を返さない。`lexical_fallback` は定義パターンに一致した行だけを平坦に並べ、終了行を返さない。

**事後条件**:

- Tier 1〜2 の各 `symbols[].range` は、同ファイルを Tree-sitter で解析した対応ノードの開始行・終了行と一致する。
- 骨格に含まれる各テキスト行（シグネチャ行、import 行、`doc:` の文）は、元ファイルの対応行の内容（前後の空白を除く）と一致する。
- `.ipynb` の応答文字列に、その Notebook の `outputs` 配列に含まれる文字列が 1 つも現れない。
- `paths` が 21 件のとき応答は `E_INVALID_ARGUMENT` で、`items` を持たない。
- `budget.used_tokens_est ≤ max_tokens`。

### T-03 `search_workspace_symbols`

**目的**: 名前（曖昧一致・前方一致・完全一致・正規表現）、種別、型シグネチャでワークスペース横断にシンボルを探し、URN 付きで返す。名前が分からない段階で `resolve_symbols` の前に呼ぶ。

**マイルストーン**: M1

**対応要求**: FR-EXP-3, FR-LANG-2, FR-FMT-1, FR-FMT-2, FR-FMT-3, NFR-PERF-1

**入力**（`max_tokens` 既定 1500）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `query` | string | はい | なし | 空文字は `E_INVALID_ARGUMENT` |
| `match` | enum | いいえ | `"fuzzy"` | `"fuzzy"` \| `"prefix"` \| `"exact"` \| `"regex"` |
| `symbol_kinds` | string[] | いいえ | 全種別 | §6.1 の `kind` の小文字形 |
| `type_signature_filter` | string | いいえ | なし | `"A -> B"`（引数に A を含み戻り値に B を含む）または `"A"`（どちらかに含む）。部分文字列一致 |
| `scope_paths` | string[] | いいえ | 全体 | |
| `include_tests` | boolean | いいえ | `true` | |
| `limit` | integer | いいえ | `30` | 1〜200 |
| `fallback_grep` | boolean | いいえ | `true` | 0 件のとき内部 grep（単語境界）で Tier 4 ファイルを探す |

**処理規約**:

1. `symbols` テーブルを引く。`fuzzy` と `prefix` は FTS5 相当の索引、`exact` は `name`（最後の部分）の等価比較だけで、`Class.method` のような限定名では当てず、`regex` は索引を使わない走査で 200 ms を上限とし、超えたら部分結果付きの `E_TIMEOUT`。
2. **当てる名前**: `fuzzy` と `prefix` は、query に `.` が無ければシンボル自身の名前（`name`）だけに当て、`qualified_name` では当てない（MUST）。query に `.` があるとき（`InstanceLock.acq` のように限定名で探している）だけ `qualified_name` に当てる。理由: `.` の無い query を限定名に当てると、クラス名で探したときにそのメンバー（`InstanceLock.acquire`・`InstanceLock.release` …）が名前の一致として並び、探している定義が埋もれる（呼ぶ側の AI のトークンも使う）。メンバーが欲しいときは `.` 付きで探せる（BUG-0121、2026-10-31。能力ベンチの T02 で適合率 0.5 だった）。`fuzzy` の FTS はこのとき `name` / `signature` / `doc_summary` の語を見る（シグネチャ・Doc だけの一致は下の順位の最後に残る）。
   順位は、完全一致 > 前方一致 > 名前の部分一致（上の「当てる名前」が query を含む、または query の各語がその名前の語の前方一致）> **シグネチャ・Doc の文字列だけの一致**（`fuzzy` の FTS が `signature` / `doc_summary` だけで当てたもの。`Packer` に対する `Pipeline.__init__(…, packer: Packer | None = None, …)` など。BUG-0069、2026-10-31）の順、同順位内は**コードのシンボル（`Key` / `Tag` 以外）をデータファイル（JSON / YAML / TOML / XML。Tier 3）の `Key` / `Tag` より先**、次に**そのシンボルのファイルの `pagerank`**（`files.pagerank_score`。`FileInfo.pagerank`。未計算・グラフ未準備は 0）降順、さらにパスの辞書順とする（73ee3fb・8139279。実装で確定 2026-10-29）。`symbol_kinds` を渡さない既定では全種別が混ざるので、設定ファイルのキーがコードの定義を押しのけないための規則。`Key` / `Tag` だけが欲しいときは `symbol_kinds` で指定する（既定の `symbol_kinds` は全種別のまま）。
3. `type_signature_filter` は `->` で分けて、左側を `signature` の引数部、右側を `return_type` に対して部分文字列一致で適用する。
4. 0 件かつ `fallback_grep: true` のとき、Tier 4 ファイルに対して `\bquery\b` と定義パターン（`query\s*[:=(]`）で内部 grep を実行し、`lex:` セレクタの URN で返す。このとき `is_degraded: true`、先頭に `! DEGRADED` 行（MUST）。
5. 1 レコードは 1 シンボル。`limit` 件を超える一致は `page.total_estimate` に総数を入れ、`next_cursor` を発行する。
6. compact のヒットの行は `path#qualified(sig)`。URN はそこから導けるので `urn:` 行は導けないとき（`lex:` の URN、オーバーロードの arity・sighash など）だけ付ける（§6.1 規約 2）。`sig` は 160 文字で切る（§6.1 規約 6。json の `signature` は全文）（BUG-0069、2026-10-31）。

**出力**（compact）:

```
# search_workspace_symbols ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~150/1500tok
query: "process_charge" match=fuzzy hits=3
[Method] src/services/payment.py#PaymentProcessor.process_charge(self, req: PaymentRequest) -> Receipt @L40-115
[Function] src/legacy/charge.py#process_charge_v1(order_id: str) -> dict @L10-55
[Function] tests/test_payment.py#test_process_charge_ok() @L40-60
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `query`, `match` | string | 受け取った条件のエコー |
| `hits_total` | integer | 一致総数（`limit` 前） |
| `symbols[]` | object[] | SymbolRecord + `score`（0〜1） |
| `fallback_used` | boolean | 内部 grep に降格したか |

**エラー**:

- `E_INVALID_ARGUMENT`: `query` が空、`regex` が不正、`limit` が範囲外、`symbol_kinds` に未知の値。
- `E_TIMEOUT`: `regex` 走査が 200 ms を超えた（`details.partial` に途中結果の件数）。
- `E_WORKSPACE_NOT_CONFIGURED`, `E_REVISION_NOT_FOUND`, `E_CURSOR_EXPIRED`: §5.8 のとおり。

**精度と降格**: `exact` と `structural` は同じ結果を返す（索引は Tree-sitter 由来）。`heuristic` のシンボル（ctags 由来）は `symbols` テーブルに入っているので通常の検索対象に含まれ、各行に `conf=heuristic` を付ける。`lexical_fallback` は手順 4 の降格時のみ現れ、`kind` は `Section`、`range` は一致行のみとなる。

**事後条件**:

- `match: "exact"` のとき返す全シンボルの `name` は `query` と等しい。
- `page.returned ≤ limit`。
- `symbol_kinds` 指定時、返す全シンボルの `kind` はその集合に含まれる。
- `fallback_used: true` の応答は `_resolution_meta.is_degraded = true` で、compact ではヘッダ直後に `! ` 行がある。
- `budget.used_tokens_est ≤ max_tokens`。

---

## 11.3 カテゴリ B: 定義・参照・コード取得

### T-04 `resolve_symbols`

**目的**: 最大 20 件の TargetRef について、定義位置、展開済みシグネチャ、Doc コメント全文、関連する型の定義 URN を一括で返す。

**マイルストーン**: M1

**対応要求**: FR-NAV-1, FR-WS-3, FR-LANG-2, FR-LANG-3, FR-FMT-2, FR-FMT-3, NFR-PERF-1, NFR-OPS-3

**入力**（`max_tokens` 既定 2000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `targets` | TargetRef[] | はい | なし | 1〜20 件 |
| `resolve_type_definitions` | boolean | いいえ | `true` | シグネチャ中のユーザー定義型を URN に解決する（1 段、1 対象あたり 10 件まで） |
| `include_doc_full` | boolean | いいえ | `true` | json に Doc 全文を入れる。compact では 1 行目と `doc_lines=N` のみ |
| `follow_aliases` | boolean | いいえ | `true` | 再エクスポート・import 別名をたどって元の定義を返す |

**処理規約**:

1. 各 TargetRef を §5.3 で解決する。項目の失敗は T-02 手順 1 のバッチ規約に従い、曖昧な項目は `candidates[]` 付きの `E_TARGET_AMBIGUOUS` を項目に入れ、他項目は処理を続ける。
2. `exact`: LSP の定義ジャンプと hover 相当の情報からジェネリクス展開後の型を取り、SCIP 索引があれば `scip_symbol` を添える。**M1 の実装**（実装で確定 2026-10-29、76a2aca・9259466）: ツールの `max_confidence` は `EXACT`（以前は `STRUCTURAL`）。解決したシンボル 1 件ごとに、(a) 内部表 `_hover_cache` に **(`urn`, 定義ファイルの `sha256`)** の行があれば LSP を呼ばず（起動も促さず）`exact` で答える（LSP が止まっていても、別のセッションでも、定義ファイルの内容が同じなら `exact`。設計書 R-11）。(b) 無ければ LSP が `ready` のときだけ、名前の位置（`selection.start`。文字境界）に `definition` と `hover` を送り、hover の型文字列を `signature_expanded` に入れて `_hover_cache` に書き戻す（`data_version` は進めない。同じ `urn` の古い `sha256` の行は消す）。`ready` でなければ**待たずに** `structural`（`standby` は起動を促すだけ）。(c) `definition` は確認に使う: 宣言の名前の位置から引いた定義が構文木の解決と違えば（別名・再エクスポート・オーバーロード）項目の `warnings` に LSP の答えを書き、解決先は付け替えない（`definition.range` と T-05 の `range_returned` の一致〔事後条件 1〕を保つため）。hover が空なら `structural`（`warnings` に「LSP が型情報を返さなかった」旨）。(d) タイムアウトと `E_LSP_UNAVAILABLE`（`position_clamped` を含む）は `structural` に降格する（`position_clamped` は位置が文字境界に当たらず要求を送らなかったとき。設計書 §7.8.4）。`scip_symbol` は未実装。
3. `structural`: Tree-sitter の宣言と import 文のモジュールパス→ファイル対応（ワークスペース内に限る）で定義を引く。型の展開は行わず、宣言どおりの文字列を返す。 **Python の絶対 import を探す根**（T-06・T-07 の `structural` の行も同じ。実装で確定 2026-10-31、BUG-0082）は、そのファイルについて実際に `sys.path` に載るものの順: (a) ファイルがテストのパス（`is_test`。`conftest.py` を含む）なら、pytest の既定の import（prepend）が足すディレクトリ（`__init__.py` を持たない最初の祖先〔`tests/unit/test_a.py` の `import helpers` は `tests/unit/helpers.py`。`tests/unit/__init__.py` があれば `tests` から `unit.helpers`〕、続いて祖先の `conftest.py` の同じディレクトリ。近いものから。`--import-mode=importlib` なら足さない）、 (b) pytest の設定（`pytest.toml`・`pytest.ini`・`pyproject.toml` の `[tool.pytest(.ini_options)]`・`tox.ini`・`setup.cfg`。pytest と同じ優先順で 1 つ）の `pythonpath`、 (c) ルートと `src/`。 テストフィクスチャ（`tests/fixtures/<名前>/`。T-06 処理規約 7 と同じルート）の中のファイルは、フィクスチャを入れ子のワークスペースとして先に (a)〜(c) で探し（フィクスチャの設定・`src/`）、無ければワークスペースの (b)(c)。 ファイルが在るときだけ結び、無ければ今までどおり外部（`lk://_ext/`）。(a)(b) の根では標準ライブラリと pytest 自身の名前を解かない（`tests/unit/json.py` があっても `import json` は外部。pytest がその根を足すのはテストを集める時点で、`json` など標準ライブラリの多くは既に `sys.modules` にあり、そちらが勝つ。決定的に保つため標準ライブラリの名前は一律に外部）。 標準ライブラリでない名前は (a) が site-packages より先なので手元のファイルに結ぶ。pytest の設定を変えたら、違う設定で解いたファイルだけを索引が解き直す（サーバーが止まっている間の編集は次の差分走査で）。 コードの中の `sys.path.insert(...)` は読まない。（理由: テストの土台〔`Env`・`Rig`〕やフィクスチャのクラスを変える AI に、それを import して使うテストが参照・呼び出し階層で 0 件に見えると影響を見落とす。pytest と Python が実際に使う根なので推定ではない。）
4. Python 周辺ブリッジ（FR-LANG-3）: `.py` に定義が無く `.pyi` スタブ、Cython の `.pxd`/`.pyx`、同一ワークスペースの PyO3 属性付き Rust 関数、`*_pb2.py` の生成元 `.proto` に対応する宣言があるときは、それを `definition` とし `declared_in` に種別（`stub` \| `cython` \| `pyo3` \| `proto`）を書く。
5. `heuristic` / `lexical_fallback`: ctags エントリまたは定義パターンの grep 一致行を `definition` とし、`signature` は一致行のテキストを返す。
6. 1 レコードは 1 対象（TS の型リテラルのメンバーも `path` + `symbol_name` の修飾名 `KyOptions.inline.before` で解決できる。BUG-0114）。compact の見出しは `path#qualified`（モジュールは `[Module] <path>`）で、URN はそこから導けるので `urn:` 行は導けないとき（arity・sighash 付きなど）だけ付ける（§6.1 規約 2。BUG-0069、2026-10-31）。

**出力**（compact）:

```
# resolve_symbols ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~300/2000tok
[Method] src/services/payment.py#PaymentProcessor.process_charge @L40-115
  sig: (self, req: PaymentRequest) -> Receipt
  type: PaymentRequest=lk://app/src/services/payment.py#PaymentRequest Receipt=lk://app/src/models/receipt.py#Receipt
  doc: 注文に対する決済を実行し領収書を発行する (doc_lines=6)
  decorators: @transactional
[Function] src/legacy/charge.py#process_charge_v1 @L10-55
  sig: (order_id: str) -> dict
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `items[]` | object[] | 1 対象 1 項目（`ok`, `error`, `request` のエコー） |
| `items[].symbol` | SymbolRecord | |
| `items[].definition` | object | `path`, `range`, `selection_range`, `declared_in`（`source` \| `stub` \| `cython` \| `pyo3` \| `proto`） |
| `items[].signature_expanded` | string | 型を展開したシグネチャ（`exact` のみ。他は `signature` と同じ） |
| `items[].doc_full` | string | Doc 全文 |
| `items[].decorators[]` | string[] | |
| `items[].type_definitions[]` | object[] | `name`, `urn`, `path`, `range` |
| `items[].confidence_level` | string | |

**エラー**:

- `E_INVALID_ARGUMENT`: `targets` が 0 件または 21 件以上、TargetRef が 3 形式のいずれでもない。
- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: 項目単位。全件失敗で全体のエラー。
- `E_WORKSPACE_NOT_CONFIGURED`, `E_REVISION_NOT_FOUND`: §5.8 のとおり。LSP が未起動・索引中のときはエラーにせず `structural` に降格する。

**精度と降格**: `exact` は展開済み型、別名追跡、`type_definitions` を返す。`structural` はワークスペース内の宣言と宣言どおりの型を返し、外部パッケージの定義は `E_TARGET_NOT_FOUND` ではなく `definition: null` と `warnings` で示す。`heuristic` は定義行と一致行テキストのみ。`lexical_fallback` は候補行を最大 3 件まで `candidates` に並べ、`definition` は最初の 1 件とする。

**事後条件**:

- 各成功項目の `definition.range` は、同じ URN を `get_symbol_bodies` に渡したときの `range_returned`（`context_lines: 0`）と一致する。
- `resolve_type_definitions: true` で返す各 `type_definitions[].urn` を `resolve_symbols` に渡すと `E_TARGET_NOT_FOUND` にならない。
- 曖昧な対象を含む呼び出しで、その項目は `E_TARGET_AMBIGUOUS` と 10 件以下の `candidates[]` を持ち、他の項目は `ok: true` である。
- `exact` 項目の `signature_expanded` は、同じ位置で LSP に hover を要求して得た型文字列と一致する。

### T-05 `get_symbol_bodies`

**目的**: ファイル全体を読まずに、指定したシンボルの実装本体だけを AST ノード境界で切り出して返す。データファイルはセレクタで部分抽出する。

**マイルストーン**: M1

**対応要求**: FR-NAV-2, FR-LANG-2, FR-LANG-3, FR-WS-3, FR-FMT-2, FR-FMT-3, NFR-PERF-1

**入力**（`max_tokens` 既定 3000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `targets` | TargetRef[] | はい | なし | 1〜20 件。`symbol_name` にセレクタ `xpath:` `jsonpath:` `jq:` `cell:` `toml:` `yaml:` を書ける |
| `context_lines` | integer | いいえ | `2` | 0〜20。本体の直前に付ける行数（デコレータ・コメント用） |
| `with_line_numbers` | boolean | いいえ | `true` | 各行に `@L<n>: ` を付ける |
| `include_referenced_symbols` | boolean | いいえ | `true` | 本体内で参照しているワークスペース内シンボルの URN を最大 20 件添える |
| `line_range` | object | いいえ | なし | `{start, end}`。巨大なシンボルの一部だけを取る（本体範囲内に丸める） |

**処理規約**:

1. 各 TargetRef を §5.3 で解決する（バッチ規約は T-02 手順 1）。
2. 返す範囲は、宣言ノードの範囲に、構文上そのノードに付属するデコレータ・属性・直前の Doc コメント（JSDoc、Rustdoc、`///`、`#[...]`）を加えたものとし、さらに `context_lines` 行を前に付ける。`context_lines` の行はデコレータ等と重ならない分だけ付ける。
3. セレクタ: `xpath:` は XML / HTML / SVG、`jsonpath:` と `jq:` は JSON / JSONL、`toml:` は TOML、`yaml:` は YAML、`cell:<n>` は `.ipynb` に適用する。ファイル種別に合わないセレクタは `E_UNSUPPORTED`。セレクタの構文誤りは `E_INVALID_ARGUMENT`。一致が複数のノードなら最初の 20 件を 1 項目内に並べ、総数を `match_count` に書く。
4. `cell:<n>` は `cells[n].source` の連結だけを返し、`outputs` を含めてはならない（MUST）。行番号は T-02 手順 3 の仮想行。
5. `heuristic`: ctags の開始行から、インデントまたは括弧の対応で推定した終了行までを返す。`lexical_fallback`: 定義パターンの一致行を中心に前後 15 行（最大 31 行）の窓を返し、一致が複数なら最大 3 窓を `windows[]` に並べる。先頭に `! DEGRADED` 行（MUST）。
6. `include_referenced_symbols` は、本体内の識別子のうち `symbols` テーブルに解決できたものを `- refs:` 行に列挙する（`exact` / `structural` のみ）。
7. 1 レコードは 1 対象。1 対象が予算に収まらないときは先頭から詰め、`truncated_at_line` を記録して `next_cursor` で同じ対象の続きを返す。**続き**（実装で確定 2026-10-29、04ed9b7、BUG-0016）: カーソルは「同じ対象の `truncated_at_line` の次の行から本体の終わりまで」を指し（前置き〔`context_lines`〕は続きには付けない）、次のページの `range_returned` は前のページの終わりの次の行から始まる。全ページを連結すると本体全体とバイト一致する。切った項目には `items[].continue_with.line_range = {start, end}` を付け、compact には注記 `truncated at line N of M: continue with the cursor, or line_range N-M` と `# more: cursor=…` を出す（`budget.truncated` は true、`remaining` はそのレコードの残り）。`line_range` を明示して続きを取ってもよい。前置き（`context_lines`）の途中では切らない（本体の先頭の行が入らないなら、そのレコードは次のページに回す）。本体が変わってカーソルの行が範囲外になったときは `E_CURSOR_EXPIRED`。切れたレコードの後ろの対象は、そのページには詰めない（順を保つ）。
8. 各項目に対象ファイルの SHA-256 を付ける（MUST）。json は `items[].file_sha256`（64 桁）、compact はシンボル行直下の `sha:` 属性行（先頭 12 桁）。エージェントはこれを編集ツールの `expected_file_sha256` に渡す（§10.4）。

**出力**（compact）:

````
# get_symbol_bodies ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~520/3000tok
[Method] src/services/payment.py#PaymentProcessor._validate_limit @L116-140
  sha: 3f9c1a2b4c5d
```python
@L116:     # 通貨ごとの上限を確認する
@L117:     def _validate_limit(self, amount: Decimal) -> bool:
@L118:         limit = self._limits.get(self.currency)
@L119:         if limit is None:
@L120:             raise LimitExceeded(self.currency)
```
- refs: lk://app/src/services/limits.py#LimitTable.get, lk://app/src/errors.py#LimitExceeded
````

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `items[]` | object[] | 1 対象 1 項目（`ok`, `error`, `request`） |
| `items[].symbol` | SymbolRecord | セレクタ指定時は `kind` を `Tag` / `Key` / `Cell` とする |
| `items[].range_returned` | object | `start_line`, `end_line`（`context_lines` を含む） |
| `items[].file_sha256` | string | 対象ファイルの SHA-256（64 桁） |
| `items[].context_before_lines` | integer | 実際に付けた前置き行数 |
| `items[].code` | string | 本体テキスト（行番号接頭辞を含まない） |
| `items[].truncated`, `truncated_at_line` | boolean, integer | |
| `items[].referenced_symbols[]` | object[] | `name`, `urn` |
| `items[].match_count` | integer | セレクタの一致総数 |
| `items[].windows[]` | object[] | `lexical_fallback` のみ: `start_line`, `end_line`, `code` |
| `items[].confidence_level` | string | |

**エラー**:

- `E_INVALID_ARGUMENT`: `targets` が 0 件または 21 件以上、`context_lines` が範囲外、セレクタの構文誤り、`line_range` の `start > end`。
- `E_UNSUPPORTED`: ファイル種別に合わないセレクタ（例: `.py` への `xpath:`）。
- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: 項目単位。
- `E_WORKSPACE_NOT_CONFIGURED`, `E_REVISION_NOT_FOUND`, `E_CURSOR_EXPIRED`: §5.8 のとおり。

**精度と降格**: `exact` と `structural` は同じ本体を返す（差は `refs:` の解決率のみ）。`heuristic` は終了行が推定で、`accuracy_warning` に「終了行はインデントから推定」と書く。`lexical_fallback` は本体ではなく窓であり、`symbol.kind` は `Section`、`range_returned` は窓の範囲となる。

**事後条件**:

- Tier 1〜2 の各項目で、`code` を改行で分割した各行は、対象ファイルの `range_returned.start_line` から `end_line` までの各行と順に一致する。
- `with_line_numbers: true` のとき、各行の `@L<n>` は `range_returned` 内の連番である。
- `cell:<n>` の項目の `code` は、Notebook JSON の `cells[n].source` を連結した文字列と一致し、`outputs` 配列内の文字列を含まない。
- `lexical_fallback` の各窓は `end_line - start_line + 1 ≤ 31` で、応答は `is_degraded: true`。
- `budget.used_tokens_est ≤ max_tokens`。

### T-06 `find_references_and_implementations`

**目的**: 1 つのシンボルの全参照箇所（使用種別付き）、インターフェース・抽象メソッド・トレイトの実装、型階層を決定論的に列挙する。

**マイルストーン**: M1

**対応要求**: FR-NAV-3, FR-LANG-2, FR-FMT-2, FR-FMT-3, FR-FMT-4, NFR-PERF-1, NFR-OPS-3

**入力**（`max_tokens` 既定 2000。`format` は `dot` / `mermaid` 可）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `target` | TargetRef | はい | なし | 曖昧なら `E_TARGET_AMBIGUOUS` |
| `query_type` | enum | いいえ | `"all"` | `"references"` \| `"implementations"` \| `"type_hierarchy"` \| `"all"` |
| `include_tests` | boolean | いいえ | `true` | |
| `include_declaration` | boolean | いいえ | `false` | 定義箇所自身を参照に含める |
| `context_snippet` | boolean | いいえ | `true` | 該当行のコードを添える（200 文字で省略） |
| `use_kinds` | string[] | いいえ | 全種別 | `r` `w` `rw` `call` `type` `import` `impl` で絞る |
| `scope_paths` | string[] | いいえ | 全体 | |
| `include_lexical_in_other_files` | boolean | いいえ | `false` | 対象が Tier 1〜2 でも、Tier 3〜4 のファイル（設定・テンプレート・未知言語）を単語境界 grep して `lex:` の参照を加える |

**処理規約**:

1. `target` を §5.3 で解決する。
2. `exact`: LSP の参照検索・実装検索・型階層で位置を集め、各位置の使用種別を AST から決める。呼び出し式の関数位置なら `call`、代入の左辺なら `w`、複合代入なら `rw`、型注釈・ジェネリクス引数・キャストなら `type`、import 文なら `import`、基底クラス・`impl` ブロック・`implements` 節なら `impl`、それ以外は `r`。**M1 の実装は参照検索だけ LSP を使い**、実装検索と型階層は `structural` の推定のまま返し、`warnings[]` にその旨を書く（実装で確定 2026-10-29、1d8700d）。LSP の結果は `relations` に `exact` で書き戻し（`data_version` は進めない）、同じ問い合わせの 2 回目以降は、書き戻した行がまだ新しい（索引のどのファイルも変わっていない）間は LSP を呼ばず PaaD の `exact` 行だけで答える（LSP が止まっていても `exact`）。LSP が 0 件と答えたのに索引に参照があるときは信用せず `structural` に降格する（内部では `E_LSP_UNAVAILABLE`〔`details.reason: "indexing"`〕として扱い、エラーにはしない。呼び直すと `exact` になる）。TypeScript / JavaScript は、LSP（tsserver）が `tsconfig.json` の `include` に入らないファイル（`test/` など）を開かれるまで探さないので、対象の名前を単語境界で含む同系のファイル（LSP が未オープンのもの、テスト優先、1 回に 100 件まで。超えた分は `warnings[]`）を先に開かせてから聞く（実装で確定 2026-10-29、BUG-0047）。設計書 §7.5.6。 参照検索の要求（と宣言の位置の definition）は、要求 1 つの期限 3,000 ms ではなく 1 問い合わせの予算（10 秒。最初の診断の待ちを含む）の残りまで待ち、連続タイムアウトには要求 1 つの期限以上待っても答えなかったときだけ数える（§12.3）。予算の内に答えなければ `structural` に降格し、`accuracy_warning` と `warnings[]` の 1 行に、どのサーバーのどの要求が予算（実際の値）の内に答えなかったか・LSP は動いていること・呼び直すと `exact` になる見込みを書く（実装で確定 2026-10-31、BUG-0091。理由: T-07 と同じ。冷えたサーバーで中核のシンボルの参照検索が 3 秒を超えるだけで、3 回で LSP をセッションの間 `unavailable` にしていた）。basedpyright の参照検索には期限切れでも `$/cancelRequest` を送らない（BUG-0090）のは同じ。
3. `structural`: Tree-sitter の識別子一致をワークスペース内で集める。スコープを区別しないため同名別シンボルを含みうる旨を `accuracy_warning` に書く。実装は基底クラス名の文字列一致で推定する。
4. `heuristic` / `lexical_fallback`: 対象名の単語境界 grep で集め、`use` は定義パターンから `w`、それ以外 `r` とする。
5. 並びはパス辞書順、次に行番号。`implementations` と `type_hierarchy` は先頭にまとめて出し、ページネーションしない（50 件を超えたら件数と先頭 50 件）。`references` は 1 参照箇所を 1 レコードとしてページネーションする。
6. `dot` / `mermaid` では参照元の囲み関数（`enclosing_urn`）→対象のエッジを `use` ラベル付きで出し、同じ囲み関数からの複数参照は 1 エッジに `×N` を付ける。
7. **受け手の型が分からない呼び出し**（`x.name(...)` で `x` の型が構文から読めない: 注釈なし・`Any`・`self.x` の型が不明・呼び出しの戻り値など）は `relations` に先が決まらないので、索引器が位置と名前を内部表 `_name_calls` に残す。対象が `Method` のとき、**呼び出し箇所ごとに**、同じ言語族（Python / TS・JS）の同名の `Method` を近い段から探し、最初に定義のある段で決める（BUG-0077）: ①**同じファイル**（テストファイルの中のテストダブルも含む）→ ②呼び出し箇所を含む**テストフィクスチャ**（テストディレクトリ〔`test` / `tests` と、言語の `test_path_globs` のうち `**/<名前>/**` の形のもの。JS / TS の `__tests__` など〕の配下にある `fixtures` / `fixture` / `__fixtures__` / `testdata` / `test_data` の直下のディレクトリ。例 `tests/fixtures/<名前>/`。入れ子なら内側から外へ。`workspace/languages.py` の `fixture_roots`）→ ③**ワークスペース**（どのフィクスチャにも入っていない定義）。②③の段では、その段のルートから見てテストファイルの定義（テストダブル。フィクスチャの中ならそのルートからの相対パスで `files.is_test` と同じ規則）は数えず、段に同名の定義がテストダブルしか無ければ候補なしとしてそこで止まる（外の段の同名に結ばない。`conn.execute()` をテストの偽物 `Slow.execute` に結ばない）。呼び出し箇所を含まないフィクスチャの定義は候補にしない（別の見本のプロジェクトのクラス）。呼び出しを囲むクラス自身のメソッドは候補に数えるが、唯一の候補でも結ばない（`self.m()` は構文木で結ぶので、型の分からない受け手は別のオブジェクト。`def kill(self): self.proc.kill()` のような委譲）。(a) 決めた段の候補が**対象 1 つだけ**で、名前が組み込み型のメソッド名（`list.append`・`dict.get`・`Array.push` など。固定の一覧）でなければ、`structural` の答えにその位置を `use: call`・`confidence_level: heuristic`・`match: "name"` で足し（compact は行末に `conf=heuristic`、見出しの `references:` に `by name: N`）、`warnings[]` に件数と根拠（「`<対象>` is the only method named '<名前>' in the workspace」。段に応じて `in the test fixture <ルート>/ that holds the call` / `in the calling file`）を書く。(b) 決めた段に同名が **2 つ以上**（同じ近さ）、または組み込み型と同名ならどの候補にも足さず、対象が候補に入っている箇所だけ、件数・同名の定義・先頭の位置（warnings は 5 件、`data.unattributed_calls[].sites` は 20 件まで）を `warnings[]` と `data.unattributed_calls[]` に書く（全候補に足すと `run`・`get` のような名前で参照の一覧が推定で埋まるため）。受け手の型が分かっていてそのクラスに名前が無い呼び出し（`runner: Runner` の `runner.pack()`）は名前で結ばない。`use_kinds`・`scope_paths`・`include_tests` は名前で足す行にも効く。`exact`（LSP・PaaD の `exact` 行）の答えの `references[]` には足さない（LSP の答えのまま）: (a) の条件で LSP の答えに無い位置（`Any` の受け手は LSP も型を決められない）は、**補足の欄** に位置まで並べる。compact は本体の参照の後ろに `- not in exact answer (matched by name; receiver type unknown to the LSP): N sites` の `note` と、行末に `conf=heuristic` の `ref-line`（並びは本体と同じ）。json は `data.name_matched[]`（`references[]` と同じ形で `confidence_level: heuristic`・`match: "name"`）と `totals.name_matched`。見出しの `references:` は `… sites (tests: K) + N by name, not confirmed by the LSP (tests: M)`、`warnings[]` に件数と根拠（`data.name_matched` を指す）。補足の欄も本体と同じく予算に詰め、`next_cursor` でページをまたぐ（本体の後ろに続く）が、`_resolution_meta` の精度の集計には数えない（§5.5）。同名が同じ近さに複数なら LSP の答えが正なので何も書かない（実装で確定 2026-10-31、BUG-0071。理由: 呼ぶ側の AI にとって、本番の呼び出し元が黙って 0 件に見えるのは、印の付いた推定より悪い〔編集の影響を見落とす〕。補足の欄は BUG-0107 で確定 2026-10-31。理由: 件数だけの警告では位置が見えず、LSP が温まると構文木の答えより情報が減っていた〔`fit.py:102`・`:142` が見えなくなった〕。`exact` の一覧は純粋なまま、AI が影響範囲に要る位置は印つきで別に読めるようにする）。段を近い順に見るのは BUG-0077 で確定（2026-10-31。理由: 自前の同名クラスを持つフィクスチャの中の呼び出しを本体のメソッドに結ぶ誤りは、取りこぼしより悪い〔影響範囲を誤認する〕。ディレクトリの近さ〔最も近い共通の祖先〕では順位を付けない: Lucianke 自身で測ると本番の曖昧な 127 箇所が近い方の推定に変わり、隣のテストファイルの偽物が別のテストの呼び出しを奪ったため、確かな境界であるフィクスチャのルートだけを段にした）。
   **属性の読み書き**（`x.name`・`x.name = v`・`x.name += 1` で `x` の型が構文から読めない。`_name_calls` の `use_kind` が `r` / `w` / `rw`）も同じ段の規則で判定する: 対象がクラスの `Field` / `Property` / `Constant` か `Method`（`x.name` を渡すのはメソッドの参照）のとき、候補は同じ言語族の同名の **クラスのメンバー**（`Method`・`Field`〔クラス属性・dataclass のフィールド〕・`Property`・クラスの `Constant`）と **インスタンス属性**（メソッドの中の `self.x = ...` / `this.x = ...`。シンボルではないので内部表 `_attr_defs` に残す。クラスの C3 線形化の基底が同名のシンボルかインスタンス属性を持てば、基底のものと同じ属性として 1 つに数える）。(a) の条件なら `use: r` / `w` / `rw`・`confidence_level: heuristic`・`match: "name"` で足し（`warnings[]` は「N read/write reference(s) marked conf=heuristic are matched by name only … `<対象>` is the only member named '<名前>' …」）、(b) は組み込み型の属性・メソッドと同名（`name`・`args`・`path`・`length`・`append` など固定の一覧と `__x__`）と、**外部の型のメンバーと同名**（ワークスペースの型の分かる受け手が使った `lk://_ext/<pkg>#<Class>.<名前>`。`node: Node` の `node.parent` → `tree_sitter.Node.parent`。`sys.path` を足して読む兄弟のモジュールを import の解決が外部にしたものは数えない。`reason: "external_name"`）を含め、件数・候補（インスタンス属性は `kind: "instance_attribute"`）・先頭の位置を `warnings[]` と `data.unattributed_refs[]` に書く。呼び出しの候補は`Method` だけのまま、T-07 は属性の読み書きを使わない（実装で確定 2026-10-31、BUG-0095。理由: 型の分からない受け手を `structural` で結ばなくした〔BUG-0085〕ことで、フィールドの読み書きが T-06 に件数も警告も無く 0 件に見えていた〔Lucianke 自身で 1,293 箇所〕。呼び出しと同じく、印つきで結ぶか曖昧と書けば AI は影響を見落とさない。外部の型のメンバーと同名を結ばないのは、型の分からない受け手はその外部の型であることが多いため: Lucianke 自身で名前で結んだ属性の読みの上位〔`SkelNode.type` 332・`_Def.parent` 153 箇所〕は tree-sitter の`Node.type`・`Node.parent` だった。これを外すと名前で結ぶ 2,715 箇所の抜き取り 25 件中 22 件が正しい先）。
8. **基底クラスから継承したメンバー**（受け手の型は分かるが、そのクラスに名前が無い: `self.helper()`・`cls.make()`・`s: Sub` の `s.helper()`・`Sub().helper()` で `helper` が基底クラスにある）は、受け手のクラスの C3 線形化（Python の MRO。基底は索引の `INHERITS` 行を書いた順、ファイルをまたぐ。TS / JS は `extends`〔interface の `extends` を含む〕。`implements` は数えない）を先頭から見て、最初にその名前を持つクラスのメンバーへの `structural` の参照・呼び出しにする（`match` は付けない）。派生クラス・途中の基底が上書きしていればそちらに結ぶ。中身の分からない外部の基底（`lk://_ext/`。ただし `object`・`typing.Generic`・`typing.Protocol`・`abc.ABC` はユーザーのメソッドを持たないので素通りする）やクラスでない基底（`Base = make_base()` の別名など）に先に当たったら結ばない（その基底が同名を持つかもしれない。名前でも結ばない）。基底のメンバー・上書き・基底の並びが変わったら、索引がその派生クラスを受け手にする参照を結び直す（`_member_refs`。設計書 §7.6.2）（実装で確定 2026-10-31、BUG-0075。理由: `self` 経由の継承メソッドの呼び出しは OO のコードで最も多い形の一つで、基底のメソッドを変える AI にその呼び出し元が 0 件に見えると影響を見落とす。受け手の型と継承関係から決まるので推定〔heuristic〕ではない）。
9. **受け手の決め方（属性の連鎖と `super()`）**:
   (a) `a.b.m()` の受け手は `a.b` で、`type(a)` のメンバーにも同じファイルの同名にも結ばない。`a.b` の型は構文から分かるときだけ決める（`a` の型が同じファイルのクラスで、`b` がその注釈つきのクラス属性・インスタンス属性〔`self.b: T = ...`・dataclass のフィールド〕・`self.b = T()`・戻り値の注釈つきの property〔TS の getter・フィールド・パラメータプロパティ〕のとき。同じファイルの基底も C3 の順に見る。別ファイルのクラスの属性は分からないとする）。分からなければ受け手の型が分からない呼び出し（処理規約 7）にする。呼び出しの戻り値・添字など名前でない受け手も同じ（実装で確定 2026-10-31、BUG-0079。理由: 呼ぶ側の AI にとって誤った呼び出し元は取りこぼしより悪い。httpx の `url._uri_reference.copy_with()`〔`ParseResult.copy_with`〕が `URL.copy_with` の呼び出し元に入り、影響範囲を誤らせた）。
   受け手が名前（`x.m()`）のときも、型が構文から分かるときだけその型のメンバーに結ぶ: パラメータ・変数の注釈（前方参照の文字列 `x: "Foo"`・`Optional["Foo"]` を含む。`Any` は分からないとする）、`x = Foo()`（同じファイル・import したクラスの構築。TS / JS は `new Foo()`）、`self` / `cls` / `this`（囲みのクラス）、モジュールの名前（`import m`・`from pkg import m`・TS の `import * as m`。`m.f()` はメソッドではなくモジュールの関数 `f`。外部の `os.path.join` も同じ）。分からなければ（注釈の無い引数・型の分からないローカル・ループ変数・型の分からないモジュール直下の変数・クラスの外の `self` / `this`）、同じファイルにその名前が 1 つしか無くても、同じ名前を import していても結ばず、処理規約 7 にする（実装で確定 2026-10-31、BUG-0085。理由: 同じファイルの唯一の同名に `structural` で結ぶと、別のファイルのクラス・組み込み型の受け手でも確かな行に見える。Lucianke 自身では `_log.error(...)`〔`logging.Logger`〕が同じファイルの `StepResult.error` に、`conn.close()`〔`sqlite3.Connection`〕が `ReaderPool.close` に、`urn.relax()` が同じ名前を import したモジュールの関数 `relax` に結ばれていた。処理規約 7 なら印〔`conf=heuristic`〕つきで結ぶか、曖昧・組み込み型と同名と書く）。
   **Python の受け手の注釈**は先頭の名前に縮めずに読む: `A | None`・`Optional[A]`・`Union[A, None]`（前方参照の文字列 `"Optional[A]"`・`Optional["A"]` も）は `A`、`None` を除いて構成が 2 つ以上の合併（`A | B`・`Union[A, B]`・`"B | A"`・`type[A | B]`）は分からない（処理規約 7。TS の受け手の合併と同じく、構成のどれか 1 つだけがメンバーを持つかは見ない）。容器 `list[A]`・`dict[K, V]`・`Sequence[A]`・総称のクラス `Box[A]` は容器の型（`xs.append()` は `lk://_ext/builtins#list.append`。要素の型 `A` のメンバーにしない。typing の古い綴り `List`・`Dict`・`Set`・`FrozenSet`・`Tuple` は組み込みの型）、`type[A]` / `Type[A]` はクラス `A`（`t.make()` は `A.make`）、`Annotated[A, ...]`・`Final[A]`・`ClassVar[A]`・`Required[A]`・`NotRequired[A]`・`ReadOnly[A]` は `A`、点つきの `mod.Foo`・`pkg.mod.Foo` はそのモジュールの `Foo`（いちばん長いワークスペースのモジュールから。外部の `argparse.Namespace` は `lk://_ext/argparse#Namespace`）。同じファイルの型の別名（`type AB = A | B`・`OptA: TypeAlias = Optional[A]`）は値を同じ規則で読み、別ファイルの型の別名は構成を索引に書いていないので分からない。`Literal[...]`・`Callable[...]` なども分からない。`Any`・`Self`・素の `Final` / `ClassVar` / `TypeAlias` は型の手がかりにならず、代入の値（`x: Final = Foo()` の `Foo`）に任せる。それ以外は注釈が決める（`x: A | B = A()` の `x` は分からない）。モジュール直下の変数の型（import した変数の `settings.reload()`。BUG-0094）も同じ規則。同じファイルの定義・ワークスペースの import がtyping の名前を隠すとき（`class Literal`）は特別な形にしない（実装で確定 2026-10-31、BUG-0117。理由: 前は注釈の最初の型の名前に縮めていて、`x: A | B` の `x.run()` を `A.run` に `structural` で結び〔`B.run` の参照からは抜け、`A` に無い `x.only_b()` は黙って落ちた〕、`src: str | bytes` の `src.encode()` を `str.encode` に、`OA: TypeAlias = ...` を `typing.TypeAlias` 型の変数にしていた。TS の BUG-0113 と同じく、誤った確かな行は取りこぼしより悪い）。
   **点つきのモジュールのパス**（`import pkg.helpers` の `pkg.helpers.flush()`・`import a.b as ab` の `ab.c.f()`・`from pkg import sub` の`sub.inner.f()`。`__init__.py` の無い名前空間パッケージの `import ns.mod` も）は、ワークスペースにそのサブモジュールのファイルがあればモジュールの受け手で、`flush` はその関数（`structural`。`pkg.helpers` の `helpers` の読みはサブモジュールのファイルへの参照）。モジュールの名前の属性の連鎖（`from pkg import conf` の `conf.Settings.make()`・`conf.settings.reload()`）も `conf.py#Settings` / `conf.py#settings` を受け手にする。**import した変数**（`from conf import settings` の `settings.reload()`・`settings.debug`）は、定義元のモジュール直下の変数の型が構文から分かれば（`settings = Settings()`・`settings: Settings = ...`・TS の `export const s = new S()`。内部表 `_var_types`）その型の C3 線形化で見つかるメンバー（外部の型なら外部のメンバー）に `structural` で結び、分からなければ処理規約 7 の名前の判定にする。**再び公開した名前**（`from pkg import flush` で `pkg/__init__.py` が `from .helpers import flush`。別名・絶対 import・何段の連鎖・`from x import *`〔`__all__` があれば載っている名前、無ければ `_` で始まらない名前〕も）は定義元のシンボルまでたどって同じ水準で結ぶ（`flush()` は `pkg/helpers.py#flush` に `structural`、再び公開したクラス・変数のメンバー〔`shared.reload()`・`EST = TokenEstimator()` の `EST.count()`〕・基底も定義元で決める。循環・5 段を超える連鎖は結ばない）。連鎖の途中のファイルを編集すると、import している側を結び直す。たどれない（再公開を消した・束ねていない）ときは、素の名前は結ばず、受け手の名前は処理規約 7 にする（実装で確定 2026-10-31、BUG-0100。理由: パッケージは公開 API を `__init__.py` で再び公開するのが普通で、そこから import した呼び出し元が黙って消えると、ライブラリの関数の実際の呼び出し元の大半が見えない）。**TS / JS のバレル**（`index.ts` の `export { flush } from './helpers'`・`export { a as b } from`・`export * from`・`export * as ns from`〔`ns.fn()`〕・import した名前の `export { a }`・既定の export の明示の再公開。`import { flush } from './lib'` の `./lib` は `./lib/index.ts`）も同じ規則で定義元まで `structural` でたどる（`export *` は公開する名前だけで、既定の export と export していない定義は公開しない。実装で確定 2026-10-31、BUG-0104。理由: バレルは TS のパッケージの公開 API の普通の書き方で、LSP が無いと呼び出し元の大半が見えない）。**import する側の `from helpers import *`** が持ち込んだ名前（`__all__` があれば載っている名前、無ければ `_` で始まらない名前。`*` の先がさらに再び公開する名前も）も定義元に`structural` で結ぶ。`*` の先の `__all__` がリテラルでなく公開する名前が決まらないときは、公開名の規則で見つけた定義に `heuristic` で結ぶ（名前だけで決めた。どの `*` の先にも無い名前は結ばない。実装で確定 2026-10-31、BUG-0105。理由: `*` で使っている呼び出し元が黙って消えるより、決まる範囲は確かに、決まらない範囲は水準で知らせて結ぶ方が編集の影響を見落とさない）。**TS / JS の既定の import**（`import main from './helpers'`・`import main, { other } from`・`import * as m` の `m.default()`）は、そのモジュールの既定の export（`export default function f`・`export default class C`・`export default h`〔同じファイルの定義〕・`export { x as default }`・バレルの `export { default } from` / `export { default as foo } from`）の定義まで同じ規則で `structural` でたどり、呼び出し・参照・型の使用・import 文の行を結ぶ。無名の既定の export（`export default () => …`・`export default { … }`）はシンボルが無いので、import 文の行・呼び出し・参照はそのモジュール（ファイルの URN）に結ぶ（黙って捨てない。T-07 では `(module)` のノード。無名のオブジェクトのメンバー `cfg.level` は処理規約 7 の名前の判定）。既定の export を変えると import している側を結び直す（実装で確定 2026-10-31、BUG-0109。理由: 既定の import は TS / JS の普通の書き方で、LSP が無いとその呼び出し元が import 文の行ごと消えていた）。**TS の型の別名を注釈にした受け手**（`o: Options` で `type Options = Base & { extra: string }`）は、別名の構成からメンバーを探す: 別名そのもの（`type A = B`。総称の別名 `type Box<T> = Base<T> & {…}` は `Base`、`Partial` / `Required` / `Readonly` / `NonNullable` / `Pick` / `Omit` は第 1 型引数）と交差は、書いた順に最初にメンバーを持つ構成（クラス・インターフェースは C3 の順、別名はさらにその構成、外部の型の構成は最後にそのメンバー）に `structural` で結ぶ。合併（`B | C`。`null` / `undefined` は数えない）はメンバーを持つ構成がちょうど 1 つのときだけ結ぶ。別名自身の型リテラルのメンバー（`o.extra`）は索引にシンボルがあればそれに結び、無ければ結ばない（別の型の同名にも当てない）。合併で持つ構成が 2 つ以上・中身の分からない構成（関数型・条件型・添字のシグネチャ・`string`・型引数など）は処理規約 7 の名前の判定にする。別名を継承したインターフェース（`interface Options extends KyOptions`）の受け手も別名の構成をたどる。別名・構成の型を編集すると受け手を結び直す（実装で確定 2026-10-31、BUG-0108。理由: 型の別名は TS の型の普通の書き方で、注釈を書いた受け手の読みが行にも名前の一致にも残らず、注釈の無い受け手より見えなくなっていた）。v13 から別名自身の型リテラルのメンバーはシンボル（`Options.extra`。§7.2）で、受け手の読みはそれに `structural` で結び、交差ではほかの構成より先に見る（クラス自身のメンバーが基底より先なのと同じ。合併の別々の型リテラルの同名のメンバーは素の URN を 1 つに数える）（実装で確定 2026-10-31、BUG-0114）。**TS の受け手の注釈は先頭の名前に縮めずに読む**: 添字の型 `r: Opts['retry']`（鍵が識別子の形の文字列のとき。`Opts['a']['b']` も）の受け手はメンバー `Opts.retry` の型で、`r.limit` はその型（メンバーの型注釈。インターフェース・型リテラルのプロパティ・クラスのフィールド）のメンバーに `structural` で結ぶ（メンバーの型が型リテラルならその入れ子のメンバー `Opts.retry.limit`、名前の型・別名・合併は上の別名と同じ規則）。配列 `Opts[]`・`Array<Opts>`・タプルは標準の `Array`、`readonly Opts[]` は `ReadonlyArray` の受け手（`xs.map()` は `lk://_ext/globals#Array.map`。要素の型 `Opts` のメンバーにしない。`xs[0].x` の要素の読みは型の分からない受け手）。`Foo | null | undefined` は `Foo`、`Promise<T>` は `Promise`（`await` した値は `T`）、`Partial<Foo>` などメンバーを持ち越す標準の型は `Foo`、`ns.Foo` は名前空間・`import * as ns` の `Foo`（同名の標準のグローバルにしない）、`x as Foo` は `Foo`。構成が 2 つ以上の合併・交差・型リテラル・型引数・鍵が文字列でない添字・型の分からないメンバー（`count: number`）・無いメンバー（`Opts['missing']`）は型の分からない受け手（処理規約 7）。属性の連鎖 `o.retry.limit`（`o` の型が別ファイルの型・同じファイルのインターフェース）も `o.retry` の型をメンバーの型として同じく決める（基底から継承したメンバー・型の別名の構成のメンバーもたどる）。メンバー・その型を編集すると受け手を結び直す（実装で確定 2026-10-31、BUG-0113。理由: 前は注釈の綴りの最初の型の名前に縮めていて、`Opts['retry']` の `r.limit` を `Opts.limit` に、`errors: Error[]` の `errors.push()` を `Error.push` に `structural` で結んでいた。誤った確かな行は取りこぼしより悪い）。**名前空間パッケージ**（`__init__.py` の無いワークスペースのディレクトリ。`import nsp.tools.fmt` の `nsp`・`nsp.tools`、`import nsp.tools as nt`、`from nsp import tools`）の読みは参照にも外部（`lk://_ext/nsp`）にもせず、その下のモジュール（`nt.fmt.render()`）はワークスペースの関数に結ぶ（実装で確定 2026-10-31、BUG-0101。理由: ワークスペースのディレクトリを確かな外部パッケージへの参照にすると誤った答えになり、ディレクトリはシンボルでもファイルでもないので T-06 の対象にもならない）。変数の定義・型のクラスを編集すると、import している側を取り込み直さずに結び直す（実装で確定 2026-10-31、BUG-0093・BUG-0094。理由: どちらも先が無い URN〔`pkg/conf.py#settings.reload`〕か名前だけの呼び出しの候補外〔モジュールの関数〕になって、`structural` でも名前の一致でも出ず、警告も無かった。共有のインスタンス・サブモジュール経由の呼び出し元が黙って 0 件に見えると、AI は編集の影響を見落とす）。
   (b) `super().m()`（`super(C, self).m()`、TS / JS のクラスのメソッド〔とその中のアロー関数〕の `super.m()`）は、`C`（`super()` と `super.m()` は囲みのクラス）の C3 線形化で **`C` の次から** 最初に `m` を持つクラスのメンバーへの `structural` の参照・呼び出しにする（`match` は付けない。基底が別ファイルでも索引の整えが `INHERITS` 行をたどる。`_member_refs` の `via_super`）。名前の一致（処理規約 7）には回さない。処理規約 8 と同じく、中身の分からない外部の基底・クラスでない基底に先に当たったら結ばない。`C` が分からない（`super(type(self), self)`・クラスの外）ときも結ばない（実装で確定 2026-10-31、BUG-0081。理由: 上書きから `super()` で基底を呼ぶのは基底のメソッドの代表的な呼び出し元で、上書きがある限り同名は 2 つ以上あるので名前では決して結べない。受け手は構文から分かるので「型不明」でもない）。
   (c) 処理規約 8 と (b) で、先に当たった基底のために結ばなかった参照のうち、対象のクラスがその基底の後ろにある（その基底が名前を持たなければ対象に届く）ものは、`structural` の答えに件数・理由・先頭の位置を `warnings[]` と `data.unattributed_calls[]`（`reason: "external_base"`〔中身の分からない外部の基底〕\| `"unknown_base"`〔クラスでない基底〕）で書く。`exact` の答えには書かない（LSP は外部の基底の中身も知っている）（実装で確定 2026-10-31、BUG-0081。理由: 結ばなかった理由が「型不明・曖昧」ではないことを AI が読め、必要ならその位置を自分で確かめられる）。

**出力**（compact）:

```
# find_references_and_implementations ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~420/2000tok
- target: [Method] lk://app/src/services/payment.py#PaymentProcessor.process_charge
- implementations: (none; concrete method)
- type_hierarchy: (n/a)
- references: 3 files, 5 sites (tests: 2)
src/api/routes.py:88:20  [call] in create_order  receipt = await processor.process_charge(req)
src/api/routes.py:131:20  [call] in retry_order  receipt = await processor.process_charge(req)
tests/test_payment.py:44:12  [call] in test_charge_ok  r = await p.process_charge(req)
# more: cursor=c730215948 remaining≈2
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `target` | SymbolRecord | |
| `implementations[]` | SymbolRecord[] | |
| `type_hierarchy` | object | `supertypes[]`, `subtypes[]`（SymbolRecord） |
| `references[]` | object[] | `path`, `line`, `col`, `use`, `enclosing_urn`, `enclosing_name`, `snippet`, `is_test`, `confidence_level`。名前だけで結んだ行（処理規約 7）にだけ `match: "name"`（`confidence_level` は `heuristic`） |
| `totals` | object | `files`, `sites`, `tests`。名前で足した行があれば `by_name`。`exact` の答えに補足の欄があれば `name_matched`（全ページの件数） |
| `name_matched[]` | object[] | `exact` の答えの補足の欄（処理規約 7。あるときだけ・そのページに含まれる分）。LSP の答えに無いが名前で対象に結べる位置。`references[]` と同じ形で、`confidence_level` は `heuristic`、`match: "name"`。`references[]` の後ろに続けてページネーションする（BUG-0107） |
| `unattributed_calls[]` | object[] | 処理規約 7 で結ばなかった型不明の呼び出しのうち対象が候補のもの（あるときだけ。理由と候補の組ごとに 1 要素）。`name`, `total`, `reason`（`ambiguous` \| `builtin_name`）, `candidates[]`（`qualified_name`, `path`, `line`）, `candidates_total`, `builtin_name`, `sites[]`（`path`, `line`, `col`, `enclosing_name`, `is_test`。先頭 20 件）。処理規約 9 (c) の要素は `reason` が `external_base` \| `unknown_base` で、`candidates` を持たず、`sites[]` に `use`・`via_super`（`super()` 経由か）・`receiver_class`・`blocked_by`（先に当たった基底の URN）を足す |
| `unattributed_refs[]` | object[] | 処理規約 7 で結ばなかった型不明の受け手の属性の読み書き（`x.name`）のうち対象が候補のもの（あるときだけ。BUG-0095）。`unattributed_calls[]` と同じ形で、`reason` は `ambiguous` \| `builtin_name` \| `external_name`、`candidates[]` に `kind`（`Field` / `Property` / `Constant` / `Method` / `instance_attribute`）、`sites[]` に `use`（`r` / `w` / `rw`）を足す。`external_name` のときは `external_members[]`（`tree_sitter.Node.parent` の形）を持つ |

**エラー**:

- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: 対象の解決。
- `E_INVALID_ARGUMENT`: `use_kinds` に未知の値、ルート外の `scope_paths`。
- `E_TIMEOUT`: 降格時の grep が 2,000 ms を超えた（部分結果を `details.partial` に）。
- `E_WORKSPACE_NOT_CONFIGURED`, `E_REVISION_NOT_FOUND`, `E_CURSOR_EXPIRED`: §5.8 のとおり。

**精度と降格**: `exact` はスコープと型を区別した参照、実装、型階層をすべて返す。`structural` は同名識別子の一致を返し、実装は推定、型階層は直接の基底・派生のみ。`heuristic` と `lexical_fallback` は参照のみで、実装と型階層は `(unavailable)` とする。`include_lexical_in_other_files: true` で加わった行は個別に `conf=lexical_fallback` を付け、応答は `mixed: true`。処理規約 7 で名前だけで結んだ行は個別に `conf=heuristic` を付け、応答は `mixed: true`（`confidence_level` は最も低い `heuristic`）。`exact` の答えの `references[]` には名前で結んだ行を混ぜず、補足の欄（`name_matched[]`）に別に並べる（集計に数えないので応答は `exact` のまま）。

**事後条件**:

- `exact` 水準のとき、`references[]` の `(path, line, col)` の集合は LSP の参照検索結果（`include_declaration` の設定を反映）と等しい。
- 各参照行の `use` は 7 値のいずれかである。
- `include_tests: false` のとき `is_test: true` の行は 0 件である。
- 全ページを通じて同じ `(path, line, col)` は 1 度しか現れない。
- 解決できない `import` は外部シンボル `lk://_ext/<パッケージ名>#<名前>`（`is_external`）として索引に現れ、それを `target` にした `references` は `import` の使用箇所を返す。
- `budget.used_tokens_est ≤ max_tokens`。

### T-07 `query_call_hierarchy`

**目的**: 関数・メソッドを起点に、呼び出し元（incoming）または呼び出し先（outgoing）を指定深さまで木で返す。各呼び出しがどの条件分岐の中にあるか（ガード条件）を添える。

**マイルストーン**: M1

**対応要求**: FR-NAV-4, FR-LANG-2, FR-FMT-2, FR-FMT-3, FR-FMT-4, NFR-PERF-1, NFR-OPS-3

**入力**（`max_tokens` 既定 2000。`format` は `dot` / `mermaid` 可）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `target` | TargetRef | はい | なし | 呼び出し可能なシンボル（Function, Method, Constructor）。Class を渡したときはコンストラクタ呼び出しを起点にする |
| `direction` | enum | はい | なし | `"incoming"` \| `"outgoing"` \| `"both"` |
| `depth` | integer | いいえ | `2` | 1〜5 |
| `include_guard_conditions` | boolean | いいえ | `true` | `when:` を付ける |
| `exclude_external` | boolean | いいえ | `true` | 標準ライブラリ・外部パッケージの関数を展開しない（葉として `(external)` 表示） |
| `include_tests` | boolean | いいえ | `true` | |
| `max_children` | integer | いいえ | `20` | 1 ノードあたりの子の上限。超過分は `- children: N more` に要約 |

**処理規約**:

1. `target` を §5.3 で解決する。`Variable` / `Constant` / `Type` 等の呼び出せない種別は `E_UNSUPPORTED`。
2. `exact`: LSP の呼び出し階層（prepare → incoming / outgoing）を幅優先で `depth` まで辿る。辺は `relations` に `exact` で書き戻し（`data_version` は進めない）、2 回目以降はノードごとに、書き戻した行がまだ新しい（索引のどのファイルも変わっていない）間は LSP を呼ばず PaaD の `exact` 行だけで答える。根について LSP が 0 件と答えたのに索引に呼び出しがあるときは信用せず `structural` に降格する（設計書 §7.5.6。実装で確定 2026-10-29、1d8700d）。LSP が呼び出し階層を提供しない、または索引中のときは `relations` の `CALLS` エッジで代替し `structural` とする。 根の要求（根の子を求める prepare と incoming / outgoing）は要求 1 つの期限ではなく予算の残りまで待ち、連続タイムアウトには要求 1 つの期限以上待っても答えなかったときだけ数える（§12.3。BUG-0091）。 **LSP の時間の上限で切れたとき**（1 問い合わせで LSP に使える時間の合計 `EXACT_BUDGET_S` = 10 秒、または深いノードの要求の§12.3 の期限 5,000 ms）: それまでに LSP（または PaaD の `exact` 行）が答えたノードは `exact` のまま返し、残りのノードの子だけを `relations` で補う（そのノード以下は `structural`。処理規約 9 の名前の判定も補った分にだけ使う。応答は `mixed: true`）。補ったノードには json で `children_confidence_level: "structural"`、compact で `- children: from the syntax index (conf=structural; the LSP did not answer)` を付ける。根の子を取れなければ木ごと `structural`。木は深さごとに聞く（幅優先。切れても浅い段は揃う）。`accuracy_warning`（降格したとき）と `warnings[]` の 1 行には、切れたのが 1 問い合わせの予算か要求の期限か・その実際の値（要求の期限ならその要求とノード）・`exact` で答えたノードの数と全部 `exact` の深さ・次にすること（LSP は動いているので呼び直す。答えた分は書き戻し済みなので、呼び直すと残りだけを LSP に聞く）を書く。（実装で確定 2026-10-31、BUG-0091。理由: 冷えた LSP への最初の問い合わせが予算に届くと、以前は答えた分も捨てて木ごと `structural` にし、警告は使い切った予算ではなく要求の期限「5000 ms」を書いていた。呼ぶ側の AI は答えた分を `exact` のまま使え、警告から「呼び直せば exact になる」と分かり、呼び直しは前の問い合わせの続きから進む。）
3. `heuristic` / `lexical_fallback`: `name(` の単語境界 grep で呼び出し元候補を集め、1 段のみ返す。`depth ≥ 2` を要求されても 1 段で打ち切り、`accuracy_warning` にその旨を書く。
4. ガード条件: 呼び出し箇所から囲みの関数本体まで AST の祖先を遡り、`if` / `elif` / `else`（否定で表す）/ `match`・`case` / `try`・`except`（`on: 例外型`）/ `while` / 三項演算 / 短絡 `and`・`or` の条件式のソーステキストを `&&` で連ねる。120 文字で省略する。囲みに条件が無ければ `when:` を付けない。否定は言語によらず `!(条件)` と書き、`elif` の中の呼び出しは「前の分岐の条件の否定」と「その `elif` の条件」を連ねる（例: `if a: … elif b: f()` の `f` は `when: !(a) && b`）。`or` などを最上位に持つ条件は、ほかと連ねるとき `(条件)` で囲む。書き方は `exact` でも `structural` でも同じ（どちらも同じ構文木から作る）。理由: 呼ぶ側の AI が言語ごとの否定の書き方を知らずに、その呼び出しが実行される条件をそのまま読めるようにする（BUG-0074）。
5. 循環: 根からそのノードまでの経路に既に現れた URN は `(cycle)` を付けて 1 回だけ表示し、展開しない（MUST）。
6. 動的ディスパッチ: `outgoing` で抽象メソッド・インターフェース経由の呼び出しは抽象側を表示し、実装の数を `-> impl ×N` の `note` で添える。展開はしない。
7. `both` は incoming の木、outgoing の木の順に 2 本出す。
8. 1 レコードは 1 `tree-line`。予算が尽きたら幅優先の途中で止め、残りを `# summary: depth 3: 42 more callers in 9 files` のように要約し、`next_cursor` で続きを返す。
9. **受け手の型が分からない呼び出し**（T-06 処理規約 7 と同じ `_name_calls` と同じ判定）: `structural` の木では、incoming はノードが `Method` なら、その名前の型不明の呼び出しのうち、T-06 処理規約 7 の段の判定（同じファイル → 呼び出し箇所を含むテストフィクスチャ → ワークスペース。BUG-0077）で候補がそのノードだけの箇所を呼び出し元として足す（深さ 2 以降のノードにも適用する）。outgoing はノードの中の型不明の呼び出しのうち、段の判定で候補が 1 つだけのものをそのメソッドへの呼び出しとして足す。どちらも `confidence_level: heuristic`・`match: "name"`（compact は `tree-line` の `  conf=heuristic`）。ノードの水準は呼び出し箇所のうち最も高いもの（名前解決済みの箇所が 1 つでもあればその水準）で、`match` はすべての箇所が名前だけのときに付ける。結べない（決めた段に同名が複数・組み込み型と同名）ものは向きごと・名前と候補の組ごとに件数と先頭の位置（incoming は根やノードが候補の箇所だけ）を `warnings[]` と `data.unattributed_calls[]`（`direction` 付き）に書く（outgoing は組み込み型と同名のものを数えない。`append`・`get` などは組み込み型の呼び出しがほとんどのため）。`exact` の木には混ぜず、根の incoming で結べる（その箇所の候補が根 1 つだけ）のに LSP の答えに無い呼び出しの呼び出し元を **補足の欄** に並べる: compact は incoming の木の直後に `- not in exact tree (matched by name; receiver type unknown to the LSP): N callers, M sites` の `note` と `<- <呼び出し元> @<path>:<line>[  when: …]  conf=heuristic` の `tree-line`（深さ 1 だけ・その先は辿らない。`max_children` まで、超えたら見出しに `; first K callers shown (max_children)`）、json は `data.name_matched[]`（`nodes[]` と同じ形で `depth: 1`・`confidence_level: heuristic`・`match: "name"`。`edges[]` には入れない）と、省いた数 `data.name_matched_truncated`。`warnings[]` に件数と根拠。予算・ページネーションは木と同じ（incoming の木の後ろに続く）、`_resolution_meta` の精度の集計には数えない（§5.5）。`include_tests: false` ならテストの呼び出し元は出さない。`dot` / `mermaid` では補足の辺を降格の印（`conf=heuristic`）つきで描く（実装で確定 2026-10-31、BUG-0071。理由: 呼ぶ側の AI にとって、本番の呼び出し元が黙って 0 件に見えるのは、印の付いた推定より悪い〔編集の影響を見落とす〕。補足の欄は BUG-0107 で確定 2026-10-31。理由: T-06 処理規約 7 と同じ。件数だけでは位置が見えず、構文木の木より情報が減っていた）。型の分からない受け手の属性の読み書き（T-06 処理規約 7 の `use_kind` が `r` / `w` / `rw` の行）は呼び出しではないので木に入れない（BUG-0095）。
10. **基底クラスから継承したメンバー**（T-06 処理規約 8 と同じ）: `self.helper()`・`s: Sub` の `s.helper()` などは、受け手のクラスの C3 線形化で最初に見つかるメンバーへの `CALLS` の辺（`structural`）として、incoming（基底のメソッドを根にしたとき派生クラス側の呼び出し元が出る）にも outgoing（呼び先は上書きがあればその上書き）にも出す（実装で確定 2026-10-31、BUG-0075。理由: T-06 処理規約 8 と同じ）。
11. **受け手の決め方（属性の連鎖と `super()`）**（T-06 処理規約 9 と同じ）: `a.b.m()` の辺は受け手 `a.b` の型で決め（分からなければ処理規約 9 の名前の判定）、名前の受け手 `x.m()` も型が構文から分かるとき〔注釈・構築・`self` / `cls` / `this`・モジュールの名前〕だけ `structural` の辺にし、分からなければ処理規約 9 の名前の判定にする（BUG-0085）、点つきのモジュールのパス（`pkg.helpers.flush()`）と import した変数（`settings.reload()`）の受け手と、再び公開した名前（`from pkg import flush` の `flush()`。TS / JS のバレル経由・import する側の `from x import *` も）・名前空間パッケージ・TS / JS の既定の import（無名の既定の export はモジュールのノード）・TS の型の別名を注釈にした受け手・TS の受け手の注釈（添字の型 `Opts['retry']`・配列・`Promise<T>`）と型リテラルのメンバー（`k.timeout` の `KyOptions.timeout`）・Python の受け手の注釈（`A | B`・`Union[A, B]` は分からない、`Optional[A]` は `A`、容器 `list[A]` は容器、`type[A]` はクラス `A`、点つきの `mod.Foo`）は T-06 処理規約 9 (a) と同じに決め（BUG-0093・BUG-0094・BUG-0100・BUG-0101・BUG-0104・BUG-0105・BUG-0108・BUG-0109・BUG-0113・BUG-0114・BUG-0117）、`super().m()`・`super(C, self).m()`・`super.m()` の辺は `C` の C3 線形化で `C` の次から最初に `m` を持つクラスのメンバーへの `CALLS`（`structural`）にする（outgoing の `Sub.save` の `super().save()` の呼び先は基底の `save` で、自分自身ではない）。先に当たった基底のために結ばなかった呼び出しは、`structural` の木の根の incoming に `warnings[]`（`incoming:` で始まる）と `data.unattributed_calls[]`（`direction: "incoming"`、`reason: "external_base"` \| `"unknown_base"`）で書く（実装で確定 2026-10-31、BUG-0079・BUG-0081。理由: T-06 処理規約 9 と同じ）。

**出力**（compact）:

```
# query_call_hierarchy ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~380/2000tok
- root: [Method] lk://app/src/services/payment.py#PaymentProcessor.process_charge direction=incoming depth=2
<- create_order @src/api/routes.py:88  when: req.amount > 0 && !order.paid
  <- handle_post_order @src/api/app.py:41  when: auth.ok
<- retry_order @src/api/routes.py:131  when: attempt < 3
  <- scheduler_tick @src/jobs/retry.py:20
  <- retry_order @src/api/routes.py:140 (cycle)
<- test_charge_ok @tests/test_payment.py:44
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `root` | SymbolRecord | |
| `direction`, `depth` | string, integer | |
| `nodes[]` | object[] | `urn`, `name`, `path`, `call_site_line`, `depth`, `parent_urn`, `when`, `is_test`, `is_external`, `cycle`, `children_truncated`, `confidence_level`。すべての呼び出し箇所を名前だけで結んだノード（処理規約 9）にだけ `match: "name"`。`call_sites[]` の各要素も同じ条件で `match`。`exact` の木で子を LSP から取れず構文木で補ったノード（処理規約 2）にだけ `children_confidence_level: "structural"` |
| `edges[]` | object[] | `source_urn`, `target_urn`, `call_site_line`, `guard`（dot / mermaid の元）。名前だけで結んだ辺にだけ `match: "name"` |
| `unattributed_calls[]` | object[] | 処理規約 9 で結ばなかった型不明の呼び出し（あるときだけ）。T-06 の同名の欄と同じ形に `direction` を足したもの |
| `name_matched[]` | object[] | `exact` の木の補足の欄（処理規約 9。あるときだけ・そのページに含まれる分）。根の incoming で LSP の答えに無いが名前で根に結べる呼び出しの呼び出し元。`nodes[]` と同じ形で `depth: 1`、`confidence_level: heuristic`、`match: "name"`（BUG-0107） |
| `name_matched_truncated` | integer | `name_matched[]` のうち `max_children` で省いた呼び出し元の数（あるときだけ） |

**エラー**:

- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: 対象の解決。
- `E_UNSUPPORTED`: 呼び出せない種別の対象。
- `E_INVALID_ARGUMENT`: `direction` 欠落、`depth` または `max_children` が範囲外。
- `E_TIMEOUT`: 降格時の grep が 2,000 ms を超えた。
- `E_WORKSPACE_NOT_CONFIGURED`, `E_REVISION_NOT_FOUND`, `E_CURSOR_EXPIRED`: §5.8 のとおり。

**精度と降格**: `exact` は型解決済みの呼び出し関係を `depth` まで返す。`structural` は `relations` の名前解決済みエッジを使い、動的ディスパッチと同名メソッドの区別が落ちる旨を `accuracy_warning` に書く。加えて処理規約 9 で名前だけで結んだノードは個別に `conf=heuristic` を付け、応答は `mixed: true`。`exact` の木には混ぜず（LSP の時間の上限で切れて構文木で補ったノードの子には使う。処理規約 2）、根の incoming の補足の欄（`name_matched[]`）に別に並べる（集計に数えないので応答は `exact` のまま）。LSP の時間の上限で切れたときは、答えた分の `exact` のノードと補った分の `structural` のノードが 1 つの木に混ざる（`mixed: true`、ノードごとの `confidence_level`。処理規約 2。BUG-0091）。`heuristic` / `lexical_fallback` は 1 段のみで `when:` を付けない。

**事後条件**:

- `nodes[].depth` の最大値は `depth` 以下である。
- `exact` 水準のとき、`depth = 1` の incoming ノード集合は LSP の incoming calls の結果と等しい。
- 各 `when:` の文字列は、その呼び出し箇所を囲む条件ノードのソーステキストと、処理規約 4 の連ねる記号（` && `・`!( )`・`( )`・`on: `・末尾の `…`）だけから構成される（固定フィクスチャで期待文字列と一致）。
- 同じ経路上で同じ URN が 2 回以上展開されない（`cycle: true` のノードは `children` を持たない）。
- 解決できない `import` の呼び先は、`is_external: true` の葉ノード（`lk://_ext/<パッケージ名>#<名前>`。`children` を持たない）として `nodes[]` に現れる。
- `budget.used_tokens_est ≤ max_tokens`。

---
## 11.4 カテゴリ C: 診断・変更影響分析

編集の前後でエージェントが「いま壊れている場所」と「これから壊れる場所」を、ビルドやテストランナーを起動せずに知るためのツール群。どちらも読み取り専用であり、ファイルを変更しない。

### T-08 `get_diagnostics`

**目的**: バックグラウンドの LSP と Tree-sitter が保持している最新の構文・型・Lint 診断を、シェルでビルドやリンターを走らせずに取得する。QuickFix が存在する診断には `apply_code_action`（T-13）で使う `code_action_id` を付ける。

**マイルストーン**: M1

**対応要求**: FR-DIAG-1, FR-FMT-1, FR-FMT-2, FR-FMT-3, FR-LANG-2, NFR-PERF-1, NFR-OPS-3

**入力**（`max_tokens` 既定 2000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `paths` | string[] | 任意 | なし（ワークスペース全体） | ファイルまたはディレクトリの相対パス。最大 50 件。ディレクトリは配下の索引済みファイルすべてに展開する |
| `min_severity` | enum | 任意 | `"warning"` | `"error"` \| `"warning"` \| `"information"` \| `"hint"`。指定以上の重大度だけ返す |
| `include_code_actions` | boolean | 任意 | `true` | 各診断に対する QuickFix 候補を問い合わせ、`code_action_id` を付ける |
| `sources` | string[] | 任意 | なし（全ソース） | 診断源で絞る（例 `["pyright"]`, `["tree-sitter"]`）。未知の名前は無視して `warnings` に記録する |

**処理規約**:

1. `paths` を §5.3 規則 5 で正規化する。省略時はワークスペースの索引済みファイル全体を対象とする。
2. 診断は 2 系統から集める。(a) Tree-sitter の構文木にある `ERROR` / `MISSING` ノードを `source: "tree-sitter"`, `severity: "error"`, `code: "syntax"` の診断に変換する。(b) 稼働中の LSP が `textDocument/publishDiagnostics` で通知した最新の診断（サーバーが保持するキャッシュ）。同一位置・同一メッセージの重複は (b) を優先して 1 件にする。
3. 対象言語の LSP が `ready` でない（`indexing` / `standby` / `unavailable`）場合、起動完了を待たずに (a) だけで応答し、`_resolution_meta.lsp_status` と `warnings` にその旨を書く（MUST）。LSP の起動は本ツールの呼び出しが契機になってよい（遅延起動、§12）。
4. `revision` が `"WORKING_TREE"` 以外のときは、M1 では `E_UNSUPPORTED`（`details.reason: "revision_not_supported_until_M2"`。実装で確定 2026-10-29。過去断面の構文木診断は M1 の範囲外）。M2 では (a) のみを返す（LSP は作業ツリーの状態しか持たない）。`lsp_status` は `"n/a"` とする。
5. `include_code_actions: true` のとき、`severity` が `error` または `warning` の診断ごとに LSP の `textDocument/codeAction`（`only: ["quickfix"]`）を問い合わせ、得られた各アクションに **セッション内で一意な不透明 ID**（例 `qf_7a1c`）を割り当てて `code_action_id` とする。ID とアクションの対応はサーバーが保持し、対象ファイルの `sha256` が変わった時点で無効化する（MUST）。実装は ID を `qf_` + 4 桁 16 進の連番（`qf_0001` から。例の `qf_7a1c` は形の見本）で発行し、クライアントは中身を解釈しない。問い合わせるのは、現在のページ（`cursor` 以降）の並びの先頭から数えて LSP 由来の `error` / `warning` の診断 100 件までで、それ以降の診断には `code_action_id` を付けない（実装で確定 2026-10-29）。
6. 並び順は重大度の高い順 → パス → 行。1 レコード = 1 診断。予算を超えたら残りをファイル別・重大度別の件数に要約し、`# summary:` 行と `next_cursor` を出す。
7. 各診断には、その位置を範囲に含む最も内側のシンボルの URN を `enclosing_urn` として付ける（シンボル外なら null）。
8. 本ツールは子プロセス（ビルドツール、テストランナー、リンターの CLI）を新たに起動してはならない（MUST NOT）。起動してよいのは LSP サーバーのみ。
9. `paths` を省略したワークスペース全体の呼び出しで LSP に開かせるファイルは 100 ファイルまで（`didOpen` の LRU 上限 200 の半分）。超えた分のファイルは構文エラーだけで応答し、`warnings` に書く（実装で確定 2026-10-29）。

**出力**

compact:

```
# get_diagnostics ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~260/2000tok
diagnostics: 3 in 2 files (1 E, 2 W; min_severity=warning)
E src/services/payment.py:58:16 [pyright:reportArgumentType] "str" は "Decimal" に割り当てできません  fix=qf_7a1c
W src/services/payment.py:9:1 [ruff:F401] `os` imported but unused  fix=qf_02b9
W src/api/routes.py:131:9 [pyright:reportUnusedVariable] 変数 "retry" は使われていません
```

json `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `diagnostics[]` | object[] | 1 件 = 1 診断 |
| `diagnostics[].path` | string | 相対パス |
| `diagnostics[].line`, `character` | integer | 開始位置（§5.10） |
| `diagnostics[].end_line`, `end_character` | integer | 終了位置 |
| `diagnostics[].severity` | enum | `error` \| `warning` \| `information` \| `hint` |
| `diagnostics[].source` | string | `tree-sitter` または LSP サーバー名（`pyright`, `ruff`, `tsc`, …） |
| `diagnostics[].code` | string \| null | 診断コード |
| `diagnostics[].message` | string | メッセージ原文 |
| `diagnostics[].enclosing_urn` | string \| null | 位置を含む最内シンボル |
| `diagnostics[].snippet` | string | 該当行のテキスト（200 文字で省略） |
| `diagnostics[].code_actions[]` | object[] | `{ id, title, kind }`。`include_code_actions: false` なら空配列 |
| `diagnostics[].confidence_level` | enum | `tree-sitter` 由来は `structural`、LSP 由来は `exact` |
| `summary` | object | `{ files, by_severity: {error, warning, information, hint}, by_source: {<source>: n} }`（切り詰め前の総数） |
| `lsp_sources[]` | object[] | `{ language, role, server, status, progress }`。対象ファイルの言語 × 役（`role`: `types` / `lint`）ごとの LSP 状態 |

**エラー**:

- `E_INVALID_ARGUMENT`: `paths` がルート外、`paths` が 50 件超、`min_severity` が列挙外。
- `E_WORKSPACE_NOT_CONFIGURED`: `workspace_id` 未登録、または `paths` からルートを解決できない。
- `E_REVISION_NOT_FOUND`: `revision` を解決できない。
- `E_UNSUPPORTED`: `revision` が `WORKING_TREE` 以外（M1。`details.reason: "revision_not_supported_until_M2"`）。
- `E_CURSOR_EXPIRED`: `cursor` 発行後に対象ファイルのいずれかが変更された。
- `E_TIMEOUT`: `include_code_actions` の問い合わせが合計 3,000 ms を超えた。得られた分は `data` に入れ、`details.partial: true`。

**精度と降格**:

| 水準 | 返すもの | 返せないもの |
|---|---|---|
| exact | LSP 診断 + Tree-sitter 構文エラー、QuickFix ID | — |
| structural | Tree-sitter 構文エラーのみ。`accuracy_warning` に「型・未定義参照・Lint の診断は含まれていない」と明記 | 型エラー、未解決インポート、QuickFix |
| heuristic / lexical_fallback | 診断なし（0 件）。該当ファイルは `summary.by_source` に `"n/a"` として件数を数えない | すべて |

ファイルごとに水準が異なるときは `_resolution_meta.mixed: true` とし、最低水準を `confidence_level` に書く。

**事後条件**:

1. 返したすべての診断の `severity` は `min_severity` 以上である。
2. 返したすべての診断について、`path` のファイルが存在し、`line` は 1 以上かつファイルの行数以下であり、`snippet` は応答時点のその行のテキストと一致する。
3. `include_code_actions: true` で返した各 `code_actions[].id` は、対象ファイルが変更されない限り、同一セッションで `apply_code_action` に渡して `E_TARGET_NOT_FOUND` にならない。
4. 呼び出しの前後で、ワークスペース内のどのファイルの `sha256` も変わらない。
5. 呼び出し中に LSP サーバー以外の子プロセスが起動されない。

### T-09 `analyze_change_impact`

**目的**: シンボルを変更・削除する前に、直接壊れる呼び出し元、推移的に影響を受けるモジュール、実行すべき最小のテスト、そして Git 履歴上いつも一緒に変更されてきたファイル（共変更結合）を 1 回で算出する。

**マイルストーン**: M2

**対応要求**: FR-DIAG-2, FR-NAV-3, FR-FMT-1, FR-FMT-2, FR-FMT-3, FR-FMT-4, FR-LANG-2

**入力**（`max_tokens` 既定 2500）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `target` | TargetRef | 必須 | — | 変更予定のシンボル（§5.3） |
| `change_kind` | enum | 任意 | `"signature_change"` | `"signature_change"` \| `"behavior_change"` \| `"deletion"` |
| `max_depth` | integer | 任意 | 3 | 推移的依存の追跡深度。1〜6 |
| `include_tests` | boolean | 任意 | `true` | `affected_test_targets` を算出する |
| `test_globs` | string[] | 任意 | サーバー既定（`**/tests/**`, `**/test_*.py`, `**/*_test.py`, `**/*.spec.*`, `**/*.test.*`） | テストファイルとみなすパターン。指定時は既定を置き換える |
| `include_co_change` | boolean | 任意 | `true` | Git 履歴から共変更結合を算出する |
| `min_coupling_ratio` | number | 任意 | 0.3 | `co_change_hotspots` に含める結合度の下限（0.0〜1.0） |

**処理規約**:

1. `target` を §5.3 で 1 件に解決する。
2. **直接破壊候補**: `relations` で `target_urn` が対象に一致する行を `change_kind` ごとに選ぶ。`signature_change` は `CALLS` / `REFERENCES` / `IMPLEMENTS` / `INHERITS`（呼び出し元と、オーバーライド・実装側）。`behavior_change` は `CALLS` のみ（型は壊れないが挙動が変わる箇所）。`deletion` は全関係種別（インポート文を含む）。各候補に理由コード（`reason`）を付ける。
3. **推移的依存**: 直接破壊候補の `source_urn` を起点に `relations` を逆向きに再帰走査（深さ `max_depth` まで）し、到達したシンボルを深さと経由シンボル（`via_urn`）付きで列挙する。同一シンボルは最小深さの 1 件だけ残す。
4. **影響テスト**: `include_tests: true` のとき、推移的依存（深さ 0 の直接候補を含む）のうち `test_globs` に一致するファイル内のシンボルを抽出し、テストランナーが受け付ける形の `test_id`（Python は `path::Class::func`、それ以外は `path::name`）に整形する。
5. **共変更ホットスポット**: `include_co_change: true` のとき、`git_co_changes` から対象シンボルおよびそのファイルを `entity_a` とする行を引き、`coupling_ratio >= min_coupling_ratio` のものを結合度の高い順に最大 10 件返す。`.git` が無い、または履歴の索引が未完了なら空配列とし `warnings` に理由を書く。
6. 予算は「直接破壊候補 → 影響テスト → 推移的依存 → 共変更」の優先順で配分する。各節は独立に切り詰め、切り詰めた節には件数の要約を残す。1 レコード = 直接破壊候補 1 件（ページネーションはこの節のみに適用し、他の節は 1 ページ目に要約込みで収める）。
7. `format: "dot"` / `"mermaid"` では、対象を根、直接候補・推移的依存をノード、`relations` の種別をエッジラベルとするグラフを出す。テストノードは属性 `test=true`、共変更は `CO_CHANGES` エッジに `ratio` を付ける。
8. 内部の処理上限は 2,000 ms とし、超えたら得られた分で `E_TIMEOUT`（`details.partial: true`）。

**出力**

compact:

```
# analyze_change_impact ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~610/2500tok
target: lk://app/src/services/payment.py#PaymentProcessor.process_charge  change=signature_change depth≤3
direct_breakage: 2 sites in 1 file
src/api/routes.py:88:20  [call] in create_order  receipt = await processor.process_charge(req)
src/api/routes.py:131:20  [call] in retry_order  receipt = await processor.process_charge(req)
transitive_dependents: 2 symbols in 2 files
  <- create_order @src/api/routes.py:80
    <- register_routes @src/api/__init__.py:12
affected_test_targets: 2
- tests/test_payment.py::test_charge_ok (depth 1)
- tests/test_api.py::test_create_order (depth 2)
co_change_hotspots: tests/test_payment.py 0.88 (11/13)  config/limits.yaml 0.62 (8/13)
```

json `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `target` | SymbolRecord | 解決した対象 |
| `change_kind` | enum | 入力値 |
| `direct_breakage_candidates[]` | object[] | `{ urn, path, line, character, use, enclosing_urn, snippet, reason, confidence_level }`。`reason` は `arity_or_type_mismatch_possible` \| `override_signature_mismatch` \| `behavior_dependency` \| `dangling_reference` |
| `transitive_dependents[]` | object[] | `{ urn, path, depth, via_urn }` |
| `affected_test_targets[]` | object[] | `{ test_id, urn, path, depth }` |
| `co_change_hotspots[]` | object[] | `{ entity, entity_kind: "file" \| "symbol", coupling_ratio, co_change_count, total_count, last_co_changed_sha }` |
| `summary` | object | `{ direct, transitive, tests, hotspots }`（切り詰め前の総数） |

**エラー**:

- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: `target` の解決失敗（§5.3）。
- `E_INVALID_ARGUMENT`: `max_depth` が 1〜6 の外、`change_kind` が列挙外、`min_coupling_ratio` が範囲外、`format` が非対応。
- `E_WORKSPACE_NOT_CONFIGURED`, `E_REVISION_NOT_FOUND`: 共通。
- `E_TIMEOUT`: 処理上限超過。部分結果を返す。
- `E_CURSOR_EXPIRED`: 続きの取得時に `relations` が更新されていた。

**精度と降格**:

| 水準 | 直接・推移的依存 | テスト | 共変更 |
|---|---|---|---|
| exact | LSP / SCIP の参照関係。インターフェース経由の呼び出しも含む | 同左から抽出 | Git 履歴があれば算出 |
| structural | Tree-sitter の名前一致による呼び出し候補。同名別シンボルの誤検知を含みうるため各行に `conf=structural` | 同左 | 同左 |
| heuristic / lexical_fallback | 内部の単語境界 grep による一致行を候補として列挙（`use` は `r`）。推移的依存は深さ 1 まで | grep 一致のうちテストファイル内のもの | 同左 |

共変更結合は Git 履歴だけに依存し、コード解析の水準とは独立である。混在時は `_resolution_meta.mixed: true`。

**事後条件**:

1. `direct_breakage_candidates[]` の各要素は、exact / structural では `relations` に `target_urn` = 対象、`source_urn` = `enclosing_urn` の行が存在し、heuristic / lexical_fallback では `snippet` が対象シンボル名を単語境界で含む。
2. `transitive_dependents[]` の各要素の `depth` は `max_depth` 以下であり、`via_urn` を辿って対象に至る `relations` の経路が存在する。
3. `affected_test_targets[]` の各要素の `path` は `test_globs` のいずれかに一致する。
4. `co_change_hotspots[]` はすべて `coupling_ratio >= min_coupling_ratio` を満たし、`coupling_ratio` の降順に並ぶ。
5. 呼び出しの前後で、ワークスペース内のどのファイルの `sha256` も変わらない。

---

## 11.5 カテゴリ D: 決定論的編集・リファクタリング

行番号や文字列一致に頼らず、AST ノード境界と LSP の `WorkspaceEdit` を単位にファイルを書き換えるツール群。4 ツールはすべて第 10 章の手順「解決 → 計画 → 前提検証 → 構文検証 → 原子的書き込み → 再索引 → 診断 → 応答」に従わなければならない（MUST）。本節では各ツールに固有の事項だけを書き、手順の細部は §10 を参照する。

### 11.5.0 カテゴリ D 共通事項

**共通入力**（各ツールの入力表には再掲しない）

| パラメータ | 型 | 既定 | 規約 |
|---|---|---|---|
| `dry_run` | boolean | ツール別 | `true` のときファイルを書かず、差分と（可能なら）診断予測だけを返す |
| `allow_heuristic_mutation` | boolean | `false` | 降格ファイル（`heuristic` / `lexical_fallback`）への実書き込みを許可する（§10.5） |
| `expected_file_sha256` | string | なし | 単一ファイル系（T-10, T-13）。指定時、対象ファイルの現在の SHA-256 と一致しなければ `E_PRECONDITION_FAILED` |
| `expected_file_sha256s` | object (path → sha256) | なし | 複数ファイル系（T-11, T-12）。列挙したファイルについて同上。列挙外のファイルは陳腐化検知（§10.4）のみ |
| `diagnostics_wait_ms` | integer | 2000 | 書き込み後に LSP の診断更新を待つ上限。0〜10000。0 のときは待たずに `new_diagnostics.status: "pending"` で応答する（§14.4 のレイテンシ測定が用いる）。書き込みをする呼び出しでは、編集前の診断（§10.7 の比較の基準）が出ていないサーバーを書き込みの前にも同じ上限まで待つ（別枠。出ていれば待たない） |

**共通規約**

1. `revision` が `"WORKING_TREE"` 以外なら、何もせず `E_INVALID_ARGUMENT`（`details.param: "revision"`）。
2. 対象ファイルの水準が `heuristic` または `lexical_fallback` で `allow_heuristic_mutation` が `true` でないとき、サーバーは `dry_run: true` として処理し、`applied: false` の結果に `warnings` でブロック理由（「降格ファイルのため dry_run を強制した。実書き込みには `allow_heuristic_mutation: true` が必要」）を書いて **成功応答** とする（MUST）。`E_MUTATION_BLOCKED` は、許可フラグがあっても書き込みを安全に確定できない場合（各ツールのエラー欄）に限って返す。
3. 書き込み後の診断差分は、編集したファイルと、その中で変更されたシンボルを参照している「直接の参照元ファイル」（`relations.source_urn` のファイル）に限る。LSP が `diagnostics_wait_ms` 内に該当ファイルの `publishDiagnostics` を返さなければ `new_diagnostics.status: "pending"` で応答し、エージェントは `get_diagnostics` で後追いする。編集の前にあるサーバーがいまの内容の診断を出していなければ（基準が不明）、そのサーバーの編集後の診断は `added[]` ではなく `present_after[]` に入れ、`status: "partial"` とする（§10.7。BUG-0098）。
4. `dry_run: true` のとき、サーバーは LSP への `didChange` を書き込みなしのオーバーレイとして送って診断を予測してもよい（MAY）。予測したときは `new_diagnostics.status: "complete"`、しなかったときは `"not_computed"` とし、オーバーレイは応答前に元に戻す。
5. 書き込みはワークスペース単位の書き込みロック（§5.9）の下で行う。

**共通出力**（json `data` の共通フィールド）

| フィールド | 型 | 意味 |
|---|---|---|
| `applied` | boolean | 実ファイルに書き込んだか。`dry_run`、ブロック、エラーでは `false` |
| `edit_id` | string \| null | 書き込みごとに発番する識別子（例 `ed_0007`）。`applied: false` なら null |
| `files_changed[]` | object[] | `{ path, sha256_before, sha256_after, hunks }`。`dry_run` では「書くはずのファイル」を `sha256_after` を予測値として列挙する |
| `diff` | string | unified diff。予算を超える分は末尾のハンクを省略し `# summary:` にハンク数を書く |
| `syntax_check` | enum | `"ok"`（適用後のすべての対象ファイルで新規の構文エラーノードがゼロ） \| `"skipped"`（Tier 4 など構文木が無い） |
| `new_diagnostics` | object | `{ status: "complete" \| "partial" \| "pending" \| "not_computed", added[], resolved[], present_after[], checked_files[] }`。`added[]` / `resolved[]` / `present_after[]` の要素は T-08 の診断オブジェクトと同形。`present_after[]` は基準が不明なサーバーの編集後の診断（増えたかどうか分からない。§10.7）。`checked_files[]` の要素は `{ path, fresh }` と、基準が不明なら `unknown_baseline: [サーバー名]`。compact の `new_diagnostics:` 行は `partial (+0 -1 ?2) checked=1 files`（`?N` が `present_after` の件数。0 なら書かない）で、`present_after:` の見出しの下に診断行を並べる |

compact では、これらを `applied:` 行、`files_changed:` 行、`syntax_check:` 行、`new_diagnostics:` 行とそれに続く `diag-line` で表す。差分は ```` ```diff ```` のコードブロックで出す。

### T-10 `edit_symbol_body`

**目的**: 関数・メソッド・クラス・型定義などのシンボルを単位として、AST ノード境界でコードを置換・挿入・削除し、同じ応答で構文妥当性と診断差分を返す。

**マイルストーン**: M1

**対応要求**: FR-EDIT-1, FR-EDIT-5, FR-EDIT-6, FR-EDIT-7, FR-LANG-3, FR-FMT-3, NFR-SAFE-1, NFR-SAFE-4, NFR-PERF-1

**入力**（`max_tokens` 既定 1500、`dry_run` 既定 `false`）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `target` | TargetRef | 必須 | — | 対象シンボル（§5.3）。`insert_member` では親となるクラス・構造体・モジュール |
| `operation` | enum | 必須 | — | `"replace_body"` \| `"insert_before"` \| `"insert_after"` \| `"insert_member"` \| `"delete_symbol"` |
| `new_code` | string | `delete_symbol` 以外で必須 | — | 置換・挿入するコード断片。インデントはサーバーが正規化する |
| `position` | enum | 任意 | `"end"` | `insert_member` のみ。`"start"` \| `"end"`（親の本体の先頭または末尾） |

**処理規約**:

1. `target` を §5.3 で 1 件に解決し、その **定義ノード** の範囲を取る。範囲は §10.8 の境界規則で決める（デコレータ・属性・`export` 等の修飾子と直前の Doc コメントを含み、空行で隔てられた通常コメントは含まない）。この範囲は T-05 が `context_lines: 0` で返す範囲と一致する。
2. `replace_body` は定義ノード全体を `new_code` で置き換える（名前やシグネチャの変更を含んでよい）。`insert_before` / `insert_after` は定義ノードの直前・直後に、同じ親の兄弟として挿入する。`insert_member` は `target` が本体ブロックを持つノード（クラス・構造体・enum・impl・モジュール）であることを要し、そうでなければ `E_INVALID_ARGUMENT`。`delete_symbol` は定義ノードを**行単位**で取り除き、**直前の連続空行**を一緒に取り除く（直前に空行が無い、つまりファイルや本体の先頭のメンバーのときだけ**直後**の連続空行を取り除く）。これで隣り合う定義の間の空行は削除前と同じ数のまま残る（実装で確定 2026-10-29、cc031cc。§10.8）。
3. **構文検証（書き込み前）**: `new_code` を共通先頭空白を除いた形で対象ファイルの文法で解析する。`insert_member` では親と同種のダミー容器で包んで解析する。`new_code` 単体に `ERROR` / `MISSING` ノードがあれば `E_SYNTAX_INVALID`。続いて編集をメモリ上で適用して再解析し、編集前に存在しなかった `ERROR` / `MISSING` ノードが 1 つでも生じれば `E_SYNTAX_INVALID`（書き込まない）。`delete_symbol` で親の本体が空になり構文エラーになる場合も同様で、サーバーは `pass` 等を補ってはならない（MUST NOT）。`details.hint` に理由を書く。Python で**親の本体の唯一の文**（コメントは文に数えない）である定義を消す場合は、書き込み前に `E_SYNTAX_INVALID`（`details.reason: "parent_body_would_be_empty"`）で拒否する（クラスや関数の唯一のメソッド・文を消す場合。親ごと消すか別の文を残す）。波括弧の言語は空の本体が妥当なので、そのまま消す。モジュール直下は空でもよい（cc031cc）。
4. **インデント正規化**: (a) 対象ファイルのインデント単位は、行頭空白の増分を集計して多数派（タブ or スペース、幅 2/4/8）を採る。集計できなければ言語既定（Python 4 スペース、JavaScript/TypeScript 2 スペース、Rust 4 スペース、Go タブ）。(b) `new_code` の共通先頭空白を除き、`new_code` 内のインデント単位を同じ方法で推定して対象ファイルの単位に変換する。`new_code` がタブとスペースを混在させていれば `E_INVALID_ARGUMENT`（`details.param: "new_code"`）。(c) 基準列は、`replace_body` と `insert_before` / `insert_after` では元の定義ノードの先頭行の列、`insert_member` では既存メンバの先頭行の列（メンバが無ければ親の列 + 1 単位）。(d) 改行コードは対象ファイルの多数派（CRLF / LF）に合わせ、BOM と末尾改行の有無は保持する。(e) 挿入時の空行は言語慣習に従う（Python: モジュール直下の関数・クラスの前後 2 行・クラス内 1 行、それ以外: 1 行）。**既存の空行を数えて、前後が合わせて `N` 行（`blank_lines_around`）になるよう足す**: `insert_before` は定義の行頭に `new_code` と空行 `N` 行を差し込み、直前に兄弟が居れば既存の空行と合わせて `N` 行にする（本体の先頭なら足さない）。`insert_after` は定義の行末に改行 + 空行 `N` 行 + `new_code` を差し込み（元の行の終端は後ろに残るので末尾改行の有無が変わらない）、直後に兄弟が居れば既存の空行と合わせて `N` 行にする。`insert_member` はメンバーの間を空行 1 行にし（`position: "end"` は最後のメンバーの直後、`"start"` は最初のメンバー〔Python の docstring があればその後ろ〕の前）、本体が空なら Python は `pass` / `...` だけの本体を置換、波括弧の言語は `{` の直後に差し込む（`class E {}` は閉じ括弧を次の行に送る）。
5. 前提検証・原子的書き込み・再索引・診断・応答は §10 の手順に従う。再索引後、置換・挿入したシンボルの新しい URN と範囲を `target_urn` / `new_range` に入れる。compact の `files_changed` の sha256 は先頭 12 桁 + `…` で示す（json の `sha256_before` / `sha256_after` は 64 桁。実装で確定 2026-10-29）。
6. **Tier 2・3 の対象（M2。§8.6 の B2 編集・B4 置換。M1 では Tier 2〜3 への編集は `E_UNSUPPORTED`）**: `.ipynb` は仮想 Python セル（§8）上で同じ操作を行い、元の JSON の他セル・`outputs`・`metadata` を変更せずに書き戻す。XML / JSON / YAML / TOML はセレクタ（`xpath:`, `jsonpath:`, `jq:`, `toml:`, `yaml:`）で選んだ要素・値に対して `replace_body` と `delete_symbol` を提供し、書き込み前にそのフォーマットのパーサで再解析できることを確認する。`insert_*` は Tier 3 では `E_UNSUPPORTED`。
7. 1 回の呼び出しで変更するファイルは常に 1 つである。

**出力**

compact:

```
# edit_symbol_body ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~240/1500tok
applied: true  edit_id=ed_0007  op=replace_body
target: lk://app/src/services/payment.py#PaymentProcessor._validate_limit @L117-142
files_changed: src/services/payment.py (+4 -2)  sha256 3f9c…→a71e…
syntax_check: ok
new_diagnostics: complete (+0 -1) checked=2 files
resolved:
W src/services/payment.py:120:9 [pyright:reportUnusedVariable] 変数 "tmp" は使われていません
```

json `data`（共通フィールドに加えて）:

| フィールド | 型 | 意味 |
|---|---|---|
| `operation` | enum | 入力値 |
| `target_urn` | string | 適用後の対象 URN（`delete_symbol` では削除前の URN） |
| `new_range` | object \| null | `{ start_line, end_line }`。適用後の定義ノード範囲。`delete_symbol` では null |
| `indent_unit` | string | 採用したインデント単位（例 `"4sp"`, `"tab"`） |

**エラー**:

- `E_INVALID_ARGUMENT`: `revision` が `WORKING_TREE` 以外、`operation` が列挙外、`new_code` 欠落、`insert_member` の対象が本体を持たない、`new_code` のタブ・スペース混在、`position` が列挙外、`target` がシンボルを指さない（`path` だけで `symbol_name` も `line` も無い。`details.param: "target"`）、`lexical_fallback` の対象に `allow_heuristic_mutation: true` で実書き込み（`dry_run: false`）をするのに `expected_file_sha256` が無い（`details.param: "expected_file_sha256"`、`details.reason: "required_for_lexical_fallback"`）。
- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: §5.3。
- `E_PRECONDITION_FAILED`: `expected_file_sha256` 不一致、または解決から書き込みまでの間に外部変更を検知（§10.4）。
- `E_SYNTAX_INVALID`: 処理規約 3 のいずれか。
- `E_MUTATION_BLOCKED`: `allow_heuristic_mutation: true` だが、`heuristic` / `lexical_fallback` の推定で定義ノードの終了行を一意に決められない（ブロック境界の候補が 2 つ以上）。
- `E_UNSUPPORTED`: Tier 3 への `insert_*`、または構文木もヒューリスティックも持たない対象。`details.reason` は、`no_symbol_range`（構文木か解決したシンボルが無い）、`insert_not_allowed_for_lexical_fallback`、`empty_body_unsupported`（波括弧の空の本体の `{` が見つからない）、**`definition_shares_line`**（`insert_before`、および `insert_member` で最初のメンバーの前に差し込むとき〔`position: "start"`〕に、基準にする定義が、同じ行の前に別のコードがあって行頭から始まらない。例: `class A { foo() {} }` の 1 行。行を割らずに差し込めないため。`delete_symbol` は先行する空白を含めて核の範囲だけを取り除き、結果の構文が壊れるなら構文検証が拒否する）（96866a5、実装で確定 2026-10-29）。
- `E_TIMEOUT`: 書き込みロックの待機が 5,000 ms を超えた。

**精度と降格**:

| 水準 | 範囲の根拠 | 書き込み |
|---|---|---|
| exact / structural | Tree-sitter の定義ノード | 通常どおり |
| heuristic | Ctags の開始行 + インデント／括弧対応による終了行推定 | `allow_heuristic_mutation: true` のときのみ。`syntax_check` は文法があれば行い、無ければ `"skipped"` |
| lexical_fallback | 単語境界 grep の一致行 + インデントブロック推定 | `allow_heuristic_mutation: true` **かつ** `expected_file_sha256` 指定のときのみ `replace_body` / `delete_symbol` を許す。`insert_*` は `E_UNSUPPORTED` |

**事後条件**:

1. `applied: true` のとき、対象ファイルの SHA-256 は `files_changed[0].sha256_after` に等しく、`files_changed[0].sha256_before` の内容に `diff` を適用した結果とバイト単位で一致する。
2. `applied: true` かつ `syntax_check: "ok"` のとき、適用後のファイルを解析した `ERROR` / `MISSING` ノードの集合は、適用前の集合の部分集合である。
3. `applied: true` かつ `operation` が `delete_symbol` 以外のとき、`resolve_symbols` に `target_urn` を渡すと 1 件に解決し、その範囲は `new_range` に等しい。`delete_symbol` のときは `E_TARGET_NOT_FOUND` になる。
4. `applied: false`（`dry_run`、ブロック、エラー）のとき、ワークスペース内のどのファイルの SHA-256 も呼び出し前と同じである。
5. 適用後のファイルの改行コードの多数派と BOM の有無は適用前と同じである。

### T-11 `rename_symbol`

**目的**: LSP の `textDocument/prepareRename` / `textDocument/rename` を用いて、定義とワークスペース内の全参照を一括で改名する。同名別スコープの識別子やコメント内の単語を巻き込まない。

**マイルストーン**: M1

**対応要求**: FR-EDIT-2, FR-EDIT-5, FR-EDIT-6, FR-EDIT-7, FR-FMT-3, NFR-SAFE-1, NFR-SAFE-4

**入力**（`max_tokens` 既定 1500、`dry_run` 既定 `false`）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `target` | TargetRef | 必須 | — | 改名するシンボル（§5.3）。`path` + `line` + `character` 指定も可 |
| `new_name` | string | 必須 | — | 新しい識別子。対象言語の識別子として Tree-sitter で解析できなければ `E_INVALID_ARGUMENT` |
| `scope_paths` | string[] | 任意 | なし | **降格モード（§精度と降格）でのみ** 有効。字句置換の対象をこのパス配下に限る。exact では無視して `warnings` に記録 |

**処理規約**:

1. `target` を §5.3 で 1 件に解決する。`new_name` が現在の名前と同じなら `E_INVALID_ARGUMENT`。
2. 対象言語の LSP が `ready` でなければ `E_LSP_UNAVAILABLE`（降格ファイルは「精度と降格」の規則に従う）。
3. `prepareRename` で改名可能性を確認し、`rename` で `WorkspaceEdit` を取得する。`WorkspaceEdit` が含むファイルの作成・削除・改名（resource operation）は、ファイル改名のみ支持し（`files_changed[].renamed_from` に記録）、作成・削除を含む場合は書き込み前に `E_UNSUPPORTED` とする。
4. `WorkspaceEdit` をファイルごとの編集に変換し、全ファイルに対して §10 の前提検証と構文検証を行う。1 ファイルでも失敗すれば **どのファイルも書かずに** エラーを返す（MUST）。
5. 全ファイルを §10 の原子的多ファイル書き込みで確定し、再索引後に `urn_map` を作る。`urn_map` には対象だけでなく、`qualified_name` が変わったすべてのシンボル（改名したクラスのメンバなど）の旧 URN → 新 URN を含める（MUST）。
6. 診断差分は変更した全ファイルを対象とし、`diagnostics_wait_ms` 内に揃わなければ `pending`。
7. 1 レコード = 変更箇所 1 件。予算を超えたら変更箇所はファイル別件数に要約し、`urn_map` と `new_diagnostics` は要約せずに優先して残す。

**出力**

compact:

```
# rename_symbol ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~300/1500tok
applied: true  edit_id=ed_0008  process_charge -> charge
files_changed: 3 files, 5 edits
src/services/payment.py:40:15  async def charge(self, req: PaymentRequest) -> Receipt:
src/api/routes.py:88:36  [call] in create_order  receipt = await processor.charge(req)
src/api/routes.py:131:36  [call] in retry_order  receipt = await processor.charge(req)
tests/test_payment.py:44:24  [call] in test_charge_ok  r = await p.charge(req)
urn_map: lk://app/src/services/payment.py#PaymentProcessor.process_charge -> lk://app/src/services/payment.py#PaymentProcessor.charge
syntax_check: ok
new_diagnostics: complete (+0 -0) checked=3 files
```

json `data`（共通フィールドに加えて）:

| フィールド | 型 | 意味 |
|---|---|---|
| `old_name`, `new_name` | string | 改名前後の識別子 |
| `edits[]` | object[] | `{ path, line, character, use, enclosing_urn, snippet_after }`。変更箇所 1 件ずつ |
| `edits_count` | integer | 切り詰め前の変更箇所総数 |
| `urn_map` | object | 旧 URN → 新 URN |
| `files_changed[].renamed_from` | string \| null | ファイル改名を伴った場合の旧パス |

**エラー**:

- `E_INVALID_ARGUMENT`: `revision` が `WORKING_TREE` 以外、`new_name` が識別子でない・予約語である・現在の名前と同じ。
- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: §5.3。
- `E_LSP_UNAVAILABLE`: 対象言語の LSP が起動できない・`ready` でない・`prepareRename` を拒否した（`details.reason`）。
- `E_PRECONDITION_FAILED`: `expected_file_sha256s` 不一致、または解決から書き込みまでの間に変更対象ファイルのいずれかが外部変更された。
- `E_SYNTAX_INVALID`: 適用後のいずれかのファイルに新規の構文エラーノードが生じる。
- `E_MUTATION_BLOCKED`: 降格モードで `allow_heuristic_mutation: true` だが、書き換え対象ファイルのいずれかに `new_name` が単語として既に出現する（置換後に区別不能になる）。
- `E_UNSUPPORTED`: Tier 3 の対象、または `WorkspaceEdit` がファイルの作成・削除を含む。
- `E_TIMEOUT`: 書き込みロックの待機、または LSP の `rename` 応答が 10,000 ms を超えた。

**精度と降格**:

| 水準 | 挙動 |
|---|---|
| exact | LSP の `WorkspaceEdit` に従う |
| structural | 参照が対象ファイル内に閉じるローカル変数・引数に限り、Tree-sitter のスコープ解析による同一ファイル内改名を提供してもよい（MAY）。それ以外は `E_LSP_UNAVAILABLE`。**M1 の実装**（実装で確定 2026-10-29、8dc7658、`tools/_t11_scope.py`）: Python の関数内の引数・ローカル変数（代入・拡張代入・`for` / `with` / `except` の `as`・`:=`・入れ子の `def` / `class` の名前）で、対象を `path` + `line` + `character` で指すときに限る。`scope_paths` は無視して `warnings` に書く。LSP が使えない（`standby` / `unavailable` / `indexing` / 時間切れ）ときの降格で、その関数（と入れ子）の中の同じ綴りの識別子を改名する。属性アクセスの属性名（`obj.name`）、キーワード引数の名前、import 文の中、同名を自分で束縛する入れ子の関数・ラムダ・内包表記の中は除く。`global` / `nonlocal` の宣言がある、import で束縛している、入れ子のクラスが同名をクラス直下で束縛している、モジュール直下・クラス直下・メンバーのときは対象にせず、元の `E_LSP_UNAVAILABLE` を返す（誤って改名するより断る）。`new_name` が関数の中（または外側の関数）で既に使われていれば `E_MUTATION_BLOCKED`。書き込みは通常どおり行い（`allow_heuristic_mutation` は要らない）、変更箇所は `conf=structural` |
| heuristic / lexical_fallback | 単語境界（`\b旧名\b`）の字句置換。対象は同じ拡張子のファイル（`scope_paths` 指定時はその配下）。`allow_heuristic_mutation` が無ければ `dry_run` 強制。各変更箇所に `conf=lexical_fallback` を付け、コメントや文字列内の一致も置換対象になることを `accuracy_warning` に明記する。**M1 の実装**（実装で確定 2026-10-29、8dc7658、`tools/_t11_degrade.py`）: 対象は文法の無いファイル（Tier 4、文法の無い Tier 1）で、LSP を使わない。単語境界は `(?<!\w)旧名(?!\w)`。`new_name` が書き換え対象のどこかに単語として既にあれば、`allow_heuristic_mutation: true` のとき `E_MUTATION_BLOCKED`（許可が無いときは強制 `dry_run` なので警告だけ）。**実書き込み**（`dry_run: false`）には `allow_heuristic_mutation: true` **と**、書き換える各ファイルの `expected_file_sha256s`（T-10 の `lexical_fallback` と同じ規則）が要る。Tier 3 と、文法の無い Tier 2 は `E_UNSUPPORTED` |

**事後条件**:

1. `applied: true` のとき、`files_changed[]` の各ファイルの SHA-256 は `sha256_after` に等しく、`files_changed[]` に無いファイルの SHA-256 は呼び出し前と同じである。
2. `applied: true` かつ exact のとき、`urn_map` の各キーは呼び出し前の `symbols` に存在し、各値は呼び出し後の `symbols` に存在し、キーは呼び出し後の `symbols` に存在しない。
3. `applied: true` かつ exact のとき、`edits_count` は、呼び出し前の LSP の参照件数 + 1（定義）に等しい。T-06 の件数とは一致しなくてよい（basedpyright の `rename` は `tests/` の呼び出しを含めないことがあり、構造的に数える T-06 の件数とずれる。19b の観察）。
4. `applied: false` のとき、ワークスペース内のどのファイルの SHA-256 も呼び出し前と同じである。

### T-12 `structural_search_replace`

**目的**: 正規表現では書けない構文木パターン（メタ変数 `$VAR`、`$$$ARGS` を含むコードテンプレート）でワークスペースを横断検索し、任意で一括書き換え（Codemod）を行う。

**マイルストーン**: M2

**対応要求**: FR-EDIT-3, FR-EDIT-5, FR-EDIT-7, FR-FMT-2, FR-FMT-3, NFR-SAFE-1, NFR-SAFE-4

**入力**（`max_tokens` 既定 2000、`dry_run` 既定 `true`）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `language` | string | 必須 | — | 対象言語 ID（`python`, `typescript`, `rust`, `go`, …）。文法を持たない言語は `E_UNSUPPORTED`（Tree-sitter の文法があっても ast-grep が対応しない `toml` / `xml` / `dtd` / `rst` / `sql` / `markdown_inline` を含む。S-3 で確認） |
| `pattern` | string | 必須 | — | ast-grep 互換のパターン。メタ変数は `$NAME`（単一ノード）、`$$$NAME`（0 個以上の連続ノード） |
| `rewrite` | string | 任意 | なし | 書き換えテンプレート。同じメタ変数を参照できる。省略時は検索のみ |
| `constraints` | object | 任意 | なし | メタ変数名 → 正規表現。一致したノードのテキストがその正規表現に合うものだけを採る（MAY） |
| `scope_paths` | string[] | 任意 | なし | 対象ディレクトリ・ファイル。省略時は `language` に属する索引済みファイル全体 |
| `max_matches` | integer | 任意 | 200 | 一致の上限。超えたら `warnings` に記録し、`rewrite` 指定時は書き込まずに `applied: false` とする |

**処理規約**:

1. `pattern`（および `rewrite`）を `language` の文法で解析できることを確かめる。解析できなければ `E_SYNTAX_INVALID`（`details.param: "pattern"` または `"rewrite"`）。ast-grep は不正なパターンでも例外にならず 0 件を返すことがあるので、一致が 0 件のときに限り、パターンが解析できない可能性を `warnings` に添えてよい（MAY。S-3 で確認）。
2. 対象ファイルを列挙し（除外設定 §12 を適用）、各ファイルの構文木に対してパターン照合を行う。1 レコード = 一致 1 件。一致は `path` → `line` → `character` の順に並べる。入れ子の一致（外側の式と内側の式の両方がパターンに合う場合）は外側だけを採り、重なる一致を作らない（MUST。ast-grep は両方を返すので `outermost` を通す。S-3 で確認）。
3. `rewrite` が無い場合、本ツールは読み取り専用である。`applied: false`、`files_changed: []` を返す。`allow_heuristic_mutation` と `expected_file_sha256s` は無視する。
4. `rewrite` がある場合、一致ごとに置換テキストを生成し、ファイル単位でまとめて §10 の前提検証と構文検証を行う。置換後のファイルに新規の `ERROR` / `MISSING` ノードが生じるファイルが 1 つでもあれば、**どのファイルも書かずに** `E_SYNTAX_INVALID`（`details.errors[]` に該当ファイルと行）。
5. `dry_run: false` のとき、全対象ファイルを §10 の原子的多ファイル書き込みで確定する。一致数が `max_matches` を超えるときは書き込まない。
6. 診断差分は変更した全ファイルが対象。変更ファイルが 20 を超えるときは `new_diagnostics.added[]` / `resolved[]` を各 20 件に制限し、残りを `checked_files[]` と件数で示す。
7. 本ツールが照合するのは文法を持つファイル（`structural` 以上）だけである。`heuristic` / `lexical_fallback` のファイルは対象から除外し、除外件数を `warnings` に書く。したがって `allow_heuristic_mutation` は受け付けるが効果を持たない。
8. `_resolution_meta.confidence_level` は `structural`、`is_degraded` は `false` とする（構文一致が本ツールの本来の精度であり、型による絞り込みは行わない旨を `engine_used: "ast-grep"` と `warnings` で示す）。

**出力**

compact:

````
# structural_search_replace ok conf=structural lsp=ready rev=WORKING_TREE ws=app ~420/2000tok
pattern: print($MSG)  rewrite: logger.info($MSG)  lang=python  dry_run=true
matches: 3 in 2 files
src/cli.py:12:5  print(f"loaded {n} rows")
src/cli.py:40:9  print(err)
src/tools/export.py:77:13  print("done")
applied: false  files_to_change: 2  syntax_check: ok
```diff
--- a/src/cli.py
+++ b/src/cli.py
@@ -12 +12 @@
-    print(f"loaded {n} rows")
+    logger.info(f"loaded {n} rows")
```
new_diagnostics: not_computed
````

json `data`（共通フィールドに加えて）:

| フィールド | 型 | 意味 |
|---|---|---|
| `language`, `pattern`, `rewrite` | string | 入力値 |
| `matches[]` | object[] | `{ path, line, character, end_line, end_character, matched, replacement, meta_vars: {<name>: text} }`。`replacement` は `rewrite` 無しなら null |
| `matches_count` | integer | 切り詰め前の一致総数 |
| `files_to_change` | integer | `rewrite` 指定時に変更対象となるファイル数 |

**エラー**:

- `E_INVALID_ARGUMENT`: `revision` が `WORKING_TREE` 以外、`max_matches` が 1 未満、`scope_paths` がルート外。
- `E_UNSUPPORTED`: `language` の文法を持たない。
- `E_SYNTAX_INVALID`: `pattern` / `rewrite` が解析できない、または置換後にいずれかのファイルに新規の構文エラーノードが生じる。
- `E_PRECONDITION_FAILED`: `expected_file_sha256s` 不一致、または書き込み直前に変更対象ファイルの外部変更を検知。
- `E_TIMEOUT`: 照合が 5,000 ms を超えた（部分結果を `data.matches[]` に入れ、`rewrite` 指定時は書き込まない）、または書き込みロックの待機超過。
- `E_CURSOR_EXPIRED`: 続きの取得時に対象ファイルのいずれかが変更された。

**精度と降格**:

| 水準 | 挙動 |
|---|---|
| exact / structural | 構文木パターン照合。型やスコープによる区別はしない（同名の別シンボルも一致する）。`conf=structural` |
| heuristic / lexical_fallback | 対象外（照合も書き換えもしない）。除外件数を `warnings` に書く |

**事後条件**:

1. `matches[]` の各要素の `matched` は、呼び出し時点でそのファイルの `line:character`〜`end_line:end_character` の範囲のテキストと一致する。
2. `applied: true` のとき、`files_changed[]` の各ファイルの SHA-256 は `sha256_after` に等しく、`files_changed[]` に無いファイルの SHA-256 は呼び出し前と同じである（全体が成功するか、何も書かれないかのいずれか）。
3. `applied: true` のとき、変更後の各ファイルに同じ `pattern` を照合した結果は、`replacement` 自体が `pattern` に一致する場合を除いて 0 件である。
4. `applied: true` かつ `syntax_check: "ok"` のとき、各変更ファイルの `ERROR` / `MISSING` ノードの集合は変更前の部分集合である。
5. `rewrite` 省略時、または `applied: false` のとき、ワークスペース内のどのファイルの SHA-256 も呼び出し前と同じである。

### T-13 `apply_code_action`

**目的**: `get_diagnostics` が提示した QuickFix、または指定位置で利用できる LSP Code Action（不足インポートの追加、未実装メソッドのスタブ生成、インポート整理、抽出リファクタリングなど）を適用し、解消した診断を同じ応答で返す。

**マイルストーン**: M1

**対応要求**: FR-EDIT-4, FR-EDIT-5, FR-EDIT-7, FR-DIAG-1, FR-FMT-3, NFR-SAFE-1, NFR-SAFE-4

**入力**（`max_tokens` 既定 1500、`dry_run` 既定 `false`）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `path` | string | 必須 | — | 対象ファイル |
| `line` | integer | 必須 | — | 対象行（1 始まり）。`action_kind_or_id` が ID のときも、ID の発行元の位置と一致しなければ `E_INVALID_ARGUMENT` |
| `character` | integer | 任意 | 行の最初の非空白文字 | 対象列 |
| `end_line` | integer | 任意 | `line` | 範囲を要するアクション（`refactor.extract` 等）の終了行 |
| `action_kind_or_id` | string | 必須 | — | `get_diagnostics` の `code_action_id`（`qf_` で始まる）、または LSP の CodeActionKind（`quickfix`, `source.organizeImports`, `source.fixAll`, `refactor.extract`, `refactor.inline`, `refactor.rewrite`, …） |

**処理規約**:

1. `path` を §5.3 規則 5 で正規化する。対象言語の LSP が `ready` でなければ `E_LSP_UNAVAILABLE`。
2. **ID 指定**: サーバーが保持する ID → アクションの対応を引く。未知の ID は `E_TARGET_NOT_FOUND`。ID 発行後に対象ファイルの SHA-256 が変わっていれば `E_PRECONDITION_FAILED`（`details.reason: "code_action_id_expired"`）。
3. **種別指定**: `textDocument/codeAction` を範囲 `line`〜`end_line`、`only: [種別]` で問い合わせる。候補 0 件は `E_TARGET_NOT_FOUND`、2 件以上は `E_TARGET_AMBIGUOUS` とし、`candidates[]` に新たに発番した `{ id, title, kind }` を入れる（エージェントはその ID で呼び直す）。`quickfix` は範囲に重なる診断を `context.diagnostics` で渡すため、問い合わせの前に各サーバーがいまの内容の診断を出すのを最大 2,000 ms 待つ。**待っても出さなかったサーバーがあり、候補が 1 件以下なら** `E_TARGET_NOT_FOUND` でも 1 件の自動適用でもなく `E_TIMEOUT`（`details.reason: "diagnostics_pending"`、`details.server`・`servers`、`details.retry_after_ms`。1 件あれば ID を付けて `candidates[]` に入れる。何も適用しない）とする。2 件以上なら `E_TARGET_AMBIGUOUS` のまま、`details.diagnostics_pending` に遅れたサーバーを書く。理由: 診断の無いサーバーは QuickFix を出さないので、その時点の 0 件・1 件は「無い」「これだけ」と確かめられていない。確定の形で答えると、呼び手の AI は直せる箇所を諦めるか、別のサーバーの抑止コメントだけを適用される（BUG-0099）。
4. アクションに `edit` が無ければ `codeAction/resolve` で補う。それでも `edit` が無く `command` だけを持つアクションは `E_UNSUPPORTED`（サーバー側で任意コマンドを実行しない。MUST NOT）。
5. `WorkspaceEdit` を T-11 と同じ規則でファイル別編集に変換し（作成・削除を含めば `E_UNSUPPORTED`）、§10 の前提検証・構文検証・原子的書き込み・再索引・診断を行う。
6. `new_diagnostics.resolved[]` には、ID が付いていた診断が含まれるかどうかを `data.target_diagnostic_resolved` で明示する。
7. 1 回の呼び出しで適用するアクションは 1 つである。

**出力**

compact:

```
# apply_code_action ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~200/1500tok
applied: true  edit_id=ed_0009  action=qf_7a1c kind=quickfix "Add import 'Decimal' from 'decimal'"
files_changed: src/services/payment.py (+1 -0)
syntax_check: ok
new_diagnostics: complete (+0 -1) checked=1 files  target_resolved=true
resolved:
E src/services/payment.py:58:16 [pyright:reportUndefinedVariable] "Decimal" が定義されていません
```

json `data`（共通フィールドに加えて）:

| フィールド | 型 | 意味 |
|---|---|---|
| `action` | object | `{ id, title, kind, source }`。`source` は提供した LSP サーバー名 |
| `target_diagnostic_resolved` | boolean \| null | ID 指定のとき、その ID が付いていた診断が `resolved[]` に含まれるか。種別指定では null |

**エラー**:

- `E_INVALID_ARGUMENT`: `revision` が `WORKING_TREE` 以外、`line` がファイルの行数の外、`end_line < line`、ID の発行位置と `path` / `line` が一致しない。
- `E_LSP_UNAVAILABLE`: 対象言語の LSP が起動できない・`ready` でない・Code Action 機能を公開していない。
- `E_TARGET_NOT_FOUND`: 未知の ID、または種別に一致するアクションが 0 件（`quickfix` は、問い合わせたすべてのサーバーがいまの内容の診断を出したうえで 0 件のときだけ）。
- `E_TARGET_AMBIGUOUS`: 種別指定で候補が 2 件以上（`quickfix` で診断がまだのサーバーがあれば `details.diagnostics_pending`）。
- `E_PRECONDITION_FAILED`: ID の発行後にファイルが変更された、`expected_file_sha256` 不一致、書き込み直前の外部変更検知。
- `E_SYNTAX_INVALID`: 適用後のいずれかのファイルに新規の構文エラーノードが生じる。
- `E_UNSUPPORTED`: `command` のみのアクション、またはファイル作成・削除を含む `WorkspaceEdit`。
- `E_TIMEOUT`: `codeAction` / `resolve` の応答が 5,000 ms を超えた、書き込みロックの待機超過、または種別 `quickfix` で文脈の診断が間に合わず候補が 1 件以下（`details.reason: "diagnostics_pending"`。処理規約 3。`details.retry_after_ms` の後に再試行する）。

**精度と降格**:

| 水準 | 挙動 |
|---|---|
| exact | LSP の Code Action に従う |
| structural / heuristic / lexical_fallback | 提供しない。`E_LSP_UNAVAILABLE`。`allow_heuristic_mutation` は受け付けるが効果を持たない（Tree-sitter だけで安全に実行できる組み込みアクションの提供は未決事項、§17） |

**事後条件**:

1. `applied: true` のとき、`files_changed[]` の各ファイルの SHA-256 は `sha256_after` に等しく、`files_changed[]` に無いファイルの SHA-256 は呼び出し前と同じである。
2. `applied: true` のとき、`diff` は LSP が返した `WorkspaceEdit` をファイルに適用した結果と等しい（サーバーは編集内容を加工しない。ただしインデント・改行コードは対象ファイルの規約に正規化してよい）。
3. `applied: true` かつ ID 指定かつ `new_diagnostics.status: "complete"` のとき、`target_diagnostic_resolved` は、その ID が付いていた診断が直後の `get_diagnostics` に現れないことと一致する。
4. `applied: false` のとき、ワークスペース内のどのファイルの SHA-256 も呼び出し前と同じである。

---
## 11.6 カテゴリ E: 仕様抽出・データフロー・論理検証

カテゴリ E は Verification Path（第 4 章）に属する。応答時間の目標は NFR-PERF-2（p95 500 ms 未満）であり、Fast Path の目標は適用しない。本カテゴリのツールは **応答生成の過程で LLM を呼ばない**（MUST）。自然言語で書かれた仕様を論理式に直す役目はホスト側のコーディングエージェントが負い、Lucianke は決定論的な抽出と判定だけを行う。

### T-14 `find_code_paths`

**目的**: 起点シンボル（source）から終点シンボル（sink）へ至る制御フロー経路またはデータフロー経路を列挙する。未サニタイズ入力の到達確認や、状態変数がどの経路で書き換えられるかの追跡に用いる。

**マイルストーン**: M2

**対応要求**: FR-VER-1, FR-DIAG-2, FR-FMT-2, FR-FMT-3, FR-FMT-4, FR-LANG-2, NFR-PERF-2

**入力**（`max_tokens` の既定は 2500）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `source` | object | MUST | — | §5.3 の TargetRef に `variable_name`（string, 任意）を加えたもの。`variable_name` はそのシンボルの引数名またはローカル変数名 |
| `sink` | object | MUST | — | 同上 |
| `flow_type` | enum | MAY | `"both"` | `"control"` \| `"data"` \| `"both"` |
| `max_hops` | integer | MAY | 8 | 1〜20。範囲外は `E_INVALID_ARGUMENT` |
| `max_paths` | integer | MAY | 20 | 1〜200。これを超える経路は `page.total_estimate` に数だけ入れる |
| `required_intermediate_filter` | string | MAY | なし | シンボル名。`name` なら「経路上に `name` を含む経路のみ」、`!name` なら「含まない経路のみ」 |
| `include_guards` | boolean | MAY | true | 各ステップを囲む分岐条件（`when:`）を AST から付記するか |
| `timeout_ms` | integer | MAY | 500 | 50〜5000 |

**処理規約**

1. `source` と `sink` を §5.3 の規則で解決する。どちらかが `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS` なら経路探索に入らない。
2. `flow_type: "control"` は `relations`（`relation_kind = 'CALLS'`）の推移閉包を `source` から `sink` へ向けて辿る。`flow_type: "data"` は `dataflows` と、`relations` の呼び出しにおける実引数→仮引数の対応を辿る。`"both"` は両者の和集合を返し、各経路に `flow_kind` を付ける。
3. 探索エンジンは SQLite の再帰 CTE、または組み込み Datalog（候補）とする。どちらを使うかは規範としない。`max_hops` に達した経路は打ち切る。
4. `variable_name` を省いたデータフロー探索は、`source` の全引数と `sink` の全引数・戻り値を対象にする。
5. `required_intermediate_filter` は経路確定後に適用する。`!name` のとき、返す経路のどのステップも `qualified_name` が `name` に一致しない（末尾一致）。
6. 経路はホップ数の昇順、同数なら各ステップのパスと行番号の辞書順で安定ソートする（MUST）。同一入力・同一 DB 内容で返る順序が変わってはならない。
7. 1 レコードは 1 経路。予算を超えたら §5.6 に従って `next_cursor` を発行し、`# summary:` に残り経路数とホップ数の分布を書く。
8. 経路の `confidence_level` は、その経路を構成する `relations` / `dataflows` 行の最低水準とする。応答全体の水準は返した経路の最低水準。
9. `timeout_ms` を超えたら、それまでに確定した経路を `data.paths` に入れて `E_TIMEOUT`（`details.partial: true`）を返す。

**出力**

compact の例:

```
# find_code_paths ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~640/2500tok
source: lk://app/src/api/routes.py#create_order  var=payload
sink: lk://app/src/db/repo.py#OrderRepository.execute_raw  var=sql
query: flow=data hops<=8 filter=!sanitize_sql  paths: 2
path 1 (3 hops)
  1. create_order @src/api/routes.py:84  var=payload
  2. build_query @src/services/order.py:40  var=req.note  when: req.note is not None
  3. OrderRepository.execute_raw @src/db/repo.py:112  var=sql
path 2 (4 hops)
  1. create_order @src/api/routes.py:91  var=payload  when: retry
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `source` | object | 解決後の `{urn, variable_name}` |
| `sink` | object | 同上 |
| `flow_type` | string | 入力と同じ |
| `filter_applied` | string \| null | 適用したフィルタ |
| `engine` | string | 使用した探索エンジンの識別子 |
| `paths[]` | array | 経路。各要素は `{path_id, flow_kind, hops, confidence_level, steps[]}` |
| `paths[].steps[]` | array | `{order, urn, path, line, variable, guard}`。`guard` は `include_guards: false` なら null |
| `paths_total_estimate` | integer | `max_paths` で打ち切る前に見つかった経路数 |

**エラー**

- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: `source` または `sink` の解決に失敗。`details.which` に `"source"` / `"sink"`。
- `E_INVALID_ARGUMENT`: `max_hops`・`max_paths`・`timeout_ms` が範囲外。`variable_name` が対象シンボルに存在しない（`details.known_variables[]`）。
- `E_UNSUPPORTED`: Tier 3（データファイル）または Tier 4 の対象に `flow_type: "data"` を指定した。
- `E_TIMEOUT`: 探索が `timeout_ms` を超えた。部分結果を `data.paths` に含む。
- `E_REVISION_NOT_FOUND` / `E_WORKSPACE_NOT_CONFIGURED` / `E_CURSOR_EXPIRED`: §5.8 のとおり。

**精度と降格**

| 水準 | 制御フロー | データフロー |
|---|---|---|
| `exact` | LSP の呼び出し階層から得た `relations` を辿る。多態呼び出しは実装クラスへ展開 | `dataflows`（Def-Use 連鎖）と引数対応を辿る。手続き間も可 |
| `structural` | Tree-sitter の呼び出し式と import 解決で推定した `relations` を辿る。多態は展開しない | 手続き内（同一関数内）の Def-Use のみ。関数をまたぐ経路は返さず `warnings` に理由 |
| `heuristic` | ctags のシンボル名一致で呼び出し候補を辿る。誤検知を含みうる旨を `accuracy_warning` に書く | 提供しない。`flow_type: "data"` は経路 0 件 + `warnings`、`"both"` は制御フローのみ |
| `lexical_fallback` | 呼び出し先名の単語境界一致で辿る。1 ホップごとに候補が増えるため `max_hops` を 3 に自動で下げ、`warnings` に記す | 提供しない |

**事後条件**

1. 返した各経路の最初のステップの `urn` は解決後の `source.urn` と一致し、最後のステップの `urn` は `sink.urn` と一致する。
2. 返した各経路の `hops` は `max_hops` 以下であり、`steps[]` の要素数は `hops` と一致する。
3. 水準が `exact` または `structural` の経路では、連続する 2 ステップごとに `relations`（`CALLS`）または `dataflows` に対応する行が 1 行以上存在する。
4. `required_intermediate_filter` が `!name` のとき、返した経路のどのステップの `qualified_name` も `name` に末尾一致しない。
5. 同一の入力を同一の DB 内容に対して 2 回実行したとき、`data.paths` のバイト列は同一である。

---

### T-15 `extract_code_contracts`

**目的**: 対象ファイルまたはシンボルから、型シグネチャ・ガード節・構造化 Doc コメント・OpenAPI/JSON Schema・Gherkin に書かれた契約（事前条件・事後条件・不変条件・例外）を決定論的に抽出し、人間可読の式と SMT-LIB 式の両方で返す。

**マイルストーン**: M3

**対応要求**: FR-VER-2, FR-LANG-1, FR-LANG-3, FR-FMT-2, FR-FMT-3, NFR-PERF-2

**入力**（`max_tokens` の既定は 2000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `path` | string | MUST | — | ソースファイル、OpenAPI（YAML/JSON）、JSON Schema、Gherkin `.feature`、または Markdown（埋め込みの OpenAPI/Gherkin ブロックのみ対象） |
| `symbol_name` | string | MAY | なし | ソースファイルで特定シンボルに絞るとき。§5.3 の規則で解決 |
| `sources` | array of enum | MAY | 全部 | `"TYPE"` \| `"GUARD"` \| `"DOCSTRING"` \| `"OPENAPI"` \| `"GHERKIN"` のうち抽出する系統 |
| `include_smt` | boolean | MAY | true | SMT-LIB v2 式を生成するか |
| `link_external_specs` | boolean | MAY | true | ソースファイルに対応する OpenAPI / Gherkin をワークスペース内から探し、`Endpoint` 単位で結び付けるか |

**処理規約**

1. `path` の種別を `files.tier` と拡張子で判定する。ソースファイルなら `symbol_name` の有無に応じてファイル内の全シンボルまたは 1 シンボルを対象にする。
2. `TYPE`: 引数の型（`Optional` / `Union` / `Literal` / 数値型 / `Result` / `Option` 等）を `PRE`、戻り値型を `POST` に変換する。型エイリアスの展開は `exact` 水準でのみ行う。
3. `GUARD`: 関数本体の先頭から連続する「条件 + raise/throw/return/assert」文を `PRE`（条件の否定）と `THROWS` に変換する。最初のガードでない文が現れた時点で走査を止める。`assert` は位置を問わず `INVARIANT` として拾う。
4. `DOCSTRING`: 構造化タグ（Python Docstring の `Args:` / `Returns:` / `Raises:`、JSDoc の `@param` / `@returns` / `@throws`、Rustdoc の `# Panics` / `# Errors`、および `@requires` / `@ensures`）だけを対象とする。タグ本文のうち、数値範囲や比較の定型パターン（例: `0以上150以下`, `must be > 0`, `non-empty`）に一致する部分だけを式に変換し、`parsed_from_text: true` を付ける。一致しない本文は `expression` に原文を入れ、`smt_lib` を null、`smt_reason` を `"unparsed_text"` とする。
5. `OPENAPI`: `minimum` / `maximum` / `exclusiveMinimum` / `minLength` / `maxLength` / `pattern` / `enum` / `required` / `nullable` を、リクエスト側は `PRE`、レスポンス側は `POST` に変換する。対象は `Endpoint` / `Message` 種のシンボル。
6. `GHERKIN`: `Given` を `PRE`、`Then` を `POST`、`When` は対象シンボルの特定（`link_external_specs` が true のとき、ステップ文中の関数名・エンドポイント文字列で `symbols` を引く）にだけ使う。式はデータテーブルの数値列に限って生成し、それ以外は原文のまま。
7. 抽出した契約は `contracts` テーブルへ upsert する（`contract_id` は `target_urn + contract_type + source_kind + 正規化した expression` の SHA-256 先頭 16 桁）。既存行の `last_verify_status` は変更しない。
8. 1 レコードは 1 契約。並び順は対象シンボルの開始行、次に `contract_type`（PRE → POST → INVARIANT → THROWS）、次に `source_kind`。
9. `revision` 指定時はその断面のファイルから抽出し、DB には書き込まない。

**出力**

compact の例:

```
# extract_code_contracts ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~380/2000tok
=== src/pricing.py (88 lines) ===
[Function] apply_discount(price: Decimal, rate: int) -> Decimal @L12-30
- PRE [TYPE] rate: int
- PRE [GUARD] rate >= 0 and rate <= 100  @L14
- PRE [DOCSTRING] price > 0
- POST [DOCSTRING] return >= 0
- THROWS [GUARD] ValueError when rate < 0 or rate > 100  @L14
- INVARIANT [DOCSTRING] "割引額は端数を切り捨てる"  smt=null (unparsed_text)
# summary: contracts 6, smt generated 5/6
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `target` | object | `{path, urn}`。ファイル全体なら `urn` はファイル URN |
| `contracts[]` | array | 契約。各要素は下表 |
| `summary` | object | `{total, by_contract_type, by_source_kind, smt_generated}` |
| `linked_specs[]` | array | `link_external_specs` で結び付けた外部仕様の `{path, kind, matched_symbols}` |

`contracts[]` の要素:

| フィールド | 型 | 意味 |
|---|---|---|
| `contract_id` | string | 処理規約 7 の決定論的 ID |
| `target_urn` | string | 契約が属するシンボル |
| `contract_type` | enum | `PRE` \| `POST` \| `INVARIANT` \| `THROWS` |
| `source_kind` | enum | `TYPE` \| `GUARD` \| `DOCSTRING` \| `OPENAPI` \| `GHERKIN` |
| `expression` | string | T-16 の制約式文法（§11.6 T-16）に従う人間可読の式。変換できなかった原文はそのまま |
| `parsed_from_text` | boolean | 自然文の定型パターンから式を得たとき true |
| `smt_lib` | string \| null | SMT-LIB v2 の `(assert ...)` 式 |
| `smt_reason` | string \| null | `smt_lib` が null のときの理由（`unparsed_text`, `unsupported_type`, `nonlinear`, `include_smt_false`） |
| `location` | object | `{path, line}`。`TYPE` はシグネチャ行、`GUARD` はガード文の行、`DOCSTRING` はタグ行、外部仕様はその仕様ファイルの行 |
| `confidence_level` | enum | §5.5 |

**エラー**

- `E_TARGET_NOT_FOUND`: `path` が存在しない、または `symbol_name` が 0 件。
- `E_TARGET_AMBIGUOUS`: `symbol_name` が 2 件以上。
- `E_INVALID_ARGUMENT`: `sources` に未知の値。
- `E_UNSUPPORTED`: 対象が Tier 4、バイナリ、または OpenAPI / Gherkin として解析できない仕様ファイル（`details.reason` に解析エラー位置）。
- `E_REVISION_NOT_FOUND` / `E_WORKSPACE_NOT_CONFIGURED` / `E_CURSOR_EXPIRED`: §5.8 のとおり。

**精度と降格**

| 水準 | 挙動 |
|---|---|
| `exact` | 5 系統すべて。型エイリアス・ジェネリクスは LSP で展開してから変換する。ガード式中の名前はスコープ解決済み |
| `structural` | 5 系統すべて。型は書かれたままの文字列で扱い、未知の型名は `smt_reason: "unsupported_type"`。ガード式は AST から取るが名前解決はしない |
| `heuristic` | `DOCSTRING` / `OPENAPI` / `GHERKIN` のみ。`TYPE` / `GUARD` は返さず `warnings` に理由。`smt_lib` はすべて null |
| `lexical_fallback` | ソースファイルは `E_UNSUPPORTED`。OpenAPI / Gherkin は専用パーサで扱うため、この水準には落ちない |

**事後条件**

1. 応答生成の過程で外部 LLM API を呼ばない。サーバーのログに LLM 呼び出しの記録が無く、処理中にワークスペース外へのネットワーク送信が発生しない。
2. 同一のファイル内容と同一の入力に対する 2 回の呼び出しは、`contract_id` を含めて同一の `contracts[]` を返す。
3. `smt_lib` が非 null の各契約は、SMT-LIB v2 のパーサで構文エラーなく読め、先頭の S 式が `assert` である。
4. `source_kind: "GUARD"` の各契約の `location.line` は、`target_urn` のシンボルの `start_line` 以上 `end_line` 以下である。
5. `revision` 省略時、返した契約数は `contracts` テーブルで `target_urn` が一致する行数と等しい。

---

### T-16 `verify_contract`

**目的**: 対象関数の実装（AST から得た SSA 形式の制御フローと演算式）が、事前条件のもとで事後条件・不変条件・暗黙の安全性を満たすかを SMT ソルバで判定し、違反があれば具体的な反例入力を返す。

**マイルストーン**: M3

**対応要求**: FR-VER-3, FR-VER-2, FR-FMT-2, FR-FMT-3, NFR-PERF-2

**入力**（`max_tokens` の既定は 2000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `target` | object | MUST | — | §5.3 の TargetRef。関数・メソッドに限る |
| `postconditions` | array of string | MUST | — | 1 件以上。下記の制約式文法に従う |
| `preconditions` | array of string | MAY | なし | 省略時は T-15 の `PRE` 契約を自動適用する |
| `invariants` | array of string | MAY | なし | ループ不変条件・クラス不変条件 |
| `check_implicit_safety` | boolean | MAY | true | ゼロ除算・配列境界外・Null 参照・整数オーバーフローを同時に検査する |
| `loop_unroll_depth` | integer | MAY | 3 | 0〜10 |
| `timeout_ms` | integer | MAY | 500 | 50〜5000。ソルバに与える上限 |
| `assume_callees` | object | MAY | なし | 呼び出し先の名前または URN → 事後条件式の配列。未モデル呼び出しに仮定を与える |

制約式の文法（`preconditions` / `postconditions` / `invariants` / `assume_callees` の値、および T-15 の `expression`）:

```ebnf
expr     = or-expr ;
or-expr  = and-expr , { ( "||" | "or" ) , and-expr } ;
and-expr = not-expr , { ( "&&" | "and" ) , not-expr } ;
not-expr = [ "!" | "not" ] , cmp ;
cmp      = arith , [ ( "==" | "!=" | "<" | "<=" | ">" | ">=" | "implies" ) , arith ] ;
arith    = term , { ( "+" | "-" ) , term } ;
term     = unary , { ( "*" | "/" | "%" ) , unary } ;
unary    = [ "-" ] , primary ;
primary  = number | string | "true" | "false" | "null" | "return"
         | "old(" , ident , ")" | "len(" , ident , ")" | "is_null(" , ident , ")"
         | ident , [ "(" , [ expr , { "," , expr } ] , ")" ] | "(" , expr , ")" ;
ident    = name , { "." , name } ;
```

`return` は戻り値、`old(x)` は関数入口での `x` の値、`len(x)` は列・文字列の長さ、`is_null(x)` は Null 判定を表す。自然言語で書かれた仕様をこの文法の式に直すのは **ホスト側エージェントの責務** であり、Lucianke は式を受け取るだけである。

**処理規約**

1. `target` を §5.3 で解決する。対象が関数・メソッドでなければ `E_INVALID_ARGUMENT`。Tier 1 以外は `E_UNSUPPORTED`（Cython は MAY）。
2. 入力の各式を上記文法で構文解析する。1 つでも失敗したら `E_INVALID_ARGUMENT`（`details.errors[] {field, index, message}`）。式中の `ident` が対象関数の引数・戻り値・ローカル変数・`self` 属性のいずれにも一致しなければ同じく `E_INVALID_ARGUMENT`。
3. `preconditions` 省略時は T-15 を同じ `target` で実行し、`contract_type: PRE` の契約のうち `smt_lib` が非 null のものを採用する。採用した契約は応答の `preconditions_used[]` に `origin: "contracts"` として列挙する。
4. 関数本体を SSA 形式に変換する。対象とする構文は代入・算術・比較・論理・`if` / `match` / 三項・`return` / `raise` / `for` / `while`・ローカル呼び出し。`for` / `while` は `loop_unroll_depth` 回まで展開する。
5. 呼び出し先は、`assume_callees` に仮定があればその事後条件を使い、無ければ未解釈関数として扱い `unmodeled_calls[]` に記録する。再帰呼び出しも未解釈関数として扱う。
6. `check_implicit_safety` が true のとき、除算・剰余のたびに「除数 ≠ 0」、添字アクセスのたびに「0 ≤ i < len」、属性アクセスのたびに「非 Null」、固定幅整数の演算のたびに「型の範囲内」を検査項目に加える。Python の `int` は任意精度のためオーバーフロー検査の対象外とし、その旨を `implicit_checks[]` に `skipped` で残す。
7. 判定: 「事前条件 ∧ 実装の経路条件 ∧ ¬（事後条件 ∧ 暗黙検査）」の充足可能性をソルバに問う。充足なら `VIOLATED`、充足不能かつループ無しなら `VERIFIED`、充足不能かつループ有りなら `BOUNDED_VERIFIED`、`timeout_ms` 到達または非線形・未対応演算で判定不能なら `UNKNOWN_OR_TIMEOUT`。いずれも `ok: true` で返す（ソルバのタイムアウトはエラーではなく判定結果）。
8. `VIOLATED` のとき、ソルバのモデルから引数と `self` 属性の値を `counterexample.inputs` に、経路上の各分岐の行・真偽・その時点の変数値を `execution_path_trace[]` に書く。
9. ソルバの乱数シードは固定し（MUST）、同一入力で同一の `status` を返す。反例の具体値はソルバの実装に依存するため同一性を要求しない。
10. `preconditions` を自動適用した場合に限り、採用した契約の `contracts.last_verify_status` を `status` で更新する。エージェントが与えた式は DB に保存しない。
11. 本ツールはページネーションしない。`execution_path_trace[]` が予算を超えるときは末尾を省き、`# summary:` に省略ステップ数を書く。
12. ツール全体の実行時間（抽出・SSA 変換・ソルバ）が `timeout_ms + 2000 ms` を超えたら `E_TIMEOUT`。

**出力**

compact の例:

```
# verify_contract ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~420/2000tok
target: lk://app/src/pricing.py#apply_discount  status: VIOLATED  solver=smt 118ms unroll=3
pre: rate >= 0 && rate <= 100 ; price > 0  (2 from contracts)
post: return >= 0 ; return <= price  (2 given)
violated: implicit_safety.division_by_zero @L54
counterexample: price=100 rate=100
trace:
- 1. @L50 if rate > 0 -> true  (rate=100)
- 2. @L54 factor = 100 / (100 - rate)  divisor=0
unmodeled_calls: [lk://app/src/pricing.py#round_half_up]
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `target` | object | `{urn, path, signature}` |
| `status` | enum | `VERIFIED` \| `VIOLATED` \| `BOUNDED_VERIFIED` \| `UNKNOWN_OR_TIMEOUT` |
| `solver` | object | `{engine, time_ms, timeout_ms, loop_unroll_depth, loops_found}` |
| `preconditions_used[]` | array | `{expression, origin: "given" \| "contracts", contract_id}` |
| `postconditions[]` | array | `{expression, result: "holds" \| "violated" \| "unknown"}` |
| `invariants[]` | array | 同上 |
| `implicit_checks[]` | array | `{kind: "division_by_zero" \| "index_out_of_bounds" \| "null_dereference" \| "integer_overflow", line, result: "holds" \| "violated" \| "unknown" \| "skipped"}` |
| `counterexample` | object \| null | `{inputs: {名前: 値}, state: {self.属性: 値}}`。`VIOLATED` 以外は null |
| `execution_path_trace[]` | array | `{step, line, kind: "branch" \| "assign" \| "call" \| "return", text, values}` |
| `unmodeled_calls[]` | array | `{urn, name, treated_as: "uninterpreted" \| "assumed"}` |
| `unknown_reason` | string \| null | `UNKNOWN_OR_TIMEOUT` のとき `"timeout"` \| `"nonlinear"` \| `"unsupported_construct"` と位置 |

**エラー**

- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: `target` の解決に失敗。
- `E_INVALID_ARGUMENT`: `postconditions` が空。式が文法違反または未知の識別子を含む（`details.errors[]`）。`loop_unroll_depth` / `timeout_ms` が範囲外。対象が関数・メソッドでない。
- `E_UNSUPPORTED`: 対象が Tier 2〜4（Cython を除く）。対象が `async` 関数・ジェネレータ・`yield` を含む関数（M3 の範囲外。`details.reason`）。
- `E_TIMEOUT`: 処理規約 12 の全体上限を超えた。
- `E_REVISION_NOT_FOUND` / `E_WORKSPACE_NOT_CONFIGURED`: §5.8 のとおり。

**精度と降格**

| 水準 | 挙動 |
|---|---|
| `exact` | 変数の型を LSP から取り、SMT のソート（Int / Real / Bool / String / 配列）を決める。呼び出し先のシグネチャも解決して引数の型制約を加える |
| `structural` | 実行する。型注釈があればそれを、無ければリテラルから Int / Real / Bool を推定し、推定できない変数は制約なしの Int として扱う。`accuracy_warning` に「型は推定」と書く |
| `heuristic` / `lexical_fallback` | `E_UNSUPPORTED` |

**事後条件**

1. `status: "VIOLATED"` のとき `counterexample` は非 null であり、Python 対象では `counterexample.inputs` で対象関数を呼ぶと、`result: "violated"` の事後条件が偽になるか、`implicit_checks[]` で `violated` と報告した種類の例外（`ZeroDivisionError` / `IndexError` / `AttributeError` / `TypeError`）が報告した行で発生する。
2. `status: "VERIFIED"` のとき `solver.loops_found` は 0 であり、`"BOUNDED_VERIFIED"` のとき 1 以上である。
3. `status` が `UNKNOWN_OR_TIMEOUT` 以外のとき、`solver.time_ms` は `timeout_ms` 以下である。
4. 応答生成の過程で外部 LLM API を呼ばない。ログに呼び出し記録が無い。
5. 同一のファイル内容・同一の入力で 2 回実行したとき、`status` と `postconditions[].result` は同一である。

---

## 11.7 カテゴリ F: Git 時系列・セマンティック差分

カテゴリ F は `.git` のオブジェクトを `dulwich` で直接読み（シェルの `git` 起動に依存しない。例外は履歴〔T-17 の `git log --follow` 相当〕だけで、読み取り専用の `git` 子プロセスを使い、`git` が無ければ dulwich の走査に戻る。§17 OD-20、設計書 §7.9.2）、Tree-sitter の AST と突き合わせて「シンボル単位」の履歴と差分を返す。応答時間の目標は NFR-PERF-2。ワークスペースが Git リポジトリでないとき、本カテゴリの全ツールは `E_UNSUPPORTED`（`details.reason: "not_a_git_repository"`）を返す。

### T-17 `get_symbol_history`

**目的**: 特定のシンボルを変更したコミットだけを、コミットメッセージ・変更種別・行数とともに返し、あわせてそのシンボルと同時に変更されることが多いファイル・シンボル（共変更ホットスポット）を示す。ファイル全体の `git log -p` や行単位の `blame` を読む代わりに使う。

**マイルストーン**: M2

**対応要求**: FR-GIT-1, FR-WS-3, FR-FMT-2, FR-FMT-3, NFR-PERF-2

**入力**（`max_tokens` の既定は 2000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `target` | object | MUST | — | §5.3 の TargetRef |
| `max_commits` | integer | MAY | 5 | 1〜50 |
| `since` | string | MAY | なし | ISO 8601 の日時、またはリビジョン。これより古いコミットは走査しない |
| `include_semantic_diff` | boolean | MAY | true | 各コミットの変更種別と 1 行要約を付けるか |
| `include_body_diff` | boolean | MAY | false | 各コミットで当該シンボルの範囲に限った unified diff を付けるか |
| `include_co_changed_files` | boolean | MAY | true | 共変更ホットスポットを付けるか |
| `co_change_min_ratio` | number | MAY | 0.3 | 0.0〜1.0。これ未満の結合度は返さない |
| `co_change_limit` | integer | MAY | 5 | 1〜20 |

**処理規約**

1. `target` を `revision` の断面で解決し、その URN をキーに `git_symbol_changes` を引く。
2. 索引されていないコミット範囲があれば、その場で対象ファイルの履歴（ファイル名変更の追跡を含む）を辿り、各コミットの前後でファイルを AST 化してシンボルを比較し、`git_symbol_changes` に追記してから返す。走査の上限は 2,000 コミットとし、超えたら `E_TIMEOUT`（`details.partial: true`、確定分を返す）。
3. 変更種別の判定規則（`git_symbol_changes.change_type` と同じ語彙）:
   - `ADDED`: 親コミットに同一 URN のシンボルが無く、当該コミットにある。
   - `DELETED`: その逆。
   - `MODIFIED_SIG`: 両方にあり、空白と末尾カンマを正規化したシグネチャ文字列が異なる。
   - `MODIFIED_BODY`: 両方にあり、シグネチャが同一で本体のハッシュが異なる。
   - `RENAMED`: 同一コミット内で、本体ハッシュが同一のシンボルが別名または別ファイルに現れる。
4. マージコミットは、第 1 親と比べて当該シンボルが変わっているときだけ含める。`is_merge: true` を付ける。
5. 並び順はコミット日時の降順。1 レコードは 1 コミット。予算を超えたら `next_cursor`。
6. 共変更ホットスポットは `git_co_changes` から `entity_a` が当該 URN（無ければ当該ファイル）の行を `coupling_ratio` の降順で取り、`co_change_min_ratio` 以上のものを `co_change_limit` 件まで返す。`coupling_ratio` は「当該シンボルが変更されたコミットのうち、相手も変更されたコミットの割合」。
7. 本ツールは LSP を必要としない。ヘッダの `lsp` は `n/a`。

**出力**

compact の例:

```
# get_symbol_history ok conf=exact lsp=n/a rev=WORKING_TREE ws=app ~520/2000tok
target: lk://app/src/pricing.py#apply_discount  first_seen=4c5b6a7 (2026-03-02)  commits=12
commits:
- a1b2c3d 2026-10-21 okas MODIFIED_BODY +4 -1  "fix: clamp discount rate to 0..100"
- 9f8e7d6 2026-09-30 okas MODIFIED_SIG +6 -3  "feat: accept Decimal price"  was: (price: float, rate: int) -> float
- 7d6c5b4 2026-07-11 okas MODIFIED_BODY +2 -2  "refactor: extract rounding"
co_changed_hotspots:
- tests/test_pricing.py  0.92 (11/12)  last=a1b2c3d
- config/limits.yaml  0.58 (7/12)  last=9f8e7d6
# more: cursor=c911470253 remaining≈9
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `target` | object | `{urn, path, qualified_name}` |
| `first_seen` | object | `{commit_sha, timestamp}`。シンボルが最初に `ADDED` されたコミット |
| `total_commits` | integer | 当該シンボルを変更した全コミット数（走査済み範囲） |
| `commits[]` | array | 各要素は `{commit_sha, timestamp, author, message_summary, is_merge, change_type, lines_added, lines_deleted, old_signature, new_signature, body_diff}`。`message_summary` は件名行（72 文字で省略）。`old_signature` / `new_signature` は `MODIFIED_SIG` のときのみ非 null。`body_diff` は `include_body_diff: true` のときのみ |
| `co_changed_hotspots[]` | array | `{entity, entity_kind: "file" \| "symbol", coupling_ratio, co_change_count, total_count, last_co_changed_sha}` |

**エラー**

- `E_TARGET_NOT_FOUND` / `E_TARGET_AMBIGUOUS`: `target` の解決に失敗。
- `E_INVALID_ARGUMENT`: `max_commits` / `co_change_min_ratio` / `co_change_limit` が範囲外。`since` が日時にもリビジョンにも解釈できない。
- `E_REVISION_NOT_FOUND`: `revision`、または `since` に与えたリビジョンが解決できない。
- `E_UNSUPPORTED`: Git リポジトリでない。
- `E_TIMEOUT`: 処理規約 2 の走査上限を超えた。
- `E_WORKSPACE_NOT_CONFIGURED` / `E_CURSOR_EXPIRED`: §5.8 のとおり。`cursor` 発行後に新しいコミットが積まれた場合も `E_CURSOR_EXPIRED`。

**精度と降格**

| 水準 | 挙動 |
|---|---|
| `exact` | 現在断面のシンボルが LSP で解決済みで、かつ各コミットでその修飾名のシンボルがファイル内に一意に存在する。履歴の同定に曖昧さが無い |
| `structural` | 現在断面の解決が Tree-sitter のみ、または各コミットで同名シンボルが複数あり（オーバーロード等）シグネチャハッシュで同定した。`accuracy_warning` に「同定は AST による」と書く |
| `heuristic` | ctags の推定範囲と各コミットの行範囲の重なりで判定する。`change_type` は `ADDED` / `DELETED` / `MODIFIED_BODY` のみ（`MODIFIED_SIG` / `RENAMED` は判定しない） |
| `lexical_fallback` | シンボル単位の履歴を返さず、ファイル単位の履歴を返す。`entity_kind` は `"file"`。`accuracy_warning` にその旨を書く |

**事後条件**

1. 返した各 `commit_sha` は、対象ファイルについて名前変更を追跡した Git のログ（`git log --follow -- <path>` と同等の集合）に含まれる。
2. `commits[]` は `timestamp` の降順であり、要素数は `max_commits` 以下である。
3. `change_type: "MODIFIED_SIG"` の各コミットでは `old_signature` と `new_signature` が正規化後に異なり、`"MODIFIED_BODY"` では両者が等しい。
4. `co_changed_hotspots[]` の各要素で `coupling_ratio` は `co_change_count / total_count` に等しく（小数第 3 位で四捨五入）、`co_change_min_ratio` 以上であり、配列は `coupling_ratio` の降順である。
5. `first_seen.commit_sha` のコミットでの当該シンボルの `change_type` は `ADDED` である。

---

### T-18 `analyze_git_diff_symbols`

**目的**: 作業ツリーの未コミット変更、ステージ済み変更、またはブランチ間の差分を、行差分ではなく「どのシンボルが追加・削除・シグネチャ変更・本体変更・改名されたか」として構造化し、変更シンボル群に対する影響範囲と実行すべき最小のテスト集合を一括で返す。エージェントのセルフレビューと他者の変更把握に使う。

**マイルストーン**: M2

**対応要求**: FR-GIT-2, FR-DIAG-2, FR-WS-3, FR-FMT-2, FR-FMT-3, FR-FMT-4, NFR-PERF-2

**入力**（`max_tokens` の既定は 3000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `base_ref` | string | MAY | `"HEAD"` | 比較元。`"HEAD"` \| `"STAGED"` \| ブランチ \| タグ \| SHA |
| `target_ref` | string | MAY | `"WORKING_TREE"` | 比較先。`"WORKING_TREE"` \| `"STAGED"` \| `"HEAD"` \| ブランチ \| タグ \| SHA |
| `scope_paths` | array of string | MAY | なし | 差分を取るパスまたは Glob の配列 |
| `change_types` | array of enum | MAY | 全部 | 返す変更種別のフィルタ |
| `include_diagnostics` | boolean | MAY | true | 変更行に重なる現在の診断を付けるか |
| `include_impact_and_tests` | boolean | MAY | true | 影響範囲と推奨テストを計算するか |
| `impact_max_depth` | integer | MAY | 2 | 1〜5。T-09 に渡す深さ |

本ツールは共通パラメータの `revision` を使わない。与えられた場合は無視し、`warnings` に「`base_ref` / `target_ref` を使う」と記す。

**処理規約**

1. `base_ref` と `target_ref` を解決する。両者が同じオブジェクトを指すなら、変更 0 件の成功応答を返す。
2. 変更ファイルの一覧を名前変更検出つきで取る。`target_ref` が `WORKING_TREE` のときは未追跡かつ無視対象でないファイルも `added` として含める。
3. 各変更ファイルを両断面で AST 化し、シンボル集合を比較して `change_type` を付ける。判定規則は T-17 の処理規約 3 と同一。ファイルが名前変更され本体ハッシュが同じシンボルは `RENAMED`（`old_urn` に旧 URN）。
4. Tier 3 / Tier 4 のファイルはシンボル単位の比較をせず、`kind: "File"` の 1 要素として `ADDED` / `DELETED` / `MODIFIED_BODY` / `RENAMED` を付ける。
5. 並び順はファイルパスの昇順、同一ファイル内は新しい断面の開始行の昇順（`DELETED` は旧断面の開始行）。1 レコードは 1 変更シンボル。
6. `include_diagnostics` が true かつ `target_ref` が `WORKING_TREE` または `STAGED` のとき、`diagnostics` から行範囲が変更シンボルの `range` に重なるものを `uncommitted_diagnostics[]` に入れる。それ以外の `target_ref` では空配列と `warnings`。
7. `include_impact_and_tests` が true のとき、`DELETED` と `MODIFIED_SIG` のシンボルを優先し、次に `MODIFIED_BODY` の順で T-09 相当の計算を `impact_max_depth` で行い、結果を URN で重複排除して集約する。`ADDED` は影響計算の対象外。変更シンボル自身が `direct_breakage_candidates` に含まれる場合は除く。
8. `recommended_test_suite[]` は、`affected_test_targets` のうちテストファイルのパターン（`files.path` が `test` / `spec` を含む）に一致するものを、`runner_hint` とともに返す。`runner_hint` はファイル種別から決める候補文字列（Python: `pytest <path>::<test_name>`、TypeScript/JavaScript: `<runner> <path> -t "<test_name>"`、Rust: `cargo test <test_name>`）であり、実行可能性を保証しない。
9. 予算の詰め方: `modified_symbols[]` を先に詰め、`uncommitted_diagnostics[]`、`impact`、`recommended_test_suite[]` の順に残りを割り当てる。後段が入り切らないときは件数と上位 5 件だけを残し、`# summary:` に省略数を書く。
10. `format: "dot"` / `"mermaid"` では、変更シンボルをノード、`impact` の依存関係をエッジ（`CALLS` / `IMPLEMENTS` / `CO_CHANGES`）とするグラフを出す。変更シンボルのノードには属性 `changed="<change_type>"` を付ける。

**出力**

compact の例:

```
# analyze_git_diff_symbols ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~980/3000tok
diff: HEAD..WORKING_TREE  files=3  symbols: added 1, sig 1, body 2, deleted 0, renamed 0
=== src/pricing.py (91 lines) ===
[Function] apply_discount(price: Decimal, rate: int) -> Decimal @L12-30
  change: MODIFIED_SIG  was (price: float, rate: int) -> float
[Function] clamp_rate(rate: int) -> int @L33-38
  change: ADDED
uncommitted_diagnostics: 1
E src/api/routes.py:88:30 [pyright:reportArgumentType] "float" は "Decimal" に割り当てできません  fix=qf_11a0
impact: direct_breakage 2 sites / 1 file, transitive 4 symbols, co_change 2 files
recommended_test_suite: tests/test_pricing.py::test_apply_discount, tests/test_routes.py::test_create_order
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `base_ref` / `target_ref` | string | 入力と同じ |
| `resolved` | object | `{base_sha, target_sha}`。`WORKING_TREE` / `STAGED` の `target_sha` は null |
| `files_changed[]` | array | `{path, old_path, status: "added" \| "modified" \| "deleted" \| "renamed", tier, language}` |
| `modified_symbols[]` | array | `{urn, old_urn, path, kind, change_type, range, old_range, old_signature, new_signature, lines_added, lines_deleted, confidence_level}` |
| `summary` | object | `{files, by_change_type: {ADDED, DELETED, MODIFIED_SIG, MODIFIED_BODY, RENAMED}}` |
| `uncommitted_diagnostics[]` | array | T-08 の診断レコードと同じ形に `enclosing_urn` を加えたもの |
| `impact` | object \| null | `{direct_breakage_candidates[], transitive_dependents[], affected_test_targets[], co_change_hotspots[]}`。各配列の要素は T-09 と同じ形に、原因となった変更シンボルの `reason_urn` を加える |
| `recommended_test_suite[]` | array | `{test_urn, path, test_name, reason_urn, runner_hint}` |

**エラー**

- `E_REVISION_NOT_FOUND`: `base_ref` または `target_ref` が解決できない（`details.which`）。
- `E_INVALID_ARGUMENT`: `change_types` に未知の値。`impact_max_depth` が範囲外。`scope_paths` がルート外。
- `E_UNSUPPORTED`: Git リポジトリでない。
- `E_TIMEOUT`: 差分ファイル数または AST 比較が上限（既定 500 ファイル、または 10,000 ms）を超えた。処理済みファイルまでの結果を `data` に入れ、`details.partial: true`、`details.files_processed`。
- `E_WORKSPACE_NOT_CONFIGURED` / `E_CURSOR_EXPIRED`: §5.8 のとおり。`target_ref: "WORKING_TREE"` でページ間に作業ツリーが変われば `E_CURSOR_EXPIRED`。

**精度と降格**

| 水準 | 挙動 |
|---|---|
| `exact` | Tier 1 のシンボル比較と、LSP の参照グラフに基づく影響計算。多態呼び出しを実装へ展開する |
| `structural` | Tree-sitter のシンボル比較は同じ。影響計算は名前解決による `relations` に限り、`accuracy_warning` に「参照は名前一致」と書く |
| `heuristic` | ctags のアウトラインを名前と行範囲の重なりで比較する。`MODIFIED_SIG` と `RENAMED` は判定しない。`impact` は計算せず null、`warnings` に理由 |
| `lexical_fallback` | ファイル単位（`kind: "File"`）のみ。`impact` は null |

**事後条件**

1. `files_changed[]` のパス集合は、`base_ref` と `target_ref` の間で Git が報告する変更ファイル集合（`WORKING_TREE` のときは未追跡かつ無視対象でないファイルを加えた集合）と一致する。
2. `modified_symbols[]` の各要素で、`change_type: "MODIFIED_SIG"` なら `old_signature` と `new_signature` が正規化後に異なり、`"MODIFIED_BODY"` なら両者が等しく本体ハッシュが異なり、`"RENAMED"` なら `old_urn` が非 null である。
3. `uncommitted_diagnostics[]` の各診断の行は、`modified_symbols[]` のいずれかの `range` に含まれる。
4. `recommended_test_suite[]` の各要素の `path` はテストファイルのパターンに一致し、`test_urn` から `reason_urn` へ `relations` 上で `impact_max_depth` 以内に到達できる。
5. `base_ref` と `target_ref` が同じオブジェクトを指すとき、`modified_symbols[]` と `files_changed[]` は空である。

---

## 11.8 カテゴリ G: プロジェクト知識 DB 直接クエリ

カテゴリ 0〜F の定型ツールは、第 9 章のプロジェクト知識 DB（PaaD）に対するパラメータ化済みの問い合わせである。カテゴリ G は、その DB をエージェントが直接読むための 2 ツールを提供する。定型ツールで表せない複合条件（履歴 × 呼び出し関係 × 診断 × 契約）を 1 回の往復で引き出すことが目的であり、**書き込みは一切できない**（NFR-SAFE-2）。

### T-19 `get_project_db_schema`

**目的**: 公開ビューの定義（テーブル名・列名・型・意味・キー）、現在の行数、精度水準の構成比、すぐ使えるサンプル SQL を返し、エージェントが T-20 のクエリを書けるようにする。

**マイルストーン**: M1

**対応要求**: FR-DB-1, FR-FMT-2, FR-FMT-3, NFR-PERF-1, NFR-SAFE-2

**入力**（`max_tokens` の既定は 6000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `tables` | array of string | MAY | 全部 | 返すビュー名の絞り込み。未知の名前は `E_INVALID_ARGUMENT` |
| `include_stats` | boolean | MAY | true | 行数と構成比を付けるか |
| `include_sample_queries` | boolean | MAY | true | サンプル SQL を付けるか |
| `include_indexes` | boolean | MAY | false | 利用可能なインデックスを列ごとに示すか |

**処理規約**

1. 返す対象は第 9 章の公開ビュー 10 個（`workspaces`, `files`, `symbols`, `relations`, `dataflows`, `diagnostics`, `contracts`, `git_commits`, `git_symbol_changes`, `git_co_changes`）に限る。内部テーブル（索引状態、編集ジャーナル、キャッシュ）は列挙しない（MUST）。
2. 各ビューについて、列名・宣言型・NULL 可否・主キー/外部キー・1 行の説明を返す。説明文は第 9 章の DDL コメントと同一の文字列とする。
3. `include_stats` が true のとき、各ビューの行数を返す。`COUNT(*)` が 100 ms 以内に終わればその値を `row_count_exact: true` で、終わらなければ最終索引時のキャッシュ値を `row_count_exact: false` で返す。構成比は `symbols` と `relations` の `confidence_level` 別の行数割合。
4. `include_sample_queries` が true のとき、付録 A のサンプル SQL から 5 本以内を返す。各サンプルは T-20 にそのまま渡して実行できる形（バインド変数は `:name` 形式で、`params` の例を添える）でなければならない（MUST）。
5. 1 レコードは 1 ビュー。通常は予算内に収まるが、超えた場合は §5.6 に従う。
6. 本ツールの `_resolution_meta.confidence_level` は常に `exact`（メタデータ自体は正確）。データの精度構成は `data.coverage` で示す。
7. LSP を必要としない。ヘッダの `lsp` は `n/a`。
8. compact では各ビューを `table: <名前> (<N> rows)` の見出し行（`kv-line`）とし、`sig:`・`doc:` を付帯行（`attr-line`）にする。`[Table]` の `symbol-block` は `@L` と範囲を要するが、ビューには行範囲が無いため（T-02 の SQL の `[Table]` 要素とは別。実装で確定 2026-10-29）。

**出力**

compact の例:

```
# get_project_db_schema ok conf=exact lsp=n/a rev=WORKING_TREE ws=app ~900/6000tok
db: sqlite  views=10  indexed_at=2026-10-29T11:20:04+09:00  coverage: exact 94% structural 4% heuristic 1% lexical_fallback 1%
table: symbols (14230 rows)
  sig: urn PK, file_id FK files, parent_urn, name, qualified_name, kind, modifiers_json, start_line, end_line, signature, return_type, doc_summary, declared_in, scip_symbol, confidence_level
  doc: クラス・関数・変数・スキーマ要素・未知言語タグ。1 行 1 シンボル
table: relations (48910 rows)
  sig: relation_id PK, source_urn -> symbols.urn, target_urn -> symbols.urn, relation_kind, source_file_id FK files, target_file_id FK files, call_site_line, use_kind, guard_condition, confidence_level
  doc: シンボル間の有向エッジ（CALLS, IMPLEMENTS, INHERITS, REFERENCES, IMPORTS, TYPE_USES, READS_TABLE, WRITES_TABLE, RENDERS_TEMPLATE, EXPOSES_ENDPOINT, BINDS_NATIVE）
sample_queries:
- A-1 直近 N コミットで変更され、テストから参照されず、エラーを持つ関数: SELECT s.urn, ... (params: {"n": 10})
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `engine` | object | `{name: "sqlite", version}` |
| `indexed_at` | string | 最後に索引を更新した時刻（ISO 8601） |
| `coverage` | object | `{exact, structural, heuristic, lexical_fallback}` の割合（0.0〜1.0） |
| `tables[]` | array | 各要素は `{name, row_count, row_count_exact, description, columns[], indexes[]}` |
| `tables[].columns[]` | array | `{name, decl_type, nullable, pk, fk: "table.column" \| null, description}` |
| `tables[].indexes[]` | array | `include_indexes: true` のとき `{name, columns[]}` |
| `sample_queries[]` | array | `{id, title, sql, params_example, appendix_ref}` |

**エラー**

- `E_INVALID_ARGUMENT`: `tables` に公開ビュー以外の名前（`details.known_tables[]`）。
- `E_WORKSPACE_NOT_CONFIGURED`: §5.8 のとおり。
- `E_CURSOR_EXPIRED`: 索引が更新されてから `cursor` が使われた。

**精度と降格**

本ツールは降格しない。`_resolution_meta.confidence_level` は常に `exact`、`is_degraded` は常に `false`。DB 内のデータの精度構成は `coverage` に表れ、ワークスペースに Tier 4 のファイルが含まれるときは `warnings` に「`lexical_fallback` の行が N% 含まれる。クエリでは `confidence_level` 列で絞ること」と記す。

**事後条件**

1. `tables` 省略時、返した `tables[].name` の集合は第 9 章の公開ビュー 10 個の集合と一致する。
2. 各ビューの `columns[].name` の集合は、第 9 章の DDL に定義された列名の集合と一致する（差集合が空）。
3. `sample_queries[].sql` のそれぞれを `params_example` とともに T-20 に渡すと、`E_QUERY_REJECTED` にも `E_INVALID_ARGUMENT` にもならず `ok: true` を返す。
4. `row_count_exact: true` の各 `row_count` は、同時点の `SELECT COUNT(*)` の値と一致する。
5. `coverage` の 4 値の和は 1.0（小数第 3 位で四捨五入）である。

---

### T-20 `query_project_db`

**目的**: プロジェクト知識 DB に対して読み取り専用の SQL を実行し、結果をコンパクトな表・JSON・グラフで返す。複数ツールの結果をエージェントが文脈内で結合する代わりに、サーバー側で JOIN と集計を済ませる。

**マイルストーン**: M1

**対応要求**: FR-DB-2, FR-FMT-2, FR-FMT-3, FR-FMT-4, FR-LANG-2, NFR-PERF-1, NFR-SAFE-2

**入力**（`max_tokens` の既定は 2000）

| パラメータ | 型 | 必須 | 既定 | 規約 |
|---|---|---|---|---|
| `query` | string | MUST | — | 単一の `SELECT` 文または `WITH ... SELECT` 文 |
| `query_language` | enum | MAY | `"sql"` | `"sql"` \| `"datalog"`。`"datalog"` は将来拡張であり、M1 では `E_UNSUPPORTED` を返す |
| `params` | object | MAY | なし | 名前付きバインド。`query` 内の `:name` に対応する |
| `limit` | integer | MAY | 200 | 1〜2000。返す最大行数 |
| `auto_fallback_grep_pattern` | string | MAY | なし | 指定時、ワークスペース内を単語境界付き正規表現で検索し、結果を一時テーブル `temp_grep_matches` としてクエリから参照できるようにする |
| `grep_scope_paths` | array of string | MAY | なし | 上記 grep の対象パス制限 |
| `timeout_ms` | integer | MAY | 300 | 50〜5000 |
| `explain` | boolean | MAY | false | true のとき実行せず、実行計画（`EXPLAIN QUERY PLAN`）だけを返す |

**処理規約**

1. **静的検査**: `query` を SQLite の準備段階（`prepare` 相当）に通し、次のいずれかなら `E_QUERY_REJECTED` とする（MUST）: 文が 2 つ以上ある（`;` 区切りの複文。末尾の `;` とコメントは許し、`;;` は複文とみなす）／先頭のキーワードが `SELECT` / `WITH` 以外／`ATTACH` / `DETACH` / `PRAGMA` / `VACUUM` / `load_extension` / `writefile` / `readfile` を含む／公開ビュー以外のオブジェクトを参照する／構文エラー（`details.sqlite_message`）。許可する関数は SQLite 組み込みの集計・文字列・日付・JSON・FTS5 関数と、Lucianke が登録する読み取り専用関数に限る。
2. **接続**: 読み取り専用で開き（`SQLITE_OPEN_READONLY` 相当）、`PRAGMA query_only = ON` を設定し、認可フック（authorizer 相当）で読み取り以外の操作を拒否する。DB 本体への書き込みについて、3 重の防御のいずれか 1 つが欠けても他が効く構成とする（MUST。副作用を止められるのは authorizer と禁止語検査だけなので、これらは省かない。§13.2）。
3. **一時テーブル**: `auto_fallback_grep_pattern` が指定されたとき、クエリ実行前に `temp_grep_matches(path TEXT, line INTEGER, snippet TEXT, confidence_level TEXT)` を接続ローカルの一時領域に作り、`.gitignore` と `exclude_globs` を尊重して単語境界付きで検索した結果を入れる。`confidence_level` は常に `'lexical_fallback'`。マッチ上限は 10,000 件で、超えたら `data.grep_fallback.truncated: true`。概念文書の「結果 0 件のとき自動的に grep」は採用せず、**指定があれば必ず作る** 決定論的な挙動とする。作成と投入は `query_only` と認可フックの設定より前に済ませる（設計書 §7.6.4）。
4. **バインド**: `params` の各キーは `query` 中に `:key` として現れなければならず、現れないキー、または `params` に無いバインド名があれば `E_INVALID_ARGUMENT`。
5. **実行**: `limit + 1` 行まで取り、`limit + 1` 行目が存在すれば `truncated: true`。進捗ハンドラ（`progress_handler` 相当）で `timeout_ms` を監視し、超えたら中断して `E_TIMEOUT`（取得済みの行を `data.rows` に入れ、`details.partial: true`）。
6. **結果の形**: 1 レコードは 1 行。compact では Markdown 表とし、各セルは 80 文字で `…` 省略、NULL は `NULL` と表示する。列名はクエリの別名をそのまま使う。
7. **グラフ出力**: `format: "dot"` / `"mermaid"` は結果に `source_urn` と `target_urn` の両列があるときだけ許可し、`relation_kind` 列があればエッジのラベルにする。両列が無ければ `E_INVALID_ARGUMENT`。
8. **精度メタデータ**: 結果に `confidence_level` 列があれば、その値を集計して `_resolution_meta.confidence_breakdown` と `mixed` を埋め、最低水準を `confidence_level` に書く。列が無ければ、参照したビューに `exact` 以外の行が存在するとき `mixed: true`、`confidence_level: "exact"`、`accuracy_warning` に「結果に降格行が含まれうる。`confidence_level` 列を選択して確かめること」と書く。`temp_grep_matches` を参照したクエリは常に `is_degraded: true`。
9. `explain` が true のとき、`EXPLAIN QUERY PLAN` の出力を `data.plan[]` に入れ、`rows` は空とする。
10. LSP を必要としない。ヘッダの `lsp` は `n/a`。

**出力**

compact の例:

```
# query_project_db ok conf=exact lsp=n/a rev=WORKING_TREE ws=app ~610/2000tok
rows: 3 (limit 200, not truncated)  12ms  grep_fallback: none
| urn | caller_count | current_error |
| lk://app/src/pricing.py#apply_discount | 7 | "float" は "Decimal" に割り当てできません |
| lk://app/src/api/routes.py#create_order | 4 | NULL |
| lk://app/src/services/order.py#build_query | 3 | NULL |
```

json の `data`:

| フィールド | 型 | 意味 |
|---|---|---|
| `columns[]` | array | `{name, decl_type}`。`decl_type` は SQLite が報告する宣言型（式の列は null） |
| `rows[]` | array | 行の配列。**各行は列名をキーにしたオブジェクト**（キーの順は `columns[]` の列順。同じ列名が重複するときは 2 つ目以降に `_2`、`_3` を付ける。実装で確定 2026-10-29。旧版は `columns[]` と同じ順の値配列） |
| `row_count` | integer | 返した行数（`limit` 以下） |
| `truncated` | boolean | `limit` で切ったか |
| `elapsed_ms` | integer | 実行時間 |
| `grep_fallback` | object \| null | `{pattern, matches, truncated, scope_paths}` |
| `plan[]` | array | `explain: true` のときの実行計画行 |

**エラー**

- `E_QUERY_REJECTED`: 処理規約 1 のいずれか（`details.reason` に `"multiple_statements"` / `"not_select"` / `"forbidden_keyword"` / `"non_public_object"` / `"syntax_error"`、必要なら `details.sqlite_message`）。
- `E_INVALID_ARGUMENT`: `params` と `:name` の不一致。`limit` / `timeout_ms` が範囲外。`format: "dot"` / `"mermaid"` で `source_urn` / `target_urn` 列が無い。`grep_scope_paths` がルート外。
- `E_UNSUPPORTED`: `query_language: "datalog"`。
- `E_TIMEOUT`: `timeout_ms` 超過。部分結果あり。
- `E_WORKSPACE_NOT_CONFIGURED` / `E_CURSOR_EXPIRED`: §5.8 のとおり。索引更新後の `cursor` は `E_CURSOR_EXPIRED`。

**精度と降格**

クエリの実行自体は DB の内容に対して常に正確であり、精度は行ごとの `confidence_level` 列に表れる。本ツールは **応答全体の水準を行から集計する**（処理規約 8）。`temp_grep_matches` に由来する行は `lexical_fallback` であり、`relations` や `symbols` と JOIN した結果も `lexical_fallback` として扱う。LSP の稼働状況は結果に影響しない（LSP が `indexing` の間は `relations` の行が増え続けるため、`_resolution_meta.lsp_status` にその状態を転記する）。

**事後条件**

1. どのような `query` を与えても、呼び出し前後で DB ファイルの SHA-256 と公開ビューの各行数が変わらない。
2. `INSERT` / `UPDATE` / `DELETE` / `CREATE` / `DROP` / `ALTER` / `ATTACH` / `PRAGMA` のいずれかを先頭に持つ文、または `;` で区切った複文は、`ok: false`・`error.code: "E_QUERY_REJECTED"` で返り、DB に変更が無い。
3. `row_count` は `limit` 以下であり、`truncated` は「`limit + 1` 行目が存在したか」と一致する。
4. `ok: true` のとき `elapsed_ms` は `timeout_ms` 以下である。
5. `params` の各キーは `query` 中に `:key` として 1 回以上現れ、`query` 中の各 `:name` は `params` のキーとして存在する。

---
# 12. 運用仕様

## 12.1 `stdio` トランスポートとストリーム隔離

1. Lucianke 本体は起動直後、**最初の 1 バイトを書く前に**、ログ出力先を `stderr` とログファイル（§12.6）へ向ける（MUST）。`stdout` には改行区切りの JSON-RPC 2.0 メッセージ以外を書いてはならない（NFR-OPS-1）。
2. 子プロセス（LSP、ctags、grep、ソルバ）は `stdout` / `stderr` を必ずパイプで捕捉し、親の `stdout` を継承させない（MUST）。子の `stderr` はログへ転送する。子に親の `stdin` を継承させない（fd 0 は NUL に差し替える。子が MCP の入力を横取りしないため。S-1 で確認）。
3. 標準ライブラリや依存ライブラリが `stdout` に書く経路（`print`、警告、進捗バー）は、起動時に `stdout` のファイル記述子を差し替えて `stderr` に流す（SHOULD）。あわせて `stdin`（fd 0）も NUL に差し替え、MCP のトランスポートには退避した fd のストリームを明示的に渡す（設計書 ADR-3。S-1 で確認）。
4. 受入: 起動から終了までの `stdout` を記録し、各行が JSON として解析でき、かつ JSON-RPC のメッセージであることを検査する（§14.2 の全テストで常時検査する）。
5. HTTP 系トランスポートは提供しない（§5.1）。ネットワークを聴くポートを開かないことは §13.7 の受入でも確かめる。

## 12.2 位置エンコーディング

- LSP との `initialize` で `positionEncodings: ["utf-8", "utf-16"]` を提示し、サーバーが選んだ単位で内部変換する（MUST）。交渉できない LSP は UTF-16 とみなす。応答に `positionEncoding` が無ければ UTF-16 とし、サーバー別の上書きは持たない。LSP へ送る列は文字の境界だけで、丸めが要るときは要求を送らず降格する（S-11: ruff は文字の途中の列でパニックする。実測は basedpyright・vtsls・typescript-language-server が UTF-16、ruff が utf-8）。
- 外部に出す行・列は §5.10（1 始まり、コードポイント単位）に統一する（NFR-OPS-4）。
- 受入: 非 ASCII（全角文字、絵文字、結合文字）を含む行でのシンボル位置が、Tree-sitter の範囲と一致し、`rename_symbol` が正しい範囲を書き換える。

## 12.3 LSP プール

| 項目 | 規約 |
|---|---|
| 起動 | 遅延起動（Lazy Spawn）。当該言語への最初の要求で非同期に起動し、その要求には降格応答を返す（NFR-OPS-3）。起動時に全言語を立ち上げてはならない（MUST NOT） |
| 単位 | 言語 × ワークスペース ごとに 1 プロセス。同一ワークスペースの Python には型解決用（pyright 系）と Lint / Code Action 用（ruff 系）の 2 つを並列に持ってよい（MAY） |
| 発見 | 実行ファイルの探索順: 環境変数 `LUCIANKE_LSP_<サーバー名>_CMD`（サーバー名単位。先）→ `LUCIANKE_LSP_<LANG>_CMD`（言語単位。lint 役は `…_LINT_CMD`）→ ワークスペースの仮想環境（T-00 処理規約 3 と同じ候補: `.venv`、`pyvenv.cfg` のある `venv` / `env` / `.env`）の `bin`・`node_modules/.bin` → `PATH`。見つからなければ `lsp_status: "unavailable"` とし、理由を `configure_workspace status` に出す |
| 状態 | `standby`（未起動）→ `indexing`（起動済み・初期索引中。`$/progress` または `window/workDoneProgress` から進捗を取る。取れなければ最初の `publishDiagnostics` 受信まで）→ `ready` → `unavailable`（起動失敗・クラッシュ上限到達） |
| 文書同期 | `textDocument/didOpen` は要求で初めて触れたファイルに送り、編集後は `didChange`（全文または増分）、外部変更は `workspace/didChangeWatchedFiles`。開いた文書は LRU で 200 件を上限とし、超えた分は `didClose` |
| 要求のタイムアウト | 定義・参照・ホバー: 3,000 ms。`callHierarchy`: 5,000 ms。`codeAction`: 5,000 ms。`rename`: 10,000 ms。超過は段を 1 つ降りて応答し、`warnings[]` に記録する。超過した要求には `$/cancelRequest` を送る。ただし取り消しを受けるとその種類の要求に以後答えられなくなるサーバーの要求（basedpyright / pyright の参照・実装・`executeCommand`）には送らず、遅れて届く応答を捨てる（実装で確定 2026-10-31、BUG-0090）。 **1 問い合わせの予算つきの exact の要求**は、要求 1 つの期限ではなく 1 問い合わせの予算（`EXACT_BUDGET_S` = 10 秒）の残りまで待つ: T-07 の根の要求（根の子を求める prepare と incoming / outgoing。深いノードの要求は 5,000 ms のまま）と、T-06 の参照検索（references と、同じ答えの宣言の位置を引く definition。3,000 ms の代わり）。**連続タイムアウトの数え**（3 回で `unavailable`）に入れるのは、要求 1 つの期限以上待っても答えなかった要求だけ。予算の内に遅れて答えた要求、予算が尽きて（期限より短い残りで）打ち切った要求は数えない。T-07 は予算で打ち切ったとき、すでに送っていた要求の答えを裏で待ち（根は予算の残り＋60 秒、深いノードは 5,000 ms まで。同時に 8 ノードまで）、届けば `relations` に `exact` で書き戻す。同じノードを同じファイルの状態で聞く次の問い合わせは、要求を送り直さずその答えを待つ（実装で確定 2026-10-31、BUG-0123。理由: basedpyright は取り消しを受けても計算を止めないので、答えを捨てると呼び直しが毎回やり直しになり、その後ろに並んだ要求も予算を使い切っていた）。遅れて答えた要求は数えず、裏で待つ上限まで答えなかった要求は数える。予算の無い要求（T-04 の hover・T-11 の rename など）の期限切れは今までどおり数える。予算の内に答えなかったときの警告は、切れたのが予算であることとその値・次にすること（呼び直す）を書く。（実装で確定 2026-10-31、BUG-0091。理由: 冷えたサーバーが最初の問い合わせで遅いだけで LSP をセッションの間 `unavailable` にするのは、呼ぶ側の AI にとって最初の答えが数秒遅いことよりずっと悪い。） |
| 再起動 | クラッシュ時は指数バックオフ（1 s, 2 s, 4 s）で最大 3 回再起動。超えたら `unavailable`。`configure_workspace reindex_scope: "lsp"` で手動リセット。プロセスが生きたまま、ある要求に以後答えられない壊れ方をした（サーバーごとに既知のエラー文が返った）ときも起動し直し、その間は `ready` と言わない（`lsp_status: "indexing"`、`configure_workspace status` の `reason: "restarting"`）。起動から 60 s 未満で 3 回続いたら `unavailable`（`reason: "broken_3x"`）（実装で確定 2026-10-31、BUG-0090） |
| 待機解放 | 30 分間要求が無い LSP は `shutdown` → `exit` して `standby` に戻してよい（MAY） |
| 候補サーバー | Python: pyright / basedpyright + ruff。TypeScript / JavaScript: typescript-language-server（既定）または vtsls（TypeScript が見つからず前者が起動できないときの代替）。Rust: rust-analyzer。Go: gopls。C / C++: clangd。C#: csharp-ls または OmniSharp。Kotlin: kotlin-language-server。Swift: sourcekit-lsp。いずれも採用前に実機で `initialize` と主要要求を確かめる |

## 12.4 ファイル監視と増分同期

1. OS ネイティブの監視（Linux `inotify`、macOS `FSEvents`、Windows `ReadDirectoryChangesW`）を用い、`.gitignore` と `exclude_globs` に一致するパスを除外する。入れ子の Git の作業ツリー・リポジトリ（根以外で `.git` のファイルかディレクトリを持つディレクトリ。根の `.gitmodules` が挙げるサブモジュールは除く）の中は、走査でも監視でも索引しない（Git もそれを親の作業ツリーの一部として扱わない。並行の作業ツリー `.claude/worktrees/*` を何十も取り込まないため。BUG-0118）。
2. イベントは静穏 10 ms で配信し（パスが増え続ける間は最大 1 s まで束ねる）、種別は信用せずパスの集合に畳んで、ファイルごとに `sha256` を再計算して **変化があったときだけ** 再解析する（S-6 で確認: 単発変更が p95 約 20 ms で届く。50 ms のデバウンスでは単発が約 100 ms になるため採らない）。ディレクトリの移動・改名は子のイベントが届かないので、列挙して取り込む。
3. 単一ファイルの変更は、検知から PaaD 反映と LSP 通知まで 500 ms 以内（NFR-PERF-3）。
4. 1 回のバーストで 500 ファイル以上が変わった場合（ブランチ切り替えなど）は、個別処理をやめて差分索引（起動時と同じ手順）に切り替える。1 回の配信は 500 に届かないことがあるため、大きい配信が続くあいだは 0.3 秒静まるまで合算して数える（S-6 で確認）。
5. 既定 5 MB を超えるファイルは構文木を作らず、Tier 3 ならストリーミング骨格、それ以外は `files` 行のみ（`confidence_level: "lexical_fallback"`）とする。上限は設定で変えられる。圧縮済み・束ねた生成物の JS / TS（名前が `*.min.js` / `*.min.mjs` / `*.min.cjs`、または 32 KB 以上で 1 行の平均が 300 バイト以上）も構文木を作らず `files` 行のみとする（1〜2 文字の名前のシンボル・関係を数千作り、1 本の取り込みに数十秒かかるため。BUG-0119）。
6. 監視が利用できない環境（ネットワークドライブなど。Windows のネットワークドライブは未検証。`force_polling` は取りこぼすので使わない）では、要求時に対象ファイルの `sha256` を照合する遅延検証に切り替え、`configure_workspace status` に `watcher: "polling"` と表示する。

## 12.5 終了処理

- `stdin` の EOF、`SIGTERM` / `SIGINT`（Windows では `CTRL_BREAK_EVENT` と親プロセスの終了）を終了契機とする。
- 手順: 新規要求の受付停止 → 実行中の編集トランザクションの完了またはロールバック → 各 LSP に `shutdown` → `exit` → 500 ms 以内に終了しない子を強制終了 → WAL のチェックポイント → 終了。
- 受入: 終了後 1 秒以内に Lucianke が起動した子プロセスが 0 になる（NFR-OPS-2）。親が異常終了した場合も同じ（`stdin` EOF で検知）。

## 12.6 ログ

| 項目 | 規約 |
|---|---|
| 出力先 | `stderr`（人が読む形式）と `<cache_root>/logs/lucianke-<日付>.log`（JSON Lines） |
| 水準 | `error` / `warn` / `info` / `debug`。既定 `info`。環境変数 `LUCIANKE_LOG_LEVEL` |
| 必ず残すもの | 各ツール呼び出しの `tool`、引数の要約（パスと URN。コード本文は含めない）、`timing_ms`、`confidence_level`、エラーコード。編集トランザクションの `edit_id` と結果。LSP の起動・終了・クラッシュ |
| 残さないもの | ファイル本文、`new_code`、秘密らしき値（§13.5）。`debug` でもスニペットは 200 文字まで |
| ローテーション | 日次。既定 7 日保持 |

## 12.7 設定

| 手段 | 項目 |
|---|---|
| 環境変数 | `LUCIANKE_CACHE_DIR`、`LUCIANKE_LOG_LEVEL`、`LUCIANKE_LSP_<サーバー名>_CMD` / `LUCIANKE_LSP_<LANG>_CMD`（起動コマンドの上書き。サーバー名単位が先）、`LUCIANKE_DISABLE_LSP`（カンマ区切りの言語）、`LUCIANKE_MAX_FILE_BYTES` |
| ワークスペース設定ファイル | ルートの `.lucianke.toml` を **読むだけ**（MAY。採否は §17 OD-13）。項目: `exclude_globs`、`python_interpreter`、`lsp.<lang>.cmd`、`lsp.position_encodings`（MAY。既定 `["utf-8", "utf-16"]`、`["utf-16"]` に絞る逃げ道。**未実装**）、`test_globs`、`co_change_window` |
| ツール | `configure_workspace`（T-00）。セッション内の設定は PaaD の `workspaces` 行に保存し、次回起動でも使う |

優先順位: ツールでの明示指定 > 環境変数 > `.lucianke.toml` > 自動検出。

## 12.8 資源上限

| 資源 | 既定上限 | 超過時 |
|---|---|---|
| 構文木キャッシュ | 2,000 ファイルまたは 512 MB（LRU）。メモリは構文木の推定量（ソース長 × 25）で数える。構文木はソースの約 20〜26 倍のメモリを食う（S-2 で確認） | 古いものを破棄し再解析で賄う |
| PaaD ファイルサイズ | 制限なし（目安として 10 万シンボルで 100 MB 程度を想定。実測は §14） | — |
| 1 要求の処理時間 | 読み取り 10,000 ms、編集 30,000 ms | `E_TIMEOUT`（部分結果つき） |
| `new_code` の長さ | 1 MB | `E_INVALID_ARGUMENT` |
| `query` の長さ | 64 KB | `E_INVALID_ARGUMENT` |
| 同時実行の読み取り要求 | 16 | 待機（FIFO） |

---

# 13. セキュリティと安全性

## 13.1 パスの封じ込め

- 受け取ったパスは正規化（`.`・`..` の解決、区切りの統一）し、シンボリックリンクを解決した実体パスが登録済みルートのいずれかの配下であることを検査する（MUST）。配下でなければ `E_INVALID_ARGUMENT`。
- 読み取りも書き込みも、監視も、この検査を通ったパスにのみ行う（NFR-SAFE-3）。
- `configure_workspace set_roots` で登録できるのは、サーバープロセスが読める既存ディレクトリに限る。MCP クライアントの `roots/list` 以外のルートを登録したときは `warnings[]` にその旨を出す。

## 13.2 プロジェクト知識 DB の読み取り専用公開

- `query_project_db` 用の接続は読み取り専用フラグで開き、`PRAGMA query_only = ON` を設定する（MUST）。
- 実行前に文を解析し、先頭が `SELECT` または `WITH`（最終的に `SELECT`）以外は `E_QUERY_REJECTED`（MUST）。複文（`;` 区切り）は拒否する。
- authorizer で、公開テーブル（§9.5）以外のオブジェクト、`ATTACH` / `DETACH`、`PRAGMA`、`load_extension`、ファイル入出力系の関数（`readfile`, `writefile`, `fts5` の外部コンテンツ書き込み）を拒否する（MUST）。
- progress handler で `timeout_ms` の超過を検知して中断する（期限は `time.perf_counter()` で判定する）。再帰 CTE の深さの上限は SQLite に機構が無く実装できないため設けず、時間で止める。巨大な値は `SQLITE_LIMIT_LENGTH`（16 MiB）と `SQLITE_LIMIT_SQL_LENGTH`（64 KiB）で抑える（S-7 で確認）。
- 防御は静的検査を含めて **4 段**（静的検査 → `mode=ro` → `query_only` → authorizer）。T-20 の `temp_grep_matches` の作成と投入は、`query_only` と authorizer を有効にする前に済ませる（`query_only` の下では一時テーブルも作れない）。authorizer は `PRAGMA` のうち `data_version` と `query_only` の読み取りだけを許可する（実装で確定 2026-10-29。S-7 で確認した順序）。
- 読み取り専用・`query_only`・authorizer の 3 重は、DB 本体への書き込みについてはどれか 1 つが欠けても他が効く。ただし `ATTACH` が任意パスに 0 バイトのファイルを作る副作用と、`PRAGMA journal_mode` による DB の変更は、authorizer と禁止語検査（`ATTACH` / `PRAGMA` など）でのみ止まる。禁止語検査は省かない（S-7 で確認）。
- 返却行数は `limit`（既定 200、最大 2,000）で切り、切ったことを `page` と `warnings[]` に書く。

## 13.3 子プロセスの起動

- 起動コマンドは設定（§12.7）で決まるものに限り、ユーザー入力（ツール引数）からコマンドや引数を組み立てない（MUST）。
- シェルを介さずに直接起動する。環境変数は必要最小限（`PATH`、ロケール、`VIRTUAL_ENV` 等）を明示的に渡す。
- 子の作業ディレクトリはワークスペースルート。

## 13.4 編集の安全装置

第 10 章の前提検証・構文検証・降格インターロック・原子的書き込み・ジャーナルが規範である。加えて:

- 編集ツールはワークスペースルート外、`.git/` 配下、および `exclude_globs` に一致するファイルを書き換えない（MUST）。
- 1 トランザクションで変更できるファイル数は 500 を上限とし、超える場合は `dry_run` の結果だけを返して `E_INVALID_ARGUMENT` とする（`rename_symbol` で大規模な改名が必要な場合は `scope_paths` で分割する）。

## 13.5 秘密情報

- 既定で、名前が秘密らしいファイル（`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `*credentials*.json`）は `files` に登録するが本文を索引せず、骨格・本体取得には `E_UNSUPPORTED` と理由を返す（SHOULD）。`configure_workspace` で個別に解除できる。
- ログにファイル本文や `new_code` を書かない（§12.6）。
- 診断メッセージや参照スニペットに秘密値が含まれうることは避けられないため、`snippet` は該当行のみ・200 文字までに限る。

## 13.6 入力検証

- すべての引数は `tools/list` で公開した JSON Schema で検証し、未知のプロパティは拒否する（`additionalProperties: false`。拒否は `E_INVALID_ARGUMENT`〔§5.1〕。MCP SDK は引数を検証しないので、Lucianke のパイプラインで検証する）。
- 正規表現を受ける引数（`query`、`auto_fallback_grep_pattern`、`exclude_globs`）は、バックトラックしないエンジン（RE2 系または Rust `regex` 系）で評価するか、評価時間の上限を設ける（MUST）。
- セレクタ（XPath / JSONPath / jq）は評価時間の上限（1,000 ms）を設ける。

## 13.7 LLM 不使用と外部通信の禁止

- Lucianke は応答の生成過程で外部 LLM API を呼ばない（MUST）。自然言語の解釈はホスト側エージェントの責務である。
- Lucianke は LSP や依存ツールの子プロセス以外に外向きのネットワーク接続を開かない（MUST）。LSP 自身が行う通信（パッケージの型定義取得など）は LSP の設定に従う。
- 受入: 外向き接続を遮断した環境で第 14 章の全テストが同じ結果になる。

---

# 14. 性能目標と測定手順

## 14.1 位置づけ

第 1.5 節と第 3.2 節の数値は、本章の手順で測って初めて合否になる。ベンチマークの台本・フィクスチャ定義・結果の記録形式は Lucianke のリポジトリ（`bench/` 配下）に置き、本体と同じコミットで保守する。受入検査の役はこの台本を実行し、結果を `.pair-agent/acceptance/` に残す。人手で測る項目を作らない（検証の仕組みの欠陥とみなす）。

## 14.2 フィクスチャと環境

| 項目 | 規約 |
|---|---|
| フィクスチャ F-PY | 公開リポジトリの Python プロジェクト 1 つ。コミット SHA を `bench/fixtures.toml` に固定する。規模の目安は 1 万〜5 万行、テストディレクトリと `pyproject.toml` を持つ。候補は §17 OD-10 |
| フィクスチャ F-TS | 同様の TypeScript プロジェクト 1 つ |
| フィクスチャ F-POLY | Lucianke のリポジトリ内に置く合成プロジェクト。Tier 2〜4 のファイル（`.ipynb`, `.sql`, `.proto`, `.dot`, XML, JSON, YAML, TOML, `.j2`, `.rst`, 独自 DSL, COBOL 風のテキスト）と、意図的な型エラー・循環呼び出し・オーバーロード・非 ASCII 識別子を含む |
| OS | Windows、macOS、Linux のそれぞれで実行し、結果に OS を記録する（NFR-PORT-2）。閾値は OS によらず同一 |
| 機材 | CPU・メモリ・ストレージ種別を結果に記録する。レイテンシの閾値は「同時に他の負荷を掛けない状態」で判定する |
| 共通の前提 | すべての測定の間、`stdout` の JSON 純度（§12.1）と子プロセス数（§12.5）を監視し、違反があればその実行全体を不合格とする |

## 14.3 トークン削減率（NFR-TOK-1）

1. **シナリオ**: 次の 4 つを F-PY と F-TS の双方で定義し、対象シンボルは台本に固定する。
   - S1: 指定した 5 つの関数・メソッドの引数と戻り値の型、Doc 要約を知る。
   - S2: 指定したメソッドの全呼び出し元と、それを呼ぶテストを列挙する。
   - S3: 指定した関数のシグネチャ変更で壊れる箇所と実行すべきテストを求める。
   - S4: 指定ファイルの型エラーを 1 件直し、残る診断が 0 であることを確かめる。
2. **従来手法（基準値 B）**: ファイル全文読み込みと grep だけを使う手順を台本に固定し、各ツール呼び出しの入出力テキストを `o200k_base` で計数して合計する。grep のヒットしたファイルはすべて全文を読む（人手の取捨選択を模さない）。
3. **Lucianke（測定値 L）**: 各シナリオを Lucianke のツール呼び出しの列として台本に固定し、同じ方法で計数する。
4. **判定**: シナリオごとに `1 - L / B >= 0.75`。4 シナリオ × 2 フィクスチャの 8 件すべてで満たす。
5. 結果には B・L・呼び出し回数を併記し、概念文書第 8 章の数値（96% 等）との比較を §14.7 として報告する。

## 14.4 レイテンシ（NFR-PERF-1、NFR-PERF-2）

1. **ウォームアップ**: 対象言語の LSP が `ready` になり、起動時の差分索引が終わるまで待つ。これを満たすまでの時間も記録する（合否には用いない）。**初回の索引が重く時間がかかることは許容する**（師匠 2026-10-29「初回分析はいずれにせよ重く時間がかかる可能性があるのは仕方ない」）。合否の対象は定常状態の応答であり、初回索引の所要は報告値にとどめる。起動時間も同様で、コールドスタート（プロセス起動 → `initialize` 応答）は約 1 秒（中央値 980 ms。MCP SDK の import が主因。S-1 で確認）であり、初回の重さとして許容し、報告値にとどめる。ただし索引中もツールは降格応答で答え続けなければならない（NFR-OPS-3）。
2. **母集団**: ツールごとに、台本が F-PY / F-TS からランダムに選んだ対象で 200 回呼び出す。対象は Tier 1 のファイルに限り、成功し、かつ `is_degraded == false`（§5.5。そのツールの最高水準で応答した）呼び出しだけを集計する。T-02・T-03・T-05 は最高水準が `structural` なので、`exact` だけでは母集団が空になる。`exact` で応答した呼び出しだけの集計も併記する。降格応答（`is_degraded == true`）は別に集計して報告する。
3. **計測**: サーバー内の `timing_ms`（受信から送信直前）と、クライアント側の往復時間の両方を記録する。合否はサーバー内時間で判定する。
4. **判定**: Fast Path ツール（§3.2 の NFR-PERF-1 の一覧）は p95 < 50 ms。Verification Path ツールは p95 < 500 ms。`verify_contract` は `timeout_ms` で打ち切った呼び出しを母集団から除く。
5. 編集ツールは `diagnostics_wait_ms: 0` で呼び、診断待ちを含めない時間で判定する。診断が揃うまでの時間は別途記録する。

## 14.5 編集の構文妥当性（NFR-SAFE-1）

1. **母集団**: F-PY と F-TS から 1,000 件のシンボルを選び、次の操作を行う: 恒等 `replace_body`（同じ本体）300 件、決定的な変換（ローカル変数の改名、文の末尾追加、早期 return の挿入）を施した `replace_body` 400 件、`insert_member` 150 件、`insert_after` 100 件、`delete_symbol` 50 件。
2. **判定**: 各操作のあと、Tree-sitter でファイルを解析してエラーノードが操作前以下であり、LSP が新しい構文エラーを報告しないことを「成功」とする。成功率 ≥ 99.9%（1,000 件中 999 件以上）。
3. 恒等編集はバイト単位で同一であること（§10.10 EP-6）を別途 100% 要求する。
4. 操作の前後でジャーナルからの復元が可能であることを 50 件で確かめる。

## 14.6 外部変更の反映時間（NFR-PERF-3）

1. 台本が Lucianke の外（別プロセス）から F-PY のファイル 1 つを書き換え、書き込み完了時刻を記録する。
2. Lucianke 側で、当該ファイルの `files.sha256` が新しい値になった時刻と、LSP への `didChange` / `didChangeWatchedFiles` 送信時刻を記録する（ログから取る）。
3. 100 回繰り返し、p95 で 500 ms 以内。

## 14.7 概念文書の仮説の扱い

概念文書第 8 章の「総トークン約 90% 削減 / 作業時間約 80% 短縮」は、本書では仮説である。§14.3 の結果に、シナリオを通した往復回数と合計トークンを添えて報告し、仮説の当否を記す。閾値は設けない。

## 14.8 結果の記録

- 1 回の実行につき 1 つの JSON（`bench/results/<日時>-<OS>.json`）に、フィクスチャ SHA、Lucianke のコミット、機材、各測定の生データと判定を記録する。
- 要約は Markdown で `.pair-agent/acceptance/` に置き、合否と不合格項目を先頭に書く。

---

# 15. 実装ロードマップ（マイルストーン）

## 15.1 原則

- マイルストーンは **期間ではなく出口条件** で定義する。出口条件はすべて第 14 章の手順か第 11 章の事後条件で機械的に判定できる。
- 各マイルストーンの終わりで、Lucianke はコーディングエージェントに接続して実用できる状態にある。
- 前のマイルストーンの出口条件を壊さない（非回帰）。
- 概念文書の「2〜3 週間」といった期間は本書に持ち込まない。作業する者がエージェントである以上、人の暦での見積もりは何も測っていない。

## 15.2 M1: Fast-Path MVP

**含むもの**

| 領域 | 内容 |
|---|---|
| ツール（14 本） | T-00 `configure_workspace`、T-01 `get_repo_map`、T-02 `get_file_skeleton`、T-03 `search_workspace_symbols`、T-04 `resolve_symbols`、T-05 `get_symbol_bodies`、T-06 `find_references_and_implementations`、T-07 `query_call_hierarchy`、T-08 `get_diagnostics`、T-10 `edit_symbol_body`、T-11 `rename_symbol`、T-13 `apply_code_action`、T-19 `get_project_db_schema`、T-20 `query_project_db` |
| 基盤 | `stdio` と隔離（§12.1）、エンベロープ・Compact DSL・予算・ページネーション（§5〜6）、URN（§7）、PaaD の 10 テーブル（Git 系 3 テーブルは定義のみで空でよい）、Tree-sitter 索引、LSP プール、ファイル監視、編集プロトコル（第 10 章）全体 |
| 言語 | Tier 1 は Python と TypeScript / JavaScript を必須。Rust / Go / C / C++ / C# / Kotlin / Swift は LSP が見つかれば動く（best effort、§17 OD-7）。Tier 2〜4 は §8.6 の M1 範囲（B1a、B2 読み取り、B3 出力、B4 骨格・部分取得）と Tier 4 のラダー |
| `revision` | T-02、T-05 のみ（§8.5） |

**出口条件**

| # | 条件 |
|---|---|
| M1-1 | 14 ツールが第 11 章の事後条件を F-PY、F-TS、F-POLY で満たす |
| M1-2 | §10.10 の EP-1〜EP-9 を満たす |
| M1-3 | NFR-PERF-1 を M1 の 14 ツールで満たす（§14.4） |
| M1-4 | NFR-TOK-1 をシナリオ S1、S2、S4 で満たす（S3 は T-09 が M2 のため除く） |
| M1-5 | NFR-SAFE-1〜4、NFR-OPS-1〜4 を満たす |
| M1-6 | Claude Code の MCP 設定（`.mcp.json`）で接続し、台本化したセッション（骨格 → 本体 → 編集 → 診断ゼロ確認）が完走する。台本と記録は `bench/e2e/` に置く |
| M1-7 | Windows / macOS / Linux で M1-1〜M1-6 が同じ結果になる |
| M1-8 | 第 16 章 L-8 のライセンス検査が CI で通る（依存木に許可リスト外の SPDX 識別子が無い）。`LICENSE` と `THIRD_PARTY_NOTICES.md` が配布物に含まれる |

## 15.3 M2: 構造化リファクタリング・影響分析・Git 時系列

**含むもの**

| 領域 | 内容 |
|---|---|
| ツール（5 本） | T-09 `analyze_change_impact`、T-12 `structural_search_replace`、T-14 `find_code_paths`、T-17 `get_symbol_history`、T-18 `analyze_git_diff_symbols` |
| 基盤 | Git 取り込み（`git_commits`、`git_symbol_changes`、`git_co_changes`）、CFG / Def-Use 抽出と `dataflows`、構文パターンエンジン、`revision` の全読み取りツール対応、SCIP の取り込み（MAY、§17 OD-4） |
| ブリッジ | §8.6 の B1b、B2 編集、B3 入力、B4 置換、B5、B6、B7 |

**出口条件**

| # | 条件 |
|---|---|
| M2-1 | 5 ツールが事後条件を満たす。T-09 の `co_change_hotspots` は F-PY の実履歴から求めた値である |
| M2-2 | NFR-PERF-2 を M2 のツールで満たす |
| M2-3 | NFR-TOK-1 をシナリオ S3 でも満たす |
| M2-4 | M1 の出口条件がすべて保たれている（非回帰） |
| M2-5 | `revision` 指定の読み取りが、作業ツリーの `sha256` を変えずに完了する（§8.5） |

## 15.4 M3: 契約抽出と境界付き検証

**含むもの**

| 領域 | 内容 |
|---|---|
| ツール（2 本） | T-15 `extract_code_contracts`、T-16 `verify_contract` |
| 基盤 | ルールベース契約抽出器（型・ガード節・構造化 Doc コメント・OpenAPI / JSON Schema・Gherkin）、関数内 SSA 変換、SMT ソルバ連携（タイムアウト・ループ展開上限） |

**出口条件**

| # | 条件 |
|---|---|
| M3-1 | 2 ツールが事後条件を満たす。F-POLY に仕込んだ境界バグ（ゼロ除算、境界外、Null）に対して `VIOLATED` と反例を返す |
| M3-2 | NFR-PERF-2 を満たす（`timeout_ms` 打ち切りを除く） |
| M3-3 | 外向き通信を遮断した環境で結果が変わらない（§13.7） |
| M3-4 | M1・M2 の出口条件が保たれている |

## 15.5 `TODO.md` との対応

リポジトリの `TODO.md` は層ごとの 4 フェーズ（Core Engine / MCP Interface / Deterministic Edit Protocol / Optimization）で書かれている。本書は「各段の終わりでエージェントが使える」ことを優先して能力ごとの 3 段にした。対応は次のとおり。`TODO.md` の書き換えは師匠の判断に委ねる（§17 OD-14）。

| `TODO.md` | 本書 | 補足 |
|---|---|---|
| Phase 1: Core Engine（Tree-sitter、骨格抽出、LSP シンボル解決） | M1 の基盤と T-01〜T-07 | |
| Phase 2: MCP Interface | M1 の §5〜7 と T-00、T-19、T-20 | MCP の入口が無いと何も試せないため M1 に含める |
| Phase 3: Deterministic Edit Protocol | M1 の第 10 章と T-10、T-11、T-13 | 編集と診断の一体化が無いと実戦投入の価値が半減するため M1 に含める。T-12 は M2 |
| Phase 4: Optimization | 各マイルストーンの出口条件（第 14 章） | 最適化を最後に回さず、各段で閾値を満たす |
| （未記載） | M2 の Git・影響・経路、M3 の契約 | |

---

# 16. ライセンス方針と依存ライブラリの確認

## 16.1 方針

Lucianke は **できるだけ自由なライセンス** で公開する。本体は **MIT** に確定した（師匠 2026-10-29、§17 OD-19）。この自由を依存で損なわないため、次を規範とする。

| # | 規約 |
|---|---|
| L-1 | 本体のソースコードと配布物は MIT。`LICENSE`（MIT 全文）をリポジトリ直下に置く |
| L-2 | **直接依存（import・リンク・同梱）してよいのは寛容ライセンスのみ**（MUST）: MIT、BSD-2/3-Clause、Apache-2.0（LLVM 例外つきを含む）、ISC、Unlicense、CC0-1.0、UPL-1.0、パブリックドメイン。デュアルライセンスは寛容側を選び、その旨を `THIRD_PARTY_NOTICES.md` に書く |
| L-3 | 弱いコピーレフト（EPL、MPL、LGPL）およびリンク例外つき GPL は、**同梱せず、サブプロセスとして PATH 上のものを起動する使い方に限る**（SHOULD）。例外的にリンクする場合は §17 の未決事項として師匠が決め、当該ライセンスの文面と著作権表示を配布物に同梱する |
| L-4 | 強いコピーレフト（GPL、AGPL）は **リンクも同梱もしない**（MUST NOT）。サブプロセスとして利用者の環境にあるものを起動するのは可。その場合も配布物には含めない |
| L-5 | 第三者の成果物を多数同梱するパッケージ（文法バンドルなど）は、同梱物のライセンスを個別に確認するまで採用しない（MUST）。個別パッケージを使う |
| L-6 | 実行時に外部からファイルを取得する依存（語彙・モデル・文法のダウンロード）は、§13.7 の外部通信禁止に反するため採用しない（MUST NOT） |
| L-7 | 配布物に `LICENSE`、`THIRD_PARTY_NOTICES.md`（依存ごとに名前・版・SPDX・確認 URL・必要な表示文）を含める（MUST）。Apache-2.0 の依存は NOTICE の内容を転記する |
| L-8 | CI でライセンス検査を回し、L-2 の許可リスト外の SPDX 識別子が依存木に入ったら失敗させる（MUST）。候補ツール: `pip-licenses`（MIT）、`cargo-deny`（MIT OR Apache-2.0）。検査は M1 の出口条件（§15.2 M1-8） |
| L-9 | 依存の版を固定するたびにライセンスを再確認し、付録 D の表を更新する。「未確認」を残したまま版を固定しない |

## 16.2 調査の結果と採否

2026-10-29 に、候補ライブラリ・文法・外部ツールのライセンスを一次情報（リポジトリの LICENSE 本文、PyPI / crates.io / npm のメタデータ）で確認した。全表は付録 D。ここでは **方針に影響するもの** と採否を示す。法的助言ではなく事実の整理である。

### 16.2.1 注意が要るもの

| 対象 | ライセンス（確認結果） | 問題 | 採否 |
|---|---|---|---|
| pygit2 / libgit2 | GPL-2.0-only + 独自のリンク例外 | 例外は「リンクして配布してよい」旨だが、wheel を同梱再配布するなら GPL 文面と著作権表示の同梱が要る。SPDX の正式な例外 ID は無い | **第一候補にしない。** Python では dulwich（Apache-2.0 OR GPL-2.0-or-later のデュアル。Apache-2.0 側を選ぶ）を第一候補、Rust では gix（MIT OR Apache-2.0）。性能が足りないときだけ pygit2 を L-3 の手続きで検討する（§17 OD-20） |
| docutils | Public Domain / BSD / Python-2.0 に加え一部モジュールが GPL | どのファイルが GPL かが未特定 | **不採用。** reStructuredText のロール（`:func:` 等）の抽出は tree-sitter-rst（MIT）または自前の字句解析で行う |
| Universal Ctags | GPL-2.0（"or later" の有無は未確認） | 同梱すると GPL 義務 | **同梱しない。** PATH にあれば子プロセスとして使い（L-4）、無ければ内蔵のブロック推定（§8.4 段 3）だけで動く。ラダーは ctags 非依存で成立させる |
| Graphviz（`dot`） | EPL-2.0 | 弱いコピーレフト | **不要。** Lucianke は DOT テキストを生成するだけで描画しない。DOT の入力解析は tree-sitter-dot（MIT）。`pydot` / `graphviz`（PyPI）も使わない |
| tiktoken | MIT | 語彙ファイルを **実行時にダウンロード** する | **不採用**（L-6）。トークン計数はヒューリスティック近似（§5.7、§17 OD-9） |
| tree-sitter-language-pack / tree-sitter-languages | パッケージ自身は MIT / Apache-2.0 | 同梱する多数の第三者文法のライセンスが未検証 | **不採用**（L-5）。個別の `tree-sitter-<lang>` パッケージを使う。確認した 20 文法はすべて MIT（付録 D 表 B） |
| rmcp（Rust MCP SDK） | MIT から Apache-2.0 へ移行中（貢献者の同意状況で混在） | どちらも寛容。Apache-2.0 の NOTICE 義務 | 採用可（Rust を選んだ場合）。L-7 で表示 |
| jq（PyPI バインディング） | BSD-2-Clause。wheel に jq 本体（MIT）と鬼車（BSD）を同梱 | 同梱物の表示が要る | 条件付き採用。`jq:` セレクタは M2 とし、採用時に同梱物を `THIRD_PARTY_NOTICES.md` に載せる。JSONPath（jsonpath-ng、Apache-2.0）を M1 の必須経路にする |
| lxml | BSD-3-Clause。wheel に libxml2 / libxslt（MIT 系）を同梱 | 同梱物の表示 | 採用可。L-7 で表示。標準ライブラリの `xml.etree` で足りる範囲はそちらを使う |
| git 本体（GitPython 経由） | GPL-2.0 | サブプロセスなので本体に影響しない | GitPython（BSD-3-Clause）は候補に残すが、§4.2 の「シェルの git を呼ばない（SHOULD NOT）」と相反するため第一候補にしない。履歴専用の読み取りだけ `git` を子プロセスで起動する例外は OD-20 で決定済みで、GitPython は介さず直接起動する（L-4: 利用者の環境のものを起動するだけで同梱しない） |

### 16.2.2 問題の無いもの（要約）

| 区分 | 確認結果 |
|---|---|
| Python の主要候補 | mcp（MIT）、tree-sitter（MIT）、ast-grep-py（MIT）、z3-solver（MIT）、watchfiles（MIT）、sqlglot（MIT）、nbformat（BSD-3）、PyYAML（MIT）、jsonpath-ng（Apache-2.0）、pydantic（MIT）。標準ライブラリの `sqlite3`・`tomllib`・`json`・`xml.etree` はライセンス問題なし |
| Tree-sitter 文法（20 件） | python, typescript, javascript, rust, go, c, cpp, c-sharp, kotlin（fwcd）, swift（alex-pinkus）, json, yaml, toml, xml, markdown（tree-sitter-grammars）, rst（stsewd）, sql（DerekStride）, dot（rydesun）, proto（mitchellh）, graphql（bkegley）はすべて MIT |
| Rust の主要候補 | tree-sitter（MIT）、ast-grep-core（MIT）、rusqlite（MIT。bundled の SQLite はパブリックドメイン）、gix（MIT OR Apache-2.0）、notify（CC0-1.0）、petgraph・crepe・regex（MIT OR Apache-2.0）、ascent（MIT）、z3（MIT）、grep-regex（Unlicense OR MIT）、lsp-types（MIT）、tokio（MIT）、Soufflé（UPL-1.0） |
| LSP サーバー（利用者の環境で起動。同梱しない） | pyright（MIT）、basedpyright（MIT）、ruff（MIT）、vtsls（MIT）、typescript-language-server（Apache-2.0）、rust-analyzer（MIT OR Apache-2.0）、gopls（BSD-3）、clangd（Apache-2.0 WITH LLVM-exception）、csharp-ls（MIT）、OmniSharp（MIT）、kotlin-language-server（MIT）、sourcekit-lsp（Apache-2.0） |
| その他 | ripgrep（Unlicense OR MIT）、Z3 バイナリ（MIT）、SQLite（パブリックドメイン） |

### 16.2.3 未確認のもの

| 対象 | 状態 | 扱い |
|---|---|---|
| Cython の Tree-sitter 文法 | 専用文法のリポジトリを特定できていない | B1b（§8.6）の着手前に決める（§17 OD-21）。無ければ `.pyx` を Python 文法で寛容に解析する |
| Jinja2 の Tree-sitter 文法 | `tree-sitter-grammars` 配下に無い。参考の cathaysia/tree-sitter-jinja は Apache-2.0 | B7（§8.6）の着手前に決める（§17 OD-21）。Jinja2 本体（BSD-3 と推定、classifier のみ確認）は Lucianke には不要 |
| pydot / notify / z3 crate | メタデータのみ確認、LICENSE 本文は未読 | pydot は不採用。notify・z3 crate は Rust 採用時に本文を確認する |
| sourcekit-lsp | Apache-2.0。Swift 通例のランタイム例外の有無は未精査 | サブプロセスなので影響しない |
| Universal Ctags の "or later" | 未確認 | 同梱しないので影響しない |

## 16.3 配布物に含めるもの

| ファイル | 内容 |
|---|---|
| `LICENSE` | 本体のライセンス全文（MIT） |
| `THIRD_PARTY_NOTICES.md` | 依存ごとに: 名前、版、SPDX、確認した URL、デュアルの場合に選んだ側、Apache-2.0 の NOTICE 転記、wheel 同梱物（libxml2、jq、鬼車、SQLite など）の表示 |
| `docs/licenses/`（任意） | 同梱が必要な全文（Apache-2.0、BSD の著作権表示など） |

`THIRD_PARTY_NOTICES.md` は手で書かず、L-8 の検査ツールの出力から生成する（SHOULD）。実装（ec55f1b・f748e04。実装で確定 2026-10-29）: `tools/gen_notices.py` が `uv.lock` の依存木（本体と extras。dev は除く）から生成し、`--check` で最新であることを検査する。`pyproject.toml` の `license-files` で wheel に入れ、sdist は許可リスト方式にして作業用ファイル（`.claude`・`.pair-agent`・`.mcp.json`）を配らない。`tools/check_dist.py` が wheel と sdist の中身（`LICENSE`・`THIRD_PARTY_NOTICES.md` が入り、後者がリポジトリのものと一致する）を検査し、CI の license ジョブが両方を通す（M1-8）。詳細は設計書 §10.5。

---

# 17. 未決事項（Open Decisions）

師匠にしか決められない方針・設計の判断を、推奨つきで列挙する。推奨で進めてよければ返答は不要。覆す場合だけ指示をもらう。「決める時点」は、それ以降の作業が分岐する節目を示す。

| ID | 論点 | 選択肢 | 推奨と理由 | 決める時点 |
|---|---|---|---|---|
| OD-1 | 実装言語 | (a) Python、(b) Rust、(c) Python + 重い部分を Rust 拡張 | **(a) Python で M1 を作る**（推奨で進行中。2026-10-29 の報告に異論なし。アーキテクチャ設計書はこの前提で書く）。 リポジトリの `requirements.txt` が Python の `tree-sitter` を挙げている。Tree-sitter・SQLite・SMT・構文パターン・libgit2 はいずれも Python バインディングがある。Fast Path のレイテンシはキャッシュと I/O が支配的で、言語差より設計差が効く見込み。§14.4 で NFR-PERF-1 を満たさないコンポーネントが出たときだけ (c) に移す | M1 着手前 |
| OD-2 | 配布形態 | (a) `uv tool install` / `pipx`、(b) 単一バイナリ | **(a)。** OD-1 と整合し、LSP の導入も同じ経路で案内できる | M1 出口 |
| OD-3 | `query_project_db` の Datalog | (a) M1 は SQL のみ、Datalog は保留、(b) M1 から両対応 | **(a)。** 再帰 CTE で経路探索は書ける。Datalog は需要が確認できてから | 今 |
| OD-4 | SCIP インデックスの取り込み | (a) M2 で取り込みのみ MAY、(b) M1 必須、(c) 対象外 | **(a)。** 大規模リポジトリのコールドスタート短縮に効くが、M1 の価値には不要 | M2 着手前 |
| OD-5 | 取り消しツール | (a) `undo_edit` を 22 本目として M2 で追加、(b) 追加しない | **(a)。** ジャーナル（§10.6）があるので実装は小さく、エージェントの自己修正ループが短くなる | M2 着手前 |
| OD-6 | URN に revision を含めるか | (a) 含めない（本書）、(b) 含める | **(a)。** 編集でずれない安定識別子が主目的。断面はヘッダと `_resolution_meta` が示す | 今 |
| OD-7 | M1 の Tier 1 必須言語 | (a) Python + TypeScript/JavaScript 必須、他は best effort、(b) 10 言語すべて必須 | **(a)。** 受入のフィクスチャを 2 言語に絞れる。他言語は LSP の存在に依存するため、必須にすると受入が環境に揺れる | 今 |
| OD-8 | M1 の Python 周辺ブリッジ範囲 | §8.6 の表のとおり、(a) B1a・B2 読み取り・B3 出力・B4 骨格/部分取得を M1、(b) 全ブリッジを M1 | **(a)。** `.ipynb` と構造化データの骨格が文脈節約に最も効く。SQL / IDL / テンプレートの整合は M2 | 今 |
| OD-9 | トークン計数器 | (a) ヒューリスティック近似（誤差 ±10% を §14 で確認）、(b) トークナイザライブラリ依存 | **(a)。** 依存を増やさず、予算制御の目的には十分。tiktoken は語彙を実行時にダウンロードするため §13.7 と第 16 章 L-6 に反し、採れない | M1 着手前 |
| OD-10 | ベンチマークのフィクスチャ | F-PY、F-TS に使う公開リポジトリ | 候補は作業開始時に規模（行数・ファイル数・テストの有無）を実測して 3 つずつ挙げ、師匠が選ぶ。**推奨は、師匠が日常で触る種類のプロジェクトに近いもの** | M1 の受入設計前 |
| OD-11 | 準拠する MCP 仕様の版 | 概念文書は 2026-07-28 版を参照 | **実装着手時点の最新安定版を 1 つ固定し、§0 の表に書く。** 版が変わる機能（`structuredContent`、`roots`）は固定版の範囲で使う | M1 着手前 |
| OD-12 | 秘密らしきファイルの既定 | (a) 本文を索引しない（§13.5）、(b) 通常どおり索引 | **(a)。** 文脈に秘密値が流れる経路を既定で閉じる | 今 |
| OD-13 | `.lucianke.toml` の採否 | (a) 読むだけで採用、(b) 採用しない（環境変数とツールのみ） | **(a)。** リポジトリに設定を同梱でき、チームで同じ除外規則を共有できる。書き込みはしない | M1 出口 |
| OD-14 | `TODO.md` の扱い | (a) 本書の M1〜M3 に合わせて書き直す、(b) 現状維持 | **(a)。** 二重管理を避ける。書き直しは師匠の指示があってから行う | 今 |
| OD-15 | T-06 の設定ファイル横断 grep | (a) `include_lexical_in_other_files`（既定 false）で明示オプトイン、(b) 概念文書どおり自動フォールバック | **(a)。** 設定ファイル中の同名文字列が参照一覧を汚し、トークンを浪費する。必要なときだけ広げる | 今 |
| OD-16 | T-20 の `auto_fallback_grep_pattern` | (a) 指定があれば必ず一時テーブルを作る（本書）、(b) 結果 0 件のときだけ自動実行 | **(a)。** 「0 件のとき自動で」は挙動が結果に依存して読みにくい。明示のほうが決定論的 | 今 |
| OD-17 | T-16 の対象外 | `async` 関数とジェネレータを M3 の範囲外（`E_UNSUPPORTED`）とする | **範囲外のまま M3 を閉じ、需要があれば M4 で扱う** | M3 着手前 |
| OD-18 | LSP 不在時の Code Action | (a) 提供しない（T-13 は `E_LSP_UNAVAILABLE`）、(b) Tree-sitter だけで安全に行える数種（未使用 import の除去など）を内蔵する | **(a) で M1 を閉じる。** 需要が見えたら (b) を M2 以降で | M2 着手前 |
| OD-19 | 本体のライセンス | (a) MIT、(b) Apache-2.0、(c) MIT OR Apache-2.0 のデュアル | **決定済み: (a) MIT**（師匠 2026-10-29「ライセンスは MIT に確定」）。リポジトリ直下に `LICENSE` を置いた。依存側の確認は第 16 章 | 決定済み |
| OD-20 | Git 層のライブラリ（Python を選んだ場合） | (a) dulwich（Apache-2.0 側を選択）、(b) pygit2（GPL-2.0-only + リンク例外。GPL 文面の同梱が要る）、(c) GitPython（BSD-3。シェルの git を起動） | **(a) を第一候補。** 純 Python で寛容。M2 のベンチマーク（NFR-PERF-2）で足りなければ (b) を第 16 章 L-3 の手続きで検討する。(c) は §4.2 の方針と相反。アーキテクチャ設計書 §11.1 は dulwich を M1 の必須依存に置いた（T-02 / T-05 の revision 解決に要る）。**S-8 の結果**（2026-10-29）: M1 の用途（参照解決・blob・`STAGED`・`.gitignore`・不変性）は dulwich で合格（1〜5 ms）。M2 の履歴だけ、dulwich の `Walker` が F-PY 規模で 13 秒超（`git log --follow` は 106 ms）で不合格。M2 の履歴は「(a) ＋ 履歴専用の `git` 子プロセス（読み取り専用。`git` が無い環境では (a) の dulwich で走査する〔`Walker` そのものではなく、自前の path 限定 walk を `max_commits` で区切る。設計書 §7.9.2〕）」に**決定**（師匠 2026-10-29「推奨: 読み取り専用で使うことを認る」）。`git` は別プロセスとして起動するだけで同梱しない（第 16 章の GPL 同梱禁止に触れない）。(b) pygit2 は採らない | **決定**（M1 は (a)、M2 の履歴は (a) ＋ `git` 子プロセス〔読み取り専用〕） |
| OD-22 | HTTP 系トランスポート | (a) 提供しない（stdio のみ）、(b) Streamable HTTP を任意で提供 | **決定済み: (a)**（師匠 2026-10-29「stdio のコマンド実行型でできるよね？ HTTP 型はあまり望ましくない」）。配布物に HTTP サーバーの起動経路を持たない | 決定済み |
| OD-21 | Cython と Jinja2 の Tree-sitter 文法 | Cython: 専用文法が未特定。Jinja2: 公式系に無く、参考の cathaysia/tree-sitter-jinja は Apache-2.0 | **着手前に一次情報で確認して決める。** 見つからなければ Cython は Python 文法で寛容に解析、Jinja2 は自前の字句解析で `{{ }}` / `{% %}` だけを拾う | B1b / B7 着手前（M2） |

---

# 付録 A. サンプルクエリ

`query_project_db`（T-20）に渡せる SQL の例。いずれも公開テーブル（§9.2）だけを使う。

**A-1. 直近 10 コミットで変わった関数のうち、3 か所以上から呼ばれ、テストから参照が無く、エラー診断を持つもの（上位 5）**

```sql
WITH recent AS (
  SELECT commit_sha FROM git_commits ORDER BY committed_at DESC LIMIT 10
)
SELECT s.urn, f.path, s.signature,
       COUNT(DISTINCT r_in.source_urn) AS caller_count,
       MAX(d.message)                  AS current_error
FROM symbols s
JOIN files f               ON f.file_id = s.file_id
JOIN git_symbol_changes g  ON g.urn = s.urn AND g.commit_sha IN (SELECT commit_sha FROM recent)
LEFT JOIN relations r_in   ON r_in.target_urn = s.urn AND r_in.relation_kind = 'CALLS'
LEFT JOIN diagnostics d    ON d.enclosing_urn = s.urn AND d.severity = 'error'
WHERE s.kind IN ('Function', 'Method')
  AND NOT EXISTS (
    SELECT 1 FROM relations rt JOIN files ft ON ft.file_id = rt.source_file_id
    WHERE rt.target_urn = s.urn AND ft.is_test = 1
  )
GROUP BY s.urn
HAVING caller_count >= 3 OR current_error IS NOT NULL
ORDER BY caller_count DESC
LIMIT 5;
```

**A-2. 公開メソッドのうち Doc 要約が無いもの（ファイル別件数）**

```sql
SELECT f.path, COUNT(*) AS undocumented
FROM symbols s JOIN files f ON f.file_id = s.file_id
WHERE s.kind = 'Method'
  AND s.modifiers_json LIKE '%"pub"%'
  AND (s.doc_summary IS NULL OR s.doc_summary = '')
GROUP BY f.path
ORDER BY undocumented DESC
LIMIT 20;
```

**A-3. あるファイルと一緒に変わりやすいファイル（共変更結合 0.5 以上）**

```sql
SELECT entity_b AS path_urn, coupling_ratio, co_change_count, last_co_changed_sha
FROM git_co_changes
WHERE entity_a = 'lk://app/src/services/payment.py'
  AND entity_kind = 'file'
  AND coupling_ratio >= 0.5
ORDER BY coupling_ratio DESC;
```

**A-4. 推移的な呼び出し元（深さ 3 まで）を再帰 CTE で辿る**

```sql
WITH RECURSIVE callers(urn, depth) AS (
  SELECT :target, 0
  UNION
  SELECT r.source_urn, c.depth + 1
  FROM relations r JOIN callers c ON r.target_urn = c.urn
  WHERE r.relation_kind = 'CALLS' AND c.depth < 3
)
SELECT c.urn, c.depth, s.signature, s.confidence_level
FROM callers c JOIN symbols s ON s.urn = c.urn
WHERE c.depth > 0
ORDER BY c.depth, c.urn;
```

**A-5. 降格（`exact` 以外）で索引されているファイルの一覧と理由になりうる言語**

```sql
SELECT language, tier, confidence_level, COUNT(*) AS files
FROM files
WHERE confidence_level <> 'exact'
GROUP BY language, tier, confidence_level
ORDER BY files DESC;
```

---

# 付録 B. 概念文書 v3.0 からの変更点

| 領域 | 概念文書 | 本書 | 理由 |
|---|---|---|---|
| 名称・版 | DLSR-MCP、v2.0 / v2.2 / v3.0 が混在 | Lucianke、1.0（Draft） | 正本を 1 つにする |
| ツール数 | 見出し「全 5 カテゴリ・16 ツール」と本文「全 7 カテゴリ・21 ツール」 | 21 ツール、カテゴリ 0 と A〜G、`T-00`〜`T-20` | 不一致の解消 |
| カテゴリ順 | G（5.19, 5.20）が F（5.17, 5.18）の前 | 番号順（E → F → G） | 可読性 |
| 共通プロトコル | 共通パラメータの列挙のみ | エンベロープ、`_resolution_meta` の全フィールド、ページネーション、エラー分類、バッチ部分失敗、冪等性を規範化（第 5 章） | 失敗時の形が無いと実装も受入もできない |
| Compact DSL | 例のみ | EBNF と規約（§6.1） | 全ツールの出力の正本 |
| URN | SCIP 形式の例を借用 | `lk://` スキームと Tier 3〜4 のセレクタ、安定性の規約（第 7 章） | Tier 3〜4 を同じ入口で指す必要 |
| 編集の安全規約 | 概念文書の 2.3(2)・4.4・5.10〜5.13 に分散 | 第 10 章に集約。8 段の手順、陳腐化書き込みの防止、原子的書き込みとジャーナル、診断差分の定義、境界規則、正規化、事後条件 EP-1〜9 | README の核心的価値「決定論的編集プロトコル」の正本 |
| ライセンス | 記述なし | 第 16 章に方針（本体 MIT、依存は寛容のみ、コピーレフトは同梱しない）と一次情報で確認した依存の採否。全表は付録 D | 師匠の指示（2026-10-29）: できるだけ自由なライセンスにする。依存の実態も含めて確認する |
| 降格インターロック | `dry_run` 強制と `allow_heuristic_mutation` | 同じ。加えて `structural` 水準でのリネームの扱い、Tier 3 置換のバイト保存を規定 | |
| ファイル監視 | 「5 ms 以内に検知」 | 検知から反映まで 500 ms 以内（測定手順つき）。解決と書き込みの間の競合を前提検証で防ぐ | 5 ms は測定方法が無く、競合の問題は検知速度では解けない |
| 列番号 | 「UTF-8 文字単位」 | Unicode コードポイント単位、1 始まり（§5.10） | バイトとコードポイントの曖昧さを除く |
| PaaD | 列名の抜粋 | DDL（主キー・外部キー・索引・`CHECK`）、更新規約、公開と保護（第 9 章） | `query_project_db` の正本 |
| KPI | 数値の宣言（75〜90%、p95 50 ms、99.9%） | 閾値と測定手順（第 14 章）。概念文書第 8 章の数値は仮説として報告対象に | 閾値は測り方が決まって初めて合否になる |
| ロードマップ | フェーズ 1〜3 に「2〜3 週間」などの期間 | M1〜M3 を出口条件で定義。期間を書かない | 作業者がエージェントである以上、人の暦の見積もりは測っていない（師匠の既定ルール） |
| 技術スタック | Rust 推奨、crate 名を本文に | 規範は言語中立。候補スタックは §4.6 の参考表、選定は OD-1 | 未確認の能力を規範にしない |
| Datalog | `query_project_db` で SQL / Datalog 両対応 | M1 は SQL のみ、Datalog は OD-3 | 需要未確認 |
| T-06 の設定ファイル横断 grep | 自動フォールバック | 明示オプトイン（OD-15） | 参照一覧の汚染を防ぐ |
| T-20 の grep 一時テーブル | 結果 0 件で自動 | 指定時に必ず（OD-16） | 決定論性 |
| Python 周辺ブリッジ | 7 項目の説明 | B1〜B7 として水準とマイルストーンを付与（§8.6） | 受入の単位にする |
| 秘密ファイル | 記述なし | 既定で本文を索引しない（§13.5、OD-12） | |
| 自然言語仕様の論理式化 | ホスト側エージェントの責務 | 同じ（§1.3.2、§13.7 で LLM 不使用を受入条件に） | |

---

# 付録 C. 参考文献

1. Model Context Protocol — Specification（Transports, Tools, Roots）: https://modelcontextprotocol.io/specification/
2. Language Server Protocol — Specification 3.17 / 3.18（`positionEncodings`, `textDocument/rename`, `callHierarchy`, `codeAction`, `publishDiagnostics`）: https://microsoft.github.io/language-server-protocol/specifications/
3. Tree-sitter — Incremental Parsing System: https://tree-sitter.github.io/
4. ast-grep — Structural Search and Rewrite: https://ast-grep.github.io/
5. SCIP — Source Code Intelligence Protocol（Sourcegraph）: https://github.com/sourcegraph/scip
6. SQLite — WAL Mode, FTS5, Recursive CTE, Authorizer, `query_only`: https://www.sqlite.org/docs.html
7. Z3 Theorem Prover: https://github.com/z3prover/z3
8. Universal Ctags: https://github.com/universal-ctags/ctags
9. ripgrep / `grep-regex`: https://github.com/BurntSushi/ripgrep
10. gitoxide（`gix`）: https://github.com/GitoxideLabs/gitoxide ／ libgit2 / pygit2: https://github.com/libgit2/pygit2
11. Soufflé Datalog（経路探索エンジンの候補の一つ）: https://github.com/souffle-lang/souffle
12. GitHub CodeQL — "Treat code like data"（PaaD の着想元）: https://codeql.github.com/
13. Meta Glean（コードファクトのデータベース化）: https://github.com/facebookincubator/Glean
14. 概念文書: 『決定論的プロジェクト知識DB・LSP統合クエリ＆論理解析MCPサーバー完全仕様書 (v3.0: Project-as-a-Database Architecture)』（本リポジトリ `docs/`）
15. 初期たたき台（v1.0）: 概念文書 §10 に記載の Google ドキュメント

外部仕様の版は OD-11 で固定し、固定後に本表へ版番号を書き入れる。

---

# 付録 D. 依存ライセンス調査（一次情報・2026-10-29）

調査日: 2026-10-29。法的助言ではなく事実の整理。採否と方針は第 16 章。表 D.1〜D.4 の「分類」は本調査での整理であり、L-2〜L-4 の判定は第 16 章に従う。
確認方法: PyPI JSON API（`license` / `license_expression` / classifiers）、crates.io API（最新版の `license`）、npm registry、GitHub リポジトリの LICENSE ファイル本文（raw.githubusercontent.com）、Graphviz は GitLab の LICENSE。
「確認済み」= 上記いずれかを実際に取得して読んだもの。SPDX は取得した文面・メタデータから判断（PyPI の古い形式のものは license 欄の文字列や LICENSE 本文からの判断）。

## D.1 Python 側

| 名前 | 用途 | SPDX | 分類 | 確認状態 | 確認 URL | 備考 |
|---|---|---|---|---|---|---|
| mcp | MCP SDK | MIT | 寛容 | 確認済み | https://pypi.org/pypi/mcp/json ; https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE | LICENSE 本文は MIT（Copyright Anthropic, PBC）。Rust SDK は Apache-2.0 へ移行中なので、Python SDK も将来変わりうる（現時点では MIT） |
| tree-sitter (py-tree-sitter) | 構文解析バインディング | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/py-tree-sitter/blob/master/LICENSE ; PyPI classifiers | - |
| tree-sitter-language-pack | 文法一括バンドル | MIT（パッケージ自身） | 寛容（同梱文法は別） | 確認済み（パッケージ本体のみ） | https://pypi.org/pypi/tree-sitter-language-pack/json ; https://github.com/Goldziher/tree-sitter-language-pack/blob/main/LICENSE | 多数の第三者文法を同梱・取得するため、同梱文法ごとのライセンスは本表では未検証。再配布するなら同梱文法一覧のライセンス確認が必要 |
| tree-sitter-languages | 文法一括バンドル | Apache-2.0 | 寛容 | 確認済み | https://pypi.org/pypi/tree-sitter-languages/json ; https://github.com/grantjenks/py-tree-sitter-languages/blob/main/LICENSE | 同梱文法は別ライセンスの可能性（未検証） |
| ast-grep-py | ast-grep の Python 版 | MIT | 寛容 | 確認済み | PyPI classifiers / license 欄 ; https://github.com/ast-grep/ast-grep/blob/main/LICENSE | - |
| z3-solver | SMT ソルバ（Python 配布） | MIT | 寛容 | 確認済み | https://pypi.org/pypi/z3-solver/json ; https://github.com/Z3Prover/z3/blob/master/LICENSE.txt | - |
| pygit2 | libgit2 の Python バインディング | GPL-2.0-only + linking exception（PyPI 表記 "GPLv2 with linking exception"） | 強いコピーレフト＋リンク例外（弱いコピーレフト相当） | 確認済み | https://pypi.org/pypi/pygit2/json ; https://github.com/libgit2/pygit2/blob/master/COPYING | COPYING に "LINKING EXCEPTION": コンパイル済み版を他プログラムにリンクし、その組み合わせを配布することに制限なし。ただし「GPL の制限はそれ以外（改変など）には適用」と明記。正式な SPDX 例外 ID は無い（独自例外）。wheel を再配布するなら GPL 文面と著作権表示の同梱が必要 |
| libgit2 | Git ライブラリ（C） | GPL-2.0-only + linking exception | 同上 | 確認済み | https://github.com/libgit2/libgit2/blob/main/COPYING | GPL "v2 のみ"（v3 不可）と明記。リンク例外あり。pygit2 の wheel は libgit2 を同梱 |
| dulwich | 純 Python Git | Apache-2.0 OR GPL-2.0-or-later | デュアル | 確認済み | https://pypi.org/pypi/dulwich/json | Apache-2.0 側を選べば寛容 |
| GitPython | Git ラッパ | BSD-3-Clause | 寛容 | 確認済み | https://pypi.org/pypi/GitPython/json ; https://github.com/gitpython-developers/GitPython/blob/main/LICENSE | 実行時に git バイナリ（GPL-2.0）をサブプロセス起動。影響なし |
| watchfiles | ファイル監視 | MIT | 寛容 | 確認済み | https://pypi.org/pypi/watchfiles/json | - |
| sqlglot | SQL パーサ | MIT | 寛容 | 確認済み | https://pypi.org/pypi/sqlglot/json | - |
| nbformat | Notebook 形式 | BSD-3-Clause | 寛容 | 確認済み | https://pypi.org/pypi/nbformat/json | - |
| PyYAML | YAML | MIT | 寛容 | 確認済み | https://pypi.org/pypi/PyYAML/json | - |
| lxml | XML | BSD-3-Clause | 寛容 | 確認済み | https://pypi.org/pypi/lxml/json | wheel は libxml2/libxslt（MIT 系）を静的同梱。同梱物の個別確認は未実施 |
| jsonpath-ng | JSONPath | Apache-2.0 | 寛容 | 確認済み | https://pypi.org/pypi/jsonpath-ng/json | - |
| jq（PyPI バインディング） | jq バインディング | BSD-2-Clause（PyPI 表記） | 寛容 | 確認済み | https://pypi.org/pypi/jq/json ; https://github.com/mwilliamson/jq.py/blob/master/LICENSE | LICENSE は "Copyright (c) 2013, Michael Williamson" の BSD 形式。wheel は jq 本体（MIT）と oniguruma を同梱するはず（同梱内容は未検証） |
| jq 本体 | JSON 処理（C） | MIT（+ 同梱の ICU License、Lucent 表記等） | 寛容 | 確認済み | https://github.com/jqlang/jq/blob/master/COPYING | COPYING に jq 本体 MIT の他、ICU License・Lucent 表記が併記。サブプロセスなら影響なし |
| pydantic | データ検証 | MIT | 寛容 | 確認済み | https://pypi.org/pypi/pydantic/json | - |
| tiktoken | トークナイザ | MIT | 寛容 | 確認済み | https://pypi.org/pypi/tiktoken/json | license 欄が MIT 本文（OpenAI, Shantanu Jain）。語彙ファイルは実行時に別途ダウンロードされる（そのライセンスは未検証） |
| pydot | DOT 操作 | MIT | 寛容 | 確認済み（PyPI のみ） | https://pypi.org/pypi/pydot/json | GitHub の LICENSE 本文は取得できず未読 |
| graphviz（PyPI） | dot ラッパ | MIT | 寛容 | 確認済み | https://pypi.org/pypi/graphviz/json | 実行時に Graphviz 本体（EPL-2.0）をサブプロセス起動 |
| Jinja2 | テンプレート | BSD-3-Clause と推定 | 寛容 | 確認済み（classifier のみ） | https://pypi.org/pypi/Jinja2/json | classifier は "BSD License" だけで 2/3-Clause を区別できない。LICENSE 本文は未読 |
| docutils | reST 処理 | 複合: Public Domain / BSD-2-Clause / Python-2.0 / GPL（一部モジュール） | パブリックドメイン＋寛容＋一部 GPL | 確認済み（classifier のみ） | https://pypi.org/pypi/docutils/json | classifiers に BSD・GPL・Public Domain が併記。GPL は一部モジュール由来。どのファイルが GPL かは未特定。同梱せず依存として pip で入れる形が無難 |

## D.2 Tree-sitter 文法

全て GitHub の LICENSE 本文の先頭（名称・著作権者・許諾文）を読んで確認。

| 名前 | 用途 | SPDX | 分類 | 確認状態 | 確認 URL | 備考 |
|---|---|---|---|---|---|---|
| tree-sitter-python | Python 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/tree-sitter-python/blob/master/LICENSE | - |
| tree-sitter-typescript | TS/TSX 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/tree-sitter-typescript/blob/master/LICENSE | - |
| tree-sitter-javascript | JS 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/tree-sitter-javascript/blob/master/LICENSE | - |
| tree-sitter-rust | Rust 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/tree-sitter-rust/blob/master/LICENSE | - |
| tree-sitter-go | Go 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/tree-sitter-go/blob/master/LICENSE | - |
| tree-sitter-c | C 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/tree-sitter-c/blob/master/LICENSE | - |
| tree-sitter-cpp | C++ 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/tree-sitter-cpp/blob/master/LICENSE | - |
| tree-sitter-c-sharp | C# 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/tree-sitter-c-sharp/blob/master/LICENSE | - |
| tree-sitter-kotlin (fwcd) | Kotlin 文法 | MIT | 寛容 | 確認済み | https://github.com/fwcd/tree-sitter-kotlin/blob/main/LICENSE | - |
| tree-sitter-swift (alex-pinkus) | Swift 文法 | MIT | 寛容 | 確認済み | https://github.com/alex-pinkus/tree-sitter-swift/blob/main/LICENSE | - |
| tree-sitter-json | JSON 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter/tree-sitter-json/blob/master/LICENSE | - |
| tree-sitter-yaml (grammars) | YAML 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter-grammars/tree-sitter-yaml/blob/master/LICENSE | 著作権者: tree-sitter-grammars contributors / Ika |
| tree-sitter-toml (grammars) | TOML 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter-grammars/tree-sitter-toml/blob/master/LICENSE | 著作権者: Ika |
| tree-sitter-xml (grammars) | XML 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter-grammars/tree-sitter-xml/blob/master/LICENSE | 著作権者: ObserverOfTime（許諾文は MIT 型） |
| tree-sitter-markdown (grammars) | Markdown 文法 | MIT | 寛容 | 確認済み | https://github.com/tree-sitter-grammars/tree-sitter-markdown/blob/split_parser/LICENSE | 著作権者: Matthias Deiml |
| tree-sitter-rst (stsewd) | reST 文法 | MIT | 寛容 | 確認済み | https://github.com/stsewd/tree-sitter-rst/blob/master/LICENSE | - |
| tree-sitter-sql (DerekStride) | SQL 文法 | MIT | 寛容 | 確認済み | https://github.com/DerekStride/tree-sitter-sql/blob/main/LICENSE | - |
| tree-sitter-dot (rydesun) | DOT 文法 | MIT | 寛容 | 確認済み | https://github.com/rydesun/tree-sitter-dot/blob/main/LICENSE | - |
| tree-sitter-proto (mitchellh) | Protobuf 文法 | MIT | 寛容 | 確認済み | https://github.com/mitchellh/tree-sitter-proto/blob/main/LICENSE | treywood/tree-sitter-proto も MIT（同一文面）を確認 |
| tree-sitter-graphql (bkegley) | GraphQL 文法 | MIT | 寛容 | 確認済み | https://github.com/bkegley/tree-sitter-graphql/blob/master/LICENSE | - |
| tree-sitter-cython | Cython 文法 | - | - | 未確認 | - | 専用の文法リポジトリを特定できず（検索未実施）。本体 Cython は Apache-2.0（https://github.com/cython/cython/blob/master/LICENSE.txt）だが文法とは別物 |
| tree-sitter-jinja2 | Jinja2 文法 | - | - | 見つからず | - | tree-sitter-grammars/tree-sitter-jinja2 は存在せず（取得不能）。参考: cathaysia/tree-sitter-jinja は Apache-2.0（LICENSE 本文 Apache License 2.0）を確認したが、採用候補かは別 |

## D.3 Rust 側

crates.io API の最新版 `license` フィールドを確認（版は括弧内）。

| 名前 | 用途 | SPDX | 分類 | 確認状態 | 確認 URL | 備考 |
|---|---|---|---|---|---|---|
| rmcp | MCP SDK (Rust) | Apache-2.0 (3.8.0) | 寛容 | 確認済み | https://crates.io/api/v1/crates/rmcp ; https://github.com/modelcontextprotocol/rust-sdk/blob/main/LICENSE | LICENSE 先頭に「MIT から Apache-2.0 への移行中。同意取得済み貢献は Apache-2.0、未取得は元の MIT のまま」と明記。どちらも寛容 |
| tree-sitter (crate) | 構文解析 | MIT (0.27.1) | 寛容 | 確認済み | https://crates.io/api/v1/crates/tree-sitter ; https://github.com/tree-sitter/tree-sitter/blob/master/LICENSE | - |
| ast-grep-core | AST 検索 | MIT (0.51.0) | 寛容 | 確認済み | https://crates.io/api/v1/crates/ast-grep-core ; https://github.com/ast-grep/ast-grep/blob/main/LICENSE | - |
| rusqlite | SQLite バインディング | MIT (0.41.1) | 寛容 | 確認済み | https://crates.io/api/v1/crates/rusqlite ; https://github.com/rusqlite/rusqlite/blob/master/LICENSE | bundled 機能で SQLite（パブリックドメイン）を同梱 |
| gix (gitoxide) | Git 実装 | MIT OR Apache-2.0 (0.90.0) | デュアル | 確認済み | https://crates.io/api/v1/crates/gix ; https://github.com/GitoxideLabs/gitoxide/blob/main/LICENSE-MIT | - |
| notify | ファイル監視 | CC0-1.0 (9.0.0-rc.7) | パブリックドメイン相当 | 確認済み（crates.io メタデータのみ） | https://crates.io/api/v1/crates/notify | 最新が rc 版。GitHub の LICENSE ファイルは取得できず未読 |
| petgraph | グラフ | MIT OR Apache-2.0 (0.8.3) | デュアル | 確認済み | https://crates.io/api/v1/crates/petgraph ; https://github.com/petgraph/petgraph/blob/master/LICENSE-MIT | - |
| crepe | Datalog マクロ | MIT OR Apache-2.0 (0.2.0) | デュアル | 確認済み | https://crates.io/api/v1/crates/crepe ; https://github.com/ekzhang/crepe/blob/master/LICENSE-MIT | - |
| ascent | Datalog マクロ | MIT (0.8.1) | 寛容 | 確認済み | https://crates.io/api/v1/crates/ascent ; https://github.com/s-arash/ascent/blob/master/LICENSE | - |
| z3 (crate) | Z3 バインディング | MIT (0.22.0) | 寛容 | 確認済み（crates.io のみ） | https://crates.io/api/v1/crates/z3 | GitHub の LICENSE ファイルは取得できず未読。Z3 本体は MIT |
| grep-regex | ripgrep の正規表現層 | Unlicense OR MIT (0.1.14) | デュアル（PD 相当 / 寛容） | 確認済み | https://crates.io/api/v1/crates/grep-regex | - |
| regex | 正規表現 | MIT OR Apache-2.0 (1.13.1) | デュアル | 確認済み | https://crates.io/api/v1/crates/regex ; https://github.com/rust-lang/regex/blob/master/LICENSE-MIT | - |
| lsp-types | LSP 型定義 | MIT (0.97.0) | 寛容 | 確認済み | https://crates.io/api/v1/crates/lsp-types ; https://github.com/gluon-lang/lsp-types/blob/master/LICENSE | - |
| tokio | 非同期ランタイム | MIT (1.54.1) | 寛容 | 確認済み | https://crates.io/api/v1/crates/tokio ; https://github.com/tokio-rs/tokio/blob/master/LICENSE | - |
| Soufflé | Datalog エンジン（C++） | UPL-1.0 | 寛容 | 確認済み | https://github.com/souffle-lang/souffle/blob/master/LICENSE | LICENSE 本文は "The Universal Permissive License (UPL), Version 1.0"（Oracle）。サブプロセス利用なら影響小。ビルド依存物のライセンスは未調査 |

## D.4 外部ツール（サブプロセス起動想定）

| 名前 | 用途 | SPDX | 分類 | 確認状態 | 確認 URL | 備考 |
|---|---|---|---|---|---|---|
| pyright | Python 型検査/LSP | MIT | 寛容 | 確認済み | https://github.com/microsoft/pyright/blob/main/LICENSE.txt ; https://registry.npmjs.org/pyright/latest | - |
| basedpyright | pyright フォーク | MIT | 寛容 | 確認済み | https://github.com/DetachHead/basedpyright/blob/main/LICENSE.txt ; PyPI classifiers | Microsoft の MIT 文面を含む |
| ruff | Python Linter | MIT | 寛容 | 確認済み | https://github.com/astral-sh/ruff/blob/main/LICENSE ; https://pypi.org/pypi/ruff/json | LICENSE 末尾に同梱第三者コードの個別表記が続く（全体は未精査） |
| vtsls | TS LSP | MIT | 寛容 | 確認済み | https://github.com/yioneko/vtsls/blob/main/LICENSE ; npm @vtsls/language-server | - |
| typescript-language-server | TS LSP | Apache-2.0 | 寛容 | 確認済み | npm registry typescript-language-server ; package.json | LICENSE ファイルには VS Code 由来コード用の MIT 注記が併記 |
| rust-analyzer | Rust LSP | MIT OR Apache-2.0 | デュアル | 確認済み（LICENSE-MIT / LICENSE-APACHE の両ファイルを確認、Cargo の SPDX 式は未読） | https://github.com/rust-lang/rust-analyzer/blob/master/LICENSE-APACHE | - |
| gopls | Go LSP | BSD-3-Clause | 寛容 | 確認済み（Go Authors の BSD 型文面。3-Clause と判断） | https://github.com/golang/tools/blob/master/LICENSE | - |
| clangd (LLVM) | C/C++ LSP | Apache-2.0 WITH LLVM-exception | 寛容 | 確認済み | https://github.com/llvm/llvm-project/blob/main/LICENSE.TXT | "LLVM Project is under the Apache License v2.0 with LLVM Exceptions" |
| csharp-ls | C# LSP | MIT | 寛容 | 確認済み | https://github.com/razzmatazz/csharp-language-server/blob/main/LICENSE | - |
| OmniSharp (omnisharp-roslyn) | C# LSP | MIT | 寛容 | 確認済み | https://github.com/OmniSharp/omnisharp-roslyn/blob/master/license.md | 著作権者 .NET Foundation and Contributors。依存の Roslyn 等は別途 |
| kotlin-language-server (fwcd) | Kotlin LSP | MIT | 寛容 | 確認済み | https://github.com/fwcd/kotlin-language-server/blob/main/LICENSE.txt | - |
| sourcekit-lsp | Swift LSP | Apache-2.0 | 寛容 | 確認済み（LICENSE.txt は Apache License 2.0。Swift 通例のランタイムライブラリ例外の有無は未精査） | https://github.com/swiftlang/sourcekit-lsp/blob/main/LICENSE.txt | - |
| Universal Ctags | タグ生成 | GPL-2.0（COPYING は GPL v2 本文。"or later" かは未確認） | 強いコピーレフト | 確認済み（COPYING の種別のみ） | https://github.com/universal-ctags/ctags/blob/master/COPYING | サブプロセス起動のみなら本体ライセンスに影響しない。同梱再配布は GPL 義務が発生 |
| ripgrep | 検索 | Unlicense OR MIT | パブリックドメイン／寛容デュアル | 確認済み（UNLICENSE 本文と grep-regex のデュアル表記。MIT ファイルは未読） | https://github.com/BurntSushi/ripgrep/blob/master/UNLICENSE | - |
| Graphviz（dot） | グラフ描画 | EPL-2.0 | 弱いコピーレフト | 確認済み | https://gitlab.com/graphviz/graphviz/-/raw/main/LICENSE | 先頭 "Eclipse Public License - v 2.0"。サブプロセスなら影響なし。バイナリ同梱時は EPL の条件に注意 |
| Z3（バイナリ） | SMT ソルバ | MIT | 寛容 | 確認済み | https://github.com/Z3Prover/z3/blob/master/LICENSE.txt | - |
| SQLite | DB | パブリックドメイン（SPDX 識別子なし） | パブリックドメイン | 確認済み | https://github.com/sqlite/sqlite/blob/master/LICENSE.md ; https://sqlite.org/copyright.html | "SQLite Is Public Domain" |

## D.5 MIT で公開する本体にとって注意が要るもの

コピーレフト・例外つき
- pygit2 / libgit2: GPL-2.0-only + リンク例外。例外は「リンクして配布してよい」旨だが、wheel を同梱再配布するなら GPL 文面・著作権表示の同梱が要る。回避したいなら dulwich（Apache-2.0 側を選択）、gix、またはサブプロセスの git を使う。
- dulwich: Apache-2.0 OR GPL-2.0-or-later のデュアル。Apache-2.0 側で利用する旨を明確に。
- docutils: 一部モジュールが GPL。どのファイルかは未特定。依存として pip 解決に任せ、同梱しない。
- Universal Ctags: GPL-2.0。サブプロセス起動に留め、バイナリ同梱は避ける。
- Graphviz: EPL-2.0（弱いコピーレフト）。サブプロセスなら影響なし、同梱は条件あり。
- git 本体（GitPython 経由で起動）: GPL-2.0。サブプロセスなので影響なし（一般知識。本調査では取得していない）。

寛容だが表示義務・混在あり
- rmcp: Apache-2.0 へ移行中で、未同意の貢献は MIT のまま。どちらも寛容。Apache-2.0 の NOTICE 義務に注意。
- clangd: Apache-2.0 WITH LLVM-exception。通常の Apache-2.0 の表示義務。
- Soufflé: UPL-1.0（寛容）。
- notify: CC0-1.0。最新が rc 版。
- jq バインディング / lxml / rusqlite(bundled) / pygit2 の wheel: 他ライブラリを同梱するため、同梱物の表示が必要になりうる（個別未検証）。

同梱文法
- tree-sitter-language-pack と tree-sitter-languages は第三者文法を同梱するため、同梱物ごとのライセンスは未検証。個別文法（表 B）は確認した範囲で全て MIT（Cython・Jinja2 は除く）。

未確認・見つからず
- tree-sitter-cython: 文法リポジトリ未特定。
- tree-sitter-jinja2: tree-sitter-grammars 配下に無し（参考の cathaysia/tree-sitter-jinja は Apache-2.0）。
- pydot / notify / z3 crate: GitHub の LICENSE 本文は取得できず、PyPI / crates.io のメタデータのみ確認。
- Jinja2: classifier のみ（BSD の 2/3-Clause 区別は未読）。docutils: classifier のみ。
- Universal Ctags: GPL-2.0 の "or later" 有無未確認。sourcekit-lsp: ランタイム例外の有無未精査。
