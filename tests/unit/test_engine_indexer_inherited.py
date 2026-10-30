"""基底クラスが別ファイルにある継承メンバーへの参照（BUG-0075。``index/inherited.py``・``engine/indexer._settle_relations``）。

フィクスチャ ``tests/fixtures/inherited_calls``（README に各呼び出しの意図）を ``tmp_path`` に写して ``Indexer`` で取り込み、
``relations`` を SQL で見る。編集（上書きを足す・消す、基底のメンバーを消す・戻す、基底の並びを変える、途中の基底のファイルを消す、
基底より先に派生クラスを取り込む、差分走査）のあとも、行が受け手のクラスの C3 の順で最初に見つかるメンバーを指すことを確かめる。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.model import Confidence, RelPath, Tier
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool, open_writer
from lucianke.index.ingest import FileFacts, ingest_file, upsert_workspace
from lucianke.index.migrate import SchemaAction, current_schema_version, ensure_schema
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "inherited_calls"
WS_ID = "ic"

BASE = "pkg/base.py"
MID = "pkg/mid.py"
SUB = "pkg/sub.py"
USE = "pkg/use.py"
MIXED = "pkg/mixed.py"


@dataclass
class Env:
    indexer: Indexer
    readers: ReaderPool
    root: Path

    def text(self, rel: str) -> str:
        return (self.root / rel).read_bytes().decode("utf-8")

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def targets(self, source: str, kind: str = "CALLS") -> set[str]:
        """``source``（``path#sym``）が出す ``kind`` の行の先（ワークスペース内は相対表記）。"""
        return {
            t.removeprefix(f"lk://{WS_ID}/")
            for (t,) in self.rows(
                "SELECT target_urn FROM relations WHERE source_urn = ? AND relation_kind = ?",
                f"lk://{WS_ID}/{source}",
                kind,
            )
        }

    def callers(self, target: str) -> set[tuple[str, int]]:
        """``target``（``path#sym``）を指す CALLS の ``(元の path#sym, 行)``。"""
        return {
            (s.removeprefix(f"lk://{WS_ID}/"), int(line))
            for s, line in self.rows(
                "SELECT source_urn, call_site_line FROM relations WHERE target_urn = ? AND relation_kind = 'CALLS'",
                f"lk://{WS_ID}/{target}",
            )
        }

    def helper_calls(self) -> set[tuple[str, str]]:
        """Python 側（``pkg/``）で ``helper`` という名前のメンバーを指す CALLS の ``(元, 先)``（誤って別の定義に結んでいないかを見る）。"""
        return {
            (s.removeprefix(f"lk://{WS_ID}/"), t.removeprefix(f"lk://{WS_ID}/"))
            for s, t in self.rows(
                "SELECT source_urn, target_urn FROM relations WHERE relation_kind = 'CALLS'"
                " AND target_urn LIKE ? AND target_urn LIKE '%.helper'",
                f"lk://{WS_ID}/pkg/%",
            )
        }


async def _make(tmp_path: Path, *, scan: bool) -> tuple[Env, IndexWriter]:
    root = tmp_path / WS_ID
    shutil.copytree(FIXTURE, root)
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache())
    if scan:
        report = await indexer.scan()
        assert not report.errors, report.errors
    return Env(indexer, readers, root), writer


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e, writer = await _make(tmp_path, scan=True)
    yield e
    await e.indexer.aclose()
    e.readers.close()
    writer.close()


@pytest.fixture
async def unscanned(tmp_path: Path) -> AsyncIterator[Env]:
    """フィクスチャをディスクに置いただけ（取り込みは ``index_file`` で好きな順に）。"""
    e, writer = await _make(tmp_path, scan=False)
    yield e
    await e.indexer.aclose()
    e.readers.close()
    writer.close()


def line_of(env: Env, rel: str, needle: str) -> int:
    return next(i + 1 for i, text in enumerate(env.text(rel).splitlines()) if needle in text)


# --- 取り込み直後 ---------------------------------------------------------------------------------


async def test_scan_resolves_members_inherited_across_files(env: Env) -> None:
    assert env.targets(f"{SUB}#Sub.run") == {f"{BASE}#Base.helper"}  # 2 段上（Sub → Mid → Base）
    assert env.targets(f"{SUB}#Sub.loud") == {f"{MID}#Mid.shout"}  # 途中の Mid が上書き
    assert env.targets(f"{SUB}#Sub.build") == {f"{BASE}#Base.make"}  # cls.make()
    assert env.targets(f"{SUB}#Sub.own") == {f"{SUB}#Sub.tag"}  # 自分のもの
    assert env.targets(f"{SUB}#use") == {f"{BASE}#Base.helper"}  # s: Sub
    assert env.targets(f"{SUB}#fresh") == {f"{SUB}#Sub", f"{MID}#Mid.shout"}  # Sub().shout()
    assert env.targets(f"{USE}#go") == {f"{BASE}#Base.helper"}  # base も mid も import しないファイル
    assert env.targets(f"{USE}#label", "REFERENCES") >= {f"{BASE}#Base.name"}  # クラス属性の参照
    assert env.callers(f"{BASE}#Base.shout") == set()  # Sub からの shout は Mid.shout


async def test_scan_rows_are_structural_and_point_at_the_base_file(env: Env) -> None:
    rows = env.rows(
        "SELECT r.confidence_level, r.target_file_id = (SELECT file_id FROM files WHERE path = ?), r.use_kind,"
        " r.call_site_line, r.call_site_col FROM relations r WHERE r.source_urn = ? AND r.relation_kind = 'CALLS'",
        BASE,
        f"lk://{WS_ID}/{USE}#go",
    )
    line = line_of(env, USE, "return s.helper()")
    col = env.text(USE).splitlines()[line - 1].index("helper") + 1
    assert rows == [("structural", 1, "call", line, col)]


async def test_external_base_before_the_member_is_not_attributed(env: Env) -> None:
    """``Worker(threading.Thread, Base)`` の ``self.helper()`` は結ばない。``Plain(Base, Thread)``・``Shape(ABC, Base)`` は結ぶ。"""
    assert env.targets(f"{MIXED}#Plain.go") == {f"{BASE}#Base.helper"}
    assert env.targets(f"{MIXED}#Shape.go") == {f"{BASE}#Base.helper"}
    assert env.targets(f"{MIXED}#Worker.go") == set()
    assert env.rows(
        "SELECT resolved_urn FROM _member_refs WHERE class_urn = ?", f"lk://{WS_ID}/{MIXED}#Worker"
    ) == [(None,)]


async def test_typescript_class_and_interface_members(env: Env) -> None:
    assert env.targets("web/sub.ts#Sub.run") == {"web/base.ts#Base.helper"}
    assert env.targets("web/sub.ts#use") == {"web/base.ts#Base.helper", "web/base.ts#Pingable.ping"}
    assert env.targets("web/sub.ts#fresh") == {"web/sub.ts#Sub", "web/base.ts#Base.helper"}


# --- 編集に追従する -------------------------------------------------------------------------------


async def test_override_added_and_removed_in_the_subclass(env: Env) -> None:
    original = env.text(SUB)
    env.write(
        SUB,
        original.replace(
            "    def tag(self):", "    def helper(self):\n        return 2\n\n    def tag(self):"
        ),
    )
    await env.indexer.index_file(SUB)
    assert env.targets(f"{SUB}#Sub.run") == {f"{SUB}#Sub.helper"}
    assert env.targets(f"{SUB}#use") == {f"{SUB}#Sub.helper"}
    assert env.targets(f"{USE}#go") == {f"{SUB}#Sub.helper"}
    assert env.targets(f"{MIXED}#Plain.go") == {f"{BASE}#Base.helper"}  # 別の派生クラスは基底のまま

    env.write(SUB, original)
    await env.indexer.index_file(SUB)
    assert env.targets(f"{SUB}#Sub.run") == {f"{BASE}#Base.helper"}
    assert env.targets(f"{USE}#go") == {f"{BASE}#Base.helper"}
    assert env.rows("SELECT count(*) FROM symbols WHERE qualified_name = 'Sub.helper'") == [(0,)]


async def test_override_in_an_intermediate_base_rebinds_files_that_do_not_import_it(env: Env) -> None:
    """``Mid`` に ``helper`` を足すと、``mid`` を import していない ``use.py`` の ``s.helper()`` も ``Mid.helper`` に結び直る（取り込み直さずに）。"""
    original = env.text(MID)
    ingested = env.rows("SELECT indexed_at FROM files WHERE path = ?", USE)
    env.write(MID, original + "\n    def helper(self):\n        return 3\n")
    await env.indexer.index_file(MID)
    assert env.targets(f"{USE}#go") == {f"{MID}#Mid.helper"}
    assert env.targets(f"{SUB}#Sub.run") == {f"{MID}#Mid.helper"}
    assert env.targets(f"{MIXED}#Plain.go") == {f"{BASE}#Base.helper"}  # Mid を通らない
    assert env.rows("SELECT indexed_at FROM files WHERE path = ?", USE) == ingested

    env.write(MID, original)
    await env.indexer.index_file(MID)
    assert env.targets(f"{USE}#go") == {f"{BASE}#Base.helper"}
    assert env.targets(f"{SUB}#Sub.run") == {f"{BASE}#Base.helper"}


async def test_member_removed_from_the_base_and_added_back(env: Env) -> None:
    original = env.text(BASE)
    env.write(BASE, original.replace("    def helper(self):\n        return 1\n\n", ""))
    await env.indexer.index_file(BASE)
    assert env.helper_calls() == set()  # 別の定義に誤って結ばない
    assert env.targets(f"{USE}#go") == set()

    env.write(BASE, original)
    await env.indexer.index_file(BASE)
    assert env.callers(f"{BASE}#Base.helper") == {
        (f"{SUB}#Sub.run", line_of(env, SUB, "return self.helper()")),
        (f"{SUB}#use", line_of(env, SUB, "return s.helper()")),
        (f"{USE}#go", line_of(env, USE, "return s.helper()")),
        (f"{MIXED}#Plain.go", line_of(env, MIXED, "return self.helper()")),
        (f"{MIXED}#Shape.go", 19),
    }


async def test_changing_the_base_list_rebinds_descendants(env: Env) -> None:
    """``Mid(Base)`` → ``Mid(Other)``（シンボルの集合は変わらない）。``Sub`` 経由の ``helper`` は ``Other.helper``、``make`` は結べなくなる。"""
    env.write("pkg/other.py", "class Other:\n    def helper(self):\n        return 4\n")
    await env.indexer.index_file("pkg/other.py")
    env.write(
        MID,
        env.text(MID)
        .replace("from pkg.base import Base", "from pkg.other import Other")
        .replace("class Mid(Base):", "class Mid(Other):"),
    )
    await env.indexer.index_file(MID)
    assert env.targets(f"{USE}#go") == {"pkg/other.py#Other.helper"}
    assert env.targets(f"{SUB}#Sub.run") == {"pkg/other.py#Other.helper"}
    assert env.targets(f"{SUB}#Sub.build") == set()
    assert env.targets(f"{SUB}#Sub.loud") == {f"{MID}#Mid.shout"}


async def test_removing_the_intermediate_file_drops_rows_through_it(env: Env) -> None:
    (env.root / MID).unlink()
    assert await env.indexer.remove(MID) is True
    assert env.targets(f"{SUB}#Sub.run") == set()
    assert env.targets(f"{USE}#go") == set()
    assert env.targets(f"{SUB}#Sub.loud") == set()
    assert env.targets(f"{MIXED}#Plain.go") == {f"{BASE}#Base.helper"}


async def test_differential_scan_follows_base_edits(env: Env) -> None:
    env.write(MID, env.text(MID) + "\n    def helper(self):\n        return 3\n")
    report = await env.indexer.scan()
    assert report.indexed == 1
    assert env.targets(f"{USE}#go") == {f"{MID}#Mid.helper"}
    assert env.targets(f"{SUB}#use") == {f"{MID}#Mid.helper"}


async def test_typescript_override_follows_edits(env: Env) -> None:
    original = env.text("web/sub.ts")
    env.write(
        "web/sub.ts",
        original.replace(
            "  run(): number {", "  helper(): number {\n    return 2;\n  }\n\n  run(): number {"
        ),
    )
    await env.indexer.index_file("web/sub.ts")
    assert env.targets("web/sub.ts#Sub.run") == {"web/sub.ts#Sub.helper"}
    assert env.targets("web/sub.ts#use") == {"web/sub.ts#Sub.helper", "web/base.ts#Pingable.ping"}
    env.write("web/sub.ts", original)
    await env.indexer.index_file("web/sub.ts")
    assert env.targets("web/sub.ts#use") == {"web/base.ts#Base.helper", "web/base.ts#Pingable.ping"}


async def test_subclasses_indexed_before_their_bases_resolve_when_the_bases_arrive(unscanned: Env) -> None:
    """派生クラス側を先に取り込む（基底のファイルはディスクにあるが索引に無い）。基底が来たら結ばれる。"""
    env = unscanned
    for rel in ("pkg/__init__.py", USE, SUB):
        await env.indexer.index_file(rel)
    assert env.targets(f"{USE}#go") == set()
    await env.indexer.index_file(MID)
    assert env.targets(f"{SUB}#Sub.loud") == {f"{MID}#Mid.shout"}
    assert env.targets(f"{USE}#go") == set()  # helper は Base にある（まだ索引に無い）
    await env.indexer.index_file(BASE)
    assert env.targets(f"{USE}#go") == {f"{BASE}#Base.helper"}
    assert env.targets(f"{SUB}#Sub.run") == {f"{BASE}#Base.helper"}
    assert env.targets(f"{SUB}#Sub.build") == {f"{BASE}#Base.make"}


# --- スキーマの移行（v5 → v6） ---------------------------------------------------------------------


def _tx(conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], object]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        fn(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _facts(path: str, language: str) -> FileFacts:
    return FileFacts(
        workspace_id=WS_ID,
        path=RelPath(path),
        language=language,
        tier=Tier.T1,
        line_count=1,
        byte_size=1,
        sha256="a" * 64,
        is_test=False,
        confidence=Confidence.STRUCTURAL,
    )


def test_migration_5_to_6_adds_the_table_and_reingests_code_files(tmp_path: Path) -> None:
    """v5 の DB は表を足し、関係を抽出する言語のファイルの ``sha256`` を空にする（捨てていた参照を次の差分走査で取り込み直す）。"""
    conn = open_writer(tmp_path / "paad.sqlite")
    try:
        ensure_schema(conn)
        _tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS_ID, "root_path": "/ws"}))
        for path, language in (
            ("a.py", "python"),
            ("b.ts", "typescript"),
            ("c.js", "javascript"),
            ("R.md", "markdown"),
        ):
            _tx(conn, lambda c, p=path, lang=language: ingest_file(c, _facts(p, lang)))
        conn.execute("DROP TABLE _member_refs")
        conn.execute("UPDATE _meta SET schema_version = 5")
        assert ensure_schema(conn) is SchemaAction.MIGRATED
        assert conn.execute("SELECT schema_version FROM _meta").fetchone()[0] == current_schema_version() >= 6
        assert conn.execute("SELECT count(*) FROM _member_refs").fetchone()[0] == 0
        assert dict(conn.execute("SELECT path, sha256 FROM files").fetchall()) == {
            "a.py": "",
            "b.ts": "",
            "c.js": "",
            "R.md": "a" * 64,
        }
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"_member_refs_class", "_member_refs_file"} <= names
    finally:
        conn.close()


async def test_member_refs_go_with_the_file(env: Env) -> None:
    assert (
        env.rows(
            "SELECT count(*) FROM _member_refs m JOIN files f ON f.file_id = m.file_id WHERE f.path = ?", USE
        )[0][0]
        > 0
    )
    (env.root / USE).unlink()
    await env.indexer.remove(USE)
    assert env.rows("SELECT count(*) FROM _member_refs WHERE source_urn LIKE ?", f"lk://{WS_ID}/{USE}%") == [
        (0,)
    ]
