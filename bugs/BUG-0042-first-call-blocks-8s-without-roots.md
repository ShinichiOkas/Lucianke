# BUG-0042 roots を宣言しないクライアントで、最初のツール呼び出しが 8〜10 秒ブロックされ cwd の索引が始まる

- 報告: 2026-10-29 / 見た人: dogfood-2（SDK の `mcp.Client`。roots を宣言しない stdio クライアント）
- 症状: ワークスペースが未登録の状態での最初のツール呼び出しが 8〜10 秒ブロックする。
  - 実測: 最初の `configure_workspace {action: "set_roots", root_paths: [<F-POLY のコピー>]}` が 8,208〜8,439 ms（4 回とも 8.2 秒台）。別の回の最初の `configure_workspace {action: "status"}`（ルート登録前）は 10,289 ms。2 回目以降の `set_roots` は 137 ms。
  - サーバーのログ: `WARNING lucianke: no roots decision after 8 s; indexing the cwd` の直後に `index opened: ws=<cwd 名>` と `scan job job_0001 started: ws=<cwd 名>`（作業ディレクトリ＝このリポジトリ全体の索引が始まる）。その後 `set_roots` で F-POLY のコピーが登録される。
  - 影響: クライアントが `roots` を宣言しない（`roots/list` を持たない）とき、`set_roots` を最初に呼んでも 8 秒待たされ、頼んでいない cwd の索引が始まる。NFR-PERF-1（p95 < 50 ms）の対象外の初回とはいえ、`tools/call` の既定のタイムアウトを短くしたクライアントでは初回が失敗しうる。
- 期待: `initialize` で roots を宣言していないクライアントには待たずに決める（または `set_roots` の呼び出しが決定として待ちを終える）。
- 再現手順: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> cfg`（`obs2_cfg.json` の `status_before_roots_ms`）。または SDK の `Client(StdioServerParameters(python -m lucianke))` で最初に `set_roots` を呼んで時間を測る。
- 調査はしていない（直す役の仕事）。

## HEAD での再確認（dogfood-2、2026-10-29 15:21、42640ac の使い捨て作業ツリー）
- HEAD でも再現する: ro の最初の set_roots が 8,402 ms、cfg の最初の status が 11,705 ms。

## 原因
- 再現: SDK の `mcp.Client` の既定は `mode="auto"` で、`server/discover` を送り、2026-07-28 版（modern）を採る。この版には `initialize` も `notifications/initialized` も無く、サーバー起点の `roots/list` もできない（SDK 2.3 の `ServerSession.can_send_request` が偽）。`mode="legacy"`（`initialize` の握手）なら、roots 能力が無くても `initialized` の通知で即座に cwd が決まり、最初の呼び出しは 0.4 秒で返る（実測）。
- 根の決定を起こすのが `notifications/initialized` のハンドラ（`on_roots_event`）だけだったので、modern の接続では決定が一度も来ず、`Pipeline.ready_gate`（`hub.wait_started`）が退避の 8 秒（`ROOTS_FALLBACK_S`）まで最初の `tools/call` を止め、退避が cwd の索引を始めた。`status` の 10.3 秒は、ゲートの上限 `STARTUP_WAIT_S`（10 秒）に近い値。
- 退避を除いて決定の手段が無いうえ、`set_roots`（T-00）も同じゲートで待たされた（根を決めるツールが根の決定を待つ循環）。ゲートを外すだけでは足りず、`WiredServices.resolve_workspace_id(None)` が登録前に cwd 由来の ID（`ws`）を返し、T-00 が「未登録の ID」で `E_WORKSPACE_NOT_CONFIGURED` になる（`roots()` に cwd が常にあるため、BUG-0004 の「ルートが無い間は None」の条件が効いていなかった）。
- ゲートを外すと、退避のタイマー（と遅れて届く cwd の決定）が、`set_roots` で決めた根を cwd で上書きする。

## 直し方
- `RootsState.client_can_list`（`mcp/server.py`）: 要求を受けるたびに、相手が `roots/list` に答えられるか（roots 能力の宣言あり かつ `can_send_request`）を記録する。`on_call_tool` が `pipeline.call_tool` の前に更新する。
- `app.RootsDecision`（新規）が根の決定を 1 か所にまとめる。`Pipeline.ready_gate` はツール名と生の引数を受けるようにした（`ready_gate(spec.name, raw)`）。
  - `gate`: `client_can_list` が偽（roots を渡せない相手）なら、cwd を待たずに既定にする（`on_roots(cwd)`、同時の呼び出しでも 1 回）。ただし T-00 `set_roots` は決定そのものなので、待たず、cwd の索引も始めずに通す。`None`（不明）・真（宣言あり）は従来どおり `hub.wait_started` で待つ（Claude Code の経路は変えない。宣言のある相手の `set_roots` も従来どおり待つ。`roots/list` の結果が後から上書きしないため）。
  - `on_roots`: cwd 由来の決定は、すでに誰かが根を決めていれば（`hub.started`）上書きしない。クライアントの `roots/list` の結果（`list_changed` を含む）は常に適用する。
  - `fallback`: `not settled and not hub.started` のときだけ cwd にする。
- `RuntimeHub.started`（最初の `sync_roots` が済んだか）を足した。`WiredServices.resolve_workspace_id`: 束があるときは、`roots()` ではなく登録の有無で、登録が 1 つも無い間は `None` を素通しする。
- 試験用に `LUCIANKE_ROOTS_FALLBACK_S`（退避の秒数。正の数だけ有効）。
- テスト: `tests/unit/test_app_roots_decision.py`（14 件。ゲートの 6 通り・cwd が既存の決定を上書きしない・退避・ready_gate の引数・resolve_workspace_id）、`tests/e2e/test_roots_undeclared_stdio.py`（実プロセス 5 件: modern の最初の `set_roots`／`status` が 4 秒未満で返り、`set_roots` では cwd が登録されない／退避の期限〈2 秒に短縮〉を過ぎても根が残る／legacy で roots 無しは即 cwd／legacy で roots 宣言は `roots/list` の根になる）。コミットは 49aeb7a（スキーマ削減の役〈23a〉の共有の索引に載って同じコミットに入った。内容は本票の変更のみが対象）。

## 確認
- 直す前: modern の最初の `set_roots` 8.2 秒、`status` 8.2 秒（再現スクリプトで実測）。直した後: `set_roots` 125 ms（`other` の索引だけが開く）、`status` 157 ms（cwd）。
- 変異の確認: `hub.started` の保護を外すと、退避の期限（2 秒）の後に根が `other` から `ws` に戻る（e2e の「期限を過ぎても残る」がこれを捕まえる）。
- `tests/e2e` + `tests/invariants` 209 件 green、`tests/unit` + `tests/contract` 3429 件 green（`tests/contract/test_unsupported_tools.py::test_stdio_server_lists_and_answers_the_planned_tools_with_a_pure_stdout` は 23a の `tools/list` 変更との食い違いで赤かった。本票とは無関係）、`ruff check` green。
