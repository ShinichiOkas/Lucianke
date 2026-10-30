"""テスト全体の見張り（BUG-0076）: 索引の書き手スレッドを残したテストを、そのテスト自身のエラーにする。

負荷の高いときに実 LSP の試験のセットアップが時間切れで落ちると、フィクスチャの後始末が走らず ``IndexWriter`` の書き手スレッド
（``lucianke-index-writer``）が生き残り、後の無関係な試験（``test_index_core`` のスレッドの検査）を巻き添えで落としていた。
ここでは、どの試験・どのフィクスチャがスレッドを作ったかを数えて、**作った側**の teardown をエラーにし、残ったスレッドは止める
（後の試験に持ち越さない）。

* 試験（関数スコープのフィクスチャ・本体・後始末を含む）が作ったスレッドは、その試験の teardown の後に見る。
* 関数より広いスコープ（module / class / package / session）のフィクスチャが作ったスレッドは、そのフィクスチャが片づいた時点
  （``pytest_fixture_post_finalizer``）で見て、そのとき teardown 中の試験のエラーにする（フィクスチャの名前を書く）。
* 止まりかけのスレッドは ``EXIT_GRACE_S`` まで終わるのを待つ（条件待ち。``join``）。それでも生きていれば「残した」とみなす。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator, Iterable
from typing import Any

import pytest

WATCHED_THREADS = frozenset({"lucianke-index-writer"})
EXIT_GRACE_S = 3.0  # 後始末を終えたスレッドが実際に終わるまでの待ちの上限（終われば即戻る）
STOP_WAIT_S = 30.0  # 残ったスレッドを止めるときの待ちの上限
LEAK_HINT = (
    "leaked threads (BUG-0076: close the IndexWriter in a finally / fixture finalizer, also when the setup "
    "fails or times out): "
)


def _alive() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name in WATCHED_THREADS and t.is_alive()}


def _still_alive(
    threads: Iterable[threading.Thread], grace_s: float = EXIT_GRACE_S
) -> list[threading.Thread]:
    deadline = time.monotonic() + grace_s
    out: list[threading.Thread] = []
    for t in threads:
        t.join(max(deadline - time.monotonic(), 0.0))
        if t.is_alive():
            out.append(t)
    return out


def _stop(t: threading.Thread) -> bool:
    """残った書き手スレッドを止める（持ち主の ``IndexWriter.close``）。止まれば ``True``。"""
    owner = getattr(getattr(t, "_target", None), "__self__", None)
    close = getattr(owner, "close", None)
    if callable(close):
        closer = threading.Thread(target=close, name="leak-guard-close", daemon=True)
        closer.start()
        closer.join(STOP_WAIT_S)
    t.join(STOP_WAIT_S if callable(close) else 0.0)
    return not t.is_alive()


def _describe(threads: list[threading.Thread]) -> str:
    _guard.reported.update(threads)
    stopped = [_stop(t) for t in threads]
    return ", ".join(
        f"{t.name}#{t.ident} ({'stopped by the guard' if ok else 'COULD NOT BE STOPPED'})"
        for t, ok in zip(threads, stopped, strict=True)
    )


class _LeakGuard:
    def __init__(self) -> None:
        self.before_item: set[threading.Thread] = set()
        # 広いスコープのフィクスチャ → それが作ったスレッド
        self.scoped: dict[Any, set[threading.Thread]] = {}
        # 片づいたのにスレッドを残したフィクスチャ（teardown 中の試験で報告する）
        self.scoped_leaks: list[str] = []
        # 報告済み（止められなかったものを 2 度数えない）
        self.reported: set[threading.Thread] = set()

    def owned_by_scoped(self) -> set[threading.Thread]:
        return set().union(*self.scoped.values()) if self.scoped else set()

    def item_leaks(self) -> list[str]:
        new = _alive() - self.before_item - self.owned_by_scoped() - self.reported
        msgs = list(self.scoped_leaks)
        self.scoped_leaks.clear()
        left = _still_alive(sorted(new, key=lambda t: t.ident or 0))
        if left:
            msgs.append(f"this test left {len(left)} index writer thread(s) running: {_describe(left)}")
        return msgs


_guard = _LeakGuard()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, object, object]:
    _guard.before_item = _alive()
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: pytest.FixtureRequest) -> Generator[None, object, object]:
    before = _alive()
    try:
        return (yield)
    finally:
        if fixturedef.scope != "function":
            new = _alive() - before
            if new:
                _guard.scoped.setdefault(fixturedef, set()).update(new)


def pytest_fixture_post_finalizer(fixturedef: Any, request: pytest.FixtureRequest) -> None:
    owned = _guard.scoped.pop(fixturedef, None)
    if not owned:
        return
    left = _still_alive(owned - _guard.reported)
    if left:
        _guard.scoped_leaks.append(
            f"fixture {fixturedef.argname!r} (scope={fixturedef.scope}, {fixturedef.baseid}) left "
            f"{len(left)} index writer thread(s) running after its teardown: {_describe(left)}"
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, object, object]:
    try:
        result = yield
    except BaseException as exc:
        msgs = _guard.item_leaks()  # teardown 自体の失敗は隠さず、残したスレッドは注記にする（止めてはおく）
        if msgs:
            exc.add_note(LEAK_HINT + "; ".join(msgs))
        raise
    msgs = _guard.item_leaks()
    if msgs:
        pytest.fail(LEAK_HINT + "; ".join(msgs), pytrace=False)
    return result
