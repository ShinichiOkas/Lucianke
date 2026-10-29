"""公開クエリ用の読み取り専用接続（設計書 §6.2・§7.6.4）の最小実装。S-7 の「使うコードの型」。

使い方: run.py から import される。index 層はこの形を `src/lucianke/index/public.py` に移す。
  con, guard = open_public(Path("idx.db"), timeout_ms=300, setup=fill_temp_grep)
  rows = guard.run(con, "SELECT ...", {}, limit=200)
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

PUBLIC_TABLES = frozenset(
    {
        "workspaces", "files", "symbols", "relations", "dataflows", "diagnostics",
        "contracts", "git_commits", "git_symbol_changes", "git_co_changes", "symbols_fts",
    }
)  # fmt: skip
# FTS5 は MATCH のたびに自分の影テーブルを読む。authorizer は内側の文にも呼ばれる（S-7 項目 3）。
FTS_SHADOW = frozenset({"symbols_fts_config", "symbols_fts_data", "symbols_fts_docsize", "symbols_fts_idx"})
# JSON の表値関数は READ(table=関数名) として届く。
TABLE_FUNCTIONS = frozenset({"json_each", "json_tree"})
TEMP_TABLE = "temp_grep_matches"
READ_OK = PUBLIC_TABLES | FTS_SHADOW | TABLE_FUNCTIONS
HIDDEN_PREFIXES = ("_", "sqlite_", "pragma_")
FUNC_DENY = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})

OK, DENY = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
# 拒否理由に action 名を出すための表（数値のままだと ATTACH=24 が SQLITE_FORMAT と衝突して読めない）
ACTION_NAMES = {
    getattr(sqlite3, n): n.removeprefix("SQLITE_")
    for n in dir(sqlite3)
    if n.startswith(("SQLITE_CREATE_", "SQLITE_DROP_", "SQLITE_ALTER_"))
    or n in {"SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_PRAGMA",
             "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT", "SQLITE_ANALYZE", "SQLITE_REINDEX"}
}  # fmt: skip


class Guard:
    """authorizer と progress handler の状態。1 要求 1 インスタンス。"""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        self.deadline = float("inf")  # run() で起算する（接続を開く時間・temp の投入時間を含めない）
        self.timed_out = False
        self.hidden: frozenset[str] = frozenset()  # DB に実在する非公開のテーブル・ビュー名（open_public が埋める）
        self.denied: tuple[str, str | None] | None = None  # 最初に拒否した (種別, 対象)。E_QUERY_REJECTED の details 用

    def authorize(self, action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return OK
        if action == sqlite3.SQLITE_READ:  # arg1=表名, arg2=列名, dbname='main'|'temp'|None
            if (arg1 in READ_OK or arg1 == TEMP_TABLE) and dbname in (None, "main", "temp"):
                return OK
            # count(*) や SELECT 1 のように列を読まない参照は arg2='' かつ dbname=None で届く。CTE・サブクエリの別名も同じ形で届く
            # （`WITH c AS (...) SELECT count(*) FROM c`）。実在の非公開オブジェクトの名前だけを拒否し、それ以外は別名として通す。
            if arg2 == "" and dbname is None and arg1 not in self.hidden and not arg1.startswith(HIDDEN_PREFIXES):
                return OK
            return self._deny("non_public_object", arg1)
        if action == sqlite3.SQLITE_FUNCTION:  # arg2=関数名
            if (arg2 or "").lower() in FUNC_DENY:
                return self._deny("forbidden_keyword", arg2)
            return OK
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "data_version" and arg2 is None:
            return OK  # FTS5 が内部で発行する。これを拒否すると MATCH が失敗する
        return self._deny("forbidden_keyword", f"{ACTION_NAMES.get(action, action)} {arg1}")

    def _deny(self, kind: str, target: str | None) -> int:
        if self.denied is None:
            self.denied = (kind, target)
        return DENY

    def check_deadline(self) -> int:  # progress handler。非 0 を返すと文が中断される（OperationalError: interrupted）
        if time.perf_counter() > self.deadline:
            self.timed_out = True
            return 1
        return 0

    def run(self, con: sqlite3.Connection, sql: str, params: Mapping[str, Any] | None, *, limit: int) -> tuple[list[tuple], bool]:
        """(行, truncated)。拒否・期限超過は呼び出し側が guard.denied / guard.timed_out で E_* に写す。"""
        self.deadline = time.perf_counter() + self.timeout_ms / 1000  # monotonic は Windows の 3.12 で約 15.6 ms 刻み
        cur = con.execute(sql, params or {})
        rows = cur.fetchmany(limit + 1)
        cur.close()
        return rows[:limit], len(rows) > limit


def open_public(
    db_path: Path,
    *,
    timeout_ms: int = 300,
    progress_ops: int = 1000,
    max_value_bytes: int = 16 * 1024 * 1024,
    setup: Callable[[sqlite3.Connection], None] | None = None,
    layers: frozenset[str] = frozenset({"ro", "query_only", "authorizer"}),
) -> tuple[sqlite3.Connection, Guard]:
    """layers は S-7 の層別検査用（本番は既定のまま 3 層）。"""
    uri = db_path.resolve().as_uri() + ("?mode=ro" if "ro" in layers else "")  # 空白・日本語・# は as_uri が符号化する
    con = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=1.0)  # 自動コミット。timeout=busy_timeout 1 s
    guard = Guard(timeout_ms)
    try:
        if setup is not None:
            setup(con)  # temp テーブルの作成・投入は query_only を入れる前に行う（項目 4）
        if "query_only" in layers:
            con.execute("PRAGMA query_only = ON")
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max_value_bytes)  # 1 値・1 行の上限。zeroblob(2e9) などの巨大化を即エラーにする
        con.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 64 * 1024)
        if "authorizer" in layers:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master UNION SELECT name FROM sqlite_temp_master")}
            guard.hidden = frozenset(names) - READ_OK - {TEMP_TABLE}
            con.set_authorizer(guard.authorize)
        con.set_progress_handler(guard.check_deadline, progress_ops)
    except BaseException:
        con.close()
        raise
    return con, guard
