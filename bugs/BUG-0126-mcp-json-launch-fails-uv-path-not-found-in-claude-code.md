# BUG-0126 リポジトリの `.mcp.json` で Claude Code から起動すると、毎回 `uv` が「指定されたパスが見つかりません (os error 3)」で落ちてつながらない

- 報告: 2026-10-31 / 見た人: 親（このセッションの MCP サーバー `lucianke` が CONNECTION_CLOSED になっていたので、ログを見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 見たこと

- `.mcp.json`（リポジトリの根）: `"command": "uv", "args": ["run", "--directory", ".", "python", "-P", "-m", "lucianke"]`。
- Claude Code の MCP のログ（`%LOCALAPPDATA%\claude-cli-nodejs\Cache\C--Users-0000950207-work-Lucianke\mcp-logs-lucianke\*.jsonl`）は 17 個あり（2026-10-29T07:34Z〜2026-10-30T17:04Z）、
  **17 個すべて**同じ失敗だった。最後の 1 個（`2026-10-30T17-04-18-949Z.jsonl`、cwd は `C:\Users\0000950207\work\Lucianke`）の中身:
  - `Starting connection with timeout of 30000ms`
  - `version negotiation probe closed the stdio server (rmcp-class pre-init hard close); respawning pinned legacy`
  - `stderr of the stdio server before restarting it: error: 指定されたパスが見つかりません。 (os error 3)`
  - `Server stderr: error: 指定されたパスが見つかりません。 (os error 3)`
  - `Connection failed after 1530ms ... (CONNECTION_CLOSED): Connection closed`
- その時刻に Lucianke 自身のログ（`%LOCALAPPDATA%\lucianke\cache\logs\lucianke-2026-10-*.log`）に `starting` の行は無い（Lucianke の Python まで届いていない）。
- 同じコマンドを Git Bash から手で流すと正常に動く（2026-10-31 15:57 JST、`printf '<initialize>' | uv run --directory . python -P -m lucianke` → `initialize` の応答が返り、stdin の EOF で正常終了）。
- `~/.claude.json` に `lucianke` の定義は無い（使われているのはリポジトリの `.mcp.json`）。

## 利用者として困ること

- このリポジトリで Claude Code を開いても Lucianke のツールが一度も使えていない（開発中の当のツールを自分で使って確かめられない）。
- 配布物（exe）の案内でも、同じ形の `.mcp.json` を書く利用者が同じ目に遭うかもしれない。

## 再現

1. このリポジトリで Claude Code を起動する（または `/mcp` で再接続する）。
2. `lucianke` が CONNECTION_CLOSED になり、上の MCP のログに `os error 3` が出る。
