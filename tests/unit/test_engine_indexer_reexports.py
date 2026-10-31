"""再び公開した名前を定義元までたどる（BUG-0100）・名前空間パッケージを外部にしない（BUG-0101）。

フィクスチャ ``tests/fixtures/reexports``（README に各箇所の意図）を ``tmp_path`` に写して ``Indexer`` で取り込み、``relations`` と
内部表（``_reexports``・``_reexport_deps``・``_name_calls``・``_var_types``）を SQL で見る。

* ``from pkg import flush`` の ``flush()``（``pkg/__init__.py`` が ``from .helpers import flush``）は ``pkg/helpers.py#flush`` への
  ``structural`` の呼び出し。2 段の連鎖・別名・絶対 import・普通のモジュールの再公開・``from x import *``（``__all__`` の有無）・外部の
  再公開・循環・再び公開したクラスのメンバー・基底・変数の型も同じ。
* 連鎖の途中のファイルを編集すると、import している側が結び直る（先を変える・消す・足す）。
* 名前空間パッケージ（``__init__.py`` の無いディレクトリ）の ``nsp``・``nsp.tools`` の読みは参照を作らず、外部 ``lk://_ext/nsp`` にしない。
* TS / JS のバレル（``web/lib/index.ts`` の ``export { flush } from './helpers'``・``export *``・``export * as ns``・別名・ディレクトリの
  ``index.ts``）を経由した呼び出し・参照・型の使用は定義元へ ``structural``（BUG-0104）。バレルの編集で import している側が結び直る。
* import する側の ``from x import *``（``star_use.py``）が持ち込んだ名前は ``*`` の先が公開する定義へ。``__all__`` がリテラルでない先は
  ``heuristic``（BUG-0105）。``*`` の先の編集で結び直る。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.model import Confidence, RelPath, Tier
from lucianke.core.urn import CodeSel
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
    作れない。名前空間パッケージの読みも取り込み直しで外部でなくなる）。他の言語は取り込み直さない（TS / JS は続く v11 の移行が
    取り込み直させる。BUG-0104）。"""
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
            "b.ts": "",  # v11（BUG-0104）
            "R.md": "a" * 64,
        }
    finally:
        conn.close()


def test_migration_10_to_11_reingests_ts_js_and_python_star_or_dynamic_all_files(tmp_path: Path) -> None:
    """v10 の DB は、TS / JS のファイル（バレルの公開する名前。BUG-0104）・``from x import *`` を持つ Python のファイル（import する側の
    ``*`` の仮の目標。BUG-0105）・``__all__`` を書くがリテラルの名前の無い Python のファイル（``dynamic_all`` の印）の ``sha256`` を空に
    する。ほかの Python のファイル（リテラルの ``__all__``・``__all__`` も ``*`` も無い）と他の言語は取り込み直さない。"""
    from lucianke.core.model import Kind, SymbolRecord
    from lucianke.core.urn import Urn

    conn = open_writer(tmp_path / "paad.sqlite")
    try:
        ensure_schema(conn)
        _tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS_ID, "root_path": "/ws"}))
        files = (
            ("plain.py", "python"),
            ("star.py", "python"),
            ("dyn.py", "python"),
            ("listed.py", "python"),
            ("b.ts", "typescript"),
            ("c.tsx", "tsx"),
            ("d.js", "javascript"),
            ("R.md", "markdown"),
        )
        for path, language in files:
            _tx(conn, lambda c, p=path, lang=language: ingest_file(c, _facts(p, lang)))

        def all_symbol(path: str) -> SymbolRecord:
            return SymbolRecord(
                urn=Urn(WS_ID, path, CodeSel("__all__")),
                path=RelPath(path),
                name="__all__",
                qualified_name="__all__",
                kind=Kind.VARIABLE,
                range=_line_range(),
                confidence=Confidence.STRUCTURAL,
            )

        for path in ("dyn.py", "listed.py"):
            facts = _facts(path, "python")
            _tx(conn, lambda c, f=facts, p=path: ingest_file(c, replace(f, symbols=(all_symbol(p),))))
        fid = dict(conn.execute("SELECT path, file_id FROM files").fetchall())
        conn.execute(
            "INSERT INTO _reexports (file_id, name, target_urn, kind, ord) VALUES (?, '*', ?, 'star', 0)",
            (fid["star.py"], P + "plain.py"),
        )
        conn.execute(
            "INSERT INTO _reexports (file_id, name, target_urn, kind, ord) VALUES (?, 'x', '', 'all', 0)",
            (fid["listed.py"],),
        )
        conn.execute("UPDATE _meta SET schema_version = 10")
        assert ensure_schema(conn) is SchemaAction.MIGRATED
        assert conn.execute("SELECT schema_version FROM _meta").fetchone()[0] == current_schema_version() >= 11
        assert dict(conn.execute("SELECT path, sha256 FROM files").fetchall()) == {
            "plain.py": "a" * 64,
            "star.py": "",
            "dyn.py": "",
            "listed.py": "a" * 64,
            "b.ts": "",
            "c.tsx": "",
            "d.js": "",
            "R.md": "a" * 64,
        }
    finally:
        conn.close()


def _line_range() -> Any:
    from lucianke.core.model import Position, Range

    return Range(Position(0, 0), Position(0, 10))


# --- BUG-0104: TS / JS のバレル --------------------------------------------------------------------


def W(needle: str) -> int:  # noqa: N802 - 読みやすさのため
    return line_of("web/use.ts", needle)


async def test_ts_barrel_calls_bind_to_the_defining_functions(env: Env) -> None:
    """``import { flush } from './lib'``（``web/lib/index.ts`` の ``export { flush } from './helpers'``）の ``flush()`` は定義元
    ``web/lib/helpers.ts#flush`` への ``structural`` の呼び出し。別名（``export { flush as fl }``）・``export * from './math'``・
    ``export * as deep`` の ``deep.deepGo()``・2 段（``index.ts`` → ``deep/index.ts`` の ``export *`` → ``deep/impl.ts``）・ディレクトリの
    ``index.ts``（``from './lib/deep'``）・名前空間 import ``lib.flush()``・クラスの構築とメソッド・3 段（``export * from './local'`` →
    ``local.ts`` の ``export { add as plus }``〈import した名前〉→ ``math.ts``）・同じファイルの定義の別名・既定の export の明示の再公開も同じ。"""
    run = "web/use.ts#run"
    assert env.calls_at(run, W("  flush();")) == {"web/lib/helpers.ts#flush"}
    assert env.calls_at(run, W("fl();")) == {"web/lib/helpers.ts#flush"}
    assert env.calls_at(run, W("add(1, 2);")) == {"web/lib/math.ts#add"}
    assert env.calls_at(run, W("deep.deepGo();")) == {"web/lib/deep/impl.ts#deepGo"}
    assert env.calls_at(run, W("  deepGo();")) == {"web/lib/deep/impl.ts#deepGo"}
    assert env.calls_at(run, W("viaDir();")) == {"web/lib/deep/impl.ts#deepGo"}
    assert env.calls_at(run, W("new Calc().run();")) == {"web/lib/math.ts#Calc", "web/lib/math.ts#Calc.run"}
    assert env.calls_at(run, W("lib.flush();")) == {"web/lib/helpers.ts#flush"}
    assert env.calls_at(run, W("plus(1, 2);")) == {"web/lib/math.ts#add"}
    assert env.calls_at(run, W("ownAlias();")) == {"web/lib/local.ts#own"}
    assert env.calls_at(run, W("mainFn();")) == {"web/lib/helpers.ts#main"}
    assert all(conf == "structural" for *_, conf in env.rels(run, ("CALLS", "REFERENCES", "TYPE_USES")))
    assert ("web/lib/math.ts#Options", "TYPE_USES", W("run(o: Options)"), "structural") in env.rels(run, ("TYPE_USES",))
    assert env.name_rows(run) == []
    imports = env.rels("web/use.ts", ("IMPORTS",))
    assert {
        ("web/lib/helpers.ts#flush", "IMPORTS", 1, "structural"),
        ("web/lib/math.ts#add", "IMPORTS", 1, "structural"),
        ("web/lib/deep/impl.ts#deepGo", "IMPORTS", 1, "structural"),
        ("web/lib/math.ts#Options", "IMPORTS", 2, "structural"),
        ("web/lib/deep/impl.ts#deepGo", "IMPORTS", 4, "structural"),
    } <= imports
    assert not any(t.startswith(("web/lib/index.ts#", "web/lib/local.ts#plus")) for t, *_ in env.rels("web/use.ts", ("IMPORTS",)))


async def test_ts_barrel_star_skips_default_unexported_and_cycles(env: Env) -> None:
    """``export *`` は既定の export（``mathDefault``）と export していない関数（``secret``）を公開しない。``cyc_a.ts`` と ``cyc_b.ts`` が
    互いに ``loop`` を再公開する循環は止まる。どれも行を作らない（無い先に結ばない）。"""
    run = "web/use.ts#run"
    for needle in ("mathDefault();", "secret();", "loop();"):
        assert env.calls_at(run, W(needle)) == set(), needle
    targets = {t for t, *_ in env.rels("web/use.ts", ("IMPORTS", "CALLS", "REFERENCES"))} | {t for t, *_ in env.rels(run)}
    assert not any(t.endswith(("#mathDefault", "#secret", "#loop")) or "cyc_" in t for t in targets)


async def test_ts_reexports_table_records_module_exports(env: Env) -> None:
    """``_reexports`` の TS の行: 別の定義を公開する名前の束ね先（``import``）・``export *`` の先（``star``）・公開する名前（``export``。
    既定の export は入れない）。既定の export は ``default`` をそのローカルの定義に束ねる。"""

    def of(path: str) -> set[tuple[str, str, str]]:
        return {
            (str(k), str(n), str(t).removeprefix(P))
            for k, n, t in env.rows(
                "SELECT r.kind, r.name, r.target_urn FROM _reexports r JOIN files f ON f.file_id = r.file_id WHERE f.path = ?",
                path,
            )
        }

    assert of("web/lib/index.ts") == {
        ("import", "flush", "web/lib/helpers.ts#flush"),
        ("import", "fl", "web/lib/helpers.ts#flush"),
        ("import", "mainFn", "web/lib/helpers.ts#default"),
        ("star", "*", "web/lib/math.ts"),
        ("import", "deep", "web/lib/deep/index.ts"),
        ("import", "deepGo", "web/lib/deep/index.ts#deepGo"),
        ("import", "loop", "web/lib/cyc_a.ts#loop"),
        ("star", "*", "web/lib/local.ts"),
        *{("export", n, "") for n in ("flush", "fl", "mainFn", "deep", "deepGo", "loop")},
    }
    assert of("web/lib/local.ts") == {
        ("import", "plus", "web/lib/math.ts#add"),
        ("import", "ownAlias", "web/lib/local.ts#own"),
        ("export", "plus", ""),
        ("export", "ownAlias", ""),
    }
    assert of("web/lib/helpers.ts") == {("export", "flush", ""), ("import", "default", "web/lib/helpers.ts#main")}
    assert of("web/lib/math.ts") == {
        *{("export", n, "") for n in ("add", "Options", "Calc")},
        ("import", "default", "web/lib/math.ts#mathDefault"),
    }


async def test_ts_retargeting_a_barrel_export_rebinds_importers(env: Env) -> None:
    """バレルの ``export { flush } from './helpers'`` を ``'./helpers2'`` に変えると ``flush()`` は ``helpers2.ts#flush`` に結び直る
    （別名 ``fl`` は変わらない）。戻せば戻る。"""
    original = env.text("web/lib/index.ts")
    env.write("web/lib/index.ts", original.replace("export { flush } from './helpers';", "export { flush } from './helpers2';"))
    await env.indexer.index_file("web/lib/index.ts")
    assert env.calls_at("web/use.ts#run", W("  flush();")) == {"web/lib/helpers2.ts#flush"}
    assert env.calls_at("web/use.ts#run", W("lib.flush();")) == {"web/lib/helpers2.ts#flush"}
    assert env.calls_at("web/use.ts#run", W("fl();")) == {"web/lib/helpers.ts#flush"}
    env.write("web/lib/index.ts", original)
    await env.indexer.index_file("web/lib/index.ts")
    assert env.calls_at("web/use.ts#run", W("  flush();")) == {"web/lib/helpers.ts#flush"}


async def test_ts_removing_and_restoring_export_star(env: Env) -> None:
    """バレルから ``export * from './math'`` を消すと ``add()``・``new Calc()`` の行は消え（``plus()`` は ``local.ts`` が ``math.ts`` を
    直接 import しているので残る）、戻すと結び直る。"""
    original = env.text("web/lib/index.ts")
    env.write("web/lib/index.ts", original.replace("export * from './math';\n", ""))
    await env.indexer.index_file("web/lib/index.ts")
    assert env.calls_at("web/use.ts#run", W("add(1, 2);")) == set()
    assert env.calls_at("web/use.ts#run", W("new Calc().run();")) == set()
    assert env.calls_at("web/use.ts#run", W("plus(1, 2);")) == {"web/lib/math.ts#add"}
    env.write("web/lib/index.ts", original)
    await env.indexer.index_file("web/lib/index.ts")
    assert env.calls_at("web/use.ts#run", W("add(1, 2);")) == {"web/lib/math.ts#add"}
    assert env.calls_at("web/use.ts#run", W("new Calc().run();")) == {"web/lib/math.ts#Calc", "web/lib/math.ts#Calc.run"}


async def test_ts_unexporting_and_deleting_at_the_end_of_the_chain(env: Env) -> None:
    """連鎖の終わりの ``math.ts`` で ``add`` の ``export`` を外すと（シンボルは残る）``export *`` は公開しなくなり行が消える。
    ``deep/impl.ts`` から ``deepGo`` を消すと 3 つの書き方の行が消え、戻せば結び直る（``use.ts`` を直接 import していなくても）。"""
    math = env.text("web/lib/math.ts")
    env.write("web/lib/math.ts", math.replace("export function add(", "function add("))
    await env.indexer.index_file("web/lib/math.ts")
    assert env.calls_at("web/use.ts#run", W("add(1, 2);")) == set()
    env.write("web/lib/math.ts", math)
    await env.indexer.index_file("web/lib/math.ts")
    assert env.calls_at("web/use.ts#run", W("add(1, 2);")) == {"web/lib/math.ts#add"}
    impl = env.text("web/lib/deep/impl.ts")
    env.write("web/lib/deep/impl.ts", "export function other(): number {\n  return 1;\n}\n")
    await env.indexer.index_file("web/lib/deep/impl.ts")
    for needle in ("deep.deepGo();", "  deepGo();", "viaDir();"):
        assert env.calls_at("web/use.ts#run", W(needle)) == set(), needle
    env.write("web/lib/deep/impl.ts", impl)
    await env.indexer.index_file("web/lib/deep/impl.ts")
    for needle in ("deep.deepGo();", "  deepGo();", "viaDir();"):
        assert env.calls_at("web/use.ts#run", W(needle)) == {"web/lib/deep/impl.ts#deepGo"}, needle


async def test_ts_body_only_barrel_edit_does_not_reingest_importers(env: Env) -> None:
    """公開する名前が変わらない編集（コメント・関数の本体）では、バレルをたどった側を取り込み直さない。"""
    before = env.indexer.reingested_importers
    env.write("web/lib/index.ts", env.text("web/lib/index.ts") + "// comment\n")
    await env.indexer.index_file("web/lib/index.ts")
    env.write("web/lib/helpers.ts", env.text("web/lib/helpers.ts").replace("return 0;", "return 10;"))
    await env.indexer.index_file("web/lib/helpers.ts")
    assert env.indexer.reingested_importers == before
    assert env.calls_at("web/use.ts#run", W("  flush();")) == {"web/lib/helpers.ts#flush"}


# --- BUG-0105: import する側の from x import * ------------------------------------------------------


def S(needle: str) -> int:  # noqa: N802 - 読みやすさのため
    return line_of("star_use.py", needle)


async def test_importing_side_star_binds_the_provided_names(env: Env) -> None:
    """``star_use.py`` の ``from pkg import *``（リテラルの ``__all__``。``flush`` は ``pkg/__init__.py`` がさらに再び公開する）・
    ``from pkg.stars import *``（``__all__``）・``from pkg.loose import *``（``__all__`` なし）・``from pkg.widgets import *`` が持ち込んだ
    名前の呼び出し・読み・構築・メソッド・変数の型・基底は定義元へ ``structural``。組み込み名は今までどおり。"""
    go = "star_use.py#go"
    assert env.calls_at(go, S("    flush()")) == {"pkg/helpers.py#flush"}
    assert env.calls_at(go, S("deep_go()")) == {"pkg/sub/impl.py#deep_go"}
    assert env.calls_at(go, S("starred()")) == {"pkg/stars.py#starred"}
    assert env.calls_at(go, S("public()")) == {"pkg/loose.py#public"}
    assert env.calls_at(go, S("Widget().spin()")) == {"pkg/widgets.py#Widget", "pkg/widgets.py#Widget.spin"}
    assert env.calls_at(go, S("ITEM.spin()")) == {"pkg/widgets.py#Widget.spin"}
    assert env.calls_at(go, S('TokenEstimator().count("z")')) == {"pkg/est.py#TokenEstimator", "pkg/est.py#TokenEstimator.count"}
    assert env.calls_at(go, S('len("x")')) == {"lk://_ext/builtins#len"}
    assert ("pkg/helpers.py#flush", "REFERENCES", S("return flush"), "structural") in env.rels(go)
    assert env.calls_at("star_use.py", S("ITEM = Widget()")) == {"pkg/widgets.py#Widget"}
    assert env.rels("star_use.py#Mine", ("INHERITS",)) == {("pkg/widgets.py#Widget", "INHERITS", S("class Mine"), "structural")}
    assert env.rows("SELECT type_urn FROM _var_types WHERE var_urn = ?", P + "star_use.py#ITEM") == [
        (P + "pkg/widgets.py#Widget",)
    ]
    assert all(conf == "structural" for t, _, ln, conf in env.rels(go) if ln != S("dyn_fn()"))


async def test_importing_side_star_does_not_bind_names_no_source_provides(env: Env) -> None:
    """``hidden``（``pkg/stars.py`` の ``__all__`` に無く、``pkg`` の ``__all__`` にも無い）・``_private``・どの ``*`` の先にも無い
    ``fresh`` は行を作らない（仮の目標 ``star_use.py#…`` も残さない）。型の分からない受け手 ``nowhere.attr()`` は今までどおり名前だけ。"""
    go = "star_use.py#go"
    for needle in ("hidden()", "_private()", "fresh()"):
        assert env.calls_at(go, S(needle)) == set(), needle
    assert not any(
        t.startswith("star_use.py#") and t not in ("star_use.py#ITEM", "star_use.py#go", "star_use.py#Mine")
        for t, *_ in env.rels("star_use.py", ("IMPORTS", "CALLS", "REFERENCES", "INHERITS", "TYPE_USES")) | env.rels(go)
    )
    assert env.name_rows(go) == [("attr", S("nowhere.attr()"), "call")]


async def test_dynamic_all_star_source_binds_by_name_as_heuristic(env: Env) -> None:
    """``pkg/dyn.py`` の ``__all__ = list(_EXPORTED)``（リテラルでない）: 公開する名前が決まらないので、公開名の規則で見つけた ``dyn_fn``
    を ``heuristic`` で結ぶ（名前だけで決めた。``_reexports`` に ``dynamic_all`` の印）。リテラルにすると ``structural`` になる。"""
    assert env.rels("star_use.py#go") >= {("pkg/dyn.py#dyn_fn", "CALLS", S("dyn_fn()"), "heuristic")}
    assert env.rows(
        "SELECT r.kind, r.name FROM _reexports r JOIN files f ON f.file_id = r.file_id WHERE f.path = 'pkg/dyn.py'"
    ) == [("dynamic_all", "*")]
    env.write("pkg/dyn.py", env.text("pkg/dyn.py").replace("__all__ = list(_EXPORTED)", '__all__ = ["dyn_fn"]'))
    await env.indexer.index_file("pkg/dyn.py")
    assert ("pkg/dyn.py#dyn_fn", "CALLS", S("dyn_fn()"), "structural") in env.rels("star_use.py#go")


async def test_star_provider_edits_rebind_importers(env: Env) -> None:
    """``*`` の先の編集で import する側が結び直る: ``pkg/loose.py`` に ``fresh`` を足すと ``fresh()`` が結ばれ、消すと消える。
    ``pkg/stars.py`` の ``__all__`` に ``hidden`` を足すと ``hidden()`` が結ばれる。"""
    loose = env.text("pkg/loose.py")
    env.write("pkg/loose.py", loose + "\n\ndef fresh():\n    return 9\n")
    await env.indexer.index_file("pkg/loose.py")
    assert env.calls_at("star_use.py#go", S("fresh()")) == {"pkg/loose.py#fresh"}
    env.write("pkg/loose.py", loose)
    await env.indexer.index_file("pkg/loose.py")
    assert env.calls_at("star_use.py#go", S("fresh()")) == set()
    stars = env.text("pkg/stars.py")
    env.write("pkg/stars.py", stars.replace('__all__ = ["starred"]', '__all__ = ["starred", "hidden"]'))
    await env.indexer.index_file("pkg/stars.py")
    assert env.calls_at("star_use.py#go", S("hidden()")) == {"pkg/stars.py#hidden"}


# --- BUG-0109: TS / JS の既定の import ------------------------------------------------------------


def D(needle: str) -> int:  # noqa: N802 - 読みやすさのため
    return line_of("web/defaults/use.ts", needle)


DU = "web/defaults/use.ts#useDefaults"


async def test_ts_default_imports_bind_to_the_default_export(env: Env) -> None:
    """``import main from './fn'`` の ``main()`` は既定の export ``export default function main`` へ ``structural``（BUG-0109）。
    ``export default class Widget``（構築・メソッド・型の使用・注釈つきの受け手）・``export default helper``（同じファイルの定義）・
    ``export { viaClause as default }``・``import main, { other }``（混ぜた import）・バレルの ``export { default } from``・
    ``export { default as Widget } from``・``import * as fnMod`` の ``fnMod.default()`` も同じ。import 文の行も定義元へ。"""
    assert env.calls_at(DU, D("  main();")) == {"web/defaults/fn.ts#main"}
    assert env.calls_at(DU, D("  other();")) == {"web/defaults/fn.ts#other"}
    assert env.calls_at(DU, D("  new Widget().spin();")) == {"web/defaults/cls.ts#Widget", "web/defaults/cls.ts#Widget.spin"}
    assert env.calls_at(DU, D("w.spin();")) == {"web/defaults/cls.ts#Widget.spin"}
    assert env.calls_at(DU, D("helper();")) == {"web/defaults/ident.ts#helper"}
    assert env.calls_at(DU, D("viaClause();")) == {"web/defaults/clause.ts#viaClause"}
    assert env.calls_at(DU, D("fwd();")) == {"web/defaults/fn.ts#main"}
    assert env.calls_at(DU, D("new FwdWidget().spin();")) == {"web/defaults/cls.ts#Widget", "web/defaults/cls.ts#Widget.spin"}
    assert env.calls_at(DU, D("fnMod.default();")) == {"web/defaults/fn.ts#main"}
    assert ("web/defaults/cls.ts#Widget", "TYPE_USES", D("useDefaults(w: Widget)"), "structural") in env.rels(
        DU, ("TYPE_USES",)
    )
    assert env.rels("web/defaults/use.ts", ("IMPORTS",)) == {
        ("web/defaults/fn.ts#main", "IMPORTS", 1, "structural"),
        ("web/defaults/fn.ts#other", "IMPORTS", 1, "structural"),
        ("web/defaults/cls.ts#Widget", "IMPORTS", 2, "structural"),
        ("web/defaults/ident.ts#helper", "IMPORTS", 3, "structural"),
        ("web/defaults/clause.ts#viaClause", "IMPORTS", 4, "structural"),
        ("web/defaults/anon.ts", "IMPORTS", 5, "structural"),
        ("web/defaults/obj.ts", "IMPORTS", 6, "structural"),
        ("web/defaults/fn.ts#main", "IMPORTS", 7, "structural"),
        ("web/defaults/cls.ts#Widget", "IMPORTS", 7, "structural"),
        ("web/defaults/anon.ts", "IMPORTS", 7, "structural"),
        ("web/defaults/fn.ts", "IMPORTS", 8, "structural"),
    }
    assert all(conf == "structural" for *_, conf in env.rels(DU, ("CALLS", "REFERENCES", "TYPE_USES")))


async def test_ts_anonymous_default_import_goes_to_the_module(env: Env) -> None:
    """無名の既定の export（``export default () => 5``・``export default { level: 1 }``）はシンボルが無いので、import 文の行・呼び出し・
    参照はそのモジュール（ファイルの URN）へ（黙って捨てない。BUG-0109）。バレルが転送した無名の既定の export（``anonFwd()``）も
    終わりのモジュールへ。無名のオブジェクトのメンバー ``cfg.level`` は名前だけ。``#default`` の目標はどこにも残らない。"""
    assert env.calls_at(DU, D("  anon();")) == {"web/defaults/anon.ts"}
    assert env.calls_at(DU, D("anonFwd();")) == {"web/defaults/anon.ts"}
    assert ("web/defaults/obj.ts", "REFERENCES", D("return cfg.level"), "structural") in env.rels(DU)
    assert env.name_rows(DU) == [("level", D("return cfg.level"), "r")]
    targets = {t for t, *_ in env.rels(DU, ("CALLS", "REFERENCES", "TYPE_USES"))}
    targets |= {t for t, *_ in env.rels("web/defaults/use.ts", ("IMPORTS",))}
    assert not any("#default" in t for t in targets)


async def test_ts_default_export_edits_rebind_importers(env: Env) -> None:
    """既定の export を変えると import している側が結び直る（``_reexport_deps``・``export_changes``。BUG-0109）: ``fn.ts`` の既定の
    export を ``other`` にすると ``main()``（とバレル経由の ``fwd()``）は ``fn.ts#other`` に、無名の ``anon.ts`` に名前をつけると
    ``anon()`` はその関数に。戻せば戻る。"""
    fn = env.text("web/defaults/fn.ts")
    env.write(
        "web/defaults/fn.ts",
        fn.replace("export default function main()", "function main()") + "\nexport default other;\n",
    )
    await env.indexer.index_file("web/defaults/fn.ts")
    assert env.calls_at(DU, D("  main();")) == {"web/defaults/fn.ts#other"}
    assert env.calls_at(DU, D("fwd();")) == {"web/defaults/fn.ts#other"}
    env.write("web/defaults/fn.ts", fn)
    await env.indexer.index_file("web/defaults/fn.ts")
    assert env.calls_at(DU, D("  main();")) == {"web/defaults/fn.ts#main"}
    assert env.calls_at(DU, D("fwd();")) == {"web/defaults/fn.ts#main"}
    anon = env.text("web/defaults/anon.ts")
    env.write("web/defaults/anon.ts", "export default function anonNamed(): number {\n  return 5;\n}\n")
    await env.indexer.index_file("web/defaults/anon.ts")
    assert env.calls_at(DU, D("  anon();")) == {"web/defaults/anon.ts#anonNamed"}
    assert env.calls_at(DU, D("anonFwd();")) == {"web/defaults/anon.ts#anonNamed"}
    env.write("web/defaults/anon.ts", anon)
    await env.indexer.index_file("web/defaults/anon.ts")
    assert env.calls_at(DU, D("  anon();")) == {"web/defaults/anon.ts"}


# --- BUG-0108: TS の型の別名を注釈にした受け手 ------------------------------------------------------


def A(needle: str) -> int:  # noqa: N802 - 読みやすさのため
    return line_of("web/aliases/use.ts", needle)


AU = "web/aliases/use.ts#readAll"
RETRY = "web/aliases/base.ts#Base.retry"


def _reads_at(env: Env, source: str, line: int) -> set[str]:
    return {t for t, k, ln, _ in env.rels(source) if k == "REFERENCES" and ln == line and "#" in t and "." in t.split("#")[1]}


async def test_ts_type_alias_receivers_bind_through_the_alias(env: Env) -> None:
    """型の別名を注釈にした受け手のメンバー（BUG-0108）: 交差 ``Base & {...}`` の ``o.retry`` は ``Base.retry`` へ ``structural``。
    別名 = クラス（``g.start()``）・総称の別名（``Box<number>``）・``Partial<Base>``・別名の別名（``Chain``）・別名を継承した
    インターフェース（``d.retry``。``d.own`` はそのインターフェース自身）・``Base | null | undefined``・合併で持つ構成が 1 つ
    （``e.onlyOther``）・同じファイルの別名（``local.ts`` の ``Mine``）も同じ。別名自身の型リテラルのメンバー（``o.extra``・``o.tick()``）は
    シンボルが無いので行も名前だけの行も作らない。合併で持つ構成が 2 つ（``e.shared``）・中身の分からない別名（関数型 ``Fn``・
    添字のシグネチャ ``Bag``）は名前の一致に任せる（``_name_calls``）。"""
    for needle in ("o.retry;", "b.retry;", "m.retry", "l.retry", "c.retry;", "d.retry;"):
        assert _reads_at(env, AU, A(needle)) == {RETRY}, needle
    assert _reads_at(env, AU, A("e.onlyOther")) == {"web/aliases/base.ts#Other.onlyOther"}
    assert _reads_at(env, AU, A("d.own;")) == {"web/aliases/types.ts#Derived.own"}
    assert env.calls_at(AU, A("g.start();")) == {"web/aliases/base.ts#Engine.start"}
    assert _reads_at(env, "web/aliases/local.ts#same", line_of("web/aliases/local.ts", "m.retry")) == {RETRY}
    for needle in ("o.extra.length", "e.shared", "f.length", "f.arity", "bag.anything"):
        assert _reads_at(env, AU, A(needle)) == set(), needle
    assert env.calls_at(AU, A("o.tick();")) == set()
    assert env.name_rows(AU) == [
        ("length", A("o.extra.length"), "r"),  # 受け手 o.extra（別名のメンバーの型）は分からない
        ("shared", A("e.shared"), "r"),
        ("length", A("f.length"), "r"),
        ("arity", A("f.arity"), "r"),
        ("anything", A("bag.anything"), "r"),
    ]
    assert not any(n in ("extra", "tick", "retry") for n, *_ in env.name_rows(AU))
    assert all(conf == "structural" for *_, conf in env.rels(AU, ("CALLS", "REFERENCES", "TYPE_USES")))


async def test_ts_type_alias_table_records_constituents(env: Env) -> None:
    """``_type_aliases``: 別名・交差は ``and``、合併は ``or``（``null`` / ``undefined`` は書かない）、型リテラルのメンバーは ``member``
    （``<別名>.<名前>``）、``Partial<Base>`` は ``Base``、総称の別名の型引数・関数型・添字のシグネチャは ``opaque``。"""
    rows = {
        (str(a).removeprefix(P), str(op), int(o), str(k), str(t).removeprefix(P))
        for a, op, o, k, t in env.rows(
            "SELECT a.alias_urn, a.op, a.ord, a.kind, a.target_urn FROM _type_aliases a JOIN files f"
            " ON f.file_id = a.file_id WHERE f.path IN ('web/aliases/types.ts', 'web/aliases/local.ts')"
        )
    }
    T, B = "web/aliases/types.ts#", "web/aliases/base.ts#"  # noqa: N806
    assert rows == {
        (T + "EngineAlias", "and", 0, "type", B + "Engine"),
        (T + "Options", "and", 0, "type", B + "Base"),
        (T + "Options", "and", 1, "member", T + "Options.extra"),
        (T + "Options", "and", 1, "member", T + "Options.tick"),
        (T + "Box", "and", 0, "type", B + "Base"),
        (T + "Box", "and", 1, "member", T + "Box.value"),
        (T + "Either", "or", 0, "type", B + "Base"),
        (T + "Either", "or", 1, "type", B + "Other"),
        (T + "MaybeBase", "or", 0, "type", B + "Base"),
        (T + "Loose", "and", 0, "type", B + "Base"),
        (T + "Fn", "and", 0, "opaque", ""),
        (T + "Chain", "and", 0, "type", T + "Options"),
        (T + "Bag", "and", 0, "opaque", ""),
        ("web/aliases/local.ts#Mine", "and", 0, "type", B + "Base"),
        ("web/aliases/local.ts#Mine", "and", 1, "member", "web/aliases/local.ts#Mine.tag"),
    }


async def test_ts_type_alias_edits_rebind_receivers(env: Env) -> None:
    """別名・構成の編集で受け手が結び直る（BUG-0108）: ``Options`` を ``Other & {...}`` にすると ``o.retry``・``c.retry``（別名の別名）・
    ``d.retry``（継承したインターフェース）の行が消え、戻すと戻る。構成の ``Base`` から ``retry`` を消すと別名の受け手の行が消え、
    戻すと戻る（別のファイルの構成）。``Other`` から ``shared`` を消すと合併 ``e.shared`` の持つ構成が 1 つになり、名前だけの行が
    ``Base.shared`` への ``structural`` の行に変わる。"""
    types = env.text("web/aliases/types.ts")
    env.write("web/aliases/types.ts", types.replace("export type Options = Base &", "export type Options = Other &"))
    await env.indexer.index_file("web/aliases/types.ts")
    for needle in ("o.retry;", "c.retry;", "d.retry;"):
        assert _reads_at(env, AU, A(needle)) == set(), needle
    assert _reads_at(env, AU, A("b.retry;")) == {RETRY}
    env.write("web/aliases/types.ts", types)
    await env.indexer.index_file("web/aliases/types.ts")
    for needle in ("o.retry;", "c.retry;", "d.retry;"):
        assert _reads_at(env, AU, A(needle)) == {RETRY}, needle
    base = env.text("web/aliases/base.ts")
    env.write("web/aliases/base.ts", base.replace("  retry: number;\n", ""))
    await env.indexer.index_file("web/aliases/base.ts")
    for needle in ("o.retry;", "b.retry;", "l.retry", "d.retry;"):
        assert _reads_at(env, AU, A(needle)) == set(), needle
    env.write("web/aliases/base.ts", base)
    await env.indexer.index_file("web/aliases/base.ts")
    for needle in ("o.retry;", "b.retry;", "l.retry", "d.retry;"):
        assert _reads_at(env, AU, A(needle)) == {RETRY}, needle
    env.write("web/aliases/base.ts", base.replace("  shared: number;\n", ""))
    await env.indexer.index_file("web/aliases/base.ts")
    assert _reads_at(env, AU, A("e.shared")) == {"web/aliases/base.ts#Base.shared"}
    assert ("shared", A("e.shared"), "r") not in env.name_rows(AU)
    env.write("web/aliases/base.ts", base)
    await env.indexer.index_file("web/aliases/base.ts")
    assert _reads_at(env, AU, A("e.shared")) == set()
    assert ("shared", A("e.shared"), "r") in env.name_rows(AU)


def test_migration_11_to_12_adds_type_aliases_and_reingests_ts_js_files(tmp_path: Path) -> None:
    """v11 の DB は ``_type_aliases`` を作り（BUG-0108）、TS / JS のファイルの ``sha256`` を空にする（既定の import の名前・別名の構成は
    取り込み直さないと作れない。BUG-0109）。Python と他の言語は取り込み直さない。"""
    conn = open_writer(tmp_path / "paad.sqlite")
    try:
        ensure_schema(conn)
        _tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS_ID, "root_path": "/ws"}))
        files = (("a.py", "python"), ("b.ts", "typescript"), ("c.tsx", "tsx"), ("d.js", "javascript"), ("R.md", "markdown"))
        for path, language in files:
            _tx(conn, lambda c, p=path, lang=language: ingest_file(c, _facts(p, lang)))
        conn.execute("DROP TABLE _type_aliases")
        conn.execute("UPDATE _meta SET schema_version = 11")
        assert ensure_schema(conn) is SchemaAction.MIGRATED
        assert conn.execute("SELECT schema_version FROM _meta").fetchone()[0] == current_schema_version() >= 12
        assert conn.execute("SELECT count(*) FROM _type_aliases").fetchone()[0] == 0
        assert dict(conn.execute("SELECT path, sha256 FROM files").fetchall()) == {
            "a.py": "a" * 64,
            "b.ts": "",
            "c.tsx": "",
            "d.js": "",
            "R.md": "a" * 64,
        }
    finally:
        conn.close()
