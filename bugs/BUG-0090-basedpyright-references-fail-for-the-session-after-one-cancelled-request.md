# BUG-0090 basedpyright の references の要求が 1 度取り消される（製品の期限 3 秒で切れる）と、同じ LSP のプロセスでその後の T-06 がすべて `structural` に降格する

- 報告: 2026-10-31 / 見た人: BUG-0080 を直す役
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `c7f2b7e` + BUG-0080 の直し、basedpyright 1.40.2、Windows 11）

F-PY の rig（`tests/contract_fixtures/_rig.open_rig`。本番と同じ結線）で、`warm_up(httpx/_urls.py)` の後に T-06
`find_references_and_implementations`（`URL.copy_with`、`query_type=references`、`include_tests=true`）を聞いた。

1. 普通に聞く → `exact`（29 件、1.7 秒、`engine_used=basedpyright+tree-sitter`）。続けてもう 1 回 → `lsp-cache` で `exact`。
2. 新しい rig で、`lucianke.lsp.features.LSP_TIMEOUT_REFERENCES_MS` を 1 にして 1 回聞く（負荷で 3 秒を超えたときと同じ: 要求を送り、
   時間切れで `$/cancelRequest` を送る）→ `structural`、警告「LSP の応答が 3000 ms 内に得られなかったため構文木で解決しました。…」。
3. 期限を 3000 に戻して聞く → 0.03 秒で `structural`、`lsp_status=ready`、警告「LSP が利用できないため構文木のみで解決しました。…
   `configure_workspace status` で LSP の状態を確認してください。…」。ログ（`lucianke.tools.t06`）:
   `LSP references failed; answering from the index: LuciankeError('LSP basedpyright failed textDocument/references: Request textDocument/references failed with message: this._token.cancel is not a function')`
4. 20 秒待ってから聞く → 同じエラーで `structural`。3 つの rig で、取り消しの後の references は 4 回とも同じエラーだった。
   同じプロセスの callHierarchy（T-07 depth 3）は、その後も `exact`（4.4 秒）で答えた。
- 比べ: T-07 の callHierarchy の要求を途中で取り消した後（`EXACT_BUDGET_S` を 0.3 秒にして 1 回聞く → `$/cancelRequest`）は、予算を
  戻した次の問い合わせが `exact`（1.0 秒）。T-04 の hover / definition を 1 ms で切った後も、次の問い合わせは `exact` になった。
- このエラーの応答は `lsp_status=ready` のまま（降格の理由は `lsp_error` に見える）。連続時間切れの数（3 回で `unavailable`）は
  時間切れのときしか増えないので、プロセスは止まらず、そのまま使われ続ける。

## 再現手順

1. F-PY の rig を開き、`warm_up(rig, ["httpx/_urls.py"])`。
2. `lucianke.lsp.features.LSP_TIMEOUT_REFERENCES_MS = 1` にして、上の T-06 を 1 回聞く。
3. `LSP_TIMEOUT_REFERENCES_MS = 3000` に戻して、同じ T-06 を聞く → `structural`（上のエラー）。何度聞いても同じ。

## 困ること

- 負荷の高いときに T-06 の LSP の要求が 1 度でも 3 秒を超えると、その LSP のプロセスが続く間（セッションの間ずっと）、T-06 は
  `exact` で答えられない。呼ぶ側の AI は参照の型の区別を失い、警告は「LSP が利用できない」と言うのに、`configure_workspace status` は `ready`。
- 契約テスト（BUG-0080 の直し）は、このため T-06 だけ最初から延ばした期限で聞いている（`_rig.ask_exact(..., product_first=False)`）。

## 原因・直し方・確認（2026-10-31）

### 原因（basedpyright = pyright の不具合。こちらの `$/cancelRequest` の形は LSP 仕様どおり）

こちらが送るのは `{"jsonrpc":"2.0","method":"$/cancelRequest","params":{"id":N}}`（LSP 仕様の `CancelParams` そのまま）。壊れるのは
basedpyright 1.40.2（pyright 1.1.414 由来）の側で、同梱の `dist/pyright-langserver.js` を読んで確かめた。

1. `onReferences`（117182 行）は最初に `this._pendingFindAllRefsCancellationSource.cancel()` を呼び、`getProgressReporter` で
   `CancelAfter(provider, token, …)` の source を作って同じ欄に入れる。`finally` では `source.dispose()` するだけで、欄は戻さない。
2. `CancelAfter` → `setupCombinedTokensFor`（105181 行）は、要求の token が **すでに取り消されている** と、`source.token` に触れる前に
   `source.cancel()` を呼ぶ。vscode-jsonrpc 9.0.1 の `CancellationTokenSource.cancel()`（20462 行）は token が未生成だと `_token` に凍結された
   `CancellationToken.Cancelled`（`cancel` メソッドが無い）を入れる。`dispose()` はそれに何もしない。
3. 次の references は 1. の `cancel()` で `this._token.cancel()` を呼んで TypeError。欄を `undefined` に戻す前に落ちるので、そのプロセスの
   references は以後すべて `this._token.cancel is not a function`（-32603）。callHierarchy・hover・definition はこの欄を使わないので無事
   （票の比べと一致）。同じ作りは `onImplementation`・長い `onExecuteCommand` にもある。
4. 要求の token が `CancelAfter` より前に取り消されるのは、取り消しが **処理の始まる前** に届いたとき: 要求がキューで待っている間
   （vscode-jsonrpc の `knownCanceledRequests` → 処理の直前に `cancellationSource.cancel()`、20965 行）か、`getProgressReporter` が
   `window/workDoneProgress/create` の往復を待っている間。負荷で 3 秒の期限が切れるときは要求がまだキューにいるのでこれに当たる。
5. 前の要求の処理中に次の references が届くと、次の要求が欄を健全な source で上書きするので壊れない。壊れるのは前の要求が
   終わってから次が届いたとき（期限切れで降格した後の次の問い合わせはふつうこれ）。

再現（`LspPool` + 実の basedpyright、`tests/fixtures/poly`）: references を期限 0 ms で打ち切る（`$/cancelRequest` が要求の直後に続く）→
0.5 秒待つ → references を 3 回: 3 回とも `this._token.cancel is not a function`、hover は答える。取り消しを送らずに打ち切ると 3 回とも
答える。要求が終わった後に取り消すと壊れない。

### 直し方（製品）

1. **防ぐ**: `LspServerDef.no_cancel_methods`（サーバーごとの能力）を足し、basedpyright と pyright に `textDocument/references`・
   `textDocument/implementation`・`workspace/executeCommand` を入れた。`LspClient.request` は期限切れ・タスクの取り消しのとき、これらの
   method には `$/cancelRequest` を送らない（待つのをやめるだけ。遅れて届いた応答は捨てる。LSP 仕様の上で取り消しは任意）。サーバーは
   次の同じ種類の要求で、自身の健全な source で前の要求を止める。callHierarchy・hover・definition の取り消しは今までどおり送る。
2. **それでも壊れたら回復する**: `LspServerDef.restart_on_errors`（basedpyright / pyright は `this._token.cancel is not a function`）。
   `features._call` がエラー応答にこの印を見つけたら `LspHandle.record_broken` を呼ぶ。ハンドルは **その場で** `starting`
   （`reason: restarting`）になり、`LspPool._restart_broken` が `_stop` → `_begin` で起動し直す（クラッシュと同じ `restarts` を数える）。
   起動から 60 秒未満で 3 回続いたら `unavailable`（`broken_3x`）。要求の経路は起動を待たない（§8.3）ので、その呼び出しは降格し、
   再起動の完了後の呼び出しが `exact`。
3. **状態と警告を事実どおりに**: `engine.selector.on_lsp_failure`
   - `lsp_error`（LSP は動いたままエラーを返した）: `lsp_status=ready` のまま、警告は「LSP（basedpyright）が textDocument/references の
     要求にエラーを返した（<エラー文>）ため構文木で解決しました。…」。以前は `lsp_unavailable` の雛形で「LSP が利用できない」と書いていた。
   - `restarting`（新しい理由）: `lsp_status=indexing`、警告は「LSP（basedpyright）が textDocument/references に以後答えられない状態に
     なった（<エラー文>）ため、LSP を再起動しています。今回は構文木で解決しました。…再起動の完了後に呼び直すと `exact` になります。」。
     `configure_workspace status` の `lsp_daemons[]`・T-08 の `lsp_sources[]` も `pool.status` から `indexing` / `restarting`。
   - 起動し直しの上限なら `unavailable`（`broken_3x`）で、その要求の理由も `unavailable`（再起動していないのに「再起動しています」と言わない）。
4. 試験で見つけて直した: `LspPool._stop` が重なる（`record_broken` / `record_timeout` の停止と `shutdown_all`）と、2 つ目の `_stop` が
   監督タスクを取り消し直し、監督タスクの `finally` が待つ `client.shutdown` の途中に CancelledError が入って `ProcessRegistry.terminate`
   に届かず、子プロセスが残った。すでに取り消し中のタスク（`task.cancelling()`）は取り消し直さず待つだけにした（`record_timeout` と
   `shutdown_all` の重なりにも効く）。
5. 契約テストの回避（`test_fx_read_T00_T08.py` の T-06 の `product_first=False`）を外した。T-06 も製品の期限から聞く。

仕様書 §8.4（警告の書き方）・§12.3（タイムアウト・再起動）、設計書 §7.5.2（`record_broken`・`broken_3x`・`_stop` の重なり）・§7.5.3
（取り消しを送らない method）・§7.8.4（`lsp_error` / `restarting` の警告）を更新した。

### 確認

- `tests/unit/test_lsp_features.py`（実機 basedpyright）:
  - `test_real_timed_out_references_do_not_break_later_references`: 期限 0 ms で打ち切り、サーバーがその要求を終えるのを待ってから聞き直す
    → 答える・起動し直さない。直す前の定義（`no_cancel_methods` を空）では票と同じ `this._token.cancel is not a function` で落ちることを確かめた。
  - `test_real_broken_references_are_recognised_and_basedpyright_is_restarted`: 早い `$/cancelRequest` をわざと送って実際に壊し、
    印を見て `restarting`・`status=indexing/restarting`・起動し直し（pid が変わる）・再起動後に references が答える。basedpyright が
    直ってもう壊れなければ、事実どおりの理由で skip する。
- 偽サーバー: `no_cancel_methods` の要求には取り消しを送らず、他の method には送る／印のあるエラーで起動し直し、その場で `ready` と
  言わない／印の無いエラーは `lsp_error` のまま／壊れ続けると 3 回で `unavailable`（`broken_3x`）。
- `tests/contract/test_T06_T07_lsp.py`: 実機で T-06 を期限 0 ms で 1 回降格させた後の T-06 が `exact`（basedpyright）／偽サーバーで
  壊れたとき T-06 の答えが `lsp_status=indexing` と「再起動しています」の警告（「利用できない」と言わない）、再起動後は `exact`。
- `tests/unit/test_engine_selector_failure.py`・`test_lsp_exact_degrade.py`: `lsp_error` / `restarting` の `lsp_status` と警告文。
