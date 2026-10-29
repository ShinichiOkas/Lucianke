"""ワークスペースのハッシュ（仕様書 §4.5、設計書 §7.3.1）。

``<cache_root>/<workspace_hash>/`` の名前に使う。PaaD（``index/connection.py``）も編集のジャーナル
（``<cache_root>/<workspace_hash>/journal/``）も同じ名前を指すので、``index`` と ``workspace`` が互いを import できなくても
どちらからも呼べるよう ``core`` に置く。

桁数は **16 桁** が正（実装の値）。設計書 §7.3.1 の「12 桁」は古い記述。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final

__all__ = ["WORKSPACE_HASH_LEN", "workspace_hash"]

WORKSPACE_HASH_LEN: Final = 16  # 実体パスの正規化文字列の SHA-1 の 16 進の先頭桁数


def workspace_hash(root: Path | str) -> str:
    """ワークスペースのルートの実体パス（``realpath`` + ``normcase``）の SHA-1 の先頭 16 桁（16 進）。

    ``<cache_root>/<workspace_hash>/`` の名前に使う。Windows では大文字小文字と区切りの違いを吸収する。
    ``..`` やシンボリックリンクは ``realpath`` で解決するので、同じ実体への別の書き方は同じ値になる。
    """
    normalized = os.path.normcase(os.path.realpath(root))
    return hashlib.sha1(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()[:WORKSPACE_HASH_LEN]
