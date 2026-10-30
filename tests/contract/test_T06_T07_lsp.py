"""T-06 ``find_references_and_implementations`` と T-07 ``query_call_hierarchy`` の LSP ``exact`` 経路の契約テスト。

* 実機: ``.venv`` の basedpyright に ``tests/fixtures/poly``（``tmp_path`` に複製）を読ませる。見つからなければ skip。索引は実の
  ``Indexer``（``structural`` の ``relations`` が入る）、書き手は実の ``IndexWriter``。ツールは ``Pipeline`` 経由で呼ぶ。
* 1 回目は LSP から ``exact``（``relations`` に書き戻す）、2 回目は LSP を止めても PaaD から ``exact``。ファイルを編集すると
  目印が外れて LSP に聞き直す。
* 偽サーバー: ``references`` / ``callHierarchy`` に答えない LSP でタイムアウトを起こし、``structural`` に降格する。
* ``tests/contract/test_T06_T07.py``（structural 版）には依存しない。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _lsp_settle import first_analysis, settle

from lucianke.app import build_services
from lucianke.core.model import Position, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.core.urn import CodeSel, Urn
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool, open_reader
from lucianke.index.writer import IndexWriter
from lucianke.lsp import features
from lucianke.lsp.pool import FileChangeType, LspPool
from lucianke.lsp.servers import LspServerDef, default_server, find_command
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import _lsp_exact
from lucianke.tools import t06_find_references_and_implementations as t06
from lucianke.tools import t07_query_call_hierarchy as t07
from lucianke.tools._services import ToolServices
from lucianke.workspace.pyenv import detect_python_env
from lucianke.workspace.types import Workspace

REPO = Path(__file__).resolve().parents[2]
POLY = REPO / "tests" / "fixtures" / "poly"
WS = "poly"
PAY = "src/shop/services/payment.py"
ROUTES = "src/shop/api/routes.py"
TESTS = "tests/test_payment.py"
PROCESS_CHARGE = "PaymentProcessor.process_charge"
CALL = "await processor.process_charge(req)"

_BASEDPYRIGHT = default_server("python", "types")
_BASEDPYRIGHT_CMD = (
    find_command(_BASEDPYRIGHT, workspace_root=REPO, venv=Path(sys.prefix)) if _BASEDPYRIGHT else None
)
needs_basedpyright = pytest.mark.skipif(_BASEDPYRIGHT_CMD is None, reason="basedpyright is not installed")

READY_WAIT_MS = 60_000


def urn(path: str, qualified: str | None = None) -> str:
    return str(Urn(WS, path, CodeSel(qualified) if qualified else None))


PC = {"urn": urn(PAY, PROCESS_CHARGE)}


def find_lines(root: Path, path: str, needle: str) -> list[int]:
    """``needle`` を含む行の番号（1 始まり）。"""
    text = (root / path).read_text(encoding="utf-8").splitlines()
    return [i + 1 for i, line in enumerate(text) if needle in line]


@dataclass(slots=True)
class Services(ToolServices):
    """``WiredServices`` が持つものだけを duck typing で足した入れ物（``lsp_pool`` / ``writer`` / ``db_path``）。"""

    db_path: Path | None = None
    lsp_pool: Any = None
    writer: Any = None
    fixed: Workspace | None = None

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        assert self.fixed is not None
        return self.fixed


class Env:
    def __init__(
        self,
        ws: Workspace,
        db: Path,
        writer: IndexWriter,
        readers: ReaderPool,
        indexer: Indexer,
        pool: LspPool,
        pipeline: Pipeline,
    ) -> None:
        self.ws, self.db, self.writer, self.readers = ws, db, writer, readers
        self.indexer, self.pool, self.pipeline = indexer, pool, pipeline

    @property
    def root(self) -> Path:
        return self.ws.root

    def sql(self, query: str, *params: Any) -> list[tuple[Any, ...]]:
        conn = open_reader(self.db)
        try:
            return [tuple(r) for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()

    def exact_rows(self, target: str, kind: str | None = None) -> list[tuple[Any, ...]]:
        where = " AND relation_kind = ?" if kind else ""
        return self.sql(
            "SELECT source_urn, call_site_line, call_site_col, relation_kind, use_kind FROM relations"
            " WHERE target_urn = ? AND confidence_level = 'exact'" + where + " ORDER BY 1, 2, 3",
            target,
            *([kind] if kind else []),
        )

    async def refs(self, target: dict[str, Any], **args: Any):
        return await self.pipeline.call_tool(
            "find_references_and_implementations", {"target": target, "format": "json", **args}
        )

    async def calls(self, target: dict[str, Any], direction: str, **args: Any):
        return await self.pipeline.call_tool(
            "query_call_hierarchy", {"target": target, "direction": direction, "format": "json", **args}
        )

    async def stop_lsp(self) -> None:
        await self.pool.shutdown_all(300)


def make_pipeline(
    base: ToolServices, ws: Workspace, db: Path, pool: LspPool, writer: IndexWriter
) -> Pipeline:
    registry = ToolRegistry()
    registry.register(t06.TOOL_SPEC)
    registry.register(t07.TOOL_SPEC)
    svc = Services(
        roots=base.roots,
        trees=base.trees,
        lines=base.lines,
        db_path=db,
        lsp_pool=pool,
        writer=writer,
        fixed=ws,
    )
    return Pipeline(registry, services=svc, data_version_provider=writer.data_version)


async def build_env(registry: ProcessRegistry, root: Path, cache: Path, pool: LspPool, ws: Workspace) -> Env:
    """索引を作って ``Env`` を返す。途中で失敗したら、作った分（書き手のスレッド・読み手）を閉じてから上げる（BUG-0076）。"""
    base = build_services(lambda: [str(root)])
    db = cache / "paad.sqlite"
    writer = await asyncio.to_thread(IndexWriter, db)
    readers = ReaderPool(db)
    indexer = Indexer(ws, writer, readers, base.trees)
    try:
        await indexer.scan()
        return Env(ws, db, writer, readers, indexer, pool, make_pipeline(base, ws, db, pool, writer))
    except BaseException:
        try:
            await indexer.aclose()
        finally:
            readers.close()
            await asyncio.to_thread(writer.close)
        raise


async def close_env(env: Env, registry: ProcessRegistry) -> None:
    """LSP・索引器・読み手・書き手・子プロセスを閉じる。どこかで失敗しても残りは必ず閉じる（BUG-0076）。"""
    async with contextlib.AsyncExitStack() as stack:
        stack.push_async_callback(registry.terminate_all, 200, close=True)
        stack.push_async_callback(asyncio.to_thread, env.writer.close)
        stack.callback(env.readers.close)
        stack.push_async_callback(env.indexer.aclose)
        stack.push_async_callback(env.pool.shutdown_all, 300)
    assert registry.live() == []


@contextlib.asynccontextmanager
async def opened_env(
    registry: ProcessRegistry, root: Path, cache: Path, pool: LspPool, ws: Workspace
) -> AsyncIterator[Env]:
    """``build_env`` して、本体（LSP の起動・準備の待ちを含む）が失敗・時間切れでも ``close_env`` まで走らせる（BUG-0076）。

    負荷の高いときに準備の待ちで落ちたセットアップが書き手のスレッドを残し、後の別のテストまで落としていた。"""
    env = await build_env(registry, root, cache, pool, ws)
    try:
        yield env
    finally:
        await close_env(env, registry)


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


async def warm_up(pool: LspPool, ws: Workspace) -> None:
    """basedpyright は ``ready`` の合図のあとも解析が続き、最初の問い合わせが空や部分的な答えになる（実測）。
    対象のファイルの最初の診断（解析の完了の合図。製品と同じ）を待ち、process_charge の参照と呼び出し先が揃うまで条件待ちする
    （固定の sleep ではない）。負荷の高いときは 1 回の問い合わせが本番の期限（参照 3 秒）を超えるので、準備の問い合わせだけ期限を
    延ばす（BUG-0076）。開くのは対象のファイルだけ（呼び元のファイルを開くと、ディスクを書き換えて ``didChangeWatchedFiles`` を
    送る編集の試験の前提が変わる。開いた文書の中身はディスクではなく送った版になる）。"""
    handle = pool.get("python", ws)
    assert handle is not None
    await first_analysis(handle, [PAY])
    text = (ws.root / PAY).read_text(encoding="utf-8").splitlines()
    line = next(i for i, t in enumerate(text) if "async def process_charge" in t)
    pos = Position(line, text[line].index("process_charge"))

    async def complete() -> bool:
        refs = await features.references(handle, RelPath(PAY), pos, include_declaration=False)
        items = await features.prepare_call_hierarchy(handle, RelPath(PAY), pos)
        out = await features.outgoing_calls(handle, items[0]) if items else []
        return len(refs) >= 3 and {"_validate_limit", "Receipt"} <= {e.item.name for e in out}

    await settle(complete, what="references and outgoing calls of process_charge")


@pytest.fixture
async def real(registry: ProcessRegistry, tmp_path: Path):
    _lsp_exact.MEMO.clear()
    root = tmp_path / "poly"
    shutil.copytree(POLY, root, ignore=shutil.ignore_patterns("__pycache__"))
    # tests/ から `shop`（src/ の下）を import 解決できるのは、検出した import の根（`src`）が LSP の extraPaths に渡るから
    # （pyrightconfig.json は置かない。20d）。本番と同じ検出を通す
    ws = Workspace(
        workspace_id=WS, root=root.resolve(), python_env=detect_python_env(root.resolve(), sys.executable)
    )
    assert ws.python_env is not None and ws.python_env.extra_paths == ("src",)
    pool = LspPool(
        registry,
        servers=[_BASEDPYRIGHT],
        resolver=lambda defn, w: list(_BASEDPYRIGHT_CMD),
        init_timeout_ms=READY_WAIT_MS,
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    async with opened_env(registry, root, cache, pool, ws) as env:
        await pool.ensure_started("python", ws)
        assert await pool.wait_ready("python", ws, READY_WAIT_MS), pool.status("python", ws)
        await warm_up(pool, ws)
        yield env


# --- T-06: exact → PaaD ------------------------------------------------------------------------


@needs_basedpyright
async def test_t06_first_call_is_exact_from_lsp_and_second_call_is_exact_from_paad(real: Env) -> None:
    routes_lines = find_lines(real.root, ROUTES, CALL)
    test_line = find_lines(real.root, TESTS, "p.process_charge(")[0]
    want = [(ROUTES, routes_lines[0]), (ROUTES, routes_lines[1]), (TESTS, test_line)]

    r1 = await real.refs(PC)
    assert not r1.is_error, r1.text
    meta = r1.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    assert meta["lsp_status"] == "ready" and "basedpyright" in meta["engine_used"], meta
    data = r1.structured["data"]
    assert [(x["path"], x["line"]) for x in data["references"]] == want
    assert {x["use"] for x in data["references"]} == {"call"}
    assert [x["enclosing_name"] for x in data["references"]] == [
        "create_order",
        "retry_order",
        "test_charge_ok",
    ]
    assert {x["confidence_level"] for x in data["references"]} == {"exact"}
    assert data["totals"] == {"files": 2, "sites": 3, "tests": 1}

    # relations に exact で書き戻されている（呼び出し元の囲み → 対象。使用種別は CALLS / call）
    rows = real.exact_rows(PC["urn"])
    assert len(rows) == 3 and {r[3] for r in rows} == {"CALLS"} and {r[4] for r in rows} == {"call"}

    # 2 回目: LSP を止めても PaaD の exact 行で同じ答え
    await real.stop_lsp()
    r2 = await real.refs(PC)
    assert not r2.is_error, r2.text
    meta2 = r2.structured["_resolution_meta"]
    assert meta2["confidence_level"] == "exact" and meta2["is_degraded"] is False, meta2
    assert meta2["engine_used"] == "lsp-cache+tree-sitter" and meta2["lsp_status"] != "ready", meta2
    assert [(x["path"], x["line"], x["use"]) for x in r2.structured["data"]["references"]] == [
        (x["path"], x["line"], x["use"]) for x in data["references"]
    ]
    assert r2.structured["data"]["totals"] == data["totals"]


@needs_basedpyright
async def test_t06_a_timed_out_references_request_does_not_degrade_later_calls(
    real: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """負荷で references が期限を超えて 1 回降格しても、次の問い合わせは exact（BUG-0090）。

    以前は期限切れで送る ``$/cancelRequest`` が basedpyright の references をプロセスが続く間ずっと壊し、以後の T-06 は
    「LSP が利用できない」と言いながら ``lsp_status=ready`` の structural を返し続けた。"""
    monkeypatch.setattr(features, "LSP_TIMEOUT_REFERENCES_MS", 0)
    r1 = await real.refs(PC)
    assert not r1.is_error, r1.text
    meta1 = r1.structured["_resolution_meta"]
    assert meta1["confidence_level"] == "structural" and meta1["lsp_status"] == "ready", meta1
    assert "0 ms" in (meta1["accuracy_warning"] or ""), meta1  # 期限切れの警告（事実どおり）
    monkeypatch.setattr(features, "LSP_TIMEOUT_REFERENCES_MS", 30_000)
    await asyncio.sleep(0.5)  # 打ち切った要求をサーバーが終える間（以前はこの後の要求が壊れていた）
    r = await real.refs(PC)  # 1 回目は降格したので PaaD の目印は無い: LSP に聞く
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and "basedpyright" in meta["engine_used"], meta
    assert meta["lsp_status"] == "ready", meta
    assert r.structured["data"]["totals"] == {"files": 2, "sites": 3, "tests": 1}
    handle = real.pool.get("python", real.ws, start_if_needed=False)
    assert handle is not None and handle.restarts == 0


@needs_basedpyright
async def test_t06_editing_a_file_makes_the_cache_stale_and_lsp_is_asked_again(real: Env) -> None:
    first = await real.refs(PC)
    assert first.structured["_resolution_meta"]["confidence_level"] == "exact"
    assert len(first.structured["data"]["references"]) == 3

    # routes.py に process_charge の呼び出しを 1 つ足す（索引器が再取り込みして exact 行が消える）
    path = real.root / ROUTES
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n\nasync def audit_order(order_id: str, req):\n    return await processor.process_charge(req)\n",
        encoding="utf-8",
    )
    assert await real.indexer.index_file(ROUTES) is not None
    await real.pool.notify_watched(real.ws, [(RelPath(ROUTES), FileChangeType.CHANGED)])  # 監視の経路と同じ

    second = await real.refs(PC)
    assert not second.is_error, second.text
    meta = second.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and "basedpyright" in meta["engine_used"], (
        meta
    )  # PaaD ではなく LSP
    names = [x["enclosing_name"] for x in second.structured["data"]["references"]]
    assert names == ["create_order", "retry_order", "audit_order", "test_charge_ok"]

    # LSP を止めて、もう一度ファイルを変えると、目印が外れて structural に降格する
    path.write_text(path.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")
    assert await real.indexer.index_file(ROUTES) is not None
    await real.stop_lsp()
    third = await real.refs(PC)
    assert not third.is_error, third.text
    assert third.structured["_resolution_meta"]["engine_used"] != "lsp-cache+tree-sitter"
    assert third.structured["_resolution_meta"]["confidence_level"] != "exact"


# --- T-07: exact -> PaaD ------------------------------------------------------------------------

GUARD_CREATE = "req.amount > 0 and not paid"
GUARD_RETRY = "attempt < 3"
GUARD_DECLINED = "not self._validate_limit(req.amount)"
CREATE = {"urn": urn(ROUTES, "create_order")}


def shape(r: Any) -> list[tuple[Any, ...]]:
    """木のノードの比較用の形（向き・深さ・名前・呼び出し箇所・when・外部か）。"""
    return [
        (
            n["direction"],
            n["depth"],
            n["name"],
            n["call_site_path"],
            n["call_site_line"],
            n["when"],
            n["is_external"],
        )
        for n in r.structured["data"]["nodes"]
    ]


@needs_basedpyright
async def test_t07_incoming_is_exact_from_lsp_then_from_paad(real: Env) -> None:
    r1 = await real.calls(PC, "incoming", depth=2)
    assert not r1.is_error, r1.text
    meta = r1.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    assert meta["lsp_status"] == "ready" and "basedpyright" in meta["engine_used"], meta
    nodes = r1.structured["data"]["nodes"]
    by_name = {n["name"]: n for n in nodes}
    assert set(by_name) == {"create_order", "retry_order", "test_charge_ok"}
    assert by_name["create_order"]["when"] == GUARD_CREATE and by_name["retry_order"]["when"] == GUARD_RETRY
    assert by_name["test_charge_ok"]["when"] is None and by_name["test_charge_ok"]["is_test"] is True
    assert {n["confidence_level"] for n in nodes} == {"exact"}
    want_lines = find_lines(real.root, ROUTES, CALL)
    assert [by_name["create_order"]["call_site_line"], by_name["retry_order"]["call_site_line"]] == want_lines

    rows = real.exact_rows(PC["urn"], "CALLS")  # 呼び元の囲み -> 対象（exact で書き戻し済み）
    assert len(rows) == 3 and {r[4] for r in rows} == {"call"}

    await real.stop_lsp()
    r2 = await real.calls(PC, "incoming", depth=2)
    assert not r2.is_error, r2.text
    meta2 = r2.structured["_resolution_meta"]
    assert meta2["confidence_level"] == "exact" and meta2["is_degraded"] is False, meta2
    assert meta2["engine_used"] == "lsp-cache+tree-sitter", meta2
    assert shape(r2) == shape(r1)


@needs_basedpyright
async def test_t07_outgoing_is_exact_from_lsp_then_from_paad(real: Env) -> None:
    r1 = await real.calls(CREATE, "outgoing", depth=3)
    assert not r1.is_error, r1.text
    meta = r1.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    assert "basedpyright" in meta["engine_used"], meta
    got = {(n["name"], n["depth"]): n for n in r1.structured["data"]["nodes"]}
    for key in (
        ("PaymentRequest", 1),
        (PROCESS_CHARGE, 1),
        ("PaymentProcessor._validate_limit", 2),
        ("PaymentDeclined", 2),
        ("LimitTable.get", 3),
        ("LimitExceeded", 3),
    ):
        assert key in got, (key, sorted(got))
    assert got[(PROCESS_CHARGE, 1)]["when"] == GUARD_CREATE
    assert got[("PaymentDeclined", 2)]["when"] == GUARD_DECLINED
    assert got[(PROCESS_CHARGE, 1)]["parent_urn"] == CREATE["urn"]
    assert any(n["is_external"] for n in got.values())  # Decimal / ValueError は外部の葉
    assert {n["confidence_level"] for n in got.values()} == {"exact"}

    await real.stop_lsp()
    r2 = await real.calls(CREATE, "outgoing", depth=3)
    assert not r2.is_error, r2.text
    meta2 = r2.structured["_resolution_meta"]
    assert meta2["confidence_level"] == "exact" and meta2["engine_used"] == "lsp-cache+tree-sitter", meta2
    assert shape(r2) == shape(r1)


@needs_basedpyright
async def test_t07_editing_a_file_makes_the_cache_stale(real: Env) -> None:
    first = await real.calls(PC, "incoming", depth=1)
    assert first.structured["_resolution_meta"]["confidence_level"] == "exact"
    path = real.root / ROUTES
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n\nasync def audit_order(order_id: str, req):\n    return await processor.process_charge(req)\n",
        encoding="utf-8",
    )
    assert await real.indexer.index_file(ROUTES) is not None
    await real.pool.notify_watched(real.ws, [(RelPath(ROUTES), FileChangeType.CHANGED)])
    second = await real.calls(PC, "incoming", depth=1)
    assert not second.is_error, second.text
    meta = second.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and "basedpyright" in meta["engine_used"], meta
    assert {n["name"] for n in second.structured["data"]["nodes"]} == {
        "create_order",
        "retry_order",
        "audit_order",
        "test_charge_ok",
    }


# --- 偽サーバー: references / callHierarchy に答えない -> タイムアウトで structural に降格 -------------

SILENT_SERVER = r"""
import json
import sys

inp, out = sys.stdin.buffer, sys.stdout.buffer


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
    body = json.dumps(msg).encode("utf-8")
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()


while True:
    msg = read()
    if msg is None:
        break
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        break
    elif method == "textDocument/didOpen":
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": msg["params"]["textDocument"]["uri"], "diagnostics": []}})
    # それ以外の要求（references / callHierarchy）には答えない
"""

SILENT_DEF = LspServerDef(
    language_id="python", name="silent", argv_candidates=(("silent",),), locale="en", requests_m1=frozenset()
)

TARGET_URN = str(Urn("fk", "pkg/a.py", CodeSel("target")))


def make_fk(tmp_path: Path) -> Path:
    """偽サーバー用の小さなワークスペース（``pkg/a.py`` の ``target`` を ``pkg/b.py`` の ``caller`` が呼ぶ）。"""
    root = tmp_path / "fk"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text(
        "from pkg.a import target\n\n\ndef caller():\n    return target()\n", encoding="utf-8"
    )
    return root


@pytest.fixture
async def silent(registry: ProcessRegistry, tmp_path: Path):
    _lsp_exact.MEMO.clear()
    root = make_fk(tmp_path)
    script = tmp_path / "silent_lsp.py"
    script.write_text(SILENT_SERVER, encoding="utf-8")
    ws = Workspace(workspace_id="fk", root=root.resolve())
    pool = LspPool(
        registry,
        servers=[SILENT_DEF],
        resolver=lambda defn, w: [sys.executable, str(script)],
        ready_fallback_s=0.2,
        grace_ms=200,
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    async with opened_env(registry, root, cache, pool, ws) as env:
        await pool.ensure_started("python", ws)
        assert await pool.wait_ready("python", ws, READY_WAIT_MS)  # 条件待ち（ready_fallback_s で即戻る）
        yield env


async def test_t06_lsp_timeout_degrades_to_structural(silent: Env) -> None:
    r = await silent.refs({"urn": TARGET_URN})
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "structural" and meta["is_degraded"] is True, meta
    assert meta["lsp_status"] == "ready", meta  # LSP は落とさない。その要求に限った降格
    assert "3000 ms" in (meta["accuracy_warning"] or ""), meta
    refs = r.structured["data"]["references"]
    assert {x["path"] for x in refs} == {"pkg/b.py"} and "caller" in {x["enclosing_name"] for x in refs}
    assert {x["confidence_level"] for x in refs} == {"structural"}
    assert silent.pool.status("python", silent.ws).status.value == "ready"


async def test_t07_lsp_timeout_degrades_to_structural(silent: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    # T-07 の根の要求は要求 1 つの期限（5000 ms）ではなく 1 問い合わせの予算の残りまで待つ（BUG-0091）。予算を縮めて速く切る
    monkeypatch.setattr(_lsp_exact, "EXACT_BUDGET_S", 2.0)
    r = await silent.calls({"urn": TARGET_URN}, "incoming", depth=1)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "structural" and meta["is_degraded"] is True, meta
    assert meta["lsp_status"] == "ready", meta
    assert "予算 2 秒" in (meta["accuracy_warning"] or ""), meta  # 切れたのは予算（警告は事実どおり）
    assert [n["name"] for n in r.structured["data"]["nodes"]] == ["caller"]
    assert silent.pool.status("python", silent.ws).status.value == "ready"


# --- 偽サーバー: references が 1 度だけ basedpyright の「壊れた」エラーを返す -> 起動し直す（BUG-0090） -------------

BROKEN_MARK = "this._token.cancel is not a function"

BROKEN_SERVER = r"""
import json
import os
import sys

inp, out = sys.stdin.buffer, sys.stdout.buffer
marker, a_uri, b_uri = sys.argv[1], sys.argv[2], sys.argv[3]


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
    body = json.dumps(msg).encode("utf-8")
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()


def rng(sl, sc, el, ec):
    return {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}}


while True:
    msg = read()
    if msg is None:
        break
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        break
    elif method == "textDocument/didOpen":
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": msg["params"]["textDocument"]["uri"], "diagnostics": []}})
    elif method == "textDocument/references":
        if not os.path.exists(marker):  # 偽サーバーの全プロセスを通じて最初の 1 回だけ（起動し直した後は答える）
            open(marker, "x").close()
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32603,
                "message": "Request textDocument/references failed with message: this._token.cancel is not a function"}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": [
                {"uri": a_uri, "range": rng(0, 4, 0, 10)}, {"uri": b_uri, "range": rng(4, 11, 4, 17)}]})
    elif method == "textDocument/definition":
        send({"jsonrpc": "2.0", "id": mid, "result": [{"uri": a_uri, "range": rng(0, 4, 0, 10)}]})
    elif mid is not None and method is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": None})
"""

BROKEN_DEF = LspServerDef(
    language_id="python",
    name="broken",
    argv_candidates=(("broken",),),
    locale="en",
    requests_m1=frozenset(),
    restart_on_errors=(BROKEN_MARK,),
)


@pytest.fixture
async def broken(registry: ProcessRegistry, tmp_path: Path):
    _lsp_exact.MEMO.clear()
    root = make_fk(tmp_path).resolve()
    script = tmp_path / "broken_lsp.py"
    script.write_text(BROKEN_SERVER, encoding="utf-8")
    argv = [
        sys.executable,
        str(script),
        str(tmp_path / "broken.once"),
        (root / "pkg" / "a.py").as_uri(),
        (root / "pkg" / "b.py").as_uri(),
    ]
    ws = Workspace(workspace_id="fk", root=root)
    pool = LspPool(
        registry,
        servers=[BROKEN_DEF],
        resolver=lambda defn, w: list(argv),
        ready_fallback_s=0.2,
        grace_ms=200,
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    async with opened_env(registry, root, cache, pool, ws) as env:
        await pool.ensure_started("python", ws)
        assert await pool.wait_ready("python", ws, READY_WAIT_MS)
        yield env


async def test_t06_a_broken_lsp_is_restarted_and_the_answer_says_so(broken: Env) -> None:
    """LSP が references に以後答えられない壊れ方をしたら（basedpyright の BUG-0090 のエラー文）、その答えは ``lsp_status`` を
    ``ready`` と言わず（``indexing``）、警告は何が起きたか（サーバー・要求・エラー文・再起動中）を書く。LSP は起動し直され、
    次の問い合わせは exact。"""
    handle = broken.pool.get("python", broken.ws, start_if_needed=False)
    assert handle is not None
    pid = handle.pid
    r = await broken.refs({"urn": TARGET_URN})
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "structural" and meta["is_degraded"] is True, meta
    assert meta["lsp_status"] == "indexing", meta  # 再起動中: ready とは言わない
    warning = meta["accuracy_warning"] or ""
    assert "LSP（broken）が textDocument/references に以後答えられない状態になった" in warning, warning
    assert BROKEN_MARK in warning and "LSP を再起動しています" in warning, warning
    assert "利用できない" not in warning, warning
    assert {x["path"] for x in r.structured["data"]["references"]} == {"pkg/b.py"}  # 構文木の答えは出す

    assert await broken.pool.wait_ready("python", broken.ws, READY_WAIT_MS), broken.pool.status(
        "python", broken.ws
    )
    assert handle.pid != pid and handle.restarts == 1
    r2 = await broken.refs({"urn": TARGET_URN})
    assert not r2.is_error, r2.text
    meta2 = r2.structured["_resolution_meta"]
    assert meta2["confidence_level"] == "exact" and meta2["engine_used"] == "broken+tree-sitter", meta2
    assert meta2["lsp_status"] == "ready" and meta2.get("accuracy_warning") is None, meta2
    refs = r2.structured["data"]["references"]
    assert [(x["path"], x["enclosing_name"]) for x in refs] == [("pkg/b.py", "caller")]
