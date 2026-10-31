# BUG-0137 initialize が instructions を返さず、ホストのエージェントが Lucianke を使おうとしない

見たことだけを書く（調査していない）。2026-11-01、Claude Code（VS Code 拡張・Opus 5.5）で Lucianke を user スコープ
（`~/.local/bin/lucianke.exe`）と project スコープ（`.mcp.json`）に登録したセッションで見た。

## 症状
- `src` を `instructions` で検索すると 0 件。サーバーは `initialize` の結果に `instructions` を入れていない。
- 同じセッションのシステムプロンプトの「MCP Server Instructions」には、別の MCP（claude.ai Claude Docs）の instructions は載っていて、
  lucianke のものは無い。
- Claude Code では `mcp__lucianke__*` の 14 ツールは遅延読み込み（deferred）で、セッションの最初に見えるのは名前だけ。説明文
  （「First call in a new codebase」「Use instead of reading whole files」など）は ToolSearch で読み込むまでエージェントに見えない。
- そのセッションのエージェントは、Lucianke のリポジトリ自身の src を調べるのにも組み込みの Grep・Read・Bash を使い、Lucianke のツールを
  1 回も呼ばなかった。
- ツールをまたぐ流れ（get_repo_map → get_file_skeleton → get_symbol_bodies → edit_symbol_body の `expected_file_sha256`、
  `urn:` を次のツールに渡す、`max_tokens` と `cursor` で続きを取る、compact と json の使い分け）は、各ツールの説明文に散っていて、
  1 か所にまとめて書かれたものがない。

## 再現
1. Lucianke を MCP に登録した Claude Code で新しいセッションを始める。
2. システムプロンプトの「MCP Server Instructions」に lucianke の項が無いことを見る。
3. 「この関数の呼び元を探して」のようなコードの探索を頼むと、エージェントは Grep・Read で答える。

## 仕様書
§6.5（起動・initialize）。師匠の宣言 2026-10-31「Lucianke の主な利用者は呼ぶ側の AI。仕様の判断は AI にとっての有用さで AI が決めてよい」。

## 原因・直し方・確認（2026-11-01）

### 原因
- `build_server`（`mcp/server.py`）が公式 SDK の `Server(...)` に `instructions` を渡していなかった。SDK は `instructions` 引数を持つので、
  渡せば `initialize` の結果にそのまま載る。

### 直し方
- `src/lucianke/mcp/instructions.py` に `INSTRUCTIONS`（英語・約 1,350 文字・約 300 トークン）を置き、`build_server` が `Server(instructions=...)` に渡す。
  中身: Read・Grep・Glob より優先する場面（Python・TypeScript・JavaScript のコードの理解と変更。非コードのテキストや縮退・未対応は Read・Grep のまま）、
  標準の流れ（`get_repo_map` → `get_file_skeleton` → `get_symbol_bodies` → 検索・解決・参照・呼び出し階層・診断 → 編集は `expected_file_sha256` と
  `dry_run`）、規約（`urn:` を次のツールへ・`max_tokens` と `cursor`・compact が既定で json は形が固定・`configure_workspace` の `status`）。
  既定の `tools/list` に無いツール（M2 以降の計画分・開発用 `ping`）は書かない。
- 仕様書 §4.3.1 手順 2・設計書 §6.5 起動順 6 を更新（MUST）。

### 確認
- 新しい試験 `tests/e2e/test_initialize_instructions_bug0137.py`（実機の stdio。`instructions` が空でないこと、2,400 文字以下、
  載せたツール名がすべて既定の `tools/list` に実在すること）。関係する試験（縦切り e2e・スキーマ予算・stdout 純度）と合わせて 11 件 passed、
  `uv run ruff check` クリーン。
- `.\build.ps1`（`-Force` なし）で exe を作り直して `~/.local/bin/lucianke.exe` に入れ、実機の JSON-RPC ハンドシェイクで
  `initialize` の結果に `instructions` が載ることを見た。smoke PASSED。
- コミット: dcde056（コード・試験・仕様書・設計書・本票）。
