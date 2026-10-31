"""再び公開した名前を定義元までたどる（BUG-0100）・名前空間パッケージを外部にしない（BUG-0101）。

フィクスチャ ``tests/fixtures/reexports``（README に各箇所の意図）を ``tmp_path`` に写して ``Indexer`` で取り込み、``relations`` と
内部表（``_reexports``・``_reexport_deps``・``_name_calls``・``_var_types``）を SQL で見る。

* ``from pkg import flush`` の ``flush()``（``pkg/__init__.py`` が ``from .helpers import flush``）は ``pkg/helpers.py#flush`` への
  ``structural`` の呼び出し。2 段の連鎖・別名・絶対 import・普通のモジュールの再公開・``from x import *``（``__all__`` の有無）・外部の
  再公開・循環・再び公開したクラスのメンバー・基底・変数の型も同じ。
* 連鎖の途中のファイルを編集すると、import している側が結び直る（先を変える・消す・足す）。
* 名前空間パッケージ（``__init__.py`` の無いディレクトリ）の ``nsp``・``nsp.tools`` の読みは参照を作らず、外部 ``lk://_ext/nsp`` にしない。
"""

from __future__ import annotations

import shutil
import sqlite3
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

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "reexports"
WS_ID = "rx"
P = f"lk://{WS_ID}/"


@dataclass
class Env:
    indexer: Indexer
    readers: ReaderPool
    root: Path

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def text(self, rel: str) -> str:
        return (self.root / rel).read_bytes().decode("utf-8")

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def rels(self, source: str, kinds: tuple[str, ...] = ("CALLS", "REFERENCES")) -> set[tuple[str, str, int, str]]:
        """``source``（``path`` か ``path#sym``）の ``(先, 種別, 行, 水準)``（ワークスペース内は相対表記）。"""
        marks = ",".join("?" * len(kinds))
        return {
            (str(t).removeprefix(P), str(k), int(line), str(conf))
            for t, k, line, conf in self.rows(
                "SELECT target_urn, relation_kind, call_site_line, confidence_level FROM relations"
                f" WHERE source_urn = ? AND relation_kind IN ({marks})",
                P + source,
                *kinds,
            )
        }

    def calls_at(self, source: str, line: int) -> set[str]:
        return {t for t, k, ln, _ in self.rels(source) if k == "CALLS" and ln == line}

    def name_rows(self, source: str) -> list[tuple[str, int, str]]:
        return [
            (str(n), int(line), str(use))
            for n, line, use in self.rows(
                "SELECT name, call_site_line, use_kind FROM _name_calls WHERE source_urn = ?"
                " ORDER BY call_site_line, call_site_col",
                P + source,
            )
        ]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / WS_ID
    shutil.copytree(FIXTURE, root)
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache())
    report = await indexer.scan()
    assert not report.errors, report.errors
    e = Env(indexer, readers, root)
    yield e
    await indexer.aclose()
    readers.close()
    writer.close()


def line_of(rel: str, needle: str) -> int:
    lines = (FIXTURE / rel).read_bytes().decode("utf-8").splitlines()
    return next(i + 1 for i, text in enumerate(lines) if needle in text)


def U(rel: str) -> int:  # noqa: N802 - 読みやすさのため
    return line_of("use.py", rel)


# --- BUG-0100: 再び公開した名前 -----------------------------------------------------------------


async def test_reexported_functions_bind_to_the_defining_file(env: Env) -> None:
    """1 段（``from .helpers import flush``）・2 段（``pkg/__init__`` → ``pkg/sub/__init__`` → ``pkg/sub/impl.py``）・普通のモジュールの
    別名（``from .helpers import flush as fl``）・絶対 import の別名（``from pkg.helpers import flush as absolute``）は定義元の関数への
    ``structural`` の呼び出し。名前だけの呼び出しにはしない。"""
    assert env.calls_at("use.py#run", U("    flush()")) == {"pkg/helpers.py#flush"}
    assert env.calls_at("use.py#run", U("deep_go()")) == {"pkg/sub/impl.py#deep_go"}
    assert env.calls_at("use.py#run", U("fl()")) == {"pkg/helpers.py#flush"}
    assert env.calls_at("use.py#run", U("absolute()")) == {"pkg/helpers.py#flush"}
    assert all(conf == "structural" for *_, conf in env.rels("use.py#run"))
    assert env.name_rows("use.py#run") == []
    # 呼び出しでない読みと import 文の行も定義元へ（直接 import したときと同じ）
    assert ("pkg/helpers.py#flush", "REFERENCES", U("return flush"), "structural") in env.rels("use.py#run")
    imports = env.rels("use.py", ("IMPORTS",))
    assert ("pkg/helpers.py#flush", "IMPORTS", 1, "structural") in imports
    assert ("pkg/sub/impl.py#deep_go", "IMPORTS", 1, "structural") in imports
    assert not any(t.startswith("pkg/__init__.py#") for t, *_ in env.rels("use.py", ("IMPORTS", "CALLS", "REFERENCES")))


async def test_star_reexports_follow_all_and_public_names(env: Env) -> None:
    """``from .stars import *``（``__all__ = ["starred"]``）は ``starred`` だけを公開し、``hidden`` は公開しない。``__all__`` の無い
    ``from .loose import *`` は ``public`` を公開し、``_private`` は公開しない。公開しない名前の行は作らない（無い先に結ばない）。"""
    assert env.calls_at("use.py#run", U("starred()")) == {"pkg/stars.py#starred"}
    assert env.calls_at("use.py#run", U("public()")) == {"pkg/loose.py#public"}
    assert env.calls_at("use.py#run", U("hidden()")) == set()
    assert env.calls_at("use.py#run", U("_private()")) == set()
    assert not any(t.endswith(("#hidden", "#_private")) for t, *_ in env.rels("use.py", ("IMPORTS",)))


async def test_reexport_cycle_is_guarded(env: Env) -> None:
    """``pkg/cyc_a.py`` と ``pkg/cyc_b.py`` が互いに ``loop`` を再び公開する循環は止まり、行を作らない（索引は終わる）。"""
    assert env.calls_at("use.py#run", U("loop()")) == set()
    assert not any("cyc_" in t for t, *_ in env.rels("use.py", ("IMPORTS", "CALLS", "REFERENCES")))


async def test_reexported_class_members_bases_and_variable_types(env: Env) -> None:
    """再び公開したクラス ``TokenEstimator``: 構築・``TokenEstimator().count()``・``EST = TokenEstimator()`` の ``EST.count()``・
    継承したメンバー ``EST.reset()``（``Base.reset``）・``class Sub(TokenEstimator)`` の基底はどれも定義元へ ``structural``。
    外部の再公開 ``Lock()`` は ``lk://_ext/threading#Lock``。``_var_types`` の型も定義元に書き換わる。"""
    assert env.calls_at("use.py#run", U('TokenEstimator().count("x")')) == {
        "pkg/est.py#TokenEstimator",
        "pkg/est.py#TokenEstimator.count",
    }
    assert env.calls_at("use.py#run", U('EST.count("y")')) == {"pkg/est.py#TokenEstimator.count"}
    assert env.calls_at("use.py#run", U("EST.reset()")) == {"pkg/est.py#Base.reset"}
    assert env.calls_at("use.py#run", U("Lock()")) == {"lk://_ext/threading#Lock"}
    assert env.rels("use.py#Sub", ("INHERITS",)) == {("pkg/est.py#TokenEstimator", "INHERITS", U("class Sub"), "structural")}
    assert env.rows("SELECT type_urn FROM _var_types WHERE var_urn = ?", P + "use.py#EST") == [
        (P + "pkg/est.py#TokenEstimator",)
    ]
    assert env.name_rows("use.py#run") == []


async def test_reexports_table_records_bindings_stars_and_all(env: Env) -> None:
    """``_reexports``: モジュール直下の import が束ねた名前（ワークスペース・外部）・``*`` の先・``__all__``。"""
    got = {
        (str(k), str(n), str(t).removeprefix(P))
        for k, n, t in env.rows(
            "SELECT r.kind, r.name, r.target_urn FROM _reexports r JOIN files f ON f.file_id = r.file_id"
            " WHERE f.path = 'pkg/__init__.py'"
        )
    }
    assert got == {
        ("import", "Lock", "lk://_ext/threading#Lock"),
        ("import", "absolute", "pkg/helpers.py#flush"),
        ("import", "loop", "pkg/cyc_a.py#loop"),
        ("import", "TokenEstimator", "pkg/est.py#TokenEstimator"),
        ("import", "flush", "pkg/helpers.py#flush"),
        ("import", "deep_go", "pkg/sub/__init__.py#deep_go"),
        ("star", "*", "pkg/loose.py"),
        ("star", "*", "pkg/stars.py"),
        *{("all", n, "") for n in ("Lock", "TokenEstimator", "absolute", "deep_go", "flush", "loop")},
    }
    deps = {
        (str(path), str(name))
        for path, name in env.rows(
            "SELECT d.path, d.name FROM _reexport_deps d JOIN files f ON f.file_id = d.file_id WHERE f.path = 'use.py'"
        )
    }
    # 連鎖の途中のファイルもすべて覚える（何段先の変化でも 1 回で引ける）
    assert {("pkg/__init__.py", "deep_go"), ("pkg/sub/__init__.py", "deep_go"), ("pkg/alias.py", "fl")} <= deps


async def test_changing_the_reexport_target_rebinds_importers(env: Env) -> None:
    """``pkg/__init__.py`` の ``from .helpers import flush`` を ``from .impl2 import flush`` に変えると、``use.py`` の ``flush()`` は
    ``pkg/impl2.py#flush`` に結び直る（``use.py`` を取り込み直す）。戻せば ``pkg/helpers.py#flush`` に戻る。"""
    original = env.text("pkg/__init__.py")
    env.write("pkg/__init__.py", original.replace("from .helpers import flush\n", "from .impl2 import flush\n"))
    await env.indexer.index_file("pkg/__init__.py")
    assert env.calls_at("use.py#run", U("    flush()")) == {"pkg/impl2.py#flush"}
    assert env.calls_at("use.py#run", U("absolute()")) == {"pkg/helpers.py#flush"}  # 別の名前は変わらない
    assert env.indexer.reingested_importers >= 1
    env.write("pkg/__init__.py", original)
    await env.indexer.index_file("pkg/__init__.py")
    assert env.calls_at("use.py#run", U("    flush()")) == {"pkg/helpers.py#flush"}


async def test_changing_a_middle_hop_rebinds_importers(env: Env) -> None:
    """2 段目の ``pkg/sub/__init__.py`` の ``from .impl import deep_go`` を ``from ..impl2 import deep_go`` に変えると、``pkg/__init__.py``
    を経由する ``use.py`` の ``deep_go()`` も ``pkg/impl2.py#deep_go`` に結び直る（``_reexport_deps`` に途中のファイルがある）。"""
    env.write("pkg/sub/__init__.py", "from ..impl2 import deep_go\n")
    await env.indexer.index_file("pkg/sub/__init__.py")
    assert env.calls_at("use.py#run", U("deep_go()")) == {"pkg/impl2.py#deep_go"}


async def test_removing_and_restoring_the_reexport(env: Env) -> None:
    """再公開を消すと、``flush()`` の行は消え（素の名前は無い先に結ばない）、再び公開したクラスのメンバー ``TokenEstimator().count()`` は
    名前だけの呼び出し（``_name_calls``。BUG-0094 の名前の一致）に戻る。戻せば定義元に結び直る。"""
    original = env.text("pkg/__init__.py")
    edited = original.replace("from .helpers import flush\n", "").replace("from .est import TokenEstimator\n", "")
    env.write("pkg/__init__.py", edited)
    await env.indexer.index_file("pkg/__init__.py")
    assert env.calls_at("use.py#run", U("    flush()")) == set()
    assert env.calls_at("use.py#run", U('TokenEstimator().count("x")')) == set()
    names = env.name_rows("use.py#run")
    assert ("count", U('TokenEstimator().count("x")'), "call") in names
    assert ("count", U('EST.count("y")'), "call") in names
    env.write("pkg/__init__.py", original)
    await env.indexer.index_file("pkg/__init__.py")
    assert env.calls_at("use.py#run", U("    flush()")) == {"pkg/helpers.py#flush"}
    assert env.calls_at("use.py#run", U('EST.count("y")')) == {"pkg/est.py#TokenEstimator.count"}
    assert env.name_rows("use.py#run") == []


async def test_defining_file_symbol_changes_rebind_through_the_chain(env: Env) -> None:
    """連鎖の終わりのファイルからシンボルを消すと行が消え、足し直すと（``use.py`` を直接 import していなくても）結び直る。"""
    original = env.text("pkg/sub/impl.py")
    env.write("pkg/sub/impl.py", "def other():\n    return 1\n")
    await env.indexer.index_file("pkg/sub/impl.py")
    assert env.calls_at("use.py#run", U("deep_go()")) == set()
    env.write("pkg/sub/impl.py", original)
    await env.indexer.index_file("pkg/sub/impl.py")
    assert env.calls_at("use.py#run", U("deep_go()")) == {"pkg/sub/impl.py#deep_go"}


async def test_star_source_all_change_rebinds(env: Env) -> None:
    """``pkg/stars.py`` の ``__all__`` に ``hidden`` を足すと ``hidden()`` が結ばれ、外すと消える。"""
    original = env.text("pkg/stars.py")
    env.write("pkg/stars.py", original.replace('__all__ = ["starred"]', '__all__ = ["starred", "hidden"]'))
    await env.indexer.index_file("pkg/stars.py")
    assert env.calls_at("use.py#run", U("hidden()")) == {"pkg/stars.py#hidden"}
    env.write("pkg/stars.py", original)
    await env.indexer.index_file("pkg/stars.py")
    assert env.calls_at("use.py#run", U("hidden()")) == set()


async def test_body_only_edit_does_not_reingest_importers(env: Env) -> None:
    """公開する名前が変わらない編集（関数の本体だけ）では、連鎖をたどった側を取り込み直さない。"""
    before = env.indexer.reingested_importers
    env.write("pkg/helpers.py", "def flush():\n    return 1\n")
    await env.indexer.index_file("pkg/helpers.py")
    assert env.indexer.reingested_importers == before
    env.write("pkg/__init__.py", env.text("pkg/__init__.py") + "\n# comment\n")
    await env.indexer.index_file("pkg/__init__.py")
    assert env.indexer.reingested_importers == before
    assert env.calls_at("use.py#run", U("    flush()")) == {"pkg/helpers.py#flush"}


# --- BUG-0101: 名前空間パッケージ ---------------------------------------------------------------


async def test_namespace_package_segments_are_not_external(env: Env) -> None:
    """``import nsp.tools.fmt`` の ``nsp``・``nsp.tools`` の読み、``import nsp.tools as nt`` の ``nt``、``from nsp import tools`` の
    ``tools`` は参照を作らず外部にもしない。``nt.fmt.render()``・``tools.fmt.render()`` はワークスペースの関数。import 文の行は
    import したモジュール ``nsp/tools/fmt.py`` へ（ディレクトリだけの import は行を作らない）。"""
    run = env.rels("use_ns.py#run")
    render = {
        line_of("use_ns.py", "nsp.tools.fmt.render()"),
        line_of("use_ns.py", "nt.fmt.render()"),
        line_of("use_ns.py", "    tools.fmt.render()"),
    }
    assert {ln for t, k, ln, _ in run if t == "nsp/tools/fmt.py#render" and k == "CALLS"} == render
    assert {ln for t, k, ln, _ in run if t == "nsp/tools/fmt.py" and k == "REFERENCES"} == render
    ret = line_of("use_ns.py", "return nsp")
    assert not any(ln == ret for *_, ln, _ in run)
    assert not any(t in ("lk://_ext/nsp", "lk://_ext/nsp#tools") or t.startswith("lk://_ext/nsp#tools.") for t, *_ in run)
    assert env.rels("use_ns.py", ("IMPORTS",)) == {
        ("nsp/tools/fmt.py", "IMPORTS", 2, "structural"),
        ("lk://_ext/nsp#other", "IMPORTS", 3, "structural"),  # ``nsp/other`` はワークスペースに無い（別の場所の部分かもしれない）
    }
    assert env.rows("SELECT urn FROM symbols WHERE urn IN ('lk://_ext/nsp', 'lk://_ext/nsp#tools')") == []
    assert env.name_rows("use_ns.py#run") == []


async def test_creating_a_namespace_directory_reresolves_importers(env: Env) -> None:
    """``from nsp import other``（``nsp/other`` が無い）は外部。``nsp/other/x.py`` を作ると ``nsp/other`` は名前空間パッケージになり、
    ``use_ns.py`` を取り込み直して（調べたディレクトリの ``?dir:`` の語幹で引く）外部の ``other`` の行が消える。"""
    other = line_of("use_ns.py", "other.go()")
    assert ("lk://_ext/nsp#other", "REFERENCES", other, "structural") in env.rels("use_ns.py#run")
    env.write("nsp/other/x.py", "def go():\n    return 1\n")
    await env.indexer.index_file("nsp/other/x.py")
    assert ("lk://_ext/nsp#other", "REFERENCES", other, "structural") not in env.rels("use_ns.py#run")
    assert ("lk://_ext/nsp#other", "IMPORTS", 3, "structural") not in env.rels("use_ns.py", ("IMPORTS",))
    # ``nsp.other.go`` はワークスペースに無い（``go`` は ``nsp/other/x.py`` の関数でパッケージの属性ではない）ので外部のまま
    assert ("lk://_ext/nsp#other.go", "CALLS", other, "structural") in env.rels("use_ns.py#run")
    # 最後のファイルとディレクトリを消すと、外部に戻る
    (env.root / "nsp" / "other" / "x.py").unlink()
    (env.root / "nsp" / "other").rmdir()
    await env.indexer.remove("nsp/other/x.py")
    assert ("lk://_ext/nsp#other", "REFERENCES", other, "structural") in env.rels("use_ns.py#run")


# --- スキーマ v10 -----------------------------------------------------------------------------


def _tx(conn: sqlite3.Connection, fn: Any) -> None:
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


def test_migration_9_to_10_adds_the_tables_and_reingests_python_files(tmp_path: Path) -> None:
    """v9 の DB は ``_reexports``・``_reexport_deps`` を作り、Python のファイルの ``sha256`` を空にする（束ねた名前は取り込み直さないと
    作れない。名前空間パッケージの読みも取り込み直しで外部でなくなる）。TS / JS・他の言語は取り込み直さない。"""
    conn = open_writer(tmp_path / "paad.sqlite")
    try:
        ensure_schema(conn)
        _tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS_ID, "root_path": "/ws"}))
        for path, language in (("a.py", "python"), ("b.ts", "typescript"), ("R.md", "markdown")):
            _tx(conn, lambda c, p=path, lang=language: ingest_file(c, _facts(p, lang)))
        conn.execute("DROP TABLE _reexports")
        conn.execute("DROP TABLE _reexport_deps")
        conn.execute("UPDATE _meta SET schema_version = 9")
        assert ensure_schema(conn) is SchemaAction.MIGRATED
        assert conn.execute("SELECT schema_version FROM _meta").fetchone()[0] == current_schema_version() >= 10
        assert conn.execute("SELECT count(*) FROM _reexports").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM _reexport_deps").fetchone()[0] == 0
        assert dict(conn.execute("SELECT path, sha256 FROM files").fetchall()) == {
            "a.py": "",
            "b.ts": "a" * 64,
            "R.md": "a" * 64,
        }
    finally:
        conn.close()
