"""``index/schema.sql`` の公開部分が仕様書 §9.2 の DDL と一致することを守る（設計書 §10.5 の schema ジョブ）。

比較するのは ``CREATE TABLE`` / ``CREATE INDEX`` / ``CREATE VIRTUAL TABLE`` の文の集合（空白とコメントは無視）。
``schema.sql`` だけが持ってよいもの（許容リスト）は、内部テーブル ``_meta`` / ``_hover_cache`` / ``_symbol_rank`` と、
``symbols_fts`` を ``symbols`` に追従させるトリガ 3 本。仕様書側の ``PRAGMA`` は文の集合に入れない。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "Lucianke仕様書.md"
SCHEMA = ROOT / "src" / "lucianke" / "index" / "schema.sql"

PUBLIC_DDL = re.compile(r"CREATE\s+(TABLE|INDEX|VIRTUAL\s+TABLE)\b", re.IGNORECASE)
CREATE_NAME = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?(?:VIRTUAL\s+)?(?P<kind>TABLE|INDEX|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>\w+)",
    re.IGNORECASE,
)

# schema.sql だけが持ってよいもの（設計書 §6.2、§7.6.5）
ALLOWED_INTERNAL_TABLES = {
    "_meta",
    "_hover_cache",
    "_symbol_rank",
    "_import_probes",
    "_exact_marks",
    "_name_calls",
    "_member_refs",
    "_attr_defs",  # BUG-0095
    "_var_types",  # BUG-0094
    "_reexports",  # BUG-0100
    "_reexport_deps",  # BUG-0100
    "_type_aliases",  # BUG-0108（v13 から型注釈つきのメンバーの型の構成も。BUG-0113）
}
ALLOWED_TRIGGERS = {"symbols_ai", "symbols_ad", "symbols_au"}
ALLOWED_INTERNAL_INDEXES = {
    "_import_probes_stem",
    "_name_calls_name",
    "_name_calls_source",
    "_name_calls_file",
    "_member_refs_class",
    "_member_refs_file",
    "_attr_defs_name",
    "_attr_defs_file",
    "_var_types_type",
    "_var_types_file",
    "_reexports_name",
    "_reexport_deps_path",
    "_type_aliases_alias",
    "_type_aliases_target",
    "_type_aliases_file",
}


def _strip_comments(sql: str) -> str:
    """行末の ``--`` コメントを除く。DDL の文字列リテラルに ``--`` は含まれない（このテストで保証する）。"""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _statements(sql: str) -> list[str]:
    """コメントを除いて 1 文ずつに分ける（トリガの中の ``;`` は文の終わりにしない）。"""
    out: list[str] = []
    buf: list[str] = []
    for line in _strip_comments(sql).splitlines():
        if not line.strip() and not buf:
            continue
        buf.append(line)
        chunk = "\n".join(buf)
        if sqlite3.complete_statement(chunk):
            out.append(chunk.strip())
            buf.clear()
    assert not "".join(buf).strip(), f"終わりの ';' が無い文が残っている: {buf!r}"
    return out


def _normalize(statement: str) -> str:
    """空白の違いを無くす: 空白をすべて除く（トークンの区切りが空白だけの場所は DDL に無い前提で十分）。"""
    return re.sub(r"\s+", "", statement.rstrip(";"))


def _name(statement: str) -> str:
    m = CREATE_NAME.match(statement)
    assert m, f"CREATE 文として読めない: {statement[:60]!r}"
    return m.group("name")


def _spec_ddl_block() -> str:
    text = SPEC.read_text(encoding="utf-8")
    heading = re.search(r"^## 9\.2 DDL\s*$", text, re.MULTILINE)
    assert heading, "仕様書に '## 9.2 DDL' が見つからない"
    block = re.compile(r"```sql\n(.*?)\n```", re.DOTALL).search(text, heading.end())
    assert block, "仕様書 §9.2 に ```sql ブロックが見つからない"
    return block.group(1)


def _public(statements: list[str]) -> list[str]:
    return [s for s in statements if PUBLIC_DDL.match(s) and not _name(s).startswith("_")]


def test_spec_block_is_only_public_ddl_and_pragma() -> None:
    """仕様書側に、比較の対象外の文（PRAGMA 以外）が紛れ込んでいない。"""
    statements = _statements(_spec_ddl_block())
    others = [s for s in statements if not PUBLIC_DDL.match(s) and not s.upper().startswith("PRAGMA")]
    assert others == []


def test_no_comment_marker_inside_string_literals() -> None:
    """コメント除去が ``--`` を行単位で切るので、リテラルの中に ``--`` が無いことを前提として守る。"""
    for sql in (SCHEMA.read_text(encoding="utf-8"), _spec_ddl_block()):
        for line in sql.splitlines():
            code = line.split("--", 1)[0]
            assert code.count("'") % 2 == 0, f"'--' がリテラルを切っている可能性: {line!r}"


def test_public_ddl_matches_spec_9_2() -> None:
    schema_public = {_normalize(s): s for s in _public(_statements(SCHEMA.read_text(encoding="utf-8")))}
    spec_public = {_normalize(s): s for s in _public(_statements(_spec_ddl_block()))}
    assert (
        len(spec_public) >= 20
    )  # 10 テーブル + symbols_fts + 索引。抽出の失敗で空集合どうしが一致するのを防ぐ

    only_schema = [schema_public[k] for k in schema_public.keys() - spec_public.keys()]
    only_spec = [spec_public[k] for k in spec_public.keys() - schema_public.keys()]
    assert not only_schema and not only_spec, (
        "schema.sql と仕様書 §9.2 の DDL が違う。\n"
        f"schema.sql だけにある:\n{chr(10).join(only_schema)}\n仕様書だけにある:\n{chr(10).join(only_spec)}"
    )


def test_schema_sql_extras_are_on_the_allow_list() -> None:
    """schema.sql が仕様書に無いものを持つなら、内部テーブル 4 つとその索引 1 つ、トリガ 3 本だけ。"""
    statements = _statements(SCHEMA.read_text(encoding="utf-8"))
    public = set(_public(statements))
    extras = [s for s in statements if s not in public]
    tables = {_name(s) for s in extras if _name_kind(s) == "TABLE"}
    triggers = {_name(s) for s in extras if _name_kind(s) == "TRIGGER"}
    indexes = {_name(s) for s in extras if _name_kind(s) == "INDEX"}
    other = [s for s in extras if _name_kind(s) not in {"TABLE", "TRIGGER", "INDEX"}]
    assert tables == ALLOWED_INTERNAL_TABLES
    assert indexes == ALLOWED_INTERNAL_INDEXES
    assert triggers == ALLOWED_TRIGGERS
    assert other == []


def _name_kind(statement: str) -> str:
    m = CREATE_NAME.match(statement)
    assert m, statement[:60]
    return m.group("kind").upper()


def test_public_table_count_is_ten_plus_fts() -> None:
    tables = [s for s in _public(_statements(SCHEMA.read_text(encoding="utf-8"))) if _name_kind(s) == "TABLE"]
    names = {_name(s) for s in tables}
    assert len(names) == 11  # 公開 10 テーブル + symbols_fts
    assert "symbols_fts" in names
