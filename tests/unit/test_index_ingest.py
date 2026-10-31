"""``index/ingest.py``（PaaD へのファイル単位の取り込み）と ``index/queries.py``（名前つき読み取りクエリ）。

DB は ``tmp_path`` の実ファイル（``open_writer`` + ``ensure_schema``）。取り込みはライタースレッドの
ジョブと同じ形（``BEGIN IMMEDIATE`` → 関数 → ``data_version`` を +1 → ``COMMIT``）で呼ぶ。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.model import Confidence, Kind, Position, Range, RelPath, SymbolRecord, Tier
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import CodeSel, Urn
from lucianke.index import queries
from lucianke.index.connection import open_writer
from lucianke.index.ingest import (
    DiagnosticFact,
    FileFacts,
    RelationFact,
    SymbolExtra,
    ingest_file,
    remove_file,
    replace_lsp_diagnostics,
    upsert_exact_relations,
    upsert_workspace,
)
from lucianke.index.migrate import ensure_schema
from lucianke.index.writer import IndexWriter

WS = "w"


# --- 道具 -------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_writer(tmp_path / "paad.sqlite")
    ensure_schema(c)
    tx(
        c,
        lambda cc: upsert_workspace(cc, {"workspace_id": WS, "root_path": str(tmp_path / "root")}),
        bump=False,
    )
    yield c
    c.close()


def tx[T](conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], T], *, bump: bool = True) -> T:
    """``IndexWriter`` のジョブと同じ形で ``fn`` を 1 トランザクションで実行する。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(conn)
        if bump:
            conn.execute("UPDATE _meta SET data_version = data_version + 1")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return result


def urn(path: str, qualified: str | None = None) -> Urn:
    return Urn(WS, path, CodeSel(qualified) if qualified else None)


def sym(
    path: str,
    qualified: str,
    start: int,
    end: int,
    *,
    kind: Kind = Kind.FUNCTION,
    signature: str | None = None,
    doc: str | None = None,
) -> SymbolRecord:
    """``start``〜``end`` は 1 始まり・両端を含む行。名前トークンは開始行の 4 列目から。"""
    name = qualified.rpartition(".")[2]
    return SymbolRecord(
        urn=urn(path, qualified),
        path=RelPath(path),
        name=name,
        qualified_name=qualified,
        kind=kind,
        range=Range.from_line_span_external(start, end),
        selection=Range(Position(start - 1, 4), Position(start - 1, 4 + len(name))),
        signature=signature,
        doc_summary=doc,
        confidence=Confidence.STRUCTURAL,
    )


def facts(
    path: str = "src/a.py",
    symbols: tuple[SymbolRecord, ...] = (),
    relations: tuple[RelationFact, ...] = (),
    syntax_errors: tuple[tuple[int, int, int, int, str], ...] = (),
    extras: dict[str, SymbolExtra] | None = None,
    sha: str = "s1",
    line_count: int = 20,
    is_test: bool = False,
) -> FileFacts:
    return FileFacts(
        workspace_id=WS,
        path=RelPath(path),
        language="python",
        tier=Tier.T1,
        line_count=line_count,
        byte_size=100,
        sha256=sha,
        is_test=is_test,
        confidence=Confidence.STRUCTURAL,
        symbols=symbols,
        relations=relations,
        syntax_errors=syntax_errors,
        symbol_extras=extras or {},
    )


def rel(
    src: Urn | str,
    tgt: Urn | str,
    kind: str = "CALLS",
    line: int | None = 0,
    col: int | None = 0,
    confidence: Confidence = Confidence.STRUCTURAL,
) -> RelationFact:
    return RelationFact(
        source_urn=src,
        target_urn=tgt,
        relation_kind=kind,
        call_site_line=line,
        call_site_col=col,
        use_kind="call",
        confidence=confidence,
    )


def ingest(conn: sqlite3.Connection, f: FileFacts, *, bump: bool = True):
    return tx(conn, lambda c: ingest_file(c, f), bump=bump)


def rows(conn: sqlite3.Connection, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    return conn.execute(sql, params).fetchall()


def scalar(conn: sqlite3.Connection, sql: str, *params: Any) -> Any:
    return conn.execute(sql, params).fetchone()[0]


def symbol_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return rows(conn, "SELECT * FROM symbols ORDER BY urn")


def relation_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """``relation_id`` を除いた内容。"""
    return rows(
        conn,
        "SELECT source_urn, target_urn, relation_kind, source_file_id, target_file_id, call_site_line,"
        " call_site_col, use_kind, guard_condition, confidence_level FROM relations ORDER BY 1, 2, 3, 6, 7",
    )


def class_with_method(path: str = "src/a.py") -> tuple[SymbolRecord, ...]:
    return (
        sym(path, "Foo", 1, 10, kind=Kind.CLASS),
        sym(path, "Foo.bar", 3, 5, kind=Kind.METHOD, signature="(self) -> int"),
        sym(path, "baz", 12, 14, signature="(x: int) -> str"),
    )


# --- ingest: files / symbols ---------------------------------------------------------------


def test_new_file_is_ingested(conn: sqlite3.Connection) -> None:
    stats = ingest(conn, facts(symbols=class_with_method(), is_test=True, line_count=14))

    assert stats.file_created and stats.symbols_added == 3 and stats.symbols_removed == 0
    assert rows(
        conn,
        "SELECT path, language, tier, line_count, byte_size, sha256, is_test, confidence_level FROM files",
    ) == [("src/a.py", "python", 1, 14, 100, "s1", 1, "structural")]
    assert rows(
        conn,
        "SELECT urn, parent_urn, start_line, end_line, sel_line, sel_start_col, sel_end_col, kind "
        "FROM symbols ORDER BY start_line",
    ) == [
        ("lk://w/src/a.py#Foo", None, 1, 10, 1, 5, 8, "Class"),
        ("lk://w/src/a.py#Foo.bar", "lk://w/src/a.py#Foo", 3, 5, 3, 5, 8, "Method"),
        ("lk://w/src/a.py#baz", None, 12, 14, 12, 5, 8, "Function"),
    ]
    assert rows(conn, "SELECT name FROM symbols_fts WHERE symbols_fts MATCH 'baz'") == [("baz",)]
    assert scalar(conn, "SELECT data_version FROM _meta") == 1


@pytest.mark.parametrize(
    ("content", "expected"), [(b"", 0), (b"a", 1), (b"a\nb", 2), (b"a\nb\n", 2), (b"a\n\n", 2)]
)
def test_line_count_is_content_line_count(conn: sqlite3.Connection, content: bytes, expected: int) -> None:
    count = LineIndex.from_bytes(content).content_line_count
    assert count == expected
    ingest(conn, facts(line_count=count))
    assert scalar(conn, "SELECT line_count FROM files") == expected


def test_reingest_same_content_changes_nothing(conn: sqlite3.Connection) -> None:
    syms = class_with_method()
    rels = (rel(urn("src/a.py", "baz"), urn("src/a.py", "Foo.bar")),)
    ingest(conn, facts(symbols=syms, relations=rels, syntax_errors=((1, 0, 1, 3, "oops"),)))
    before = (symbol_rows(conn), relation_rows(conn), rows(conn, "SELECT file_id, sha256 FROM files"))
    diag_before = rows(conn, "SELECT * FROM diagnostics")

    stats = ingest(conn, facts(symbols=syms, relations=rels, syntax_errors=((1, 0, 1, 3, "oops"),)))

    assert not stats.file_created and not stats.symbols_changed and stats.symbols_unchanged == 3
    assert (symbol_rows(conn), relation_rows(conn), rows(conn, "SELECT file_id, sha256 FROM files")) == before
    assert [r[1:] for r in rows(conn, "SELECT * FROM diagnostics")][0][:-2] == diag_before[0][1:-2]
    assert scalar(conn, "SELECT count(*) FROM diagnostics") == 1
    assert scalar(conn, "SELECT count(*) FROM symbols_fts WHERE symbols_fts MATCH 'baz'") == 1


def test_body_change_keeps_urn_and_changes_body_hash(conn: sqlite3.Connection) -> None:
    foo = sym("src/a.py", "baz", 1, 3, signature="(x) -> int")
    key = str(foo.urn)
    ingest(conn, facts(symbols=(foo,), extras={key: SymbolExtra(body_sha256="h1")}))
    ingest(
        conn,
        facts(
            path="src/b.py",
            symbols=(sym("src/b.py", "use", 1, 2),),
            relations=(rel(urn("src/b.py", "use"), foo.urn),),
        ),
    )
    rowid_before = scalar(conn, "SELECT rowid FROM symbols WHERE urn = ?", key)

    stats = ingest(conn, facts(symbols=(foo,), extras={key: SymbolExtra(body_sha256="h2")}, sha="s2"))

    assert (stats.symbols_updated, stats.symbols_added, stats.symbols_removed) == (1, 0, 0)
    assert rows(conn, "SELECT urn, body_sha256 FROM symbols WHERE name = 'baz'") == [(key, "h2")]
    assert scalar(conn, "SELECT rowid FROM symbols WHERE urn = ?", key) == rowid_before
    # 他ファイルからの関係は保たれる
    assert queries.count_references_to(conn, foo.urn) == 1


def test_signature_change_updates_fts(conn: sqlite3.Connection) -> None:
    ingest(conn, facts(symbols=(sym("src/a.py", "baz", 1, 2, signature="(alpha)"),)))
    ingest(conn, facts(symbols=(sym("src/a.py", "baz", 1, 2, signature="(omega)"),), sha="s2"))
    assert rows(conn, "SELECT name FROM symbols_fts WHERE symbols_fts MATCH 'omega'") == [("baz",)]
    assert rows(conn, "SELECT name FROM symbols_fts WHERE symbols_fts MATCH 'alpha'") == []


def test_removed_symbol_takes_its_relations_with_it(conn: sqlite3.Connection) -> None:
    a_syms = class_with_method("src/a.py")
    bar, baz = a_syms[1].urn, a_syms[2].urn
    ingest(conn, facts(symbols=a_syms, relations=(rel(baz, bar),)))
    ingest(
        conn,
        facts(
            path="src/b.py",
            symbols=(sym("src/b.py", "use", 1, 2),),
            relations=(rel(urn("src/b.py", "use"), bar),),
        ),
    )
    assert queries.count_references_to(conn, bar) == 2

    # Foo.bar を消して再取り込み
    stats = ingest(conn, facts(symbols=(a_syms[0], a_syms[2]), sha="s2"))

    assert stats.symbols_removed == 1
    assert rows(conn, "SELECT urn FROM symbols WHERE urn LIKE '%Foo.bar'") == []
    assert (
        rows(conn, "SELECT * FROM relations WHERE target_urn = ? OR source_urn = ?", str(bar), str(bar)) == []
    )
    assert rows(conn, "SELECT name FROM symbols_fts WHERE symbols_fts MATCH 'bar'") == []
    assert scalar(conn, "SELECT count(*) FROM symbols") == 3  # Foo, baz, use


def test_removed_parent_cascades_but_surviving_child_is_kept(conn: sqlite3.Connection) -> None:
    ingest(conn, facts(symbols=class_with_method()))
    # 親 Foo の URN が消え、子 Foo.bar は残る（修飾名から導く親が同じファイルに無い）
    ingest(conn, facts(symbols=(class_with_method()[1],), sha="s2"))
    assert rows(conn, "SELECT urn, parent_urn FROM symbols") == [("lk://w/src/a.py#Foo.bar", None)]


def test_parent_is_chosen_by_range_among_same_qualified_names(conn: sqlite3.Connection) -> None:
    syms = (
        sym("src/a.py", "Foo", 1, 5, kind=Kind.CLASS),
        sym("src/a.py", "Foo", 10, 20, kind=Kind.CLASS),  # 再定義（同じ URN になるので sighash 付きにする）
        sym("src/a.py", "Foo.run", 12, 14, kind=Kind.METHOD),
    )
    redefined = SymbolRecord(
        urn=Urn(WS, "src/a.py", CodeSel("Foo", None, "0123abcd")),
        path=syms[1].path,
        name="Foo",
        qualified_name="Foo",
        kind=Kind.CLASS,
        range=syms[1].range,
        selection=syms[1].selection,
        confidence=Confidence.STRUCTURAL,
    )
    ingest(conn, facts(symbols=(syms[0], redefined, syms[2])))
    assert scalar(conn, "SELECT parent_urn FROM symbols WHERE name = 'run'") == str(redefined.urn)


def test_duplicate_urns_keep_the_first(conn: sqlite3.Connection) -> None:
    first = sym("src/a.py", "dup", 1, 2, signature="(first)")
    second = sym("src/a.py", "dup", 5, 6, signature="(second)")
    stats = ingest(conn, facts(symbols=(first, second)))
    assert stats.symbols_duplicate == 1 and stats.symbols_added == 1
    assert rows(conn, "SELECT signature FROM symbols") == [("(first)",)]


def test_symbol_of_another_file_is_rejected_and_rolled_back(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="does not belong"):
        ingest(conn, facts(path="src/a.py", symbols=(sym("src/other.py", "x", 1, 2),)))
    assert scalar(conn, "SELECT count(*) FROM files") == 0
    assert scalar(conn, "SELECT data_version FROM _meta") == 0


def test_ingest_outside_a_transaction_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="transaction"):
        ingest_file(conn, facts())
    with pytest.raises(RuntimeError, match="transaction"):
        remove_file(conn, WS, "src/a.py")


def test_upsert_workspace_updates_only_given_columns(conn: sqlite3.Connection) -> None:
    root = scalar(conn, "SELECT root_path FROM workspaces")
    tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS, "root_path": root, "head_sha": "abc"}))
    tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS, "root_path": root, "active_branch": "main"}))
    assert rows(conn, "SELECT head_sha, active_branch, confidence_level FROM workspaces") == [
        ("abc", "main", "exact")
    ]
    with pytest.raises(ValueError, match="unknown"):
        tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS, "root_path": root, "bogus": 1}))


# --- ingest: relations -------------------------------------------------------------------------


def test_relations_are_replaced_including_exact_rows(conn: sqlite3.Connection) -> None:
    a = class_with_method()
    baz, bar = a[2].urn, a[1].urn
    ingest(conn, facts(symbols=a, relations=(rel(baz, bar, line=12, col=4),)))
    tx(conn, lambda c: upsert_exact_relations(c, [rel(baz, bar, line=12, col=4)]), bump=True)
    assert rows(conn, "SELECT confidence_level FROM relations") == [("exact",)]

    stats = ingest(
        conn, facts(symbols=a, relations=(rel(baz, a[0].urn, kind="REFERENCES", line=13, col=1),), sha="s2")
    )

    assert stats.relations_removed == 1 and stats.relations_added == 1
    assert rows(
        conn,
        "SELECT target_urn, relation_kind, confidence_level, call_site_line, call_site_col FROM relations",
    ) == [(str(a[0].urn), "REFERENCES", "structural", 14, 2)]


def test_relation_target_file_id_is_resolved_and_backfilled(conn: sqlite3.Connection) -> None:
    target = urn("src/a.py", "baz")
    ingest(
        conn,
        facts(
            path="src/b.py",
            symbols=(sym("src/b.py", "use", 1, 2),),
            relations=(rel(urn("src/b.py", "use"), target),),
        ),
    )
    assert scalar(conn, "SELECT target_file_id FROM relations") is None  # a.py はまだ無い

    stats = ingest(conn, facts(symbols=(sym("src/a.py", "baz", 1, 2),)))

    a_id = scalar(conn, "SELECT file_id FROM files WHERE path = 'src/a.py'")
    assert stats.file_id == a_id
    assert scalar(conn, "SELECT target_file_id FROM relations") == a_id
    b_id = scalar(conn, "SELECT file_id FROM files WHERE path = 'src/b.py'")
    assert scalar(conn, "SELECT source_file_id FROM relations") == b_id


def test_external_symbols_get_a_minimal_row(conn: sqlite3.Connection) -> None:
    use = urn("src/a.py", "use")
    f = facts(
        symbols=(sym("src/a.py", "use", 1, 5),),
        relations=(
            rel(use, "lk://_ext/requests@2.31#Session.get", kind="CALLS", line=2),
            rel(use, "lk://_ext/requests@2.31#Session.get", kind="CALLS", line=3),
            rel(use, "lk://_ext/os", kind="IMPORTS", line=0),
        ),
    )
    stats = ingest(conn, f)
    assert stats.external_symbols_added == 2 and stats.relations_added == 3
    assert rows(
        conn,
        "SELECT urn, file_id, is_external, name, qualified_name, kind, confidence_level FROM symbols"
        " WHERE is_external = 1 ORDER BY urn",
    ) == [
        ("lk://_ext/os", None, 1, "os", "os", "Module", "heuristic"),
        ("lk://_ext/requests@2.31#Session.get", None, 1, "get", "Session.get", "Method", "heuristic"),
    ]
    # 再取り込みで増えない。ファイルのシンボルを消しても外部の行は壊れない
    assert ingest(conn, f).external_symbols_added == 0
    ingest(conn, facts(sha="s2"))
    assert scalar(conn, "SELECT count(*) FROM symbols WHERE is_external = 1") == 2
    ext = queries.symbol_by_urn(conn, "lk://_ext/requests@2.31#Session.get")
    assert ext is not None and ext.name == "get" and ext.path == "requests@2.31"


def test_exact_relation_replaces_structural_for_the_same_triple(conn: sqlite3.Connection) -> None:
    a = class_with_method()
    baz, bar, foo = a[2].urn, a[1].urn, a[0].urn
    ingest(
        conn,
        facts(symbols=a, relations=(rel(baz, bar, line=12, col=4), rel(baz, foo, "REFERENCES", line=13))),
    )

    count = tx(
        conn,
        lambda c: upsert_exact_relations(c, [rel(baz, bar, line=12, col=5, confidence=Confidence.HEURISTIC)]),
    )

    assert count == 1
    by_target = {
        r[0]: r[1:]
        for r in rows(
            conn, "SELECT target_urn, relation_kind, confidence_level, call_site_col FROM relations"
        )
    }
    assert by_target[str(bar)] == ("CALLS", "exact", 6)  # 水準は引数に関わらず exact
    assert by_target[str(foo)][1] == "structural"  # 別の組は触らない
    # source_file_id は source_urn の持ち主のファイル（再取り込みで exact も消えるように）
    a_id = scalar(conn, "SELECT file_id FROM files")
    assert scalar(conn, "SELECT source_file_id FROM relations WHERE target_urn = ?", str(bar)) == a_id
    # 同じ組をもう一度 exact で入れても重複しない
    tx(conn, lambda c: upsert_exact_relations(c, [rel(baz, bar, line=12, col=5)]))
    assert scalar(conn, "SELECT count(*) FROM relations WHERE target_urn = ?", str(bar)) == 1


# --- ingest: diagnostics -----------------------------------------------------------------------


def test_tree_sitter_diagnostics_are_replaced_wholesale(conn: sqlite3.Connection) -> None:
    syms = class_with_method()
    ingest(conn, facts(symbols=syms, syntax_errors=((3, 1, 3, 5, "e1"), (12, 0, 13, 2, "e2"))))
    tx(
        conn,
        lambda c: replace_lsp_diagnostics(
            c,
            WS,
            "src/a.py",
            [DiagnosticFact(severity=1, source="pyright", message="lsp", start_line=0, start_col=0)],
        ),
        bump=False,
    )
    assert rows(
        conn,
        "SELECT source, severity, message, start_line, start_col, end_line, end_col, enclosing_urn, confidence_level"
        " FROM diagnostics WHERE source = 'tree-sitter' ORDER BY start_line",
    ) == [
        ("tree-sitter", "error", "e1", 4, 2, 4, 6, "lk://w/src/a.py#Foo.bar", "structural"),
        ("tree-sitter", "error", "e2", 13, 1, 14, 3, "lk://w/src/a.py#baz", "structural"),
    ]

    stats = ingest(conn, facts(symbols=syms, syntax_errors=((0, 0, 0, 1, "only"),), sha="s2"))

    assert stats.diagnostics_added == 1
    assert rows(conn, "SELECT source, message, enclosing_urn FROM diagnostics ORDER BY diag_id") == [
        ("pyright", "lsp", "lk://w/src/a.py#Foo"),
        ("tree-sitter", "only", "lk://w/src/a.py#Foo"),
    ]
    ingest(conn, facts(symbols=syms, sha="s3"))
    assert rows(conn, "SELECT source FROM diagnostics") == [("pyright",)]  # 構文エラーが無くなれば消える


def test_lsp_diagnostics_replace_non_tree_sitter_rows_only(conn: sqlite3.Connection) -> None:
    ingest(conn, facts(symbols=class_with_method(), syntax_errors=((0, 0, 0, 1, "ts"),)))

    def publish(source: str | None, *diags: DiagnosticFact) -> int:
        return tx(
            conn, lambda c: replace_lsp_diagnostics(c, WS, "src/a.py", list(diags), source=source), bump=False
        )

    def d(source: str, message: str, severity: str | int = "warning") -> DiagnosticFact:
        return DiagnosticFact(severity=severity, source=source, message=message, start_line=4, start_col=0)

    assert publish(None, d("pyright", "p1"), d("ruff", "r1", 2), d("tree-sitter", "ignored")) == 2
    # source 指定: pyright だけ置換、ruff は残る
    assert publish("pyright", d("pyright", "p2", "error"), d("ruff", "ignored")) == 1
    assert rows(
        conn,
        "SELECT source, message, severity FROM diagnostics WHERE source != 'tree-sitter' ORDER BY source",
    ) == [
        ("pyright", "p2", "error"),
        ("ruff", "r1", "warning"),
    ]
    # source 指定なし: tree-sitter 以外を全置換、tree-sitter は残る
    assert publish(None) == 0
    assert rows(conn, "SELECT source FROM diagnostics") == [("tree-sitter",)]
    # 知らないファイルは何もしない
    assert tx(conn, lambda c: replace_lsp_diagnostics(c, WS, "nope.py", [d("pyright", "x")]), bump=False) == 0
    with pytest.raises(ValueError, match="severity"):
        publish(None, d("pyright", "bad", "fatal"))


def test_lsp_diagnostics_do_not_need_a_version_bump(tmp_path: Path) -> None:
    with IndexWriter(tmp_path / "paad.sqlite") as w:
        w.submit(
            lambda c: upsert_workspace(c, {"workspace_id": WS, "root_path": "/r"}),
            bumps_version=True,
            label="ws",
        ).result()
        base = w.data_version()
        stats = w.submit(
            lambda c: ingest_file(c, facts(symbols=class_with_method())), bumps_version=True, label="ingest"
        ).result()
        assert stats.symbols_added == 3 and w.data_version() == base + 1  # 1 ジョブ = ちょうど +1
        w.submit(
            lambda c: replace_lsp_diagnostics(
                c,
                WS,
                "src/a.py",
                [DiagnosticFact(severity=2, source="ruff", message="m", start_line=0, start_col=0)],
            ),
            bumps_version=False,
            label="diag",
        ).result()
        assert w.data_version() == base + 1  # 診断だけでは増えない
        w.submit(lambda c: remove_file(c, WS, "src/a.py"), bumps_version=True, label="remove").result()
        assert w.data_version() == base + 2


# --- remove_file -------------------------------------------------------------------------------


def test_remove_file_cascades_and_drops_incoming_relations(conn: sqlite3.Connection) -> None:
    a = class_with_method()
    ingest(conn, facts(symbols=a, relations=(rel(a[2].urn, a[1].urn),), syntax_errors=((0, 0, 0, 1, "x"),)))
    ingest(
        conn,
        facts(
            path="src/b.py",
            symbols=(sym("src/b.py", "use", 1, 2),),
            relations=(rel(urn("src/b.py", "use"), a[1].urn),),
        ),
    )

    assert tx(conn, lambda c: remove_file(c, WS, "src/a.py"))

    assert rows(conn, "SELECT path FROM files") == [("src/b.py",)]
    assert rows(conn, "SELECT name FROM symbols") == [("use",)]
    assert scalar(conn, "SELECT count(*) FROM relations") == 0
    assert scalar(conn, "SELECT count(*) FROM diagnostics") == 0
    assert rows(conn, "SELECT name FROM symbols_fts WHERE symbols_fts MATCH 'baz'") == []
    assert not tx(conn, lambda c: remove_file(c, WS, "src/a.py"))  # 元から無ければ False


# --- queries -----------------------------------------------------------------------------------


def test_file_by_path_and_files_in_scope(conn: sqlite3.Connection) -> None:
    for path, is_test in [
        ("src/a.py", False),
        ("src/pkg/b.py", False),
        ("tests/test_a.py", True),
        ("README.md", False),
    ]:
        ingest(conn, facts(path=path, is_test=is_test, sha=path))
    info = queries.file_by_path(conn, WS, "src/a.py")
    assert info is not None and (info.language, info.tier, info.sha256, info.is_test) == (
        "python",
        Tier.T1,
        "src/a.py",
        False,
    )
    assert queries.file_by_path(conn, WS, "nope.py") is None

    def paths(globs: list[str], **kw: Any) -> list[str]:
        return [f.path for f in queries.files_in_scope(conn, WS, globs, **kw)]

    assert paths([]) == ["README.md", "src/a.py", "src/pkg/b.py", "tests/test_a.py"]
    assert paths(["src/*.py"]) == ["src/a.py"]
    assert paths(["src/**/*.py"]) == ["src/a.py", "src/pkg/b.py"]
    assert paths(["**/*.py"]) == ["src/a.py", "src/pkg/b.py", "tests/test_a.py"]
    assert paths(["tests/**", "README.*"]) == ["README.md", "tests/test_a.py"]
    assert paths(["**/*.py"], include_tests=False) == ["src/a.py", "src/pkg/b.py"]


def test_symbols_by_qualified_suffix(conn: sqlite3.Connection) -> None:
    syms = (
        sym("src/a.py", "A", 1, 10, kind=Kind.CLASS),
        sym("src/a.py", "A.run", 2, 4, kind=Kind.METHOD),
        sym("src/a.py", "B", 11, 20, kind=Kind.CLASS),
        sym("src/a.py", "B.run", 12, 14, kind=Kind.METHOD),
        sym("src/a.py", "B.rerun", 15, 16, kind=Kind.METHOD),
        sym("src/a.py", "prerun", 21, 22),
    )
    ingest(conn, facts(symbols=syms))
    ingest(conn, facts(path="src/b.py", symbols=(sym("src/b.py", "run", 1, 2),)))

    def q(name: str) -> list[str]:
        return [s.qualified_name for s in queries.symbols_by_qualified_suffix(conn, WS, "src/a.py", name)]

    assert q("A.run") == ["A.run"]  # 1 件
    assert q("run") == ["A.run", "B.run"]  # 2 件（曖昧）。prerun / rerun は '.' 区切りでないので入らない
    assert q("B") == ["B"]
    assert q("un") == []
    assert q("A.ru") == []
    assert q("") == []
    assert len(queries.symbols_by_qualified_suffix(conn, WS, "src/a.py", "run", limit=1)) == 1
    found = queries.symbols_by_qualified_suffix(conn, WS, "src/a.py", "A.run")[0]
    assert found.urn == syms[1].urn and found.kind is Kind.METHOD and found.path == "src/a.py"
    assert found.range.line_span_external() == (2, 4)
    assert found.selection == syms[1].selection


def test_symbol_at_returns_the_innermost(conn: sqlite3.Connection) -> None:
    syms = (
        sym("src/a.py", "Outer", 1, 20, kind=Kind.CLASS),
        sym("src/a.py", "Outer.method", 5, 12, kind=Kind.METHOD),
        sym("src/a.py", "Outer.method.inner", 7, 8),
        sym("src/a.py", "Outer.other", 14, 14, kind=Kind.METHOD),
    )
    ingest(conn, facts(symbols=syms))

    def at(line0: int, col: int | None = None) -> str | None:
        s = queries.symbol_at(conn, WS, "src/a.py", line0, col)
        return None if s is None else s.qualified_name

    assert at(6) == "Outer.method.inner"  # 7 行目（1 始まり）
    assert at(7) == "Outer.method.inner"
    assert at(8) == "Outer.method"
    assert at(0) == "Outer"
    assert at(13) == "Outer.other"
    assert at(19) == "Outer"
    assert at(20) is None
    assert queries.symbol_at(conn, WS, "other.py", 6) is None


def test_symbol_at_breaks_same_line_ties_by_the_name_token(conn: sqlite3.Connection) -> None:
    one = sym("src/a.py", "first", 3, 3)  # 名前は 5〜9 列（1 始まり）
    two = SymbolRecord(
        urn=urn("src/a.py", "second"),
        path=RelPath("src/a.py"),
        name="second",
        qualified_name="second",
        kind=Kind.FUNCTION,
        range=Range.from_line_span_external(3, 3),
        selection=Range(Position(2, 20), Position(2, 26)),
        confidence=Confidence.STRUCTURAL,
    )
    ingest(conn, facts(symbols=(one, two)))
    pick = lambda col: queries.symbol_at(conn, WS, "src/a.py", 2, col).name  # noqa: E731
    assert pick(5) == "first"
    assert pick(22) == "second"


def _search_fixture(conn: sqlite3.Connection) -> None:
    syms = (
        sym("src/pay.py", "PaymentProcessor", 1, 30, kind=Kind.CLASS, doc="charges cards"),
        sym(
            "src/pay.py",
            "PaymentProcessor.process_charge",
            3,
            9,
            kind=Kind.METHOD,
            signature="(self, amount: Money) -> Receipt",
        ),
        sym("src/pay.py", "process", 31, 33),
        sym("src/pay.py", "refund_payment", 34, 36),
        sym("src/pay.py", "unrelated", 37, 38, doc="about invoices"),
    )
    ingest(conn, facts(path="src/pay.py", symbols=syms))
    other = Urn("other", "x.py", CodeSel("process_other"))
    tx(conn, lambda c: upsert_workspace(c, {"workspace_id": "other", "root_path": "/other"}))
    ingest(
        conn,
        FileFacts(
            workspace_id="other",
            path=RelPath("x.py"),
            language="python",
            tier=Tier.T1,
            line_count=1,
            byte_size=1,
            sha256="x",
            is_test=False,
            confidence=Confidence.STRUCTURAL,
            symbols=(
                SymbolRecord(
                    urn=other,
                    path=RelPath("x.py"),
                    name="process_other",
                    qualified_name="process_other",
                    kind=Kind.FUNCTION,
                    range=Range.from_line_span_external(1, 1),
                    confidence=Confidence.STRUCTURAL,
                ),
            ),
        ),
    )


def test_search_prefix(conn: sqlite3.Connection) -> None:
    _search_fixture(conn)

    def names(q: str, **kw: Any) -> list[str]:
        return [s.qualified_name for s in queries.search_symbols_fts(conn, WS, q, "prefix", **kw)]

    assert set(names("process")) == {"process", "PaymentProcessor.process_charge"}
    assert names("process")[0] == "process"  # 名前が query と一致するものが先
    assert set(names("proc")) == {"process", "PaymentProcessor.process_charge"}
    # "." の無い query は自分の名前だけに当てる: クラス名の前方でそのメンバーを当てない（BUG-0121）
    assert set(names("pay")) == {"PaymentProcessor", "refund_payment"}
    assert names("PaymentProcessor.proc") == ["PaymentProcessor.process_charge"]  # "." があれば限定名も見る
    assert names("process charge") == ["PaymentProcessor.process_charge"]  # 各語が AND
    assert names("invoices") == []  # prefix は name / qualified_name だけ（doc は見ない）
    assert names("process", kinds=["method"]) == ["PaymentProcessor.process_charge"]
    assert names("process", kinds=[Kind.FUNCTION]) == ["process"]
    assert names("process", limit=1) == ["process"]
    assert names("!!!") == [] and names("   ") == []
    assert names('pro"cess') == []  # FTS の構文を壊さない


def test_search_fuzzy(conn: sqlite3.Connection) -> None:
    _search_fixture(conn)

    def names(q: str, **kw: Any) -> list[str]:
        return [s.qualified_name for s in queries.search_symbols_fts(conn, WS, q, "fuzzy", **kw)]

    assert "PaymentProcessor" in names("processor")  # 部分文字列（語の途中）
    assert "unrelated" in names("invoices")  # doc_summary の語
    assert "PaymentProcessor.process_charge" in names("amount")  # signature の語
    assert names("zzz") == []
    assert "process_other" not in "".join(names("process"))  # 別ワークスペースは出ない
    # "." の無い query は限定名を見ない: クラス名で、名前にもシグネチャにも当たらないメンバーを当てない（BUG-0121）
    assert set(names("pay")) == {"PaymentProcessor", "refund_payment"}
    assert set(names("PaymentProcessor")) == {"PaymentProcessor"}
    assert "PaymentProcessor.process_charge" in names("PaymentProcessor.process")  # "." があれば限定名に当てる
    assert names("process", kinds=["Class"]) == ["PaymentProcessor"]  # 部分文字列でも種別で絞れる
    assert names("process", kinds=["Enum"]) == []
    assert names("PROCESSOR")[0] == "PaymentProcessor"


def test_search_exact(conn: sqlite3.Connection) -> None:
    _search_fixture(conn)

    def names(q: str) -> list[str]:
        return [s.qualified_name for s in queries.search_symbols_fts(conn, WS, q, "exact")]

    assert names("process") == ["process"]
    assert names("PaymentProcessor.process_charge") == ["PaymentProcessor.process_charge"]
    assert names("process_charge") == ["PaymentProcessor.process_charge"]
    assert names("Process") == []  # 大文字小文字を区別
    with pytest.raises(ValueError, match="unknown match mode"):
        queries.search_symbols_fts(conn, WS, "x", "regex")  # type: ignore[arg-type]


def test_references_to(conn: sqlite3.Connection) -> None:
    target = urn("src/a.py", "baz")
    ingest(conn, facts(symbols=(sym("src/a.py", "baz", 1, 2),)))
    ingest(
        conn,
        facts(
            path="src/b.py",
            symbols=(sym("src/b.py", "use", 1, 9),),
            relations=(
                rel(urn("src/b.py", "use"), target, "CALLS", line=4, col=2),
                rel(urn("src/b.py", "use"), target, "REFERENCES", line=6, col=0),
            ),
        ),
    )
    ingest(
        conn,
        facts(
            path="tests/test_a.py",
            is_test=True,
            symbols=(sym("tests/test_a.py", "test_baz", 1, 3),),
            relations=(rel(urn("tests/test_a.py", "test_baz"), target, "CALLS", line=1, col=4),),
        ),
    )

    refs = queries.references_to(conn, target)
    assert [(r.path, r.relation_kind, r.site) for r in refs] == [
        ("src/b.py", "CALLS", Position(4, 2)),
        ("src/b.py", "REFERENCES", Position(6, 0)),
        ("tests/test_a.py", "CALLS", Position(1, 4)),
    ]
    assert refs[0].confidence is Confidence.STRUCTURAL and refs[0].use_kind == "call"
    assert [r.relation_kind for r in queries.references_to(conn, target, ["CALLS"])] == ["CALLS", "CALLS"]
    assert len(queries.references_to(conn, target, include_tests=False)) == 2
    assert queries.count_references_to(conn, target, ["CALLS"], include_tests=False) == 1
    assert [r.site for r in queries.references_to(conn, target, offset=1, limit=1)] == [Position(6, 0)]
    assert queries.references_to(conn, "lk://w/src/a.py#nothing") == []


def test_diagnostics_for(conn: sqlite3.Connection) -> None:
    ingest(conn, facts(symbols=class_with_method(), syntax_errors=((2, 0, 2, 4, "syntax"),)))
    ingest(conn, facts(path="src/b.py"))

    def publish(path: str, *diags: DiagnosticFact) -> None:
        tx(conn, lambda c: replace_lsp_diagnostics(c, WS, path, list(diags)), bump=False)

    publish(
        "src/a.py",
        DiagnosticFact(
            severity="warning", source="ruff", code="F401", message="unused", start_line=0, start_col=0
        ),
        DiagnosticFact(
            severity="hint", source="pyright", message="h", start_line=12, start_col=2, end_line=12, end_col=5
        ),
    )
    publish(
        "src/b.py",
        DiagnosticFact(severity="information", source="pyright", message="info", start_line=0, start_col=0),
    )

    got = queries.diagnostics_for(conn, WS, ["src/a.py"])
    assert [(d.severity, d.message, d.range.start, d.range.end, d.enclosing_urn) for d in got] == [
        ("warning", "unused", Position(0, 0), Position(0, 0), "lk://w/src/a.py#Foo"),
        ("error", "syntax", Position(2, 0), Position(2, 4), "lk://w/src/a.py#Foo.bar"),
        ("hint", "h", Position(12, 2), Position(12, 5), "lk://w/src/a.py#baz"),
    ]
    assert (
        got[0].code == "F401"
        and got[1].confidence is Confidence.STRUCTURAL
        and got[0].confidence is Confidence.EXACT
    )
    assert [d.severity for d in queries.diagnostics_for(conn, WS, ["src/a.py"], "warning")] == [
        "warning",
        "error",
    ]
    assert [d.message for d in queries.diagnostics_for(conn, WS, None, "information")] == [
        "unused",
        "syntax",
        "info",
    ]
    assert [d.message for d in queries.diagnostics_for(conn, WS, ["src/a.py"], sources=["ruff"])] == [
        "unused"
    ]
    assert queries.diagnostics_for(conn, WS, []) == []
    with pytest.raises(ValueError, match="severity"):
        queries.diagnostics_for(conn, WS, None, "fatal")


def test_symbols_in_file_parents_and_data_version(conn: sqlite3.Connection) -> None:
    ingest(conn, facts(symbols=class_with_method()))
    assert [s.qualified_name for s in queries.symbols_in_file(conn, WS, "src/a.py")] == [
        "Foo",
        "Foo.bar",
        "baz",
    ]
    assert queries.symbol_parents(conn, WS, "src/a.py") == {
        "lk://w/src/a.py#Foo": None,
        "lk://w/src/a.py#Foo.bar": "lk://w/src/a.py#Foo",
        "lk://w/src/a.py#baz": None,
    }
    assert queries.symbol_by_urn(conn, class_with_method()[1].urn) is not None
    assert queries.symbol_by_urn(conn, "lk://w/src/a.py#nope") is None

    version = queries.data_version(conn)
    ingest(conn, facts(symbols=class_with_method(), sha="s2"))
    assert queries.data_version(conn) == version + 1
    tx(conn, lambda c: None, bump=False)
    assert queries.data_version(conn) == version + 1


# --- 外部シンボルの kind（行の到着順に依らない。設計書 §7.4.6 項 3） --------------------------


@pytest.mark.parametrize(
    ("qualified", "relation_kinds", "expected"),
    [
        (None, ["IMPORTS"], Kind.MODULE),
        ("Decimal", ["IMPORTS"], Kind.CLASS),
        ("Decimal", ["CALLS"], Kind.CLASS),
        ("Decimal", ["REFERENCES"], Kind.CLASS),
        ("Exception", ["INHERITS"], Kind.CLASS),
        ("Disposable", ["IMPLEMENTS"], Kind.CLASS),
        ("str", ["TYPE_USES"], Kind.CLASS),
        ("ROUND_UP", ["REFERENCES"], Kind.CONSTANT),
        ("UUID", ["CALLS"], Kind.CLASS),
        ("getcwd", ["CALLS"], Kind.FUNCTION),
        ("getcwd", ["REFERENCES"], Kind.VARIABLE),
        ("getcwd", ["IMPORTS"], Kind.VARIABLE),
        ("Decimal.quantize", ["CALLS"], Kind.METHOD),
        ("Decimal.quantize", ["REFERENCES"], Kind.PROPERTY),
        ("path.join", ["CALLS"], Kind.FUNCTION),
        ("path.sep", ["REFERENCES"], Kind.VARIABLE),
    ],
)
def test_external_kind_follows_the_name_and_usage(
    qualified: str | None, relation_kinds: list[str], expected: Kind
):
    from lucianke.index.ingest import external_kind

    assert external_kind(qualified, relation_kinds) == expected


@pytest.mark.parametrize("order", [("IMPORTS", "CALLS", "TYPE_USES"), ("TYPE_USES", "CALLS", "IMPORTS")])
def test_external_kind_does_not_depend_on_which_relation_came_first(
    conn: sqlite3.Connection, order: tuple[str, ...]
):
    """``Decimal`` は import が先でも、型注釈が先でも ``Class``。小文字の名前は、呼ぶ行が後から来ても ``Function`` に上がる。"""
    for i, kind in enumerate(order):
        path = f"src/f{i}.py"
        ingest(
            conn,
            facts(
                path=path,
                symbols=(sym(path, "use", 1, 5),),
                relations=(
                    rel(urn(path, "use"), "lk://_ext/decimal#Decimal", kind=kind),
                    rel(
                        urn(path, "use"),
                        "lk://_ext/os#getcwd",
                        kind="REFERENCES" if kind != "CALLS" else "CALLS",
                    ),
                ),
            ),
        )
    assert rows(
        conn,
        "SELECT urn, kind FROM symbols WHERE is_external = 1 ORDER BY urn",
    ) == [("lk://_ext/decimal#Decimal", "Class"), ("lk://_ext/os#getcwd", "Function")]
