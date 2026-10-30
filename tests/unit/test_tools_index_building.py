"""初回の索引の途中のツールの応答（BUG-0070）。結線は本番と同じ（``RuntimeHub`` + ``WiredServices`` + ``Pipeline`` + 全ツール）。

走査は時間ではなく門で止める（決定的）: ``Indexer._walk``（ファイルの列挙。スレッドで走る）を ``threading.Event`` で止め、
``configure_workspace set_roots`` の応答が ``jobs: ... running 0%`` の状態を作る。LSP は使わない（``structural`` の経路）。

* ディスクにある（除外されていない）ファイルの対象は、走査がまだ届いていなくても ``E_TARGET_NOT_FOUND`` にしない（先取りして答える）。
* 索引を読むツールの応答には進みつきの警告（``index building: ...``）が付く。0 件の検索も本当の 0 件と見分けられる。
* 先取りできなければ、再試行を促す ``E_TIMEOUT``（``details.reason: "index_building"``）。
* 走査を終えたら、警告は付かず、答えは従来どおり。
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.engine.indexer import Indexer
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import register_tools
from lucianke.tools._runtime import RuntimeHub, WiredServices

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAYMENT = "src/shop/services/payment.py"
ROUTES = "src/shop/api/routes.py"
TARGET = {"path": PAYMENT, "symbol_name": "PaymentProcessor.process_charge"}
LISTING = "index building: listing files; results may be incomplete - call again when done"
GATE_S = 60.0


class WalkGate:
    """``Indexer._walk`` を止める門（クラスに差す。束は ``sync_roots`` の中で索引器を作るため）。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        real = Indexer._walk
        gate = self

        def walk(ix: Indexer) -> tuple[list[Any], bool]:
            gate.entered.set()
            if not gate.release.wait(GATE_S):
                raise TimeoutError("the walk gate was never opened")
            return real(ix)

        monkeypatch.setattr(Indexer, "_walk", walk)

    async def wait_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, GATE_S), "the scan never reached the walk"


@dataclass
class Server:
    hub: RuntimeHub
    pipeline: Pipeline
    gate: WalkGate

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        rendered = await self.pipeline.call_tool(tool, {"format": "json", **args})
        assert rendered.structured is not None, rendered.text
        return dict(rendered.structured)

    async def finish_scan(self) -> None:
        self.gate.release.set()
        rt = self.hub.runtime()
        assert rt is not None and rt.scan_job_id is not None
        status = await asyncio.wait_for(rt.indexer.wait_job(rt.scan_job_id), GATE_S)
        assert status is not None and status.state == "done", status


@pytest.fixture
async def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Server]:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root)
    gate = WalkGate(monkeypatch)
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    registry = ToolRegistry()
    register_tools(registry)
    services = WiredServices(roots=lambda: [str(root)], trees=hub.trees, lines=compact, hub=hub)
    srv = Server(hub, Pipeline(registry, services=services, data_version_provider=hub.data_version), gate)
    status = await srv.call("configure_workspace", action="set_roots", root_paths=[str(root)])
    assert status["ok"], status
    await gate.wait_entered()
    try:
        yield srv
    finally:
        gate.release.set()
        await hub.stop_watchers()
        await hub.stop_indexers()
        await hub.close_storage()


def building_warnings(out: dict[str, Any]) -> list[str]:
    return [w for w in out.get("warnings") or [] if w.startswith("index building:")]


async def test_targets_the_scan_has_not_reached_are_answered_not_reported_missing(server: Server) -> None:
    # 検索の 0 件は「まだ索引していない」と見分けられる
    search = await server.call("search_workspace_symbols", query="process_charge")
    assert search["ok"] and search["data"]["symbols"] == []
    assert building_warnings(search) == [LISTING]

    calls = await server.call("query_call_hierarchy", target=TARGET, direction="incoming", depth=1)
    assert calls["ok"], calls["error"]
    assert calls["data"]["root"]["urn"].endswith(f"{PAYMENT}#PaymentProcessor.process_charge")
    assert building_warnings(calls) == [LISTING]

    refs = await server.call("find_references_and_implementations", target=TARGET)
    assert refs["ok"], refs["error"]
    assert building_warnings(refs) == [LISTING]

    resolved = await server.call(
        "resolve_symbols", targets=[TARGET, {"path": ROUTES, "symbol_name": "PaymentProcessor"}]
    )
    assert resolved["ok"], resolved["error"]
    items = resolved["data"]["items"]
    assert [item["ok"] for item in items] == [True, True], items
    assert (
        items[1]["definition"]["path"] == PAYMENT
    )  # import をたどった先（まだ索引に無かった）も先取りして答える
    assert building_warnings(resolved) == [LISTING]

    repo_map = await server.call("get_repo_map")
    assert repo_map["ok"] and building_warnings(repo_map) == [LISTING]

    # ディスクに無いファイルは、索引の途中でも従来どおり E_TARGET_NOT_FOUND（それは正しい答え）
    missing = await server.call(
        "query_call_hierarchy", target={"path": "src/shop/nope.py", "symbol_name": "x"}, direction="incoming"
    )
    assert not missing["ok"] and missing["error"]["code"] == "E_TARGET_NOT_FOUND"

    # 索引を読まないツール（構文木から答える）には警告を付けない
    skeleton = await server.call("get_file_skeleton", paths=[PAYMENT])
    assert skeleton["ok"] and building_warnings(skeleton) == []

    # 走査を終えたら警告は付かず、呼び出し元（まだ索引に無かった routes.py）も揃う
    await server.finish_scan()
    calls = await server.call("query_call_hierarchy", target=TARGET, direction="incoming", depth=1)
    assert calls["ok"] and building_warnings(calls) == []
    callers = {node["name"] for node in calls["data"]["nodes"]}
    assert {"create_order", "retry_order"} <= callers, calls["data"]
    search = await server.call("search_workspace_symbols", query="process_charge")
    assert len(search["data"]["symbols"]) == 1 and building_warnings(search) == []


async def test_a_target_that_cannot_be_indexed_ahead_is_a_retryable_timeout(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Indexer, rel: object) -> str:
        raise OSError("disk on fire")

    monkeypatch.setattr(Indexer, "ingest_ahead", broken)
    out = await server.call("query_call_hierarchy", target=TARGET, direction="incoming")
    assert not out["ok"]
    error = out["error"]
    assert error["code"] == "E_TIMEOUT", error
    assert error["details"]["reason"] == "index_building" and error["details"]["retry_after_ms"] == 1000
    assert error["details"]["path"] == PAYMENT and error["details"]["files_total"] is None
    assert "retry" in error["message"] and "index building: listing files" in error["message"]

    resolved = await server.call(
        "resolve_symbols", targets=[TARGET]
    )  # バッチは項目単位の失敗（全件なら全体のエラー）
    assert not resolved["ok"] and resolved["error"]["code"] == "E_TIMEOUT"
