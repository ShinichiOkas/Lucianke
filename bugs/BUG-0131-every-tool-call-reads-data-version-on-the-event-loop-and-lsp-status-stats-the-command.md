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

## 原因・直し方・確認（2026-10-31）

原因: (1) `IndexWriter.data_version()` が呼び出しのたびに錠をとり、大きさ 1 の `ReaderPool` の接続で `SELECT data_version FROM _meta` を
読んでいた（イベントループのスレッドの上で同期。書き手・索引・LSP のスレッドが GIL を持っていると、1 回の読みが標本で約 0.8 ms に伸び、
並行する呼び出しは錠と大きさ 1 の接続で並ぶ）。(2) 編集系の `lsp_status`（`EditTransaction` の言語ごとの LSP の状態）が `LspPool.status` を呼び、
standby のときの「次に起こす名前」のために優先順で `find_command`（`workspace_venv_dirs`・`venv_root_of`・`_probe` の stat）を毎回走らせていた。
編集に要るのは状態だけで、名前は使わない。

直し:
- `IndexWriter` は書き手スレッドが開いた直後（`ensure_schema` の後）に `_meta.data_version` を 1 回読んで覚え、`bumps_version` のジョブの
  トランザクションの中で `+1` した値を読み、`COMMIT` が通った後（Future に結果を載せる前）にメモリの値を更新する。自分のコミットは
  `data_version()` からすぐ見える。失敗（ROLLBACK）・取り消し・`bumps_version=False` は進めない。
  **別プロセスのコミット**: 同じ根を 2 つのプロセスが開くと同じ `paad.sqlite` を共有し、両方が書き手になる（プロセス間の錠は無く
  busy_timeout だけ）ので、「書き手は 1 つ」とは言えない。B が上げた版を A がメモリだけで見ないと、A は古い答えのキャッシュを返し続けうる
  （A の監視も同じ変更に気づくが DB の sha256 が新しく書かない）。そこで `data_version()` は、前回見に行ってから `external_check_s`
  （既定 25 ms。`EXTERNAL_CHECK_S`）以上たっていれば、検知用の読み取り接続 1 本で `PRAGMA data_version`（接続ごと。他の接続のコミットで変わる）を
  読み、前回の印と違うときだけ `_meta` を読み直す。ほかのスレッドが見に行っている最中は待たずに覚えた値を返す。閉じるときは検知用の
  接続を、最後の接続を閉じる書き手より先に閉じる（-wal / -shm が消える）。他のプロセスの書き込みが見えるまでの古さの上限は 25 ms
  （自分のコミットは 0）。呼び出しごとに見に行かないのは、GIL を放す呼び出し（sqlite・stat）はスレッドが混んでいると 1 回で ms 単位
  待つため（下の測定）。stat（WAL の mtime・size）は PRAGMA より遅く、Windows のファイル時刻にも頼るので採らなかった。
  ツールの読み取りの中で行と同じ断面の値が要るときは、これまでどおり借りた接続で `read_data_version(conn)`。
- `LspPool.state_of(language_id, ws, role)` を足した（`status(...).status` と同じ値。起動コマンドを探さない）。`_runtime.py` の編集用の
  `lsp_status` はこれを使う（`state_of` を持たない偽のプールは今までどおり `status`）。名前が要る `status`（T-00・T-08 など）は変えていない。
  `find_command` の結果の覚えは作っていない（編集の経路から呼ばなくなったので不要。覚えにすると venv の作り直しの無効化が要る）。

確認（同じプロセスの中で繰り返し。`perf_counter`、5 組の最小）:
```
data_version  DB を読む（旧）0.0053 ms → メモリ 0.0000 ms（静かな状態の 1 回。標本で出た 0.8 ms は GIL の取り合いの分で、読みが無くなったので
              その待ちも無い。錠も無いので並行する呼び出しは並ばない）
lsp_status    status（.venv のある workspace、standby の python）0.2865 ms → state_of 0.0008 ms
```
GIL を取り合うスレッドがいるときの 1 回（同じ DB・同じプロセス。最小 / 中央値 ms）: PRAGMA data_version 静か 0.003、busy 1 本 0.79 / 3.9、
busy 3 本 41 / 59。stat（WAL）0.006・1.9 / 7.4・31 / 42。メモリの値は 0.0000 のまま。
試験: 新しい `tests/unit/test_fast_path_fixed_cost_bug0131.py`（10 件。初めの 6 件: DB を読まない、DB の値と常に一致〈複数回のコミット・失敗・診断だけ・再起動〉、
取り消しは進めない、閉じた後の例外、並行して読んでも単調、`state_of` が resolver を呼ばず `status` と同じ値。足した 4 件: 同じ DB を別々に開いた 2 つの `IndexWriter` で、片方のコミットがもう片方の `data_version()` に出る〈間隔 0 で即・既定の間隔で待てば〉、間隔の間は DB を読まない、閉じたら -wal が消える）。関係する unit（index_core・
lsp_pool・tools_runtime・leak_guard・mcp_pipeline・budget_cursor・engine_indexer・tools_index_building）・contract（T-03/T-04・T-10・T-11）・見張り 4 本が
green、`uv run ruff check` クリーン。
