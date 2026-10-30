"""受け手の決め方（BUG-0079・BUG-0081。``syntax/relations.py``・``index/inherited.py``・``engine/indexer._settle_relations``）。

フィクスチャ ``tests/fixtures/receiver_calls``（README に各呼び出しの意図）を ``tmp_path`` に写して ``Indexer`` で取り込み、
``relations`` と内部表を SQL で見る。

* ``super().m()``（``super(C, self).m()``・TS の ``super.m()``）は、書いたクラスの C3 線形化で **次のクラスから** 最初に ``m`` を
  持つクラスのメンバーへの ``structural`` の行になる（基底が別ファイルでも、菱形でも）。名前だけの呼び出し（``_name_calls``）にはしない。
  中身の分からない外部の基底に先に当たったら結ばない。基底のファイルを編集すると結び直る。
* ``a.b.m()`` は受け手 ``a.b`` の型で決める。型が構文から分からなければ ``type(a)`` のクラス・同じファイルの同名には結ばない。
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
from lucianke.index.inherited import blocked_member_refs
from lucianke.index.migrate import SchemaAction, current_schema_version, ensure_schema
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "receiver_calls"
WS_ID = "rc"

BASE = "pkg/base.py"
SUB = "pkg/sub.py"
MID = "pkg/mid.py"
LEAF = "pkg/leaf.py"
DIAMOND = "pkg/diamond.py"
DIAMOND_USE = "pkg/diamond_use.py"
MIXED = "pkg/mixed.py"
CHAIN = "pkg/chain.py"


@dataclass
class Env:
    indexer: Indexer
    readers: ReaderPool
    root: Path

    def text(self, rel: str) -> str:
        return (self.root / rel).read_bytes().decode("utf-8")

    def write(self, rel: str, text: str) -> None:
        (self.root / rel).write_bytes(text.encode("utf-8"))

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def targets(self, source: str, name: str | None = None) -> set[str]:
        """``source``（``path#sym``）の CALLS の先（ワークスペース内は相対表記。``name`` を渡すとその名前のメンバーだけ）。"""
        out = set()
        for (t,) in self.rows(
            "SELECT target_urn FROM relations WHERE source_urn = ? AND relation_kind = 'CALLS'",
            f"lk://{WS_ID}/{source}",
        ):
            short = t.removeprefix(f"lk://{WS_ID}/")
            if name is None or short.endswith(f".{name}"):
                out.add(short)
        return out

    def name_calls(self, source: str) -> list[tuple[str, int]]:
        return [
            (n, int(line))
            for n, line in self.rows(
                "SELECT name, call_site_line FROM _name_calls WHERE source_urn = ? ORDER BY call_site_line",
                f"lk://{WS_ID}/{source}",
            )
        ]


async def _make(tmp_path: Path) -> tuple[Env, IndexWriter]:
    root = tmp_path / WS_ID
    shutil.copytree(FIXTURE, root)
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache())
    report = await indexer.scan()
    assert not report.errors, report.errors
    return Env(indexer, readers, root), writer


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e, writer = await _make(tmp_path)
    yield e
    await e.indexer.aclose()
    e.readers.close()
    writer.close()


def line_of(env: Env, rel: str, needle: str) -> int:
    return next(i + 1 for i, text in enumerate(env.text(rel).splitlines()) if needle in text)


# --- super()（BUG-0081） -------------------------------------------------------------------------


async def test_super_call_binds_to_the_base_in_another_file(env: Env) -> None:
    """``Sub.save`` の ``super().save()`` は ``Base.save``（``Sub.save`` 自身でも名前だけの呼び出しでもない）。"""
    assert env.targets(f"{SUB}#Sub.save", "save") == {f"{BASE}#Base.save"}
    assert env.targets(f"{SUB}#Sub.__init__", "__init__") == {f"{BASE}#Base.__init__"}
    assert env.name_calls(f"{SUB}#Sub.save") == []
    rows = env.rows(
        "SELECT confidence_level, use_kind, call_site_line FROM relations WHERE source_urn = ? AND target_urn = ?",
        f"lk://{WS_ID}/{SUB}#Sub.save",
        f"lk://{WS_ID}/{BASE}#Base.save",
    )
    assert rows == [("structural", "call", line_of(env, SUB, "return super().save()"))]


async def test_explicit_two_argument_super(env: Env) -> None:
    assert env.targets(f"{SUB}#Sub.load", "load") == {f"{BASE}#Base.load"}
    assert env.name_calls(f"{SUB}#Sub.load") == []


async def test_super_skips_the_class_itself_and_finds_an_intermediate_override(env: Env) -> None:
    assert env.targets(f"{LEAF}#Leaf.save", "save") == {f"{MID}#Mid.save"}  # 途中の基底の上書き
    assert env.targets(f"{LEAF}#Leaf.load", "load") == {f"{BASE}#Base.load"}  # Mid は load を持たない


async def test_super_follows_c3_in_a_diamond(env: Env) -> None:
    assert env.targets(f"{DIAMOND}#B.hello", "hello") == {f"{DIAMOND}#A.hello"}  # B の線形化は B, A
    assert env.targets(f"{DIAMOND}#D.hello", "hello") == {f"{DIAMOND}#B.hello"}
    assert env.targets(f"{DIAMOND}#D.greet", "greet") == {f"{DIAMOND}#C.greet"}  # 深さ優先なら A.greet
    assert env.targets(f"{DIAMOND_USE}#E.greet", "greet") == {f"{DIAMOND}#C.greet"}  # 基底が別ファイルの菱形


async def test_external_base_before_the_definition_leaves_super_unattributed(env: Env) -> None:
    """``Worker(threading.Thread, Base)`` の ``super().save()`` は結ばない（名前でも結ばない）。``Plain(Base, Thread)`` は結ぶ。"""
    assert env.targets(f"{MIXED}#Worker.save", "save") == set()
    assert env.targets(f"{MIXED}#Plain.save", "save") == {f"{BASE}#Base.save"}
    assert env.targets(f"{MIXED}#Runner.run", "run") == set()
    assert env.name_calls(f"{MIXED}#Worker.save") == []
    assert env.rows(
        "SELECT name, via_super, resolved_urn FROM _member_refs WHERE source_urn = ?",
        f"lk://{WS_ID}/{MIXED}#Worker.save",
    ) == [("save", 1, None)]
    with env.readers.reader() as conn:
        blocked = blocked_member_refs(conn, WS_ID, f"lk://{WS_ID}/{BASE}#Base.save")
        assert [(b.source_urn.removeprefix(f"lk://{WS_ID}/"), b.blocker, b.reason, b.via_super) for b in blocked] == [
            (f"{MIXED}#Worker.save", "lk://_ext/threading#Thread", "external_base", True)
        ]
        assert blocked[0].line == line_of(env, MIXED, "return super().save()")
        # Runner.run はワークスペースのどのメソッドにも届かないので、どの対象にも書かない
        assert blocked_member_refs(conn, WS_ID, f"lk://{WS_ID}/{BASE}#Base.load") == []


async def test_typescript_super_member_call(env: Env) -> None:
    assert env.targets("web/sub.ts#Sub.save", "save") == {"web/base.ts#Base.save"}
    lines = {
        int(line)
        for (line,) in env.rows(
            "SELECT call_site_line FROM relations WHERE source_urn = ? AND target_urn = ?",
            f"lk://{WS_ID}/web/sub.ts#Sub.save",
            f"lk://{WS_ID}/web/base.ts#Base.save",
        )
    }
    assert lines == {line_of(env, "web/sub.ts", "const later"), line_of(env, "web/sub.ts", "return super.save()")}
    assert env.name_calls("web/sub.ts#Sub.save") == []


async def test_super_rebinds_when_the_base_changes(env: Env) -> None:
    """``Mid`` から ``save`` を消すと ``Leaf.save`` の ``super().save()`` は ``Base.save`` に、戻すと ``Mid.save`` に結び直る。"""
    original = env.text(MID)
    env.write(MID, original.replace("    def save(self):\n        return 10\n", "    pass\n"))
    await env.indexer.index_file(MID)
    assert env.targets(f"{LEAF}#Leaf.save", "save") == {f"{BASE}#Base.save"}
    env.write(MID, original)
    await env.indexer.index_file(MID)
    assert env.targets(f"{LEAF}#Leaf.save", "save") == {f"{MID}#Mid.save"}


async def test_super_rebinds_when_the_override_in_the_class_itself_changes(env: Env) -> None:
    """``Leaf`` を取り込み直しても ``super()`` は ``Leaf`` 自身を探さない（自分自身への呼び出しにしない）。"""
    await env.indexer.index_file(LEAF)
    assert env.targets(f"{LEAF}#Leaf.save", "save") == {f"{MID}#Mid.save"}
    assert env.rows(
        "SELECT count(*) FROM relations WHERE source_urn = target_urn AND relation_kind = 'CALLS'"
    ) == [(0,)]


# --- 属性を辿った受け手（BUG-0079） ---------------------------------------------------------------


async def test_attribute_chain_with_unknown_type_is_not_bound_to_the_enclosing_class(env: Env) -> None:
    """``other.raw.copy_with()``（``raw`` の型は分からない）は ``Url.copy_with`` に結ばない。名前だけの呼び出しに残す。"""
    raw_line = line_of(env, CHAIN, "other.raw.copy_with()")
    assert env.rows(
        "SELECT target_urn FROM relations WHERE source_urn = ? AND call_site_line = ? AND relation_kind = 'CALLS'",
        f"lk://{WS_ID}/{CHAIN}#Url.__init__",
        raw_line,
    ) == []
    assert ("copy_with", raw_line) in env.name_calls(f"{CHAIN}#Url.__init__")
    assert f"{CHAIN}#Url.copy_with" not in env.targets(f"{CHAIN}#Url.__init__")


async def test_attribute_chain_with_an_annotated_attribute_or_property_binds_to_its_type(env: Env) -> None:
    calls = {
        int(line): t.removeprefix(f"lk://{WS_ID}/")
        for t, line in env.rows(
            "SELECT target_urn, call_site_line FROM relations WHERE source_urn = ? AND relation_kind = 'CALLS'"
            " AND target_urn LIKE '%.copy_with'",
            f"lk://{WS_ID}/{CHAIN}#Url.__init__",
        )
    }
    assert calls == {
        line_of(env, CHAIN, "other.parsed.copy_with()"): f"{CHAIN}#Parsed.copy_with",  # parsed: Parsed
        line_of(env, CHAIN, "self.view.copy_with()"): f"{CHAIN}#Parsed.copy_with",  # @property -> Parsed
    }


# --- 名前の受け手（BUG-0085） ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "fn_untyped", "fn_annotated", "fn_built", "fn_module", "mod"),
    [
        ("pkg/names.py", "untyped", "annotated", "built", "module_function", "pkg/names_mod.py"),
        ("web/names.ts", "untyped", "annotated", "built", "moduleFunction", "web/names_mod.ts"),
    ],
)
async def test_untyped_name_receiver_goes_to_name_calls_and_known_types_bind(
    env: Env, path: str, fn_untyped: str, fn_annotated: str, fn_built: str, fn_module: str, mod: str
) -> None:
    """``x.persist()`` は、``x`` が注釈の無い引数なら同じファイルの唯一の ``Ledger.persist`` に結ばず ``_name_calls`` に入れる。
    ``x: Ledger``・``x = Ledger()`` / ``new Ledger()`` はその型のメンバー、モジュールの名前の ``persist()`` はモジュールの関数。"""
    untyped = f"{path}#{fn_untyped}"
    assert env.targets(untyped) == set()
    assert env.name_calls(untyped) == [("persist", line_of(env, path, "return x.persist()"))]
    for fn in (fn_annotated, fn_built):
        assert env.targets(f"{path}#{fn}", "persist") == {f"{path}#Ledger.persist"}, fn
        assert env.name_calls(f"{path}#{fn}") == []
    assert env.targets(f"{path}#{fn_module}") == {f"{mod}#persist"}
    assert env.name_calls(f"{path}#{fn_module}") == []


# --- スキーマ v7 ----------------------------------------------------------------------------------


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


def test_migration_6_to_7_adds_via_super_and_reingests_code_files(tmp_path: Path) -> None:
    """v6 の DB は ``_member_refs.via_super`` を足し（既存の行は 0）、関係を抽出する言語のファイルの ``sha256`` を空にする
    （``super()`` の呼び出しと、属性を辿った受け手の誤った行を次の差分走査で取り込み直す）。"""
    conn = open_writer(tmp_path / "paad.sqlite")
    try:
        ensure_schema(conn)
        _tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS_ID, "root_path": "/ws"}))
        for path, language in (("a.py", "python"), ("b.ts", "typescript"), ("R.md", "markdown")):
            _tx(conn, lambda c, p=path, lang=language: ingest_file(c, _facts(p, lang)))
        conn.execute("ALTER TABLE _member_refs DROP COLUMN via_super")
        conn.execute(
            "INSERT INTO _member_refs (file_id, source_urn, class_urn, name, relation_kind, call_site_line,"
            " call_site_col, confidence_level) VALUES ((SELECT file_id FROM files WHERE path = 'a.py'),"
            " 'lk://rc/a.py#f', 'lk://rc/a.py#C', 'm', 'CALLS', 1, 1, 'structural')"
        )
        conn.execute("UPDATE _meta SET schema_version = 6")
        assert ensure_schema(conn) is SchemaAction.MIGRATED
        assert conn.execute("SELECT schema_version FROM _meta").fetchone()[0] == current_schema_version() >= 7
        assert conn.execute("SELECT name, via_super FROM _member_refs").fetchall() == [("m", 0)]
        assert dict(conn.execute("SELECT path, sha256 FROM files").fetchall()) == {
            "a.py": "",
            "b.ts": "",
            "R.md": "a" * 64,
        }
    finally:
        conn.close()


def test_migration_7_to_8_reingests_code_files(tmp_path: Path) -> None:
    """v7 の DB は表を変えず、関係を抽出する言語のファイルの ``sha256`` を空にする（型の分からない名前の受け手 ``x.save()`` を同じ
    ファイルの唯一の ``.save`` に結んだ誤った行を、次の差分走査で取り込み直して消す。BUG-0085）。"""
    conn = open_writer(tmp_path / "paad.sqlite")
    try:
        ensure_schema(conn)
        _tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS_ID, "root_path": "/ws"}))
        for path, language in (("a.py", "python"), ("b.tsx", "tsx"), ("c.js", "javascript"), ("R.md", "markdown")):
            _tx(conn, lambda c, p=path, lang=language: ingest_file(c, _facts(p, lang)))
        conn.execute("UPDATE _meta SET schema_version = 7")
        assert ensure_schema(conn) is SchemaAction.MIGRATED
        assert conn.execute("SELECT schema_version FROM _meta").fetchone()[0] == current_schema_version() >= 8
        assert dict(conn.execute("SELECT path, sha256 FROM files").fetchall()) == {
            "a.py": "",
            "b.tsx": "",
            "c.js": "",
            "R.md": "a" * 64,
        }
    finally:
        conn.close()
