# BUG-0007 DiagnosticsHub が DB への書き込みより先に待ち手を起こす（設計書 §7.7.6-4 と逆の順）

- 報告: 2026-10-29 14:18 / 見た人: 編集トランザクションの担当（9c）
- 見たこと:
  - `src/lucianke/lsp/diagnostics.py` の `DiagnosticsHub.on_publish` は、待ち手（`wait_for`）を起こした後で `on_diagnostics`（DB への書き込み）を非同期に走らせる。
  - 設計書 §7.7.6-4 は「書き込んだ後に通知する」順を求めている。
  - この順だと EP-7（編集の応答の診断差分と、直後の `get_diagnostics` の結果が一致する）が競合しうる。
  - 9c は transaction 側で `sleep(0)` を 2 回挟み、IndexWriter に空のジョブ（barrier）を投げて待つ回避を入れている（`src/lucianke/edit/transaction.py`）。
- 再現手順: 未作成（直す役の仕事）。
- 調査はしていない（直す役の仕事）。

## 原因
- `DiagnosticsHub.on_publish` は、受信した診断をメモリに入れて待ち手（`wait_for` の `Future`）を起こした**あとで** `on_diagnostics` を呼び、返り値が awaitable なら `asyncio.ensure_future` で走らせるだけで、その完了を誰も待たなかった。待ち手が起きる時点では、index への書き込みジョブはまだキューにある（書き手スレッドが走っていない）ことがあり、直後に `diagnostics` 表を読むと受信前の行が見えた。設計書 §7.7.6-4（書き込みが済んでから通知する）と逆の順。
- `_satisfied` も受信の有無・版しか見なかったので、受信**のあとに** `wait_for` を始めた待ち手は、書き込み中でも即座に「揃った」と返った。
- 呼び手が `IndexWriter.submit` の `concurrent.futures.Future`（`__await__` を持たない）を返しても `inspect.isawaitable` が偽で、ハブは書き込みを見張れなかった。
- 書き込みが失敗しても（例外は握りつぶしてログだけ）待ち手は「揃った」と返るので、DB に載っていない診断で差分を取れた。
- `edit/transaction.py` の回避（`sleep(0)` 2 回 + `IndexWriter` への空ジョブ）は、「ジョブがキューに載ったはず」という時間待ちの推測で、ハブが IndexWriter の外で何をしているかを知らないと成り立たない。

## 直し方
- `lsp/diagnostics.py`: `on_diagnostics` の返り値が awaitable か `concurrent.futures.Future` なら、ファイルごとに「書き込み中」の件数（`_pending_writes`）を数え、完了で減らして待ち手を起こす。`_satisfied` と `settle_ms` の静止判定は、書き込み中・書き込み失敗のファイルを「揃った」としない（`_written`）。待ち手を起こす `on_publish` 自体は合図として残し、判定は `_satisfied` に一本化した（起きた待ち手は書き込み中ならまた待つ）。
  - 失敗の扱い: 同期の例外・非同期の例外・取り消しは、そのファイルを「書き込み失敗」とし、次の受信（の書き込み）が通るまで揃ったとしない（DB に載っていない診断で差分を取らない。`wait_for` はタイムアウトで `False` = 応答は pending）。古い受信の失敗は新しい受信が上書きするので、最新の受信の失敗だけを印にする（`seq` で照合）。受信したメモリ上の診断（`get`）は残す。
  - 呼び手の約束: 非同期に書くなら完了を表すもの（コルーチン・`IndexWriter.submit` の `Future`）を**返す**こと。返り値が `None` などなら同期で済んだ扱い。`lsp/pool.py` の docstring（`OnDiagnostics`）にも書いた。
- `edit/transaction.py`: `_barrier()` とその呼び出しを削除（`wait_for` が `True` なら書き込み済み）。`HubLike` の docstring にその約束を書いた。
- テスト:
  - `tests/unit/test_lsp_pool.py` に 6 件（ゲート付きの偽の書き込み手で、起こされた時点で書き込みが終わっていること。受信の前から待つ手と後から待つ手の両方、重なる書き込み 2 件、`IndexWriter.submit` の `Future` をコミット済みまで待つこと〈別接続で `edit_seq` を読んで確かめる〉、`settle_ms` の静止だけでは足りないこと、書き込み失敗〈同期・非同期〉が揃った扱いにならず次の受信で回復すること）。固定の `sleep` は使わず、イベントループを回して「進めるだけ進んだ」状態で未完了を確かめる。
  - `tests/unit/test_edit_transaction.py` に 1 件（`test_ep7_delta_waits_for_the_diagnostics_write_without_a_barrier`）: 本物の `DiagnosticsHub` と `IndexWriter` のジョブ（ゲート付き）をトランザクションにつなぎ、バリア無しで診断差分が書き込み後の内容になること。`_written` を常に真にするとこのテストが落ちることを確かめた。

## 確認
- 直す前: 追加した 6 件はすべて red（旧実装では受信の瞬間に待ち手が起きる）。
- 直した後: `tests/unit/test_lsp_pool.py` は既存 38 件 + 追加 6 件が green（3 回続けて）、`tests/unit/test_import_graph.py` も green。
- バリアを外した `tests/unit/test_edit_transaction.py`（既存 40 件 + 追加 1 件）を 10 回続けて green（EP-7 を含む）。
- 注意: 本番では `LspPool(on_diagnostics=...)` を結ぶ配線がまだ無い（`src` に呼び手が無い）。結ぶ側は、`IndexWriter.submit` の `Future`（または `asubmit` のコルーチン）を `on_diagnostics` の返り値にすること。捨てると、トランザクションの EP-7 はバリア無しでは保証されない。
