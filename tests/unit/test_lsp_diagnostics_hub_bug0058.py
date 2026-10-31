"""lsp/diagnostics.py の ``DiagnosticsHub``: 解析の合図（``analysed``）と、同じ内容の待ちを繰り返さない（``gave_up``）。BUG-0058。

プロセスは立てず、``on_publish`` を手で呼ぶ。「届かない」ことの確認だけ短いタイムアウトを使う。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lucianke.core.model import RelPath
from lucianke.lsp.diagnostics import DiagnosticsHub
from lucianke.workspace.paths import to_file_uri
from lucianke.workspace.types import Workspace

A = RelPath("a.py")
B = RelPath("b.py")


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = Path(os.path.realpath(tmp_path / "proj"))
    root.mkdir()
    return Workspace(workspace_id="w", root=root, case_insensitive=os.name == "nt")


def publish(hub: DiagnosticsHub, ws: Workspace, path: RelPath, version: int | None, n: int = 1) -> None:
    diag = {
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
        "message": "m",
    }
    hub.on_publish({"uri": to_file_uri(ws, path), "version": version, "diagnostics": [diag] * n})


def test_analysed_is_set_by_any_publish_and_cleared_by_reset(ws: Workspace) -> None:
    hub = DiagnosticsHub(ws)
    assert hub.analysed is False
    publish(hub, ws, B, 1)
    assert hub.analysed is True  # どの文書の診断でも、LSP が解析を 1 周した合図
    hub.reset()  # 起動し直し: 解析の前に戻る
    assert hub.analysed is False


async def test_a_wait_that_timed_out_is_not_repeated_for_the_same_content(ws: Workspace) -> None:
    hub = DiagnosticsHub(ws)
    assert hub.gave_up(A, min_version=1, sent_at=10.0) is False
    assert await hub.wait_for(A, min_version=1, sent_at=10.0, timeout_ms=50) is False
    assert hub.gave_up(A, min_version=1, sent_at=10.0) is True
    # 内容が変わった（版・送信時刻が違う）待ちは別
    assert hub.gave_up(A, min_version=2, sent_at=11.0) is False
    assert hub.gave_up(A, min_version=1, sent_at=12.0) is False
    # 0 ms の待ち（待たずに確かめるだけ）は時間切れとして覚え直さない・消さない
    assert await hub.wait_for(A, min_version=1, sent_at=10.0, timeout_ms=0) is False
    assert hub.gave_up(A, min_version=1, sent_at=10.0) is True


async def test_gave_up_is_false_once_the_diagnostics_arrive(ws: Workspace) -> None:
    hub = DiagnosticsHub(ws)
    assert await hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=50) is False
    assert hub.gave_up(A, min_version=1, sent_at=0.0) is True
    publish(hub, ws, A, 1)  # 遅れて届いた
    assert hub.gave_up(A, min_version=1, sent_at=0.0) is False
    assert await hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=0) is True


async def test_gave_up_is_forgotten_on_close_and_reset(ws: Workspace) -> None:
    hub = DiagnosticsHub(ws)
    assert await hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=50) is False
    hub.forget(A)
    assert hub.gave_up(A, min_version=1, sent_at=0.0) is False
    assert await hub.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=50) is False
    hub.reset()
    assert hub.gave_up(A, min_version=1, sent_at=0.0) is False


async def test_t06_t07_do_not_open_and_wait_for_the_target_once_the_lsp_has_analysed(ws: Workspace) -> None:
    """``await_first_diagnostic``（T-06 / T-07 の最初の診断の待ち）は、LSP が起動後にどれかの文書の診断を出していれば、
    対象の文書を開かず待たずに ``True``（このハンドルは文書を開けない。開こうとすれば落ちる）。"""
    from types import SimpleNamespace

    from lucianke.tools import _lsp_exact

    hub = DiagnosticsHub(ws)
    publish(hub, ws, B, 1)
    handle = SimpleNamespace(diagnostics=hub)
    assert await _lsp_exact.await_first_diagnostic(handle, A, timeout_s=5.0) is True  # type: ignore[arg-type]


async def test_a_first_empty_publish_is_final_for_a_server_without_provisional_empties(ws: Workspace) -> None:
    """ruff（``provisional_empty=False``）の最初の空の診断は本物: 静止時間（300 ms）を待たずに揃う。既定（basedpyright など）は待つ。"""
    import time

    from lucianke.lsp.diagnostics import PROVISIONAL_QUIET_MS
    from lucianke.lsp.servers import default_server

    lint = default_server("python", "lint")
    assert lint is not None and lint.name == "ruff" and lint.provisional_empty is False
    types = default_server("python", "types")
    assert types is not None and types.provisional_empty is True

    final = DiagnosticsHub(ws)
    final.provisional_empty = False
    publish(final, ws, A, 1, n=0)
    start = time.perf_counter()
    assert await final.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=5000) is True
    assert time.perf_counter() - start < PROVISIONAL_QUIET_MS / 1000

    maybe = DiagnosticsHub(ws)
    publish(maybe, ws, A, 1, n=0)
    assert maybe.is_provisional(A) is True
    assert (
        await maybe.wait_for(A, min_version=1, sent_at=0.0, timeout_ms=50) is False
    )  # 静止時間の前は揃わない
