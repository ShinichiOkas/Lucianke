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
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager

from lucianke.core.model import RelPath
from lucianke.lsp import features
from lucianke.lsp.pool import LspHandle

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
