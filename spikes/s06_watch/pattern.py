"""S-6 watcher.py の型（検証済み）。python spikes/s06_watch/pattern.py で自己検査する。

要点: awatch(step=10, debounce=1000) -> 種別は捨ててパス集合に畳む -> 大きい窓だけ静まるまで合算 -> 500 超なら rescan。
パスごとの扱い（存在・sha256・ディレクトリ）は resolve() が stat で決める。イベントの種別は信用しない。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchfiles import awatch

STEP_MS = 10  # 既定 50。単発変更の遅延はほぼ 2×step で決まる（p95: 50->100.6 ms / 10->20 ms）
DEBOUNCE_MS = 1000  # 連続して「新しいパス」が増える間のグループ化の上限。単発の遅延には効かない
BURST_HINT = 100  # 1 窓がこの件数以上なら「続きが来る」とみなして静まるまで待つ
BURST_QUIET_S = 0.3
WATCH_BURST_FILES = 500


def make_filter(root: Path, extra: Callable[[str], bool] | None = None) -> Callable[[object, str], bool]:
    root_s = str(root)

    def accept(_change: object, path: str) -> bool:
        rel = path[len(root_s) :].lstrip("\\/") if path.startswith(root_s) else path
        parts = rel.replace("\\", "/").split("/")
        if ".git" in parts or parts[-1].startswith(".lucianke-tmp-"):
            return False
        return extra(rel) if extra else True  # .gitignore / exclude_globs

    return accept


async def pump(root: Path, q: asyncio.Queue[set[str] | None], stop: threading.Event | asyncio.Event) -> None:
    async for changes in awatch(root, debounce=DEBOUNCE_MS, step=STEP_MS, watch_filter=make_filter(root), stop_event=stop):
        q.put_nowait({os.path.relpath(p, root).replace("\\", "/") for _, p in changes})  # 種別は捨てる
    q.put_nowait(None)


async def consume(q: asyncio.Queue[set[str] | None], on_paths: Callable[[set[str]], Awaitable[None]], on_rescan: Callable[[], Awaitable[None]]) -> None:
    while (paths := await q.get()) is not None:
        ended = False
        while len(paths) >= BURST_HINT:  # 大きい窓だけ、静まる（0.3 秒来ない）まで合算。小さい窓は待たず即処理
            try:
                nxt = await asyncio.wait_for(q.get(), BURST_QUIET_S)  # queue.get のキャンセルは安全（awatch 自体は止めない）
            except TimeoutError:
                break
            if nxt is None:
                ended = True
                break
            paths |= nxt
        await (on_rescan() if len(paths) > WATCH_BURST_FILES else on_paths(paths))
        if ended:
            return


def resolve(root: Path, paths: set[str]) -> tuple[set[str], set[str]]:
    """(現存するファイル, 消えたパス)。ディレクトリは中身を歩いて展開する（移動・改名は子のイベントが来ない）。
    消えたパスが旧ディレクトリなら、呼び出し側が files.path の前方一致 `path + '/'` で配下を除く。"""
    present: set[str] = set()
    gone: set[str] = set()
    for rel in paths:
        p = root / rel
        try:
            if p.is_dir():
                for dp, _dn, fn in os.walk(p):
                    present.update(os.path.relpath(os.path.join(dp, f), root).replace("\\", "/") for f in fn)
            elif p.exists():
                present.add(rel)
            else:
                gone.add(rel)
        except OSError:
            gone.add(rel)
    return present, gone


async def _selftest() -> None:
    root = Path(tempfile.mkdtemp(prefix="lucianke-s06-pat-"))
    outside = Path(tempfile.mkdtemp(prefix="lucianke-s06-out-"))
    try:
        got: list[tuple[float, set[str]]] = []
        rescans: list[float] = []
        q: asyncio.Queue[set[str] | None] = asyncio.Queue()
        stop = asyncio.Event()

        async def on_paths(ps: set[str]) -> None:
            got.append((time.perf_counter(), ps))

        async def on_rescan() -> None:
            rescans.append(time.perf_counter())

        pt = asyncio.create_task(pump(root, q, stop))
        ct = asyncio.create_task(consume(q, on_paths, on_rescan))
        await asyncio.sleep(0)  # 開始通知は無い。タスクが 1 回走れば（RustNotify が作られれば）以降の変更は溜まる
        await asyncio.sleep(0)
        t = time.perf_counter()
        (root / "first.py").write_text("x")  # 起動直後の書き込みも取りこぼさないか
        await asyncio.sleep(0.5)
        print(f"起動直後の書き込み: {'届いた' if any('first.py' in p for _, p in got) else '取りこぼし'} ({(got[0][0] - t) * 1000:.0f} ms)" if got else "起動直後の書き込み: 取りこぼし")
        loop = asyncio.get_running_loop()
        lat = []
        for i in range(30):
            n = len(got)
            t = time.perf_counter()
            (root / "one.py").write_text(f"v{i}")
            while len(got) == n:
                await asyncio.sleep(0.001)
            lat.append((got[n][0] - t) * 1000)
            await asyncio.sleep(0.1)
        lat.sort()
        print(f"単発 30 回: p50={lat[15]:.1f} ms p95={lat[28]:.1f} ms max={lat[-1]:.1f} ms")
        # ディレクトリ移動 + resolve
        (outside / "pkg").mkdir()
        for i in range(3):
            (outside / "pkg" / f"m{i}.py").write_text("x")
        got.clear()
        os.rename(outside / "pkg", root / "pkg")
        await asyncio.sleep(0.4)
        present, gone = resolve(root, set().union(*(p for _, p in got)))
        print(f"ディレクトリ移動 -> resolve: 現存={sorted(present)} 消えた={sorted(gone)}")
        # バースト 700 ファイル
        got.clear()
        for d in range(10):
            (root / f"d{d}").mkdir()
        for i in range(700):
            (root / f"d{i % 10}" / f"f{i}.py").write_text("x")
        await asyncio.sleep(1.2)
        print(f"700 ファイル作成: on_paths={len(got)} 回 on_rescan={len(rescans)} 回")
        # 停止
        t = time.perf_counter()
        stop.set()
        await asyncio.gather(pt, ct)
        print(f"停止: {(time.perf_counter() - t) * 1000:.0f} ms")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(_selftest())
