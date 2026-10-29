"""S-6 watchfiles の Windows 挙動: 遅延・バースト・イベントの種類・フィルタ・停止の再現スクリプト。
使い方: .venv/Scripts/python.exe spikes/s06_watch/run.py        # 項目 10〜16 を全部実行（約 2〜3 分）
        .venv/Scripts/python.exe spikes/s06_watch/run.py 10 14    # 項目番号を指定（10〜16）
一時ファイルは %TEMP% 配下に作り、終了時に消す。リポジトリには何も書かない。
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import watchfiles
from watchfiles import Change, awatch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_DIRS: list[Path] = []


def tmpdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="lucianke-s06-"))
    _DIRS.append(d)
    return d


@atexit.register
def _cleanup() -> None:
    for d in _DIRS:
        shutil.rmtree(d, ignore_errors=True)


NAMES = {Change.added: "added", Change.modified: "modified", Change.deleted: "deleted"}
Filter = Callable[[Change, str], bool] | None


def make_filter(root: Path) -> Callable[[Change, str], bool]:
    """Lucianke の監視フィルタの型。.git/・自前の一時ファイル・__pycache__ を除く。.gitignore と exclude_globs は別途。"""
    root_s = str(root)

    def accept(change: Change, path: str) -> bool:
        rel = path[len(root_s) :].lstrip("\\/") if path.startswith(root_s) else path
        parts = rel.replace("\\", "/").split("/")
        if ".git" in parts or "__pycache__" in parts:
            return False
        return not parts[-1].startswith(".lucianke-tmp-")

    return accept


class Session:
    """awatch をイベントループ上で走らせ、バッチを (受信時刻, {(種別, 相対パス)}) で溜める。"""

    def __init__(self, root: Path, *, debounce: int, step: int, watch_filter: Filter, force_polling: bool = False, poll_delay_ms: int = 300) -> None:
        self.root = root
        self.batches: list[tuple[float, set[tuple[str, str]]]] = []
        self.stop = threading.Event()
        self.arrived = threading.Event()
        self._kw = dict(debounce=debounce, step=step, watch_filter=watch_filter, force_polling=force_polling, poll_delay_ms=poll_delay_ms)
        self.task: asyncio.Task | None = None

    async def _run(self) -> None:
        async for changes in awatch(self.root, stop_event=self.stop, **self._kw):
            t = time.perf_counter()
            self.batches.append((t, {(NAMES[c], os.path.relpath(p, self.root).replace("\\", "/")) for c, p in changes}))
            self.arrived.set()

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run())
        probe = self.root / "_warmup.txt"
        for _ in range(100):  # 準備ができるまで書き続ける（awatch に ready 通知は無い）
            probe.write_text(str(time.perf_counter()))
            await asyncio.sleep(0.1)
            if self.batches:
                break
        else:
            raise RuntimeError("監視が始まらない")
        await asyncio.sleep(0.3)
        self.batches.clear()
        self.arrived.clear()

    async def close(self) -> float:
        t0 = time.perf_counter()
        self.stop.set()
        assert self.task
        await self.task
        return (time.perf_counter() - t0) * 1000


def pstats(xs: list[float]) -> str:
    xs = sorted(xs)
    return f"n={len(xs)} p50={statistics.median(xs):.1f} p95={xs[max(0, int(len(xs) * 0.95) - 1)]:.1f} max={xs[-1]:.1f}"


def hdr(n: int, title: str) -> None:
    print(f"\n=== 項目 {n}: {title} ===")


# ---------------------------------------------------------------- 項目 10
async def latency(debounce: int, step: int, n: int = 100, *, polling: bool = False) -> tuple[list[float], list[int]]:
    root = tmpdir()
    s = Session(root, debounce=debounce, step=step, watch_filter=make_filter(root), force_polling=polling)
    await s.start()
    target = root / "a.txt"
    target.write_text("x")
    await asyncio.sleep(0.5)
    s.batches.clear()
    lat: list[float] = []
    nb: list[int] = []
    loop = asyncio.get_running_loop()

    def writer() -> None:
        for i in range(n):
            s.arrived.clear()
            before = len(s.batches)
            target.write_bytes(f"v{i}".encode() * 100)
            t_done = time.perf_counter()
            if not s.arrived.wait(5):
                lat.append(float("inf"))
                continue
            lat.append((s.batches[before][0] - t_done) * 1000)
            time.sleep(0.2 if not polling else 0.5)
            nb.append(len(s.batches) - before)

    await loop.run_in_executor(None, writer)
    await s.close()
    return lat, nb


async def item10() -> None:
    hdr(10, "単一ファイル書き換えからイベント受信までの遅延（別スレッドが書き、受信を待って次を書く。100 回）")
    for debounce, step in ((50, 50), (50, 20), (50, 10), (50, 5), (1600, 50), (1600, 10), (200, 10)):
        lat, nb = await latency(debounce, step)
        print(f"[10] debounce={debounce:4d} step={step:2d}: 遅延(ms) {pstats(lat)}  1 回の書き換えあたりのバッチ数 最大={max(nb)}")
    lat, nb = await latency(50, 50, 10, polling=True)
    print(f"[10] 参考 force_polling=True poll_delay_ms=300 (debounce=50 step=50): 遅延(ms) {pstats(lat)}")


# ---------------------------------------------------------------- 項目 11
async def burst(n_files: int, debounce: int, step: int) -> None:
    root = tmpdir()
    s = Session(root, debounce=debounce, step=step, watch_filter=make_filter(root))
    await s.start()
    loop = asyncio.get_running_loop()
    dirs = [root / f"d{i}" for i in range(10)]
    t_info: dict[str, float] = {}

    def creator() -> None:
        for d in dirs:
            d.mkdir()
        t_info["start"] = time.perf_counter()
        for i in range(n_files):
            (dirs[i % 10] / f"f{i}.py").write_text(f"x = {i}\n")
        t_info["end"] = time.perf_counter()

    await loop.run_in_executor(None, creator)
    await asyncio.sleep(max(1.0, debounce / 1000 * 3))
    t0 = t_info["start"]
    sizes = [len(b) for _, b in s.batches]
    got_files = {p for _, b in s.batches for k, p in b if p.endswith(".py")}
    want = {f"d{i % 10}/f{i}.py" for i in range(n_files)}
    first = (s.batches[0][0] - t0) * 1000 if s.batches else float("nan")
    last = (s.batches[-1][0] - t0) * 1000 if s.batches else float("nan")
    print(
        f"[11] {n_files} ファイル作成（作成に {1000 * (t_info['end'] - t0):.0f} ms）debounce={debounce} step={step}: バッチ数={len(sizes)} "
        f"各バッチの件数={sizes[:12]}{'…' if len(sizes) > 12 else ''} 最大={max(sizes) if sizes else 0} "
        f"取りこぼし={len(want - got_files)} 余分={len(got_files - want)} 最初のバッチ={first:.0f} ms 最後={last:.0f} ms"
    )
    await s.close()


async def item11() -> None:
    hdr(11, "多数ファイルを一斉に作ったときのイベントの届き方（バッチ数・件数・取りこぼし）")
    for debounce, step in ((50, 50), (50, 10), (200, 50), (1000, 50), (1600, 50)):
        await burst(600, debounce, step)
    await burst(5000, 50, 50)
    await burst(5000, 1000, 50)


# ---------------------------------------------------------------- 項目 12
async def item12() -> None:
    hdr(12, "イベントの種類（改名・削除・ディレクトリ作成）と watch_filter による .git/ の除外")
    root = tmpdir()
    (root / ".git" / "objects").mkdir(parents=True)
    s = Session(root, debounce=50, step=50, watch_filter=None)  # フィルタ無し（生のイベント）
    await s.start()
    loop = asyncio.get_running_loop()
    ops: list[tuple[str, Callable[[], None]]] = []
    a, b, tmpf = root / "a.py", root / "b.py", root / ".lucianke-tmp-ed_0001-123456"

    def op(label: str):  # noqa: ANN202
        def deco(fn: Callable[[], None]) -> Callable[[], None]:
            ops.append((label, fn))
            return fn

        return deco

    @op("新規ファイル a.py を作る")
    def _() -> None:
        a.write_text("1")

    @op("a.py を書き換える（内容変更）")
    def _() -> None:
        a.write_text("22")

    @op("同じバイト列を書き直す（内容は同一）")
    def _() -> None:
        a.write_text("22")

    @op("os.utime で mtime だけ変える")
    def _() -> None:
        os.utime(a, (time.time() + 5, time.time() + 5))

    @op("tmp を書いて os.replace(tmp, a.py)（既存を置換 = EditTransaction の書き込み）")
    def _() -> None:
        tmpf.write_text("333")
        os.replace(tmpf, a)

    @op("os.replace(tmp, b.py)（存在しない名前へ）")
    def _() -> None:
        tmpf.write_text("4")
        os.replace(tmpf, b)

    @op("os.rename(b.py, c.py)")
    def _() -> None:
        os.rename(b, root / "c.py")

    @op("c.py を削除")
    def _() -> None:
        (root / "c.py").unlink()

    @op("ディレクトリ sub を作る")
    def _() -> None:
        (root / "sub").mkdir()

    @op("sub の中にファイルを作る（作ったばかりのディレクトリも再帰的に監視されるか）")
    def _() -> None:
        (root / "sub" / "x.py").write_text("x")

    @op("sub を丸ごと shutil.rmtree")
    def _() -> None:
        shutil.rmtree(root / "sub")

    @op(".git/index.lock を作って .git/index へ os.replace")
    def _() -> None:
        (root / ".git" / "index.lock").write_text("l")
        os.replace(root / ".git" / "index.lock", root / ".git" / "index")

    for label, fn in ops:
        s.batches.clear()
        await loop.run_in_executor(None, fn)
        await asyncio.sleep(0.4)
        evs = sorted(e for _, bt in s.batches for e in bt)
        print(f"[12] {label}\n       -> バッチ数={len(s.batches)} {evs}")
    await s.close()

    # 同じ操作列にフィルタを掛ける
    root2 = tmpdir()
    (root2 / ".git" / "objects").mkdir(parents=True)
    s2 = Session(root2, debounce=50, step=50, watch_filter=make_filter(root2))
    await s2.start()

    def churn() -> None:
        for i in range(2000):
            (root2 / ".git" / "objects" / f"o{i}").write_text("o")
        (root2 / ".git" / "index.lock").write_text("l")
        os.replace(root2 / ".git" / "index.lock", root2 / ".git" / "index")
        (root2 / ".lucianke-tmp-ed_0001-1").write_text("t")
        os.replace(root2 / ".lucianke-tmp-ed_0001-1", root2 / "real.py")

    t0 = time.perf_counter()
    await loop.run_in_executor(None, churn)
    await asyncio.sleep(0.5)
    evs = sorted(e for _, bt in s2.batches for e in bt)
    print(f"[12] watch_filter 有り: .git/ に 2000 ファイル＋index 置換、.lucianke-tmp-* の作成と改名を行う ({1000 * (time.perf_counter() - t0):.0f} ms) -> 届いたイベント={evs}")
    await s2.close()
    print(f"[12] DefaultFilter().ignore_dirs に .git が含まれる: {'.git' in watchfiles.DefaultFilter.ignore_dirs}（Rust 側の除外手段は無く、フィルタは Python 側で全イベントに対して呼ばれる）")


# ---------------------------------------------------------------- 補足
async def item13() -> None:
    hdr(13, "停止・開始失敗・巨大バースト後の復帰")
    root = tmpdir()
    s = Session(root, debounce=50, step=50, watch_filter=make_filter(root))
    await s.start()
    ms = await s.close()
    print(f"[13] stop_event(threading.Event).set() から awatch が終わるまで: {ms:.0f} ms")
    try:
        async for _ in awatch(root / "no_such_dir", stop_event=threading.Event()):
            break
    except Exception as e:  # noqa: BLE001
        print(f"[13] 存在しないパスを監視: {type(e).__name__}: {e}")
    # 5000 ファイルのバースト直後に通常の変更が通常の遅延で届くか
    root = tmpdir()
    s = Session(root, debounce=50, step=50, watch_filter=make_filter(root))
    await s.start()
    loop = asyncio.get_running_loop()

    def go() -> None:
        for i in range(5000):
            (root / f"f{i}.py").write_text("x")

    await loop.run_in_executor(None, go)
    await asyncio.sleep(1.0)
    s.batches.clear()
    s.arrived.clear()
    t = time.perf_counter()
    (root / "after.py").write_text("y")
    await loop.run_in_executor(None, s.arrived.wait, 3)  # イベントループを塞がない（塞ぐと awatch が進まず「届かない」ように見える）
    print(f"[13] 5000 ファイルのバースト後の単一変更の遅延: {(s.batches[0][0] - t) * 1000:.0f} ms" if s.batches else "[13] バースト後のイベントが届かない")
    await s.close()


# ---------------------------------------------------------------- 項目 14
async def item14() -> None:
    hdr(14, "バースト検知（500 件超で rescan）の成立条件: 窓ごとの件数・持続書き込みの最大遅延・実際の git checkout")
    for debounce, step in ((50, 10), (200, 10), (1000, 10), (2000, 10)):
        await burst(600, debounce, step)
    # 持続的に書き換わるファイル（ログなど）を debounce が抱え込む時間
    for debounce, step in ((50, 10), (1000, 10)):
        root = tmpdir()
        s = Session(root, debounce=debounce, step=step, watch_filter=make_filter(root))
        await s.start()
        loop = asyncio.get_running_loop()
        done = threading.Event()
        t_info: dict[str, float] = {}

        def spam() -> None:
            t_info["t0"] = time.perf_counter()
            while time.perf_counter() - t_info["t0"] < 3.0:  # 3 秒間、3 ms ごとに書く
                (root / "log.txt").write_bytes(b"x" * 100)
                time.sleep(0.003)
            done.set()

        await loop.run_in_executor(None, spam)
        await asyncio.sleep(max(0.5, debounce / 1000 * 2))
        ts = [(t - t_info["t0"]) * 1000 for t, _ in s.batches]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        print(f"[14] 3 秒間 3 ms ごとに同じファイルを書く debounce={debounce} step={step}: バッチ数={len(ts)} 最初のバッチ={ts[0]:.0f} ms 最大の間隔={max(gaps) if gaps else float('nan'):.0f} ms")
        await s.close()
    # 実際の git checkout（600 ファイルが入れ替わるブランチ切替）
    import subprocess

    repo = tmpdir()

    def git(*a: str) -> None:
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=s06", *a], cwd=repo, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    for i in range(700):
        d = repo / f"pkg{i % 20}"
        d.mkdir(exist_ok=True)
        (d / f"m{i}.py").write_text(f"v = {i}\n")
    git("add", "-A")
    git("commit", "-q", "-m", "a")
    git("checkout", "-q", "-b", "other")
    for i in range(700):
        (repo / f"pkg{i % 20}" / f"m{i}.py").write_text(f"v = {i + 1000}\n")
    git("commit", "-q", "-am", "b")
    for debounce, step in ((50, 10), (200, 10), (1000, 10)):
        git("checkout", "-q", "main")
        git("checkout", "-q", "other")  # 状態を揃える
        s = Session(repo, debounce=debounce, step=step, watch_filter=make_filter(repo))
        await s.start()
        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        await loop.run_in_executor(None, git, "checkout", "-q", "main")
        t_end = time.perf_counter()
        await asyncio.sleep(max(1.0, debounce / 1000 * 3))
        sizes = [len(b) for _, b in s.batches]
        got = {p for _, b in s.batches for _, p in b if p.endswith(".py")}
        print(f"[14] git checkout（700 ファイルが入れ替わる、{1000 * (t_end - t0):.0f} ms）debounce={debounce} step={step}: バッチ数={len(sizes)} 件数={sizes[:10]} 最大={max(sizes) if sizes else 0} 届いた .py={len(got)}/700 最後のバッチ={1000 * (s.batches[-1][0] - t0):.0f} ms")
        await s.close()


# ---------------------------------------------------------------- 項目 15
async def item15() -> None:
    hdr(15, "停止: asyncio.Event / threading.Event / タスクのキャンセル（stop_event 無し）にかかる時間")
    for kind in ("threading.Event", "asyncio.Event"):
        for step in (50, 10):
            root = tmpdir()
            ev = threading.Event() if kind == "threading.Event" else asyncio.Event()

            async def run() -> None:
                async for _ in awatch(root, stop_event=ev, step=step, watch_filter=None):
                    pass

            task = asyncio.create_task(run())
            await asyncio.sleep(0.5)
            t0 = time.perf_counter()
            ev.set()
            await task
            print(f"[15] stop_event={kind} step={step}: set() から終了まで {(time.perf_counter() - t0) * 1000:.0f} ms")
    for step in (50, 10):
        root = tmpdir()

        async def run2() -> None:
            async for _ in awatch(root, step=step, watch_filter=None):
                pass

        task = asyncio.create_task(run2())
        await asyncio.sleep(0.5)
        t0 = time.perf_counter()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        print(f"[15] stop_event 無しで task.cancel() step={step}: cancel() から終了まで {(time.perf_counter() - t0) * 1000:.0f} ms")


# ---------------------------------------------------------------- 項目 16
async def item16() -> None:
    hdr(16, "アイドル時の CPU（step を小さくした代償）と force_polling の補足")
    for step in (50, 10, 5):
        root = tmpdir()
        stop = threading.Event()

        async def run() -> None:
            async for _ in awatch(root, step=step, stop_event=stop, watch_filter=None):
                pass

        task = asyncio.create_task(run())
        await asyncio.sleep(1.0)
        c0, w0 = time.process_time(), time.perf_counter()
        await asyncio.sleep(6.0)
        c1, w1 = time.process_time(), time.perf_counter()
        print(f"[16] 何も起きない 6 秒 step={step}: プロセスの CPU 時間 {1000 * (c1 - c0):.0f} ms（{100 * (c1 - c0) / (w1 - w0):.2f} %）")
        stop.set()
        await task
    # force_polling の個別値（inf の原因を見る）
    root = tmpdir()
    s = Session(root, debounce=50, step=50, watch_filter=None, force_polling=True, poll_delay_ms=100)
    await s.start()
    loop = asyncio.get_running_loop()
    res: list[str] = []

    def w() -> None:
        for i in range(8):
            s.arrived.clear()
            n0 = len(s.batches)
            (root / "p.txt").write_text(f"v{i}" * (i + 1))  # 毎回サイズも変える
            t = time.perf_counter()
            ok = s.arrived.wait(3)
            res.append(f"{(s.batches[n0][0] - t) * 1000:.0f}ms" if ok else "届かず")
            time.sleep(0.5)

    await loop.run_in_executor(None, w)
    print(f"[16] 参考 force_polling=True poll_delay_ms=100 でサイズを変えて書く 8 回: {res}")
    await s.close()


async def main() -> None:
    print(f"python {sys.version.split()[0]} / watchfiles {watchfiles.__version__} / {sys.platform}")
    items = {"10": item10, "11": item11, "12": item12, "13": item13, "14": item14, "15": item15, "16": item16}
    for k in sys.argv[1:] or list(items):
        await items[k]()


if __name__ == "__main__":
    asyncio.run(main())
