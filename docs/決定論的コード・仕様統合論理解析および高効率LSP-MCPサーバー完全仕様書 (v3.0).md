# **決定論的プロジェクト知識DB・LSP統合クエリ＆論理解析MCPサーバー完全仕様書 (v3.0: Project-as-a-Database Architecture)**

# **1\. エグゼクティブサマリーと基本コンセプトの再定義**

## **1.1 背景と真の課題：なぜコーディングエージェントはトークンと時間を浪費するのか**

現代のコーディングエージェント（Claude Code、Cline、Cursor Agent等）は、大規模コードベースの探索・編集・検証において深刻な非効率性を抱えている。旧仕様書（v1.0：[決定論的コード・仕様統合論理解析MCPサーバー仕様書](https://docs.google.com/document/d/1gpk-MuaAnhFdAc9bHK5L38fcAK7ss4s44tZpfROcpk8/edit)）では「確率的推論による階層的シンボル誤認」や「自然言語仕様と実装の不整合」を主な課題として挙げていたが、実運用におけるエージェントのトークン消費と作業時間のボトルネックを計測・分析すると、より根本的な4つの運用課題が浮かび上がる。

第一に、ファイル全体読み込みと全文検索の濫用によるコンテキスト肥大化である。エージェントが特定の関数やクラスのインターフェースを把握しようとする際、標準的なファイル読み込みツールや正規表現検索（grep）に依存すると、数百行から数千行に及ぶ実装コード全体をコンテキストウィンドウに読み込んでしまい、1回の探索で数千〜数万トークンを浪費する。

第二に、行番号・文字列一致ベースのコード編集（Diff/Patch）の脆弱性である。エージェントがファイルの一部を書き換える際、行番号のズレやインデントの不一致によってパッチ適用が頻繁に失敗し、再読み込みと再生成のループによってトークンと作業時間が倍増する。

第三に、フィードバックループ（コンパイル・型チェック・テスト実行）の遅延である。コード編集後に毎回シェル経由でビルドツールやテストランナーを起動すると、プロセスのコールドスタートだけで数秒から数十秒を要し、大量の標準出力ログがコンテキストを圧迫する。

第四に、複数ツール呼び出しによる往復（Round-trip）レイテンシである。複数のシンボル定義や参照箇所を調査する際、1回のツール呼び出しで1つのシンボルしか解決できない設計では、LLMの推論ターンが何度も発生し、作業時間が直線的に増大する。

## **1.2 旧仕様書（たたき台 v1.0）の多角的・批判的検証と根本的欠落の洗い出し**

旧仕様書（v1.0）を「トークン消費の低減」「作業時間の短縮」「実用可能なstdio型MCPサーバー」という要件から多角的に検証した結果、以下の7つの重大な設計欠落と構造的矛盾が判明した。本v2.0仕様書ではこれらを抜本的に解決する。

| 検証領域 | 旧仕様書（v1.0）の設計・記述 | 根本的な問題点・矛盾 | 本仕様書（v2.0）での刷新・解決策 |
| :---- | :---- | :---- | :---- |
| **1\. コード探索・骨格把握** | `resolve_symbol` のみ定義（ファイル全体の構造を知る手段がない） | シンボル名や正確な行番号を事前に知らないと探索できず、結局エージェントがファイルを丸ごと読み込んでトークンを浪費する。 | `get_Repo_map` および `get_file_skeleton` を新設。[Tree-sitter](https://tree-sitter.github.io/) で関数本体を折りたたんだシグネチャ骨格のみを返し、閲覧トークンを85〜95%削減する。 |
| **2\. コード部分取得（Read）** | 定義情報の取得のみで、特定の関数本体だけを切り出すツールがない | 関数の実装を確認する際に行番号指定のファイル読み込みに頼るため、無関係な前後コードまで読み込む。 | `get_symbol_body`（バッチ対応）を新設。シンボルID（URN）または名前指定で、対象シンボルのASTノード範囲のコード片のみを正確に抽出する。 |
| **3\. LSP基本ナビゲーション** | `resolve_symbol` と `query_call_hierarchy` のみ | インターフェースの実装クラス一覧（Implementation）、変数の全参照箇所（References）、型の構造定義（Type Definition）が欠落している。 | [Language Server Protocol (LSP) 3.18](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/) に準拠した `find_references_and_implementations` および `search_workspace_symbols` を完備する。 |
| **4\. 診断・エラー検知** | 診断ツールが完全に欠落（`verify_contract` のSMT検証のみ） | 構文エラーや型エラー、未定義変数の検知すらできない。編集後の即時検証ができずシェル実行に頼ることになる。 | `get_diagnostics` を新設。LSPのIncremental Syncを通じて、ファイル保存やビルドなしでミリ秒単位にコンパイル・型エラー・QuickFix候補を取得する。 |
| **5\. 安全な編集・リファクタリング** | 編集系ツールが完全に欠落（Read-onlyの解析ツールのみ） | シンボル名変更や関数差し替えを手動の文字列置換で行うため、インデント崩壊や参照元の修正漏れが多発する。 | `edit_symbol_body`、`rename_symbol`、`structural_search_replace`（[ast-grep](https://github.com/ast-grep/ast-grep) 統合）、`apply_code_action` を新設し、AST/LSP保証付きの安全な編集を実現する。 |
| **6\. 仕様論理抽出の実現性と速度** | KNP/JUMAN++/AMR \+ Outlines（LLM文法制約生成）で全文を常時変換 | MCPサーバー内部でLLMや重量級NLPを回すと数秒〜数十秒の遅延と追加トークン消費が発生し、「作業時間短縮・トークン低減」の目的に真っ向から矛盾する。また [KùzuDB](https://github.com/kuzudb/kuzu) や [CozoDB](https://github.com/cozodb/cozo) 等の外部DB多重構成は保守・配布リスクが高い。 | \*\*Fast Path（ミリ秒級・LLM不使用）**と**Verification Path（オンデマンド・軽量）\*\*を完全分離。日常の95%の操作はTree-sitter \+ LSP \+ SQLiteインメモリグラフで即時応答し、契約検証はJSDoc/Docstring/型アノテーション/OpenAPIの決定論的ルール変換＋軽量Datalog/Z3に限定する。 |
| **7\. stdioプロトコルと状態管理** | トランスポートやLSPプロセス管理、出力トークン制御の記述なし | stdioでは標準出力（stdout）へのログ混入でJSON-RPCが即座にクラッシュする。また大量の検索結果が返るとエージェントのコンテキストが溢れる。 | [MCP Transports仕様](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports) に準拠したstdout完全隔離、LSPワーカープールのライフサイクル管理、および**Compact Text DSL (`format: "compact"`)** と `max_tokens` バジェット制御を全ツールに標準実装する。 |

## **1.3 システムの目的と定量的ターゲット（KPI）**

本システム「Deterministic LSP & Symbolic Reasoning MCP Server (DLSR-MCP)」は、[Model Context Protocol (MCP)](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture) の `stdio` トランスポートを介してコーディングエージェントに接続し、コンパイラ精度のコード理解・構造化編集・論理検証を単一の軽量バイナリ（Rust実装推奨）として提供する。

* **コンテキスト消費トークンの削減率**: 従来のファイル全文読み込み＋grepベースのエージェント操作と比較して、コード探索・影響範囲調査に要するトークン消費量を **75%〜90%削減** する。  
* **ツール応答レイテンシ**: Fast Path（シンボル解決、骨格取得、参照検索、診断取得、AST編集）において **p95で50ms未満**（LSPウォームアップ完了後）、Verification Path（データフロー到達性・SMT境界検証）において **p95で500ms未満** を達成する。  
* **編集成功率の向上**: ASTノード境界およびLSP WorkspaceEditを用いた決定論的編集により、パッチ適用失敗（ハルシネーションによる行ズレ・構文破壊）を **実質ゼロ（99.9%以上の構文妥当性維持）** に抑える。

## **1.4 コアパラダイム転換：「コード解析ツール群」から「Project-as-a-Database (PaaD) クエリインターフェース」へ**

LSPやASTパーサを単なる「個別機能のラッパー（定義へ飛ぶ、参照を探す）」として捉えると、エージェントが複合的な疑問（例：「過去2週間に変更された認証系メソッドのうち、エラーが発生しており、かつ単体テストから呼ばれていないものはどれか？」）を解決するために、何十回も個別ツールを呼び出して結果を脳内（コンテキストウィンドウ内）でJOINする羽目になる。これこそがトークン浪費と推論ターン増大の最大の原因である。

そこで本v3.0仕様では、[GitHub CodeQL ("Treat code like data")](https://docs.github.com/en/code-security/concepts/code-scanning/codeql/codeql-code-scanning) や [Meta Glean](https://github.com/facebookincubator/Glean)、[DuckDB AST拡張 (Sitting Duck)](https://duckdb.org/community_extensions/extensions/sitting_duck) の思想をさらに発展させ、**「プロジェクトに関わるあらゆる情報（AST構文木、LSPの型・参照グラフ、Diagnostics、Git時系列コミット・共変更履歴、OpenAPI/SQL/設定ファイル、コメント内仕様契約）を単一の正規化された『リレーショナル＆グラフDB』としてリアルタイム同期し、MCPはそのDBから最小トークンで必要な知識を引き出すクエリインターフェースである」** と再定義する。

この **Project-as-a-Database (PaaD)** アプローチにより、以下の3つの決定的な優位性が得られる。

1. **サーバーサイドJOINによる往復ターンとトークンの劇的削減**: 「Git履歴 × シンボル呼び出し関係 × 型エラー診断 × 仕様契約」の複合フィルタリングと集計をSQLite（またはDuckDB）エンジン側で1ミリ秒で結合（JOIN）して返すため、エージェント側のコンテキスト消費とツール往復回数が **N回から1回（O(1)）** に激減する。  
2. **LLMが最も得意なSQL / Datalogによる自在なアドホック分析**: 事前定義された定型ツール（ショートカットView）だけでなく、公開ビューに対する読み取り専用の `query_project_db`（SQL / Datalogクエリツール）を提供することで、仕様書作成時点では想定していなかったどんな複雑な横断調査も、エージェント自身が1本のSELECT文を書くだけで瞬時に回答を引き出せる。  
3. **不完全な情報（未知言語のgrep結果など）の統一的格納**: すべてのテーブル・エッジに `confidence_level`（`"exact"` \| `"structural"` \| `"heuristic"` \| `"lexical_fallback"`）カラムを持たせることで、コンパイラ精度のLSP情報と、未知言語に対する内部 [ripgrep](https://github.com/burntsushi/ripgrep) の推定結果を同じSQLクエリ上で透過的に扱い、精度に応じたフィルタリングが可能になる。

---

# **2\. 対応言語・ドキュメントおよび解析ティア定義**

各言語・データフォーマットのエコシステム成熟度に応じ、提供する解析機能を4つのティア（Tier 1〜Tier 4）として定義する。さらに、主にPythonを用いる開発現場の実態に即して **Python周辺エコシステム（C拡張・Notebook・グラフ記述言語・構造化データ・SQL・IDL）のディープ統合** と、LSPやTree-sitterパーサが存在しない **未知・マイナー言語に対する自動フォールバックおよび精度フラグ（Confidence Metadata）機構** を標準実装する。

## **2.1 解析ティア（Tier 1〜Tier 4）の全体定義**

| ティア | 対象言語 / フォーマット | 構文解析・構造検索エンジン | シンボル・意味解決エンジン | 提供機能と保証精度 (`confidence_level`) |
| :---- | :---- | :---- | :---- | :---- |
| **Tier 1: 完全決定論解析** | Python (`.py`, `.pyi`), TypeScript, JavaScript, Rust, Go, C, C++, C\#, Kotlin, Swift | [Tree-sitter](https://github.com/tree-sitter/tree-sitter) / [ast-grep](https://ast-grep.github.io/) | Multiplexed LSP (`pyright`, [Ruff](https://github.com/astral-sh/ruff), `rust-analyzer`, `vtsls`, `gopls`, `clangd`) \+ [SCIP](https://sourcegraph.com/blog/cross-repository-code-navigation) | **`"exact"` (コンパイラ精度)**: 型推論、クロスファイル定義・参照解決、Call/Dataflowグラフ、即時Diagnostics、安全なAST編集・Rename、Z3契約検証すべて対応。 |
| **Tier 2: 言語境界・構造化仕様・クエリ解析** | [Cython (`.pyx`, `.pxd`)](https://cython.org/), Jupyter (`.ipynb`), [SQL (`.sql`)](https://github.com/tobymao/sqlglot), [Graphviz DOT (`.dot`, `.gv`)](https://graphviz.org/doc/info/lang.html), [Mermaid (`.mmd`)](https://mermaid.js.org/), OpenAPI, [Protobuf (`.proto`)](https://protobuf.dev/programming-guides/proto3/), GraphQL | Tree-sitter各言語文法 \+ 専用ASTパーサ（[SQLGlot](https://github.com/tobymao/sqlglot), DOT/Mermaid Parser） | 専用スキーマリンカ \+ Pythonホストコードとのクロス言語ブリッジ | **`"exact"` 〜 `"structural"`**: スキーマ・グラフ構造のシンボル化、Python本体コードとの型・変数・エンドポイント整合性検証、構造化骨格抽出に対応。 |
| **Tier 3: 階層データ・マークアップ・テンプレート** | XML (`.xml`, `.svg`, `junit.xml`), JSON / JSONL, YAML, TOML (`pyproject.toml`), Jinja2 (`.j2`), Markdown, reStructuredText (`.rst`) | Tree-sitter \+ ストリーミング構造パーサ（XPath / JSONPath対応） | スキーマ推論器（JSON Schema / XSD / Pydantic連携） | **`"structural"` (構文木精度)**: 巨大データファイルの深度制限付き骨格（スキーマ）抽出、XPath/JSONPathによる部分ノード取得・安全置換、テスト失敗箇所の即時抽出に対応。 |
| **Tier 4: 未知・非対応言語（自動フォールバック）** | 独自DSL、レガシー言語（COBOL, Fortran等）、設定ファイル、Tree-sitter未対応の任意のテキストファイル | [Universal Ctags](https://github.com/universal-ctags/ctags) \+ インデント/ブロック境界ヒューリスティック \+ [ripgrep (`grep-regex`)](https://github.com/burntsushi/ripgrep) | 語彙トークン境界（Word-boundary）正規表現マッチング | **`"heuristic"` / `"lexical_fallback"`**: 自動で内部 `ripgrep` / `ctags` にフォールバックし、**レスポンスに必ず「未知言語のため語彙一致による低精度推定である」旨のフラグと警告を付与**して返却する。 |

## **2.2 Python周辺エコシステム特化仕様（Python-Centric Deep Integration）**

Pythonを主力言語とする開発では、`.py` 単体の解析だけでは不十分であり、周辺フォーマットがコンテキスト消費やバグの温床となる。そのため以下の7つの専用ブリッジ機構を実装する。

4. **型スタブ (`.pyi`)・Cython (`.pyx`, `.pxd`)・Rust/C拡張 ([PyO3](https://github.com/pyo3/pyo3) / `cffi`) の言語横断シンボル連結**:  
   1. Pythonコードからネイティブ拡張モジュールやCython関数を呼び出している場合、`resolve_symbols` や `get_symbol_bodies` は `.py` が存在しなくても自動的に `.pyi` スタブ、[Cythonの `.pxd`/`.pyx` 定義](https://cython.readthedocs.io/en/latest/src/userguide/source_files_and_compilation.html)、または同一ワークスペース内のRust（[PyO3](https://github.com/pyo3/pyo3) の `#[pyfunction]` / `#[pyclass]` 属性）およびC/C++実装へとシンボルURNを連結して追跡する。  
   2. LSPバックエンドには型解決用の `pyright`（または `basedpyright`）に加え、超高速Lint・自動修正（Code Action）用の [Ruff (`ruff server`)](https://github.com/astral-sh/ruff) を標準で並列統合する。  
5. **Jupyter Notebook (`.ipynb`) の「出力ストリップ＆仮想Pythonセル化」エンジン**:  
   1. `.ipynb` ファイルは内部にBase64エンコード画像や数万行の実行結果JSONを含むため、通常のファイル読み込みを行うと一瞬でコンテキストウィンドウが枯渇する。  
   2. 本MCPサーバーは `.ipynb` に対して透過的なアダプタを介在させ、`outputs` と不要な `metadata` を完全に除外した上で、`# %% [Cell 3: code] (exec_count: 12)` のような軽量な仮想Pythonスクリプトとして `get_file_skeleton` や `get_symbol_bodies` に提供する。編集時も `edit_symbol_body` で特定セル内の関数だけを書き換え、元の `.ipynb` JSON構造を壊さずに保存する（**トークン消費を95%以上削減**）。  
6. **グラフ記述言語（[Graphviz DOT](https://graphviz.org/doc/info/lang.html) / [Mermaid](https://mermaid.js.org/) / PlantUML）の双方向サポート（入力解析＋出力DSL）**:  
   1. **入力としての解析**: リポジトリ内の `.dot`, `.gv`, `.mmd` ファイルやMarkdown内のMermaidブロックを単なるテキストではなく「有向グラフAST（Node集合・Edge集合・Subgraph階層・属性）」としてパースする。これにより、設計図上の状態遷移やクラス関係と、Python実装（EnumやStateクラス）との矛盾を `verify_contract` で検証できる。  
   2. **出力フォーマットとしてのサポート**: `query_call_hierarchy`, `find_code_paths`, `analyze_change_impact` の `format` パラメータに `"dot"` および `"mermaid"` を追加する。コールグラフやデータフローをDOT言語やMermaid記法で出力することで、ネストしたJSONよりもはるかに少ないトークンでグラフ構造を表現でき、人間への可視化レポートにもそのまま利用できる。  
7. **XML / HTML / SVG / JSON / JSONL / YAML / TOML のスキーマ骨格抽出とXPath/JQ部分取得**:  
   1. `pyproject.toml` の依存関係解析はもちろん、数万行に及ぶXML（`pytest` が出力する `junit.xml`、カバレッジ `coverage.xml`、スクレイピング対象のHTML/XML、SVG図面）や巨大なJSON/JSONLデータセットに対し、`get_file_skeleton` を実行すると **「タグ/キーの階層構造、属性一覧、配列要素の推論型スキーマ、要素数サマリー」** のみを数十行で返却する。  
   2. `get_symbol_bodies` において、ファイルパスと共にXPath（例: `//testcase[failure]`）やJSONPath / JQクエリ（例: `.records[0:3].user`）を `symbol_name` として指定でき、必要な要素だけをピンポイントで抽出・編集できる。  
8. **SQL（[SQLGlot](https://github.com/tobymao/sqlglot) 統合）およびORM/Pydanticモデル整合性検証**:  
   1. `.sql` ファイルおよびPythonコード内のSQL文字列（`cursor.execute("SELECT ...")` やSQLAlchemy / DuckDBクエリ）からASTを抽出し、参照しているテーブル・カラム名と、Python側の [Pydantic](https://pydantic.dev/docs/validation/2.10/get-started/contributing/)`BaseModel` やSQLAlchemy `DeclarativeBase`、TypedDict、Pandas/Polarsスキーマ定義との型・カラム存在整合性を静的に検証する。  
9. **IDL（OpenAPI / [Protocol Buffers `.proto`](https://protobuf.dev/programming-guides/proto3/) / GraphQL）とFastAPI・gRPC実装の自動リンク**:  
   1. FastAPIのルートデコレータ（`@app.post(...)`）やPydanticモデルからOpenAPIスキーマ定義をリアルタイム抽出し、また `.proto` ファイルの `service` / `message` 定義とPython側の生成コード（`*_pb2.py`, `*_pb2_grpc.py`）を単一の論理シンボルとして紐付ける。  
10. **Jinja2テンプレート (`.j2`) および reStructuredText (`.rst`) の参照解決**:  
    1. Python圏で広く使われるSphinx形式の `.rst` ドキュメント（`:func:`, `:class:` ディレクティブ）や、プロンプト管理・Web描画で使われるJinja2テンプレート内のプレースホルダ変数（`{{ context.user_id }}`）を抽出し、レンダー元のPython関数が渡している辞書キーやPydanticフィールドと一致しているかを検証する。

## **2.3 未知の言語・非対応フォーマットへの自動多段階フォールバックと精度明示フラグ（Graceful Fallback & Confidence Signaling）**

実務のリポジトリには、LSPもTree-sitter文法も存在しない独自DSL、社内設定ファイル、古いスクリプト言語、あるいは構文が激しく壊れた作成途中のファイルが必ず混在する。これらに対して「未対応エラー」で処理を止めるのではなく、**内部で自動的に検索・解析手法を段階的にフォールバックさせつつ、エージェントに対して「現在は未知言語用の低精度モード（ヒューリスティック/grep）で動作している」ことを構造化フラグで明確に伝達する** 仕組みを導入する。

### **(1) 4段階の自動フォールバック・ラダー（Fallback Ladder）**

ツールが呼び出された際、対象ファイルまたは言語のサポート状況に応じて以下の順序で自動降格（Degrade）する。

11. **Level 1 (`"exact"` \- Compiler/LSP \+ SCIP)**: LSPサーバーが稼働しており、型推論とスコープ解決が完全に機能している状態。  
12. **Level 2 (`"structural"` \- Tree-sitter AST)**: LSPは未稼働または未導入だが、Tree-sitterの文法が存在し、構文木（関数境界、クラス定義、インポート文、ローカル変数スコープ）が正確に把握できている状態。  
13. **Level 3 (`"heuristic"` \- Universal Ctags \+ インデント/括弧ブロック解析)**: Tree-sitter文法も存在しない言語に対し、組み込みの [Universal Ctags](https://github.com/universal-ctags/ctags) ルールセットと汎用字句解析（`def` 、`function` 、`fn` 、`sub` 、`class` 、`{ ... }` およびインデント階層）を用いて関数・セクションの開始行と終了行を推定する状態。  
14. **Level 4 (`"lexical_fallback"` \- 内部 [ripgrep](https://github.com/burntsushi/ripgrep) 自動実行)**: 構文手がかりすら得られない未知フォーマットに対し、MCPサーバー内部で高速な `grep-regex`（[ripgrep](https://github.com/burntsushi/ripgrep) のRustコアライブラリ）を自動実行する状態。単なる部分文字列一致によるノイズを減らすため、自動的に単語境界（`\b<symbol>\b`）と定義パターン推定（`<symbol>\s*[:=]` 等）を組み合わせて検索し、前後数行のコンテキストスニペットを返す。

### **(2) 全ツール共通の精度・フォールバック通知メタデータ（Resolution Metadata Schema）**

フォールバックが発生した際、エージェントが「コンパイラが保証した正確な結果」なのか「単なるgrepの文字列一致（同名別シンボルの誤検知や動的呼び出しの漏れがあり得る）」なのかを即座に判別できるよう、すべてのツールレスポンスのヘッダ（Compact形式では先頭ステータス行、JSON形式では `_resolution_meta` フィールド）に以下のフラグを付与する。

15. **JSON形式での `_resolution_meta` フィールド定義**:

```json
{
  "_resolution_meta": {
    "confidence_level": "lexical_fallback",
    "is_degraded": true,
    "engine_used": "internal_ripgrep_word_boundary",
    "detected_language": "unknown (.custom_dsl)",
    "accuracy_warning": "未知の言語またはパーサ未対応フォーマットのため、内部で単語境界grepにフォールバックしました。スコープ解決や多態性の区別は行われておらず、同名シンボルの誤検知（False Positive）や動的参照の見落としが含まれる可能性があります。編集時は周辺コンテキストを get_symbol_bodies で確認してください。"
  }
}
```

16. **Compact Symbol DSL形式での先頭警告バナー表現（最小トークンで注意喚起）**:

```
! [DEGRADED: confidence=lexical_fallback | engine=internal_ripgrep | lang=unknown(.custom_dsl)]
! Warning: LSP/AST unavailable. Results are lexical grep matches and may contain false positives.
```

17. **編集系ツールにおけるフォールバック時の安全ガード（Safety Interlock）**:  
    1. `confidence_level` が `"heuristic"` または `"lexical_fallback"` のファイルに対して `rename_symbol` や `edit_symbol_body` が呼ばれた場合、誤ってコメントや無関係な同名文字列を破壊するリスクがある。そのため、未知言語ファイルに対する編集ツールは **デフォルトで `dry_run: true`（変更差分プレビューと警告の返返）として動作** し、エージェントが明示的に `allow_heuristic_mutation: true` フラグをセットした場合にのみ実ファイルへの書き換えを実行する安全装置を設ける。

---

# **3\. システムアーキテクチャ（2層ハイブリッド・パイプライン）**

旧仕様書で問題となった「リアルタイム処理における重量級NLP/LLM推論のボトルネック」を解消するため、本システムは **Fast Path（リアルタイムLSP・AST層）** と **Verification Path（オンデマンド論理解析層）** の2層ハイブリッドアーキテクチャを採用する。\[Coding Agent (Claude Code / Cline / Cursor)\]

       │  ▲

       │  │ JSON-RPC 2.0 over stdio (Compact Text DSL / Token-Budgeted Payload)

       ▼  │

┌─────────────────────────────────────────────────────────────────────────┐

│ MCP Interface & Token Optimizer Layer                                   │

│  \- Request Batching / Cursor Pagination / Token Budget Truncator        │

│  \- Compact Symbol Formatter / File Virtual Overlay Manager              │

└──────────────────┬───────────────────────────────────┬──────────────────┘

                   │ (95% Operations: \< 50ms)          │ (5% Operations: \< 500ms)

                   ▼                                   ▼

┌──────────────────────────────────────┐ ┌────────────────────────────────┐

│ Fast Path: Structural & LSP Engine   │ │ Verification Path: Logic Core  │

│  \- Tree-sitter Incremental AST       │ │  \- AST/CFG to Datalog Extractor│

│  \- ast-grep Pattern Matcher/Rewriter │ │  \- Rule-based Doc/OpenAPI      │

│  \- Multiplexed LSP Client Pool       │ │    Constraint Parser (No LLM)  │

│    (rust-analyzer, pyright, vtsls..) │ │  \- Embedded Datalog Engine     │

│  \- SCIP Index & SQLite Symbol Cache  │ │  \- Z3 SMT Solver (Bounded)     │

└──────────────────────────────────────┘ └────────────────────────────────┘

## **3.1 MCPインターフェース＆トークン最適化レイヤー (MCP Interface & Token Optimizer Layer)**

コーディングエージェントとの通信を担う最上位レイヤーであり、[Model Context Protocol仕様](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports) に完全準拠する。最大の特徴は、LSPやASTが生成する冗長なJSON構造をそのまま返さず、LLMが最も少ないトークン数で誤読なく理解できる **Compact Symbol Format（コンパクト表現）** に直列化して返す点にある。また、すべてのツールに `max_tokens` パラメータを備え、指定されたトークン上限を超える場合は重要度（呼び出し階層や編集箇所からの距離）に基づいて自動的にサマリー化およびページネーション（`next_cursor` の発行）を行う。

## **3.2 Fast Path：高速コード構造化・LSP統合レイヤー (Structural & LSP Engine)**

日常的なコード探索、定義・参照解決、エラー診断、安全なコード編集をミリ秒単位で処理するコアエンジンである。

1. **Tree-sitter インクリメンタル構文木キャッシュ**: ワークスペース内のファイル群をオンメモリおよびSQLiteで管理し、ファイル全体の骨格抽出（関数本体を省略しシグネチャとDocコメントのみを残す処理）や、行番号に依存しないシンボル単位のコード切り出しを5ms以下で実行する。  
2. **Multiplexed LSP Client Pool（多重化LSPプロセスマネージャー）**: 言語ごとのLSPサーバー（`pyright`, `rust-analyzer`, `vtsls`, `gopls`, `clangd` 等）をバックグラウンドの子プロセスとして遅延起動（Lazy Spawn）し、MCPサーバー側で `textDocument/didOpen`, `textDocument/didChange`, `textDocument/publishDiagnostics` の状態を保持する。これにより、エージェントがツールを呼ぶたびにLSPを再起動するオーバーヘッドを完全に排除する。  
3. **SCIP / SQLite シンボルグラフキャッシュ**: [SCIP (Source Code Intelligence Protocol)](https://sourcegraph.com/blog/cross-repository-code-navigation) 形式の標準シンボルID（例：`scip-typescript npm app 1.0.0 src/AuthService.ts/AuthService#login().`）をキーとして、定義位置、型シグネチャ、呼び出し関係（Caller/Callee）を単一ファイルの組み込みSQLiteデータベース（WALモード）に永続化する。外部グラフDBサーバーの構築は一切不要とする。

## **3.3 Verification Path：決定論的仕様抽出・論理検証レイヤー (Symbolic Verification Core)**

旧仕様書ではKNP/JUMAN++やAMR、さらにはOutlinesを用いたLLM推論をMCP内部で実行する設計であったが、これを **「決定論的ルールベース抽出＋組み込み軽量ソルバ」** に全面刷新する。

1. **決定論的コントラクト抽出器 (Rule-based Contract Extractor)**: LLMを一切使用せず、コード内の型アノテーション（TypeScript型、Python Type Hints、Rust型）、構造化コメント（JSDoc `@param {number} age - 0以上150以下`、Python Docstring `Raises:`、Design-by-Contractアノテーション `@requires`, `@ensures`）、アサーション文（`assert`, `precondition`）、およびOpenAPIスキーマ（`minimum`, `maximum`, `pattern`, `required`）から、構文木パターンマッチングによって直接SMT-LIB式およびDatalogファクトを生成する。万が一、自由記述の自然言語仕様書を論理式に変換したい場合は、MCPサーバー内で裏のLLMを呼ぶのではなく、**ホスト側のコーディングエージェント自身が自然言語を構造化制約JSONに変換して `verify_contract` ツールに入力する** 責務分担とする。これにより、MCPサーバーの完全な決定論性と高速性、ゼロ追加APIコストを保証する。  
2. **組み込みDatalogエンジン (In-memory Datalog / SQLite Recursive CTE)**: 外部プロセスの起動コストを避けるため、Rustネイティブの軽量Datalog（Crepe / Ascent または [Soufflé](https://github.com/souffle-lang/souffle) コンパイル済みルール）とSQLite再帰クエリを併用し、制御フローグラフ（CFG）およびデータフロー（Taint Analysis / 未検証入力の伝播経路）を数ミリ秒〜数百ミリ秒で算出する。  
3. **境界付きSMT検証器 ([Z3 Theorem Prover](https://github.com/z3prover/z3) 統合)**: 関数内の分岐条件（`if/else`、`switch`、ガード節）をSSA（静的単一代入）形式に変換し、抽出された事前条件・事後条件との矛盾（境界値バグ、ゼロ除算、Null参照、配列境界外アクセス）をZ3ソルバで判定する。ソルバのハングアップを防ぐため、厳格なタイムアウト（デフォルト300ms）とループ展開上限（Bounded Model Checking: デフォルト深さ3）を設ける。

## **3.4 ワークスペース・ターゲット解決＆Git時系列グラフ統合レイヤー (Workspace Context & Git Temporal Layer)**

実運用において「どのディレクトリ（プロジェクトルート）、どのPython仮想環境（`.venv` / `conda` / `uv`）、どのGitブランチ・コミットを対象に解析するか」が曖昧であると、LSPが誤ったインタープリタを参照したり、モノレポ内で無関係なパッケージを走査してトークンを浪費する原因となる。また、コードの「現在の断面（スナップショット）」だけでなく、**Git履歴が持つ時系列の意味情報（なぜそのコードが書かれたか、どのシンボルが直近で壊れたか、どのファイル群が常に一緒に変更されるか）** を決定論的グラフに統合することで、バグ調査や変更影響分析の精度と速度が劇的に向上する。

18. **3段階のプロジェクトルート＆実行環境（Target Context）決定プロトコル**:  
    1. **自動検出（MCP `roots/list` 連携）**: 起動時に [MCP Client Roots仕様 (`roots/list` および `notifications/roots/list_changed`)](https://modelcontextprotocol.io/specification/2025-06-18/client/roots) を購読し、ホストエージェントが開いているワークスペースルートを自動認識する。さらにディレクトリ階層を走査して `pyproject.toml`, `uv.lock`, `poetry.lock`, `package.json`, `Cargo.toml`, `.git` を検出し、サブプロジェクト境界とPython仮想環境（`.venv/bin/python`）を自動バインドする。  
    2. **明示的コンテキスト管理ツール (`configure_workspace`)**: マルチリポジトリやモノレポ、特定仮想環境の切り替え、Gitの比較基準リビジョン（例: `base_ref: "main"`）を動的に設定・確認する管理ツールを提供する。  
    3. **全ツール共通コンテキスト引数 (`workspace_id` / `revision`)**: 全ツールの共通オプション引数として、対象サブプロジェクトを指定する `workspace_id`（省略時はアクティブなルート）と、過去の任意のGitコミット/ブランチ時点のコード・シンボルを直接参照できる `revision`（例: `"HEAD~3"`, `"main"`, `"v1.2.0"`、省略時は現在のWorking Tree）を標準サポートする。  
19. **Git時系列グラフエンジン（[gitoxide (`gix`)](https://github.com/gitoxidelabs/gitoxide) / [libgit2](https://github.com/libgit2/libgit2) ネイティブ統合）**:  
    1. シェルで遅い `git log` や `git blame` を何度も叩かせるのではなく、Rustネイティブの [gitoxide](https://github.com/gitoxidelabs/gitoxide) によって `.git` オブジェクトデータベースを直接インメモリ走査し、Tree-sitter ASTと結合した **「シンボル単位のGit履歴（Semantic Git History）」** を提供する。  
    2. **Co-change Coupling（共変更結合度）のDatalog統合**: 過去のコミット履歴から「関数Aを変更したコミットの85%で、設定ファイルBまたはテストCも同時変更されている」という統計的・時系列的な結合度（Temporal Coupling）を抽出し、静的なLSPコールグラフだけでは絶対に見抜けない **暗黙の依存関係（言語横断・設定ファイル間の隠れた結合）** を `analyze_change_impact` に統合する。

## **3.5 Project-as-a-Database (PaaD) 統一スキーマ定義 (Unified Project Knowledge Schema)**

Tree-sitter、LSP、SCIP、[gitoxide](https://github.com/gitoxidelabs/gitoxide)、[SQLGlot](https://github.com/tobymao/sqlglot)、および仕様抽出器が収集したすべてのファクトは、組み込みSQLite（WALモード＋[FTS5全文検索インデックス](https://www.sqlite.org/fts5.html)＋再帰CTEグラフ走査）上の以下の10個のコアテーブル（公開Read-Only View）としてリアルタイムに正規化・保持される。

| テーブル / View名 | 主な格納エンティティ | 主要カラム構成（抜粋） | 主なデータソース（Ingest元） |
| :---- | :---- | :---- | :---- |
| **`workspaces`** | プロジェクトルート・環境情報 | `workspace_id`, `root_path`, `python_env`, `active_branch`, `head_sha`, `lsp_status_json` | [MCP Client Roots](https://modelcontextprotocol.io/specification/2025-06-18/client/roots), `pyproject.toml` |
| **`files`** | 全ソース・設定・データファイル | `file_id`, `workspace_id`, `path`, `language`, `tier`, `line_count`, `sha256`, `pagerank_score`, `last_modified_commit` | ファイルシステム監視 (`notify`), `.gitignore` |
| **`symbols`** | クラス・関数・変数・スキーマ要素・未知言語タグ | `urn` (PK), `file_id`, `name`, `qualified_name`, `kind`, `start_line`, `end_line`, `signature`, `return_type`, `visibility`, `doc_summary`, `complexity`, `confidence_level` | [Tree-sitter](https://tree-sitter.github.io/), LSP, [SCIP](https://sourcegraph.com/blog/cross-repository-code-navigation), [Universal Ctags](https://github.com/universal-ctags/ctags) |
| **`relations`** | シンボル間の有向エッジ（呼び出し・継承・実装・型参照・SQL/API結合） | `source_urn`, `target_urn`, `relation_kind` (`CALLS`, `IMPLEMENTS`, `INHERITS`, `REFERENCES`, `READS_TABLE`, `RENDERS_TEMPLATE`), `call_site_line`, `guard_condition`, `confidence_level` | LSP (`callHierarchy`, `references`), Tree-sitter CFG, [ripgrep](https://github.com/burntsushi/ripgrep) |
| **`dataflows`** | 変数・引数間のデータ伝播およびTaint経路 | `flow_id`, `source_urn`, `source_var`, `sink_urn`, `sink_var`, `hop_count`, `passes_sanitizer`, `path_json` | AST Def-Use抽出 \+ 組み込みDatalog ([Soufflé](https://github.com/souffle-lang/souffle) 互換) |
| **`diagnostics`** | リアルタイム構文・型・LintエラーとQuickFix | `diag_id`, `file_id`, `enclosing_urn`, `severity`, `code`, `source` (`pyright`, `ruff`, `tsc`), `line`, `message`, `quickfix_action_id` | Multiplexed LSP (`publishDiagnostics`), [Ruff](https://github.com/astral-sh/ruff) |
| **`contracts`** | 抽出された事前・事後条件、不変条件、OpenAPI/Pydantic制約 | `contract_id`, `target_urn`, `contract_type` (`PRE`, `POST`, `INVARIANT`, `THROWS`), `source_kind` (`TYPE`, `DOCSTRING`, `ASSERT`, `OPENAPI`), `expression`, `smt_lib_formula`, `last_verify_status` | 決定論的コントラクト抽出器, [Z3 Solver](https://github.com/z3prover/z3) |
| **`git_commits`** | Gitコミット履歴メタデータ | `commit_sha` (PK), `author`, `timestamp`, `message_summary`, `is_merge` | [gitoxide (`gix`)](https://github.com/gitoxidelabs/gitoxide) / [libgit2](https://github.com/libgit2/libgit2) |
| **`git_symbol_changes`** | コミット×シンボルの時系列変更履歴 | `commit_sha`, `urn`, `file_id`, `change_type` (`ADDED`, `MODIFIED_BODY`, `MODIFIED_SIG`, `DELETED`), `lines_added`, `lines_deleted` | Git Tree差分 × Tree-sitter AST比較 |
| **`git_co_changes`** | ファイル/シンボル間の共変更結合度（Temporal Coupling） | `entity_a`, `entity_b`, `co_change_count`, `coupling_ratio` (`0.0`〜`1.0`), `last_co_changed_sha` | Git履歴マイニング集計ビュー |

#### **複合クエリによる一発回答の例（なぜPaaDが圧倒的に強力か）**

たとえばエージェントが **「直近10コミットで変更されたPython関数のうち、外部から3箇所以上呼ばれているのにテストファイルからの参照がゼロで、かつ現在型エラーまたは未検証の契約がある危険な関数トップ5」** を知りたい場合、従来なら20回以上のツール呼び出しが必要だったが、PaaDアーキテクチャでは以下のSQL（または対応ツール）1発・15msで結果が返る：

```sql
SELECT s.urn, s.path, s.signature, COUNT(DISTINCT r_in.source_urn) AS caller_count, d.message AS current_error
FROM symbols s
JOIN git_symbol_changes g ON s.urn = g.urn
LEFT JOIN relations r_in ON s.urn = r_in.target_urn AND r_in.relation_kind = 'CALLS'
LEFT JOIN diagnostics d ON s.urn = d.enclosing_urn AND d.severity = 'error'
WHERE g.commit_sha IN (SELECT commit_sha FROM git_commits ORDER BY timestamp DESC LIMIT 10)
  AND NOT EXISTS (
    SELECT 1 FROM relations r_test JOIN files f_test ON r_test.source_file_id = f_test.file_id
    WHERE r_test.target_urn = s.urn AND f_test.path GLOB '*test*'
  )
GROUP BY s.urn
HAVING caller_count >= 3 OR d.diag_id IS NOT NULL
ORDER BY caller_count DESC LIMIT 5;
```

---

# **4\. トークン消費低減・作業時間短縮のコア設計原則**

本MCPサーバーが他の単純なLSPラッパーと一線を画すため、以下の5つのトークン・時間最適化メカニズムをプロトコルレベルで実装する。

## **4.1 出力圧縮フォーマット（Compact Symbol DSL）**

LSPの生レスポンス（Rangeオブジェクトの深いネストやURI文字列の反復）はトークン効率が極めて悪い。本仕様では、すべてのツールに `format` 引数（`"compact"` \| `"json"` \| `"dot"` ([Graphviz DOT](https://graphviz.org/doc/info/lang.html)) \| `"mermaid"` ([Mermaid](https://mermaid.js.org/))、デフォルト "compact"）を設け、グラフ/階層/パス取得系ツールでの軽量ダイアグラム表示に対応する。また、フォールバック等による低精度モード時は必ずレスポンス先頭に `[confidence=...]` の警告インジケータヘッダを含める。"compact" 指定時には以下のような高密度テキスト表現を返す。

* **従来のLSP JSON表現（約180トークン）**:

{

  "uri": "file:///workspace/src/services/auth.ts",

  "name": "login",

  "kind": 6,

  "range": { "start": { "line": 41, "character": 2 }, "end": { "line": 88, "character": 3 } },

  "selectionRange": { "start": { "line": 41, "character": 8 }, "end": { "line": 41, "character": 13 } },

  "detail": "(user: string, pass: string) \=\> Promise\<SessionToken\>"

}

* **本仕様のCompact Symbol DSL表現（約28トークン：84%削減）**:

\[Method\] src/services/auth.ts\#AuthService.login @L42-89

  sig: (user: string, pass: string) \-\> Promise\<SessionToken\>

## **4.2 スケルトンファースト探索（Skeleton-First Navigation）**

エージェントが未知のファイルを調査する際、いきなりファイル全文を読ませるのではなく、`get_file_skeleton` によってクラス・関数・型定義のシグネチャと折りたたみ範囲（`@L42-89 { ...47 lines omitted }`）のみを返却する。エージェントは必要な1〜2個のメソッドだけを `get_symbol_body` でピンポイントに取得できるため、1ファイルあたりの閲覧トークンを平均90%削減できる。

## **4.3 バッチ要求（Batching by Default）による往復ターン削減**

エージェントが「5つの関連関数の実装を同時に見たい」「3つのシンボルの定義元を一度に調べたい」と考えた際、5回のツール呼び出し（または複数ターンのLLM推論）を行わせるのを防ぐため、主要な読み取りツール（`resolve_symbols`, `get_symbol_bodies`, `get_file_skeleton`）はすべて **配列による複数ターゲット一括指定** を標準サポートする。

## **4.4 編集＋即時診断のトランザクション化（Edit-and-Verify Atomic Loop）**

`edit_symbol_body` や `rename_symbol` などの編集系ツールを実行すると、MCPサーバーはファイルを書き換えるだけでなく、直ちにインメモリでTree-sitterの構文チェックとLSPの `didChange` 診断更新を実行し、**その編集によって新たに発生したコンパイルエラー・型エラー（差分Diagnostics）を同一ツールレスポンス内に含めて即座に返却する**。これにより、エージェントは「編集ツールを呼ぶ → 診断ツールを呼ぶ（またはビルドコマンドを叩く）」という2ステップを踏む必要がなくなり、1回のツールコールで自己修正まで完結できる。

## **4.5 トークンバジェット制御と自動プルーニング（Token Budget Enforcement）**

すべてのツール入力にオプションの `max_tokens`（デフォルト: `2000`）を受け付ける。巨大なコールグラフや多数の参照箇所がヒットした場合、サーバー側でトークン数（`cl100k_base` / `o200k_base` 近似カウンタ）を計算し、上限を超える分は切り捨てずに「ファイル別の件数サマリー」と `next_cursor` トークンに変換して返す。これにより、意図しない巨大出力によるコンテキストウィンドウ爆発を100%防止する。

---

# **5\. 実用MCPツールインターフェース完全仕様（全5カテゴリ・16ツール）**

旧仕様書の5つのツール（`resolve_symbol`, `query_call_hierarchy`, `verify_contract`, `find_code_paths`, `extract_specification_logic`）を再設計・統合し、現場のコーディングエージェントが探索から編集・検証までをシームレスに完結できる **全7カテゴリ・21ツール** を定義する。

## **ツール全体一覧と新旧対応表**

### **全ツール共通ベースパラメータ（Universal Target Context Parameters）**

すべてのMCPツールは以下の共通オプション引数を暗黙的に受け付ける設計とし、ターゲット特定の揺らぎを排除する。

20. `workspace_id` (`string`, optional): 対象プロジェクトルートの識別子またはルートパス（省略時は `configure_workspace` でアクティブ化されたデフォルトルート、または対象ファイルパスから自動解決された最近傍プロジェクトルート）。  
21. `revision` (`string`, optional, default: `"WORKING_TREE"`): 解析対象のGitリビジョン（`"WORKING_TREE"`, `"STAGED"`, `"HEAD"`, `"main"`, コミットSHA等）。指定された場合、作業ツリーを汚すことなく `.git` オブジェクトから該当時点の仮想ファイルツリーとASTをオンザフライ構築して解析する。  
22. `format` (`string`, optional, default: `"compact"`): `"compact"` \| `"json"` \| `"dot"` \| `"mermaid"`.  
23. `max_tokens` (`integer`, optional, default: `2000`): レスポンスの最大トークン予算。

| カテゴリ | ツール名 | 旧仕様書(v1.0)との対応 | 主な役割とトークン/時間削減効果 |
| :---- | :---- | :---- | :---- |
| **A. 探索・骨格把握** | `1. get_repo_map` | 新規追加 | PageRank重要度順のリポジトリ全体構造・主要シンボルマップ生成 |
| **A. 探索・骨格把握** | `2. get_file_skeleton` | 新規追加 | 関数本体を省略したファイル内の全型・関数シグネチャ一覧取得（複数ファイル対応） |
| **A. 探索・骨格把握** | `3. search_workspace_symbols` | 新規追加 | 曖昧一致・型シグネチャ・種別によるワークスペース横断シンボル高速検索 |
| **B. 定義・参照・コード取得** | `4. resolve_symbols` | `resolve_symbol を拡張` | 複数シンボルの定義位置・厳密な型・Docコメント・URNを一括解決 |
| **B. 定義・参照・コード取得** | `5. get_symbol_bodies` | 新規追加 | 指定したシンボル（複数可）の実装コード本体のみをピンポイント抽出 |
| **B. 定義・参照・コード取得** | `6. find_references_and_implementations` | 新規追加 | 指定シンボルの全参照箇所、インターフェース実装クラス、基底型を一括検索 |
| **B. 定義・参照・コード取得** | `7. query_call_hierarchy` | `query_call_hierarchy を改善` | 呼び出し元（Incoming）/ 呼び出し先（Outgoing）の階層ツリーと条件分岐コンテキスト取得 |
| **C. 診断・影響分析** | `8. get_diagnostics` | 新規追加 | ワークスペースまたは指定ファイルのLSP構文・型エラー、Lint警告、QuickFix候補を即時取得 |
| **C. 診断・影響分析** | `9. analyze_change_impact` | 新規追加 | 指定シンボルを変更・削除した場合に破壊される呼び出し元・型依存・関連テストを特定 |
| **D. 構造化編集・リファクタリング** | `10. edit_symbol_body` | 新規追加 | 行番号指定なしでシンボル（関数・メソッド・クラス）単位で安全に置換・挿入＋即時診断 |
| **D. 構造化編集・リファクタリング** | `11. rename_symbol` | 新規追加 | LSP `textDocument/rename` によりプロジェクト全体の定義・全参照箇所を安全に一括改名 |
| **D. 構造化編集・リファクタリング** | `12. structural_search_replace` | 新規追加 | [ast-grep](https://ast-grep.github.io/) 構文パターンによるプロジェクト横断のAST構造検索および一括書き換え |
| **D. 構造化編集・リファクタリング** | `13. apply_code_action` | 新規追加 | 未インポートの自動追加、インターフェース実装スケルトン生成等のLSP Code Action適用 |
| **E. 仕様・論理・フロー検証** | `14. find_code_paths` | `find_code_paths を高度化` | 起点から終点への制御フロー・データフロー（変数伝播・Taint経路）をDatalogで抽出 |
| **E. 仕様・論理・フロー検証** | `15. extract_code_contracts` | `extract_specification_logic を刷新` | 型・Docコメント・アサーション・OpenAPIから決定論的に事前/事後条件・不変条件を抽出 |
| **E. 仕様・論理・フロー検証** | `16. verify_contract` | `verify_contract を実用化` | 抽出された契約またはエージェント指定の論理制約をZ3 SMTソルバで境界検証し反例を生成 **カテゴリG: プロジェクトDB直接クエリ＆スキーマ内省** `19. get_project_db_schema` (新規追加) — DB全10テーブル/ViewのDDL、レコード件数統計、即座に使えるサンプルSQL集の取得 `20. query_project_db` (新規追加) — 読み取り専用SQL/Datalog実行エンジン。複合横断条件・集計クエリの一発実行 |

### **カテゴリ0：ワークスペース・ターゲット環境管理ツール（Workspace & Target Management Layer）**

#### **5.0 `configure_workspace`**

解析対象となるプロジェクトフォルダー（ルートディレクトリ）、モノレポ内の対象パッケージ、使用するPython仮想環境（インタープリタパス）、除外パスルール、およびLSPワーカーの稼働状態を確認・設定・再インデックス化する管理ツール。セッション開始時またはターゲット切り替え時に呼び出す。

24. **入力スキーマ (JSON Schema)**:  
    1. `action` (`string`, optional, default: `"status"`): `"status"`（現在の認識状態とLSP稼働状況の取得） \| `"set_roots"`（対象フォルダーの登録・切り替え） \| `"set_python_env"`（Python仮想環境・インタープリタの明示指定） \| `"reindex"`（キャッシュ強制更新）。  
    2. `root_paths` (`array of string`, optional): 解析対象とするプロジェクトフォルダーの絶対パスまたは相対パス配列。  
    3. `python_interpreter` (`string`, optional): `pyright` 等が参照するPythonバイナリまたは `.venv` パス（省略時は `uv` / `.venv` / `poetry` / `conda` を自動検出）。  
    4. `exclude_globs` (`array of string`, optional): `.gitignore` に加えて除外するGlobパターン（例: `["**/node_modules/**", "**/dist/**", "**/.venv/**"]`）。  
    5. `default_base_ref` (`string`, optional, default: `"HEAD"`): 差分比較や影響分析時の基準Gitリファレンス（例: `"main"`, `"origin/develop"`）。  
25. **出力内容**:  
    1. `active_workspaces`: 登録されている各プロジェクトの `workspace_id`、ルートパス、検出された主要言語ティア、Python仮想環境パス、現在のGitブランチと未コミット変更ファイル数。  
    2. `lsp_daemons`: 言語別のLSPプロセス稼働状態（`"ready"`, `"indexing (80%)"`, `"standby"`）とSQLite/SCIPキャッシュの最終同期時刻。

---

## **カテゴリA：コードベース俯瞰・骨格把握ツール（Exploration & Skeleton Layer）**

### **5.1 `get_repo_map`**

リポジトリ全体のディレクトリ構造と、参照頻度（コールグラフおよびインポートグラフ上のPageRankスコア）が高い中核シンボル（公開クラス、主要インターフェース、エントリポイント関数）を抽出し、指定トークン数以内に収まる俯瞰マップを生成する。

* **入力スキーマ (JSON Schema)**:  
  * `scope_paths` (`array of string`, optional): 対象を絞り込むディレクトリまたはGlobパターンの配列（省略時はワークスペース全体）。  
  * `include_signatures` (`boolean`, optional, default: `true`): シンボル名だけでなく引数・戻り値の型シグネチャを含めるか。  
  * `max_tokens` (`integer`, optional, default: `1500`): 出力マップの最大トークン予算。  
* **出力内容**: PageRankスコア順にランク付けされたファイルツリーと主要公開シンボルのCompact DSLリスト。

### **5.2 `get_file_skeleton`**

1つまたは複数のソースファイルについて、インポート文、型定義、クラス構造、関数・メソッドのシグネチャ、およびDocコメントの要約のみを残し、関数本体（実装ブロック）を折りたたんだ「ファイル骨格（Skeleton）」を返す。`.ipynb`（セル出力ストリップ済みのコード/Markdown骨格）、`.xml`/`.json`/`.yaml`（データ行を排したタグ・キー階層構造と要素数サマリー）、`.dot`/`.mmd`（ノード・サブグラフ・エッジ数の構造要約）にも対応する。未知言語に対してはUniversal Ctagsおよびインデント構造によるアウトライン抽出し、`[DEGRADED]` フラグを付与して返却する。

* **入力スキーマ (JSON Schema)**:  
  * `paths` (`array of string`, required): 対象ファイルの相対パス配列（最大20ファイルまで一括指定可能）。  
  * `include_private` (`boolean`, optional, default: `true`): プライベートメンバーを含めるか。  
  * `include_doc_summary` (`boolean`, optional, default: `true`): Docコメントの1行目要約を含めるか。  
  * `max_tokens` (`integer`, optional, default: `2000`): 出力全体の最大トークン数。  
* **出力例（Compact Format）**:

\=== src/services/payment.ts (245 lines, skeleton: 28 lines) \===

imports: \[Decimal from 'decimal.js', OrderRepository, PaymentGateway\]

\[Interface\] PaymentRequest @L12-18

  orderId: string; amount: Decimal; currency: 'JPY' \| 'USD'

\[Class\] PaymentProcessor @L25-240

  \[Constructor\] constructor(repo: OrderRepository, gateway: PaymentGateway) @L28-32 { ...4 lines }

  \[Method\] public processCharge(req: PaymentRequest): Promise\<Receipt\> @L40-115

    doc: "注文に対する決済を実行し領収書を発行する。amount \> 0 が必須。"

    urn: "scip-ts npm app src/services/payment.ts/PaymentProcessor\#processCharge()."

    body: { ...75 lines omitted }

  \[Method\] private validateLimit(amount: Decimal): boolean @L117-140 { ...23 lines omitted }

### **5.3 `search_workspace_symbols`**

シンボル名（部分一致・ファジー検索・正規表現）、シンボル種別（Class, Function, Interface, Variable等）、または型シグネチャ（例：引数に `PaymentRequest` を取り Promise\<Receipt\> を返す関数）によってワークスペース全体を高速検索する。

* **入力スキーマ (JSON Schema)**:  
  * `query` (`string`, required): シンボル名キーワードまたは正規表現。  
  * `symbol_kinds` (`array of string`, optional): `"class" | "interface" | "function" | "method" | "struct" | "enum" | "type" | "constant"` のフィルタ。  
  * `type_signature_filter` (`string`, optional): 引数型や戻り値型の部分一致フィルタ（例: `"PaymentRequest -> Receipt"`）。  
  * `scope_paths` (`array of string`, optional): 検索対象ディレクトリ制限。  
  * `limit` (`integer`, optional, default: `30`): 最大取得件数。  
* **出力内容**: マッチしたシンボルのURN、ファイルパス、行範囲、型シグネチャの一覧。

---

## **カテゴリB：シンボル解決・コード部分取得・参照追跡ツール（Symbol & Navigation Layer）**

### **5.4 `resolve_symbols`（旧 `resolve_symbol` のバッチ・高精度化版）**

ファイルパス＋シンボル名（または行・列位置、あるいはURN）のリストを受け取り、コンパイラ（LSP/SCIP）が解決した正確な定義元位置、完全な型定義（ジェネリクス展開後の型を含む）、Docコメント全文、および一意なシンボルURNを一括返却する。

* **入力スキーマ (JSON Schema)**:  
  * `targets` (`array of object`, required): 解決対象シンボルの配列。各要素は以下のいずれかを指定：  
    * `urn` (`string`, optional): 既知のシンボルURN  
    * `path` (`string`, optional): ファイルパス  
    * `symbol_name` (`string`, optional): シンボル名（`Class.method` 形式可）  
    * `line` (`integer`, optional, 1-indexed): 行番号  
    * `character` (`integer`, optional, 1-indexed): 列番号  
  * `resolve_type_definitions` (`boolean`, optional, default: `true`): 引数や戻り値に含まれるカスタム型の定義場所も併せて解決するか。  
* **出力内容**: 各ターゲットに対する確定URN、定義ファイルと行範囲、展開済み型シグネチャ、Docコメント、および関連するカスタム型の定義URNリスト。

### **5.5 `get_symbol_bodies`**

ファイル全体を読み込むことなく、指定された1つ以上のシンボル（関数、メソッド、クラス、型定義）の実装コード本体だけをASTノード境界に従って正確に切り出して返す。XML/JSON/YAML等に対するXPath（例: `symbol_name: "//testcase[failure]"`）やJSONPath/JQセレクタ、`.ipynb` のセル指定（例: `symbol_name: "cell:3"`）も指定可能。精度レベルが `"lexical_fallback"` の場合は自動的に内部 `ripgrep` で該当シンボル周辺（±15行）のコンテキストウィンドウを切り出して返却する。

* **入力スキーマ (JSON Schema)**:  
  * `targets` (`array of object`, required): 各要素は `urn` (`string`) または `{ path: string, symbol_name: string }`。  
  * `context_lines` (`integer`, optional, default: `2`): シンボル定義直前のデコレータ・コメント等の前後コンテキスト行数。  
  * `with_line_numbers` (`boolean`, optional, default: `true`): 各行に `@L42:` 形式の行番号プレフィックスを付与するか。  
  * `max_tokens` (`integer`, optional, default: `3000`): 出力トークン上限。  
* **出力内容**: 対象シンボルごとの正確な実装コードブロックと、その内部で参照されている外部シンボルの簡易URNリスト。

### **5.6 `find_references_and_implementations`**

指定したシンボルがプロジェクト内のどこで参照（呼び出し・読み書き・型利用）されているか、また対象がインターフェースや抽象メソッド・Rustトレイトである場合にどの具象クラス・構造体がそれを実装（Implementation）しているかをLSP/SCIPで決定論的に列挙する。`format` パラメータで `"compact"` \| `"json"` \| `"dot"` \| `"mermaid"` を選択可能。未知言語や未パース設定ファイル横断のクエリ時は、内部 `ripgrep` に自動フォールバックし `_resolution_meta.is_degraded = true` を返却する。

* **入力スキーマ (JSON Schema)**:  
  * `target` (`object`, required): `urn` または `{ path, symbol_name, line }`.  
  * `query_type` (`string`, optional, default: `"all"`): `"references"` \| `"implementations"` \| `"type_hierarchy"` \| `"all"`.  
  * `include_tests` (`boolean`, optional, default: `true`): テストファイル（`test/`, `*.spec.*` 等）内の参照を含めるか。  
  * `context_snippet` (`boolean`, optional, default: `true`): 参照箇所の該当1行コードスニペットを含めるか。  
  * `cursor` (`string`, optional): ページネーション用カーソル。  
* **出力内容**: 実装クラス一覧、および参照箇所（ファイルパス、行番号、包含する親関数URN、該当行の1行コード片、Read/Writeの区別）のリスト。

### **5.7 `query_call_hierarchy`**

指定された関数・メソッドを起点として、それを呼び出している上位関数群（`incoming` / Callers）またはその関数が内部で呼び出している下位関数群（`outgoing` / Callees）を指定深度まで木構造で追跡する。出力は `format: "compact" | "json" | "dot" | "mermaid"` に対応。旧仕様書からの改善点として、単なる呼び出し有無だけでなく **「どの条件分岐（ガード節やtry/catchブロック）の内部で呼ばれているか」** の要約を付与する。未知言語等で精度低下時は `_resolution_meta.is_degraded = true` を付与して自動フォールバック出力する。

* **入力スキーマ (JSON Schema)**:  
  * `target` (`object`, required): `urn` または `{ path, symbol_name }`.  
  * `direction` (`string`, required): `"incoming"` \| `"outgoing"` \| `"both"`.  
  * `depth` (`integer`, optional, default: `2`, max: `5`): 探索深度。  
  * `include_guard_conditions` (`boolean`, optional, default: `true`): 呼び出し箇所を囲む `if` / `match` 等の条件式をASTから抽出して付記するか。  
  * `exclude_external_stdlib` (`boolean`, optional, default: `true`): 標準ライブラリや外部パッケージへの呼び出しを除外するか。  
* **出力内容**: インデント表現されたコールツリー、呼び出し箇所の行番号、および呼び出しガード条件（例：`when: !user.isAuthenticated()`）。

---

## **カテゴリC：リアルタイム診断・変更影響分析ツール（Diagnostics & Impact Layer）**

### **5.8 `get_diagnostics`**

シェルでビルドコマンドやリンターを実行することなく、バックグラウンドのLSPサーバーおよびTree-sitterが検知している最新の構文エラー、型エラー、未解決インポート、非推奨警告をミリ秒単位で取得する。さらにLSPが提供するQuickFix（Code Action）が存在する場合はそのアクションIDも返す。

* **入力スキーマ (JSON Schema)**:  
  * `paths` (`array of string`, optional): 特定ファイルのリスト（省略時はワークスペース全体のエラー・警告を集計）。  
  * `min_severity` (`string`, optional, default: `"warning"`): `"error"` \| `"warning"` \| `"information"` \| `"hint"`.  
  * `include_code_actions` (`boolean`, optional, default: `true`): エラーに対する自動修正候補（QuickFix）を併せて取得するか。  
* **出力内容**: ファイル別の診断リスト（Severity、エラーコード、行・列範囲、エラーメッセージ、該当行スニペット、利用可能な `code_action_id`）。

### **5.9 `analyze_change_impact`**

コーディングエージェントが関数シグネチャの変更や型の修正を行う前に、「このシンボルを変更するとどこに波及し、どのテストを実行すべきか」を事前に算出する影響範囲分析ツール。静的なLSP/Datalog呼び出し・型グラフに加え、自動的に **Git Co-change Coupling（過去のGit履歴における共変更結合度）** を統合し、静的グラフには現れない設定ファイル（`.yaml`, `.toml`, `.env`）や緩やかに結合したスクリプトの連動変更リスクも併せて警告する。

* **入力スキーマ (JSON Schema)**:  
  * `target` (`object`, required): 変更予定のシンボル（`urn` または `{ path, symbol_name }`）。  
  * `change_kind` (`string`, optional, default: `"signature_change"`): `"signature_change"` \| `"behavior_change"` \| `"deletion"`.  
  * `max_depth` (`integer`, optional, default: `3`): 推移的依存の追跡深度。  
* **出力内容**:  
  * `direct_breakage_candidates`: 引数・戻り値型の変更で直接コンパイルエラーになる可能性が高い呼び出し元・実装箇所リスト。  
  * `transitive_dependents`: 間接的に影響を受けるモジュール群。  
  * `affected_test_targets`: 対象シンボルへ到達可能なコールパスを持つテスト関数・テストファイルの一覧（エージェントが最小限のテストだけを実行できるようにする）。

---

## **カテゴリD：安全な構造化編集・リファクタリングツール（Safe AST/LSP Mutation Layer）**

### **5.10 `edit_symbol_body`**

従来の「行番号指定パッチ」や「文字列一致置換」の失敗を根絶するため、シンボル（関数・メソッド・クラス・型定義）を単位としてASTノード境界で安全にコードの置換・メソッド追加・削除を行う。編集適用後、直ちにLSPの差分診断を実行し、新たなエラー混入の有無をレスポンスに含める。`confidence_level` が `"heuristic"` または `"lexical_fallback"` のファイルに対する変更時は、誤書き換え防止のインターロックとして `allow_heuristic_mutation` (`boolean`, optional, default: `false`) の明示的な指定を必須とする。

* **入力スキーマ (JSON Schema)**:  
  * `path` (`string`, required): 対象ファイルパス。  
  * `symbol_name` (`string`, required): 対象シンボル名（例: `AuthService.login`）または `urn`。  
  * `operation` (`string`, required): `"replace_body"`（定義全体を置換） \| `"insert_before"`（直前に挿入） \| `"insert_after"`（直後に挿入） \| `"insert_member"`（クラス/構造体内部の末尾にメンバー追加） \| `"delete_symbol"`（シンボル削除）。  
  * `new_code` (`string`, optional): 置換または挿入する新しいコード文字列（インデントは周辺ASTノードに合わせてサーバー側で自動正規化される）。  
  * `dry_run` (`boolean`, optional, default: `false`): `true` の場合はファイルを書き換えずにUnified Diffと予想されるLSP診断結果のみを返す。  
* **出力内容**:  
  * `applied` (`boolean`): 適用成否。  
  * `diff_summary` (`string`): 変更箇所のコンパクトなDiffと更新後の行番号範囲。  
  * `new_diagnostics` (`array`): この編集によって当該ファイルおよび直接の参照元ファイルで新規発生・解消したLSPエラー一覧（ゼロであれば即座に作業完了と判断できる）。

### **5.11 `rename_symbol`**

LSPの `textDocument/prepareRename` および `textDocument/rename` プロトコルを使用し、変数、関数、クラス、インターフェース、プロパティの名前をワークスペース全体で安全に一括変更する。単なる文字列置換ではないため、同名の別スコープ変数やコメント内の無関係な単語を誤って書き換えることがない。`confidence_level` が `"heuristic"` または `"lexical_fallback"` のファイルに対して実行する場合は安全装置が作動するため、`allow_heuristic_mutation: true` (`boolean`, optional, default: `false`) を明示的にセットする必要がある。

* **入力スキーマ (JSON Schema)**:  
  * `target` (`object`, required): `urn` または `{ path, symbol_name, line }`.  
  * `new_name` (`string`, required): 新しい識別子名。  
  * `dry_run` (`boolean`, optional, default: `false`): 変更されるファイルと箇所の一覧のみをプレビューするか。  
* **出力内容**: 更新されたファイル数、変更箇所の総数、変更されたファイルパス一覧、およびリネーム後のLSP診断結果。

### **5.12 `structural_search_replace`**

[ast-grep](https://github.com/ast-grep/ast-grep) エンジンを内蔵し、正規表現では不可能な「構文木パターン（メタ変数 `$VAR` や `$$$ARGS` を含むコードテンプレート）」によるプロジェクト横断のコード検索および一括書き換え（Codemod）を実行する。

* **入力スキーマ (JSON Schema)**:  
  * `language` (`string`, required): 対象言語（`"typescript"`, `"python"`, `"rust"`, `"go"` 等）。  
  * `pattern` (`string`, required): 検索するASTパターン（例: `console.log($MSG)` や `try { $$$BODY } catch ($ERR) {}`）。  
  * `rewrite` (`string`, optional): 書き換え後のASTテンプレート（例: `logger.info($MSG)`）。省略時は検索のみ実行。  
  * `scope_paths` (`array of string`, optional): 対象ディレクトリ・ファイル制限。  
  * `dry_run` (`boolean`, optional, default: `true`): 書き換え時のプレビューフラグ。  
* **出力内容**: マッチした箇所一覧（または書き換えDiff）と、書き換え後の構文・型エラー診断サマリー。

### **5.13 `apply_code_action`**

`get_diagnostics` で提示されたQuickFixや、特定行で利用可能なLSP Code Action（不足しているimport文の自動追加、インターフェースの未実装メソッドのスタブ自動生成、未使用インポートの整理など）を実行する。

* **入力スキーマ (JSON Schema)**:  
  * `path` (`string`, required): ファイルパス。  
  * `line` (`integer`, required): 対象行番号。  
  * `action_kind_or_id` (`string`, required): `get_diagnostics` で取得した `code_action_id`、または標準種別（`"quickfix"`, `"source.organizeImports"`, `"refactor.extract"`）。  
* **出力内容**: 適用された変更のDiffと、解消された診断エラーのリスト。

---

## **カテゴリE：仕様抽出・データフロー・形式論理検証ツール（Symbolic & Verification Layer）**

### **5.14 `find_code_paths`**

起点シンボル（Source）から終点シンボル（Sink）へ至る制御フロー（Control Flow）またはデータフロー（Data Flow / 変数の値伝播経路）をDatalog推論エンジンによって高速抽出する。セキュリティ監査（ユーザー入力がサニタイズされずにSQL実行やコマンド実行に到達するか）や、バグ原因調査（特定の状態変数がどの経路で書き換えられるか）に威力を発揮する。

* **入力スキーマ (JSON Schema)**:  
  * `source` (`object`, required): 起点の `urn` または `{ path, symbol_name, variable_name }`.  
  * `sink` (`object`, required): 終点の `urn` または `{ path, symbol_name, variable_name }`.  
  * `flow_type` (`string`, optional, default: `"both"`): `"control"` \| `"data"` \| `"both"`.  
  * `max_hops` (`integer`, optional, default: `8`): 探索する最大ホップ数。  
  * `required_intermediate_filter` (`string`, optional): 経路上を通過すべき（または通過していないことを検証したい）サニタイザ・バリデータ関数名（例: `"!sanitizeHtml"` で未サニタイズ経路のみを抽出）。  
* **出力内容**: 到達可能な経路のリスト。各経路はステップ順のシンボルURN、ファイル・行番号、渡される変数・引数名、および途中の分岐条件式（Guard）で構成される。

### **5.15 `extract_code_contracts`（旧 `extract_specification_logic` の決定論的刷新版）**

旧仕様書のようにMCP内部で重量級NLPやLLMを回すのではなく、対象ファイルまたはドキュメントから **決定論的（ゼロLLM・ミリ秒単位）** に抽出可能なすべての「契約（Contracts：事前条件・事後条件・不変条件・スキーマ制約）」を構造化して返す。

* **抽出ソース**:  
  1. 関数の型シグネチャ（Nullable/Optional型、Union型、リテラル型、RustのResult/Option）。  
  2. 関数冒頭のガード節・アサーション文（`if (x < 0) throw ...`, `assert!(x > 0)`, `require(...)`）。  
  3. 構造化Docコメント（JSDoc `@param`, `@returns`, `@throws`、Python Docstring、Rustdoc、および `@requires` / `@ensures` タグ）。  
  4. 対応するOpenAPI / JSON Schemaファイル内のバリデーション制約（`minimum`, `maxLength`, `enum` 等）およびGherkin `.feature` ファイルの `Given / When / Then` テーブル。  
* **入力スキーマ (JSON Schema)**:  
  * `path` (`string`, required): ソースファイルまたは仕様書（OpenAPI YAML / Markdown / Gherkin）のパス。  
  * `symbol_name` (`string`, optional): 特定関数に絞り込む場合のシンボル名。  
* **出力内容**: シンボルごとの構造化契約オブジェクト（`preconditions`, `postconditions`, `thrown_exceptions`, `invariants`）およびZ3互換のSMT-LIB v2論理式表現。

### **5.16 `verify_contract`**

対象関数の実装ロジック（ASTから変換されたSSA制御フローと算術・論理演算式）が、抽出された契約またはコーディングエージェントが明示的に与えた検証式（Precondition / Postcondition）を満たすかどうかを [Z3 SMTソルバ](https://github.com/z3prover/z3) で数学的に検証し、違反する可能性があれば **具体的な反例入力値（Counterexample）** を生成する。

* **入力スキーマ (JSON Schema)**:  
  * `target` (`object`, required): 検証対象関数の `urn` または `{ path, symbol_name }`.  
  * `preconditions` (`array of string`, optional): 前提条件の論理式リスト（例: `["amount > 0", "balance >= 0", "discount_rate >= 0 && discount_rate <= 100"]`）。省略時は `extract_code_contracts` の結果を自動適用。  
  * `postconditions` (`array of string`, required): 満たすべき事後条件のリスト（例: `["return >= 0", "new_balance == balance - result"]`）。自然言語仕様書を検証したい場合、エージェント自身が自然言語をこの簡潔な比較・論理式に変換して渡すことで、MCP内部でのLLM呼び出しを不要にする。  
  * `check_implicit_safety` (`boolean`, optional, default: `true`): ゼロ除算、整数オーバーフロー、境界外アクセス、Nullデリファレンスの暗黙的安全性も同時検証するか。  
  * `loop_unroll_depth` (`integer`, optional, default: `3`): ループの最大展開回数（Bounded Model Checking）。  
  * `timeout_ms` (`integer`, optional, default: `500`): ソルバのタイムアウト時間（ミリ秒）。  
* **出力内容**:  
  * `status` (`string`): `"VERIFIED"`（すべての入力で事後条件を充足） \| `"VIOLATED"`（反例を発見） \| `"BOUNDED_VERIFIED"`（ループ展開深さ内で充足） \| `"UNKNOWN_OR_TIMEOUT"`（非線形演算や外部動的呼び出しにより判定不能）。  
  * `counterexample` (`object`, optional): `"VIOLATED"` の場合、契約違反を引き起こす具体的な引数・状態変数の値マップ（例: `{"amount": 100, "discount_rate": 100, "division_by_zero_at_line": 54}`）。  
  * `execution_path_trace` (`array`): 反例が辿った分岐行番号と各ステップでの変数値の推移。  
  * `unmodeled_calls` (`array`): 関数内で呼び出されている外部関数のうち、副作用や戻り値が未定義のため未解釈関数（Uninterpreted Function）として扱われたシンボルのリスト。

## **カテゴリG：プロジェクトDB直接クエリ＆スキーマ内省ツール（Direct Project DB Query Layer）**

カテゴリA〜Fの定型ツール（Parameterized Views / Stored Proceduresに相当）では表現しきれない複雑な横断条件・集計・JOINを、エージェントが1回のリクエストで自在に引き出すための「Project-as-a-Database」中核インターフェースである。

### **5.19 `get_project_db_schema`**

プロジェクト知識DB内の全テーブル・ビュー定義、カラム名と型、利用可能なインデックス、現在格納されているレコード件数統計（例：`symbols: 14,230 rows`, `relations: 48,910 rows`, `diagnostics: 12 rows`）、および頻出クエリテンプレート集を返す。

* **入力スキーマ (JSON Schema)**:  
  * `include_sample_queries` (`boolean`, optional, default: `true`): 代表的な横断分析用SQL/Datalogクエリ例を含めるか。  
* **出力内容**: 全10テーブルのDDL要約、各テーブルの現在の行数、言語別カバレッジとフォールバック比率（`exact: 94%`, `lexical_fallback: 6%`）、および即座に使えるサンプルSQL集。

### **5.20 `query_project_db`（Read-Only SQL / Datalog / 構造化グラフクエリ実行エンジン）**

正規化されたプロジェクト知識DBに対して、読み取り専用のSQL（SQLite方言：再帰CTE `WITH RECURSIVE`、JSON関数、[FTS5全文検索](https://www.sqlite.org/fts5.html) 対応）またはDatalogクエリを実行し、コンパクトなMarkdownテーブル、CSV、JSON、またはGraphviz DOT形式で結果を返す。

* **入力スキーマ (JSON Schema)**:  
  * `query_language` (`string`, optional, default: `"sql"`): `"sql"` \| `"datalog"`.  
  * `query` (`string`, required): 実行するSELECTクエリまたはDatalogルール（例：上記の複合JOINクエリ）。  
  * `params` (`object`, optional): プリペアドステートメント用のバインドパラメータ（SQLインジェクション防止とキャッシュ再利用のため）。  
  * `auto_fallback_grep_pattern` (`string`, optional): クエリ結果が0件、または未知言語ファイルを含む場合に、内部 [ripgrep](https://github.com/burntsushi/ripgrep) で一時テーブル `temp_grep_matches(path, line, snippet, confidence_level)` を動的生成してJOIN可能にする正規表現パターン。  
  * `timeout_ms` (`integer`, optional, default: `300`): クエリ実行タイムアウト（暴走クエリ防止）。  
* **安全制約とガードレール**:  
  * データベース接続は厳格な `SQLITE_OPEN_READONLY`（読み取り専用モード）および `PRAGMA query_only = ON;` で実行され、スキーマ破壊やデータ書き換えは一切不可能とする（コードの変更は必ずカテゴリDのAST/LSP編集ツール経由で行うことで整合性を保証する）。  
  * 仮想マシン命令数上限（`sqlite3_progress_handler`）を設定し、無限再帰CTE等が発生しても300msで安全に中断してエラーを返す。  
* **出力内容**:  
  * クエリ結果のコンパクトテーブル（または指定された `format: "dot" | "mermaid" | "json"`）。  
  * `_resolution_meta`: クエリ結果に `confidence_level != 'exact'`（未知言語のCtags/grep推定行）が含まれている場合、その割合と注意喚起フラグを自動付与する。

## **カテゴリF：Git時系列・セマンティック差分解析ツール（Git Temporal & Semantic History Layer）**

### **5.17 `get_symbol_history`（シンボル単位のSemantic Git Log & Blame）**

ファイル全体の膨大な `git log -p` や行単位の `git blame` を読んで数千トークンを消費する代わりに、**特定の関数・クラス・メソッド（シンボル）だけに絞った変更履歴、直近のコミットメッセージ（設計意図・PR背景）、および現在の実装ブロックの主要作成時期** を [gitoxide](https://github.com/gitoxidelabs/gitoxide) \+ Tree-sitterの過去リビジョンAST比較によって高速抽出する。さらに、そのシンボルが変更される際に「一緒に変更されることが多い関連ファイル（Co-changed files）」も算出する。

26. **入力スキーマ (JSON Schema)**:  
    1. `target` (`object`, required): `urn` または `{ path, symbol_name }`.  
    2. `max_commits` (`integer`, optional, default: `5`): 取得する直近コミット数。  
    3. `include_semantic_diff` (`boolean`, optional, default: `true`): 各コミットで「シグネチャが変わったか」「ガード条件が追加されたか」「内部ロジックのみの変更か」のAST差分要約を含めるか。  
    4. `include_co_changed_files` (`boolean`, optional, default: `true`): 当該シンボルと共変更頻度（Temporal Coupling）が高い他ファイル・テストの上位リストを含めるか。  
27. **出力内容**:  
    1. シンボルを変更した直近コミット一覧（Commit SHA、日時、作成者、コミットメッセージ要約、そのコミットでの当該シンボルのみのコンパクトDiff）。  
    2. `co_changed_hotspots`: 過去の履歴上、このシンボルと同時に変更される確率が高いファイル/シンボル一覧（例: `tests/test_payment.py (88%)`, `config/limits.yaml (62%)`）。

### **5.18 `analyze_git_diff_symbols`（PR・ブランチ・未コミット変更のセマンティック構造化差分）**

現在の作業ツリーの変更（Unstaged / Staged）またはブランチ間差分（例: `main..HEAD`）について、単なるテキストの行差分（`git diff`）ではなく、**「どのシンボル（関数・クラス・型）が追加・削除・シグネチャ変更・内部実装変更されたか」** をASTレベルで構造化し、さらに変更された全シンボルに対する `analyze_change_impact`（破壊される呼び出し元と実行すべきテスト）を一括算出する。エージェントが改修作業の最終確認（セルフレビュー）や、他者の変更内容の把握を最小トークンで行うための切り札となる。

28. **入力スキーマ (JSON Schema)**:  
    1. `base_ref` (`string`, optional, default: `"HEAD"`): 比較元のGitリビジョン（`"HEAD"`, `"STAGED"`, `"main"`, コミットSHA等）。  
    2. `target_ref` (`string`, optional, default: `"WORKING_TREE"`): 比較先のリビジョン。  
    3. `include_impact_and_tests` (`boolean`, optional, default: `true`): 変更されたシンボル群に依存する未変更ファイルの呼び出し元リスクと、実行すべきテストターゲット一覧を同時計算するか。  
29. **出力内容**:  
    1. `modified_symbols`: 変更種別（`SIGNATURE_CHANGED`, `BODY_MODIFIED`, `ADDED`, `DELETED`, `RENAMED`）ごとのシンボルURNリストとコンパクトな変更要約。  
    2. `uncommitted_diagnostics`: 変更された行範囲に存在する最新のLSPエラー・警告。  
    3. `recommended_test_suite`: 今回のGit差分で影響を受けるため実行すべき最小限の `pytest` / テストターゲット一覧。

---

# **6\. stdioトランスポート・LSPプロセス管理・キャッシュ運用仕様**

実用的なMCPサーバーとして安定稼働させるため、旧仕様書で完全に欠落していたシステム運用面のプロトコル仕様を以下に定める。

## **6.1 `stdio` トランスポートの厳格なストリーム隔離規則**

[MCPのstdioトランスポート仕様](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports) では、クライアント（エージェント）とサーバーは標準入力（`stdin`）および標準出力（`stdout`）上の改行区切りJSON-RPC 2.0メッセージで通信する。

30. **stdoutへの非JSON出力の絶対禁止**: 子プロセスとして起動する各言語のLSPサーバーやTree-sitter、Z3ソルバが誤って `stdout` にバナーやデバッグログを出力すると、MCPのJSON-RPCストリームが即座に破損しセッションが切断される。そのため、MCPサーバー本体は起動直後に自身のログ出力先をすべて `stderr` またはローテーション付きログファイル（`~/.cache/dlsr-mcp/logs/`）へバインドし、子LSPプロセスの `stdout` / `stderr` は必ずパイプ（Pipe）で捕捉してMCPの `stdout` と完全に隔離する。  
31. **UTF-8 / ポジションエンコーディングの統一**: LSPサーバーによって列番号（Character offset）の計算単位が異なる問題（UTF-16がLSP標準だが、RustやTree-sitterはUTF-8バイトオフセットまたはUnicodeコードポイントを使用）を吸収するため、MCPサーバー内部で [LSP 3.17以降の `positionEncodings: ["utf-8", "utf-16"]` ネゴシエーション](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/) を行い、エージェント向けの入出力はすべて **1-indexedの行番号（Line）およびUTF-8文字単位の列番号（Column）** に正規化する。

## **6.2 バックグラウンドLSPプールとインクリメンタル同期**

1. **オンデマンド遅延起動（Lazy Initialization）**: ワークスペース内にPython、TypeScript、Rustが混在している場合でも、起動時に全LSPを立ち上げてメモリを消費するのではなく、該当言語のファイルに対するツール要求が初めて到着した時点で該当LSPを非同期起動する。  
2. **インデックス完了待機とフォールバック（Graceful Degradation）**: 大規模プロジェクトで `rust-analyzer` や `pyright` の初期インデックス構築に数秒かかる場合、Fast Pathはまず即座に利用可能な **Tree-sitter AST \+ 永続化済みSQLite/SCIPキャッシュ** から0.01秒で回答を返し、レスポンスメタデータに `"lsp_status": "indexing (65%)"` を付記する。これにより、エージェントがLSPの起動完了を待ってブロックされる時間をゼロにする。  
3. **ファイル変更ウォッチャーと仮想ドキュメント同期**: エージェントがMCP外部の標準ファイル編集ツールや `git checkout` を実行した場合でも、`notify`（OSネイティブのファイルシステムイベント監視：Linux `inotify`, macOS `FSEvents`, Windows `ReadDirectoryChangesW`）によって変更を5ms以内に検知し、変更ファイルのSHA-256ハッシュを比較した上で、Tree-sitter ASTの再パース、SQLiteシンボルテーブルの差分更新、および稼働中LSPへの `workspace/didChangeWatchedFiles` / `textDocument/didChange` 通知を自動実行する。  
4. **ゾンビプロセス防止（Orphan Reaper）**: エージェント（親プロセス）が異常終了し `stdin` がEOFに達した場合、または `SIGTERM` / `SIGINT` を受信した場合、MCPサーバーは管理下の全LSP子プロセスに対して `shutdown` → `exit` シグナルを送出し、500ms以内に終了しない子プロセスを確実に `SIGKILL` してメモリリークを防止する。

---

# **7\. 採用技術スタックの刷新（v1.0 vs v2.0 比較）**

旧仕様書（v1.0）ではPython系NLPライブラリ、複数の外部グラフDB、重量級LLMデコーダが混在しており、インストール難易度と実行時メモリ・レイテンシが実用に耐えない構成となっていた。v2.0〜v2.2では **「単一のRust製スタティックバイナリ（または軽量パッケージ）として配布可能で、外部DBサーバーやGPUを一切必要としない構成」** へと刷新する。

| コンポーネント領域 | 旧仕様書（v1.0）の選定 | 本仕様書（v2.0）の刷新選定 | 刷新の理由・技術的根拠 |
| :---- | :---- | :---- | :---- |
| **コア実装言語・MCP SDK** | `@modelcontextprotocol/sdk` (TypeScript) \+ Python混在 | **Rust (`rmcp` / 公式MCP Rust SDK)** または **TypeScript (Node/Bun) \+ Rust Native Addon (`napi-rs`)** | Tree-sitter、ast-grep、SCIP、Z3、SQLiteのすべてがC/Rustネイティブであり、単一バイナリ化とマルチスレッド並列解析においてRustが圧倒的に有利なため。 |
| **構文解析・構造検索** | `tree-sitter` のみ | [**Tree-sitter**](https://github.com/tree-sitter/tree-sitter) \+ [**ast-grep (`ast-grep-core`)**](https://github.com/ast-grep/ast-grep) | 構文木の生成だけでなく、エージェントによる構文パターン検索と安全な一括書き換え（Codemod）を高速実行するため。 |
| **LSP / シンボル解決** | `scip`, `pyright`, `clangd`, `rust-analyzer` | **Multiplexed LSP Client** \+ [**SCIP Protobuf Indexer**](https://github.com/sourcegraph/scip) | 静的なSCIPインデックスによる高速キャッシュと、動的なLSPによるリアルタイム型検査・Rename・Diagnosticsを統合するため。 |
| **グラフ・シンボルDB** | SQLite \+ Neo4j / [KùzuDB](https://github.com/kuzudb/kuzu) / [CozoDB](https://github.com/cozodb/cozo) の多重構成 | **SQLite (WALモード \+ インメモリグラフキャッシュ)** 単一構成 | Neo4j等の別サーバー起動はstdio型MCPの導入障壁となり、KùzuDB/CozoDBも開発停滞リスクがある。コールグラフとシンボル参照はSQLiteのインデックス＋オンメモリ隣接リスト（`petgraph`）で100万行規模でも数ミリ秒で走査可能。 |
| **仕様・コメント論理抽出** | KNP, JUMAN++, GiNZA, spaCy, amrlib \+ Outlines (LLM) | **決定論的AST/Docコメントパーサ \+ OpenAPI/Gherkinパーサ**（自然言語翻訳はホストLLMへ委譲） | MCP内部で數GBのNLPモデルやLLMを動かすとレイテンシが数秒〜数十秒に悪化しトークンも消費する。MCP側は100%決定論的・ミリ秒動作の構文抽出に特化する。 |
| **フロー推論エンジン** | [Soufflé](https://github.com/souffle-lang/souffle) / CozoDB | **Rust組み込みDatalog (`crepe` / `ascent`) \+ `petgraph` CFG/DFG解析器** | 外部プロセスの呼び出しオーバーヘッドなしに、インメモリで到達可能性やTaint解析を数ミリ秒で完了させるため。 |
| **SMTソルバ（契約検証）** | `z3-solver` (Python) | [**Z3 Theorem Prover (`z3` Rust/C API)**](https://github.com/z3prover/z3)（タイムアウト・深さ制限付き） | 関数単位の境界条件検証・反例生成に限定し、500ms以内のBounded Model Checkingとして実用的な応答速度を保証するため。 |

---

# **8\. コーディングエージェントのワークフロー改善比較（定量的効果検証）**

本仕様のMCPサーバーを導入した場合、典型的なタスク「大規模TypeScript/Pythonプロジェクトにおける決済処理のバグ調査と仕様変更（引数追加と境界チェック強化）」において、エージェントの行動とトークン消費・作業時間がどう変化するかを比較する。

| フェーズ | 従来のエージェント操作（Grep \+ ReadFile \+ EditPatch \+ Bash） | 本MCPサーバー（v2.0）を用いたエージェント操作 | トークン・時間の削減効果 |
| :---- | :---- | :---- | :---- |
| **1\. 関連箇所の特定** | `grep` でキーワード検索し、ヒットした5〜10個のファイル（計3,000行）を `read_file` で全文読み込む。（約18,000トークン消費 / 3〜4ターン） | `search_workspace_symbols` または `get_file_skeleton` でシグネチャ骨格のみを確認し、`get_symbol_bodies` で対象メソッド2個（計60行）のみを取得。（約650トークン消費 / 1〜2ターン） | **トークン 96%削減** / 探索ターン半減 |
| **2\. 影響範囲の調査** | 関数名で再度 `grep` し、同名メソッドの誤検知を人間またはLLMが目視でフィルタリング。インターフェース経由の呼び出しを見落とす。 | `analyze_change_impact` と `find_references_and_implementations` を1回呼び出し、コンパイラ精度で全呼び出し元と実行すべきテストファイル一覧を取得。（約400トークン消費 / 1ターン） | **見落としゼロ** / 調査トークン 90%削減 |
| **3\. コード編集とリファクタリング** | 各ファイルに対して行番号指定や文字列置換パッチを複数回実行。インデント不一致で2回失敗し再試行。（約6,000トークン消費 / 4ターン） | `rename_symbol` で一括改名し、`edit_symbol_body` で対象関数をAST単位で置換。レスポンス内の `new_diagnostics` で型エラーゼロを即時確認。（約500トークン消費 / 1ターン） | **パッチ失敗ゼロ** / 編集・ビルド確認時間を **数十秒→50msに短縮** |
| **4\. 境界条件・仕様の検証** | テストコードを新たに書いてシェルでテストランナーを実行し、エッジケース（割引率100%時のゼロ除算等）を見落とす。 | `verify_contract` に事後条件 `["return >= 0"]` を渡してZ3で検証し、`discount_rate: 100` でのゼロ除算反例を0.2秒で自動検出。 | テスト記述前に数学的反例を即時特定 |
| **合計（1タスクあたり）** | **累計消費: 約28,000〜45,000トークン / 所要時間: 90〜180秒（10〜14ターン）** | **累計消費: 約2,000〜3,500トークン / 所要時間: 15〜30秒（3〜5ターン）** | **総トークン約90%削減 / 作業時間約80%短縮** |

---

# **9\. 段階的実装ロードマップ（実用性優先アプローチ）**

旧仕様書では「フェーズ2で自然言語解析（KNP/GiNZA/Outlines）を作り、フェーズ3で初めてエージェント結合テストを行う」というウォーターフォール型の計画になっていたが、これでは早期にコーディングエージェントの効率を高めることができない。本仕様では、**フェーズ1の完了時点で直ちに実戦投入でき、大幅なトークン削減と時間短縮を体感できるアジャイルな3段階ロードマップ** を策定する。

## **フェーズ1：Core Fast-Path MVP（目標期間：2〜3週間）— 即時実戦投入・トークン90%削減の達成**

* **実装対象**:  
  * stdio JSON-RPC基盤（stdout完全隔離、Compact Symbol DSLフォーマッタ、`max_tokens` バジェット制御）。  
  * Project-as-a-Database (PaaD) 統合SQLiteエンジン（10テーブルのWALモードインメモリ/ディスクキャッシュ構成と `get_project_db_schema`, `query_project_db` の実装）。各読み取りツール（`resolve_symbols`, `get_diagnostics`等）は本DB上の最適化済みパラメータ化クエリ（View）として構築。  
  * Multiplexed LSP Client Pool（まずはTypeScript/JavaScript, Python, Rust, Go, C/C++の主要LSPに対応）。  
  * **実装ツール（計14ツール）**:  
    * カテゴリ0: `configure_workspace`  
    * カテゴリA: `get_repo_map`, `get_file_skeleton`, `search_workspace_symbols`  
    * カテゴリB: `resolve_symbols`, `get_symbol_bodies`, `find_references_and_implementations`, `query_call_hierarchy`  
    * カテゴリC: `get_diagnostics`  
    * カテゴリD: `edit_symbol_body`, `rename_symbol`, `apply_code_action`  
* **マイルストーン成果**: この時点でClaude CodeやClineに導入可能となり、ファイル全文読み込みの廃止・AST単位の安全な編集・ミリ秒単位のコンパイルエラー検知が稼働する。  
  * カテゴリG: `get_project_db_schema`, `query_project_db`

## **フェーズ2：構造化リファクタリング＆データフロー・影響範囲解析（目標期間：2週間）**

* **実装対象**:  
  * [ast-grep](https://github.com/ast-grep/ast-grep) コアの組み込みによる `structural_search_replace` ツールの実装。  
  * ASTから制御フローグラフ（CFG）および変数定義・使用関係（Def-Use Chain）を抽出し、組み込みDatalog（`crepe` / `ascent`）へ投入するコンバータ。  
  * [SCIP](https://github.com/sourcegraph/scip) インデックスのインポート/エクスポート対応（大規模リポジトリのコールドスタート高速化）。  
  * [gitoxide (gix)](https://github.com/gitoxidelabs/gitoxide) ネイティブ統合によるインメモリSemantic Git History走査および共変更結合度（Temporal Coupling）解析。  
  * **実装ツール（計5ツール）**: `analyze_change_impact`, `find_code_paths`, `structural_search_replace`, `get_symbol_history`, `analyze_git_diff_symbols`。  
* **マイルストーン成果**: プロジェクト横断の一括構文書き換え（Codemod）、変更前の破壊範囲・関連テスト特定、およびセキュリティTaint経路の決定論的追跡が可能になる。

## **フェーズ3：決定論的コントラクト抽出＆Z3 SMT境界検証（目標期間：2〜3週間）**

* **実装対象**:  
  * 型シグネチャ、ガード節、JSDoc/Docstring、OpenAPIスキーマ、Gherkin仕様からの決定論的ルールベース制約抽出器（LLM不使用）。  
  * 関数内SSA（静的単一代入）中間表現から [Z3 Theorem Prover](https://github.com/z3prover/z3) ASTへのトランスパイラ（ループ展開上限・タイムアウト制御付き）。  
  * **実装ツール（計2ツール）**: `extract_code_contracts`, `verify_contract`。  
* **マイルストーン成果**: テストケースを人間やエージェントが書く前に、数値境界バグ・ゼロ除算・Null参照・仕様違反の具体的入力値（反例モデル）を数学的に検出する完全なSymbolic Reasoning MCPサーバーが完成する。

---

# **10\. 参考文献・標準仕様・技術リファレンス**

1. **Model Context Protocol (MCP) Specification**: [Model Context Protocol Transports & Architecture Specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports) — stdioトランスポートにおけるJSON-RPC 2.0メッセージングおよびストリーム分離の標準仕様。  
2. **Language Server Protocol (LSP) Specification**: Microsoft, [Language Server Protocol Specification 3.18](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/) — 定義解決、参照検索、Rename、Code Action、DiagnosticsおよびUTF-8/UTF-16位置エンコーディングの標準規格。  
3. **SCIP (Source Code Intelligence Protocol)**: Sourcegraph, [SCIP Protocol & Cross-Repository Code Navigation](https://sourcegraph.com/blog/cross-repository-code-navigation) — コンパイラ精度のシンボルURN表現およびインデックス標準フォーマット。  
4. **Tree-sitter Incremental Parsing System**: Max Brunsfeld et al., [Tree-sitter Official Documentation](https://tree-sitter.github.io/) & [GitHub Repository](https://github.com/tree-sitter/tree-sitter) — 高速・インクリメンタルな具象構文木（CST）生成およびS式クエリエンジン。  
5. **ast-grep Structural Search & Rewrite**: Herrington Darkholme, [ast-grep Documentation](https://ast-grep.github.io/) & [GitHub Repository](https://github.com/ast-grep/ast-grep) — Tree-sitterベースの構文パターンマッチングおよび大規模コード書き換えエンジン。  
6. **Z3 Theorem Prover (SMT Solver)**: Leonardo de Moura & Nikolaj Bjørner (Microsoft Research), [Z3 Prover GitHub Repository](https://github.com/z3prover/z3) — 事前・事後条件の充足可能性判定（SAT/SMT）および反例モデル生成エンジン。  
7. **Soufflé Datalog Engine**: Bernhard Scholz et al., [Soufflé Static Analysis Datalog Engine](https://github.com/souffle-lang/souffle) — プログラムの到達可能性・ポインタ解析・データフロー推論を高速実行するDatalog処理系。  
8. **元仕様書（v1.0 たたき台）**: [決定論的コード・仕様統合論理解析MCPサーバー仕様書](https://docs.google.com/document/d/1gpk-MuaAnhFdAc9bHK5L38fcAK7ss4s44tZpfROcpk8/edit) — 本v2.0仕様書のベースとなった初期コンセプト設計書。

