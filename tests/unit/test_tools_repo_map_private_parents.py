"""T-01 ``get_repo_map``: 非公開の親（``priv`` / ``prot``）の下のメンバーは公開シンボルとして出さない（BUG-0124）。

``_DbLock`` のような ``_`` 始まりのクラスの ``hold`` / ``acquire`` は、修飾子は ``pub`` でもモジュールの外から使う API ではない。
地図の限られた行を使わず、ファイルの重要度（``files.pagerank_score`` と ``rank.repo_map_files``）にも数えない。
Python の ``_`` 始まりのクラス・入れ子の非公開クラス、TypeScript の export されていないクラスで確かめる。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from lucianke.engine.indexer import Indexer
from lucianke.index import rank
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t01_get_repo_map as t01
from lucianke.tools._base import Services
from lucianke.workspace.types import Workspace

WS_ID = "privparents"

FILES = {
    "src/db.py": (
        "class _DbLock:\n"
        "    def hold(self, what):\n"
        "        return what\n"
        "\n"
        "    def release(self):\n"
        "        return None\n"
        "\n"
        "\n"
        "class Store:\n"
        "    class _Cursor:\n"
        "        def advance(self):\n"
        "            return 1\n"
        "\n"
        "    def open(self):\n"
        "        return _DbLock().hold('open')\n"
        "\n"
        "\n"
        "def connect():\n"
        "    return Store().open()\n"
    ),
    "src/use.py": (
        "from db import Store, _DbLock, connect\n"
        "\n"
        "\n"
        "def run():\n"
        "    lock = _DbLock()\n"
        "    lock.hold('a')\n"
        "    lock.release()\n"
        "    Store._Cursor().advance()\n"
        "    return connect()\n"
        "\n"
        "\n"
        "def run2():\n"
        "    lock = _DbLock()\n"
        "    lock.hold('b')\n"
        "    return connect()\n"
    ),
    # 非公開のクラスだけのファイル（地図の代表になれるシンボルが無い）
    "src/internal.py": (
        "class _Hidden:\n    def pulse(self):\n        return 1\n\n    def beat(self):\n        return 2\n"
    ),
    "src/use_hidden.py": (
        "from internal import _Hidden\n"
        "\n"
        "\n"
        "def a():\n"
        "    return _Hidden().pulse() + _Hidden().beat()\n"
        "\n"
        "\n"
        "def b():\n"
        "    return _Hidden().pulse() + _Hidden().beat()\n"
    ),
    "web/store.ts": (
        "class Internal {\n"
        "  hold(what: string): string { return what; }\n"
        "}\n"
        "\n"
        "export class Api {\n"
        "  open(): string { return new Internal().hold('x'); }\n"
        "}\n"
    ),
    "web/use.ts": (
        "import { Api } from './store';\n"
        "\n"
        "export function go(): string { return new Api().open(); }\n"
        "export function go2(): string { return new Api().open(); }\n"
    ),
}


class _Svc(Services):
    def __init__(self, db_path: Path, lines: ModuleType, root: Path) -> None:
        self.db_path = db_path
        self.lines = lines
        self.root = root

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        return Workspace(workspace_id=WS_ID, root=self.root, case_insensitive=os.name == "nt")

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        return WS_ID


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    base = tmp_path_factory.mktemp("privparents")
    root = Path(os.path.realpath(base / WS_ID))
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    db = base / "db" / "paad.sqlite"

    async def scan() -> None:
        writer = IndexWriter(db)
        readers = ReaderPool(db, size=2)
        try:
            ws = Workspace(workspace_id=WS_ID, root=root, case_insensitive=os.name == "nt")
            indexer = Indexer(ws, writer, readers, TreeCache())
            report = await indexer.scan()
            assert not report.errors, report.errors
            await indexer.aclose()
        finally:
            readers.close()
            writer.close()

    asyncio.run(scan())
    return root, db


async def _map(indexed: tuple[Path, Path], **args: Any) -> list[dict[str, Any]]:
    root, db = indexed
    registry = ToolRegistry()
    registry.register(t01.SPEC)
    pipeline = Pipeline(registry, services=_Svc(db, compact, root))
    r = await pipeline.call_tool("get_repo_map", {"format": "json", "max_tokens": 6000, **args})
    assert not r.is_error, r.text
    return r.structured["data"]["files"]


def _names(files: list[dict[str, Any]], path: str) -> list[str]:
    return [s["qualified_name"] for f in files if f["path"] == path for s in f["symbols"]]


async def test_members_of_a_private_python_class_are_not_public_symbols(indexed: tuple[Path, Path]) -> None:
    files = await _map(indexed, max_symbols_per_file=50)
    names = _names(files, "src/db.py")
    assert "Store" in names and "Store.open" in names and "connect" in names
    assert not [n for n in names if n.startswith("_DbLock")], names
    assert not [n for n in names if "_Cursor" in n], names  # 入れ子の非公開クラスのメンバーも


async def test_members_of_a_private_class_are_hidden_in_every_ranking(indexed: tuple[Path, Path]) -> None:
    for ranking in ("pagerank", "fan_in", "alpha"):
        files = await _map(indexed, max_symbols_per_file=50, ranking=ranking)
        assert not [n for n in _names(files, "src/db.py") if n.startswith("_DbLock")], ranking


async def test_members_of_a_class_that_is_not_exported_are_not_public_symbols(
    indexed: tuple[Path, Path],
) -> None:
    files = await _map(indexed, max_symbols_per_file=50)
    names = _names(files, "web/store.ts")
    assert "Api" in names and "Api.open" in names
    assert not [n for n in names if n.startswith("Internal")], names


async def test_a_file_with_only_a_private_class_has_no_importance_from_its_members(
    indexed: tuple[Path, Path],
) -> None:
    _root, db = indexed
    conn = sqlite3.connect(db)
    try:
        scores = {f.path: f for f in rank.repo_map_files(conn, WS_ID, per_file=50)}
        stored = dict(
            conn.execute("SELECT path, pagerank_score FROM files WHERE workspace_id = ?", (WS_ID,)).fetchall()
        )
    finally:
        conn.close()
    assert scores["src/internal.py"].score == 0.0
    assert not stored["src/internal.py"]
    assert scores["src/db.py"].score > 0.0  # 公開の Store / connect は数える


async def test_public_members_keep_their_place(indexed: tuple[Path, Path]) -> None:
    files = await _map(indexed, max_symbols_per_file=50)
    assert {"run", "run2"} <= set(_names(files, "src/use.py"))
    assert {"go", "go2"} <= set(_names(files, "web/use.ts"))
