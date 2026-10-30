# BUG-0069 compact の出力が、導ける `urn:` 行と繰り返しの import にトークンを使い、既定の予算で骨格が切れる

- 報告: 2026-10-31 / 見た人: 親（dogfood。Lucianke 自身のリポジトリを stdio で索引して、AI の利用者として使った）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `cf4497c`、`uv run --directory . python -P -m lucianke`、cwd = リポジトリの根、LSP は indexing 中）

- `get_file_skeleton {"paths": ["src/lucianke/mcp/budget.py"]}`（既定 `max_tokens=2000`）: ヘッダ `~1885/2000tok`、`(522 lines, skeleton: 78 lines)`、6,647 文字。
  - 記号の行ごとに `  urn: lk://Lucianke/src/lucianke/mcp/budget.py#<修飾名>` の行が 1 行ずつ付く（先頭 1,500 文字で 6 記号・6 行）。値は `lk://<ws>/<path>#<修飾名>` で、直前の行のパスと修飾名から導ける。
  - `imports:` の 1 行が `Callable from collections.abc, Mapping from collections.abc, Sequence from collections.abc, ... Record from lucianke.core.records, ToolOutput from lucianke.core.records, join_offset from lucianke.core.records, split_offset from lucianke.core.records, ...` と、名前ごとにモジュール名を繰り返す。
- `search_workspace_symbols {"query": "Packer"}`: `hits=21`、`~1388/1500tok`。各ヒットにも `urn:` 行。2 件目は `Pipeline.__init__`（引数の型注釈に `Packer` を含む）で、約 600 文字のシグネチャを丸ごと出す。

## 利用者（呼ぶ側の AI）として困ること

- 既定の予算の多くが導ける情報に使われ、骨格が途中で切れる（同じ予算で見える記号が減る）。
- 名前が一致しないヒット（シグネチャの文字列一致）が上位に来て、長いシグネチャで予算を使う。

## 関連

- 師匠の宣言（2026-10-31）: 仕様の判断は呼ぶ側の AI にとっての有用さで決めてよい（ペア固有 Skill `the-tools-primary-user-decides-its-spec`）。

## 原因・直し方・確認（2026-10-31）

### 原因

- `urn:` 行: 仕様書 §6.1 規約 2 が「`search_workspace_symbols`・`resolve_symbols`・`get_file_skeleton` の公開シンボルには付ける（SHOULD）」だったので、実装（`syntax/skeleton.py` の `_emit`、`tools/t03_search_workspace_symbols.py` の `_record`、`tools/t04_resolve_symbols.py` の `_symbol_record`・`_module_record`）は、URN が直前の行のパスと修飾名の連結（`lk://<ws>/<path>#<qualified>`）と同じでも毎回 1 行出していた。
- `imports:` 行: `syntax/skeleton.py` の `extract_imports` が json 用の「1 名前 1 要素」（`Callable from collections.abc`）をそのまま compact にも並べていた。
- 検索の順位: T-03 の `_match_tier` が 完全一致 / 前方一致 / それ以外 の 3 段で、FTS がシグネチャ・Doc だけで当てたヒット（`Pipeline.__init__(…, packer: Packer | None = None, …)`）も名前の部分一致と同じ段だった。同じ段の中は `pagerank` 順なので、ファイルの順位しだいで上位に来た。シグネチャは compact でも全文（約 600 文字）。

### 直し方（仕様の判断。呼ぶ側の AI のトークンを使わない向き）

1. **導ける `urn:` 行を出さない**（仕様書 §6.1 規約 2 を MUST NOT に書き換え、導き方を明記）。`core/urn.py` に `derivable(urn, path, qualified, workspace_id=)`（文字列の連結で一致するか）、`core/compact_lines.py` に `urn_lines(...)`（導けるなら 0 行）を足し、T-02（入れ子をたどった修飾名で比べる）・T-03・T-04（`[Module] <path>` も）で使う。arity・sighash（オーバーロード・同名の再定義）、`lex:` などのセレクタ、パーセントエンコードの要るパス、別ワークスペースのものは導けないので従来どおり出す。`include_urns` は「付けてよい範囲」になった。縮退の注 `(doc, urn omitted)` は、`urn:` 行がもともと無ければ `urn` を挙げない。json（§6.2 の SymbolRecord）は変えない。
2. **`imports:` をモジュールごとにまとめる**（§6.1 規約 10 を新設、EBNF の注）。`imports: [base64; collections.abc: Callable, Mapping, Sequence; lucianke.core.records: Record, ToolOutput, join_offset, split_offset]`。項目の区切りは `; `、`from m import …` は `m: a, b as c`（同じモジュールの文が複数あっても 1 項目）、`import m` は `m`。別名は残す。JS / TS も同じ（`m: X, A as B`、`m: * as ns`、`m`）。json の `items[].imports[]` は従来の 1 名前 1 要素のまま。
3. **T-03 の順位と大きさ**: 名前・限定名に当たらない（シグネチャ・Doc だけの）ヒットを第 4 段（score 0.05〜0.25）にし、全部の名前の一致の後ろに並べる（§11 T-03 処理規約 2）。compact のヒットのシグネチャは 160 文字（`core/limits.py` の `SEARCH_SIGNATURE_MAX_CHARS`）で引数部を `…)` で閉じ、戻り値は残す（`core/compact_lines.py` の `cap_signature`。§6.1 規約 6・§11 T-03 処理規約 6）。json の `signature` は全文。
4. **呼ぶ側が見たもので記号を指せるか（確かめた）**: 記号を受けるツールは T-04・T-05・T-06・T-07・T-09・T-10・T-11・T-14・T-16・T-17 がどれも `urn` / `path` + `symbol_name` / `path` + `line` の 3 形式を受ける（`_nav.TargetRefArg`・`t05.TargetRefArg`・`core.model.TargetRef`）。`path` + `symbol_name` は `qualified_name` の完全一致を優先する（`_nav.resolve_target`）。導けない URN はオーバーロードなど `path` + `symbol_name` が曖昧になる記号なので、`urn:` 行で出る。未実装（M3 の入口だけ）の T-15 は仕様どおり `path` + `symbol_name` だけ（表示した `path#qualified` で指せる）。契約テスト `test_compact_symbols_are_addressable_from_what_is_shown` が、検索の compact に出た各記号を、導いた URN・`urn:` 行の URN・`path` + `symbol_name` で T-04・T-05 に渡して同じ記号に届くことを確かめる。

仕様書で変えた節: §6.1 の EBNF（`import-line` の注）、規約 2（導ける `urn:` 行は MUST NOT と導き方）、規約 6（T-03 の `sig` の 160 文字）、規約 10（新設: import の書式）、§6.1.1 の例、§11 T-02（`include_urns`・`include_imports` の表、処理規約 8、json の `imports[]`）、§11 T-03（処理規約 2 の順位、処理規約 6 を新設、例）、§11 T-04（処理規約 6、例）、§11 T-15 の例（導ける `urn:` 行を除いた）。

### 確認（実測。このリポジトリを stdio のサーバー〈`python -P -m lucianke`〉で索引して呼んだ。ws=`agent-ab559f5276b943940`〈作業ツリーの名前。`Lucianke` より 15 文字長いので urn 行 1 行あたりの削減は少し大きめに出る〉）

| 呼び出し | 前（08b946e） | 後 |
|---|---|---|
| `get_file_skeleton {"paths": ["src/lucianke/mcp/budget.py"]}`（既定 `max_tokens=2000`） | `~1377/2000tok`、記号 41。ただし全部入りが入らず `doc:` と `urn:` を落とした縮退版（`- truncated: 0 members omitted (doc, urn omitted)`、`doc:` 0 行） | `~1852/2000tok`、記号 41、**縮退なしの全部入り**（`doc:` 14 行・`urn:` 0 行） |
| 同、`max_tokens=20000`（全部入りの大きさ） | `~2962tok`（93 行、`urn:` 29 行 = 1,074 トークン、`imports:` 565 文字 = 111 トークン） | `~1853tok`（64 行、`urn:` 0 行、`imports:` 350 文字 = 77 トークン）。**−37%** |
| `search_workspace_symbols {"query": "Packer"}`（既定 1500） | `~1422/1500tok`、14 件、`urn:` 14 行（519 トークン）、最長の行 569 文字（`Pipeline.__init__`） | `~730/1500tok`、14 件、`urn:` 0 行、最長の行 230 文字。**−49%**。`Pipeline.__init__`・`pack_and_render`（シグネチャだけの一致）は名前の一致 12 件の後ろ |
| 同、`max_tokens=20000` | `~1422tok` | `~730tok` |

測定の台本: 作業ツリーの外（スクラッチ）の `probe0069.py`（`bench/_stdio.py` の `McpStdioClient` で `search_workspace_symbols` の件数が安定するまで待ってから 4 回呼ぶ）。

テスト（期待を仕様の変更に合わせて意図して変え、守りを足した）:

- `tests/contract/compact_grammar.py`: 規約 2 の検査を足した（`urn:` 行の値が、属性を持つ記号の行〈ファイルブロックのパス + 入れ子の修飾名、横断の `path#qualified`、`[Module] <path>`〉から導ける URN と同じなら違反）。検査器を使う全ツールの契約テストがこれで守られる。陽性対照は `tests/contract/test_compact_grammar.py`（導ける行を落とす・arity / sighash / `lex:` / 別ワークスペースは通す）。§6.1.1 の例を新しい形に。
- `tests/unit/test_syntax_skeleton.py`: 導ける URN を出さない・同名の再定義（`#f/1`・`#f/2`）は出す・入れ子（`C.m.inner`）は導ける・`include_urns` の範囲・縮退の注・TS のオーバーロードで doc → urn の順に落ちる・import のまとめ方（Python・TS）。
- `tests/unit/test_core_compact_lines.py`: `urn_lines`（導けるもの・導けないもの 5 種）、`cap_signature`、`import_line`。`tests/unit/test_mcp_render.py` の `import_line`。
- `tests/contract/test_T02_skeleton.py`: 全部入りに `urn:` 行が無いこと、縮退の注に `urn` を挙げないこと、TS のオーバーロードの `urn:` 行だけが出て json は全記号が `urn` を持つこと。
- `tests/contract/test_T03_T04.py`: compact に `urn:` 行が無いこと（T-03・T-04）、シグネチャだけの一致が名前の一致の後ろで score も低いこと、compact のシグネチャを 160 文字で切り json は全文、見たもので指せること（上の 4）。
- `bench/e2e/run_session.py`（e2e の台本）: 骨格の `urn:` 行を読んでいたので、行から URN を導く形に（`urn:` 行があればそれを使う）。

実行の結果: `uv run ruff check src tests bench` は clean。全体テスト（`uv run pytest -q -p no:cacheprovider`、8536a5f の上）は 3809 passed・29 skipped・1 failed（`tests/e2e/test_m1_6_session.py::test_m1_6_scripted_session_completes`。上の e2e の台本が `urn:` 行を読んでいたため。直して 2 passed）。`tests/contract_fixtures` は 39 passed・5 skipped。origin/main（BUG-0070・BUG-0071 の後）へ載せ直した後は対象のテスト（compact・骨格・検索・解決・文法・T-06/T-07 の name_calls・e2e の M1-6）を流して push した（全体テストは親が main でまとめて流す）。
