"""S-10 Windows の原子的改名（os.replace）: 共有違反・再試行・バイト保持・fsync の再現スクリプト。
使い方: uv run python spikes/s10_replace/run.py            # 項目 7〜9 を全部実行（約 20 秒、Windows 専用）
        uv run python spikes/s10_replace/run.py 8          # 項目番号を指定（7 / 8 / 9）
一時ファイルは %TEMP% 配下に作り、終了時に消す。リポジトリには何も書かない。
"""

from __future__ import annotations

import atexit
import ctypes
import os
import shutil
import statistics
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.CreateFileW.restype = wintypes.HANDLE
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
INVALID = wintypes.HANDLE(-1).value
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
SHARE_R, SHARE_W, SHARE_D = 1, 2, 4
OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, FILE_FLAG_BACKUP_SEMANTICS = 3, 0x80, 0x02000000

_DIRS: list[Path] = []


def tmpdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="lucianke-s10-"))
    _DIRS.append(d)
    return d


@atexit.register
def _cleanup() -> None:
    for d in _DIRS:
        for p in d.rglob("*"):
            try:
                os.chmod(p, stat.S_IWRITE)
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)


def hold(path: Path, share: int, access: int = GENERIC_READ) -> Callable[[], None]:
    """CreateFileW で path を開いたままにする。戻り値の関数で閉じる。"""
    h = k32.CreateFileW(str(path), access, share, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if h == INVALID:
        raise OSError(ctypes.get_last_error(), "CreateFileW 失敗")
    return lambda: k32.CloseHandle(h)


def err_str(e: OSError) -> str:
    return f"{type(e).__name__} errno={e.errno} winerror={getattr(e, 'winerror', None)}"


def replace_with_retry(
    tmp: Path, target: Path, delays: Sequence[float] = (0.02,) * 5, retry_winerrors: frozenset[int] = frozenset({5, 32})
) -> tuple[bool, int, float, str]:
    """(成功, 試行回数, 経過 ms, 最後のエラー)。設計書 §7.7.5 手順 5 の最小実装。"""
    t0 = time.perf_counter()
    last = ""
    for attempt in range(len(delays) + 1):
        try:
            os.replace(tmp, target)
            return True, attempt + 1, (time.perf_counter() - t0) * 1000, last
        except PermissionError as e:
            last = err_str(e)
            if getattr(e, "winerror", None) not in retry_winerrors or attempt == len(delays):
                return False, attempt + 1, (time.perf_counter() - t0) * 1000, last
            time.sleep(delays[attempt])
    raise AssertionError


def hdr(n: int, title: str) -> None:
    print(f"\n=== 項目 {n}: {title} ===")


# ---------------------------------------------------------------- 項目 7
def item7() -> None:
    hdr(7, "os.replace(tmp, target): target を別の手段で開いているときの挙動")
    d = tmpdir()
    t, tmp = d / "t.txt", d / ".lucianke-tmp-ed_0001-123456"

    def setup() -> None:
        t.write_bytes(b"old")
        tmp.write_bytes(b"new")

    def attempt(label: str, closer: Callable[[], None] | None) -> None:
        try:
            os.replace(tmp, t)
            res = "成功"
        except OSError as e:
            res = f"失敗 {err_str(e)}"
        if closer:
            closer()
        after = t.read_bytes() if t.exists() else None
        print(f"[7] {label}: {res} / 閉じた後の target={after} tmp 残存={tmp.exists()}")

    setup()
    f = open(t, "rb")  # noqa: SIM115
    attempt("(a) target を Python open() で開いたまま", f.close)
    setup()
    attempt("(b) target を CreateFileW 共有=READ|WRITE（DELETE なし）で開いたまま", hold(t, SHARE_R | SHARE_W))
    setup()
    attempt("(c) target を CreateFileW 共有=0 で開いたまま", hold(t, 0))
    setup()
    attempt("(f) target を CreateFileW 共有=READ|WRITE|DELETE で開いたまま（libuv / Node.js 流）", hold(t, SHARE_R | SHARE_W | SHARE_D))
    setup()
    attempt("(f') target を書き込みアクセス＋共有=READ|WRITE|DELETE で開いたまま", hold(t, SHARE_R | SHARE_W | SHARE_D, GENERIC_READ | GENERIC_WRITE))
    setup()
    attempt("(e) tmp 側を共有=READ|WRITE（DELETE なし）で開いたまま（ウイルス対策・インデクサのパターン）", hold(tmp, SHARE_R | SHARE_W))
    setup()
    attempt("(e') tmp 側を共有=READ|WRITE|DELETE で開いたまま", hold(tmp, SHARE_R | SHARE_W | SHARE_D))
    setup()
    os.chmod(t, stat.S_IREAD)
    attempt("(d) target が読み取り専用属性（ハンドルは無し）", None)
    os.chmod(t, stat.S_IWRITE)
    setup()
    os.chmod(tmp, stat.S_IREAD)
    attempt("(d') tmp が読み取り専用属性で target は通常", None)
    print(f"[7] (d') 改名後の target の読み取り専用属性={not os.access(t, os.W_OK)}（tmp の属性がそのまま移る）")
    os.chmod(t, stat.S_IWRITE)
    # 別プロセスが開いている
    setup()
    code = "import sys,time; f=open(sys.argv[1],'rb'); print('ready',flush=True); time.sleep(1.0)"
    p = subprocess.Popen([sys.executable, "-c", code, str(t)], stdout=subprocess.PIPE, text=True)
    p.stdout.readline()
    attempt("(g) 別プロセスが Python open() で開いたまま", None)
    p.wait()
    # 存在しない target / 同じ内容の target
    setup()
    t.unlink()
    attempt("(h) target が存在しない（新規ファイルの改名）", None)
    # 改名先を開いている相手が FILE_SHARE_DELETE を許していても os.replace は通らない。POSIX 意味論の改名ならどうか
    print("[7] --- 参考: POSIX 意味論の改名（SetFileInformationByHandle, FileRenameInfoEx, REPLACE_IF_EXISTS|POSIX_SEMANTICS）---")
    for label, share in (("(a') Python open() 相当 共有=READ|WRITE", SHARE_R | SHARE_W), ("(c') 共有=0", 0), ("(f'') 共有=READ|WRITE|DELETE", SHARE_R | SHARE_W | SHARE_D)):
        setup()
        closer = hold(t, share)
        try:
            posix_replace(tmp, t)
            res = "成功"
        except OSError as e:
            res = f"失敗 {err_str(e)}"
        closer()
        print(f"[7]   {label}: {res} / 閉じた後の target={t.read_bytes()}")
    setup()
    closer = hold(t, SHARE_R | SHARE_W | SHARE_D)
    try:
        posix_replace(tmp, t)
        print("[7]   (f'') 成功後、保持者がいる間に新しく開くと:", t.read_bytes())
    except OSError:
        pass
    closer()


FILE_RENAME_FLAG_REPLACE_IF_EXISTS, FILE_RENAME_FLAG_POSIX_SEMANTICS, FileRenameInfoEx = 0x1, 0x2, 22
DELETE_ACCESS = 0x10000


class FileRenameInfo(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE), ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * 520)]


def posix_replace(src: Path, dst: Path) -> None:
    """Windows 10 1709 以降: src を DELETE 権限で開き、POSIX 意味論で dst に改名する。失敗は OSError。"""
    h = k32.CreateFileW(str(src), DELETE_ACCESS | GENERIC_READ, SHARE_R | SHARE_W | SHARE_D, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if h == INVALID:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = FileRenameInfo()
        info.Flags = FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS
        name = str(dst)
        info.FileName = name
        info.FileNameLength = len(name) * 2
        k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        if not k32.SetFileInformationByHandle(h, FileRenameInfoEx, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(h)


# ---------------------------------------------------------------- 項目 8
def item8() -> None:
    hdr(8, "再試行（20 ms × 5 回）で、相手が一定時間後にハンドルを閉じる場合")
    d = tmpdir()
    t, tmp = d / "t.txt", d / ".lucianke-tmp-ed_0001-123456"
    sl = []
    for _ in range(30):
        t0 = time.perf_counter()
        time.sleep(0.02)
        sl.append((time.perf_counter() - t0) * 1000)
    print(f"[8] time.sleep(0.02) の実測(ms): p50={statistics.median(sl):.1f} max={max(sl):.1f}（Python 3.12 は Windows で高分解能タイマーを使う）")
    policies = {
        "A 固定 20ms×5（設計書 §7.7.5）": (0.02,) * 5,
        "B 指数 10,20,40,80,160ms（合計 310ms）": (0.01, 0.02, 0.04, 0.08, 0.16),
    }
    for hold_ms in (10, 50, 90, 110, 200, 400):
        row = []
        for name, delays in policies.items():
            ok_n, att, el = 0, [], []
            for _ in range(5):
                t.write_bytes(b"old")
                tmp.write_bytes(b"new")
                close = hold(t, SHARE_R | SHARE_W)
                timer = threading.Timer(hold_ms / 1000, close)
                timer.start()
                ok, a, e, _err = replace_with_retry(tmp, t, delays)
                timer.join()
                ok_n += ok
                att.append(a)
                el.append(e)
                if not ok:
                    assert t.read_bytes() == b"old"  # 失敗しても target は無傷
                    tmp.unlink()
            row.append(f"{name}: 成功 {ok_n}/5 試行回数={att} 経過 p50={statistics.median(el):.0f}ms")
        print(f"[8] 相手が {hold_ms:3d} ms 後に閉じる -> " + " | ".join(row))
    # winerror が 5 でも 32 でもない失敗は再試行しない（読み取り専用属性）
    t.write_bytes(b"old")
    tmp.write_bytes(b"new")
    os.chmod(t, stat.S_IREAD)
    ok, a, e, err = replace_with_retry(tmp, t)
    print(f"[8] target が読み取り専用: 成功={ok} 試行={a} 経過={e:.0f}ms {err}（読み取り専用も winerror=5 なので再試行しても直らない）")
    os.chmod(t, stat.S_IWRITE)


# ---------------------------------------------------------------- 項目 9
def item9() -> None:
    hdr(9, "改名後のバイト保持と fsync")
    d = tmpdir()
    t = d / "t.txt"
    cases = {
        "CRLF+BOM+末尾改行なし": b"\xef\xbb\xbfline1\r\nline2\r\nlast",
        "LF+末尾改行あり": b"a\nb\n",
        "CR のみ": b"a\rb\rc",
        "混在(先頭 LF)+日本語": "あ\nい\r\nう\r".encode(),
        "空ファイル": b"",
        "NUL を含む": b"a\x00b\r\n",
    }
    for name, content in cases.items():
        t.write_bytes(b"old\r\n")
        tmp = d / ".lucianke-tmp-ed_0002-654321"
        with open(tmp, "wb") as f:  # 必ずバイナリ。テキストモードだと \n が \r\n になる
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, t)
        print(f"[9] {name}: 改名後のバイト列が一致={t.read_bytes() == content} ({len(content)} B)")
    # テキストモードの罠
    tmp = d / "trap"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("a\nb\n")
    print(f"[9] 罠: open(mode='w') に 'a\\nb\\n' を書くと {tmp.read_bytes()!r}（\\n が \\r\\n に変わる）")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("a\nb\n")
    print(f"[9] open(mode='w', newline='') なら {tmp.read_bytes()!r}")
    # 属性・タイムスタンプ
    t.write_bytes(b"old")
    m0 = t.stat().st_mtime
    time.sleep(0.05)
    tmp = d / "tmp3"
    tmp.write_bytes(b"new")
    os.replace(tmp, t)
    print(f"[9] mtime は tmp の書き込み時刻になる（旧 mtime から {t.stat().st_mtime - m0:+.3f} 秒）")
    # 作成時刻・代替データストリーム・明示 ACL は引き継がれない
    t.write_bytes(b"old")
    with open(str(t) + ":meta", "wb") as f:
        f.write(b"ads")
    subprocess.run(["icacls", str(t), "/grant", "Users:(R)"], capture_output=True)
    b0 = t.stat().st_birthtime
    acl0 = "Users:(R)" in subprocess.run(["icacls", str(t)], capture_output=True, text=True).stdout
    time.sleep(0.1)
    tmp = d / "tmp3b"
    tmp.write_bytes(b"new")
    os.replace(tmp, t)
    acl1 = "Users:(R)" in subprocess.run(["icacls", str(t)], capture_output=True, text=True).stdout
    print(
        f"[9] 改名で失われるもの: 作成時刻は {t.stat().st_birthtime - b0:+.2f} 秒変わる / 代替データストリーム 残存={os.path.exists(str(t) + ':meta')} / "
        f"明示 ACE(Users:R) 前={acl0} 後={acl1}（tmp 側の継承 ACL になる）"
    )

    # fsync
    ts = []
    for _ in range(200):
        tmp = d / "tmp4"
        t0 = time.perf_counter()
        with open(tmp, "wb") as f:
            f.write(b"x" * 4096)
            f.flush()
            os.fsync(f.fileno())
        ts.append((time.perf_counter() - t0) * 1000)
        tmp.unlink()
    ts.sort()
    print(f"[9] ファイルの write+flush+os.fsync(fd)（4 KB、os.fsync は FlushFileBuffers）(ms): p50={statistics.median(ts):.2f} p95={ts[189]:.2f} max={ts[-1]:.2f}")
    ts = []
    for _ in range(200):
        tmp = d / "tmp5"
        t0 = time.perf_counter()
        with open(tmp, "wb") as f:
            f.write(b"x" * 4096)
        ts.append((time.perf_counter() - t0) * 1000)
        tmp.unlink()
    ts.sort()
    print(f"[9] 同じ書き込みで fsync 無し (ms): p50={statistics.median(ts):.2f} p95={ts[189]:.2f}")
    ts = []
    for _ in range(200):
        tmp = d / "tmp6"
        tmp.write_bytes(b"y")
        t0 = time.perf_counter()
        os.replace(tmp, t)
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    print(f"[9] os.replace 単体 (ms): p50={statistics.median(ts):.2f} p95={ts[189]:.2f}")
    # ディレクトリの fsync
    try:
        fd = os.open(d, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
        print("[9] os.open(dir, O_RDONLY) + os.fsync: 成功")
    except OSError as e:
        print(f"[9] os.open(dir, O_RDONLY): 失敗 {err_str(e)} {e.strerror}（Windows では Python からディレクトリを開けない）")
    for access, label in ((GENERIC_READ, "GENERIC_READ"), (GENERIC_WRITE, "GENERIC_WRITE")):
        h = k32.CreateFileW(str(d), access, SHARE_R | SHARE_W | SHARE_D, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if h == INVALID:
            print(f"[9] CreateFileW(dir, {label}, BACKUP_SEMANTICS): 失敗 winerror={ctypes.get_last_error()}")
            continue
        ok = k32.FlushFileBuffers(h)
        print(f"[9] CreateFileW(dir, {label}, BACKUP_SEMANTICS) + FlushFileBuffers: {'成功' if ok else f'失敗 winerror={ctypes.get_last_error()}'}")
        k32.CloseHandle(h)


def main() -> None:
    if sys.platform != "win32":
        print("このスパイクは Windows 専用です")
        return
    print(f"python {sys.version.split()[0]} / {sys.platform} / {os.environ.get('OS', '')}")
    items = {"7": item7, "8": item8, "9": item9}
    for k in sys.argv[1:] or ["7", "8", "9"]:
        items[k]()


if __name__ == "__main__":
    main()
