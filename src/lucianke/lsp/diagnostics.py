"""診断の受信と版待ち（設計書 §7.5.5、仕様書 §12.3、S-04 の実測）。

``DiagnosticsHub`` は **LSP サーバー 1 つ分** の ``textDocument/publishDiagnostics`` を受け、ファイル（``RelPath``）ごとに
最新の ``(version | None, received_at, diagnostics)`` を持つ。

* index への書き込みはここではしない。呼び手が渡す ``on_diagnostics(path, diags, version)`` を呼ぶだけ
  （``(file_id, source)`` 単位の全置換は呼び手＝index 側の仕事。``source`` はサーバー名で、呼び手が閉じ込める）。
* **書き込みが済んでから「揃った」とする**（設計書 §7.7.6-4、EP-7: 編集の応答の診断差分と直後の ``get_diagnostics`` を一致させる）。
  ``on_diagnostics`` が awaitable か ``concurrent.futures.Future``（``IndexWriter.submit``）を返したら、その完了までは
  そのファイルについて ``wait_for`` を返さない。書き込みが失敗したら、次の受信（の書き込み）が通るまで「揃った」としない
  （DB に載っていない診断で差分を取らない。タイムアウトで ``False`` = pending）。返り値が ``None`` などなら同期で済んだ扱い。
* ``wait_for`` は編集ツールの診断待ち（``DiagnosticsWaiter``）の土台。**版**を付けるサーバー（basedpyright・ruff）は
  ``version >= min_version`` で（``==`` ではない: basedpyright はバーストを最後の版 1 件に畳む）、版を付けないサーバーは
  ``didChange`` を送った時刻 ``sent_at`` 以降に受信した最初の診断で「揃った」とする。
* 版を付けないサーバー（vtsls など）は古い診断を後から返すことがあり、診断の無いファイルでは何も届けない。``sent_at`` 以降の
  **受信回数**（``min_publishes``。既定 1）と ``settle_ms`` で追い越しを抑え、届かなければタイムアウト（``False`` = pending）。
* 複数文書を続けて ``didOpen`` すると、最初の ``publishDiagnostics`` に **空の診断**が混じり、本物は少し後に届く（S-04）。
  そのファイルの **最初の受信が空**のときは、``$/progress end``（``settle_all``）か ``PROVISIONAL_QUIET_MS`` の静止までは
  「揃った」と見なさない（空を「解消」と誤読しない）。
* 時刻は ``time.perf_counter()``（Windows の ``time.monotonic()`` は粗い）。``DocumentSync`` が記録する送信時刻と同じ時計。
* 診断の中身は LSP の ``Diagnostic`` の dict のまま持つ（位置の変換は ``client.from_lsp_range`` の境界で呼び手がする）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lucianke.core.model import RelPath
from lucianke.workspace.paths import from_file_uri
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.lsp.diagnostics")

PUBLISH_DIAGNOSTICS = "textDocument/publishDiagnostics"

PROVISIONAL_QUIET_MS = 300
"""最初の受信が空の診断のとき、それを信用するまでの静止時間（S-04: 複数文書の ``didOpen`` 直後の空の診断）。"""

_RECENT_RECEIPTS = 16

DiagnosticsCallback = Callable[[RelPath, list[dict[str, Any]], int | None], Any]
"""``on_diagnostics(path, diags, version)``: 受信のたびに呼ぶ。

返り値で「書き込みが済んだ時点」をハブに伝える: ``None`` など（同期で済んだ）、awaitable（コルーチン）、
``concurrent.futures.Future``（``IndexWriter.submit`` の戻り）。後ろ 2 つは、完了するまで ``wait_for`` を返さない。
書き込みを fire-and-forget にする（Future を捨てる）と、この順序の保証が外れる。
"""


@dataclass(frozen=True, slots=True)
class DiagnosticsEntry:
    """あるファイルについて最後に受信した診断。"""

    version: int | None
    received_at: float
    diagnostics: tuple[dict[str, Any], ...]
    seq: int = 1
    """このファイルについてハブが受信した通数（1 始まり）。``seq == 1`` で空なら、本物の前の仮の診断かもしれない。"""


class DiagnosticsHub:
    """1 サーバー分の診断の受け皿。``attach(client)`` で ``publishDiagnostics`` の通知に結ぶ。"""

    def __init__(
        self,
        ws: Workspace,
        *,
        source: str = "lsp",
        on_diagnostics: DiagnosticsCallback | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.ws = ws
        self.source = source
        self._on_diagnostics = on_diagnostics
        self._clock = clock
        self._entries: dict[RelPath, DiagnosticsEntry] = {}
        self._receipts: dict[RelPath, deque[float]] = {}
        self._seq: dict[RelPath, int] = {}
        self._settled_at = float("-inf")
        self._waiters: dict[RelPath, set[asyncio.Future[None]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending_writes: dict[RelPath, int] = {}
        """``on_diagnostics`` が返した書き込みのうち、まだ終わっていない件数（ファイルごと）。"""
        self._write_failed: set[RelPath] = set()
        """最後の受信の書き込みが失敗したファイル（次の受信で解く）。"""
        self._closed = False
        self._gen = 0
        self.publish_count = 0
        """受信した ``publishDiagnostics`` の通数（ルート外の URI も含む）。"""
        self.last_received_at: float | None = None
        self.analysed = False
        """起動（``reset`` の後）から、どれかの文書の ``publishDiagnostics`` を 1 通でも受けたか（ルート外の URI も含む）。
        basedpyright は解析の周期の終わりに診断を出すので、``True`` は「LSP がワークスペースのファイルの一覧を持って解析を
        1 周した」合図（``ready`` は猶予時間でも立つので、それだけでは解析の前かもしれない。BUG-0012・BUG-0058）。"""

    # --- 受信 -----------------------------------------------------------------------

    def attach(self, client: Any) -> None:
        """``client``（``LspClient``）の ``publishDiagnostics`` に ``on_publish`` を登録する。"""
        client.on_notification(PUBLISH_DIAGNOSTICS, self.on_publish)

    def on_publish(self, params: Any) -> None:
        """``publishDiagnostics`` の ``params`` を取り込む（同期。受信順を保つため通知ハンドラの中でその場で呼ぶ）。"""
        if not isinstance(params, dict):
            return
        received_at = self._clock()
        self.publish_count += 1
        self.last_received_at = received_at
        self.analysed = True
        uri = params.get("uri")
        path = from_file_uri(self.ws, uri) if isinstance(uri, str) else None
        if path is None:
            _log.debug("[%s] publishDiagnostics for a URI outside the workspace: %r", self.source, uri)
            return
        raw_version = params.get("version")
        version = raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) else None
        raw = params.get("diagnostics")
        diags = [d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else []
        seq = self._seq.get(path, 0) + 1
        self._seq[path] = seq
        self._entries[path] = DiagnosticsEntry(version, received_at, tuple(diags), seq)
        self._receipts.setdefault(path, deque(maxlen=_RECENT_RECEIPTS)).append(received_at)
        self._write_failed.discard(path)
        if self._on_diagnostics is not None:
            # 書き込みを先に始める。待ち手を起こす（下）のは「受信があった」という合図にすぎず、揃ったかどうかは
            # ``_satisfied`` が書き込みの済み具合も見て決める（書き込み中は、起きた待ち手がまた待つ）。
            try:
                result = self._on_diagnostics(path, list(diags), version)
            except Exception:
                _log.exception("[%s] on_diagnostics failed for %s", self.source, path)
                self._write_failed.add(path)
            else:
                if isinstance(result, concurrent.futures.Future) or inspect.isawaitable(result):
                    self._spawn(result, path, seq)
        self._wake(path)

    def _wake(self, path: RelPath) -> None:
        for fut in self._waiters.pop(path, ()):
            if not fut.done():
                fut.set_result(None)

    def _spawn(self, write: Awaitable[Any] | concurrent.futures.Future[Any], path: RelPath, seq: int) -> None:
        """``on_diagnostics`` が返した書き込みを見張る。終わるまで ``path`` を「書き込み中」にする。"""

        async def run() -> None:
            ok = False
            try:
                if isinstance(write, concurrent.futures.Future):
                    await asyncio.wrap_future(write)
                else:
                    await write
                ok = True
            except Exception:
                _log.exception("[%s] async on_diagnostics failed for %s", self.source, path)
            finally:  # 取り消されたときも「書き込み中」を残さない（そのときは失敗扱い）
                self._write_finished(path, seq, ok)

        self._pending_writes[path] = self._pending_writes.get(path, 0) + 1
        coro = run()
        try:
            task = asyncio.ensure_future(coro)
        except Exception:  # 走っているイベントループが無い
            coro.close()
            if inspect.iscoroutine(write):
                write.close()
            _log.exception("[%s] could not schedule the on_diagnostics write for %s", self.source, path)
            self._write_finished(path, seq, False)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _write_finished(self, path: RelPath, seq: int, ok: bool) -> None:
        left = self._pending_writes.get(path, 0) - 1
        if left > 0:
            self._pending_writes[path] = left
        else:
            self._pending_writes.pop(path, None)
        if not ok and self._seq.get(path) == seq:  # 古い受信の失敗は、新しい受信の書き込みが上書きする
            self._write_failed.add(path)
        self._wake(path)

    def _written(self, path: RelPath) -> bool:
        """``path`` の最後の受信が index に書き込み済み（書き込み中でも、失敗でもない）。"""
        return path not in self._pending_writes and path not in self._write_failed

    # --- 参照 -----------------------------------------------------------------------

    def get(self, path: RelPath) -> DiagnosticsEntry | None:
        """最後に受信した診断。まだ受信していなければ ``None``。"""
        return self._entries.get(path)

    def paths(self) -> list[RelPath]:
        return list(self._entries)

    def forget(self, path: RelPath) -> None:
        """``path`` の保持を捨てる（``didClose`` のあと）。"""
        self._entries.pop(path, None)
        self._receipts.pop(path, None)
        self._seq.pop(path, None)
        self._write_failed.discard(path)

    def clear(self) -> None:
        self._entries.clear()
        self._receipts.clear()
        self._seq.clear()
        self._write_failed.clear()

    def settle_all(self) -> None:
        """いま保持している診断を「確定」とする（``$/progress end``）。空の最初の診断の仮置きを解く。"""
        self._settled_at = self._clock()
        for futs in list(self._waiters.values()):
            for fut in futs:
                if not fut.done():
                    fut.set_result(None)

    def is_provisional(self, path: RelPath) -> bool:
        """最初の受信が空で、まだ確定していない（``settle_all`` も静止時間も済んでいない）。"""
        entry = self._entries.get(path)
        if entry is None or entry.seq != 1 or entry.diagnostics:
            return False
        if entry.received_at <= self._settled_at:
            return False
        return self._clock() - entry.received_at < PROVISIONAL_QUIET_MS / 1000

    def publishes_since(self, path: RelPath, sent_at: float) -> int:
        """``sent_at`` 以降に受信した通数（直近 16 通まで）。"""
        return sum(1 for t in self._receipts.get(path, ()) if t >= sent_at)

    def _wake_all(self) -> None:
        waiters, self._waiters = self._waiters, {}
        for futs in waiters.values():
            for fut in futs:
                if not fut.done():
                    fut.set_result(None)

    def close(self) -> None:
        """終わり。待っている ``wait_for`` を起こして ``False`` で返し、以後の ``wait_for`` も ``False``。"""
        self._closed = True
        self._wake_all()

    def reset(self) -> None:
        """サーバーの再起動・停止のとき: 保持を捨て、待っている ``wait_for`` を ``False`` で返す（タイムアウトまで待たせない）。

        ``close`` と違い、以後の ``wait_for`` は使える。
        """
        self._gen += 1
        self.clear()
        self._settled_at = float("-inf")
        self.analysed = False
        self._wake_all()

    # --- 版待ち ---------------------------------------------------------------------

    def _satisfied(self, path: RelPath, min_version: int | None, sent_at: float, min_publishes: int) -> bool:
        entry = self._entries.get(path)
        if entry is None or self.is_provisional(path) or not self._written(path):
            return False
        if min_version is not None and entry.version is not None:
            return entry.version >= min_version
        return entry.received_at >= sent_at and self.publishes_since(path, sent_at) >= min_publishes

    async def wait_for(
        self,
        path: RelPath,
        *,
        min_version: int | None,
        sent_at: float,
        timeout_ms: int | float,
        settle_ms: int | float = 0,
        min_publishes: int = 1,
    ) -> bool:
        """``path`` の診断が「編集後のもの」になるまで待つ。揃えば ``True``、``timeout_ms`` を超えるか ``close`` なら ``False``。

        * 受信した診断に ``version`` があり ``min_version`` が与えられていれば ``version >= min_version``。
        * ``version`` が無ければ（または ``min_version`` が ``None``）、``sent_at``（``DocumentSync.sent_at_of``）以降に
          ``min_publishes`` 通以上の受信。
        * ``settle_ms`` を指定すると、揃ったあとさらにそのファイルの診断が ``settle_ms`` 静かになるまで待つ
          （S-04: ``didChangeWatchedFiles`` 後の依存元・複数文書の ``didOpen`` 直後は、最初の診断のあとに本物が続く）。
        * 受信した診断の ``on_diagnostics``（index への書き込み）が終わっていること。``True`` で返ったときは書き込み済みなので、
          直後に index を読めば同じ診断が見える（EP-7）。書き込みが終わらない・失敗したときは ``False``。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_ms, 0) / 1000
        settle_s = max(settle_ms, 0) / 1000
        gen = self._gen

        def aborted() -> bool:
            return self._closed or self._gen != gen

        async def wait_publish(limit_s: float) -> bool:
            """次の受信まで最大 ``limit_s`` 秒待つ。受信（または close）で ``True``、時間切れで ``False``。"""
            if aborted() or limit_s <= 0:
                return False
            fut: asyncio.Future[None] = loop.create_future()
            self._waiters.setdefault(path, set()).add(fut)
            try:
                await asyncio.wait_for(fut, timeout=limit_s)
            except TimeoutError:
                return False
            finally:
                futs = self._waiters.get(path)
                if futs is not None:
                    futs.discard(fut)
                    if not futs:
                        self._waiters.pop(path, None)
            return not aborted()

        while not self._satisfied(path, min_version, sent_at, min_publishes):
            entry = self._entries.get(path)
            limit = deadline - loop.time()
            if entry is not None and self.is_provisional(path):
                # 仮の空の診断: 静止時間が過ぎれば信用する（それまでに本物が届けば、その受信で起きる）。
                limit = min(limit, PROVISIONAL_QUIET_MS / 1000 - (self._clock() - entry.received_at) + 0.001)
            if not await wait_publish(limit):
                if aborted():
                    return False
                if loop.time() >= deadline and not self._satisfied(path, min_version, sent_at, min_publishes):
                    return False
        if settle_s <= 0:
            return True
        while True:
            entry = self._entries.get(path)
            if entry is None:  # reset / forget の最中
                return False
            quiet = self._clock() - entry.received_at
            written = self._written(path)
            if written and quiet >= settle_s:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            # 書き込み中は静止時間では足りない: 書き込みの終わり（または次の受信）で起きる
            await wait_publish(min(settle_s - quiet, remaining) if written else remaining)
            if aborted():
                return False
