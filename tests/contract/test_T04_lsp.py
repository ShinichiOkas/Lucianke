"""T-04 ``resolve_symbols`` の LSP 経路の契約テスト（仕様書 §11 T-04 処理規約 2・精度と降格、設計書 §8.3・R-11・ADR-6）。

F-POLY（``tests/fixtures/poly``）を ``tmp_path`` の ``poly/`` にコピーし、実の ``Indexer`` で PaaD に取り込んで、
パイプライン経由で ``resolve_symbols`` を呼ぶ。

* 実機: ``.venv`` の basedpyright。冷（LSP に聞く）→ 温（``_hover_cache`` から。LSP の呼び出しが増えない）→ LSP を止めた後（キャッシュで
  ``exact`` のまま。プロセスを起こさない）。``signature_expanded`` は同じ位置の hover と一致する（事後条件 4）。見つからなければ skip。
* 偽サーバー（テスト内の Python スクリプト。応答は JSON の台本）: タイムアウト・``position_clamped``・hover が空・ファイルの変更による
  キャッシュの失効が ``structural`` に降格する／再び聞くこと。``ready`` でない LSP を待たないこと。
* 条件待ちだけで固定の ``sleep`` は使わない（``ready`` は ``wait_ready``）。LSP の子プロセスはテストの後に残らない。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.core.model import Position, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.engine import selector
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.lsp import features
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import LspServerDef, default_server, find_command
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t04_resolve_symbols as t04
from lucianke.tools._services import ToolServices
from lucianke.tools._t04_exact import NO_HOVER_WARNING
from lucianke.workspace.types import PythonEnv, Workspace

REPO = Path(__file__).resolve().parents[2]
POLY = REPO / "tests" / "fixtures" / "poly"
WS = "poly"
PAY_PY = "src/shop/services/payment.py"
PAY_TS = "web/src/payment.ts"
METHOD = {"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"}
CLASS = {"path": PAY_PY, "symbol_name": "PaymentProcessor"}
TS_CLASS = {"path": PAY_TS, "symbol_name": "PaymentClient"}
METHOD_URN = f"lk://{WS}/{PAY_PY}#PaymentProcessor.process_charge"
METHOD_LINE0 = 38  # `async def process_charge` の行（0 始まり）

_BASEDPYRIGHT = default_server("python", "types")
_BASEDPYRIGHT_CMD = (
    find_command(_BASEDPYRIGHT, workspace_root=REPO, venv=REPO / ".venv") if _BASEDPYRIGHT else None
)
needs_basedpyright = pytest.mark.skipif(_BASEDPYRIGHT_CMD is None, reason="basedpyright is not installed")


# --- 環境 ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Svc(ToolServices):
    """合成ルートが組む形（``reader_pool`` / ``lsp_pool`` / 書き込み手）を duck typing で足した入れ物。"""

    reader_pool: ReaderPool | None = None
    lsp_pool: LspPool | None = None
    writer: IndexWriter | None = None


@dataclass(slots=True)
class Env:
    root: Path
    ws: Workspace
    svc: _Svc
    pipeline: Pipeline
    writer: IndexWriter
    readers: ReaderPool
    registry: ProcessRegistry
    pool: LspPool | None
    log: Path | None = None  # 偽サーバーの要求の記録

    async def resolve(self, fmt: str = "json", **args: Any) -> Any:
        r = await self.pipeline.call_tool("resolve_symbols", {"format": fmt, **args})
        assert not r.is_error, r.text
        return r

    def cache_rows(self) -> list[tuple[str, str, str]]:
        with self.readers.reader() as conn:
            return [
                (str(u), str(s), str(t))
                for u, s, t in conn.execute("SELECT urn, sha256, hover_text FROM _hover_cache ORDER BY urn")
            ]

    def data_version(self) -> int:
        return self.writer.data_version()

    def requests(self, method: str) -> list[Any]:
        if self.log is None or not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [r["params"] for r in rows if r["method"] == method]


PoolMaker = Callable[[Path, Workspace], Awaitable[tuple[LspPool | None, Path | None]]]


@asynccontextmanager
async def environment(tmp_path: Path, registry: ProcessRegistry, make_pool: PoolMaker) -> AsyncIterator[Env]:
    root = Path(os.path.realpath(tmp_path / WS))
    shutil.copytree(POLY, root, ignore=shutil.ignore_patterns("__pycache__"))
    db = tmp_path / "db" / "paad.sqlite"
    ws = Workspace(workspace_id=WS, root=root, python_env=PythonEnv(interpreter=sys.executable))
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    trees = TreeCache()
    indexer = Indexer(ws, writer, readers, trees)
    pool: LspPool | None = None
    try:
        report = await indexer.scan()
        assert not report.errors, report.errors
        pool, log = await make_pool(root, ws)
        svc = _Svc(
            roots=lambda: [str(root)],
            trees=trees,
            lines=compact,
            reader_pool=readers,
            lsp_pool=pool,
            writer=writer,
        )
        registry_tools = ToolRegistry()
        registry_tools.register(t04.TOOL_SPEC)
        yield Env(root, ws, svc, Pipeline(registry_tools, services=svc), writer, readers, registry, pool, log)
    finally:
        if pool is not None:
            await pool.shutdown_all(300)
        await indexer.aclose()
        readers.close()
        writer.close()
        await registry.terminate_all(200, close=True)
        assert registry.live() == []


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


def items(r: Any) -> list[dict[str, Any]]:
    return r.structured["data"]["items"]


def meta(r: Any) -> dict[str, Any]:
    return r.structured["_resolution_meta"]


def pos_of(path: Path, needle: str, *, line: int) -> Position:
    text = path.read_text(encoding="utf-8").split("\n")[line]
    return Position(line, text.index(needle))


# --- 実機: basedpyright ---------------------------------------------------------------------------


def real_pool_maker(registry: ProcessRegistry) -> PoolMaker:
    async def make(root: Path, ws: Workspace) -> tuple[LspPool | None, Path | None]:
        assert _BASEDPYRIGHT is not None and _BASEDPYRIGHT_CMD is not None
        pool = LspPool(
            registry,
            servers=[_BASEDPYRIGHT],
            resolver=lambda defn, w: list(_BASEDPYRIGHT_CMD),
            init_timeout_ms=60_000,
            ready_fallback_s=1.0,
            grace_ms=300,
        )
        await pool.ensure_started("python", ws)
        assert await pool.wait_ready("python", ws, 60_000), pool.status(
            "python", ws
        )  # 条件待ち（固定の sleep なし）
        return pool, None

    return make


@needs_basedpyright
async def test_real_exact_cold_warm_and_after_the_lsp_is_stopped(
    tmp_path: Path, registry: ProcessRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, int] = {"definition": 0, "hover": 0}
    real_definition, real_hover = features.definition, features.hover

    async def spy_definition(*a: Any, **k: Any) -> Any:
        calls["definition"] += 1
        return await real_definition(*a, **k)

    async def spy_hover(*a: Any, **k: Any) -> Any:
        calls["hover"] += 1
        return await real_hover(*a, **k)

    monkeypatch.setattr(features, "definition", spy_definition)
    monkeypatch.setattr(features, "hover", spy_hover)

    async with environment(tmp_path, registry, real_pool_maker(registry)) as env:
        assert env.pool is not None
        handle = env.pool.get("python", env.ws, start_if_needed=False)
        assert handle is not None
        pos = pos_of(env.root / PAY_PY, "process_charge", line=METHOD_LINE0)
        version0 = env.data_version()
        assert env.cache_rows() == []

        # --- 冷: LSP に聞いて exact ---
        t0 = time.perf_counter()
        cold = await env.resolve(targets=[METHOD, CLASS])
        cold_ms = (time.perf_counter() - t0) * 1000  # didOpen を含む（解析待ち）
        for item in items(cold):
            assert item["ok"] and item["confidence_level"] == "exact", item
        hover = await real_hover(handle, RelPath(PAY_PY), pos)  # 事後条件 4 の期待値: 同じ位置の hover
        assert hover is not None and "process_charge" in hover and "Receipt" in hover
        assert items(cold)[0]["signature_expanded"] == hover  # 事後条件 4
        assert (
            items(cold)[0]["signature_expanded"] != items(cold)[0]["symbol"]["signature"]
        )  # 型を展開している
        m = meta(cold)
        assert (m["confidence_level"], m["is_degraded"], m["mixed"]) == ("exact", False, False)
        assert (m["engine_used"], m["lsp_status"], m["accuracy_warning"]) == (
            "basedpyright+tree-sitter",
            "ready",
            None,
        )
        assert calls == {"definition": 2, "hover": 2}
        assert {u for u, _, _ in env.cache_rows()} == {METHOD_URN, f"lk://{WS}/{PAY_PY}#PaymentProcessor"}
        assert env.data_version() == version0  # 内部テーブルへの書き込みは data_version を進めない

        # --- 温: PaaD（_hover_cache）から。LSP は呼ばれない ---
        t0 = time.perf_counter()
        warm = await env.resolve(targets=[METHOD, CLASS])
        warm_ms = (time.perf_counter() - t0) * 1000
        assert calls == {"definition": 2, "hover": 2}
        assert [i["signature_expanded"] for i in items(warm)] == [
            i["signature_expanded"] for i in items(cold)
        ]
        mw = meta(warm)
        assert (mw["confidence_level"], mw["is_degraded"]) == ("exact", False)
        assert (mw["engine_used"], mw["lsp_status"]) == (selector.ENGINE_LSP_CACHE, "ready")
        print(f"\nT-04 exact: cold={cold_ms:.0f} ms (LSP), warm={warm_ms:.0f} ms (PaaD cache)")

        compact_warm = await env.resolve("compact", targets=[METHOD])
        assert g.validate(compact_warm.text) == [], compact_warm.text
        assert compact_warm.text.splitlines()[0].startswith("# resolve_symbols ok conf=exact lsp=ready")

        # --- 別の言語（LSP の定義が無い）の項目は structural。応答は mixed（降格はファイル単位） ---
        mixed = await env.resolve(targets=[METHOD, TS_CLASS])
        assert [i["confidence_level"] for i in items(mixed)] == ["exact", "structural"]
        mm = meta(mixed)
        assert (mm["confidence_level"], mm["mixed"], mm["is_degraded"]) == ("structural", True, True)
        assert mm["confidence_breakdown"] == {"exact": 1, "structural": 1}

        # --- LSP を止める: キャッシュにある項目は exact のまま。プロセスは起こさない ---
        await env.pool.shutdown_all(300)
        assert env.registry.live() == []
        after = await env.resolve(targets=[METHOD, CLASS])
        assert [i["confidence_level"] for i in items(after)] == ["exact", "exact"]
        assert items(after)[0]["signature_expanded"] == hover
        assert (meta(after)["confidence_level"], meta(after)["is_degraded"]) == ("exact", False)
        assert meta(after)["engine_used"] == selector.ENGINE_LSP_CACHE
        assert calls == {"definition": 2, "hover": 2}
        assert env.registry.live() == []  # cache hit は LSP を起こさない

        # --- 聞いたことのない項目は、止まった LSP を待たず structural に降格 ---
        other = await env.resolve(
            targets=[{"path": PAY_PY, "symbol_name": "PaymentProcessor._validate_limit"}]
        )
        assert items(other)[0]["confidence_level"] == "structural"
        mo = meta(other)
        assert (mo["confidence_level"], mo["is_degraded"]) == ("structural", True)
        assert mo["accuracy_warning"] and "構文木" in mo["accuracy_warning"]
        assert env.registry.live() == []


# --- 偽サーバー -----------------------------------------------------------------------------------

FAKE_SERVER = r"""
import json
import sys
import time

inp = sys.stdin.buffer
out = sys.stdout.buffer
spec = json.load(open(sys.argv[1], encoding="utf-8"))
LOG = spec["log"]


def read():
    length = None
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.decode("ascii").partition(":")
        if key.lower() == "content-length":
            length = int(value)
    return json.loads(inp.read(length))


def send(msg):
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"method": msg.get("method"), "params": msg.get("params")}, ensure_ascii=False) + "\n")


while True:
    msg = read()
    if msg is None:
        break
    method = msg.get("method")
    mid = msg.get("id")
    if method in ("textDocument/didOpen", "textDocument/didChange", "$/cancelRequest"):
        log(msg)
        if method == "textDocument/didOpen":
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": msg["params"]["textDocument"]["uri"], "diagnostics": []}})
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
        continue
    if method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
        continue
    if method == "exit":
        break
    if mid is None or method is None:
        continue
    log(msg)
    rule = spec["responses"].get(method, {"result": None})
    if "sleep" in rule:
        time.sleep(rule["sleep"])
        continue
    send({"jsonrpc": "2.0", "id": mid, "result": rule.get("result")})
"""

FAKE_DEF = LspServerDef(
    language_id="python", name="fakels", argv_candidates=(("fakels",),), locale="en", requests_m1=frozenset()
)
HOVER_TEXT = "(method) async def process_charge(self: PaymentProcessor, req: PaymentRequest) -> Receipt"


def _rng(line: int, col: int, length: int) -> dict[str, Any]:
    return {"start": {"line": line, "character": col}, "end": {"line": line, "character": col + length}}


def fake_pool_maker(
    tmp_path: Path,
    registry: ProcessRegistry,
    *,
    hover: Any = "default",
    definition: Any = "default",
    definition_sleep: float | None = None,
) -> PoolMaker:
    """``ready`` の偽サーバーのプールを作る。``hover`` / ``definition`` は台本の ``result``（既定は process_charge の宣言と型）。"""

    async def make(root: Path, ws: Workspace) -> tuple[LspPool | None, Path | None]:
        script = tmp_path / "fake_lsp.py"
        script.write_text(FAKE_SERVER, encoding="utf-8")
        log = tmp_path / "requests.jsonl"
        col = pos_of(root / PAY_PY, "process_charge", line=METHOD_LINE0).col
        uri = (root / PAY_PY).as_uri()
        hover_result = (
            {"contents": {"kind": "markdown", "value": f"```python\n{HOVER_TEXT}\n```"}}
            if hover == "default"
            else hover
        )
        definition_result = (
            [{"uri": uri, "range": _rng(METHOD_LINE0, col, len("process_charge"))}]
            if definition == "default"
            else definition
        )
        responses: dict[str, Any] = {
            "textDocument/hover": {"result": hover_result},
            "textDocument/definition": (
                {"sleep": definition_sleep} if definition_sleep is not None else {"result": definition_result}
            ),
        }
        spec = tmp_path / "spec.json"
        spec.write_text(json.dumps({"log": str(log), "responses": responses}), encoding="utf-8")
        pool = LspPool(
            registry,
            servers=[FAKE_DEF],
            resolver=lambda defn, w: [sys.executable, str(script), str(spec)],
            ready_fallback_s=0.2,
            grace_ms=200,
        )
        assert await pool.wait_ready("python", ws, 15_000)
        return pool, log

    return make


async def test_fake_exact_is_cached_and_the_second_call_does_not_ask_the_lsp(
    tmp_path: Path, registry: ProcessRegistry
) -> None:
    async with environment(tmp_path, registry, fake_pool_maker(tmp_path, registry)) as env:
        first = await env.resolve(targets=[METHOD])
        assert items(first)[0]["confidence_level"] == "exact"
        assert items(first)[0]["signature_expanded"] == HOVER_TEXT
        assert meta(first)["engine_used"] == "fakels+tree-sitter" and meta(first)["lsp_status"] == "ready"
        assert "warnings" not in items(first)[0]  # 定義が宣言と一致
        assert (
            len(env.requests("textDocument/definition")) == 1 and len(env.requests("textDocument/hover")) == 1
        )
        sent = env.requests("textDocument/hover")[0]
        assert sent["position"] == {
            "line": METHOD_LINE0,
            "character": pos_of(env.root / PAY_PY, "process_charge", line=METHOD_LINE0).col,
        }

        second = await env.resolve(targets=[METHOD])
        assert items(second)[0]["signature_expanded"] == HOVER_TEXT
        assert meta(second)["engine_used"] == selector.ENGINE_LSP_CACHE
        assert (
            len(env.requests("textDocument/definition")) == 1 and len(env.requests("textDocument/hover")) == 1
        )


async def test_fake_definition_elsewhere_is_reported_but_does_not_move_the_target(
    tmp_path: Path, registry: ProcessRegistry
) -> None:
    other = (tmp_path / "x").as_uri()
    maker = fake_pool_maker(tmp_path, registry, definition=[{"uri": other, "range": _rng(2, 0, 3)}])
    async with environment(tmp_path, registry, maker) as env:
        r = await env.resolve(targets=[METHOD])
        item = items(r)[0]
        assert item["confidence_level"] == "exact" and item["symbol"]["urn"] == METHOD_URN
        assert any(w.startswith("LSP resolves this declaration to") for w in item["warnings"])


async def test_fake_timeout_degrades_to_structural_and_skips_the_rest_of_the_call(
    tmp_path: Path, registry: ProcessRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(features, "LSP_TIMEOUT_DEFINITION_MS", 300)
    maker = fake_pool_maker(tmp_path, registry, definition_sleep=30)
    async with environment(tmp_path, registry, maker) as env:
        r = await env.resolve(targets=[METHOD, CLASS])
        got = items(r)
        assert [i["confidence_level"] for i in got] == ["structural", "structural"]
        assert got[0]["signature_expanded"] == got[0]["symbol"]["signature"]  # 型は展開されない
        m = meta(r)
        assert (m["confidence_level"], m["is_degraded"], m["lsp_status"]) == ("structural", True, "ready")
        assert m["engine_used"] == "tree-sitter+imports"
        assert m["accuracy_warning"] == selector.warning_text("lsp_timeout", timeout_ms=3000)
        assert len(env.requests("textDocument/definition")) == 1  # 2 件目は LSP に聞かない
        assert env.requests("textDocument/hover") == []
        assert env.cache_rows() == []  # 失敗は書き戻さない
        assert env.pool is not None and env.pool.handles()[0].timeouts == 1


async def test_fake_position_clamped_degrades_only_that_item(
    tmp_path: Path, registry: ProcessRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = features._lsp_position
    seen = {"n": 0}

    def clamp_once(handle: Any, client: Any, index: Any, pos: Any) -> Any:
        seen["n"] += 1
        if seen["n"] == 1:
            raise features._unavailable(handle, "position_clamped", "forced for the test")
        return real(handle, client, index, pos)

    monkeypatch.setattr(features, "_lsp_position", clamp_once)
    async with environment(tmp_path, registry, fake_pool_maker(tmp_path, registry)) as env:
        r = await env.resolve(targets=[METHOD, CLASS])
        assert [i["confidence_level"] for i in items(r)] == ["structural", "exact"]  # LSP は無効にならない
        m = meta(r)
        assert (m["confidence_level"], m["mixed"], m["is_degraded"], m["lsp_status"]) == (
            "structural",
            True,
            True,
            "ready",
        )
        assert m["accuracy_warning"] == selector.POSITION_CLAMPED_WARNING
        assert len(env.requests("textDocument/definition")) == 1  # 丸めの要る位置は送らない
        assert {u for u, _, _ in env.cache_rows()} == {f"lk://{WS}/{PAY_PY}#PaymentProcessor"}


async def test_fake_empty_hover_stays_structural_and_is_not_cached(
    tmp_path: Path, registry: ProcessRegistry
) -> None:
    async with environment(tmp_path, registry, fake_pool_maker(tmp_path, registry, hover=None)) as env:
        r = await env.resolve(targets=[METHOD])
        item = items(r)[0]
        assert (
            item["confidence_level"] == "structural"
            and item["signature_expanded"] == item["symbol"]["signature"]
        )
        assert NO_HOVER_WARNING in item["warnings"]
        assert (meta(r)["is_degraded"], meta(r)["accuracy_warning"]) == (True, NO_HOVER_WARNING)
        assert env.cache_rows() == []


async def test_fake_cache_is_keyed_by_the_file_content(tmp_path: Path, registry: ProcessRegistry) -> None:
    async with environment(tmp_path, registry, fake_pool_maker(tmp_path, registry)) as env:
        await env.resolve(targets=[METHOD])
        [(urn, sha_before, _)] = env.cache_rows()
        assert urn == METHOD_URN and len(env.requests("textDocument/hover")) == 1

        path = env.root / PAY_PY
        path.write_bytes(path.read_bytes() + b"\n# changed\n")  # 末尾に足す: 宣言の位置は変わらない
        again = await env.resolve(targets=[METHOD])
        assert items(again)[0]["confidence_level"] == "exact"
        assert len(env.requests("textDocument/hover")) == 2  # 内容が変わったのでもう一度聞く
        [(urn2, sha_after, _)] = env.cache_rows()  # 古い版の行は消える
        assert urn2 == urn and sha_after != sha_before


async def test_not_ready_lsp_is_not_waited_for_and_is_started(
    tmp_path: Path, registry: ProcessRegistry
) -> None:
    async def sleeping(root: Path, ws: Workspace) -> tuple[LspPool | None, Path | None]:
        pool = LspPool(  # initialize に答えない LSP: starting のまま
            registry,
            servers=[FAKE_DEF],
            resolver=lambda defn, w: [sys.executable, "-c", "import time; time.sleep(120)"],
            ready_fallback_s=0.2,
            grace_ms=200,
        )
        return pool, None

    async with environment(tmp_path, registry, sleeping) as env:
        assert env.pool is not None
        assert env.pool.status("python", env.ws).status.value == "standby"
        t0 = time.perf_counter()
        r = await env.resolve(targets=[METHOD])
        elapsed = time.perf_counter() - t0
        assert elapsed < 5.0  # 起動の完了を待たない（initialize には答えない LSP でも戻る）
        assert items(r)[0]["confidence_level"] == "structural"
        m = meta(r)
        assert (m["confidence_level"], m["is_degraded"], m["lsp_status"]) == ("structural", True, "indexing")
        assert m["accuracy_warning"] and m["accuracy_warning"].startswith("LSP が起動・索引中")
        assert env.pool.status("python", env.ws).status.value == "indexing"  # 起動は促した
        assert env.cache_rows() == []
