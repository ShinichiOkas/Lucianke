"""文書同期（設計書 §7.5.4、仕様書 §12.3）。

``DocumentSync`` は **LSP サーバー 1 つ分** の「開いている文書」の台帳。``didOpen`` / ``didChange`` / ``didClose`` を
``notify`` 関数（``LspClient.notify``）で送り、版番号・送信時刻・LRU を持つ。

* 全文同期（``contentChanges`` は全文 1 件）。版は ``didOpen`` が 1、``didChange`` のたびに +1。
* 開いている文書は ``max_open``（``LSP_OPEN_DOCS_MAX`` = 200）件まで。超えたら **使われたのが最も古い**文書から ``didClose``
  （いま開こうとしている文書は閉じない）。要求で触れた文書は ``touch`` で新しい側へ。
* 送信時刻 ``sent_at_of`` は ``DiagnosticsHub.wait_for(sent_at=...)`` に渡す（版を付けないサーバーの診断待ち）。送信の直前に記録する。
* LSP は作業ツリーしか知らない。``revision != WORKING_TREE`` の内容を開かないのは呼び手の責任。
* 送信に失敗（``ConnectionError``）したら状態を変えずに送出する。サーバーの再起動では台帳ごと作り直す（``pool``）。

``DocumentRouter`` は **ワークスペース 1 つ分の「言語ごとに正しいハンドルへ振り分ける口」**。編集トランザクション
（``edit/transaction.py``）は ``documents`` と ``diagnostics_hub`` を 1 組しか受けないが、Python は ``types``（basedpyright）と
``lint``（ruff）の 2 サーバーが動く。ルーターは 1 組の口として、言語の全サーバーへ ``sync`` し、診断の待ちを全サーバー分
合わせる（BUG-0023）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from lucianke.core.limits import LSP_OPEN_DOCS_MAX
from lucianke.core.model import RelPath
from lucianke.lsp.diagnostics import DiagnosticsHub
from lucianke.workspace.paths import to_file_uri
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.lsp.documents")

Notify = Callable[[str, Any], None]
"""``notify(method, params)``: 通知を送る（待たない）。``LspClient.notify`` を渡す。"""


@dataclass(slots=True)
class _Doc:
    version: int
    language_id: str
    sent_at: float
    digest: bytes


def _digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).digest()


class DocumentSync:
    """開いている文書の台帳と ``didOpen`` / ``didChange`` / ``didClose``。"""

    def __init__(
        self,
        ws: Workspace,
        notify: Notify,
        *,
        max_open: int = LSP_OPEN_DOCS_MAX,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if max_open < 1:
            raise ValueError("max_open must be >= 1")
        self.ws = ws
        self._notify = notify
        self._max_open = max_open
        self._clock = clock
        self._docs: OrderedDict[RelPath, _Doc] = OrderedDict()  # 先頭が最も古い
        self.on_close: Callable[[RelPath], None] | None = None
        """文書を閉じたとき（``did_close`` も LRU の追い出しも）に呼ぶ。``DiagnosticsHub.forget`` を結ぶ。"""

    # --- 参照 -----------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._docs)

    def __contains__(self, path: object) -> bool:
        return path in self._docs

    def is_open(self, path: RelPath) -> bool:
        return path in self._docs

    def open_paths(self) -> list[RelPath]:
        """開いている文書（古い順）。"""
        return list(self._docs)

    def version_of(self, path: RelPath) -> int | None:
        """開いていれば現在の版、開いていなければ ``None``。"""
        doc = self._docs.get(path)
        return doc.version if doc is not None else None

    def sent_at_of(self, path: RelPath) -> float | None:
        """直近の ``didOpen`` / ``didChange`` を送った時刻（``time.perf_counter()``）。開いていなければ ``None``。"""
        doc = self._docs.get(path)
        return doc.sent_at if doc is not None else None

    def touch(self, path: RelPath) -> None:
        """要求で触れた文書を LRU の新しい側へ。開いていなければ何もしない。"""
        if path in self._docs:
            self._docs.move_to_end(path)

    # --- 送信 -----------------------------------------------------------------------

    def did_open(self, path: RelPath, text: str, language_id: str) -> int:
        """``didOpen`` を版 1 で送って版を返す。すでに開いていれば何も送らず、LRU を更新して現在の版を返す。

        開いたことで ``max_open`` を超えるなら、最も古い文書を ``didClose`` する。
        """
        doc = self._docs.get(path)
        if doc is not None:
            self._docs.move_to_end(path)
            return doc.version
        sent_at = self._clock()
        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": to_file_uri(self.ws, path),
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )
        self._docs[path] = _Doc(1, language_id, sent_at, _digest(text))
        self._evict(keep=path)
        return 1

    def did_change(self, path: RelPath, text: str) -> int:
        """全文の ``didChange`` を送って新しい版を返す。開いていなければ ``ValueError``。"""
        doc = self._docs.get(path)
        if doc is None:
            raise ValueError(f"document is not open: {path}")
        version = doc.version + 1
        sent_at = self._clock()
        self._notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": to_file_uri(self.ws, path), "version": version},
                "contentChanges": [{"text": text}],
            },
        )
        doc.version = version
        doc.sent_at = sent_at
        doc.digest = _digest(text)
        self._docs.move_to_end(path)
        return version

    def sync(self, path: RelPath, text: str, language_id: str) -> int:
        """LSP に ``text`` を見せる: 未オープンなら ``didOpen``、内容が変わっていれば ``didChange``、同じなら何も送らない。版を返す。"""
        doc = self._docs.get(path)
        if doc is None:
            return self.did_open(path, text, language_id)
        if doc.digest == _digest(text):
            self._docs.move_to_end(path)
            return doc.version
        return self.did_change(path, text)

    def on_external_change(self, path: RelPath, text: str) -> int | None:
        """外部の変更（監視・git）。開いている文書なら再読込した ``text`` を ``didChange`` して新しい版を返す。

        開いていない文書、または内容が同じなら ``None``（開いていない文書は ``didChangeWatchedFiles`` の仕事）。
        """
        doc = self._docs.get(path)
        if doc is None or doc.digest == _digest(text):
            return None
        return self.did_change(path, text)

    def did_close(self, path: RelPath) -> bool:
        """``didClose`` を送って台帳から外す。開いていなければ ``False``（何も送らない）。"""
        if path not in self._docs:
            return False
        self._notify("textDocument/didClose", {"textDocument": {"uri": to_file_uri(self.ws, path)}})
        del self._docs[path]
        self._closed(path)
        return True

    def close_all(self) -> int:
        """開いている文書をすべて閉じる。送信に失敗した文書の分で打ち切らず、閉じた件数を返す。"""
        closed = 0
        for path in list(self._docs):
            try:
                closed += self.did_close(path)
            except ConnectionError:
                break
        return closed

    def reset(self) -> None:
        """台帳を空にする（何も送らない）。サーバーが落ちた・止めたとき: 文書はサーバーごと消えている。"""
        self._docs.clear()

    # --- 内部 -----------------------------------------------------------------------

    def _evict(self, *, keep: RelPath) -> None:
        while len(self._docs) > self._max_open:
            victim = next(p for p in self._docs if p != keep)
            _log.debug("LRU eviction: didClose %s", victim)
            self.did_close(victim)

    def _closed(self, path: RelPath) -> None:
        if self.on_close is not None:
            try:
                self.on_close(path)
            except Exception:
                _log.exception("on_close failed for %s", path)


# --- 言語ごとの振り分け ----------------------------------------------------------------------


class RoutedHandle(Protocol):
    """``DocumentRouter`` が見る ``lsp.pool.LspHandle`` の一部（``pool`` を import できないので形だけ）。"""

    language_id: str
    role: str
    defn: Any  # LspServerDef | None（``document_language_id`` を持つ）
    documents: DocumentSync
    diagnostics: DiagnosticsHub

    @property
    def is_ready(self) -> bool: ...


class HandleSource(Protocol):
    """``lsp.pool.LspPool`` の一部。"""

    def handles(self, ws: Workspace | None = None, language_id: str | None = None) -> Sequence[Any]: ...


_RECORDS_MAX = 1024
"""版の記録を持つ文書数の上限（超えたら古い順に捨てる。捨てても待ちは時刻に落ちるだけ）。"""


class DocumentRouter:
    """ワークスペース 1 つの、言語ごとの ``DocumentSync`` / ``DiagnosticsHub`` の振り分け。

    ``EditTransaction`` が求める ``sync(path, text, language_id)``・``sent_at_of(path)``・``is_open(path)`` を同じ形で持ち、
    ``diagnostics_hub``（``wait_for`` の口）を持つ。トランザクションには ``documents=router, diagnostics_hub=router.diagnostics_hub``
    を渡す（``LspPool.router(ws)`` が同じものを返す）。

    * **``sync``**: ``language_id`` のハンドルのうち ``ready`` のすべて（``types`` / ``lint``）の ``DocumentSync`` に ``sync`` する。
      ``didOpen`` の ``languageId`` はハンドルの定義の ``document_language_id``（``c_sharp`` → ``csharp``）に直す。
      サーバーごとに版の数え方が違うので、返す版は代表（``types`` 優先）の版にすぎず、**サーバーごとの ``(版, 送信時刻)`` を記録**して
      ``wait_for`` が使う。一部のサーバーへの送信が ``ConnectionError`` でも残りには送る。動いているサーバーが無い、または全部失敗したら
      ``ConnectionError``（トランザクションは警告にして続ける）。起動はしない（``ready`` でないハンドルは対象外）。
    * **``is_open``**: どれかのハンドルが開いていれば真。**``sent_at_of``**: 開いているハンドルの送信時刻の最小（``None`` は閉じている）。
      ``min`` なのは、``wait_for(sent_at=)`` の下限を甘い側（先に送ったサーバーの時刻）にして、後から開いたサーバーの診断を取りこぼさないため。
    * **``diagnostics_hub.wait_for``**: その文書を開いている ``ready`` のサーバーすべてを **同時に** 待ち、すべて揃ったときだけ ``True``
      （1 つでも遅れたら ``False``＝pending。診断は index から全 ``source`` ぶんを読むので、1 つでも古いと差分が狂う）。待ちの時間は
      サーバー数で増えない（``timeout_ms`` を共有する）。

      - 版つきの待ち（``min_version`` あり＝編集した文書）は、**記録したサーバーごとの版と送信時刻**を使う（呼び手の ``min_version`` は
        代表の版なので、他のサーバーには使わない）。記録の無いサーバーは版なし（``sent_at`` 以降の受信）で待つ。
      - 版なしの待ち（``min_version=None``＝トランザクションが LSP に見せただけの依存元）は、``lint`` のサーバーを待たない
        （ruff は 1 ファイル内の検査で、依存元は内容が変わらないので再発行しない。待つと必ず時間切れになる）。``lint`` しか開いていなければ全部待つ。
    * 文書の版の検査（``edit/workspace_edit.py`` の ``DocumentVersions``）は **``WorkspaceEdit`` を出したサーバーの** ``DocumentSync`` に対して
      行うこと（ルーターは ``version_of`` を持たない）。T-08 など個別のハンドルの診断を読む呼び手は ``handles_for`` / ``hubs_for``。
    """

    def __init__(self, pool: HandleSource, ws: Workspace) -> None:
        self._pool = pool
        self.ws = ws
        self._synced: OrderedDict[RelPath, dict[int, tuple[int, float | None]]] = OrderedDict()
        self.diagnostics_hub = RoutedDiagnostics(self)

    # --- 対象の引き -----------------------------------------------------------------

    def handles_for(self, language_id: str) -> list[RoutedHandle]:
        """``language_id`` の ``ready`` なハンドル（``types`` が先）。"""
        found = [h for h in self._pool.handles(self.ws, language_id) if h.is_ready]
        found.sort(key=lambda h: h.role != "types")  # 安定ソート: types を先頭に
        return found

    def hubs_for(self, language_id: str) -> list[DiagnosticsHub]:
        return [h.diagnostics for h in self.handles_for(language_id)]

    def _open_handles(self, path: RelPath) -> list[RoutedHandle]:
        """``path`` を開いている ``ready`` なハンドル（全言語。``types`` が先）。"""
        found = [h for h in self._pool.handles(self.ws) if h.is_ready and h.documents.is_open(path)]
        found.sort(key=lambda h: h.role != "types")
        return found

    # --- DocumentsLike（EditTransaction が求める形） -------------------------------------

    def sync(self, path: RelPath, text: str, language_id: str) -> int:
        targets = self.handles_for(language_id)
        if not targets:
            raise ConnectionError(f"no running LSP server for language {language_id!r}")
        record: dict[int, tuple[int, float | None]] = {}
        representative: int | None = None
        first_error: ConnectionError | None = None
        for h in targets:
            lid = h.defn.document_language_id if h.defn is not None else language_id
            try:
                version = h.documents.sync(path, text, lid)
            except ConnectionError as e:  # このサーバーは落ちた: 残りには送る
                _log.debug("document sync skipped: %s/%s is disconnected", h.language_id, h.role)
                first_error = first_error or e
                continue
            record[id(h)] = (version, h.documents.sent_at_of(path))
            if representative is None:
                representative = version
        if representative is None:
            assert first_error is not None
            raise first_error
        self._synced[path] = record
        self._synced.move_to_end(path)
        while len(self._synced) > _RECORDS_MAX:
            self._synced.popitem(last=False)
        return representative

    def sent_at_of(self, path: RelPath) -> float | None:
        times = [t for h in self._open_handles(path) if (t := h.documents.sent_at_of(path)) is not None]
        return min(times) if times else None

    def is_open(self, path: RelPath) -> bool:
        return bool(self._open_handles(path))

    # --- 診断の待ち（RoutedDiagnostics が呼ぶ） -------------------------------------------

    async def _wait_for(
        self,
        path: RelPath,
        *,
        min_version: int | None,
        sent_at: float,
        timeout_ms: int | float,
        settle_ms: int | float,
        min_publishes: int,
    ) -> bool:
        handles = self._open_handles(path)
        if min_version is None:
            handles = [h for h in handles if h.role != "lint"] or handles
        if not handles:
            return False  # 文書を開いているサーバーが無い: 診断は届かない
        record = self._synced.get(path, {})
        waits = []
        for h in handles:
            rec = record.get(id(h))
            mv, at = min_version, sent_at
            if min_version is not None:
                if rec is not None:
                    current = h.documents.version_of(path)
                    # 閉じて開き直したサーバーは版が 1 に戻っている: 記録の版ではなく現在の版まで
                    mv = rec[0] if current is None else min(rec[0], current)
                    at = rec[1] if rec[1] is not None else sent_at
                else:
                    mv = None  # 代表の版はこのサーバーの版ではない: 時刻で待つ
            waits.append(
                h.diagnostics.wait_for(
                    path,
                    min_version=mv,
                    sent_at=at,
                    timeout_ms=timeout_ms,
                    settle_ms=settle_ms,
                    min_publishes=min_publishes,
                )
            )
        results = await asyncio.gather(*waits, return_exceptions=True)
        return all(r is True for r in results)


class RoutedDiagnostics:
    """``DocumentRouter`` の診断の口（``EditTransaction`` の ``HubLike``）。"""

    def __init__(self, router: DocumentRouter) -> None:
        self._router = router

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
        return await self._router._wait_for(
            path,
            min_version=min_version,
            sent_at=sent_at,
            timeout_ms=timeout_ms,
            settle_ms=settle_ms,
            min_publishes=min_publishes,
        )
