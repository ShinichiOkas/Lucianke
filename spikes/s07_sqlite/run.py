"""S-7 sqlite3 の読み取り専用公開: 全項目の再現スクリプト。
使い方: uv run python spikes/s07_sqlite/run.py            # 項目 1〜6 を全部実行（約 30 秒）
        uv run python spikes/s07_sqlite/run.py 3 5        # 項目番号を指定
一時ファイルは %TEMP% 配下に作り、終了時に消す。リポジトリには何も書かない。
"""

from __future__ import annotations

import atexit
import ctypes
import hashlib
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from public_db import Guard, open_public  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # cp932 のコンソールでも文字化けさせない
SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
ACTIONS = {
    getattr(sqlite3, n): n
    for n in (
        "SQLITE_ALTER_TABLE SQLITE_ANALYZE SQLITE_ATTACH SQLITE_CREATE_INDEX SQLITE_CREATE_TABLE SQLITE_CREATE_TEMP_INDEX "
        "SQLITE_CREATE_TEMP_TABLE SQLITE_CREATE_TEMP_TRIGGER SQLITE_CREATE_TEMP_VIEW SQLITE_CREATE_TRIGGER "
        "SQLITE_CREATE_VIEW SQLITE_CREATE_VTABLE SQLITE_DELETE SQLITE_DETACH SQLITE_DROP_INDEX SQLITE_DROP_TABLE "
        "SQLITE_DROP_TEMP_INDEX SQLITE_DROP_TEMP_TABLE SQLITE_DROP_TEMP_TRIGGER SQLITE_DROP_TEMP_VIEW "
        "SQLITE_DROP_TRIGGER SQLITE_DROP_VIEW SQLITE_DROP_VTABLE SQLITE_FUNCTION SQLITE_INSERT SQLITE_PRAGMA "
        "SQLITE_READ SQLITE_RECURSIVE SQLITE_REINDEX SQLITE_SAVEPOINT SQLITE_SELECT SQLITE_TRANSACTION SQLITE_UPDATE"
    ).split()
}


_DIRS: list[Path] = []


def tmpdir(name: str = "s07") -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"lucianke-{name}-"))
    _DIRS.append(d)
    return d


@atexit.register
def _cleanup() -> None:
    for d in _DIRS:
        shutil.rmtree(d, ignore_errors=True)


def seed(path: Path, n_symbols: int = 1000) -> sqlite3.Connection:
    """書き手の接続（WAL）を返す。閉じずに返すので -wal / -shm が残る。"""
    w = sqlite3.connect(path, isolation_level=None)
    w.execute("PRAGMA journal_mode = WAL")
    w.execute("PRAGMA synchronous = NORMAL")
    w.execute("PRAGMA foreign_keys = ON")
    w.execute("PRAGMA busy_timeout = 5000")
    w.executescript(SCHEMA.replace("PRAGMA journal_mode = WAL;", "").replace("PRAGMA foreign_keys = ON;", ""))
    w.execute("INSERT INTO _meta VALUES (1, 1, 0, '0.0.1', 't')")
    w.execute("INSERT INTO workspaces(workspace_id, root_path) VALUES ('w', '/r')")
    w.execute("BEGIN")
    w.execute(
        "INSERT INTO files(file_id, workspace_id, path, language, tier, line_count, byte_size, sha256, indexed_at, confidence_level)"
        " VALUES (1, 'w', 'a.py', 'python', 1, 10, 100, 'x', 't', 'exact')"
    )
    for i in range(n_symbols):
        w.execute(
            "INSERT INTO symbols(urn, file_id, name, qualified_name, kind, start_line, end_line, confidence_level, signature, doc_summary)"
            " VALUES (?, 1, ?, ?, 'function', ?, ?, 'exact', ?, ?)",
            (f"u{i}", f"foo{i}", f"A.foo{i}", i, i + 1, f"def foo{i}()", f"does foo {i}"),
        )
    w.execute(
        "INSERT INTO symbols_fts(rowid, name, qualified_name, signature, doc_summary)"
        " SELECT rowid, name, qualified_name, signature, doc_summary FROM symbols"
    )
    w.execute("COMMIT")
    return w


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def p_stats(xs: list[float]) -> str:
    xs = sorted(xs)
    return f"p50={statistics.median(xs):.2f} p95={xs[int(len(xs) * 0.95) - 1]:.2f} max={xs[-1]:.2f}"


def rss_mb() -> float:
    """このプロセスのピーク作業セット（MB）。"""
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]  # fmt: skip

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return pmc.PeakWorkingSetSize / 1048576


def ro_uri(p: Path, extra: str = "mode=ro") -> str:
    return p.resolve().as_uri() + "?" + extra


def hdr(n: int, title: str) -> None:
    print(f"\n=== 項目 {n}: {title} ===")


# ---------------------------------------------------------------- 項目 1
def writer_proc(db: str, seconds: float, gap_ms: float = 0) -> None:
    w = sqlite3.connect(db, isolation_level=None, timeout=5)
    w.execute("PRAGMA synchronous = NORMAL")
    t0, n = time.perf_counter(), 0
    while time.perf_counter() - t0 < seconds:
        w.execute("BEGIN")
        for _ in range(5):
            n += 1
            w.execute("INSERT INTO relations(source_urn, target_urn, relation_kind, confidence_level) VALUES (?, 'u1', 'CALLS', 'exact')", (f"u{n}",))
        w.execute("COMMIT")
        if gap_ms:
            time.sleep(gap_ms / 1000)
    print(n)


def item1() -> None:
    hdr(1, "WAL の DB を別接続の mode=ro URI で読む")
    d = tmpdir()
    p = d / "idx.db"
    w = seed(p)
    names = lambda: sorted(x for x in os.listdir(d))  # noqa: E731
    print(f"[1a] 書き手が開いている。ファイル={names()} wal={os.path.getsize(str(p) + '-wal')}B")
    before = sha(p)
    r = sqlite3.connect(ro_uri(p), uri=True)
    n = r.execute("SELECT count(*) FROM symbols").fetchone()[0]
    print(f"[1a] mode=ro 読み取り: symbols={n} (WAL 上だけにある行も見える={'合格' if n == 1000 else '不合格'}) FTS={r.execute(chr(83)+'ELECT count(*) FROM symbols_fts WHERE symbols_fts MATCH ?', ('foo5',)).fetchone()[0]}")
    # 書き手が未コミットのトランザクションを持つとき
    w.execute("BEGIN")
    w.execute("INSERT INTO relations(source_urn, target_urn, relation_kind, confidence_level) VALUES ('a','b','CALLS','exact')")
    print(f"[1a] 書き手が未コミット: 読み手の relations={r.execute('SELECT count(*) FROM relations').fetchone()[0]} (0 なら未コミットは見えない=合格)")
    w.execute("COMMIT")
    print(f"[1a] コミット後: 読み手の relations={r.execute('SELECT count(*) FROM relations').fetchone()[0]} (1 なら新しい行が見える=合格)")
    r.close()
    print(f"[1a] 本体ファイルの sha256 は読み取りで不変: {before == sha(p)} (WAL 方式のため本体は書き手のチェックポイントまで変わらない)")
    # 書き手が書き込み中（別プロセス）に新規接続を繰り返し開く
    w.close()  # 最後の接続を閉じると WAL は本体に畳まれて -wal / -shm が消える
    print(f"[1b] 書き手を閉じた後のファイル={names()} (-wal/-shm が消える)")
    b = sha(p)
    r = sqlite3.connect(ro_uri(p), uri=True)
    n = r.execute("SELECT count(*) FROM symbols").fetchone()[0]
    print(f"[1b] -wal/-shm が無い状態で mode=ro: symbols={n} 開いている間のファイル={names()}")
    r.close()
    print(f"[1b] 読み手を閉じた後のファイル={names()} 本体 sha256 不変={b == sha(p)}")
    # 並行書き込み
    errs, counts, opens = [], [], []
    wp = subprocess.Popen([sys.executable, __file__, "--writer", str(p), "3"], stdout=subprocess.PIPE, text=True)
    t_end = time.perf_counter() + 3.2
    while time.perf_counter() < t_end:
        t0 = time.perf_counter()
        try:
            r = sqlite3.connect(ro_uri(p), uri=True, timeout=1.0)
            r.execute("PRAGMA query_only = ON")
            counts.append(r.execute("SELECT count(*) FROM relations").fetchone()[0])
            r.close()
            opens.append((time.perf_counter() - t0) * 1000)
        except sqlite3.Error as e:
            errs.append(f"{type(e).__name__}: {e}")
        time.sleep(0.005)
    written = int(wp.communicate()[0].strip())
    mono = all(a <= b for a, b in zip(counts, counts[1:]))
    print(
        f"[1c] 別プロセスの書き手が 3 秒書き込み中（計 {written} 行）に新規 ro 接続を {len(counts) + len(errs)} 回: "
        f"エラー={len(errs)} 件 {sorted(set(errs))[:2]} 件数は単調増加={mono} 最終={counts[-1]}/{written + 1}  open+SELECT+close(ms) {p_stats(opens)}"
    )
    # 同一プロセス内スレッドの書き手（本番の配置: IndexWriter スレッドと公開接続が同じプロセス）
    w = sqlite3.connect(p, isolation_level=None, timeout=5, check_same_thread=False)
    stop, wn = threading.Event(), [0]

    def wloop() -> None:
        while not stop.is_set():
            w.execute("BEGIN")
            for _ in range(5):
                wn[0] += 1
                w.execute("INSERT INTO relations(source_urn, target_urn, relation_kind, confidence_level) VALUES ('t','u1','CALLS','exact')")
            w.execute("COMMIT")

    th = threading.Thread(target=wloop)
    th.start()
    errs2, n2 = [], 0
    t_end = time.perf_counter() + 2
    while time.perf_counter() < t_end:
        try:
            r = sqlite3.connect(ro_uri(p), uri=True, timeout=1.0)
            r.execute("SELECT count(*) FROM relations").fetchone()
            r.close()
            n2 += 1
        except sqlite3.Error as e:
            errs2.append(f"{type(e).__name__}: {e}")
    stop.set()
    th.join()
    print(f"[1c'] 同一プロセスの書き手スレッド（{wn[0]} 行）に対し ro 接続 {n2 + len(errs2)} 回: エラー={len(errs2)} 件 {sorted(set(errs2))[:2]}")
    # 長い読み取りトランザクションが書き手を止めるか（読み手が BEGIN したまま）
    r = sqlite3.connect(ro_uri(p), uri=True)
    r.execute("BEGIN")
    r.execute("SELECT count(*) FROM relations").fetchone()
    t0 = time.perf_counter()
    w.execute("INSERT INTO relations(source_urn, target_urn, relation_kind, confidence_level) VALUES ('t','u1','CALLS','exact')")
    t_ins = 1000 * (time.perf_counter() - t0)
    w.execute("PRAGMA busy_timeout = 200")
    t0 = time.perf_counter()
    ck = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    t_ck = 1000 * (time.perf_counter() - t0)
    print(f"[1d] 読み手が BEGIN で読み取りを保持中でも書き手の INSERT は {t_ins:.1f} ms で成功（WAL は読み手を待たない）。TRUNCATE チェックポイントは (busy, log, checkpointed)={ck} で {t_ck:.0f} ms（busy_timeout 200）待って完了せず wal={os.path.getsize(str(p) + '-wal')}B")
    r.close()
    w.close()
    # immutable=1
    w = sqlite3.connect(p, isolation_level=None)
    w.execute("PRAGMA journal_mode = WAL")
    c0 = w.execute("SELECT count(*) FROM symbols").fetchone()[0]
    r = sqlite3.connect(ro_uri(p, "immutable=1"), uri=True)
    r.execute("SELECT count(*) FROM symbols").fetchone()
    w.execute("INSERT INTO symbols(urn, file_id, name, qualified_name, kind, start_line, end_line, confidence_level) VALUES ('imm', 1, 'n', 'n', 'function', 1, 1, 'exact')")
    print(f"[1e] immutable=1 は書き手の新しい行を見ない: 書き手={w.execute('SELECT count(*) FROM symbols').fetchone()[0]} 読み手={r.execute('SELECT count(*) FROM symbols').fetchone()[0]} (書き込み前は {c0}) -> 使わない")
    r.close()
    w.close()
    # パスの符号化
    for label in ["空白 と 日本語 #%", "plain"]:
        dd = d / label
        dd.mkdir()
        pp = dd / "索引 #1.db"
        shutil.copy(p, pp)
        try:
            con, _ = open_public(pp)
            n = con.execute("SELECT count(*) FROM symbols").fetchone()[0]
            con.close()
            print(f"[1f] Path.as_uri() で開く: {pp.relative_to(d)} -> symbols={n} 合格")
        except Exception as e:  # noqa: BLE001
            print(f"[1f] {pp.relative_to(d)} -> 不合格 {type(e).__name__}: {e}")
    # 読み取り専用ディレクトリ（作成権限なし）
    dd = d / "rodir"
    dd.mkdir()
    pp = dd / "idx.db"
    shutil.copy(p, pp)
    user = os.environ.get("USERNAME", "")
    subprocess.run(["icacls", str(dd), "/deny", f"{user}:(WD,AD)"], capture_output=True)  # ファイル作成・サブディレクトリ作成を拒否
    try:
        try:
            Path(dd / "probe.txt").write_text("x")
            print("[1g] icacls の拒否が効かない（この検査は無効）")
            Path(dd / "probe.txt").unlink()
        except OSError:
            try:
                r = sqlite3.connect(ro_uri(pp), uri=True)
                n = r.execute("SELECT count(*) FROM symbols").fetchone()[0]
                print(f"[1g] ディレクトリにファイルを作れず -wal/-shm が無い状態で mode=ro: 読めた symbols={n}")
                r.close()
            except sqlite3.Error as e:
                print(f"[1g] ディレクトリにファイルを作れず -wal/-shm が無い状態で mode=ro: 失敗 {type(e).__name__}: {e}")
    finally:
        subprocess.run(["icacls", str(dd), "/remove:d", user], capture_output=True)
    # 書き手が開いていて -wal/-shm が既にあるなら、ディレクトリに作成権限が無くても読める
    dd2 = d / "rodir2"
    dd2.mkdir()
    w2 = seed(dd2 / "idx.db")
    subprocess.run(["icacls", str(dd2), "/deny", f"{user}:(WD,AD)"], capture_output=True)
    try:
        r = sqlite3.connect(ro_uri(dd2 / "idx.db"), uri=True)
        n = r.execute("SELECT count(*) FROM symbols").fetchone()[0]
        print(f"[1g'] 書き手が開いていて -wal/-shm が既にある（ディレクトリは作成不可）: mode=ro で読めた symbols={n}")
        r.close()
    except sqlite3.Error as e:
        print(f"[1g'] 書き手が開いていて -wal/-shm が既にある（ディレクトリは作成不可）: 失敗 {type(e).__name__}: {e}")
    finally:
        subprocess.run(["icacls", str(dd2), "/remove:d", user], capture_output=True)
        w2.close()
    latency_under_write()
    shutil.rmtree(d, ignore_errors=True)


def latency_under_write() -> None:
    """[1h] 書き手がコミットを連打 / 20 ms おきのとき、毎回開く接続と開きっぱなしの接続の 1 クエリ遅延。"""
    for gap in ("0", "20"):
        for persistent in (False, True):
            d = tmpdir()
            p = d / "idx.db"
            seed(p).close()
            wp = subprocess.Popen([sys.executable, __file__, "--writer", str(p), "3.5", gap], stdout=subprocess.PIPE, text=True)
            time.sleep(0.2)
            ts, errs = [], 0
            con = sqlite3.connect(ro_uri(p), uri=True, timeout=1.0) if persistent else None
            t_end = time.perf_counter() + 3
            while time.perf_counter() < t_end:
                t0 = time.perf_counter()
                try:
                    c = con or sqlite3.connect(ro_uri(p), uri=True, timeout=1.0)
                    c.execute("SELECT urn FROM symbols WHERE name = 'foo5'").fetchall()
                    if not persistent:
                        c.close()
                except sqlite3.Error:
                    errs += 1
                ts.append((time.perf_counter() - t0) * 1000)
                time.sleep(0.003)
            wp.communicate()
            if con:
                con.close()
            print(f"[1h] 書き手のコミット間隔 {gap} ms / {'開きっぱなし' if persistent else '毎回開く  '}: n={len(ts)} {p_stats(ts)} エラー={errs}")


# ---------------------------------------------------------------- 項目 2・4（層別の検査）
WRITES = [
    ("INSERT", "INSERT INTO workspaces(workspace_id, root_path) VALUES ('x', '/x')"),
    ("INSERT OR REPLACE", "INSERT OR REPLACE INTO workspaces(workspace_id, root_path) VALUES ('w', '/y')"),
    ("UPDATE", "UPDATE workspaces SET root_path = '/z'"),
    ("DELETE", "DELETE FROM files"),
    ("CREATE TABLE", "CREATE TABLE evil(a)"),
    ("CREATE INDEX", "CREATE INDEX evil_i ON files(path)"),
    ("CREATE VIEW", "CREATE VIEW evil_v AS SELECT 1"),
    ("CREATE TRIGGER", "CREATE TRIGGER evil_t AFTER INSERT ON files BEGIN SELECT 1; END"),
    ("DROP TABLE", "DROP TABLE _hover_cache"),
    ("ALTER TABLE", "ALTER TABLE files ADD COLUMN evil TEXT"),
    ("WITH ... INSERT", "WITH x AS (SELECT 1) INSERT INTO workspaces(workspace_id, root_path) VALUES ('wx', '/wx')"),
    ("WITH ... DELETE", "WITH x AS (SELECT 1) DELETE FROM files"),
    ("INSERT ... RETURNING", "INSERT INTO workspaces(workspace_id, root_path) VALUES ('wr', '/wr') RETURNING workspace_id"),
    ("FTS5 コマンド delete-all", "INSERT INTO symbols_fts(symbols_fts) VALUES ('delete-all')"),
    ("FTS5 コマンド rebuild", "INSERT INTO symbols_fts(symbols_fts) VALUES ('rebuild')"),
    ("PRAGMA user_version=7", "PRAGMA user_version = 7"),
    ("PRAGMA journal_mode=DELETE", "PRAGMA journal_mode = DELETE"),
    ("PRAGMA wal_checkpoint", "PRAGMA wal_checkpoint(TRUNCATE)"),
    ("VACUUM", "VACUUM"),
    ("REINDEX", "REINDEX"),
    ("ANALYZE", "ANALYZE"),
    ("BEGIN", "BEGIN"),
    ("SAVEPOINT", "SAVEPOINT sp"),
    ("ATTACH(新規ファイル)", "ATTACH DATABASE '{ATT}' AS x"),
    ("CREATE TEMP TABLE", "CREATE TEMP TABLE evil_tmp(a)"),
]
LAYER_SETS = [
    ("ro のみ", {"ro"}),
    ("query_only のみ", {"query_only"}),
    ("authorizer のみ", {"authorizer"}),
    ("ro+query_only", {"ro", "query_only"}),
    ("ro+authorizer", {"ro", "authorizer"}),
    ("query_only+authorizer", {"query_only", "authorizer"}),
    ("3 層", {"ro", "query_only", "authorizer"}),
    ("0 層", set()),
]


def fingerprint(path: Path) -> tuple:
    """DB ファイルのハッシュ・journal_mode・user_version・全ダンプ・同じディレクトリのファイル一覧。"""
    h = sha(path)  # 他の接続を開く前に取る
    c = sqlite3.connect(path)
    try:
        jm = c.execute("PRAGMA journal_mode").fetchone()[0]
        uv = c.execute("PRAGMA user_version").fetchone()[0]
        dump = hashlib.sha256(chr(10).join(c.iterdump()).encode()).hexdigest()[:10]
    finally:
        c.close()
    extra = sorted(x for x in os.listdir(path.parent) if not x.startswith(path.name + "-") and x != path.name)
    return h, jm, uv, dump, extra


def item2() -> None:
    hdr(2, "query_only / 3 層それぞれ単独で書き込み文を拒否するか")
    base = tmpdir("s07base")
    w = seed(base / "idx.db")
    w.close()  # 閉じて WAL を本体に畳む。以後はコピーして使う
    work = tmpdir("s07w")
    odd: list[str] = []
    errtypes: dict[str, set[str]] = {}
    matrix: dict[str, dict[str, str]] = {}
    for sname, layers in LAYER_SETS:
        for label, sql in WRITES:
            if os.path.exists(work):
                shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
            db = work / "idx.db"
            shutil.copy(base / "idx.db", db)
            fp0 = fingerprint(db)
            sql_ = sql.replace("{ATT}", str(work / "att.db").replace("\\", "/"))
            con, g = open_public(db, layers=frozenset(layers))
            try:
                try:
                    con.execute(sql_).fetchall()
                    status = "実行"
                except sqlite3.Error as e:
                    status = "拒否"
                    errtypes.setdefault(sname, set()).add(f"{type(e).__name__}: {e}")
                    if str(e) != "attempt to write a readonly database" and sname in ("ro のみ", "query_only のみ"):
                        odd.append(f"{sname} / {label}: {type(e).__name__}: {e}")
            finally:
                con.close()
            changed = fingerprint(db) != fp0
            matrix.setdefault(label, {})[sname] = status + ("+変更" if changed else "")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(base, ignore_errors=True)
    names = [s for s, _ in LAYER_SETS]
    print("文 \\ 層 | " + " | ".join(names))
    for label, _ in WRITES:
        print(f"{label} | " + " | ".join(matrix[label][n] for n in names))
    print("\n[2] 'attempt to write a readonly database' 以外のエラーを返した、単独層での文:")
    for o in odd:
        print("  ", o)
    print("\n[2] 層ごとに現れたエラー（型: メッセージ）")
    for s, es in errtypes.items():
        print(f"  {s}: {sorted(es)}")


def item2b() -> None:
    hdr(2, "query_only の単独動作（エラーの型とメッセージ）と PRAGMA query_only=OFF 迂回")
    base = tmpdir("s07q")
    w = seed(base / "idx.db")
    c = sqlite3.connect(base / "idx.db")
    c.execute("PRAGMA query_only = ON")
    for sql in ["INSERT INTO workspaces(workspace_id, root_path) VALUES ('x','/x')", "UPDATE workspaces SET root_path='/z'", "DELETE FROM files", "CREATE TABLE evil(a)", "DROP TABLE _hover_cache"]:
        try:
            c.execute(sql)
            print(f"  query_only: {sql[:36]!r} -> 実行された（不合格）")
        except sqlite3.Error as e:
            print(f"  query_only: {sql[:36]!r} -> {type(e).__name__}: {e}")
    c.execute("PRAGMA query_only = OFF")  # authorizer も mode=ro も無ければ解除できる
    try:
        c.execute("INSERT INTO _meta VALUES (9, 9, 9, 'x', 'x')")
        print("  query_only だけの接続は PRAGMA query_only=OFF で解除でき、その後は書ける -> 単独の層としては弱い（authorizer が PRAGMA を拒否する理由）")
    except sqlite3.Error as e:
        print("  OFF 後も拒否:", e)
    c.close()
    # mode=ro 上で query_only=OFF にしても書けないか
    r = sqlite3.connect(ro_uri(base / "idx.db"), uri=True)
    r.execute("PRAGMA query_only = OFF")
    try:
        r.execute("INSERT INTO workspaces(workspace_id, root_path) VALUES ('x','/x')")
        print("  mode=ro + query_only=OFF: 書けた（不合格）")
    except sqlite3.Error as e:
        print(f"  mode=ro は query_only=OFF にしても拒否: {type(e).__name__}: {e}")
    r.close()
    # ATTACH の副作用
    att = base / "att.db"
    r = sqlite3.connect(ro_uri(base / "idx.db"), uri=True)
    r.execute("PRAGMA query_only = ON")
    r.execute(f"ATTACH DATABASE '{att.as_posix()}' AS x")
    print(f"  mode=ro + query_only で ATTACH: 成功。att.db が作られた={att.exists()} サイズ={att.stat().st_size if att.exists() else '-'}B")
    r.close()
    r = sqlite3.connect(ro_uri(base / "idx.db"), uri=True)
    r.execute(f"ATTACH DATABASE '{att.as_posix()}' AS x")
    r.execute("CREATE TABLE x.evil(a)")
    r.execute("INSERT INTO x.evil VALUES (1)")
    print(f"  mode=ro のみ: ATTACH した先には CREATE / INSERT できた（att.db={att.stat().st_size}B）。mode=ro は ATTACH 先を守らない")
    r.close()
    w.close()
    shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- 項目 3
def item3() -> None:
    hdr(3, "authorizer: action コードと引数の実例 / 許可・拒否の判定")
    d = tmpdir()
    p = d / "idx.db"
    w = seed(p)
    w.close()
    shutil.copy(p, d / "grep.db")
    cases = [
        "SELECT name FROM symbols WHERE kind = 'function'",
        "SELECT s.name, count(*) FROM symbols s JOIN files f ON f.file_id = s.file_id GROUP BY 1",
        "SELECT name FROM symbols_fts WHERE symbols_fts MATCH 'foo1*'",
        "SELECT name, rank FROM symbols_fts WHERE symbols_fts MATCH 'foo1*' ORDER BY rank LIMIT 3",
        "SELECT snippet(symbols_fts, 3, '[', ']', '..', 5) FROM symbols_fts WHERE symbols_fts MATCH 'does'",
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 5) SELECT x FROM c",
        "SELECT json_extract(modifiers_json, '$[0]') FROM symbols",
        "SELECT value FROM json_each('[1,2]')",
        "SELECT count(*) FROM main.files",
        "SELECT * FROM temp_grep_matches",
        "WITH c AS (SELECT 1 AS x) SELECT count(*) FROM c",
        "SELECT count(*) FROM (SELECT 1)",
        "SELECT count(*) FROM _meta",
        "SELECT 1 FROM _meta",
        "WITH _meta AS (SELECT 1) SELECT count(*) FROM _meta",
        "SELECT count(*) FROM sqlite_master",
        "SELECT count(*) FROM pragma_table_info('_meta')",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM sqlite_temp_master",
        "SELECT * FROM _meta",
        "SELECT * FROM pragma_table_info('_meta')",
        "SELECT * FROM symbols_fts_data LIMIT 1",
        "SELECT load_extension('x')",
        "SELECT readfile('x')",
        "SELECT fts3_tokenizer('simple')",
        "ATTACH DATABASE ':memory:' AS x",
        "DETACH DATABASE x",
        "PRAGMA table_info(symbols)",
        "PRAGMA query_only = OFF",
        "PRAGMA data_version",
        "INSERT INTO workspaces(workspace_id, root_path) VALUES ('x', '/x')",
        "UPDATE workspaces SET root_path = '/z'",
        "DELETE FROM files",
        "CREATE TABLE evil(a)",
        "CREATE TEMP TABLE evil_tmp(a)",
        "DROP TABLE _meta",
        "EXPLAIN QUERY PLAN SELECT * FROM symbols WHERE urn = 'u1'",
        "EXPLAIN QUERY PLAN SELECT * FROM _meta",
    ]
    print("-- (A) 全許可の記録用 authorizer で届く (action, arg1, arg2, dbname, source) の実例")
    for sql in cases:
        log: list[tuple] = []

        def auth(a, x, y, z, t, log=log):
            log.append((ACTIONS.get(a, a), x, y, z, t))
            return sqlite3.SQLITE_OK

        c = sqlite3.connect(ro_uri(p), uri=True, isolation_level=None)
        c.execute("CREATE TEMP TABLE temp_grep_matches(path TEXT, line INTEGER, snippet TEXT, confidence_level TEXT)")
        c.execute("INSERT INTO temp_grep_matches VALUES ('a.py', 1, 's', 'exact')")
        c.execute("PRAGMA query_only = ON")
        c.set_authorizer(auth)
        try:
            c.execute(sql).fetchall()
            st = "ok"
        except sqlite3.Error as e:
            st = f"{type(e).__name__}: {e}"
        c.set_authorizer(None)
        c.close()
        # 連続重複と列ごとの READ を畳む
        compact, seen = [], set()
        for a in log:
            key = (a[0], a[1]) if a[0] == "SQLITE_READ" else a
            if key in seen:
                continue
            seen.add(key)
            compact.append(a if a[0] != "SQLITE_READ" else (a[0], a[1], "*", a[3], a[4]))
        print(f"\nSQL> {sql}\n  実行={st}")
        for a in compact:
            print("   ", a)

    print("\n-- (B) 本番の Guard（open_public、3 層）で同じ文を流した判定")

    def setup(con: sqlite3.Connection) -> None:
        con.execute("CREATE TEMP TABLE temp_grep_matches(path TEXT, line INTEGER, snippet TEXT, confidence_level TEXT)")
        con.executemany("INSERT INTO temp_grep_matches VALUES (?, ?, ?, ?)", [("a.py", 1, "s", "exact")])

    for sql in cases:
        con, g = open_public(p, setup=setup)
        try:
            rows, _ = g.run(con, sql, None, limit=5)
            res = f"許可 ({len(rows)} 行)"
        except sqlite3.Error as e:
            res = f"拒否 {type(e).__name__}: {e} / guard.denied={g.denied}"
        con.close()
        print(f"  {sql[:70]:70s} -> {res}")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 項目 4
def item4() -> None:
    hdr(4, "query_only=ON の接続で temp_grep_matches を作れるか")
    d = tmpdir()
    p = d / "idx.db"
    seed(p).close()
    create = "CREATE TEMP TABLE temp_grep_matches(path TEXT, line INTEGER, snippet TEXT, confidence_level TEXT)"
    r = sqlite3.connect(ro_uri(p), uri=True, isolation_level=None)
    r.execute(create)
    print("[4a] mode=ro のみ（query_only OFF）: CREATE TEMP TABLE 成功")
    r.close()
    r = sqlite3.connect(ro_uri(p), uri=True, isolation_level=None)
    r.execute("PRAGMA query_only = ON")
    try:
        r.execute(create)
        print("[4b] query_only=ON: CREATE TEMP TABLE 成功")
    except sqlite3.Error as e:
        print(f"[4b] query_only=ON: CREATE TEMP TABLE 失敗 {type(e).__name__}: {e}")
    r.close()
    r = sqlite3.connect(ro_uri(p), uri=True, isolation_level=None)
    r.execute(create)
    r.executemany("INSERT INTO temp_grep_matches VALUES (?, ?, ?, ?)", [("a.py", i, "s", "exact") for i in range(10000)])
    r.execute("PRAGMA query_only = ON")
    print(f"[4c] 作成と投入のあとに query_only=ON: SELECT 可={r.execute('SELECT count(*) FROM temp_grep_matches').fetchone()[0]} 行")
    for sql in ["INSERT INTO temp_grep_matches VALUES ('b', 1, 's', 'exact')", "DELETE FROM temp_grep_matches", "DROP TABLE temp_grep_matches"]:
        try:
            r.execute(sql)
            print(f"[4c] ON 後 {sql[:30]!r}: 実行された")
        except sqlite3.Error as e:
            print(f"[4c] ON 後 {sql[:30]!r}: 拒否 {type(e).__name__}: {e}")
    r.close()
    # 本番の流れ: setup(temp 作成・投入) -> query_only -> authorizer
    t0 = time.perf_counter()

    def setup(con: sqlite3.Connection) -> None:
        con.execute(create)
        con.executemany("INSERT INTO temp_grep_matches VALUES (?, ?, ?, ?)", [("a.py", i, "s", "exact") for i in range(10000)])

    con, g = open_public(p, setup=setup)
    rows, _ = g.run(con, "SELECT path, count(*) FROM temp_grep_matches GROUP BY path", None, limit=10)
    print(f"[4d] open_public(setup=...) 順序 temp作成 -> query_only -> authorizer: SELECT 成功 {rows} / 1 万行の投入込みで {1000 * (time.perf_counter() - t0):.1f} ms")
    for sql in ["INSERT INTO temp_grep_matches VALUES ('b', 1, 's', 'exact')", "CREATE TEMP TABLE t2(a)", "DROP TABLE temp_grep_matches"]:
        try:
            g.run(con, sql, None, limit=1)
            print(f"[4d] {sql[:30]!r}: 実行された（不合格）")
        except sqlite3.Error as e:
            print(f"[4d] {sql[:30]!r}: 拒否 {type(e).__name__}: {e}")
    con.close()
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 項目 5
INF = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
BLOWUP = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c UNION ALL SELECT x + 2 FROM c) SELECT count(*) FROM c"


def item5() -> None:
    hdr(5, "progress handler による中断（無限の再帰 CTE を 300 ms 前後で止める）")
    d = tmpdir()
    p = d / "idx.db"
    seed(p).close()
    for ops in (100, 1000, 10000, 100000):
        ts = []
        for _ in range(10):
            con, g = open_public(p, timeout_ms=300, progress_ops=ops)
            t0 = time.perf_counter()
            try:
                g.run(con, INF, None, limit=10)
                err = "中断されず"
            except sqlite3.Error as e:
                err = f"{type(e).__name__}: {e}"
            ts.append((time.perf_counter() - t0) * 1000)
            assert g.timed_out, err
            con.close()
        print(f"[5a] progress_ops={ops:6d}: 中断までの実時間(ms) {p_stats(ts)}  例外={err}  timed_out={g.timed_out}")
    # 中断後に同じ接続が使えるか
    con, g = open_public(p, timeout_ms=100)
    try:
        g.run(con, INF, None, limit=1)
    except sqlite3.Error:
        pass
    g.timed_out = False
    rows, _ = g.run(con, "SELECT 1", None, limit=1)
    print(f"[5b] 中断後の同じ接続で次の文: {rows}（接続は再利用できる）")
    con.close()
    # 通常の重い文でのハンドラの費用
    heavy = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) SELECT count(*) FROM c"
    for ops in (0, 100, 1000, 10000):
        c = sqlite3.connect(ro_uri(p), uri=True)
        if ops:
            c.set_progress_handler(lambda: 0, ops)
        t0 = time.perf_counter()
        c.execute(heavy).fetchone()
        print(f"[5c] 300 万行の再帰 CTE を数える（ハンドラ ops={ops or '無し'}）: {1000 * (time.perf_counter() - t0):.0f} ms")
        c.close()
    # メモリ: 指数的に増える再帰 CTE（キューが膨らむ）を 300 ms 走らせたときのピーク
    before = rss_mb()
    con, g = open_public(p, timeout_ms=300)
    t0 = time.perf_counter()
    try:
        g.run(con, BLOWUP, None, limit=10)
    except sqlite3.Error as e:
        print(f"[5d] 指数的に膨らむ再帰 CTE: {1000 * (time.perf_counter() - t0):.0f} ms で {e}。ピーク作業セット {before:.0f} -> {rss_mb():.0f} MB")
    con.close()
    # 巨大文字列・巨大 BLOB（progress handler では止められない単発の C 関数）
    for sql in ["SELECT length(zeroblob(1500000000))", "SELECT length(hex(zeroblob(400000000)))", "SELECT length(printf('%.*c', 900000000, 'x'))"]:
        con, g = open_public(p, timeout_ms=300)
        t0 = time.perf_counter()
        try:
            r = g.run(con, sql, None, limit=1)
            res = f"実行された {r[0]}"
        except sqlite3.Error as e:
            res = f"{type(e).__name__}: {e}"
        print(f"[5e] setlimit(LENGTH, 16MiB) 下の {sql[:46]!r}: {1000 * (time.perf_counter() - t0):.0f} ms -> {res}")
        con.close()
    # 正当な大きな行が読めるか（LENGTH 上限の副作用）
    w = sqlite3.connect(p, isolation_level=None)
    for mb in (2, 12, 20):
        w.execute("INSERT INTO symbols(urn, file_id, name, qualified_name, kind, start_line, end_line, confidence_level, doc_full) VALUES (?, 1, 'big', 'big', 'function', 1, 1, 'exact', ?)", (f"big{mb}", "x" * (mb * 1024 * 1024)))
    w.close()
    con, g = open_public(p, timeout_ms=1000)
    for mb in (2, 12, 20):
        try:
            rows, _ = g.run(con, f"SELECT length(doc_full), doc_full FROM symbols WHERE urn = 'big{mb}'", None, limit=1)
            print(f"[5g] doc_full {mb} MiB の行を SELECT: 読めた len={rows[0][0]}")
        except sqlite3.Error as e:
            print(f"[5g] doc_full {mb} MiB の行を SELECT: {type(e).__name__}: {e}")
    con.close()
    # setlimit 無しだと
    c = sqlite3.connect(ro_uri(p), uri=True)
    before = rss_mb()
    t0 = time.perf_counter()
    try:
        c.set_progress_handler(Guard(300).check_deadline, 1000)
        c.execute("SELECT length(hex(zeroblob(400000000)))").fetchone()
        print(f"[5e] setlimit 無し: {1000 * (time.perf_counter() - t0):.0f} ms で完了。ピーク作業セット {before:.0f} -> {rss_mb():.0f} MB（progress handler は単発の C 関数を止められない）")
    except sqlite3.Error as e:
        print("[5e] setlimit 無し:", e)
    c.close()
    # 別スレッドからの interrupt()
    con, g = open_public(p, timeout_ms=10_000, progress_ops=1000)
    timer = threading.Timer(0.3, con.interrupt)
    timer.start()
    t0 = time.perf_counter()
    try:
        g.run(con, INF, None, limit=1)
    except sqlite3.Error as e:
        print(f"[5f] 監視スレッドの con.interrupt(): {1000 * (time.perf_counter() - t0):.0f} ms で {type(e).__name__}: {e}")
    timer.cancel()
    con.close()
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 項目 6
def item6() -> None:
    hdr(6, "複文と EXPLAIN QUERY PLAN")
    d = tmpdir()
    p = d / "idx.db"
    seed(p).close()
    con, g = open_public(p)
    for sql in ["SELECT 1; SELECT 2", "SELECT 1;", "SELECT 1; -- c", "SELECT 1; /* c */ ", "SELECT 1;;", "SELECT 1; DROP TABLE files", "SELECT 1 -- DROP TABLE files"]:
        try:
            r = g.run(con, sql, None, limit=5)[0]
            print(f"[6a] {sql!r}: 実行 {r}")
        except sqlite3.Error as e:
            print(f"[6a] {sql!r}: {type(e).__name__}: {e}")
    try:
        g.run(con, "SELECT :a, :b", {"a": 1, "b": 2, "zzz": 3}, limit=5)
        print("[6b] 余分なキー zzz を含む params: 黙って受理（名前の突き合わせはアプリ側で必要）")
    except sqlite3.Error as e:
        print("[6b]", type(e).__name__, e)
    try:
        g.run(con, "SELECT :a, :b", {"a": 1}, limit=5)
    except sqlite3.Error as e:
        print(f"[6b] 足りないキー: {type(e).__name__}: {e}")
    for sql in [
        "EXPLAIN QUERY PLAN SELECT * FROM symbols WHERE urn = 'u1'",
        "EXPLAIN QUERY PLAN SELECT name FROM symbols_fts WHERE symbols_fts MATCH 'foo'",
        "EXPLAIN QUERY PLAN WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<5) SELECT x FROM c",
        "EXPLAIN QUERY PLAN SELECT * FROM _meta",
        "EXPLAIN QUERY PLAN INSERT INTO workspaces(workspace_id, root_path) VALUES ('x','/x')",
        "EXPLAIN SELECT 1",
    ]:
        g.denied = None
        try:
            rows = g.run(con, sql, None, limit=50)[0]
            print(f"[6c] {sql[:80]!r}: 実行 {len(rows)} 行 例={rows[:2]}")
        except sqlite3.Error as e:
            print(f"[6c] {sql[:80]!r}: 拒否 {type(e).__name__}: {e} denied={g.denied}")
    con.close()
    shutil.rmtree(d, ignore_errors=True)


def open_cost() -> None:
    hdr(0, "公開接続を毎回開く費用（設計書 §6.2 の「開く費用を測る」）")
    d = tmpdir()
    p = d / "idx.db"
    seed(p).close()
    ts = []
    for _ in range(300):
        t0 = time.perf_counter()
        con, g = open_public(p)
        g.run(con, "SELECT urn FROM symbols WHERE name = 'foo5'", None, limit=10)
        con.close()
        ts.append((time.perf_counter() - t0) * 1000)
    print(f"[0] open_public + 1 クエリ + close (ms): {p_stats(ts)}")
    shutil.rmtree(d, ignore_errors=True)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--writer":
        writer_proc(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]) if len(sys.argv) > 4 else 0)
        return
    print(f"sqlite {sqlite3.sqlite_version} / python {sys.version.split()[0]} / {sys.platform}")
    items = {"0": open_cost, "1": item1, "2": lambda: (item2(), item2b()), "3": item3, "4": item4, "5": item5, "6": item6}
    want = sys.argv[1:] or ["1", "2", "3", "4", "5", "6", "0"]
    for k in want:
        items[k]()


if __name__ == "__main__":
    main()
