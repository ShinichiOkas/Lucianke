# BUG-0042 roots を宣言しないクライアントで、最初のツール呼び出しが 8〜10 秒ブロックされ cwd の索引が始まる

- 報告: 2026-10-29 / 見た人: dogfood-2（SDK の `mcp.Client`。roots を宣言しない stdio クライアント）
- 症状: ワークスペースが未登録の状態での最初のツール呼び出しが 8〜10 秒ブロックする。
  - 実測: 最初の `configure_workspace {action: "set_roots", root_paths: [<F-POLY のコピー>]}` が 8,208〜8,439 ms（4 回とも 8.2 秒台）。別の回の最初の `configure_workspace {action: "status"}`（ルート登録前）は 10,289 ms。2 回目以降の `set_roots` は 137 ms。
  - サーバーのログ: `WARNING lucianke: no roots decision after 8 s; indexing the cwd` の直後に `index opened: ws=<cwd 名>` と `scan job job_0001 started: ws=<cwd 名>`（作業ディレクトリ＝このリポジトリ全体の索引が始まる）。その後 `set_roots` で F-POLY のコピーが登録される。
  - 影響: クライアントが `roots` を宣言しない（`roots/list` を持たない）とき、`set_roots` を最初に呼んでも 8 秒待たされ、頼んでいない cwd の索引が始まる。NFR-PERF-1（p95 < 50 ms）の対象外の初回とはいえ、`tools/call` の既定のタイムアウトを短くしたクライアントでは初回が失敗しうる。
- 期待: `initialize` で roots を宣言していないクライアントには待たずに決める（または `set_roots` の呼び出しが決定として待ちを終える）。
- 再現手順: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> cfg`（`obs2_cfg.json` の `status_before_roots_ms`）。または SDK の `Client(StdioServerParameters(python -m lucianke))` で最初に `set_roots` を呼んで時間を測る。
- 調査はしていない（直す役の仕事）。
