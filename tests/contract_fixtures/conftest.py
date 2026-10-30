"""F-PY・F-TS の契約テスト（BUG-0036、M1-1）の共通設定。

* ``tests/contract``（``_t11_harness`` の ``snapshot`` / ``sha``、``compact_grammar``）を import できるように、``sys.path`` に足す。
* ``slow`` の印を登録し、このディレクトリの全試験に付ける（LSP と索引を起こす重い試験。``-m "not slow"`` で外せる）。
* フィクスチャ ``rig``: F-PY / F-TS の両方で、複製したワークスペースに結線済みのサーバーを組んで渡す。
* フィクスチャ ``fetched_fixtures``（セッションに 1 回、BUG-0062）: 取得されていない（または SHA が違う）F-PY / F-TS を
  ``bench/fetch_fixtures.py`` で取りにいく。取得に失敗したら **赤（エラー）** にする。黙って skip すると「0 failed」に見えるのに
  契約テストを検査していない状態になる（skip は pass ではない）。
  オフラインで走らせるときは ``LUCIANKE_NO_FIXTURE_FETCH=1`` で取得を止められる。そのときは未取得のフィクスチャの試験だけを
  「NOT CHECKED」と明記した理由で skip する（``bench/run_exit_criteria.py`` は ``network = "none"`` を守るためこれを立てる）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent / "contract"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _rig import BENCH, CACHE, Rig, close_rig, missing_reason, open_rig  # noqa: E402

FIXTURE_KEYS = ["F-PY", "F-TS"]

NO_FETCH_ENV = "LUCIANKE_NO_FIXTURE_FETCH"
FETCH_SCRIPT = BENCH / "fetch_fixtures.py"
FETCH_TIMEOUT_S = 900.0
# 同じ作業ツリーで pytest を同時に走らせたときに、2 つが同じ bench/.cache/<name>/ へ git init / fetch しないための鍵。
LOCK_PATH = CACHE / ".fetch.lock"
LOCK_WAIT_S = FETCH_TIMEOUT_S + 60.0
LOCK_STALE_S = FETCH_TIMEOUT_S + 120.0  # これより古い鍵は落ちたプロセスの残骸とみなして消す


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: 重い試験（LSP・索引を起こす。F-PY / F-TS の契約テスト）")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.slow)


def _fetch_disabled() -> bool:
    return os.environ.get(NO_FETCH_ENV, "").strip().lower() not in {"", "0", "false", "no"}


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """作業ツリー内のプロセス間の排他（``O_EXCL`` の鍵ファイル。OS を問わず動く）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_S:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                pytest.fail(
                    f"timed out waiting for {path} (another pytest is fetching the fixtures?). "
                    "Delete the file if no other run is active.",
                    pytrace=False,
                )
            time.sleep(0.5)
            continue
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _missing() -> dict[str, str]:
    return {k: r for k in FIXTURE_KEYS if (r := missing_reason(k)) is not None}


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


@pytest.fixture(scope="session")
def fetched_fixtures() -> dict[str, str]:
    """F-PY / F-TS を取得済みにする。戻り値は ``{key: skip の理由}``（取得を止めたときの未取得分だけ。通常は空）。

    取得の失敗は ``pytest.fail`` で赤にする（セッションに 1 回だけ試み、以降の試験には同じ失敗が出る）。
    """
    missing = _missing()
    if not missing:
        return {}
    if _fetch_disabled():
        return {
            k: f"NOT CHECKED: {r} ({NO_FETCH_ENV} is set, so the fixture was not fetched; this skip is not a pass)"
            for k, r in missing.items()
        }
    with _exclusive(LOCK_PATH):
        missing = _missing()  # 鍵を待つ間に別のプロセスが取り終えていれば何もしない
        if not missing:
            return {}
        argv = [sys.executable, str(FETCH_SCRIPT), *missing]
        try:
            proc = subprocess.run(  # noqa: S603  固定の引数列（キーは FIXTURE_KEYS から）
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"), check=False, timeout=FETCH_TIMEOUT_S,
            )  # fmt: skip
            detail = f"exit code {proc.returncode}\n{_tail(proc.stdout)}\n{_tail(proc.stderr)}".rstrip()
        except (OSError, subprocess.SubprocessError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
    still = _missing()
    if still:
        pytest.fail(
            "the contract-fixture workspaces could not be fetched, so these tests cannot be checked "
            f"(BUG-0062). Missing: {', '.join(f'{k}: {r}' for k, r in still.items())}\n"
            f"fetch command: {' '.join(argv)}\n{detail}\n"
            "Fix git/network access, or run `uv run --no-project python bench/fetch_fixtures.py` by hand. "
            f"To run offline without them, set {NO_FETCH_ENV}=1 (the tests are then skipped as NOT CHECKED).",
            pytrace=False,
        )
    return {}


@pytest.fixture(params=FIXTURE_KEYS)
async def rig(
    request: pytest.FixtureRequest, tmp_path: Path, fetched_fixtures: dict[str, str]
) -> AsyncIterator[Rig]:
    key: str = request.param
    reason = fetched_fixtures.get(key) or missing_reason(key)
    if reason is not None:
        pytest.skip(reason)
    r = await open_rig(key, tmp_path)
    try:
        yield r
    finally:
        await close_rig(r)
