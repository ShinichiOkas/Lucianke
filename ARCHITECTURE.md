# Lucianke アーキテクチャとコードベース構造解説

## 1. プロジェクト概要

**Lucianke（Lucid + Ananke）** は、AIコーディングエージェントにおける「トークン消費の浪費」と「作業時間の損失」を根本的に解消するために設計された、高効率な **コード解析・編集支援 LSP-MCP サーバー** です。

- **目的**: 
  - ファイル全体を読み込ませる従来方式をやめ、Tree-sitterによる **ASTベースの骨格抽出（Skeleton）** とSQLiteによる **事前索引（Index）** により、コンテキスト窓を節約しながら決定論的かつ高速なコード把握を実現する。
  - 必要に応じてバックグラウンドで **LSP（Language Server Protocol）** と連携し、精密な型解決や参照解決（Exact Path）を提供する。
- **通信プロトコル**: Model Context Protocol (MCP) を使用し、AIエージェントと標準入出力（stdio）経由で通信する。
- **主要スタック**: Python 3.12+, Tree-sitter, SQLite (Schema v4), basedpyright / ruff / 外部LSP, Dulwich (Git), MCP SDK。

---

## 2. システムアーキテクチャ概要

Luciankeは大きく分けて **二層の解析パス（Fast Path / Exact Path）** と **MCPインターフェース層**、**堅牢な実行基盤** から構成されます。

```
[ AI Coding Agent (Claude Code / Antigravity / etc.) ]
                        │
                        │ (MCP stdio: Tools T-00 ~ T-20)
                        ▼
┌────────────────────────────────────────────────────────┐
│ Lucianke Core Engine (src/lucianke)                    │
│                                                        │
│  ┌───────────────────┐        ┌─────────────────────┐  │
│  │ bootstrap_io / app│───────▶│ MCP Server & Registry│ │
│  └───────────────────┘        └──────────┬──────────┘  │
│                                          │             │
│                 ┌────────────────────────┴────────┐    │
│                 ▼                                 ▼    │
│  ┌───────────────────────────────┐ ┌────────────────┐  │
│  │ Fast Path (超高速・概略)       │ │ Exact Path     │  │
│  │  - Tree-sitter (syntax/)      │ │ (精密・型解決) │  │
│  │  - SQLite 索引 (index/)        │ │  - LSP Pool    │  │
│  │  - 骨格・リレーション・FTS    │ │    (lsp/)      │  │
│  └───────────────────────────────┘ └────────────────┘  │
│                 │                                 │    │
│                 └────────────────┬────────────────┘    │
│                                  ▼                     │
│  ┌──────────────────────────────────────────────────┐  │
│  │ Workspace / Engine / Edit / Verify / Git         │  │
│  │  - ファイル監視 (watchfiles)                      │  │
│  │  - 決定論的編集プロトコル (edit/)                 │  │
│  │  - Git履歴・diff解析 (dulwich)                   │  │
│  │  - 契約検証プラグイン (Z3, sqlglot)              │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

### Fast Path vs. Exact Path
- **Fast Path (Tree-sitter + SQLite)**:
  - リポジトリ全域のシンボル、定義、スコープ内インポート、呼び出し関係をローカルのSQLiteに高速インデックス。
  - ファイルを直接走査せずに SQLite / AST だけでミリ秒オーダー（目標 <50ms）で回答。
- **Exact Path (LSP Pool)**:
  - TypeScriptやPythonなどのLSPサーバーをバックグラウンドで起動・常駐させ、型定義ジャンプやリネーム・診断など厳密性が求められる場合に委託。

---

## 3. ディレクトリ・モジュール構造

```
Lucianke/
├── src/lucianke/              # メインパッケージ
│   ├── app.py                 # サーバーエントリーポイント・合成ルート
│   ├── bootstrap_io.py        # stdio保護（stdout退避・C拡張等の標準出力誤爆防止）
│   ├── config.py              # 設定管理（Settings, 環境変数読み込み）
│   ├── lifecycle.py           # 起動・終了シーケンス、バックグラウンドタスク管理
│   ├── logging_setup.py       # ログ設定（stderrまたはファイル出力）
│   │
│   ├── mcp/                   # MCP通信・プロトコル処理
│   │   ├── server.py          # stdioトランスポート、リクエストハンドラ
│   │   ├── registry.py        # ツール登録台帳（ToolRegistry）
│   │   ├── render.py          # 出力のcompact行フォーマット整形
│   │   ├── budget.py          # レスポンストークンバジェット制御
│   │   └── types.py           # MCPプロトコル用データ型
│   │
│   ├── tools/                 # AIに公開するMCPツール群（T-00 〜 T-20）
│   │   ├── _runtime.py        # ツール実行コンテキスト（RuntimeHub, WiredServices）
│   │   ├── _base.py           # ツール定義共通基盤
│   │   ├── _skeletons.py      # AST骨格抽出ヘルパー
│   │   ├── _lsp_exact.py      # LSP呼び出しとフォールバック処理
│   │   ├── t00_configure_workspace.py
│   │   ├── t01_get_repo_map.py
│   │   ├── ... (T-00 から T-20 まで各ファイルに実装)
│   │   └── t20_query_project_db.py
│   │
│   ├── syntax/                # Tree-sitterによる構文解析・AST抽出
│   │   ├── parsers.py         # パーサー管理・TreeCache
│   │   ├── skeleton.py        # 骨格（関数のシグネチャ・クラス定義）抽出
│   │   ├── boundaries.py      # シンボルやブロックの物理境界（行・バイト範囲）計算
│   │   ├── imports.py         # 各言語（Python/TS/JS等）のimport文・require抽出
│   │   └── relations.py       # スコープ内の定義・参照・シンボルリレーション抽出
│   │
│   ├── index/                 # SQLiteによるコードナレッジ永続化
│   │   ├── schema.py          # SQLiteスキーマ定義（v4: symbols, references, files, etc.）
│   │   ├── core.py            # SQLiteコネクション・トランザクション管理
│   │   ├── exact_marks.py     # 精密解析マークの管理
│   │   ├── rank.py            # PageRank風のシンボル重要度スコアリング
│   │   └── fts.py             # 全文検索（FTS5）統合
│   │
│   ├── engine/                # 解析・索引エンジン
│   │   ├── indexer.py         # 差分ファイル走査、AST解析、DB書き込み
│   │   ├── selector.py        # ツールからのクエリに応じてDBやASTを引くセレクタ
│   │   └── scheduler.py       # バックグラウンド定期再計算スケジューラ
│   │
│   ├── lsp/                   # 言語サーバー連携
│   │   ├── pool.py            # 多言語LSPプロセスのプール管理
│   │   ├── client.py          # JSON-RPCベースのLSPクライアント実装
│   │   └── features.py        # 定義ジャンプ、参照検索、リネーム、診断の仲介
│   │
│   ├── edit/                  # 決定論的コード編集
│   │   ├── transaction.py     # 編集トランザクション（アトミックな書き換え・ロールバック）
│   │   ├── writer.py          # ファイル書き込みと検証
│   │   └── syntax_check.py    # 編集後の文法破壊検出
│   │
│   ├── workspace/             # ワークスペース管理
│   │   ├── context.py         # ワークスペース状態（ルート、言語、設定）
│   │   ├── watcher.py         # watchfilesによるファイル変更検知
│   │   ├── languages.py       # 言語判定とTree-sitter/LSPの対応付け
│   │   └── pyenv.py           # Python仮想環境（venv/.venv）検出
│   │
│   ├── git/                   # Gitリポジトリ連携
│   │   └── repo.py            # dulwichを利用したコミット履歴、diff、シンボル変更解析
│   │
│   ├── verify/                # 契約抽出・形式検証
│   │   └── contracts.py       # Z3, sqlglot等を利用した事前・事後条件検証プラグイン
│   │
│   └── core/                  # システム共通型・ユーティリティ
│       ├── errors.py          # エラーコード体系（E_TARGET_AMBIGUOUS など）
│       ├── urn.py             # シンボルのグローバル一意識別子（URN）
│       ├── tokens.py          # 出力トークン数の推定・キャリブレーション
│       ├── textpos.py         # 行・列・バイトオフセットの相互変換
│       └── compact_lines.py   # compact表現の構築
│
├── bench/                     # ベンチマーク・フィクスチャ取得
├── docs/                      # 詳細仕様書・アーキテクチャ設計書・HANDOFF
├── tests/                     # ユニットテスト・契約テスト・E2Eテスト
└── pyproject.toml             # プロジェクト設定・依存関係定義
```

---

## 4. MCPツール体系（T-00 〜 T-20）

LuciankeはAIエージェントに対して以下の決定論的ツール群を公開します。

| ツール番号 | ツール名 | 主な機能・役割 |
|---|---|---|
| **T-00** | `configure_workspace` | ワークスペースの初期設定、言語環境・仮想環境の指定 |
| **T-01** | `get_repo_map` | リポジトリ全体の重要シンボルとファイルマップの概要出力 |
| **T-02** | `get_file_skeleton` | ファイルの関数・クラス骨格（シグネチャ）のみを抽出 |
| **T-03** | `search_workspace_symbols` | ワークスペース全域のシンボル名検索（FTS/部分一致） |
| **T-04** | `resolve_symbols` | 特定シンボルの定義位置・型情報・URNの解決 |
| **T-05** | `get_symbol_bodies` | 指定シンボルの実装本文（Body）のみを切り出して取得 |
| **T-06** | `find_references_and_implementations` | シンボルの呼び出し元一覧・インターフェース実装先の検索 |
| **T-07** | `query_call_hierarchy` | 上位／下位のコールグラフ（呼び出し関係）を階層展開 |
| **T-08** | `get_diagnostics` | LSPやリンター（ruff等）によるエラー・警告の取得 |
| **T-09** | `analyze_change_impact` | 変更予定箇所が影響を及ぼす他のシンボル・モジュールの特定 |
| **T-10** | `edit_symbol_body` | シンボル単位での安全かつ決定論的な実装書き換え |
| **T-11** | `rename_symbol` | リファクタリング（LSP連携によるプロジェクト横断一括リネーム） |
| **T-12** | `structural_search_replace` | ast-grep風のAST構造マッチングによる一括置換 |
| **T-13** | `apply_code_action` | LSPが提供するQuickFixやインポート整理の自動適用 |
| **T-14** | `find_code_paths` | シンボル間の到達可能パス（実行経路）の探索 |
| **T-15** | `extract_code_contracts` | Docstringや型ヒントから入出力契約（事前/事後条件）を抽出 |
| **T-16** | `verify_contract` | SMTソルバー（Z3等）を用いた関数の整合性・不変条件検証 |
| **T-17** | `get_symbol_history` | Git履歴を遡り、シンボルの過去の変更差分を追跡 |
| **T-18** | `analyze_git_diff_symbols` | 現在のgit diffから「どのシンボルがどう変化したか」を要約 |
| **T-19** | `get_project_db_schema` | 内部SQLiteインデックスのテーブル構造を取得 |
| **T-20** | `query_project_db` | 内部SQLiteインデックスに対する読み取り専用SQLの直接実行 |

---

## 5. 設計上の重要原則・制約事項

### 1. `Node.text` の全廃ルール（BUG-0020 / BUG-0051）
- `py-tree-sitter` 0.26.0 において、`node.text` を読み取るとC側メモリの二重解放やクラッシュの原因となる。
- **規則**: ノードの文字列取得は必ず `source[node.start_byte : node.end_byte]` を使用すること。
- リポジトリ内の番人テスト（`test_syntax_skeleton_realfiles.py`）により、`src/` 配下に直接の `.text` 参照がないか常時監視されている。

### 2. stdio（標準入出力）の完全保護（`bootstrap_io.py`）
- MCPは標準入出力（stdin/stdout）でJSON-RPCメッセージを送受信する。
- ライブラリの初期化時やC拡張モジュールが `stdout` にログや警告を直接書き込むとプロトコルが破損する。
- 起動直後に `bootstrap_io()` を呼び出し、ファイルディスクリプタ1番を安全なストリームに退避し、通常の `stdout` は `stderr` に振り替える設計となっている。

### 3. Windows環境でのバイナリロック防止（BUG-0030）
- MCPサーバーの起動コマンドは必ず `python -P -m lucianke` を指定する（`lucianke.exe` を直接掴まない）。
- これにより、パッケージ更新時や `uv run` 時に `os error 32`（Windowsのファイル排他ロック）が発生するのを防止する。
- `-P` オプションにより、解析対象のワークスペースが誤って `sys.path` に追加されるのを防ぐ。

---

## 6. 開発・テスト手順

### 開発環境のセットアップ
```powershell
git pull
uv sync --extra langs                  # tree-sitter 文法パッケージ群の同期
uv run --no-project python bench/fetch_fixtures.py  # 契約テスト用フィクスチャ（httpx, ky等）の取得
```

### テスト実行
```powershell
uv run pytest -q                       # 全体テスト実行
uv run pytest tests/unit/test_smoke.py # スモークテスト
uv run ruff check src tests            # 静的解析チェック
```
