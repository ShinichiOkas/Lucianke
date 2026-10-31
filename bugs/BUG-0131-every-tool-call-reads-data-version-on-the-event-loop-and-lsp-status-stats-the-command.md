# BUG-0131 どのツールの呼び出しも、イベントループの上で data_version を 1 本の接続で読み、LSP の状態のために起動コマンドを stat する

見たことだけを書く（調査していない）。2026-10-31、BUG-0128 の直す役が F-PY（httpx）でプロセス内に本番と同じ結線を組み、呼び出しの間の全スレッドの
スタックを 1 ms ごとに採った標本と cProfile（`asyncio.to_thread` をその場で呼ぶようにしてイベントループのスレッドだけで回す）を見ていて気づいた。

## 症状
- `Pipeline._run`（`src/lucianke/mcp/pipeline.py` 221 行あたり）が呼び出しのたびに `data_version_provider()` を呼び、`RuntimeHub.data_version` →
  `WorkspaceRuntime.data_version` → `IndexWriter.data_version`（`src/lucianke/index/writer.py` 165 行）が、錠をとって大きさ 1 の `ReaderPool` の接続で
  `SELECT data_version FROM _meta` を読む。イベントループのスレッドの上で同期に読んでいる。標本では T-01・T-04・T-10・T-11 のどれでも
  `connection.py:read_data_version:136` が 1 回あたり約 0.8 ms 出た（静かな状態。80 回で 52〜71 標本）。大きさ 1 なので、並行する呼び出しはここで並ぶはず。
- 編集系（T-10 の dry_run）で、`_runtime.lsp_status` → `LspPool.status` → `_expected_name` → `_pick_for` → `default_resolver` → `servers.find_command` が
  呼び出しのたびに `pyenv.workspace_venv_dirs`・`venv_root_of`・`_probe` で `stat` していた（cProfile で 1 回約 1 ms、`--no-lsp` でも出る）。

## 再現
BUG-0128 の「切り分けの方法」と同じ（本番と同じ結線をプロセス内に組み、`bench/run_latency.py` の `PLANS` の引数で同じツールを繰り返し、
`sys._current_frames()` を 1 ms ごとに採る、または cProfile）。

## 仕様書
§14.4（Fast Path は p95 < 50 ms）、§3.2 NFR-PERF-1。関連: BUG-0128。
