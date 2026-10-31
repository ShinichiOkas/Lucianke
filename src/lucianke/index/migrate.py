"""スキーマ版管理（設計書 §7.6.5、仕様書 §9）。

``schema.sql``（パッケージ内。正本）の先頭コメント ``-- schema_version: N`` が「あるべき版」である。
``ensure_schema(conn)`` は DB の状態を見て、次のどれかにする。

* 空の DB: ``schema.sql`` を流し、``_meta`` に 1 行入れる（``created``）。
* ``_meta.schema_version`` が同じ: 何もしない（``unchanged``）。
* 古い版で ``MIGRATIONS`` に前方向の移行がそろっている: 順に適用する（``migrated``）。
* それ以外（移行が無い古い版、新しすぎる版、``_meta`` の無い DB）: 作り直す（``rebuilt``）。
  PaaD はキャッシュで、再索引で復元できるので作り直してよい（設計書 §7.6.5）。

作り直しは **接続の中で** 全オブジェクトを ``DROP`` して ``schema.sql`` を流し直す。ファイルを消して作り直す
のではない（Windows では開いている DB ファイルを消せない。S-10）。結果は同じ（空の DB に最新のスキーマ）。
``edit_seq`` だけは引き継ぐ（編集 ID を再利用して、ジャーナルのディレクトリとぶつけないため）。

全体を 1 つの ``BEGIN IMMEDIATE`` トランザクションで行う。同じ DB を開く別プロセスが同時に起動しても、
後の方は待って、先の結果を見る。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from typing import TYPE_CHECKING, Final

from lucianke import __version__

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

SCHEMA_RESOURCE: Final = "schema.sql"

_VERSION_RE: Final = re.compile(r"^--\s*schema_version:\s*(\d+)\s*$", re.MULTILINE)
_NOT_INTERNAL: Final = (
    r"name NOT LIKE 'sqlite\_%' ESCAPE '\'"  # sqlite_sequence などの SQLite 自身の表は触らない
)


class SchemaAction(StrEnum):
    """``ensure_schema`` が何をしたか。"""

    CREATED = "created"
    UNCHANGED = "unchanged"
    MIGRATED = "migrated"
    REBUILT = "rebuilt"


# 前方向の移行。キーは「移行元の版」、値は N → N+1 にする関数（呼び出し側のトランザクションの中で実行）。
# ``_meta.schema_version`` の更新は ``ensure_schema`` がやる。移行を書いたら schema.sql の版も上げること。
def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """v2: シンボル単位の重要度の内部表 ``_symbol_rank``（``index/rank.py``。値は次の差分索引の後に入る）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _symbol_rank ("
        " urn TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, score REAL NOT NULL,"
        " fan_in INTEGER NOT NULL DEFAULT 0)"
    )


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """v3: import の解決先の語幹の内部表 ``_import_probes``（``engine/reresolve.py``。値は各ファイルの次の取り込みで入る）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _import_probes ("
        " file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,"
        " stem TEXT NOT NULL, PRIMARY KEY (file_id, stem)) WITHOUT ROWID"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS _import_probes_stem ON _import_probes(stem)")


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """v4: LSP の exact の集合が完全だった目印の内部表 ``_exact_marks``（``index/exact_marks.py``。値は次に LSP から取ったときに入る）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _exact_marks ("
        " workspace_id TEXT NOT NULL, kind TEXT NOT NULL, urn TEXT NOT NULL,"
        " files_token TEXT NOT NULL, rows_fp TEXT NOT NULL,"
        " PRIMARY KEY (workspace_id, kind, urn)) WITHOUT ROWID"
    )


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """v5: 受け手の型が分からない属性呼び出しの内部表 ``_name_calls``（BUG-0071。``tools/_name_calls.py``）。

    値は取り込みのときにしか作れないので、関係を抽出する言語（Python / TS / JS）のファイルの ``sha256`` を空にして、次の差分走査で
    取り込み直させる（ファイルの中身は変わらない。``relations`` もそのとき作り直される）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _name_calls ("
        " file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,"
        " source_urn TEXT NOT NULL, name TEXT NOT NULL,"
        " call_site_line INTEGER NOT NULL, call_site_col INTEGER NOT NULL, guard_condition TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS _name_calls_name ON _name_calls(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS _name_calls_source ON _name_calls(source_urn)")
    conn.execute("CREATE INDEX IF NOT EXISTS _name_calls_file ON _name_calls(file_id)")
    conn.execute(
        "UPDATE files SET sha256 = '' WHERE language IN ('python', 'typescript', 'tsx', 'javascript', 'jsx')"
    )


def _migrate_5_to_6(conn: sqlite3.Connection) -> None:
    """v6: 受け手のクラスに無い（基底クラスから継承した）メンバーへの参照の内部表 ``_member_refs``（BUG-0075。``index/inherited.py``）。

    その参照は v5 までの取り込みで捨てられていて、取り込み直さないと戻らない。v5 と同じく関係を抽出する言語（Python / TS / JS）の
    ファイルの ``sha256`` を空にして、次の差分走査で取り込み直させる。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _member_refs ("
        " file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,"
        " source_urn TEXT NOT NULL, class_urn TEXT NOT NULL, name TEXT NOT NULL,"
        " relation_kind TEXT NOT NULL, use_kind TEXT,"
        " call_site_line INTEGER NOT NULL, call_site_col INTEGER NOT NULL, guard_condition TEXT,"
        " confidence_level TEXT NOT NULL, resolved_urn TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS _member_refs_class ON _member_refs(class_urn)")
    conn.execute("CREATE INDEX IF NOT EXISTS _member_refs_file ON _member_refs(file_id)")
    conn.execute(
        "UPDATE files SET sha256 = '' WHERE language IN ('python', 'typescript', 'tsx', 'javascript', 'jsx')"
    )


def _migrate_6_to_7(conn: sqlite3.Connection) -> None:
    """v7: ``_member_refs.via_super``（``super().save()`` を基底のメソッドに結ぶ。BUG-0081）。

    v6 までの取り込みは、``super()`` 経由の呼び出しを名前だけの呼び出し（``_name_calls``）にし、属性を辿った受け手の呼び出し
    （``url._uri_reference.copy_with()``）を同じファイルの同名のメソッドに結んでいた（BUG-0079）。どちらも取り込み直さないと直らないので、
    v5・v6 と同じく関係を抽出する言語（Python / TS / JS）のファイルの ``sha256`` を空にして、次の差分走査で取り込み直させる。
    """
    conn.execute("ALTER TABLE _member_refs ADD COLUMN via_super INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "UPDATE files SET sha256 = '' WHERE language IN ('python', 'typescript', 'tsx', 'javascript', 'jsx')"
    )


def _migrate_7_to_8(conn: sqlite3.Connection) -> None:
    """v8: 表は変えない。v7 までの取り込みは、受け手が型の分からない名前の呼び出し（注釈の無い引数 ``x`` の ``x.save()``）を、
    同じファイルの唯一の ``.save`` や import した同名に ``structural`` で結んでいた（BUG-0085）。取り込み直さないと誤った行が
    ``relations`` に残るので、v5〜v7 と同じく関係を抽出する言語（Python / TS / JS）のファイルの ``sha256`` を空にして、次の差分走査で
    取り込み直させる（その呼び出しは ``_name_calls`` に入り、T-06 / T-07 が名前で判定する）。
    """
    conn.execute(
        "UPDATE files SET sha256 = '' WHERE language IN ('python', 'typescript', 'tsx', 'javascript', 'jsx')"
    )


def _migrate_8_to_9(conn: sqlite3.Connection) -> None:
    """v9: ``_name_calls.use_kind``（型の分からない受け手の属性の読み書きも名前の一致に任せる。BUG-0095）、インスタンス属性の定義の
    内部表 ``_attr_defs``（名前の一致の候補。BUG-0095）、モジュール直下の変数の型の内部表 ``_var_types``（import した変数の
    ``settings.reload()`` を型のメンバーに結ぶ。BUG-0094）。

    値は取り込みのときにしか作れない（v8 までの取り込みは属性の読み書きを捨て、点つきのモジュールのパスの呼び出し〈BUG-0093〉を
    名前だけにしていた）ので、v5〜v8 と同じく関係を抽出する言語（Python / TS / JS）のファイルの ``sha256`` を空にして、次の差分走査で
    取り込み直させる。
    """
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(_name_calls)").fetchall()}
    if "use_kind" not in columns:  # 列が既にある DB（途中まで移した・新しい schema.sql で作った）でも流せるように
        conn.execute("ALTER TABLE _name_calls ADD COLUMN use_kind TEXT NOT NULL DEFAULT 'call'")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _attr_defs ("
        " file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,"
        " class_urn TEXT NOT NULL, name TEXT NOT NULL, line INTEGER NOT NULL, col INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS _attr_defs_name ON _attr_defs(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS _attr_defs_file ON _attr_defs(file_id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _var_types ("
        " file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,"
        " var_urn TEXT NOT NULL PRIMARY KEY, type_urn TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS _var_types_type ON _var_types(type_urn)")
    conn.execute("CREATE INDEX IF NOT EXISTS _var_types_file ON _var_types(file_id)")
    conn.execute(
        "UPDATE files SET sha256 = '' WHERE language IN ('python', 'typescript', 'tsx', 'javascript', 'jsx')"
    )


def _migrate_9_to_10(conn: sqlite3.Connection) -> None:
    """v10: Python のモジュール直下の import が束ねた名前の内部表 ``_reexports`` と、それをたどって整えたファイルの依存の内部表
    ``_reexport_deps``（``from pkg import flush`` の ``flush()`` を ``pkg/__init__.py`` が再び公開した定義元に結ぶ。BUG-0100）。

    束ねた名前は取り込みのときにしか作れず、v9 までの取り込みは名前空間パッケージの読みを外部にしていた（BUG-0101）ので、Python の
    ファイルの ``sha256`` を空にして、次の差分走査で取り込み直させる。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _reexports ("
        " file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,"
        " name TEXT NOT NULL, target_urn TEXT NOT NULL, kind TEXT NOT NULL, ord INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS _reexports_name ON _reexports(file_id, name)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _reexport_deps ("
        " file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,"
        " path TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (file_id, path, name)) WITHOUT ROWID"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS _reexport_deps_path ON _reexport_deps(path, name)")
    conn.execute("UPDATE files SET sha256 = '' WHERE language = 'python'")


def _migrate_10_to_11(conn: sqlite3.Connection) -> None:
    """v11: ``_reexports`` に TS / JS のモジュールが公開する名前（``import``・``star``・``export``。バレル ``index.ts`` の
    ``export { flush } from './helpers'`` を経由した呼び出しを定義元に結ぶ。BUG-0104）と、Python のリテラルでない ``__all__`` の印
    （``dynamic_all``。BUG-0105）を書く。Python の import する側の ``from helpers import *`` が持ち込んだ名前の仮の目標
    （``<このファイル>#name``。BUG-0105）も取り込みのときに作る。

    どれも取り込みのときにしか作れないので、TS / JS のファイル・``from x import *`` を持つ Python のファイル・``__all__`` を書くが
    リテラルの名前が無い Python のファイルの ``sha256`` を空にして、次の差分走査で取り込み直させる（ほかの Python のファイルは変わらない）。
    """
    conn.execute(
        "UPDATE files SET sha256 = '' WHERE language IN ('typescript', 'tsx', 'javascript', 'jsx')"
        " OR file_id IN (SELECT file_id FROM _reexports WHERE kind = 'star')"
        " OR (language = 'python' AND file_id IN (SELECT file_id FROM symbols WHERE qualified_name = '__all__')"
        " AND file_id NOT IN (SELECT file_id FROM _reexports WHERE kind = 'all'))"
    )


def _migrate_11_to_12(conn: sqlite3.Connection) -> None:
    """v12: TS の型の別名の構成の内部表 ``_type_aliases``（型の別名を注釈にした受け手の ``o.retry`` を構成のメンバーに結ぶ。BUG-0108）。
    TS / JS の既定の import（``import main from './helpers'``）を既定の export の束ね先に結ぶ（BUG-0109）。

    どちらも取り込みのときにしか作れない（v11 までの取り込みは既定の import の名前を解かず、別名の構成も書かなかった）ので、TS / JS の
    ファイルの ``sha256`` を空にして、次の差分走査で取り込み直させる（Python のファイルは変わらない）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _type_aliases ("
        " file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,"
        " alias_urn TEXT NOT NULL, op TEXT NOT NULL, ord INTEGER NOT NULL, kind TEXT NOT NULL, target_urn TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS _type_aliases_alias ON _type_aliases(alias_urn)")
    conn.execute("CREATE INDEX IF NOT EXISTS _type_aliases_target ON _type_aliases(target_urn)")
    conn.execute("CREATE INDEX IF NOT EXISTS _type_aliases_file ON _type_aliases(file_id)")
    conn.execute("UPDATE files SET sha256 = '' WHERE language IN ('typescript', 'tsx', 'javascript', 'jsx')")


def _migrate_12_to_13(conn: sqlite3.Connection) -> None:
    """v13: TS の型リテラルのメンバー（``type KyOptions = { retry?: … }`` の ``KyOptions.retry``・入れ子の ``KyOptions.retry.limit``）を
    シンボルにし（BUG-0114）、受け手の注釈を先頭の名前に縮めずに読む（``r: Opts['retry']`` はメンバー ``Opts.retry`` の型、``Opts[]`` は
    配列。BUG-0113）。型注釈つきのメンバーの型の構成を ``_type_aliases`` に書く（表は変えない）。

    どれも取り込みのときにしか作れないので、TS / JS のファイルの ``sha256`` を空にして、次の差分走査で取り込み直させる（Python のファイルは
    変わらない）。
    """
    conn.execute("UPDATE files SET sha256 = '' WHERE language IN ('typescript', 'tsx', 'javascript', 'jsx')")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
    5: _migrate_5_to_6,
    6: _migrate_6_to_7,
    7: _migrate_7_to_8,
    8: _migrate_8_to_9,
    9: _migrate_9_to_10,
    10: _migrate_10_to_11,
    11: _migrate_11_to_12,
    12: _migrate_12_to_13,
}


def schema_sql() -> str:
    """パッケージに同梱された ``schema.sql`` の全文。"""
    return resources.files("lucianke.index").joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")


def parse_schema_version(sql: str) -> int:
    """``schema.sql`` の ``-- schema_version: N`` を読む。無ければ ``ValueError``。"""
    match = _VERSION_RE.search(sql)
    if match is None:
        raise ValueError("schema.sql has no '-- schema_version: N' line")
    return int(match.group(1))


def current_schema_version() -> int:
    """このビルドが期待するスキーマ版。"""
    return parse_schema_version(schema_sql())


def ensure_schema(conn: sqlite3.Connection) -> SchemaAction:
    """DB を最新のスキーマにする（上の docstring）。書き込み接続で呼ぶ。トランザクション外で呼ぶこと。"""
    sql = schema_sql()
    target = parse_schema_version(sql)
    # 外部キーの切り替えはトランザクションの中では効かない。作り直しで DROP するので外に出して切る。
    fk_was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if fk_was_on:
        conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            action = _ensure_in_transaction(conn, sql, target)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        if fk_was_on:
            conn.execute("PRAGMA foreign_keys=ON")
    return action


def _ensure_in_transaction(conn: sqlite3.Connection, sql: str, target: int) -> SchemaAction:
    count = conn.execute(f"SELECT count(*) FROM sqlite_master WHERE {_NOT_INTERNAL}").fetchone()[0]
    if count == 0:
        _create(conn, sql, target, edit_seq=0)
        return SchemaAction.CREATED

    found = _stored_version(conn)
    if found == target:
        return SchemaAction.UNCHANGED

    if found is not None and found < target:
        version = found
        while version < target and version in MIGRATIONS:
            MIGRATIONS[version](conn)
            version += 1
            conn.execute("UPDATE _meta SET schema_version = ?", (version,))
        if version == target:
            return SchemaAction.MIGRATED

    edit_seq = _stored_edit_seq(conn)
    _drop_all(conn)
    _create(conn, sql, target, edit_seq=edit_seq)
    return SchemaAction.REBUILT


def _stored_version(conn: sqlite3.Connection) -> int | None:
    """``_meta.schema_version``。``_meta`` が無い・空・読めない DB は ``None``。"""
    try:
        row = conn.execute("SELECT schema_version FROM _meta LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return None
    return int(row[0]) if row is not None else None


def _stored_edit_seq(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT edit_seq FROM _meta LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0]) if row is not None else 0


def _split_statements(sql: str) -> Iterator[str]:
    """``executescript`` は進行中のトランザクションを COMMIT してしまうので、1 文ずつ流すために分ける。

    ``sqlite3.complete_statement`` は ``CREATE TRIGGER … BEGIN … END;`` の途中の ``;`` を文の終わりとしない。
    文の前のコメント行は次の文に付いたまま流れる（SQLite は無視する）。
    """
    lines: list[str] = []
    for line in sql.splitlines(keepends=True):
        lines.append(line)
        chunk = "".join(lines)
        if sqlite3.complete_statement(chunk):
            yield chunk
            lines.clear()


def _create(conn: sqlite3.Connection, sql: str, version: int, *, edit_seq: int) -> None:
    for statement in _split_statements(sql):
        conn.execute(statement)
    conn.execute(
        "INSERT INTO _meta (schema_version, data_version, edit_seq, lucianke_version, created_at)"
        " VALUES (?, 0, ?, ?, ?)",
        (version, edit_seq, __version__, datetime.now(UTC).isoformat(timespec="seconds")),
    )


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _drop_all(conn: sqlite3.Connection) -> None:
    """トリガ → ビュー → 仮想表（FTS5 の影の表ごと）→ 残りの表の順に全部落とす。索引は表と一緒に消える。"""
    objects = conn.execute(f"SELECT type, name, sql FROM sqlite_master WHERE {_NOT_INTERNAL}").fetchall()
    for kind in ("trigger", "view"):
        for type_, name, _sql in objects:
            if type_ == kind:
                conn.execute(f"DROP {kind.upper()} IF EXISTS {_quote(name)}")
    for type_, name, table_sql in objects:
        if type_ == "table" and (table_sql or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
            conn.execute(f"DROP TABLE IF EXISTS {_quote(name)}")
    # 仮想表を落とすと影の表も消えるので、残りは取り直す
    rest = conn.execute(f"SELECT name FROM sqlite_master WHERE type = 'table' AND {_NOT_INTERNAL}").fetchall()
    for (name,) in rest:
        conn.execute(f"DROP TABLE IF EXISTS {_quote(name)}")
