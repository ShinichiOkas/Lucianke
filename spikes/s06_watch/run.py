"""S-6 watchfiles の Windows 挙動: 遅延・バースト・イベントの種類・フィルタ・停止の再現スクリプト。
使い方: uv run python spikes/s06_watch/run.py            # 項目 10〜12 と補足を全部実行（約 90 秒）
        uv run python spikes/s06_watch/run.py 10 12       # 項目番号を指定（10 / 11 / 12 / 13）
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
    s.arrived.wait(3)
    print(f"[13] 5000 ファイルのバースト後の単一変更の遅延: {(s.batches[0][0] - t) * 1000:.0f} ms" if s.batches else "[13] バースト後のイベントが届かない")
    await s.close()


async def main() -> None:
    print(f"python {sys.version.split()[0]} / watchfiles {watchfiles.__version__} / {sys.platform}")
    items = {"10": item10, "11": item11, "12": item12, "13": item13}
    for k in sys.argv[1:] or list(items):
        await items[k]()


if __name__ == "__main__":
    asyncio.run(main())
