"""点つきのモジュールのパスの受け手（BUG-0093）・import した変数の受け手（BUG-0094）・型の分からない受け手の属性の読み書き（BUG-0095）。

フィクスチャ ``tests/fixtures/untyped_refs``（README に各箇所の意図）を ``tmp_path`` に写して ``Indexer`` で取り込み、``relations`` と
内部表（``_name_calls``・``_attr_defs``・``_var_types``・``_member_refs``）を SQL で見る。

* ``import pkg.helpers`` の ``pkg.helpers.flush()`` はモジュールの関数 ``pkg/helpers.py#flush`` への ``structural`` の呼び出し。
* ``from pkg.conf import settings`` の ``settings.reload()`` は、定義元の変数の型が構文から分かれば（``settings = Settings()``・
  ``typed: Settings = ...``）その型のメンバーへの ``structural`` の呼び出し。分からなければ名前だけの呼び出し（``_name_calls``）。
  ``pkg/conf.py#settings.reload`` という無い先には結ばない。変数の定義を編集すると、import している側を取り込み直さずに結び直る。
* 型の分からない受け手の属性の読み書き（``mon.stdout_lines += 1``）は ``_name_calls`` に使用種別つきで残る（名前の一致に任せる）。
  インスタンス属性の定義（``self.x = ...``）は ``_attr_defs``。型の分からない受け手は ``structural`` で結ばない（BUG-0085 の後戻りなし）。
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
from lucianke.index.queries import NameCallRow
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.tools._name_calls import NameIndex
from lucianke.workspace.types import Workspace

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "untyped_refs"
WS_ID = "ur"
P = f"lk://{WS_ID}/"


@dataclass
class Env:
    indexer: Indexer
    readers: ReaderPool
    root: Path

    def write(self, rel: str, text: str) -> None:
        (self.root / rel).write_bytes(text.encode("utf-8"))

    def text(self, rel: str) -> str:
        return (self.root / rel).read_bytes().decode("utf-8")

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def rels(self, source: str) -> set[tuple[str, str, int, str]]:
        """``source``（``path#sym``）の CALLS / REFERENCES の ``(先, 種別, 行, 水準)``（ワークスペース内は相対表記）。"""
        return {
            (str(t).removeprefix(P), str(k), int(line), str(conf))
            for t, k, line, conf in self.rows(
                "SELECT target_urn, relation_kind, call_site_line, confidence_level FROM relations"
                " WHERE source_urn = ? AND relation_kind IN ('CALLS', 'REFERENCES')",
                P + source,
            )
        }

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


# --- BUG-0093: 点つきのモジュールのパス ----------------------------------------------------------


async def test_dotted_module_path_calls_bind_to_the_module_function(env: Env) -> None:
    """``pkg.helpers.flush()``・``pkg.sub.deep.go()``・``ps.deep.go()``（``import pkg.sub as ps``）・名前空間パッケージの
    ``nsp.tools.fmt.render()`` はモジュールの関数への ``structural`` の呼び出し。名前だけの呼び出しにはならない。"""
    calls = {(t, line, conf) for t, k, line, conf in env.rels("use_modules.py#run") if k == "CALLS"}
    assert calls == {
        ("pkg/helpers.py#flush", line_of("use_modules.py", "pkg.helpers.flush()"), "structural"),
        ("pkg/sub/deep.py#go", line_of("use_modules.py", "pkg.sub.deep.go()"), "structural"),
        ("pkg/sub/deep.py#go", line_of("use_modules.py", "ps.deep.go()"), "structural"),
        ("nsp/tools/fmt.py#render", line_of("use_modules.py", "nsp.tools.fmt.render()"), "structural"),
    }
    assert env.name_rows("use_modules.py#run") == []
    # ``pkg.helpers`` の ``helpers`` はサブモジュールそのものへの参照（``pkg/__init__.py#helpers`` という無い先ではない）
    refs = {(t, line) for t, k, line, _ in env.rels("use_modules.py#run") if k == "REFERENCES"}
    assert ("pkg/helpers.py", line_of("use_modules.py", "return pkg.helpers")) in refs
    assert ("nsp/tools/fmt.py", line_of("use_modules.py", "nsp.tools.fmt.render()")) in refs
    assert not any(t.startswith("lk://_ext/nsp#tools.fmt") for t, _ in refs)


async def test_module_name_attribute_chain_binds_through_the_module(env: Env) -> None:
    """``from pkg import conf`` の ``conf.settings.reload()`` は変数の型の ``Settings.reload``、``conf.Settings.debug`` はクラスの
    ``Settings.debug``（どちらも ``structural``。モジュールの名前の属性の連鎖）。"""
    got = env.rels("use_modules.py#via_module")
    line = line_of("use_modules.py", "conf.settings.reload()")
    assert ("pkg/conf.py#Settings.reload", "CALLS", line, "structural") in got
    assert ("pkg/conf.py#settings", "REFERENCES", line, "structural") in got
    debug = line_of("use_modules.py", "conf.Settings.debug")
    assert ("pkg/conf.py#Settings.debug", "REFERENCES", debug, "structural") in got
    assert env.name_rows("use_modules.py#via_module") == []


# --- BUG-0094: import した変数の受け手 ----------------------------------------------------------


async def test_imported_object_with_a_known_type_binds_to_the_type_member(env: Env) -> None:
    """``settings = Settings()``・``typed: Settings = make()`` の ``settings.reload()``・``typed.reload()`` は ``Settings.reload``、
    ``settings.debug`` は ``Settings.debug``、外部の型の ``lock = Lock()`` の ``lock.acquire()`` は外部のメンバー（どれも ``structural``）。"""
    got = env.rels("use_conf.py#apply")
    assert ("pkg/conf.py#Settings.reload", "CALLS", line_of("use_conf.py", "settings.reload()"), "structural") in got
    assert ("pkg/conf.py#Settings.reload", "CALLS", line_of("use_conf.py", "typed.reload()"), "structural") in got
    assert ("pkg/conf.py#Settings.debug", "REFERENCES", line_of("use_conf.py", "settings.debug"), "structural") in got
    assert ("lk://_ext/threading#Lock.acquire", "CALLS", line_of("use_conf.py", "lock.acquire()"), "structural") in got
    assert not any(t.startswith("pkg/conf.py#settings.") or t.startswith("pkg/conf.py#typed.") for t, *_ in got)
    assert env.rows("SELECT kind FROM symbols WHERE urn = 'lk://_ext/threading#Lock.acquire'") == [("Method",)]
    assert {
        (str(v).removeprefix(P), str(t).removeprefix(P))
        for v, t in env.rows("SELECT var_urn, type_urn FROM _var_types WHERE var_urn LIKE ?", P + "pkg/conf.py#%")
    } == {
        ("pkg/conf.py#settings", "pkg/conf.py#Settings"),
        ("pkg/conf.py#typed", "pkg/conf.py#Settings"),
        ("pkg/conf.py#lock", "lk://_ext/threading#Lock"),
    }


async def test_imported_object_with_an_unknown_type_goes_to_name_matching(env: Env) -> None:
    """``loose = make()``（型が分からない）の ``loose.reload()`` は ``relations`` に無く、名前だけの呼び出し（``_name_calls``）になる
    （無い先にも結ばず、捨てもしない）。``pkg/__init__.py`` が ``from pkg.conf import settings as shared`` で再び公開した ``shared`` の
    ``shared.reload()`` は、定義元の ``settings``（``Settings()``）までたどって ``Settings.reload`` に ``structural``（BUG-0100）。"""
    loose, shared = line_of("use_conf.py", "loose.reload()"), line_of("use_conf.py", "shared.reload()")
    got = env.rels("use_conf.py#apply")
    assert not any(k == "CALLS" and ln == loose for _, k, ln, _ in got)
    assert ("pkg/conf.py#Settings.reload", "CALLS", shared, "structural") in got
    assert ("pkg/conf.py#settings", "REFERENCES", shared, "structural") in got
    assert env.name_rows("use_conf.py#apply") == [("reload", loose, "call")]


async def test_editing_the_variable_rebinds_the_importer_without_reingesting_it(env: Env) -> None:
    """``loose`` の型が分かるようになれば ``_name_calls`` の行を消して ``Settings.reload`` に結び、分からなくなれば戻す
    （``use_conf.py`` は取り込み直さない）。"""
    line = line_of("use_conf.py", "loose.reload()")
    before = env.indexer.ingest_calls
    original = env.text("pkg/conf.py")
    env.write("pkg/conf.py", original.replace("loose = make()", "loose = Settings()"))
    await env.indexer.index_file("pkg/conf.py")
    assert ("pkg/conf.py#Settings.reload", "CALLS", line, "structural") in env.rels("use_conf.py#apply")
    assert env.name_rows("use_conf.py#apply") == []
    env.write("pkg/conf.py", original)
    await env.indexer.index_file("pkg/conf.py")
    assert not any(k == "CALLS" and ln == line for _, k, ln, _ in env.rels("use_conf.py#apply"))
    assert env.name_rows("use_conf.py#apply") == [("reload", line, "call")]
    assert env.indexer.ingest_calls == before + 2  # 取り込んだのは pkg/conf.py の 2 回だけ
    assert env.indexer.reingested_importers == 0


async def test_typescript_imported_const_binds_to_its_class_member(env: Env) -> None:
    """``export const meter = new Meter()`` を import した ``meter.reset()`` は ``Meter.reset`` への ``structural`` の呼び出し。"""
    line = line_of("web/use.ts", "meter.reset()")
    assert ("web/meter.ts#Meter.reset", "CALLS", line, "structural") in env.rels("web/use.ts#poke")


# --- BUG-0095: 型の分からない受け手の属性の読み書き ----------------------------------------------


async def test_untyped_attribute_reads_and_writes_are_kept_for_name_matching(env: Env) -> None:
    """注釈の無い引数 ``mon`` の属性の読み書きは ``relations`` に入らず、``_name_calls`` に使用種別つきで残る（呼び出しは ``call``）。"""
    assert env.rels("use_attrs.py#tick") == set()
    ln = lambda needle: line_of("use_attrs.py", needle)  # noqa: E731
    assert env.name_rows("use_attrs.py#tick") == [
        ("stdout_lines", ln("mon.stdout_lines += 1"), "rw"),
        ("samples", ln("mon.samples = "), "w"),
        ("samples", ln("mon.samples = "), "r"),
        ("total", ln("if mon.total"), "r"),
        ("violate", ln("mon.violate()"), "call"),
        ("name", ln("return mon.name"), "r"),
    ]
    assert env.name_rows("web/use.ts#poke") == [
        ("hits", line_of("web/use.ts", "m.hits += 1"), "rw"),
        ("hits", line_of("web/use.ts", "return m.hits"), "r"),
    ]


async def test_instance_attribute_definitions_are_recorded_once_per_attribute(env: Env) -> None:
    """``self.x = ...`` はクラスごと・名前ごとに 1 つ。クラスのシンボル（``Monitor.stdout_lines``）・同じファイルの基底の属性
    （``Tally(Counter)`` の ``self.hits``）は入れない。別ファイルの基底（``Wide(Counter)``）は入る（問い合わせのときにまとめる）。"""
    got = {
        (str(c).removeprefix(P), str(n), int(line))
        for c, n, line in env.rows("SELECT class_urn, name, line FROM _attr_defs")
    }
    assert got == {
        ("pkg/monitor.py#Monitor", "samples", line_of("pkg/monitor.py", "self.samples = 0")),
        ("pkg/counter.py#Counter", "total", line_of("pkg/counter.py", "self.total = 0")),
        ("pkg/counter.py#Counter", "hits", line_of("pkg/counter.py", "self.hits = 0")),
        ("pkg/wide.py#Wide", "hits", line_of("pkg/wide.py", "self.hits = 5")),
        ("scripts/rawlib.py#Raw", "lines", line_of("scripts/rawlib.py", "self.lines = 0")),
    }


async def test_name_verdicts_for_attribute_references(env: Env) -> None:
    """名前の判定: ``stdout_lines`` は唯一の ``Monitor.stdout_lines``、``samples`` は唯一のインスタンス属性 ``Monitor.samples``、
    ``total`` は 2 つ（``Monitor.total`` とインスタンス属性 ``Counter.total``）、``hits`` は 2 つ（``Wide`` の ``self.hits`` は基底の
    ``Counter.hits`` と同じ属性。TS の ``Meter.hits`` は言語族が違うので数えない）、``name`` は組み込み型の属性と同名。"""

    def row(name: str, use: str, path: str = "use_attrs.py", language: str = "python") -> NameCallRow:
        from lucianke.core.model import Position

        return NameCallRow(
            source_urn=P + f"{path}#tick",
            name=name,
            path=RelPath(path),
            language=language,
            site=Position(1, 4),
            guard_condition=None,
            is_test=False,
            use_kind=use,
        )

    with env.readers.reader() as conn:
        names = NameIndex(conn, WS_ID)
        v = names.verdict_for(row("stdout_lines", "rw"))
        assert v is not None and v.owner is not None and v.owner.qualified_name == "Monitor.stdout_lines"
        assert v.only_text() == "Monitor.stdout_lines is the only member named 'stdout_lines' in the workspace"
        v = names.verdict_for(row("samples", "r"))
        assert v is not None and v.owner is not None
        assert (v.owner.qualified_name, v.owner.modifiers) == ("Monitor.samples", ("instance",))
        v = names.verdict_for(row("total", "r"))
        assert v is not None and v.owner is None and v.reason_code() == "ambiguous"
        assert [c.qualified_name for c in v.candidates] == ["Counter.total", "Monitor.total"]
        assert "(instance attribute)" in v.reason()
        v = names.verdict_for(row("hits", "rw"))
        assert v is not None and [c.qualified_name for c in v.candidates] == ["Counter.hits", "Gauge.hits"]
        v = names.verdict_for(row("hits", "rw", "web/use.ts", "typescript"))
        assert v is not None and v.owner is not None and v.owner.qualified_name == "Meter.hits"
        v = names.verdict_for(row("name", "r"))
        assert v is not None and v.owner is None and v.reason_code() == "builtin_name"
        # ``Monitor.parent`` だけが同名でも、``p: Path`` の ``p.parent`` が外部の ``pathlib.Path.parent`` を使っているので結ばない
        v = names.verdict_for(row("parent", "r"))
        assert v is not None and v.owner is None and v.reason_code() == "external_name"
        assert [c.qualified_name for c in v.candidates] == ["Monitor.parent"]
        assert v.external == ("lk://_ext/pathlib#Path.parent",)
        assert "external types used in the workspace also have a member of that name (pathlib.Path.parent)" in v.reason()
        # 兄弟のモジュール ``rawlib``（import の解決が外部にした ``lk://_ext/rawlib#Raw.lines``）は外部の型の証拠にしない
        v = names.verdict_for(row("lines", "r", "scripts/run.py"))
        assert v is not None and v.external == () and v.owner is not None
        assert (v.owner.qualified_name, v.owner.modifiers) == ("Raw.lines", ("instance",))
        # 呼び出しの候補はメソッドだけ（BUG-0071 のまま）: ``stdout_lines()`` という呼び出しに候補は無い
        assert names.verdict_for(row("stdout_lines", "call")) is None


async def test_untyped_receiver_is_never_bound_structurally(env: Env) -> None:
    """BUG-0085 の後戻りなし: 注釈の無い引数 ``x`` の ``x.save()``・``x.note`` は、同じファイルに ``Base.save``・``Base.note`` が
    1 つずつしか無くても ``relations`` に入らない（名前だけの呼び出し・参照）。"""
    assert env.rels("untyped_bind.py#f") == set()
    assert env.name_rows("untyped_bind.py#f") == [
        ("save", line_of("untyped_bind.py", "x.save()"), "call"),
        ("note", line_of("untyped_bind.py", "return x.note"), "r"),
    ]


# --- スキーマ v9 --------------------------------------------------------------------------------


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


def test_migration_8_to_9_adds_use_kind_and_the_tables_and_reingests_code_files(tmp_path: Path) -> None:
    """v8 の DB は ``_name_calls.use_kind`` を足し（既存の行は ``call``）、``_attr_defs``・``_var_types`` を作り、関係を抽出する
    言語のファイルの ``sha256`` を空にする（属性の読み書き・点つきのモジュールのパス・import した変数の行は取り込み直さないと作れない）。"""
    conn = open_writer(tmp_path / "paad.sqlite")
    try:
        ensure_schema(conn)
        _tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS_ID, "root_path": "/ws"}))
        for path, language in (("a.py", "python"), ("b.ts", "typescript"), ("R.md", "markdown")):
            _tx(conn, lambda c, p=path, lang=language: ingest_file(c, _facts(p, lang)))
        conn.execute("DROP TABLE _attr_defs")
        conn.execute("DROP TABLE _var_types")
        conn.execute("ALTER TABLE _name_calls DROP COLUMN use_kind")
        conn.execute(
            "INSERT INTO _name_calls (file_id, source_urn, name, call_site_line, call_site_col)"
            " VALUES ((SELECT file_id FROM files WHERE path = 'a.py'), 'lk://ur/a.py#f', 'm', 1, 1)"
        )
        conn.execute("UPDATE _meta SET schema_version = 8")
        assert ensure_schema(conn) is SchemaAction.MIGRATED
        assert conn.execute("SELECT schema_version FROM _meta").fetchone()[0] == current_schema_version() >= 9
        assert conn.execute("SELECT name, use_kind FROM _name_calls").fetchall() == [("m", "call")]
        assert conn.execute("SELECT count(*) FROM _attr_defs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM _var_types").fetchone()[0] == 0
        assert dict(conn.execute("SELECT path, sha256 FROM files").fetchall()) == {
            "a.py": "",
            "b.ts": "",
            "R.md": "a" * 64,
        }
    finally:
        conn.close()
