"""ツールが使う名前つきの読み取りクエリ（設計書 §7.6.3）。SQL 文字列はここに閉じる（MUST）。

すべて読み取り専用（``SELECT`` だけ）で、パラメータ化されている。``conn`` は ``ReaderPool`` から借りた接続
（またはテストの接続）。``conn.row_factory`` には依存しない。``core`` の型で返す。

位置の表記: DB の行・列は 1 始まり（公開面。仕様書 §5.10）。このモジュールの関数が受け渡す位置と、返す
``SymbolRecord`` / ``Position`` / ``Range`` は **内部表現（0 始まり）**（ADR-14）。``symbol_at`` の ``line`` /
``col`` も 0 始まり（ツール層で ``Position.from_external`` を通す）。
シンボルの範囲は DB に行しか無いので、``SymbolRecord.range`` は行全体を覆う範囲
（``Range.from_line_span_external``。``SymbolRecord.from_json`` と同じ）で返る。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final, Literal

from lucianke.core.model import (
    Confidence,
    FileInfo,
    Kind,
    Position,
    Range,
    RelPath,
    SymbolRecord,
    Tier,
)
from lucianke.core.urn import Urn, parse

__all__ = [
    "DiagnosticRow",
    "NameCallRow",
    "ReferenceRow",
    "count_references_to",
    "data_version",
    "diagnostics_for",
    "file_by_path",
    "files_by_paths",
    "files_fingerprint",
    "files_in_scope",
    "methods_named",
    "name_calls_from",
    "name_calls_named",
    "references_from",
    "references_to",
    "search_symbols_fts",
    "symbol_at",
    "symbol_by_urn",
    "symbol_parents",
    "symbols_by_qualified_suffix",
    "symbols_in_file",
]

MatchMode = Literal["fuzzy", "prefix", "exact"]
SEVERITY_ORDER: Final = ("error", "warning", "information", "hint")

_SYMBOL_COLUMNS: Final = (
    "s.urn, f.path, s.name, s.qualified_name, s.kind, s.modifiers_json, s.start_line, s.end_line,"
    " s.sel_line, s.sel_start_col, s.sel_end_col, s.signature, s.return_type, s.doc_summary, s.declared_in,"
    " s.confidence_level, s.scip_symbol"
)
_SYMBOL_SELECT: Final = f"SELECT {_SYMBOL_COLUMNS} FROM symbols s LEFT JOIN files f ON f.file_id = s.file_id"
# UNION ALL の外側で選び直す列名（``_SYMBOL_COLUMNS`` の結果列の名前と同じ並び）
_SYMBOL_OUTER: Final = (
    "urn, path, name, qualified_name, kind, modifiers_json, start_line, end_line, sel_line, sel_start_col,"
    " sel_end_col, signature, return_type, doc_summary, declared_in, confidence_level, scip_symbol"
)
_FILE_SELECT: Final = (
    "SELECT file_id, workspace_id, path, language, tier, sha256, line_count, byte_size, is_test,"
    " confidence_level, pagerank_score FROM files"
)


# --- 行 → core の型 ---------------------------------------------------------------------


@lru_cache(maxsize=512)
def _modifiers(mods_json: str) -> tuple[str, ...]:
    return tuple(json.loads(mods_json)) if mods_json else ()


def _symbol(row: Sequence[Any]) -> SymbolRecord:
    (
        urn_s,
        path,
        name,
        qualified,
        kind,
        mods,
        start,
        end,
        sel_line,
        sel_s,
        sel_e,
        sig,
        ret,
        doc,
        declared,
        conf,
        scip,
    ) = row
    urn = parse(str(urn_s))
    selection: Range | None = None
    if sel_line is not None and sel_s is not None and sel_e is not None:
        selection = Range(
            Position(int(sel_line) - 1, int(sel_s) - 1), Position(int(sel_line) - 1, int(sel_e) - 1)
        )
    return SymbolRecord(
        urn=urn,
        path=RelPath(str(path) if path is not None else urn.path),
        name=str(name),
        qualified_name=str(qualified),
        kind=Kind.parse(str(kind)),
        modifiers=_modifiers(str(mods)) if mods else (),
        range=Range.from_line_span_external(int(start), max(int(end), int(start))),
        selection=selection,
        signature=sig,
        return_type=ret,
        doc_summary=doc,
        declared_in=str(declared),
        confidence=Confidence(str(conf)),
        scip_symbol=scip,
    )


def _file(row: Sequence[Any]) -> FileInfo:
    file_id, ws, path, language, tier, sha, lines, size, is_test, conf, pagerank = row
    return FileInfo(
        file_id=int(file_id),
        workspace_id=str(ws),
        path=RelPath(str(path)),
        language=str(language),
        tier=Tier(int(tier)),
        sha256=str(sha),
        line_count=int(lines),
        byte_size=int(size),
        is_test=bool(is_test),
        confidence=Confidence(str(conf)),
        pagerank=None if pagerank is None else float(pagerank),
    )


# --- ファイル ---------------------------------------------------------------------------


def file_by_path(conn: sqlite3.Connection, workspace_id: str, path: str) -> FileInfo | None:
    """``files`` の 1 行。無ければ ``None``。"""
    row = conn.execute(_FILE_SELECT + " WHERE workspace_id = ? AND path = ?", (workspace_id, path)).fetchone()
    return None if row is None else _file(row)


_PATHS_CHUNK = 500  # SQLite の変数の上限（古い版で 999）に余裕を残す


def files_by_paths(conn: sqlite3.Connection, workspace_id: str, paths: Iterable[str]) -> dict[str, FileInfo]:
    """``paths`` の ``files`` の行をまとめて引く（``path -> FileInfo``。無いパスは入らない）。

    ファイルごとに ``file_by_path`` を呼ぶと、ヒットが散らばる検索で問い合わせが N 回になる（BUG-0027）ので、1 回（500 件ごと）で引く。
    """
    wanted = list(dict.fromkeys(paths))
    out: dict[str, FileInfo] = {}
    for i in range(0, len(wanted), _PATHS_CHUNK):
        chunk = wanted[i : i + _PATHS_CHUNK]
        marks = ",".join("?" * len(chunk))
        sql = _FILE_SELECT + f" WHERE workspace_id = ? AND path IN ({marks})"
        for row in conn.execute(sql, (workspace_id, *chunk)):
            info = _file(row)
            out[str(info.path)] = info
    return out


@lru_cache(maxsize=256)
def _glob_regex(glob: str) -> re.Pattern[str]:
    """``**``（``/`` をまたぐ）・``*``（``/`` をまたがない）・``?``・``[...]`` の glob を正規表現にする。"""
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob.startswith("**", i):
                i += 2
                if glob.startswith("/", i):  # '**/' は 0 個以上のディレクトリ
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = glob.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = glob[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out), re.DOTALL)


def files_in_scope(
    conn: sqlite3.Connection,
    workspace_id: str,
    globs: Sequence[str] = (),
    *,
    include_tests: bool = True,
) -> list[FileInfo]:
    """ワークスペースのファイルのうち ``globs`` のどれかに合うもの（空なら全部）。パス順。

    glob は ``/`` 区切りのルート相対パスに対して全体一致で判定する。
    """
    sql = _FILE_SELECT + " WHERE workspace_id = ?"
    if not include_tests:
        sql += " AND is_test = 0"
    rows = conn.execute(sql + " ORDER BY path", (workspace_id,)).fetchall()
    files = [_file(r) for r in rows]
    if not globs:
        return files
    patterns = [_glob_regex(g) for g in globs]
    return [f for f in files if any(p.fullmatch(f.path) for p in patterns)]


# --- シンボル ---------------------------------------------------------------------------


def symbol_by_urn(conn: sqlite3.Connection, urn: Urn | str) -> SymbolRecord | None:
    """URN の完全一致（仕様書 §5.3 規則 1）。外部シンボルも返す（``path`` は URN の path）。"""
    row = conn.execute(_SYMBOL_SELECT + " WHERE s.urn = ?", (str(urn),)).fetchone()
    return None if row is None else _symbol(row)


def symbols_in_file(conn: sqlite3.Connection, workspace_id: str, path: str) -> list[SymbolRecord]:
    """ファイルのシンボル。開始行の昇順、同じ開始行なら範囲の広い順（親が先）。"""
    rows = conn.execute(
        _SYMBOL_SELECT
        + " WHERE f.workspace_id = ? AND f.path = ? ORDER BY s.start_line, s.end_line DESC, s.urn",
        (workspace_id, path),
    ).fetchall()
    return [_symbol(r) for r in rows]


def symbol_parents(conn: sqlite3.Connection, workspace_id: str, path: str) -> dict[str, str | None]:
    """ファイルのシンボルの ``{urn: parent_urn}``（骨格の親子の組み立て用）。"""
    rows = conn.execute(
        "SELECT s.urn, s.parent_urn FROM symbols s JOIN files f ON f.file_id = s.file_id"
        " WHERE f.workspace_id = ? AND f.path = ?",
        (workspace_id, path),
    ).fetchall()
    return {u: p for u, p in rows}


def symbols_by_qualified_suffix(
    conn: sqlite3.Connection, workspace_id: str, path: str, name: str, *, limit: int | None = None
) -> list[SymbolRecord]:
    """``qualified_name`` の末尾一致（仕様書 §5.3 規則 2）。``name`` は ``name`` / ``Outer.name`` / …。

    ``qualified_name == name`` か、``qualified_name`` が ``'.' + name`` で終わるもの。大文字小文字を区別する。
    開始行の昇順。2 件以上なら呼び出し側が ``E_TARGET_AMBIGUOUS`` にする。
    """
    if not name:
        return []
    sql = (
        _SYMBOL_SELECT + " WHERE f.workspace_id = :ws AND f.path = :path AND (s.qualified_name = :name OR"
        " (length(s.qualified_name) > length(:name) AND substr(s.qualified_name, -length(:name)) = :name"
        " AND substr(s.qualified_name, -length(:name) - 1, 1) = '.'))"
        " ORDER BY s.start_line, s.end_line DESC, s.urn"
    )
    params: dict[str, object] = {"ws": workspace_id, "path": path, "name": name}
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return [_symbol(r) for r in conn.execute(sql, params).fetchall()]


def symbol_at(
    conn: sqlite3.Connection, workspace_id: str, path: str, line: int, col: int | None = None
) -> SymbolRecord | None:
    """``(line, col)``（0 始まり）を範囲に含む最内のシンボル（仕様書 §5.3 規則 3）。

    DB の範囲は行単位なので、行が ``start_line``〜``end_line`` に入るもののうち最も狭いものを選ぶ。
    同じ広さが並んだら、``col`` が名前トークンに乗っているものを、次に開始行が後のもの、深い入れ子を選ぶ。
    """
    line1 = line + 1
    rows = conn.execute(
        _SYMBOL_SELECT + " WHERE f.workspace_id = ? AND f.path = ? AND s.start_line <= ? AND s.end_line >= ?",
        (workspace_id, path, line1, line1),
    ).fetchall()
    if not rows:
        return None

    def key(row: Sequence[Any]) -> tuple[int, int, int, int]:
        start, end = int(row[6]), int(row[7])
        on_name = (
            col is not None
            and row[8] == line1
            and row[9] is not None
            and row[10] is not None
            and int(row[9]) <= col + 1 < int(row[10])
        )
        return (end - start, 0 if on_name else 1, -start, -str(row[3]).count("."))

    return _symbol(min(rows, key=key))


def files_fingerprint(conn: sqlite3.Connection, workspace_id: str) -> str:
    """ワークスペースの全ファイルの ``(path, sha256)`` の指紋（16 進）。ファイルの追加・削除・内容の変更で変わる。

    LSP の結果を ``relations`` に書き戻したあと「この集合は完全」と覚えておく目印の鮮度に使う（T-06・T-07 の 2 回目以降を
    PaaD から答えてよいかの判定）。索引に載っていないファイルの変更は見えない（索引が見るものだけが対象）。
    """
    digest = hashlib.sha1()
    for path, sha in conn.execute(
        "SELECT path, sha256 FROM files WHERE workspace_id = ? ORDER BY path", (workspace_id,)
    ):
        digest.update(f"{path}|{sha};".encode())
    return digest.hexdigest()


# --- 全文検索 ---------------------------------------------------------------------------

_TOKEN_RE: Final = re.compile(r"\w+")


def _fts_tokens(query: str) -> list[str]:
    return _TOKEN_RE.findall(query)


def _fts_phrase(token: str) -> str:
    return '"' + token.replace('"', '""') + '"*'


def search_symbols_fts(
    conn: sqlite3.Connection,
    workspace_id: str,
    query: str,
    match: MatchMode = "fuzzy",
    kinds: Iterable[Kind | str] | None = None,
    limit: int = 20,
) -> list[SymbolRecord]:
    """シンボルを名前などで探す（T-03）。ワークスペース内のシンボルだけ（外部シンボルは除く）。

    * ``exact``: ``name`` または ``qualified_name`` が ``query`` と完全一致（大文字小文字を区別する）。
    * ``prefix``: ``query`` の各語が ``name`` / ``qualified_name`` のどれかの語の前方一致（FTS5。語は英数字の連なり。
      ``process_charge`` は ``process`` と ``charge``）。
    * ``fuzzy``: 各語が ``name`` / ``qualified_name`` / ``signature`` / ``doc_summary`` のどれかの語の前方一致
      （FTS5）、または ``name`` が ``query`` を（大文字小文字を無視して）部分文字列として含む。

    並びは、名前が ``query`` と一致するもの、FTS5 の ``bm25``（名前の重み大）、名前の短さ、名前順。
    """
    limit = max(1, int(limit))
    query = query.strip()
    if not query:
        return []
    kind_sql, kind_params = _kind_clause(kinds)
    order = " ORDER BY (s.name = :q) DESC, (lower(s.name) = lower(:q)) DESC, "

    if match == "exact":
        sql = (
            _SYMBOL_SELECT
            + " WHERE f.workspace_id = :ws AND (s.name = :q OR s.qualified_name = :q)"
            + kind_sql
            + " ORDER BY s.qualified_name, s.urn LIMIT :limit"
        )
        params = {"ws": workspace_id, "q": query, "limit": limit, **kind_params}
        return [_symbol(r) for r in conn.execute(sql, params).fetchall()]

    tokens = _fts_tokens(query)
    if match == "prefix":
        if not tokens:
            return []
        fts = " AND ".join("{name qualified_name} : " + _fts_phrase(t) for t in tokens)
        sql = (
            "WITH hits AS (SELECT rowid AS rid, bm25(symbols_fts, 10.0, 5.0, 2.0, 1.0) AS score"
            " FROM symbols_fts WHERE symbols_fts MATCH :fts) "
            + _SYMBOL_SELECT
            + " JOIN hits h ON h.rid = s.rowid WHERE f.workspace_id = :ws"
            + kind_sql
            + order
            + "h.score, length(s.name), s.qualified_name, s.urn LIMIT :limit"
        )
        params = {"ws": workspace_id, "q": query, "fts": fts, "limit": limit, **kind_params}
        return [_symbol(r) for r in conn.execute(sql, params).fetchall()]

    if match != "fuzzy":
        raise ValueError(f"unknown match mode: {match!r}")
    fts_all = " AND ".join(_fts_phrase(t) for t in tokens)
    # FTS の一致（bm25 つき）と、名前の部分文字列の一致（bm25 なし）を UNION ALL で足してから並べる。
    # 以前は ``LEFT JOIN hits h ON h.rid = s.rowid`` で足していたが、ORDER BY があると SQLite が ``hits`` を自動索引なしの
    # 総なめ（``SCAN h``）にし、シンボル数 × 一致数の比較になった（1,800 シンボル・200 一致で 25 ms。BUG-0027）。
    name_branch = (
        f"SELECT {_SYMBOL_COLUMNS}, NULL AS score FROM symbols s LEFT JOIN files f ON f.file_id = s.file_id"
        " WHERE f.workspace_id = :ws AND instr(lower(s.name), lower(:q)) > 0" + kind_sql
    )
    if tokens:
        fts_branch = (
            f"SELECT {_SYMBOL_COLUMNS}, bm25(symbols_fts, 10.0, 5.0, 2.0, 1.0) AS score FROM symbols_fts"
            " JOIN symbols s ON s.rowid = symbols_fts.rowid LEFT JOIN files f ON f.file_id = s.file_id"
            " WHERE symbols_fts MATCH :fts AND f.workspace_id = :ws" + kind_sql
        )
        name_branch += " AND s.rowid NOT IN (SELECT rowid FROM symbols_fts WHERE symbols_fts MATCH :fts)"
        source = f"{fts_branch} UNION ALL {name_branch}"
    else:
        source = name_branch
    sql = (
        f"SELECT {_SYMBOL_OUTER} FROM ({source})"
        " ORDER BY (name = :q) DESC, (lower(name) = lower(:q)) DESC, (score IS NULL), score, length(name),"
        " qualified_name, urn LIMIT :limit"
    )
    params = {"ws": workspace_id, "q": query, "limit": limit, **kind_params}
    if tokens:
        params["fts"] = fts_all
    return [_symbol(r) for r in conn.execute(sql, params).fetchall()]


def _kind_clause(kinds: Iterable[Kind | str] | None) -> tuple[str, dict[str, object]]:
    """``AND s.kind IN (:k0, ...)`` と、そのパラメータ。``kinds`` が空・``None`` なら絞らない。"""
    if not kinds:
        return "", {}
    values = sorted({Kind.parse(k).value if isinstance(k, str) else k.value for k in kinds})
    params: dict[str, object] = {f"k{i}": v for i, v in enumerate(values)}
    return f" AND s.kind IN ({', '.join(':' + n for n in params)})", params


# --- 関係 -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferenceRow:
    """``relations`` の 1 行（1 参照 = 1 行）。``site`` は内部表現（0 始まり）。"""

    source_urn: str
    target_urn: str
    relation_kind: str
    path: RelPath | None  # 参照元のファイル（source_file_id）。無ければ None
    site: Position | None
    use_kind: str | None
    guard_condition: str | None
    confidence: Confidence
    is_test: bool


def _reference_where(
    target_urn: Urn | str,
    kinds: Iterable[str] | None,
    use_kinds: Iterable[str] | None,
    include_tests: bool,
) -> tuple[str, dict[str, object]]:
    params: dict[str, object] = {"target": str(target_urn)}
    where = " WHERE r.target_urn = :target"
    if kinds:
        names = []
        for i, k in enumerate(sorted(set(kinds))):
            params[f"rk{i}"] = k
            names.append(f":rk{i}")
        where += f" AND r.relation_kind IN ({', '.join(names)})"
    if use_kinds:
        names = []
        for i, k in enumerate(sorted(set(use_kinds))):
            params[f"uk{i}"] = k
            names.append(f":uk{i}")
        where += f" AND r.use_kind IN ({', '.join(names)})"
    if not include_tests:
        where += " AND COALESCE(f.is_test, 0) = 0"
    return where, params


def references_to(
    conn: sqlite3.Connection,
    target_urn: Urn | str,
    kinds: Iterable[str] | None = None,
    *,
    use_kinds: Iterable[str] | None = None,
    include_tests: bool = True,
    offset: int = 0,
    limit: int | None = None,
) -> list[ReferenceRow]:
    """``target_urn`` を指す関係。``kinds`` は ``relation_kind``（``CALLS`` など）、``use_kinds`` は ``use_kind``。

    並びは、参照元のパス、呼び出し位置、``relation_id`` の順（カーソルの ``offset`` が安定する）。
    """
    where, params = _reference_where(target_urn, kinds, use_kinds, include_tests)
    sql = (
        "SELECT r.source_urn, r.target_urn, r.relation_kind, f.path, r.call_site_line, r.call_site_col,"
        " r.use_kind, r.guard_condition, r.confidence_level, COALESCE(f.is_test, 0)"
        " FROM relations r LEFT JOIN files f ON f.file_id = r.source_file_id"
        + where
        + " ORDER BY f.path, r.call_site_line, r.call_site_col, r.relation_id LIMIT :limit OFFSET :offset"
    )
    params["limit"] = -1 if limit is None else max(0, int(limit))
    params["offset"] = max(0, int(offset))
    out: list[ReferenceRow] = []
    for src, tgt, kind, path, line, col, use, guard, conf, is_test in conn.execute(sql, params):
        site = Position(int(line) - 1, max(int(col or 1) - 1, 0)) if line is not None else None
        out.append(
            ReferenceRow(
                source_urn=src,
                target_urn=tgt,
                relation_kind=kind,
                path=None if path is None else RelPath(path),
                site=site,
                use_kind=use,
                guard_condition=guard,
                confidence=Confidence(conf),
                is_test=bool(is_test),
            )
        )
    return out


def references_from(
    conn: sqlite3.Connection,
    source_urn: Urn | str,
    kinds: Iterable[str] | None = None,
    *,
    use_kinds: Iterable[str] | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> list[ReferenceRow]:
    """``source_urn`` から出る関係（T-07 の outgoing 用。``references_to`` の逆向き）。

    ``path`` / ``site`` / ``is_test`` は **参照元（``source_urn`` の持ち主）のファイル** のもの（呼び出し箇所の位置）。
    並びは、呼び出し位置、``relation_id`` の順。
    """
    params: dict[str, object] = {"source": str(source_urn)}
    where = " WHERE r.source_urn = :source"
    for column, values, prefix in (("relation_kind", kinds, "rk"), ("use_kind", use_kinds, "uk")):
        if values:
            names = []
            for i, v in enumerate(sorted(set(values))):
                params[f"{prefix}{i}"] = v
                names.append(f":{prefix}{i}")
            where += f" AND r.{column} IN ({', '.join(names)})"
    sql = (
        "SELECT r.source_urn, r.target_urn, r.relation_kind, f.path, r.call_site_line, r.call_site_col,"
        " r.use_kind, r.guard_condition, r.confidence_level, COALESCE(f.is_test, 0)"
        " FROM relations r LEFT JOIN files f ON f.file_id = r.source_file_id"
        + where
        + " ORDER BY r.call_site_line, r.call_site_col, r.relation_id LIMIT :limit OFFSET :offset"
    )
    params["limit"] = -1 if limit is None else max(0, int(limit))
    params["offset"] = max(0, int(offset))
    out: list[ReferenceRow] = []
    for src, tgt, kind, path, line, col, use, guard, conf, is_test in conn.execute(sql, params):
        site = Position(int(line) - 1, max(int(col or 1) - 1, 0)) if line is not None else None
        out.append(
            ReferenceRow(
                source_urn=src,
                target_urn=tgt,
                relation_kind=kind,
                path=None if path is None else RelPath(path),
                site=site,
                use_kind=use,
                guard_condition=guard,
                confidence=Confidence(conf),
                is_test=bool(is_test),
            )
        )
    return out


def count_references_to(
    conn: sqlite3.Connection,
    target_urn: Urn | str,
    kinds: Iterable[str] | None = None,
    *,
    use_kinds: Iterable[str] | None = None,
    include_tests: bool = True,
) -> int:
    """``references_to`` と同じ条件の件数。"""
    where, params = _reference_where(target_urn, kinds, use_kinds, include_tests)
    sql = "SELECT count(*) FROM relations r LEFT JOIN files f ON f.file_id = r.source_file_id" + where
    return int(conn.execute(sql, params).fetchone()[0])


# --- 受け手の型が分からない属性呼び出し（内部表 _name_calls。BUG-0071） ---------------------------------


@dataclass(frozen=True, slots=True)
class NameCallRow:
    """``_name_calls`` の 1 行（``x.name(...)`` で ``x`` の型が分からない呼び出し）。``site`` は内部表現（0 始まり）。"""

    source_urn: str
    name: str
    path: RelPath
    language: str
    site: Position
    guard_condition: str | None
    is_test: bool


_NAME_CALL_SELECT: Final = (
    "SELECT nc.source_urn, nc.name, f.path, f.language, nc.call_site_line, nc.call_site_col, nc.guard_condition,"
    " f.is_test FROM _name_calls nc JOIN files f ON f.file_id = nc.file_id"
)


def _in_clause(column: str, values: Iterable[str], prefix: str, params: dict[str, object]) -> str:
    names = []
    for i, v in enumerate(sorted(set(values))):
        params[f"{prefix}{i}"] = v
        names.append(f":{prefix}{i}")
    return f" AND {column} IN ({', '.join(names)})" if names else " AND 0"


def _name_call_rows(conn: sqlite3.Connection, sql: str, params: dict[str, object]) -> list[NameCallRow]:
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:  # v5 より前の DB（移行前に読んだ）。無いものとして答える
        if "no such table" in str(exc):
            return []
        raise
    return [
        NameCallRow(
            source_urn=src,
            name=name,
            path=RelPath(path),
            language=lang,
            site=Position(int(line) - 1, max(int(col) - 1, 0)),
            guard_condition=guard,
            is_test=bool(is_test),
        )
        for src, name, path, lang, line, col, guard, is_test in rows
    ]


def name_calls_named(
    conn: sqlite3.Connection,
    workspace_id: str,
    name: str,
    languages: Iterable[str],
    *,
    include_tests: bool = True,
) -> list[NameCallRow]:
    """ワークスペースの ``languages`` のファイルにある、``name`` という名前の型不明の属性呼び出し。並びは（パス、行、列）。"""
    params: dict[str, object] = {"ws": workspace_id, "name": name}
    where = " WHERE f.workspace_id = :ws AND nc.name = :name" + _in_clause("f.language", languages, "lg", params)
    if not include_tests:
        where += " AND f.is_test = 0"
    sql = _NAME_CALL_SELECT + where + " ORDER BY f.path, nc.call_site_line, nc.call_site_col"
    return _name_call_rows(conn, sql, params)


def name_calls_from(conn: sqlite3.Connection, source_urn: Urn | str) -> list[NameCallRow]:
    """``source_urn`` の中にある型不明の属性呼び出し（T-07 の outgoing 用）。並びは（行、列）。"""
    sql = _NAME_CALL_SELECT + " WHERE nc.source_urn = :src ORDER BY nc.call_site_line, nc.call_site_col"
    return _name_call_rows(conn, sql, {"src": str(source_urn)})


def methods_named(
    conn: sqlite3.Connection, workspace_id: str, name: str, languages: Iterable[str]
) -> list[tuple[SymbolRecord, bool]]:
    """ワークスペースの ``languages`` のファイルで ``name`` という名前の ``Method``。``(シンボル, テストファイルか)``、（パス、行）順。"""
    params: dict[str, object] = {"ws": workspace_id, "name": name, "kind": Kind.METHOD.value}
    sql = (
        f"SELECT {_SYMBOL_COLUMNS}, COALESCE(f.is_test, 0) FROM symbols s JOIN files f ON f.file_id = s.file_id"
        " WHERE f.workspace_id = :ws AND s.name = :name AND s.kind = :kind"
        + _in_clause("f.language", languages, "lg", params)
        + " ORDER BY f.path, s.start_line, s.urn"
    )
    return [(_symbol(r[:-1]), bool(r[-1])) for r in conn.execute(sql, params).fetchall()]


# --- 診断 -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiagnosticRow:
    """``diagnostics`` の 1 行。``range`` は内部表現（0 始まり）。終端が無い診断は始点だけの空の範囲。"""

    path: RelPath
    severity: str
    source: str
    code: str | None
    range: Range
    message: str
    enclosing_urn: str | None
    confidence: Confidence


def diagnostics_for(
    conn: sqlite3.Connection,
    workspace_id: str,
    paths: Sequence[str] | None = None,
    min_severity: str = "hint",
    *,
    sources: Sequence[str] | None = None,
) -> list[DiagnosticRow]:
    """診断。``paths`` が ``None`` ならワークスペース全体。``min_severity`` 以上（``error`` が最も重い）だけ。

    並びは、パス、開始位置、重さ。
    """
    if min_severity not in SEVERITY_ORDER:
        raise ValueError(f"unknown severity: {min_severity!r}")
    allowed = SEVERITY_ORDER[: SEVERITY_ORDER.index(min_severity) + 1]
    params: dict[str, object] = {"ws": workspace_id}
    where = " WHERE f.workspace_id = :ws"
    names = []
    for i, sev in enumerate(allowed):
        params[f"s{i}"] = sev
        names.append(f":s{i}")
    where += f" AND d.severity IN ({', '.join(names)})"
    if paths is not None:
        if not paths:
            return []
        names = []
        for i, p in enumerate(paths):
            params[f"p{i}"] = p
            names.append(f":p{i}")
        where += f" AND f.path IN ({', '.join(names)})"
    if sources:
        names = []
        for i, s in enumerate(sources):
            params[f"src{i}"] = s
            names.append(f":src{i}")
        where += f" AND d.source IN ({', '.join(names)})"
    sql = (
        "SELECT f.path, d.severity, d.source, d.code, d.start_line, d.start_col, d.end_line, d.end_col,"
        " d.message, d.enclosing_urn, d.confidence_level FROM diagnostics d JOIN files f ON f.file_id = d.file_id"
        + where
        + " ORDER BY f.path, d.start_line, d.start_col, d.diag_id"
    )
    out: list[DiagnosticRow] = []
    for path, sev, source, code, sl, sc, el, ec, message, urn, conf in conn.execute(sql, params):
        start = Position(int(sl) - 1, int(sc) - 1)
        end = Position(int(el) - 1, int(ec) - 1) if el is not None and ec is not None else start
        out.append(
            DiagnosticRow(
                path=RelPath(path),
                severity=sev,
                source=source,
                code=code,
                range=Range(start, max(start, end)),
                message=message,
                enclosing_urn=urn,
                confidence=Confidence(conf),
            )
        )
    return out


# --- _hover_cache -----------------------------------------------------------------------


def cached_hover(conn: sqlite3.Connection, urn: str, sha256: str) -> str | None:
    """``_hover_cache`` の行（定義ファイルの内容 ``sha256`` が同じもの）の hover 文。無ければ ``None``。

    書き込みは ``ingest.store_hover``。内部テーブルなので公開面（``query_project_db``）には出ない。
    """
    row = conn.execute(
        "SELECT hover_text FROM _hover_cache WHERE urn = ? AND sha256 = ?", (urn, sha256)
    ).fetchone()
    return None if row is None else str(row[0])


# --- data_version -----------------------------------------------------------------------


def data_version(conn: sqlite3.Connection) -> int:
    """``_meta.data_version``（ADR-8）。カーソルとキャッシュの失効判定に使う。"""
    row = conn.execute("SELECT data_version FROM _meta LIMIT 1").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("_meta has no row (schema not initialized)")
    return int(row[0])
