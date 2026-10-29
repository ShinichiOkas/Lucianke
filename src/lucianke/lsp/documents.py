"""文書同期（設計書 §7.5.4、仕様書 §12.3）。

``DocumentSync`` は **LSP サーバー 1 つ分** の「開いている文書」の台帳。``didOpen`` / ``didChange`` / ``didClose`` を
``notify`` 関数（``LspClient.notify``）で送り、版番号・送信時刻・LRU を持つ。

* 全文同期（``contentChanges`` は全文 1 件）。版は ``didOpen`` が 1、``didChange`` のたびに +1。
* 開いている文書は ``max_open``（``LSP_OPEN_DOCS_MAX`` = 200）件まで。超えたら **使われたのが最も古い**文書から ``didClose``
  （いま開こうとしている文書は閉じない）。要求で触れた文書は ``touch`` で新しい側へ。
* 送信時刻 ``sent_at_of`` は ``DiagnosticsHub.wait_for(sent_at=...)`` に渡す（版を付けないサーバーの診断待ち）。送信の直前に記録する。
* LSP は作業ツリーしか知らない。``revision != WORKING_TREE`` の内容を開かないのは呼び手の責任。
* 送信に失敗（``ConnectionError``）したら状態を変えずに送出する。サーバーの再起動では台帳ごと作り直す（``pool``）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lucianke.core.limits import LSP_OPEN_DOCS_MAX
from lucianke.core.model import RelPath
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
