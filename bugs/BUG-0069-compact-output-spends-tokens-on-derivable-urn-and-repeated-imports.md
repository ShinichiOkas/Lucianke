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
