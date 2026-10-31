"""監視が使えない環境（``polling``）の遅延検証と T-00 の ``watcher`` の表示（BUG-0096。仕様書 §12.4 の 6、設計書 §7.3.6 の 6）。

結線は本番と同じ（``RuntimeHub`` + ``WiredServices`` + ``Pipeline`` + 全ツール）。``watchfiles.awatch`` が開始に失敗する状況を作り
（``Watcher.mode = "polling"``）、監視のイベントが一切来ない中でファイルをディスク上で書き換える。

* 書き換えた後の要求の対象（T-04・T-06・T-07）に、新しい中身が見える（要求時に ``sha256`` を照合して取り込み直す）。
* 消されたファイルは、対象として解決できない（索引から消える）。
* ``polling`` のときだけ、索引を読むツールの応答に、対象以外は古いことがある旨の警告が付く。
* T-00 ``status`` が ``watcher``（compact の ``- watcher:`` 行と ``data.workspaces[].watcher``）を出す。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import lucianke.workspace.watcher as watcher_mod
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import register_tools
from lucianke.tools._building import POLLING_WARNING
from lucianke.tools._runtime import RuntimeHub, WiredServices

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAYMENT = "src/shop/services/payment.py"
NEW_FN = "\n\ndef brand_new_refund_helper(amount):\n    return PaymentProcessor().process_charge(amount)\n"
TARGET = {"path": PAYMENT, "symbol_name": "brand_new_refund_helper"}
CHARGE = {"path": PAYMENT, "symbol_name": "PaymentProcessor.process_charge"}
SCAN_S = 60.0


@dataclass
class Server:
    hub: RuntimeHub
    pipeline: Pipeline
    root: Path

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        rendered = await self.pipeline.call_tool(tool, {"format": "json", **args})
        assert rendered.structured is not None, rendered.text
        return dict(rendered.structured)

    def append(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.write_bytes(path.read_bytes() + text.encode())


async def _start(tmp_path: Path) -> AsyncIterator[Server]:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root)
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    registry = ToolRegistry()
    register_tools(registry)
    services = WiredServices(roots=lambda: [str(root)], trees=hub.trees, lines=compact, hub=hub)
    srv = Server(hub, Pipeline(registry, services=services, data_version_provider=hub.data_version), root)
    status = await srv.call("configure_workspace", action="set_roots", root_paths=[str(root)])
    assert status["ok"], status
    rt = hub.runtime()
    assert rt is not None and rt.scan_job_id is not None
    done = await asyncio.wait_for(rt.indexer.wait_job(rt.scan_job_id), SCAN_S)
    assert done is not None and done.state == "done", done
    try:
        yield srv
    finally:
        await hub.stop_watchers()
        await hub.stop_indexers()
        await hub.close_storage()


@pytest.fixture
async def polling_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Server]:
    def broken(*_a: object, **_k: object) -> object:
        raise OSError("watch limit reached")

    monkeypatch.setattr(watcher_mod, "awatch", broken)
    async for srv in _start(tmp_path):
        rt = srv.hub.runtime()
        assert rt is not None and rt.watcher.mode == "polling"
        yield srv


@pytest.fixture
async def native_server(tmp_path: Path) -> AsyncIterator[Server]:
    async for srv in _start(tmp_path):
        yield srv


def polling_warnings(out: dict[str, Any]) -> list[str]:
    return [w for w in out.get("warnings") or [] if w == POLLING_WARNING]


async def test_a_file_rewritten_while_polling_is_seen_by_the_next_request(polling_server: Server) -> None:
    srv = polling_server
    before = await srv.call("resolve_symbols", targets=[TARGET])
    assert not before["ok"] or not before["data"]["items"][0]["ok"], before  # 書き換える前は無い

    srv.append(PAYMENT, NEW_FN)  # 監視のイベントは来ない。索引は古いまま

    resolved = await srv.call("resolve_symbols", targets=[TARGET])
    assert resolved["ok"] and resolved["data"]["items"][0]["ok"], resolved
    calls = await srv.call("query_call_hierarchy", target=TARGET, direction="outgoing", depth=1)
    assert calls["ok"], calls.get("error")
    assert calls["data"]["root"]["urn"].endswith(f"{PAYMENT}#brand_new_refund_helper")
    refs = await srv.call("find_references_and_implementations", target=CHARGE)
    assert refs["ok"], refs.get("error")
    assert "brand_new_refund_helper" in str(refs["data"]), refs["data"]
    assert polling_warnings(refs) == [POLLING_WARNING]  # 対象以外が古いことがある旨


async def test_a_file_deleted_while_polling_is_gone_from_the_next_request(polling_server: Server) -> None:
    srv = polling_server
    assert (await srv.call("query_call_hierarchy", target=CHARGE, direction="incoming"))["ok"]
    (srv.root / PAYMENT).unlink()
    gone = await srv.call("query_call_hierarchy", target=CHARGE, direction="incoming")
    assert not gone["ok"] and gone["error"]["code"] == "E_TARGET_NOT_FOUND", gone


async def test_status_shows_the_watcher_polling(polling_server: Server) -> None:
    rendered = await polling_server.pipeline.call_tool("configure_workspace", {"action": "status"})
    assert "watcher: polling" in rendered.text
    out = await polling_server.call("configure_workspace", action="status")
    assert [w["watcher"] for w in out["data"]["workspaces"]] == ["polling"], out["data"]


async def test_status_shows_the_watcher_native_and_no_polling_warning(native_server: Server) -> None:
    rendered = await native_server.pipeline.call_tool("configure_workspace", {"action": "status"})
    assert "watcher: native" in rendered.text
    out = await native_server.call("configure_workspace", action="status")
    assert [w["watcher"] for w in out["data"]["workspaces"]] == ["native"]
    refs = await native_server.call("find_references_and_implementations", target=CHARGE)
    assert refs["ok"] and polling_warnings(refs) == []
