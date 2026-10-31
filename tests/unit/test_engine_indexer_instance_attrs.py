"""型の分かる受け手のインスタンス属性の読み書き（BUG-0122。``index/inherited.py``・``syntax/relations._member_of``・T-06）。

小さなワークスペースを ``tmp_path`` に書いて ``Indexer`` で取り込み、``relations``・``_member_refs`` を SQL で、T-06 をパイプラインで見る。

* ``a: Src``（別ファイルのクラス）の ``a.skipped``（``Src.__init__`` の ``self.skipped = 0``）の読み・書き・``+=`` は、インスタンス属性
  ``pkg/lx.py#Src.skipped``（``_attr_defs`` の行。シンボルではない）に ``structural`` で結ぶ。基底の ``self.inherited = 1`` は C3 の順で
  ``Base.inherited``。同じファイルの ``self.skipped``・``s.skipped`` も同じ先。
* クラスのメンバー（シンボル）が線形化のどこかにあればそちらが先（``Conf.limit``・メソッドは今までどおり）。
* どれでもない名前は ``_member_refs`` に結べないまま残し（黙って捨てない）、受け手の型が対象のクラスの基底なら T-06 が
  ``reason: base_receiver`` で書く。
* クラスのファイルを編集すると、使う側を取り込み直さずに結び直る（整えの段）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.app import build_services
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t06_find_references_and_implementations as t06
from lucianke.tools._services import ToolServices
from lucianke.workspace.types import Workspace

WS_ID = "ia"
P = f"lk://{WS_ID}/"
LX = "pkg/lx.py"
USE = "pkg/use.py"

FILES = {
    "pkg/__init__.py": "",
    LX: """\
class Base:
    def __init__(self) -> None:
        self.inherited = 1


class Src(Base):
    def __init__(self) -> None:
        super().__init__()
        self.skipped = 0

    def go(self) -> int:
        return self.skipped + self.inherited


class Conf:
    limit: int = 3

    def __init__(self) -> None:
        self.limit = 4


def same(s: Src, c: Conf) -> int:
    return s.skipped + c.limit + s.go()
""",
    USE: """\
from pkg import lx
from pkg.lx import Base, Conf, Src


def h(a: Src, b: lx.Src) -> int:
    a.skipped = 1
    a.skipped += 1
    return a.skipped + b.skipped + a.go() + a.inherited


def unknown(a: Src, base: Base, c: Conf) -> int:
    return a.nope + base.skipped + c.limit
""",
}


@dataclass(slots=True)
class _Services(ToolServices):
    db_path: Path | None = None


@dataclass
class Env:
    indexer: Indexer
    readers: ReaderPool
    root: Path
    db: Path

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def col(self, rel: str, line: int, needle: str, nth: int = 0) -> int:
        """``rel`` の ``line`` 行目（1 始まり）の ``needle`` の ``nth`` 番目の位置（1 始まり。``relations.call_site_col`` と同じ）。"""
        text = (self.root / rel).read_bytes().decode("utf-8").splitlines()[line - 1]
        at = -1
        for _ in range(nth + 1):
            at = text.index(needle, at + 1)
        return at + 1

    def refs(self, source: str) -> set[tuple[str, int, int, str, str]]:
        """``source`` の REFERENCES の行 → ``{(先, 行, 列, use, 水準)}``（ワークスペース内は相対表記）。"""
        return {
            (str(t).removeprefix(P), int(line), int(col), str(use), str(conf))
            for t, line, col, use, conf in self.rows(
                "SELECT target_urn, call_site_line, call_site_col, use_kind, confidence_level FROM relations"
                " WHERE source_urn = ? AND relation_kind = 'REFERENCES' AND instr(target_urn, '#') > 0",
                P + source,
            )
        }

    def pipeline(self) -> Pipeline:
        base = build_services(lambda: [str(self.root)])
        services = _Services(roots=base.roots, trees=base.trees, lines=base.lines, db_path=self.db)
        registry = ToolRegistry()
        registry.register(t06.TOOL_SPEC)
        return Pipeline(registry, services=services)


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
    yield Env(indexer, readers, root.resolve(), db)
    await indexer.aclose()
    readers.close()
    writer.close()


SKIPPED = "pkg/lx.py#Src.skipped"
INHERITED = "pkg/lx.py#Base.inherited"
S = "structural"


async def test_cross_file_typed_receiver_binds_to_the_instance_attribute(env: Env) -> None:
    """票の形: ``a: Src`` / ``b: lx.Src`` の ``a.skipped``・``b.skipped`` と書き込み・``+=``、基底の ``a.inherited``（C3）。"""
    c = env.col
    assert env.refs(f"{USE}#h") == {
        (SKIPPED, 6, c(USE, 6, "skipped"), "w", S),
        (SKIPPED, 7, c(USE, 7, "skipped"), "rw", S),
        (SKIPPED, 8, c(USE, 8, "skipped"), "r", S),
        (SKIPPED, 8, c(USE, 8, "skipped", 1), "r", S),
        (INHERITED, 8, c(USE, 8, "inherited"), "r", S),
    }
    assert env.rows("SELECT count(*) FROM _name_calls") == [(0,)]


async def test_same_file_receivers_bind_and_class_members_still_win(env: Env) -> None:
    """同じファイルの ``self.skipped``・``s.skipped`` も同じ先。``Conf.limit`` はクラスのメンバー（シンボル）が先（今までどおり）。"""
    c = env.col
    assert env.refs(f"{LX}#Src.go") == {
        (SKIPPED, 12, c(LX, 12, "skipped"), "r", S),
        (INHERITED, 12, c(LX, 12, "inherited"), "r", S),
    }
    assert env.refs(f"{LX}#same") == {
        (SKIPPED, 23, c(LX, 23, "skipped"), "r", S),
        ("pkg/lx.py#Conf.limit", 23, c(LX, 23, "limit"), "r", S),
    }
    assert env.refs(f"{LX}#Conf.__init__") == {("pkg/lx.py#Conf.limit", 19, c(LX, 19, "limit"), "w", S)}
    calls = env.rows(
        "SELECT target_urn FROM relations WHERE source_urn = ? AND relation_kind = 'CALLS'", P + f"{LX}#same"
    )
    assert calls == [(P + "pkg/lx.py#Src.go",)]


async def test_unknown_attribute_is_kept_not_dropped(env: Env) -> None:
    """``a.nope``・``base.skipped``（``Base`` には無い）は先を作らず、``_member_refs`` に結べないまま残す。``c.limit`` は ``Conf.limit``。"""
    assert env.refs(f"{USE}#unknown") == {("pkg/lx.py#Conf.limit", 12, env.col(USE, 12, "limit"), "r", S)}
    left = env.rows(
        "SELECT class_urn, name, call_site_line FROM _member_refs WHERE source_urn = ? AND resolved_urn IS NULL",
        P + f"{USE}#unknown",
    )
    assert sorted((str(k).removeprefix(P), n, line) for k, n, line in left) == [
        ("pkg/lx.py#Base", "skipped", 12),
        ("pkg/lx.py#Src", "nope", 12),
    ]


async def test_t06_targets_the_instance_attribute(env: Env) -> None:
    """T-06 は ``Src.skipped``（シンボルでないインスタンス属性）を対象にでき、型の分かる受け手の読み書きを ``structural`` で返す。
    受け手の型が基底 ``Base``（``skipped`` を持たない）の ``base.skipped`` は ``reason: base_receiver`` で書く（黙って捨てない）。"""
    pipeline = env.pipeline()
    for target in ({"path": LX, "symbol_name": "Src.skipped"}, {"urn": P + SKIPPED}):
        r = await pipeline.call_tool(
            "find_references_and_implementations",
            {"target": target, "query_type": "references", "format": "json"},
        )
        assert not r.is_error, r.text
        data = r.structured["data"]
        got = {(x["path"], x["line"], x["use"], x["confidence_level"]) for x in data["references"]}
        assert got == {
            (USE, 6, "w", S),
            (USE, 7, "rw", S),
            (USE, 8, "r", S),
            (LX, 12, "r", S),
            (LX, 23, "r", S),
        } | {(USE, 8, "r", S)}
        assert (
            len(data["references"]) == 6
        )  # use.py の 8 行目は a.skipped と b.skipped の 2 箇所。宣言（9 行目）は含めない
        assert [
            (u["reason"], [(s["path"], s["line"], s["receiver_class"]) for s in u["sites"]])
            for u in data["unattributed_calls"]
        ] == [("base_receiver", [(USE, 12, "Base")])]
        assert any("typed as a base class (Base)" in w for w in r.structured["warnings"]), r.structured[
            "warnings"
        ]
    r = await pipeline.call_tool(
        "find_references_and_implementations",
        {
            "target": {"path": LX, "symbol_name": "Src.skipped"},
            "query_type": "references",
            "include_declaration": True,
        },
    )
    assert not r.is_error, r.text
    assert f"{LX}:9:" in r.text


async def test_editing_the_class_rebinds_without_reingesting_users(env: Env) -> None:
    """``self.skipped = 0`` を ``Base`` に移すと、``use.py`` を取り込み直さずに ``Base.skipped`` に結び直る。消すと行も消える。"""
    use_sha = env.rows("SELECT sha256 FROM files WHERE path = ?", USE)
    moved = FILES[LX].replace(
        "        self.inherited = 1\n", "        self.inherited = 1\n        self.skipped = 0\n", 1
    )
    moved = moved.replace(
        "        super().__init__()\n        self.skipped = 0\n", "        super().__init__()\n", 1
    )
    (env.root / LX).write_bytes(moved.encode("utf-8"))
    await env.indexer.index_file(LX)
    assert env.rows("SELECT sha256 FROM files WHERE path = ?", USE) == use_sha
    targets = {t for t, line, *_ in env.refs(f"{USE}#h") if line in (6, 7)}
    assert targets == {"pkg/lx.py#Base.skipped"}
    gone = moved.replace("        self.skipped = 0\n", "", 1)
    (env.root / LX).write_bytes(gone.encode("utf-8"))
    await env.indexer.index_file(LX)
    assert {t for t, *_ in env.refs(f"{USE}#h")} == {INHERITED}
