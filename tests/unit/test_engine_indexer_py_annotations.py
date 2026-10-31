"""Python の受け手の注釈の読み方（BUG-0117。``syntax/relations._Extractor._py_ann_type``・``index/inherited.py``）。

小さなワークスペースを ``tmp_path`` に書いて ``Indexer`` で取り込み、``relations``・``_name_calls``・``_var_types`` を SQL で見る。

* ``x: A | B``・``Union[A, B]``・``"B | A"`` の ``x.run()`` は ``A.run`` に ``structural`` で結ばない（名前だけ。``B.run`` かもしれない）。
  ``A`` にしか無い名前（``x.only_b()``）も、``A.only_b`` が無いからと黙って捨てず名前だけにする。
* ``A | None``・``Optional[A]``・``Union[A, None]`` は ``A``。モジュール直下の変数の型（``_var_types``。BUG-0094）も同じ規則で、
  ``cur: A | B = A()`` は値 ``A()`` に任せない。``OA: TypeAlias = ...`` を ``typing.TypeAlias`` 型の変数にしない。
* 別ファイルの型の別名（``type AB = A | B``・``type JA = A | None``）は索引に構成が無いので名前だけ（``TypeAlias.run`` などに結ばない）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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

WS_ID = "pa"
P = f"lk://{WS_ID}/"

FILES = {
    "pkg/__init__.py": "",
    "pkg/models.py": """\
class A:
    def run(self) -> int:
        return 1

    @classmethod
    def make(cls) -> "A":
        return cls()


class B:
    def run(self) -> int:
        return 2

    def only_b(self) -> int:
        return 3
""",
    "pkg/aliases.py": """\
from typing import Optional, TypeAlias

from pkg.models import A, B

type AB = A | B
type JA = A | None
OA: TypeAlias = Optional[A]
cur: A | B = A()
opt: Optional[A] = None
plain = A()
""",
    "pkg/use.py": """\
from typing import Optional, Union

import pkg.models
from pkg.aliases import AB, JA, OA, cur, opt, plain
from pkg.models import A, B


def f(x: A | B, y: Union[A, B], z: "B | A", o: A | None, p: Optional[A], q: "Optional[A]", t: type[A], m: pkg.models.B) -> None:
    x.run()
    y.run()
    z.run()
    x.only_b()
    o.run()
    p.run()
    q.run()
    t.make()
    m.run()


def g(a: AB, b: JA, c: OA) -> None:
    a.run()
    b.run()
    c.run()
    cur.run()
    opt.run()
    plain.run()
""",
}


@dataclass
class Env:
    indexer: Indexer
    readers: ReaderPool
    root: Path

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def line(self, rel: str, needle: str) -> int:
        """文が ``needle`` の行（1 始まり。``relations.call_site_line`` と同じ）。"""
        text = (self.root / rel).read_bytes().decode("utf-8")
        return next(i + 1 for i, t in enumerate(text.splitlines()) if t.strip() == needle)

    def calls(self, source: str) -> dict[int, set[tuple[str, str]]]:
        """``source`` の CALLS の行 → ``{(先, 水準)}``（ワークスペース内は相対表記）。"""
        out: dict[int, set[tuple[str, str]]] = {}
        for t, line, conf in self.rows(
            "SELECT target_urn, call_site_line, confidence_level FROM relations WHERE source_urn = ? AND relation_kind = 'CALLS'",
            P + source,
        ):
            out.setdefault(int(line), set()).add((str(t).removeprefix(P), str(conf)))
        return out

    def name_calls(self, source: str) -> set[tuple[str, int]]:
        return {
            (str(n), int(line))
            for n, line in self.rows("SELECT name, call_site_line FROM _name_calls WHERE source_urn = ?", P + source)
        }


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / WS_ID
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode("utf-8"))
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache())
    report = await indexer.scan()
    assert not report.errors, report.errors
    yield Env(indexer, readers, root)
    await indexer.aclose()
    readers.close()
    writer.close()


USE = "pkg/use.py"
A_RUN = ("pkg/models.py#A.run", "structural")


async def test_union_receivers_are_left_to_name_matching(env: Env) -> None:
    """票の形: ``x: A | B``・``y: Union[A, B]``・``z: "B | A"`` の ``run()`` は ``A.run`` に結ばず名前だけ。``x.only_b()`` も名前だけ。"""
    calls = env.calls(f"{USE}#f")
    names = env.name_calls(f"{USE}#f")
    for needle, name in (("x.run()", "run"), ("y.run()", "run"), ("z.run()", "run"), ("x.only_b()", "only_b")):
        line = env.line(USE, needle)
        assert line not in calls, (needle, calls.get(line))
        assert (name, line) in names, needle


async def test_optional_type_and_dotted_receivers_bind_structurally(env: Env) -> None:
    calls = env.calls(f"{USE}#f")
    expected = {
        "o.run()": A_RUN,
        "p.run()": A_RUN,
        "q.run()": A_RUN,
        "t.make()": ("pkg/models.py#A.make", "structural"),
        "m.run()": ("pkg/models.py#B.run", "structural"),
    }
    for needle, target in expected.items():
        assert calls.get(env.line(USE, needle)) == {target}, needle


async def test_module_variable_types_and_cross_file_aliases(env: Env) -> None:
    """``_var_types``（BUG-0094）も同じ規則。別ファイルの型の別名の受け手は名前だけ。"""
    var_types = {
        str(v).removeprefix(P): str(t).removeprefix(P) for v, t in env.rows("SELECT var_urn, type_urn FROM _var_types")
    }
    assert var_types == {"pkg/aliases.py#opt": "pkg/models.py#A", "pkg/aliases.py#plain": "pkg/models.py#A"}
    calls = env.calls(f"{USE}#g")
    names = env.name_calls(f"{USE}#g")
    for needle in ("opt.run()", "plain.run()"):
        assert calls.get(env.line(USE, needle)) == {A_RUN}, needle
    for needle in ("a.run()", "b.run()", "c.run()", "cur.run()"):
        line = env.line(USE, needle)
        targets = {t for t, _ in calls.get(line, set())}
        assert not any(t.endswith(".run") for t in targets), (needle, targets)
        assert ("run", line) in names, needle


def test_migration_13_to_14_reingests_python_files(tmp_path: Path) -> None:
    """v13 の DB は表を変えず、Python のファイルの ``sha256`` を空にする（受け手の注釈の読み方は取り込み直さないと直らない）。
    TS / JS と他の言語は取り込み直さない。"""
    conn = open_writer(tmp_path / "paad.sqlite")

    def tx(fn: Any) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            fn(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def facts(path: str, language: str) -> FileFacts:
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

    try:
        ensure_schema(conn)
        tx(lambda c: upsert_workspace(c, {"workspace_id": WS_ID, "root_path": "/ws"}))
        files = (("a.py", "python"), ("b.ts", "typescript"), ("d.js", "javascript"), ("R.md", "markdown"))
        for path, language in files:
            tx(lambda c, p=path, lang=language: ingest_file(c, facts(p, lang)))
        conn.execute("UPDATE _meta SET schema_version = 13")
        assert ensure_schema(conn) is SchemaAction.MIGRATED
        assert conn.execute("SELECT schema_version FROM _meta").fetchone()[0] == current_schema_version() >= 14
        assert dict(conn.execute("SELECT path, sha256 FROM files").fetchall()) == {
            "a.py": "",
            "b.ts": "a" * 64,
            "d.js": "a" * 64,
            "R.md": "a" * 64,
        }
    finally:
        conn.close()
