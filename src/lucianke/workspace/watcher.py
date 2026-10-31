"""ファイル監視（設計書 §7.3.6、§8.5、仕様書 §12.4、スパイク S-06）。

``watchfiles.awatch`` のイベントを **パスの集合** に畳み、``IngestSink`` へ ``changed`` / ``removed`` / ``rescan`` で渡す。
S-06（Windows 実測）の結論が設計書 §7.3.6 より優先する:

* ``awatch(step=10, debounce=1000)``。``step=10`` が必須（既定 50 では単発の p95 が 100 ms 前後）。
* **イベントの種別は信用しない**。既存ファイルの ``os.replace`` で ``deleted`` も届く。種別は捨て、各パスを ``stat`` して
  存在すれば ``changed``、不在なら ``removed`` とする（追加 + 削除 = 削除、の畳み込みはしない）。
* **ディレクトリの移動・改名は子のイベントが来ない**。ディレクトリは ``os.walk`` で展開して取り込み、消えたパスは
  ``sink.known_paths_under("パス/")`` の前方一致で配下も ``removed`` にする。
* バースト: 1 窓が ``WATCH_BURST_HINT`` 件以上なら 0.3 秒静まるまで窓を合算し、合算が ``WATCH_BURST_FILES`` を超えたら
  個別処理をやめて ``rescan`` を 1 回だけ呼ぶ。小さい窓は待たず即処理する（単発の遅延を増やさない）。
* ``.git/``・一時名（``.lucianke-tmp-*``）・``exclude_globs`` は ``watch_filter`` で落とす。``.gitignore`` は別の層が担う。
* 停止は ``stop`` イベント（``awatch`` の ``stop_event``）。監視を開始できなければ ``mode = "polling"`` にして何もしない
  （要求時の遅延検証は呼び手の責任。``force_polling`` は取りこぼすので使わない）。

``sink.changed(rel)`` は **冪等** でなければならない。内容（``sha256``）が索引と同じなら何もしない責任は sink 側にある
（エディタの保存で mtime だけ変わる場合の吸収。本モジュールは ``sha256`` を ``expect`` の照合にしか使わない）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from watchfiles import awatch

from lucianke.core.errors import LuciankeError
from lucianke.core.limits import (
    WATCH_BURST_FILES,
    WATCH_BURST_HINT,
    WATCH_BURST_QUIET_S,
    WATCH_DEBOUNCE_MS,
    WATCH_EXPECT_TTL_S,
    WATCH_STEP_MS,
)
from lucianke.core.model import RelPath
from lucianke.workspace.paths import confine, is_excluded, to_abs
from lucianke.workspace.types import Workspace

__all__ = ["TMP_PREFIX", "IngestSink", "Watcher"]

_log = logging.getLogger("lucianke.workspace.watcher")

TMP_PREFIX = ".lucianke-tmp-"  # 編集トランザクションの一時名（edit/writer.py と同じ。workspace は edit を import できない）
_DIR_PROBE = "x"  # 「このディレクトリの配下が除外か」を is_excluded に問う仮の子の名前（末尾 "/" の glob 用）


class IngestSink(Protocol):
    """監視が結果を渡す先（索引の取り込み側が実装する。workspace は core しか import できないのでプロトコルで受ける）。

    * ``changed(rel)``: ファイルが作られた・変わったかもしれない。内容が索引と同じなら何もしないこと（冪等）。
    * ``removed(rel)``: ファイルが消えた。未知のパスでも何もせず返ること（冪等）。
    * ``rescan()``: 個別処理をやめて差分索引（起動時と同じ手順）を 1 回走らせる。
    * ``known_paths_under(prefix)``: 索引に載っている、``prefix`` で始まるパス（``prefix`` は ``"dir/"`` の形）。同期。
    """

    async def changed(self, rel: RelPath) -> None: ...

    async def removed(self, rel: RelPath) -> None: ...

    async def rescan(self) -> None: ...

    def known_paths_under(self, prefix: str) -> list[RelPath]: ...


@dataclass(slots=True)
class _Flush:
    """``drain`` の合図。キューの順番どおりに届くので、これより前に受けたイベントは全部処理済みになってから完了する。"""

    paths: frozenset[str]
    done: asyncio.Future[None]


_Item = set[str] | _Flush | None  # None = 監視の終わり


def _ancestors(paths: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for p in paths:
        parts = p.split("/")
        for i in range(1, len(parts)):
            out.add("/".join(parts[:i]))
    return out


def _sha256_file(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


class Watcher:
    """1 つのワークスペースの監視（設計書 §7.3.6）。``run`` を 1 つのタスクとして走らせる。"""

    def __init__(
        self,
        ws: Workspace,
        sink: IngestSink,
        *,
        step_ms: int = WATCH_STEP_MS,
        debounce_ms: int = WATCH_DEBOUNCE_MS,
    ) -> None:
        self._ws = ws
        self._sink = sink
        self._step_ms = step_ms
        self._debounce_ms = debounce_ms
        self.mode: Literal["native", "polling"] = "native"
        self.started = asyncio.Event()
        """``run`` が監視タスクを走らせ終えたら立つ（開始通知が無いので、タスクが 2 回走るのを待つ。S-06「起動」）。
        差分索引はこの後に始める（それより前の変更の取りこぼしを防ぐ）。開始に失敗したときも立ち、``mode`` が "polling" になる。"""
        self._fold_names = ws.case_insensitive or os.name == "nt"  # ".git" の比較で大文字小文字を畳むか
        self._queue: asyncio.Queue[_Item] | None = None
        self._expected: dict[RelPath, tuple[str, float]] = {}
        self.generation = 0
        """ルートの下で届いたイベントの数（``.git/``・除外・一時名で落とすものも数える）と、このサーバー自身の書き込み
        （``expect`` / ``forget``。イベントが届く前に数える）の数の和。値が変わっていなければ、前に見てからルートの下は何も
        変わっていない（外からの変更のイベントが届くまでの遅れ〈``step``、最大 ``debounce``〉の間を除く）。T-00 の ``status`` が
        Git の状態の覚えの鍵に使う（BUG-0128）。監視が動いていないときは増えないので、``live`` と合わせて見ること。"""

    # --- 公開 API ----------------------------------------------------------------------

    @property
    def live(self) -> bool:
        """監視が動いていて（``run`` の中）、イベントが届く状態（``native``）か。"""
        return self._queue is not None and self.mode == "native"

    def rebind(self, ws: Workspace) -> None:
        """設定（``exclude_globs`` など）を変えた同じワークスペースに差し替える。以降のイベントは新しい除外規則で落とす。

        ``workspace_id`` と ``root`` は変えられない。監視の対象ディレクトリ（ルート）は変わらないので、走っている監視は止めない。
        """
        if ws.workspace_id != self._ws.workspace_id or ws.root != self._ws.root:
            raise ValueError("rebind needs the same workspace_id and root")
        self._ws = ws

    async def run(self, stop: asyncio.Event) -> None:
        """``stop`` が立つまで監視する。開始できなければ ``mode = "polling"`` にして返る（何もしない）。"""
        if self._queue is not None:
            raise RuntimeError("Watcher.run is already running")
        q: asyncio.Queue[_Item] = asyncio.Queue()
        self._queue = q
        inner = (
            asyncio.Event()
        )  # awatch はキャンセル時に stop_event を立てる。呼び手の stop を汚さないよう中継する
        forwarder = asyncio.create_task(self._forward_stop(stop, inner))
        pump = asyncio.create_task(self._pump(q, inner))
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.started.set()
            await self._consume(q, stop)
        finally:
            self._queue = None
            inner.set()
            forwarder.cancel()
            pump.cancel()
            await asyncio.gather(forwarder, pump, return_exceptions=True)
            await self._release(q)
            self.started.set()

    async def drain(self, paths: Sequence[RelPath] | None = None) -> None:
        """受け取り済みのイベントを即座に処理し、sink の処理が終わるまで待つ（編集トランザクションが前提検証の直前に呼ぶ）。

        ``paths`` を渡すと、それらのパスは **イベントを待たずに** 今のディスクの状態で sink に同期する（``awatch`` の配信が
        まだでも、返った時点でこのパスの索引は最新）。省略時は受け取り済みのイベントを処理し切るだけ。
        監視が動いていない（``polling`` や停止後）ときは、``paths`` の同期だけを呼び手の文脈で行う。
        sink の中から呼んではならない（自分の処理の完了を待って詰まる）。
        """
        raw = {str(p) for p in paths} if paths is not None else set()
        q = self._queue
        if q is None:
            if raw:
                await self._apply(raw)
            return
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        q.put_nowait(_Flush(frozenset(raw), done))
        await done

    def expect(self, rel: RelPath, sha256_after: str) -> None:
        """自分の書き込みのイベント（こだま）を捨てる（二重取り込みの抑止）。

        このパスのイベントが届いたとき、現在の内容の ``sha256`` が ``sha256_after`` と一致すれば捨てる。1 回の書き込みが
        複数のイベントで届き（``os.replace`` は deleted と added。S-06）、負荷の高いときはそれらが別々の窓に割れるので、
        **一致する間は期待を残す**（BUG-0083）。一致しない内容を見たら期待を消費してそのイベントを渡す（その変更と、
        その後の別の変更を取りこぼさないため）。削除を期待するときは ``sha256_after = ""``（不在の間は捨てる）。
        期待は ``WATCH_EXPECT_TTL_S`` 秒で失効する。``generation`` を 1 つ進める（編集トランザクションが書き込んだ後に呼ぶ）。
        """
        self.generation += 1  # 自分の書き込みは、イベントが届く前に数える（T-00 の Git の状態の覚えが古いまま返らない）
        now = time.monotonic()
        for k in [k for k, (_, dl) in self._expected.items() if dl < now]:
            del self._expected[k]
        self._expected[rel] = (sha256_after, now + WATCH_EXPECT_TTL_S)

    def forget(self, rel: RelPath) -> None:
        """``rel`` の期待を消す（無ければ何もしない）。以降のこのパスのイベントは、内容によらず sink に届く。

        編集トランザクションが、書き込んだファイルを索引に取り込めなかったときに呼ぶ（BUG-0087）。こだまを捨てずに
        ``sink.changed`` で取り込み直させ、前の編集の期待（同じ内容に戻す編集なら ``sha256`` が一致する）が残っていても捨てさせない。
        ``expect`` と同じく ``generation`` を 1 つ進める（書き込んだ後に呼ばれる）。
        """
        self.generation += 1
        self._expected.pop(rel, None)

    # --- 監視ループ ---------------------------------------------------------------------

    @staticmethod
    async def _forward_stop(stop: asyncio.Event, inner: asyncio.Event) -> None:
        await stop.wait()
        inner.set()

    async def _pump(self, q: asyncio.Queue[_Item], stop: asyncio.Event) -> None:
        """``awatch`` のイベントを、種別を捨てたパス集合にしてキューへ流す。終わりは ``None``。"""
        root = str(self._ws.root)
        try:
            async with aclosing(
                awatch(
                    self._ws.root,
                    debounce=self._debounce_ms,
                    step=self._step_ms,
                    watch_filter=self._accept,
                    stop_event=stop,
                )
            ) as events:
                async for changes in events:
                    q.put_nowait({os.path.relpath(p, root).replace("\\", "/") for _, p in changes})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 開始失敗（存在しない・権限・OS の監視上限）も、動作中の失敗も
            self.mode = "polling"
            _log.warning(
                "file watching unavailable, falling back to lazy validation: %s: %s", type(exc).__name__, exc
            )
        finally:
            q.put_nowait(None)

    def _accept(self, _change: object, path: str) -> bool:
        """``watch_filter``。Python 側で全イベントに対して呼ばれるので、字句の判定だけにする（realpath しない）。"""
        self.generation += 1  # 落とすイベントも数える（``.git/`` の変化も Git の状態を変える）
        root = str(self._ws.root)
        if path.startswith(root + os.sep):
            rel = path[len(root) + 1 :]
        else:
            rel = os.path.relpath(path, root)
        rel = rel.replace("\\", "/")
        return rel not in ("", ".", "..") and not rel.startswith("../") and not self._skip(rel)

    def _skip(self, rel: str) -> bool:
        """監視の対象外か（``.git/``・一時名・``exclude_globs``）。``rel`` は ``/`` 区切りの相対パス。"""
        parts = rel.split("/")
        git = any((p.casefold() if self._fold_names else p) == ".git" for p in parts)
        if git or parts[-1].startswith(TMP_PREFIX):
            return True
        return is_excluded(self._ws, cast(RelPath, rel))  # 字句だけの判定なので封じ込め前の文字列でよい

    async def _consume(self, q: asyncio.Queue[_Item], stop: asyncio.Event) -> None:
        """窓の畳み込みとバースト検知（S-06 の型）。処理はこの 1 つのタスクで直列に行う。"""
        while True:
            item = await q.get()
            if item is None:
                return
            if isinstance(item, _Flush):
                await self._flush(item)
                continue
            if stop.is_set():
                return
            acc = set(item)
            flushes: list[_Flush] = []
            ended = False
            while (
                len(acc) >= WATCH_BURST_HINT
            ):  # 大きい窓だけ、静まる（0.3 秒来ない）まで合算。小さい窓は待たず即処理
                try:
                    nxt = await asyncio.wait_for(q.get(), WATCH_BURST_QUIET_S)  # queue.get のキャンセルは安全
                except TimeoutError:
                    break
                if nxt is None:
                    ended = True
                    break
                if isinstance(nxt, _Flush):  # drain は合算の待ちを打ち切る
                    flushes.append(nxt)
                    break
                acc |= nxt
            await self._dispatch(acc)
            for f in flushes:
                await self._flush(f)
            if ended or stop.is_set():
                return

    async def _flush(self, f: _Flush) -> None:
        try:
            if f.paths:
                await self._apply(set(f.paths))
        finally:
            if not f.done.done():
                f.done.set_result(None)

    async def _release(self, q: asyncio.Queue[_Item]) -> None:
        """終了時に、まだキューに残っている drain を済ませて待ちを解く（``paths`` の同期は行う）。"""
        while not q.empty():
            item = q.get_nowait()
            if isinstance(item, _Flush):
                await self._flush(item)

    # --- 処理 ----------------------------------------------------------------------------

    async def _dispatch(self, raw: set[str]) -> None:
        if len(raw) > WATCH_BURST_FILES:
            _log.info("watch burst of %d paths: switching to rescan", len(raw))
            await self._rescan()
            return
        await self._apply(raw)

    async def _apply(self, raw: set[str]) -> None:
        """パスの集合を stat して sink に渡す（種別は使わない）。例外は記録して続ける（監視を止めない）。"""
        t0 = time.perf_counter()
        try:
            files, dirs, gone = await asyncio.to_thread(self._classify, raw)
            parents = _ancestors(files | dirs | gone)
            overflow = False
            for d in sorted(dirs):
                # 子のイベントが同じ窓にあるディレクトリ、索引に配下が載っているディレクトリは展開しない
                # （ファイルの書き換えには親ディレクトリの "modified" が付いてくる）。新しく現れたものだけ展開する。
                if d in parents or self._known(f"{d}/"):
                    continue
                files.update(await asyncio.to_thread(self._walk, d, WATCH_BURST_FILES))
                if len(files) > WATCH_BURST_FILES:
                    overflow = True
                    break
            removed: set[RelPath] = set()
            if not overflow:
                for g in sorted(gone):
                    known = self._known(f"{g}/")  # 消えたのがディレクトリなら配下も消す
                    if known:
                        removed.update(known)
                    else:
                        removed.add(g)
                overflow = len(files) + len(removed) > WATCH_BURST_FILES
            if overflow:
                _log.info("watch batch expanded past %d files: switching to rescan", WATCH_BURST_FILES)
                await self._rescan()
                return
            files, removed = await self._without_expected(files, removed)
            for r in sorted(removed):
                await self._call("removed", self._sink.removed, r)
            for f in sorted(files):
                await self._call("changed", self._sink.changed, f)
            _log.debug(
                "watch batch: %d event paths -> %d changed, %d removed in %.1f ms",
                len(raw),
                len(files),
                len(removed),
                (time.perf_counter() - t0) * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("failed to apply %d watched paths", len(raw))

    async def _rescan(self) -> None:
        self._expected.clear()
        await self._call("rescan", self._sink.rescan)

    async def _call(self, what: str, fn: Callable[..., Awaitable[None]], *args: object) -> None:
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("sink.%s%s failed", what, args)

    def _known(self, prefix: str) -> list[RelPath]:
        try:
            return list(self._sink.known_paths_under(prefix))
        except Exception:
            _log.exception("sink.known_paths_under(%r) failed", prefix)
            return []

    async def _without_expected(
        self, files: set[RelPath], removed: set[RelPath]
    ) -> tuple[set[RelPath], set[RelPath]]:
        """``expect`` 済みのパスのうち、ディスクの今の状態が期待どおりのものを落とす。

        期待は一致する間は残し（失効まで）、食い違ったら消費する。1 回の書き込みのイベントが複数の窓に割れて届いても、
        こだまは全部捨てる（BUG-0083。1 回で消費すると 2 つ目の窓が二重取り込みになっていた）。
        """
        if not self._expected:
            return files, removed
        now = time.monotonic()
        keep_files: set[RelPath] = set()
        for rel in files:
            want = self._live_expectation(rel, now)
            if want and await asyncio.to_thread(_sha256_file, to_abs(self._ws, rel)) == want:
                continue
            self._expected.pop(rel, None)
            keep_files.add(rel)
        keep_removed: set[RelPath] = set()
        for rel in removed:
            if self._live_expectation(rel, now) == "":
                continue
            self._expected.pop(rel, None)
            keep_removed.add(rel)
        return keep_files, keep_removed

    def _live_expectation(self, rel: RelPath, now: float) -> str | None:
        """``rel`` の失効していない期待の ``sha256``（削除の期待は ``""``）。無い・失効していれば ``None``（失効分は消す）。"""
        exp = self._expected.get(rel)
        if exp is None:
            return None
        if exp[1] < now:
            del self._expected[rel]
            return None
        return exp[0]

    # --- ディスクの状態 ---------------------------------------------------------------------

    def _classify(self, raw: Iterable[str]) -> tuple[set[RelPath], set[RelPath], set[RelPath]]:
        """(ファイル, ディレクトリ, 不在) に分ける。ルート外・リンク越え・対象外は捨てる。スレッドで走らせる。"""
        files: set[RelPath] = set()
        dirs: set[RelPath] = set()
        gone: set[RelPath] = set()
        for text in raw:
            if text in ("", "."):
                continue
            try:
                rel = confine(self._ws, text)
            except LuciankeError:
                continue
            if rel == "." or self._skip(rel):
                continue
            path = to_abs(self._ws, rel)
            try:
                if path.is_dir():
                    if not self._dir_skipped(
                        rel
                    ):  # 配下が丸ごと除外のディレクトリ（"build/" など）は展開しない
                        dirs.add(rel)
                elif path.exists():
                    files.add(rel)
                else:
                    gone.add(rel)
            except OSError:
                gone.add(rel)
        return files, dirs, gone

    def _walk(self, d: RelPath, limit: int) -> list[RelPath]:
        """``d`` 配下のファイルを列挙する（ディレクトリの移動・改名は子のイベントが来ない）。``limit`` 超で打ち切る。"""
        root = str(self._ws.root)
        out: list[RelPath] = []
        for dp, dn, fn in os.walk(to_abs(self._ws, d)):
            rel_dp = os.path.relpath(dp, root).replace("\\", "/")
            dn[:] = [n for n in dn if not self._dir_skipped(f"{rel_dp}/{n}")]
            for name in fn:
                text = f"{rel_dp}/{name}"
                if self._skip(text):
                    continue
                try:
                    out.append(confine(self._ws, text))
                except LuciankeError:
                    continue
                if len(out) > limit:
                    return out
        return out

    def _dir_skipped(self, rel: str) -> bool:
        return self._skip(rel) or is_excluded(self._ws, cast(RelPath, f"{rel}/{_DIR_PROBE}"))
