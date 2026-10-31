"""実 LSP の試験で「LSP の解析が済むまで待つ」部品（BUG-0076）。

負荷の高いとき（全体テストを何本も並べて流すなど）は、basedpyright の起動直後の解析が遅れ、``ready`` の後の最初の要求が
本番の要求の期限（``LSP_TIMEOUT_REFERENCES_MS`` = 3 秒など）を超える。期限を超えた要求を降格するのは製品として正しいので
製品の期限は変えず、試験の側で **解析が済んだ合図を待ってから** 本番の要求（ツールの呼び出し）に入る。固定の sleep は使わない。

* ``first_analysis``: 文書を開き、その文書の最初の ``publishDiagnostics`` が届くまで待つ（製品の ``await_first_diagnostic`` と
  同じ合図。BUG-0012）。上限は ``SETTLE_TIMEOUT_S``（条件待ち。届けば即戻る）。
* ``probe_timeouts``: 準備のために試験が LSP に直接聞く問い合わせ（``features.*``）にだけ、要求の期限を ``PROBE_TIMEOUT_MS`` に
  延ばす。負荷で 1 回の問い合わせが本番の期限を超えても、試験が落ちたり、連続タイムアウトの計数（3 回で ``unavailable``）で
  LSP が止まったりしないようにする。ツールの呼び出しをこの中に入れないこと（本番の期限を試験から変えない）。
* ``settle``: 述語（LSP に直接聞いて期待どおりの答えが揃ったか）が真になるまで、``probe_timeouts`` の中で繰り返す。
* ``diagnostics_published``: 編集の前提として、いまの内容の版について期待の診断が出て表に入るまで待つ（BUG-0097）。
* ``HeldPublishes``: ある LSP の、ある文書の ``publishDiagnostics`` を、ハブ（``DiagnosticsHub.on_publish``）に渡す前に試験の側で
  止める・遅らせる（負荷や起動直後で LSP の診断が遅れたときの代わり。BUG-0098・BUG-0099）。順序は保つ。製品のコードは変えない。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from lucianke.core.model import RelPath
from lucianke.lsp import features
from lucianke.lsp.diagnostics import PUBLISH_DIAGNOSTICS
from lucianke.lsp.pool import LspHandle
from lucianke.workspace.paths import from_file_uri

SETTLE_TIMEOUT_S = 240.0  # 解析の完了を待つ上限。条件待ちなので、CPU が飽和していても足りる長さにする
PROBE_TIMEOUT_MS = 120_000  # 準備の問い合わせ 1 回の期限
RETRY_INTERVAL_S = 0.25


@contextmanager
def probe_timeouts(ms: int = PROBE_TIMEOUT_MS) -> Iterator[None]:
    """``features`` の要求の期限（``LSP_TIMEOUT_*_MS``）を、この中だけ ``ms`` にする。"""
    names = [n for n in vars(features) if n.startswith("LSP_TIMEOUT_") and n.endswith("_MS")]
    saved = {n: getattr(features, n) for n in names}
    for n in names:
        setattr(features, n, ms)
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(features, n, v)


async def first_analysis(
    handle: LspHandle, rels: Iterable[str], *, timeout_s: float = SETTLE_TIMEOUT_S
) -> None:
    """``rels`` を LSP に開かせ、各文書の最初の診断（= その文書の解析の完了）が届くまで待つ。"""
    paths = [RelPath(str(r)) for r in rels]
    sent: dict[RelPath, float] = {}
    for rel in paths:
        sent_at = await features.sync_document(handle, rel)
        assert sent_at is not None, rel
        sent[rel] = sent_at
    for rel in paths:
        assert await handle.diagnostics.wait_for(
            rel, min_version=None, sent_at=sent[rel], timeout_ms=timeout_s * 1000
        ), f"the LSP did not publish the first diagnostics of {rel} within {timeout_s} s"


async def settle(
    check: Callable[[], Awaitable[bool]], *, what: str, timeout_s: float = SETTLE_TIMEOUT_S
) -> None:
    """``check()`` が ``True`` を返すまで繰り返す（``probe_timeouts`` の中。期限切れの 1 回は数えずに次へ）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last: BaseException | None = None
    with probe_timeouts():
        while loop.time() < deadline:
            try:
                if await check():
                    return
            except TimeoutError as exc:
                last = exc
            await asyncio.sleep(RETRY_INTERVAL_S)
    raise AssertionError(f"the LSP did not settle within {timeout_s} s: {what} (last error: {last!r})")


async def diagnostics_published(
    handle: LspHandle, rel: str, codes: Iterable[str], *, timeout_s: float = SETTLE_TIMEOUT_S
) -> None:
    """``rel`` を LSP に見せ（いまのディスクの内容）、その版について ``codes`` の診断を出し、表に書き終えるまで待つ（BUG-0097）。

    編集ツール（T-13 など）の前提を試験が整える部品。編集前の診断の写しは ``diagnostics`` 表から取り（設計書 §7.7.6-1）、
    QuickFix の文脈の診断もハブから取る。ツールの中の短い待ち（``CONTEXT_DIAGNOSTICS_WAIT_MS`` = 2 秒）に頼ると、負荷で最初の
    解析がそれを超えたとき、写しや文脈に診断が無く答えが変わる。合図は製品と同じ ``DiagnosticsHub.wait_for``（版 ≥ いまの版、
    かつ ``on_diagnostics`` の書き込み済み）と受信した中身。中身が足りない受信（解析の途中の発行など）なら次の受信を待つ。
    """
    want = set(codes)
    path = RelPath(str(rel))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    sent_at = await features.sync_document(handle, path)
    assert sent_at is not None, rel
    min_version = handle.documents.version_of(path)
    seen: list[list[str]] = []
    while await handle.diagnostics.wait_for(
        path, min_version=min_version, sent_at=sent_at, timeout_ms=max(deadline - loop.time(), 0) * 1000
    ):
        entry = handle.diagnostics.get(path)
        assert entry is not None, rel
        got = {str(d.get("code")) for d in entry.diagnostics}
        if want <= got:
            return
        seen.append(sorted(got))
        min_version, sent_at = None, entry.received_at + 1e-6  # 次の受信（いまの受信より後）を待つ
    raise AssertionError(
        f"{handle.server_name} did not publish {sorted(want)} for {rel} within {timeout_s} s (received: {seen})"
    )


class HeldPublishes:
    """``handle`` の ``rel`` の ``publishDiagnostics`` を、ハブに渡す前に止める・遅らせる（BUG-0098・BUG-0099）。

    サーバーが ``ready`` になってから（``handle.client`` があるとき）作る。``with`` を抜けると元に戻す（止めている分は渡す）。
    受けた順に渡す（追い越さない）。``rel`` 以外の文書の発行は素通し。

    * 既定: 止めたまま。``release()`` で止めていた分を順に渡し、以後は素通し。
    * ``delay_s``: 受けてから ``delay_s`` 秒後に渡す（負荷で解析が遅れたとき）。
    * ``until_version``: その版以上の発行が届いたら、止めていた分とその発行を順に渡し、以後は素通し（編集の前の発行だけを遅らせる）。

    ``received`` に ``(受けた時刻, 版, 件数)`` を残す（赤の報告に使う）。
    """

    def __init__(
        self, handle: LspHandle, rel: str, *, delay_s: float | None = None, until_version: int | None = None
    ) -> None:
        client = handle.client
        assert client is not None, "start the server (wait_ready) before holding its publishes"
        self._handle = handle
        self._rel = RelPath(rel)
        self._delay_s = delay_s
        self._until_version = until_version
        self._handlers = client._notification_handlers[PUBLISH_DIAGNOSTICS]  # pyright: ignore[reportPrivateUsage]
        self._target = handle.diagnostics.on_publish
        self._queue: deque[Any] = deque()
        self._passing = False
        self.received: list[tuple[float, int | None, int]] = []
        self._index = self._handlers.index(self._target)
        self._handlers[self._index] = self._on_publish

    def __enter__(self) -> HeldPublishes:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
        if self._index < len(self._handlers) and self._handlers[self._index] == self._on_publish:
            self._handlers[self._index] = self._target

    def _ours(self, params: Any) -> bool:
        uri = params.get("uri") if isinstance(params, dict) else None
        return isinstance(uri, str) and from_file_uri(self._handle.ws, uri) == self._rel

    def _on_publish(self, params: Any) -> None:
        if self._passing or not self._ours(params):
            self._target(params)
            return
        version = params.get("version")
        diags = params.get("diagnostics")
        self.received.append((time.perf_counter(), version, len(diags) if isinstance(diags, list) else 0))
        self._queue.append(params)
        if self._delay_s is not None:
            asyncio.get_running_loop().call_later(self._delay_s, self._deliver_one)
        elif self._until_version is not None and isinstance(version, int) and version >= self._until_version:
            self.release()

    def _deliver_one(self) -> None:
        if self._queue:
            self._target(self._queue.popleft())

    def release(self) -> None:
        """止めていた分を受けた順に渡し、以後は素通し。"""
        self._passing = True
        while self._queue:
            self._target(self._queue.popleft())
