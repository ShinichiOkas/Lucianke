"""F-PY・F-TS の契約テスト（BUG-0036、M1-1）の共通設定。

* ``tests/contract``（``_t11_harness`` の ``snapshot`` / ``sha``、``compact_grammar``）を import できるように、``sys.path`` に足す。
* ``slow`` の印を登録し、このディレクトリの全試験に付ける（LSP と索引を起こす重い試験。``-m "not slow"`` で外せる）。
* フィクスチャ ``rig``: F-PY / F-TS の両方で、複製したワークスペースに結線済みのサーバーを組んで渡す。取得されていない
  フィクスチャは、取得の手順を理由に出して skip する（``bench/exit_criteria.toml`` の ``needs`` と ``skips = "fail"`` が、
  未取得の環境を「未検証」として扱う）。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent / "contract"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _rig import Rig, close_rig, missing_reason, open_rig  # noqa: E402

FIXTURE_KEYS = ["F-PY", "F-TS"]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: 重い試験（LSP・索引を起こす。F-PY / F-TS の契約テスト）")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.slow)


@pytest.fixture(params=FIXTURE_KEYS)
async def rig(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Rig]:
    key: str = request.param
    reason = missing_reason(key)
    if reason is not None:
        pytest.skip(reason)
    r = await open_rig(key, tmp_path)
    try:
        yield r
    finally:
        await close_rig(r)
