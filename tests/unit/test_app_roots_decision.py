"""根をいつ・何に決めるか（``app.RootsDecision``。BUG-0042）の単体テスト。

* roots を渡せない相手（roots 能力なし・2026-07-28 版）の最初の呼び出しは、cwd を待たずに既定にする。
  T-00 ``set_roots`` だけは決定そのもの: 待たず、cwd の索引も始めない。
* 一度根が決まったら、cwd（既定）はそれを上書きしない（退避のタイマーも ``initialized`` の遅れも）。
* roots を渡せる相手（宣言あり・``None``=不明）は従来どおり、決まるのを待つ。
* ``Pipeline`` の ``ready_gate`` はツール名と生の引数を受ける。
* ``WiredServices.resolve_workspace_id`` は、束があって登録が 1 つも無い間は ``None`` を素通しする。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from lucianke.app import ROOTS_FALLBACK_ENV, ROOTS_FALLBACK_S, RootsDecision, roots_fallback_seconds
from lucianke.mcp.server import RootsState
from lucianke.syntax.parsers import TreeCache
from lucianke.tools._runtime import RuntimeHub, WiredServices


class FakeHub:
    """``RuntimeHub`` の代役（``started`` / ``sync_roots`` / ``wait_started`` だけ）。"""

    def __init__(self) -> None:
        self.started = False
        self.synced: list[list[str]] = []
        self.waited = 0

    async def sync_roots(self, paths: list[str]) -> list[Any]:
        await asyncio.sleep(0)  # 決定の途中に別の呼び出しが割り込める
        self.synced.append(list(paths))
        self.started = True
        return []

    async def wait_started(self) -> bool:
        self.waited += 1
        return self.started


def _decision(*, can_list: bool | None) -> tuple[RootsDecision, FakeHub, RootsState]:
    hub = FakeHub()
    roots = RootsState(paths=["/cwd"], source="cwd", client_can_list=can_list)
    return RootsDecision(hub, roots, logging.getLogger("test.roots")), hub, roots  # type: ignore[arg-type]


async def test_a_client_that_cannot_list_roots_gets_the_cwd_without_waiting() -> None:
    d, hub, _ = _decision(can_list=False)
    await d.gate("configure_workspace", {"action": "status"})
    assert hub.synced == [["/cwd"]] and hub.waited == 1


async def test_concurrent_first_calls_decide_the_cwd_once() -> None:
    d, hub, _ = _decision(can_list=False)
    await asyncio.gather(*(d.gate("get_file_skeleton", {}) for _ in range(5)))
    assert hub.synced == [["/cwd"]] and hub.started


@pytest.mark.parametrize("can_list", [None, True])
async def test_a_client_that_may_list_roots_is_waited_for_not_decided_for(can_list: bool | None) -> None:
    d, hub, _ = _decision(can_list=can_list)
    await d.gate("configure_workspace", {"action": "status"})
    assert hub.synced == [] and hub.waited == 1  # 決めるのは roots/list の結果（か退避）。ここでは待つだけ


async def test_set_roots_is_the_decision_not_something_to_wait_for() -> None:
    d, hub, _ = _decision(can_list=False)
    await d.gate("configure_workspace", {"action": "set_roots", "root_paths": ["/other"]})
    assert hub.synced == [] and hub.waited == 0  # cwd の索引も始めず、待ちもしない


async def test_set_roots_of_a_client_that_may_list_roots_still_waits() -> None:
    """roots/list の結果が後から set_roots の根を上書きしないよう、宣言のある相手では従来どおり待つ。"""
    d, hub, _ = _decision(can_list=True)
    await d.gate("configure_workspace", {"action": "set_roots", "root_paths": ["/other"]})
    assert hub.waited == 1


async def test_other_t00_actions_still_decide_the_cwd_first() -> None:
    d, hub, _ = _decision(can_list=False)
    await d.gate("configure_workspace", {"action": "reindex"})
    assert hub.synced == [["/cwd"]]


async def test_the_cwd_never_overwrites_a_root_that_was_already_decided() -> None:
    d, hub, roots = _decision(can_list=False)
    await hub.sync_roots(["/other"])  # T-00 set_roots が先に決めた
    await d.on_roots(roots)  # 遅れて届いた initialized の cwd
    await d.gate("get_file_skeleton", {})  # 次の呼び出し
    assert hub.synced == [["/other"]]


async def test_roots_from_the_client_always_apply_even_after_a_decision() -> None:
    d, hub, roots = _decision(can_list=True)
    await hub.sync_roots(["/first"])
    roots.set_from_client(["file:///second"])
    await d.on_roots(roots)  # roots/list_changed
    assert len(hub.synced) == 2 and hub.synced[1] != ["/first"] and roots.source == "client"


async def test_the_fallback_does_not_overwrite_a_decision_made_by_set_roots() -> None:
    d, hub, _ = _decision(can_list=False)
    await d.gate("configure_workspace", {"action": "set_roots"})
    await hub.sync_roots(["/other"])  # ハンドラの結果
    await d.fallback(0.01)
    assert hub.synced == [["/other"]]


async def test_the_fallback_decides_the_cwd_when_nobody_did() -> None:
    d, hub, _ = _decision(can_list=None)
    await d.fallback(0.01)
    assert hub.synced == [["/cwd"]]


def test_the_fallback_seconds_can_be_shortened_for_tests_only_by_a_valid_number() -> None:
    assert roots_fallback_seconds({}) == ROOTS_FALLBACK_S
    assert roots_fallback_seconds({ROOTS_FALLBACK_ENV: "1.5"}) == 1.5
    for bad in ("", "abc", "0", "-3"):
        assert roots_fallback_seconds({ROOTS_FALLBACK_ENV: bad}) == ROOTS_FALLBACK_S


async def test_the_pipeline_hands_the_tool_name_and_raw_arguments_to_the_ready_gate() -> None:
    from lucianke.mcp.pipeline import Pipeline
    from lucianke.mcp.registry import ToolRegistry
    from lucianke.tools.t00_configure_workspace import SPEC

    seen: list[tuple[str, dict[str, Any]]] = []

    async def gate(tool: str, arguments: Any) -> None:
        seen.append((tool, dict(arguments)))

    pipeline = Pipeline(ToolRegistry([SPEC]), ready_gate=gate)
    await pipeline.call_tool("configure_workspace", {"action": "set_roots", "root_paths": ["/nope"]})
    assert seen == [("configure_workspace", {"action": "set_roots", "root_paths": ["/nope"]})]


async def test_resolve_workspace_id_passes_none_through_until_a_root_is_registered(tmp_path: Path) -> None:
    import lucianke.mcp.render.compact as compact

    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    svc = WiredServices(roots=lambda: [str(project)], trees=TreeCache(), lines=compact, hub=hub)
    assert svc.resolve_workspace_id(None) is None  # roots() には cwd 相当があっても、登録が無ければ cwd 由来の ID を返さない
    registered = await hub.sync_roots([str(project)])
    try:
        assert svc.resolve_workspace_id(None) == registered[0].workspace_id
    finally:
        for rt in hub.runtimes.values():
            await rt.aclose()
