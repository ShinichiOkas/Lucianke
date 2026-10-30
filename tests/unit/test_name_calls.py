"""型の分からない受け手の属性呼び出し（BUG-0071）の単体テスト: 抽出器の ``name_only``、``_name_calls`` の取り込みと移行。

T-06 / T-07 の応答は ``tests/contract/test_T06_T07_name_calls.py``（フィクスチャ ``tests/fixtures/untyped_calls`` の実索引）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

from lucianke.core.model import Confidence, RelPath, Tier
from lucianke.core.textpos import LineIndex
from lucianke.index import queries
from lucianke.index.connection import open_writer
from lucianke.index.ingest import FileFacts, NameCallFact, ingest_file, remove_file, upsert_workspace
from lucianke.index.migrate import SchemaAction, current_schema_version, ensure_schema
from lucianke.syntax.builtin_names import JS_BUILTIN_METHODS, PYTHON_BUILTIN_METHODS
from lucianke.syntax.relations import RelationCandidate, extract_relations
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import ParsedFile

WS = "ws"
_LANGUAGES: dict[str, Callable[[], object]] = {
    "python": tree_sitter_python.language,
    "typescript": tree_sitter_typescript.language_typescript,
}
# 呼び手（索引器）の import 表の代わり: typing.Any は外部、Runner は別ファイルのクラス
_NAMES = {"Any": "lk://_ext/typing#Any", "Runner": f"lk://{WS}/app/engine.py#Runner"}


def _candidates(source: str, language_id: str = "python", rel: str = "app/mod.py") -> list[RelationCandidate]:
    data = source.encode("utf-8")
    language = Language(_LANGUAGES[language_id]())  # type: ignore[arg-type]
    tree = Parser(language).parse(data)
    parsed = ParsedFile(
        language_id=language_id,
        sha256="0" * 64,
        source=data,
        tree=tree,
        line_index=LineIndex(data),
        has_errors=tree.root_node.has_error,
    )
    symbols = extract(
        parsed,
        language_id=language_id,
        language=language,
        query_source=load_query_source(language_id),
        workspace_id=WS,
        rel=RelPath(rel),
    )
    return extract_relations(
        parsed,
        language_id=language_id,
        language=language,
        symbols=symbols,
        workspace_id=WS,
        rel=RelPath(rel),
        resolve_name=_NAMES.get,
    )


def _call(cands: list[RelationCandidate], name: str) -> RelationCandidate:
    [c] = [c for c in cands if c.target_name == name and c.relation_kind == "CALLS"]
    return c


# --- 抽出器 ---------------------------------------------------------------------------------------


def test_any_receiver_is_unknown_not_an_external_method() -> None:
    """``packer: Any`` の ``packer.pack()`` を ``lk://_ext/typing#Any.pack`` に結ばない（名前でしか結べない呼び出し）。"""
    c = _call(_candidates("from typing import Any\n\ndef f(packer: Any):\n    return packer.pack(1)\n"), "pack")
    assert (c.target_urn, c.name_only) == (None, True)
    opt = _call(_candidates("from typing import Any, Optional\n\ndef f(p: Optional[Any]):\n    p.pack()\n"), "pack")
    assert (opt.target_urn, opt.name_only) == (None, True)


@pytest.mark.parametrize(
    "source",
    [
        "def f(packer):\n    return packer.pack(1)\n",  # 注釈の無い引数
        "def f(make):\n    return make().pack(1)\n",  # 呼び出しの戻り値
        "class H:\n    def __init__(self, inner):\n        self.inner = inner\n\n    def g(self):\n"
        "        return self.inner.pack(1)\n",  # self.x の型が分からない
    ],
)
def test_untyped_receivers_are_name_only(source: str) -> None:
    c = _call(_candidates(source), "pack")
    assert (c.target_urn, c.name_only) == (None, True)


def test_known_receiver_without_the_member_is_not_name_only() -> None:
    """受け手の型が分かっている（同じファイルのクラス・import したクラス）なら、名前で結ぶ候補にしない。"""
    local = _call(_candidates("class R:\n    pass\n\ndef f():\n    r = R()\n    return r.pack()\n"), "pack")
    assert (local.target_urn, local.name_only) == (None, False)
    imported = _call(
        _candidates("from app.engine import Runner\n\ndef f(r: Runner):\n    return r.pack()\n"), "pack"
    )
    assert imported.target_urn == f"lk://{WS}/app/engine.py#Runner.pack" and imported.name_only is False


def test_only_calls_are_name_only() -> None:
    cands = _candidates("def f(x):\n    y = x.pack\n    return y\n")
    [read] = [c for c in cands if c.target_name == "pack"]
    assert read.relation_kind == "REFERENCES" and read.name_only is False


def test_typescript_any_receiver_is_name_only() -> None:
    src = "export function relay(s: any, msg: string): string {\n  return s.send(msg);\n}\n"
    c = _call(_candidates(src, "typescript", "web/relay.ts"), "send")
    assert (c.target_urn, c.name_only) == (None, True)


def test_builtin_method_names_are_fixed_lists() -> None:
    assert {"append", "get", "join", "items", "format"} <= PYTHON_BUILTIN_METHODS
    assert {"push", "get", "then", "map"} <= JS_BUILTIN_METHODS
    assert "pack" not in PYTHON_BUILTIN_METHODS and "send" not in JS_BUILTIN_METHODS
    assert not [n for n in PYTHON_BUILTIN_METHODS if n.startswith("_")]


# --- 取り込みと移行 ----------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_writer(tmp_path / "paad.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


def _tx(conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], object]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        fn(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _facts(path: str, calls: list[NameCallFact], language: str = "python") -> FileFacts:
    return FileFacts(
        workspace_id=WS,
        path=RelPath(path),
        language=language,
        tier=Tier.T1,
        line_count=3,
        byte_size=10,
        sha256="a" * 64,
        is_test=path.startswith("tests/"),
        confidence=Confidence.STRUCTURAL,
        name_calls=calls,
    )


def test_name_calls_are_replaced_per_file_and_go_with_the_file(conn: sqlite3.Connection) -> None:
    _tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS, "root_path": "/ws"}))
    first = [
        NameCallFact(source_urn=f"lk://{WS}/app/a.py#f", name="pack", call_site_line=2, call_site_col=11),
        NameCallFact(source_urn=f"lk://{WS}/app/a.py#g", name="run", call_site_line=4, call_site_col=4,
                     guard_condition="ok"),
    ]
    _tx(conn, lambda c: ingest_file(c, _facts("app/a.py", first)))
    _tx(conn, lambda c: ingest_file(c, _facts("tests/test_a.py", first[:1])))
    rows = queries.name_calls_named(conn, WS, "pack", ["python"])
    assert [(str(r.path), r.site.line, r.site.col, r.source_urn, r.is_test) for r in rows] == [
        ("app/a.py", 2, 11, f"lk://{WS}/app/a.py#f", False),
        ("tests/test_a.py", 2, 11, f"lk://{WS}/app/a.py#f", True),
    ]
    assert conn.execute("SELECT call_site_line, call_site_col FROM _name_calls WHERE name = 'run'").fetchall() == [
        (5, 5)
    ]  # 列は relations と同じ 1 始まり
    assert [str(r.path) for r in queries.name_calls_named(conn, WS, "pack", ["python"], include_tests=False)] == [
        "app/a.py"
    ]
    assert queries.name_calls_named(conn, WS, "pack", ["typescript"]) == []
    [g] = queries.name_calls_from(conn, f"lk://{WS}/app/a.py#g")
    assert (g.name, g.guard_condition) == ("run", "ok")
    _tx(conn, lambda c: ingest_file(c, _facts("app/a.py", [])))  # 取り込み直しで全置換
    assert [str(r.path) for r in queries.name_calls_named(conn, WS, "pack", ["python"])] == ["tests/test_a.py"]
    _tx(conn, lambda c: remove_file(c, WS, "tests/test_a.py"))  # ファイルの削除で消える（CASCADE）
    assert conn.execute("SELECT count(*) FROM _name_calls").fetchone()[0] == 0


def test_migration_4_to_5_adds_the_table_and_reingests_code_files(conn: sqlite3.Connection) -> None:
    """v4 の DB は表を足し、関係を抽出する言語のファイルの ``sha256`` を空にする（次の差分走査で取り込み直して値を入れる）。"""
    _tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS, "root_path": "/ws"}))
    _tx(conn, lambda c: ingest_file(c, _facts("app/a.py", [])))
    _tx(conn, lambda c: ingest_file(c, _facts("web/b.ts", [], language="typescript")))
    _tx(conn, lambda c: ingest_file(c, _facts("README.md", [], language="markdown")))
    conn.execute("DROP TABLE _name_calls")
    conn.execute("UPDATE _meta SET schema_version = 4")
    assert ensure_schema(conn) is SchemaAction.MIGRATED
    assert current_schema_version() == 5
    assert conn.execute("SELECT count(*) FROM _name_calls").fetchone()[0] == 0
    shas = dict(conn.execute("SELECT path, sha256 FROM files").fetchall())
    assert shas == {"app/a.py": "", "web/b.ts": "", "README.md": "a" * 64}
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"_name_calls_name", "_name_calls_source", "_name_calls_file"} <= names


def test_missing_table_reads_as_no_name_calls(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE _name_calls")
    assert queries.name_calls_named(conn, WS, "pack", ["python"]) == []
    assert queries.name_calls_from(conn, "lk://ws/a.py#f") == []
