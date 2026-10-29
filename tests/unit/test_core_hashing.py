"""core.hashing（ワークスペースのハッシュ。index と、ジャーナルなど他の層が共有する）のテスト。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from lucianke.core import hashing
from lucianke.core.hashing import WORKSPACE_HASH_LEN, workspace_hash
from lucianke.index import connection


def test_is_16_hex_of_the_normalized_real_path(tmp_path: Path) -> None:
    h = workspace_hash(tmp_path)
    assert WORKSPACE_HASH_LEN == 16
    assert len(h) == 16
    assert int(h, 16) >= 0
    expected = hashlib.sha1(os.path.normcase(os.path.realpath(tmp_path)).encode("utf-8")).hexdigest()[:16]
    assert h == expected


def test_same_directory_written_differently_gives_the_same_hash(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert workspace_hash(tmp_path / "a" / "..") == workspace_hash(tmp_path)
    assert workspace_hash(str(tmp_path)) == workspace_hash(tmp_path)  # str も Path も受ける
    assert workspace_hash(tmp_path / "a") != workspace_hash(tmp_path)


def test_index_connection_uses_the_same_function(tmp_path: Path) -> None:
    assert connection.workspace_hash is hashing.workspace_hash
    assert connection.WORKSPACE_HASH_LEN == WORKSPACE_HASH_LEN
    # ジャーナルなど、同じ <cache_root>/<workspace_hash>/ の下に置く他の層と名前が一致する
    cache = tmp_path / "cache"
    ws = tmp_path / "ws"
    ws.mkdir()
    assert connection.db_path_for(cache, ws).parent == cache / workspace_hash(ws)
