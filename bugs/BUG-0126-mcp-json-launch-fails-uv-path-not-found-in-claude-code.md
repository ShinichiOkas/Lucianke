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

## 原因・直し方・確認（2026-10-31）

### 原因

- 17 回の失敗はすべて、`.mcp.json` の `--directory` に **別 PC の絶対パス `S:/work/develop/Lucianke`** が入っていた頃の起動だった。
  それを「.」に直した `cf4497c`（2026-10-31T03:15:46+09:00 = 2026-10-30T18:15:46Z。作業ツリーの `.mcp.json` の更新は 03:15:22 JST）より前で、
  最後の 1 個（`2026-10-30T17-04-18-949Z.jsonl`、02:04 JST）も直す 1 時間 11 分前。この PC には S: ドライブが無い（`Test-Path S:\` は False）。
- `uv` は `--directory` の先へ引数を読んだすぐ後に移り、先が無いと文脈なしの io エラーだけを出して終了コード 2 で落ちる
  （手元で: `--directory .\no_such_dir` は `os error 2`、`--directory .\no_such_dir\sub` と `--directory S:/work/develop/Lucianke` は
  `os error 3`）。だから Lucianke の Python まで届かず、Lucianke のログに `starting` が無かった。
- 票を書いた時点でこのセッション（`337bc537`、02:04 JST に起動）が切れて見えていたのは、直す前に起動したまま起動し直していないから
  （ホストは起動するときに `.mcp.json` を読む。03:15 の後、このプロジェクトの `mcp-logs-lucianke` に接続の記録は 2026-10-31T07:09Z まで無い）。
  Git Bash から手で流した 15:57 JST の確かめは直した後の「.」だったので動いた。shell・環境変数・cwd の違いではない
  （下の「確認」のとおり、同じ環境で shell なしでも動く）。
- 別の失敗の形（同じ根ではない）: clone した直後・新しい worktree では最初の `uv run` が `.venv` を作って依存を入れるので、
  Claude Code の接続の上限 30 秒に届いて `CONNECT_TIMEOUT` になる（この worktree で `claude mcp list` を最初に流したとき:
  依存 65 個を入れるだけで 15.54 秒、Lucianke の `starting` が出る前に 30.05 秒で打ち切り）。2 回目からはつながる。

### 直し方（決めたこと）

- 起動の形は直さない（`cf4497c` の `uv run --directory . python -P -m lucianke` のまま。見張り
  `tests/unit/test_server_launch_guard.py::test_host_config_launches_python_dash_m_not_the_console_script` が `args[2] == "."` を検める）。
  マシン固有のパスは焼き込まない。
- README の開発メモに書いた: clone した直後・新しい worktree では Claude Code を開く前に `uv sync` を 1 回流す（30 秒の上限）、
  `.mcp.json` を変えたら Claude Code を起動し直す、文脈なしの `os error 3` は `--directory` の先が無いということ、
  つながるかは `claude mcp list` で確かめる・ホストのログの場所。
- 設計書 §11.2 の開発時の `.mcp.json` の例が `"<repo>"`（絶対パスを入れたくなる書き方）だったのを「.」にし、上と同じ注意を書いた。
  見張りの試験の docstring の `<repo>` も「.」に。

### 確認

すべてこの PC（Windows 11・uv 0.11.25）、cwd は作業ツリーの根 `C:\Users\0000950207\work\Lucianke`。

- 旧い引数の再現（PowerShell から Python の `subprocess.Popen([...], shell=False)`、initialize を 1 行送る）:
  `uv run --directory S:/work/develop/Lucianke python -P -m lucianke` → 終了コード 2、stdout 空、stderr は
  `error: 指定されたパスが見つかりません。 (os error 3)` だけ（ホストのログの文言と一字一句同じ）。
- 今の引数を同じ方法で: `uv run --directory . python -P -m lucianke` → initialize の応答
  （`serverInfo: lucianke 0.0.1`・`tools.listChanged: false`）が返り、stdin の EOF で終了コード 0（`shutdown done: 6/6 steps ok`）。
  MCP の SDK の `getDefaultEnvironment()` 相当の最小の環境変数（APPDATA・LOCALAPPDATA・PATH・TEMP・USERPROFILE など 12 個）だけでも同じ。
- 実のホストで: `claude mcp list`（Claude Code 2.1.314。shell を通さずに起こして initialize までする）を作業ツリーの根で流すと
  `lucianke: uv run --directory . python -P -m lucianke - ✔ Connected`。ホストのログ（`2026-10-31T07-09-43-326Z.jsonl`）は
  `Successfully connected (transport: stdio) in 4110ms`・`negotiatedProtocolVersion: 2026-07-28`・`hasTools: true`。
  この worktree でも 2 回目は `✔ Connected`（1 回目は上の `CONNECT_TIMEOUT`）。
- 対話のホスト（claude.exe、PID 9236）の cwd（PEB から読んだ `C:\Users\0000950207\work\Lucianke\`）・環境変数（PATH の `uv` は
  `%USERPROFILE%\.local\bin\uv.exe` の 1 つだけ・`VIRTUAL_ENV`・`UV_*` は無い）に、つながらない理由になるものは無かった。
- 試験: `tests/unit/test_server_launch_guard.py`・`tests/invariants/test_stdout_purity.py`・`tests/unit/test_import_graph.py`・
  `tests/unit/test_leak_guard.py`、`uv run ruff check`・`ruff format --check`。

### 残ったこと

- このセッションの `lucianke` は、Claude Code を起動し直せばつながる（`/mcp` の再接続で `.mcp.json` を読み直すかは確かめていない）。
