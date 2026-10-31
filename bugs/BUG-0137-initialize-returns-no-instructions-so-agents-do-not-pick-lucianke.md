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
