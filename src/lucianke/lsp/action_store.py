"""``code_action_id`` の台帳（仕様書 §11 T-08 手順 5・T-13 手順 2、設計書 §2 の「セッション限定の軽い対応表」）。

T-08 が QuickFix を問い合わせるたびに ``put`` で不透明な ID（``qf_0001`` …）を発行し、T-13 が ``get`` で引く。

* 台帳はプロセスの中だけ（再起動で消える。ID は失効して再取得できる。ADR-16）。上限 ``max_entries``（既定 2000）を超えたら
  使われていない古いものから捨てる。
* 同じ ``(path, sha256, server, range, kind, title)`` の問い合わせは同じ ID を返す（呼び直しで台帳を膨らませない）。
* 失効の判定は呼び手（T-13）が **ファイルの現在の sha256** と ``StoredAction.sha256`` を比べて行う。台帳は古い sha256 の
  エントリを消さない（消すと T-13 が ``E_PRECONDITION_FAILED``（``code_action_id_expired``）ではなく ``E_TARGET_NOT_FOUND`` を
  返してしまう）。``is_current`` はその比較のための補助。
* ``action`` は LSP の ``CodeAction``（dict）の原本。``edit`` が無ければ T-13 が ``codeAction/resolve`` で付ける。
"""

from __future__ import annotations

import itertools
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lucianke.core.model import RelPath

__all__ = ["CodeActionStore", "StoredAction"]

ID_PREFIX = "qf_"
DEFAULT_MAX_ENTRIES = 2000


@dataclass(frozen=True, slots=True)
class StoredAction:
    """台帳の 1 件。``range`` は LSP の範囲（``{"start": {...}, "end": {...}}``、サーバーの単位のまま）。"""

    id: str
    path: RelPath
    sha256: str
    language_id: str
    server: str
    kind: str
    title: str
    range: Mapping[str, Any]
    action: Mapping[str, Any]
    diagnostic: Mapping[str, Any] | None = None


class CodeActionStore:
    """``put`` / ``get`` の台帳。スレッドセーフ（``asyncio.to_thread`` から呼んでもよい）。"""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max = max_entries
        self._by_id: OrderedDict[str, StoredAction] = OrderedDict()
        self._by_key: dict[tuple[str, str, str, str, str, str], str] = {}
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._by_id)

    def put(
        self,
        *,
        path: RelPath,
        sha256: str,
        language_id: str,
        server: str,
        action: Mapping[str, Any],
        range: Mapping[str, Any],  # noqa: A002  LSP の語彙
        diagnostic: Mapping[str, Any] | None = None,
    ) -> StoredAction:
        """アクションを台帳に載せて返す（同じキーなら既存の ID を返し、``action`` を新しいもので置き換える）。"""
        kind = str(action.get("kind") or "quickfix")
        title = str(action.get("title") or "")
        rng_key = repr(sorted((str(k), repr(sorted(v.items()))) for k, v in range.items()))
        key = (str(path), sha256, server, rng_key, kind, title)
        with self._lock:
            existing_id = self._by_key.get(key)
            if existing_id is not None and existing_id in self._by_id:
                action_id = existing_id
            else:
                action_id = f"{ID_PREFIX}{next(self._counter):04x}"
                self._by_key[key] = action_id
            stored = StoredAction(
                id=action_id,
                path=path,
                sha256=sha256,
                language_id=language_id,
                server=server,
                kind=kind,
                title=title,
                range=dict(range),
                action=dict(action),
                diagnostic=dict(diagnostic) if diagnostic is not None else None,
            )
            self._by_id[action_id] = stored
            self._by_id.move_to_end(action_id)
            while len(self._by_id) > self._max:
                old_id, old = self._by_id.popitem(last=False)
                for k, v in list(self._by_key.items()):
                    if v == old_id:
                        del self._by_key[k]
                        break
                del old
            return stored

    def get(self, action_id: str) -> StoredAction | None:
        """ID から引く。未知なら ``None``（T-13 は ``E_TARGET_NOT_FOUND``）。引いたものは新しい側へ。"""
        with self._lock:
            stored = self._by_id.get(action_id)
            if stored is not None:
                self._by_id.move_to_end(action_id)
            return stored

    @staticmethod
    def is_current(stored: StoredAction, current_sha256: str) -> bool:
        """ファイルの現在の ``sha256`` が発行時と同じか（違えば ``code_action_id_expired``）。"""
        return stored.sha256 == current_sha256
