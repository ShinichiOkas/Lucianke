"""公開クエリエンジン（仕様書 T-20・§9.5・§13.2、設計書 §7.6.4・§9.4、S-7 の実測）。

``query_project_db`` が PaaD に対して読み取り専用の SQL を流すための実行エンジン。MCP の引数モデルと描画は持たない。

防御は 4 段（S-7 の結論を反映）:

1. 静的検査 (``check_statement``): 長さ・単文・先頭が ``SELECT`` / ``WITH``・禁止語。
2. ``mode=ro`` の URI で開く。
3. ``PRAGMA query_only = ON``。
4. authorizer（公開テーブル以外の参照・書き込み・``ATTACH``・``PRAGMA``・禁止関数を拒否）。

DB 本体への書き込みは 2〜4 のどれか 1 つが欠けても拒否される（``tests/invariants/test_public_db_readonly.py``）。
ただし ``ATTACH`` による任意パスへの 0 バイトファイル作成は 2・3 では防げず、4 と 1 だけが止める（S-7 項目 2）。
``temp_grep_matches`` の作成と投入は ``query_only`` を入れる **前** に済ませる（S-7 項目 4。``setup_phase`` は不要）。

``index`` 層は ``core`` にしか依存しないため、grep の走査は呼び出し側が渡す ``roots`` と ``exclude`` で行う。
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import posixpath
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import (
    DEGRADED_GREP_MS,
    GREP_MATCHES_MAX,
    MAX_FILE_BYTES,
    QUERY_LENGTH_MAX,
    QUERY_ROWS_DEFAULT,
    QUERY_ROWS_MAX,
    QUERY_TIMEOUT_DEFAULT_MS,
    QUERY_TIMEOUT_MAX_MS,
    REGEX_PATTERN_MAX_CHARS,
    SNIPPET_MAX_CHARS,
)

# --- 公開の範囲 -----------------------------------------------------------------------------

TEMP_GREP_TABLE: Final = "temp_grep_matches"

#: 公開 10 テーブル、``symbols_fts``、grep の一時テーブル（仕様書 §9.5）。
PUBLIC_TABLES: Final = frozenset(
    {
        "workspaces", "files", "symbols", "relations", "dataflows", "diagnostics",
        "contracts", "git_commits", "git_symbol_changes", "git_co_changes",
        "symbols_fts", TEMP_GREP_TABLE,
    }
)  # fmt: skip

# FTS5 は MATCH のたびに自分の影テーブルを読む。authorizer は内側の文にも呼ばれる（S-7 項目 3）。
_FTS_SHADOW: Final = frozenset(
    {"symbols_fts_config", "symbols_fts_data", "symbols_fts_docsize", "symbols_fts_idx"}
)
# JSON の表値関数は READ(table=関数名) として届く。
_TABLE_FUNCTIONS: Final = frozenset({"json_each", "json_tree"})
_READ_OK: Final = PUBLIC_TABLES | _FTS_SHADOW | _TABLE_FUNCTIONS
# 列を読まない参照（count(*)、CTE の別名）は ``READ(名, '', None)`` で届く。非公開の実在名とこの接頭辞だけを拒否する。
_HIDDEN_PREFIXES: Final = ("_", "sqlite_", "pragma_", "dbstat")
_FUNC_DENY: Final = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})
#: 静的検査の禁止語（仕様書 T-20 手順 1）。引用符・コメントの外にある語だけを見る。
_FORBIDDEN_WORDS: Final = frozenset(
    {"attach", "detach", "pragma", "vacuum", "load_extension", "writefile", "readfile"}
)

_OK: Final = sqlite3.SQLITE_OK
_DENY: Final = sqlite3.SQLITE_DENY
# 拒否理由に action 名を出すための表（数値のままだと ATTACH=24 が SQLITE_FORMAT と衝突して読めない）
_ACTION_NAMES: Final = {
    getattr(sqlite3, n): n.removeprefix("SQLITE_")
    for n in dir(sqlite3)
    if n.startswith(("SQLITE_CREATE_", "SQLITE_DROP_", "SQLITE_ALTER_"))
    or n in {"SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_PRAGMA",
             "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT", "SQLITE_ANALYZE", "SQLITE_REINDEX"}
}  # fmt: skip

_VALUE_MAX_BYTES: Final = (
    16 * 1024 * 1024
)  # 1 値・1 行の上限（S-7: 12 MiB の doc_full は読め、zeroblob(2e9) は即エラー）
_PROGRESS_OPS: Final = 1000  # progress handler を呼ぶ VM 命令数の間隔（S-7 項目 5: 100〜1 万で誤差 0.1 ms）
_BUSY_TIMEOUT_S: Final = 1.0
_CONFIDENCE_ORDER: Final = ("exact", "structural", "heuristic", "lexical_fallback")

#: ``open_public`` の防御層。``layers`` で層を減らせるのは層別の検査（テスト）のためだけ。
ALL_LAYERS: Final = frozenset({"ro", "query_only", "authorizer"})


# --- 結果の型 -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueryResult:
    """``run_query`` の結果（仕様書 T-20 の ``data``）。

    Attributes:
        columns: ``(name, decl_type)``。``decl_type`` は SQLite が報告する宣言型（式の列は ``None``）。Python の
            ``sqlite3`` は ``sqlite3_column_decltype`` を公開しないので、結果の列名が読み取った公開テーブルの列名と
            一致するときだけスキーマの宣言型を返す近似である（``SELECT upper(name) AS name`` も ``TEXT`` と出る）。
        rows: 行（``limit`` 件まで）。
        row_count: 返した行数（``limit`` 以下）。
        truncated: ``limit + 1`` 行目が存在したか。
        elapsed_ms: 実行（``execute`` から最後の ``fetch`` まで）の時間。``timeout_ms`` 以下であることが保証される。
            grep の走査と接続を開く時間は含まない。
        grep_fallback: ``{pattern, matches, truncated, scope_paths}``。grep 指定が無ければ ``None``。
        plan: ``explain`` のときの ``EXPLAIN QUERY PLAN`` の行 ``{id, parent, detail}``。
        confidence_breakdown: ``confidence_level`` 列があるときの水準別の行数（水準の高い順）。無ければ ``None``。
        tables_read: 文が実際に読んだ公開テーブルの名前（昇順）。``temp_grep_matches`` を含めば降格扱い（手順 8）。
    """

    columns: list[tuple[str, str | None]]
    rows: list[tuple[Any, ...]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    grep_fallback: dict[str, Any] | None = None
    plan: list[dict[str, Any]] | None = None
    confidence_breakdown: dict[str, int] | None = None
    tables_read: tuple[str, ...] = ()


class QueryTimeout(LuciankeError):
    """``E_TIMEOUT``。取得済みの行を ``result`` に持つ（ツール層が ``ToolOutput`` に詰めて ``partial`` にする）。"""

    result: QueryResult

    def __init__(self, message: str, result: QueryResult, *, timeout_ms: int) -> None:
        super().__init__(
            ErrorCode.E_TIMEOUT,
            message,
            details={"partial": True, "timeout_ms": timeout_ms, "elapsed_ms": result.elapsed_ms},
        )
        self.result = result


def _rejected(reason: str, message: str, **details: Any) -> LuciankeError:
    return LuciankeError(ErrorCode.E_QUERY_REJECTED, message, details={"reason": reason, **details})


def _invalid(message: str, **details: Any) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, message, details=details)


# --- 静的検査（手順 1） -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str  # word | quoted | number | bind | semi | punct
    text: str


def _tokenize(sql: str) -> list[_Tok]:
    """コメントを捨て、引用符（``'`` ``"`` `` ` `` ``[]``）の中身を 1 トークンにまとめる最小の字句分割。"""
    toks: list[_Tok] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:  # '' / "" / `` は引用符の連続（エスケープ）
                        j += 2
                        continue
                    break
                j += 1
            toks.append(_Tok("quoted", sql[i : j + 1]))
            i = j + 1
        elif c == "[":
            j = sql.find("]", i + 1)
            j = n - 1 if j < 0 else j
            toks.append(_Tok("quoted", sql[i : j + 1]))
            i = j + 1
        elif c.isalpha() or c == "_" or ord(c) > 127:
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "_$" or ord(sql[j]) > 127):
                j += 1
            toks.append(_Tok("word", sql[i:j]))
            i = j
        elif c.isdigit():
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "._"):
                j += 1
            toks.append(_Tok("number", sql[i:j]))
            i = j
        elif c in ":@$" and i + 1 < n and (sql[i + 1].isalnum() or sql[i + 1] == "_"):
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            toks.append(_Tok("bind", sql[i:j]))
            i = j
        elif c == "?":
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            toks.append(_Tok("bind", sql[i:j]))
            i = j
        elif c == ";":
            toks.append(_Tok("semi", c))
            i += 1
        else:
            toks.append(_Tok("punct", c))
            i += 1
    return toks


@dataclass(frozen=True, slots=True)
class _Analysis:
    named_binds: frozenset[
        str
    ]  # ``:name`` / ``@name`` / ``$name`` の name（Python は先頭の記号を除いて辞書を引く）
    positional_binds: bool  # ``?`` / ``?NNN`` がある


def _analyze(sql: str) -> _Analysis:
    """手順 1 の静的検査。通れば束縛名を返す。"""
    if not isinstance(sql, str):
        raise _invalid("query must be a string", param="query")
    if len(sql.encode("utf-8", "replace")) > QUERY_LENGTH_MAX:
        raise _invalid(f"query is longer than {QUERY_LENGTH_MAX} bytes", param="query")
    if "\x00" in sql:
        raise _rejected(
            "syntax_error", "query contains a NUL character", sqlite_message="embedded null character"
        )
    toks = _tokenize(sql)
    if toks and toks[-1].kind == "semi":  # 末尾の ``;`` は 1 つだけ許す（``SELECT 1;;`` は複文。S-7 項目 6）
        toks = toks[:-1]
    if any(t.kind == "semi" for t in toks):
        raise _rejected("multiple_statements", "exactly one SELECT or WITH statement is allowed")
    if not toks or toks[0].kind != "word" or toks[0].text.lower() not in {"select", "with"}:
        raise _rejected("not_select", "the statement must start with SELECT or WITH")
    for t in toks:
        if t.kind == "word" and t.text.lower() in _FORBIDDEN_WORDS:
            raise _rejected("forbidden_keyword", f"{t.text.upper()} is not allowed", keyword=t.text.lower())
    named = frozenset(t.text[1:] for t in toks if t.kind == "bind" and t.text[0] in ":@$")
    positional = any(t.kind == "bind" and t.text[0] == "?" for t in toks)
    return _Analysis(named, positional)


def check_statement(sql: str) -> None:
    """静的検査（長さ・単文・``SELECT`` / ``WITH`` で始まる・禁止語）。違反は ``LuciankeError`` を送出する。

    長さの超過は ``E_INVALID_ARGUMENT``、それ以外は ``E_QUERY_REJECTED``（``details.reason`` は
    ``multiple_statements`` / ``not_select`` / ``forbidden_keyword`` / ``syntax_error``）。
    引用符とコメントの中の語は見ない（``WHERE name = 'PRAGMA'`` は通る）。
    """
    _analyze(sql)


_BindValue = str | int | float | bytes | None


def _check_params(params: Mapping[str, Any] | None, analysis: _Analysis) -> dict[str, _BindValue]:
    """手順 4: ``params`` のキーと ``:name`` の対応。"""
    if analysis.positional_binds:
        raise _invalid("positional placeholders (?) are not supported; use :name with params", param="query")
    given = dict(params or {})
    for key, value in given.items():
        if not isinstance(key, str):
            raise _invalid("params keys must be strings", param="params")
        if key not in analysis.named_binds:
            raise _invalid(f"params key {key!r} does not appear in query as :{key}", param=key)
        if value is not None and not isinstance(value, str | int | float | bytes):
            raise _invalid(f"params value for {key!r} must be string, number, bytes or null", param=key)
        if isinstance(value, int) and not -(2**63) <= value < 2**63:
            raise _invalid(f"params value for {key!r} does not fit in 64 bits", param=key)
    missing = sorted(analysis.named_binds - given.keys())
    if missing:
        raise _invalid(f"query binds :{missing[0]} but params has no such key", param=missing[0])
    return given


# --- 接続と authorizer（手順 2） ---------------------------------------------------------------


class Guard:
    """authorizer と progress handler の状態。1 接続 1 インスタンス。"""

    def __init__(self) -> None:
        self.deadline = float(
            "inf"
        )  # ``run_query`` が実行の直前に起算する（接続を開く時間・grep の投入時間を含めない）
        self.timed_out = False
        self.hidden: frozenset[str] = frozenset()  # DB に実在する非公開のテーブル・ビュー名
        self.denied: tuple[str, str | None] | None = None  # 最初に拒否した (種別, 対象)
        self.tables_read: set[str] = set()
        self.columns_read: set[tuple[str, str]] = set()  # (テーブル, 列)。小文字
        self.decl_types: dict[tuple[str, str], str] = {}  # (テーブル, 列) → 宣言型。小文字

    def authorize(
        self, action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None
    ) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return _OK
        if action == sqlite3.SQLITE_READ:  # arg1=表名, arg2=列名, dbname='main'|'temp'|None
            table = arg1 or ""
            if table in _READ_OK and dbname in (None, "main", "temp"):
                if table in PUBLIC_TABLES:
                    self.tables_read.add(table)
                    if arg2:
                        self.columns_read.add((table, arg2.lower()))
                return _OK
            # count(*) や SELECT 1 のように列を読まない参照は arg2='' かつ dbname=None で届く。CTE・サブクエリの
            # 別名も同じ形で届く（S-7 項目 3）。実在の非公開オブジェクトの名前だけを拒否し、それ以外は別名として通す。
            if (
                arg2 == ""
                and dbname is None
                and table not in self.hidden
                and not table.startswith(_HIDDEN_PREFIXES)
            ):
                return _OK
            return self._deny("non_public_object", table)
        if action == sqlite3.SQLITE_FUNCTION:  # arg2=関数名
            if (arg2 or "").lower() in _FUNC_DENY:
                return self._deny("forbidden_keyword", arg2)
            return _OK
        # PRAGMA は引数の無い読み取りのうち data_version（FTS5 が内部で発行する。拒否すると MATCH が失敗する）と
        # query_only だけ許す（仕様書 §9.5）。値を設定する形 ``PRAGMA query_only = OFF`` は arg2 が非 None で拒否される。
        if action == sqlite3.SQLITE_PRAGMA and arg1 in ("data_version", "query_only") and arg2 is None:
            return _OK
        return self._deny("forbidden_keyword", f"{_ACTION_NAMES.get(action, action)} {arg1}")

    def _deny(self, kind: str, target: str | None) -> int:
        if self.denied is None:
            self.denied = (kind, target)
        return _DENY

    def check_deadline(self) -> int:
        """progress handler。非 0 を返すと文が中断される（``OperationalError: interrupted``）。"""
        if time.perf_counter() > self.deadline:  # monotonic は Windows の 3.12 で約 15.6 ms 刻み（S-7）
            self.timed_out = True
            return 1
        return 0


class PublicConnection(sqlite3.Connection):
    """``open_public`` が返す接続。``guard`` に拒否・期限・読み取りの記録がある。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.guard = Guard()


def open_public(
    db_path: Path,
    *,
    setup: Callable[[sqlite3.Connection], None] | None = None,
    layers: frozenset[str] = ALL_LAYERS,
) -> PublicConnection:
    """読み取り専用の公開接続を開く（S-7 の「使うコードの型」）。

    ``mode=ro`` の URI → ``setup``（grep の一時テーブルの作成と投入。``query_only`` の前でないと作れない）→
    ``PRAGMA query_only = ON`` → 巨大値の上限 → authorizer → progress handler の順。
    ``layers`` は層別の検査（テスト）用。本番は既定の 3 層のまま使う。
    WAL の DB を ``mode=ro`` で開くには ``-shm`` を作れること（同じディレクトリへの書き込み権限）が要る。
    ``immutable=1`` は書き手の新しい行を見ないので使わない（S-7 項目 1）。
    """
    uri = db_path.resolve().as_uri() + (
        "?mode=ro" if "ro" in layers else ""
    )  # 空白・日本語・# は as_uri が符号化する
    try:
        con = sqlite3.connect(
            uri, uri=True, isolation_level=None, timeout=_BUSY_TIMEOUT_S, factory=PublicConnection
        )  # 自動コミット。timeout は busy_timeout
    except sqlite3.Error as exc:
        raise LuciankeError(
            ErrorCode.E_INTERNAL,
            f"cannot open the index database: {exc}",
            details={"sqlite_message": str(exc)},
        ) from exc
    guard = con.guard
    try:
        if setup is not None:
            setup(con)
        if "query_only" in layers:
            con.execute("PRAGMA query_only = ON")
            if con.execute("PRAGMA query_only").fetchone()[0] != 1:  # 閉じる側に倒す
                raise LuciankeError(ErrorCode.E_INTERNAL, "failed to enable query_only")
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _VALUE_MAX_BYTES)
        con.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, QUERY_LENGTH_MAX)
        names = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master UNION SELECT name FROM sqlite_temp_master")
        }
        guard.hidden = frozenset(names) - _READ_OK
        guard.decl_types = _declared_types(con)
        if "authorizer" in layers:
            con.set_authorizer(guard.authorize)
        con.set_progress_handler(guard.check_deadline, _PROGRESS_OPS)
    except BaseException:
        con.close()
        raise
    return con


def _declared_types(con: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """公開テーブルの (テーブル, 列) → 宣言型。authorizer を付ける前に 1 回だけ引く。"""
    sql = "SELECT m.name, p.name, p.type FROM {master} m JOIN pragma_table_xinfo(m.name) p WHERE m.type = 'table'"
    out: dict[tuple[str, str], str] = {}
    for master in ("sqlite_master", "sqlite_temp_master"):
        for table, column, decl in con.execute(sql.format(master=master)):
            if table in PUBLIC_TABLES and decl:
                out[(table, column.lower())] = decl
    return out


# --- grep の一時テーブル（手順 3） -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GrepSpec:
    """``auto_fallback_grep_pattern`` の指定。

    Attributes:
        pattern: 正規表現。単語境界つき（前後が単語構成文字でない）で一致を探す。
        roots: 走査するワークスペースのルート。``snippet`` の ``path`` はルート相対（``/`` 区切り）。
        scope_paths: ルート相対（またはルート配下の絶対）のパスの制限。``None`` なら全体。
        exclude: ``Path``（絶対）を受け取り、``True`` なら除外（ディレクトリは枝ごと除外）。``.gitignore`` と
            ``exclude_globs`` の適用は呼び出し側が作る。
    """

    pattern: str
    roots: list[Path]
    scope_paths: list[str] | None = None
    exclude: Callable[[Path], bool] | None = None


@dataclass(frozen=True, slots=True)
class GrepScan:
    rows: list[tuple[str, int, str]]  # (path, line, snippet)
    truncated: bool
    scope_paths: list[str] | None


_RE_CONSTANTS: Any = importlib.import_module("re._constants")
_RE_PARSER: Any = importlib.import_module("re._parser")
_REPEATS: Final = (_RE_CONSTANTS.MAX_REPEAT, _RE_CONSTANTS.MIN_REPEAT, _RE_CONSTANTS.POSSESSIVE_REPEAT)
_MAXREPEAT: Final = _RE_CONSTANTS.MAXREPEAT
_INLINE_FLAGS: Final = re.compile(r"\(\?([aimsux]+)\)")
_GREP_LINE_MAX_CHARS: Final = (
    4000  # これより長い行は先頭だけ走査する（多項式的な暴走の上限。圧縮された生成物の対策）
)
_GREP_BINARY_PROBE: Final = 4096


def _nested_quantifier(items: Any, in_unbounded: bool = False, in_multi: bool = False) -> bool:
    """``(a+)+`` のように、繰り返しの中で更に繰り返す（指数的に遅くなりうる）形か。"""
    for op, av in items:
        if op in _REPEATS:
            hi, body = av[1], av[2]
            if hi > 1 and (in_unbounded or (hi == _MAXREPEAT and in_multi)):
                return True
            if _nested_quantifier(body, in_unbounded or hi == _MAXREPEAT, in_multi or hi > 1):
                return True
        elif op == _RE_CONSTANTS.SUBPATTERN:
            if _nested_quantifier(av[3], in_unbounded, in_multi):
                return True
        elif op == _RE_CONSTANTS.BRANCH:
            if any(_nested_quantifier(b, in_unbounded, in_multi) for b in av[1]):
                return True
        elif op in (_RE_CONSTANTS.ASSERT, _RE_CONSTANTS.ASSERT_NOT):
            if _nested_quantifier(av[1], in_unbounded, in_multi):
                return True
        elif op == _RE_CONSTANTS.ATOMIC_GROUP:
            if _nested_quantifier(av, in_unbounded, in_multi):
                return True
        elif op == _RE_CONSTANTS.GROUPREF_EXISTS and (
            _nested_quantifier(av[1], in_unbounded, in_multi)
            or (av[2] is not None and _nested_quantifier(av[2], in_unbounded, in_multi))
        ):
            return True
    return False


def compile_grep_pattern(pattern: str) -> re.Pattern[str]:
    """パターンを検査して、単語境界つき（``grep -w`` 相当）の正規表現にコンパイルする。

    ``re`` には評価時間の上限が無いので、長さの上限（1,000 文字）とネストした量化子の拒否で代える（設計書 §9.4）。
    違反・構文エラーは ``E_INVALID_ARGUMENT``。
    """
    if not isinstance(pattern, str) or not pattern:
        raise _invalid("grep pattern must be a non-empty string", param="auto_fallback_grep_pattern")
    if len(pattern) > REGEX_PATTERN_MAX_CHARS:
        raise _invalid(
            f"grep pattern is longer than {REGEX_PATTERN_MAX_CHARS} characters",
            param="auto_fallback_grep_pattern",
        )
    flags = ""
    body = pattern
    if (
        m := _INLINE_FLAGS.match(pattern)
    ) is not None:  # 先頭の ``(?i)`` は包みの外へ出す（Python は先頭以外を拒否する）
        flags, body = m.group(1), pattern[m.end() :]
    prefix = f"(?{flags})" if flags else ""
    try:
        parsed = _RE_PARSER.parse(body, re.compile(prefix).flags if prefix else 0)
        if _nested_quantifier(parsed):
            raise _invalid(
                "grep pattern has nested quantifiers (e.g. (a+)+), which can take exponential time",
                param="auto_fallback_grep_pattern",
            )
        return re.compile(prefix + r"(?<!\w)(?:" + body + r")(?!\w)")
    except (re.error, RecursionError, OverflowError) as exc:
        raise _invalid(f"invalid grep pattern: {exc}", param="auto_fallback_grep_pattern") from exc


def _normalize_scopes(scope_paths: list[str], roots: list[Path]) -> list[tuple[Path, str]]:
    """``scope_paths`` を (ルート, ルート相対パス) の対にする。ルート外・``..`` は ``E_INVALID_ARGUMENT``。"""
    out: list[tuple[Path, str]] = []
    for raw in scope_paths:
        text = raw.replace("\\", "/")
        placed = False
        for root in roots:
            if PurePosixPath(text).is_absolute() or (len(text) > 1 and text[1] == ":"):
                try:
                    rel = Path(raw).resolve().relative_to(root)
                except ValueError:
                    continue
                out.append((root, rel.as_posix()))
                placed = True
                continue
            norm = posixpath.normpath(text)
            if norm == ".." or norm.startswith("../"):
                continue
            out.append((root, "" if norm == "." else norm))
            placed = True
        if not placed:
            raise _invalid(
                f"grep_scope_paths entry is outside the workspace roots: {raw!r}", param="grep_scope_paths"
            )
    return out


def _snippet(line: str, match_start: int) -> str:
    """``SNIPPET_MAX_CHARS`` 文字以内。長い行は一致の周辺を取り、切った側に ``…`` を付ける。"""
    text = line.strip()
    if len(text) <= SNIPPET_MAX_CHARS:
        return text
    pos = max(0, match_start - (len(line) - len(line.lstrip())))
    budget = SNIPPET_MAX_CHARS - 2  # 両端に ``…`` が付いても収まる
    start = max(0, min(pos - 40, len(text) - budget))
    end = start + budget
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def scan_grep(spec: GrepSpec) -> GrepScan:
    """``spec`` で決まる範囲を走査して一致行を返す。決定論的（パス昇順・行昇順）。

    マッチは ``GREP_MATCHES_MAX`` 件まで。超えたとき、または ``DEGRADED_GREP_MS`` の走査時間を超えたとき
    ``truncated=True``。シンボリックリンクは辿らない。5 MiB を超えるファイルとバイナリは読まない。
    """
    pattern = compile_grep_pattern(spec.pattern)
    roots = [Path(r).resolve() for r in spec.roots]
    scopes = _normalize_scopes(spec.scope_paths, roots) if spec.scope_paths is not None else None
    deadline = time.perf_counter() + DEGRADED_GREP_MS / 1000
    rows: list[tuple[str, int, str]] = []
    truncated = False
    seen: set[Path] = set()
    exclude = spec.exclude

    def starts() -> list[tuple[Path, Path]]:
        if scopes is None:
            return [(r, r) for r in roots]
        return [(r, (r / rel).resolve()) for r, rel in scopes]

    def files_under(root: Path, start: Path):
        if start != root and not start.is_relative_to(root):  # シンボリックリンクでルートの外へ出る指定
            return
        if start.is_file():
            if not (exclude and exclude(start)):
                yield start
            return
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            here = Path(dirpath)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d != ".git" and not (here / d).is_symlink() and not (exclude and exclude(here / d))
            )
            for name in sorted(filenames):
                p = here / name
                if p.is_symlink() or (exclude and exclude(p)):
                    continue
                yield p

    def scan_file(root: Path, path: Path) -> bool:
        """上限に達したら ``True``（走査を止める）。"""
        nonlocal truncated
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return False
            data = path.read_bytes()
        except OSError:
            return False  # 走査中に消えた・読めないファイルは飛ばす
        if b"\x00" in data[:_GREP_BINARY_PROBE]:
            return False
        rel = path.relative_to(root).as_posix()
        for lineno, raw in enumerate(data.decode("utf-8", errors="replace").split("\n"), start=1):
            if lineno % 1024 == 0 and time.perf_counter() > deadline:
                truncated = True
                return True
            line = raw.rstrip("\r")
            m = pattern.search(line[:_GREP_LINE_MAX_CHARS])
            if m is None:
                continue
            if len(rows) >= GREP_MATCHES_MAX:  # 上限 +1 件目が見つかったので切る
                truncated = True
                return True
            rows.append((rel, lineno, _snippet(line, m.start())))
        return False

    done = False
    for root, start in starts():
        if done:
            break
        for path in files_under(root, start):
            if path in seen:
                continue
            seen.add(path)
            if time.perf_counter() > deadline:
                truncated = done = True
                break
            if scan_file(root, path):
                done = True
                break
    return GrepScan(rows, truncated, list(spec.scope_paths) if spec.scope_paths is not None else None)


def _install_grep(con: sqlite3.Connection, rows: list[tuple[str, int, str]]) -> None:
    """接続ローカルの TEMP テーブル ``temp_grep_matches`` を作って投入する。``query_only`` を入れる前に呼ぶ。"""
    con.execute("PRAGMA temp_store = MEMORY")
    con.execute(
        f"CREATE TEMP TABLE {TEMP_GREP_TABLE}(path TEXT, line INTEGER, snippet TEXT, confidence_level TEXT)"
    )
    con.executemany(
        f"INSERT INTO {TEMP_GREP_TABLE}(path, line, snippet, confidence_level) VALUES (?, ?, ?, 'lexical_fallback')",
        rows,
    )


# --- 実行（手順 5・9） -------------------------------------------------------------------------


def _translate(exc: Exception, guard: Guard) -> LuciankeError:
    """SQLite / Python の例外を §5.8 のエラーに写す（S-7「エラーの型」）。"""
    message = str(exc)
    if guard.denied is not None:
        kind, target = guard.denied
        what = "non-public object" if kind == "non_public_object" else "operation"
        return _rejected(kind, f"{what} is not allowed: {target}", sqlite_message=message, target=target)
    if isinstance(exc, sqlite3.ProgrammingError):
        if "one statement at a time" in message:
            return _rejected("multiple_statements", "exactly one SELECT or WITH statement is allowed")
        return _invalid(f"parameter binding failed: {message}", param="params")
    if isinstance(exc, OverflowError | ValueError):
        return _invalid(f"parameter binding failed: {message}", param="params")
    if message.startswith("no such table"):
        name = message.rpartition(": ")[2]
        hint = " (it exists only when auto_fallback_grep_pattern is given)" if name == TEMP_GREP_TABLE else ""
        return _rejected("non_public_object", f"{message}{hint}", sqlite_message=message, target=name)
    return _rejected("syntax_error", message, sqlite_message=message)


def _confidence_breakdown(
    columns: list[tuple[str, str | None]], rows: list[tuple[Any, ...]]
) -> dict[str, int] | None:
    idx = next((i for i, (name, _) in enumerate(columns) if name.lower() == "confidence_level"), None)
    if idx is None:
        return None
    counts = Counter(r[idx] for r in rows if r[idx] is not None)
    ordered = [lv for lv in _CONFIDENCE_ORDER if lv in counts]
    ordered += sorted(str(k) for k in counts if k not in _CONFIDENCE_ORDER)
    return {lv: counts[lv] for lv in ordered}


def run_query(
    db_path: Path,
    sql: str,
    *,
    params: Mapping[str, Any] | None = None,
    limit: int = QUERY_ROWS_DEFAULT,
    timeout_ms: int = QUERY_TIMEOUT_DEFAULT_MS,
    grep: GrepSpec | None = None,
    explain: bool = False,
) -> QueryResult:
    """公開クエリを 1 本実行する（1 要求 1 接続。S-7: 開く費用 p50 1.3 ms）。ブロッキングなので読み取りスレッドで呼ぶ。

    Raises:
        LuciankeError: ``E_QUERY_REJECTED``（静的検査・authorizer・構文エラー）、``E_INVALID_ARGUMENT``（``params`` の
            不一致・範囲外・grep パターン）、``E_INTERNAL``（DB を開けない）。
        QueryTimeout: ``E_TIMEOUT``（``timeout_ms`` 超過。取得済みの行は ``result``）。
    """
    if not 1 <= limit <= QUERY_ROWS_MAX:
        raise _invalid(f"limit must be 1..{QUERY_ROWS_MAX}", param="limit")
    if not 0 < timeout_ms <= QUERY_TIMEOUT_MAX_MS:
        raise _invalid(f"timeout_ms must be at most {QUERY_TIMEOUT_MAX_MS}", param="timeout_ms")
    analysis = _analyze(sql)
    bind = _check_params(params, analysis)
    scan = scan_grep(grep) if grep is not None else None
    setup = None if scan is None else (lambda con: _install_grep(con, scan.rows))
    con = open_public(db_path, setup=setup)
    try:
        result = _execute(
            con, ("EXPLAIN QUERY PLAN " + sql) if explain else sql, bind, limit, timeout_ms, explain
        )
    finally:
        con.close()
    if scan is None or grep is None:
        return result
    fallback = {
        "pattern": grep.pattern,
        "matches": len(scan.rows),
        "truncated": scan.truncated,
        "scope_paths": scan.scope_paths,
    }
    return dataclasses.replace(result, grep_fallback=fallback)


def _execute(
    con: PublicConnection, stmt: str, bind: dict[str, _BindValue], limit: int, timeout_ms: int, explain: bool
) -> QueryResult:
    guard = con.guard
    rows: list[tuple[Any, ...]] = []
    plan: list[dict[str, Any]] | None = None
    description: Any = ()
    start = time.perf_counter()
    guard.deadline = start + timeout_ms / 1000
    cur = None
    try:
        cur = con.execute(stmt, bind)
        description = cur.description or ()
        if explain:
            plan = [{"id": r[0], "parent": r[1], "detail": r[3]} for r in cur.fetchall()]
        else:
            while len(rows) < limit + 1:  # 1 行ずつ取る: 期限で中断したとき取得済みの行を部分結果にできる
                row = cur.fetchone()
                if row is None:
                    break
                rows.append(tuple(row))
    except (sqlite3.Error, OverflowError, ValueError) as exc:
        elapsed = time.perf_counter() - start
        if guard.timed_out and guard.denied is None:
            raise QueryTimeout(
                f"query exceeded timeout_ms={timeout_ms}",
                _build(guard, description, rows, limit, elapsed, None),
                timeout_ms=timeout_ms,
            ) from None
        raise _translate(exc, guard) from exc
    finally:
        if cur is not None:
            cur.close()
    elapsed = time.perf_counter() - start
    if (
        elapsed > timeout_ms / 1000
    ):  # progress handler の呼び出し間隔の隙間で超えた分。elapsed_ms <= timeout_ms を保証する
        raise QueryTimeout(
            f"query exceeded timeout_ms={timeout_ms}",
            _build(guard, description, rows, limit, elapsed, plan),
            timeout_ms=timeout_ms,
        )
    return _build(guard, description, rows, limit, elapsed, plan)


def _build(
    guard: Guard,
    description: Any,
    rows: list[tuple[Any, ...]],
    limit: int,
    elapsed: float,
    plan: list[dict[str, Any]] | None,
) -> QueryResult:
    if plan is not None:  # explain: 実行しない。rows は空
        return QueryResult(
            [], [], 0, False, int(elapsed * 1000), plan=plan, tables_read=tuple(sorted(guard.tables_read))
        )
    columns = [(d[0], _decl_type(guard, d[0])) for d in description]
    truncated = len(rows) > limit
    kept = rows[:limit]
    return QueryResult(
        columns,
        kept,
        len(kept),
        truncated,
        int(elapsed * 1000),
        confidence_breakdown=_confidence_breakdown(columns, kept),
        tables_read=tuple(sorted(guard.tables_read)),
    )


def _decl_type(guard: Guard, name: str) -> str | None:
    types = {
        guard.decl_types[(t, c)]
        for (t, c) in guard.columns_read
        if c == name.lower() and (t, c) in guard.decl_types
    }
    return next(iter(types)) if len(types) == 1 else None
