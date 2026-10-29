"""T-11 ``rename_symbol`` の契約テスト（仕様書 §11.5 T-11 の事後条件 1〜4、§10.10 の EP-2・EP-3・EP-9）。

* 実機: ``.venv`` の basedpyright に ``tests/fixtures/poly``（``tmp_path`` に複製）を読ませ、``Pipeline`` 経由でツールを呼ぶ。
  見つからなければ skip。位置の単位は UTF-16（絵文字・日本語の行で確かめる）。
* 偽 LSP: Python の偽のハンドル（台本どおりに答えるクライアント）。prepareRename の拒否、作成・改名・ワークスペース外を含む
  ``WorkspaceEdit``、構文を壊す編集、タイムアウト、計算中の外部書き換えで、どのファイルも書かれないことを確かめる。
  実機の LSP プロセスを起こさないので速い。
* ``documents`` / ``diagnostics_hub`` は LSP ハンドル自身のもの（``edit_transaction`` ファクトリ経由）。診断は
  ``diagnostics_wait_ms=0``（``pending``）で見る（診断差分そのものはトランザクションのテストの担当）。
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lucianke.app import build_services
from lucianke.core.model import LspStatus, Position, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.core.textpos import Unit
from lucianke.core.urn import CodeSel, Urn
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.transaction import EditTransaction
from lucianke.index.writer import IndexWriter
from lucianke.lsp.client import to_lsp_position
from lucianke.lsp.documents import DocumentSync
from lucianke.lsp.pool import LspState, PoolStatus
from lucianke.lsp.servers import default_server, find_command
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import t11_rename_symbol as t11
from lucianke.tools._services import ToolServices
from lucianke.workspace.types import Workspace

REPO = Path(__file__).resolve().parents[2]
POLY = REPO / "tests" / "fixtures" / "poly"
WS = "poly"
PAY = "src/shop/services/payment.py"
ROUTES = "src/shop/api/routes.py"
TESTS = "tests/test_payment.py"
RST = "docs/api.rst"
PROCESS_CHARGE = "PaymentProcessor.process_charge"

_BASEDPYRIGHT = default_server("python", "types")
_BASEDPYRIGHT_CMD = (
    find_command(_BASEDPYRIGHT, workspace_root=REPO, venv=REPO / ".venv") if _BASEDPYRIGHT else None
)
needs_basedpyright = pytest.mark.skipif(_BASEDPYRIGHT_CMD is None, reason="basedpyright is not installed")


def U(path: str, qualified: str) -> str:
    return str(Urn(WS, path, CodeSel(qualified)))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    """ワークスペースの全ファイルの SHA-256（相対パス → sha）。"""
    return {
        p.relative_to(root).as_posix(): sha(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


# --- 土台 -----------------------------------------------------------------------------------


@dataclass(slots=True)
class Services(ToolServices):
    """``ToolServices`` に、T-11 が求める ``lsp_pool`` と ``edit_transaction(ws, **overrides)`` を足したもの。"""

    ws: Workspace | None = None
    lsp_pool: Any = None
    tx_kwargs: dict[str, Any] = field(default_factory=dict)

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        assert self.ws is not None
        return self.ws

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        return WS

    def edit_transaction(self, ws: Workspace, **overrides: Any) -> EditTransaction:
        return EditTransaction(ws, **self.tx_kwargs, **overrides)


@dataclass
class Rig:
    pipeline: Pipeline
    svc: Services
    ws: Workspace
    pool: Any
    handle: Any
    root: Path

    async def rename(self, target: dict[str, Any], new_name: str, fmt: str = "json", **args: Any):
        args.setdefault("diagnostics_wait_ms", 0)
        return await self.pipeline.call_tool(
            "rename_symbol", {"target": target, "new_name": new_name, "format": fmt, **args}
        )

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        """新しいファイルを作り、LSP に開かせる（LSP は作業ツリーの新規ファイルを自分では見つけない。
        本番では監視が ``workspace/didChangeWatchedFiles`` で知らせる）。"""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        if self.handle is not None:
            self.handle.documents.sync(RelPath(rel), text, "python")

    def pos(self, rel: str, needle: str, nth_line: int = 0) -> Position:
        """``needle`` を含む行（``nth_line`` 番目）の、その位置（0 始まり・コードポイント）。"""
        hits = [(i, t) for i, t in enumerate(self.read(rel).split("\n")) if needle in t]
        line, text = hits[nth_line]
        return Position(line, text.index(needle))


def build_rig(tmp_path: Path, pool: Any, handle: Any, ws: Workspace, writer: IndexWriter) -> Rig:
    registry = ToolRegistry()
    registry.register(t11.TOOL_SPEC)
    b = build_services(lambda: [str(ws.root)])
    svc = Services(
        roots=b.roots,
        trees=b.trees,
        lines=b.lines,
        ws=ws,
        lsp_pool=pool,
        tx_kwargs={
            "locks": WorkspaceLocks(),
            "writer_index": writer,
            "journal_dir": tmp_path / "cache" / "journal",
        },
    )
    return Rig(Pipeline(registry, services=svc), svc, ws, pool, handle, ws.root)


@pytest.fixture
async def index_writer(tmp_path: Path) -> AsyncIterator[IndexWriter]:
    w = IndexWriter(tmp_path / "cache" / "paad.sqlite")
    try:
        yield w
    finally:
        await asyncio.to_thread(w.close)


@pytest.fixture
async def procs():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


def copy_poly(tmp_path: Path) -> Path:
    dst = tmp_path / "poly"
    shutil.copytree(POLY, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst.resolve()


# --- 偽 LSP -----------------------------------------------------------------------------------


class StubClient:
    """台本どおりに答えるクライアント。``script[method]`` は値、例外、または ``(root, request) -> 値`` の関数。"""

    def __init__(self, root: Path, script: dict[str, Any]) -> None:
        self.root, self.script = root, script
        self.negotiated_encoding = Unit.UTF16
        self.server_capabilities: dict[str, Any] = {"renameProvider": {"prepareProvider": True}}
        self.is_connected = True
        self.requests: list[tuple[str, Any]] = []

    def on_request(self, method: str, handler: Any) -> None:
        pass

    def to_lsp_position(self, line_index: Any, pos: Position, *, details: Any = None) -> dict[str, int]:
        return to_lsp_position(line_index, pos, self.negotiated_encoding, details=details)

    async def request(self, method: str, params: Any, *, timeout_ms: float) -> Any:
        self.requests.append((method, params))
        rule = self.script.get(method)
        if isinstance(rule, BaseException):
            raise rule
        return rule(self.root, params) if callable(rule) else rule


class StubHub:
    async def wait_for(self, *args: Any, **kwargs: Any) -> bool:
        return True


class StubHandle:
    language_id = "python"
    server_name = "stub"
    defn = None
    state = LspState.READY

    def __init__(self, ws: Workspace, client: StubClient) -> None:
        self.ws, self.client = ws, client
        self.documents = DocumentSync(ws, lambda method, params: None)
        self.diagnostics = StubHub()

    def touch(self) -> None: ...
    def record_ok(self) -> None: ...
    def record_timeout(self) -> bool:
        return False


class StubPool:
    def __init__(self, handle: StubHandle | None, status: LspStatus = LspStatus.READY) -> None:
        self.handle, self._status = handle, status

    def status(self, language_id: str, ws: Workspace, role: str = "types") -> PoolStatus:
        return PoolStatus(
            self._status, None, "stub", None, None if self._status is LspStatus.READY else "crashed_3x"
        )

    async def wait_ready(
        self, language_id: str, ws: Workspace, timeout_ms: float, *, role: str = "types"
    ) -> bool:
        return self.handle is not None

    def get(self, language_id: str, ws: Workspace, *, role: str = "types", start_if_needed: bool = True):
        return self.handle


def rng(sl: int, sc: int, el: int, ec: int) -> dict[str, Any]:
    return {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}}


def te(
    path: str, root: Path, sl: int, sc: int, el: int, ec: int, text: str
) -> tuple[str, list[dict[str, Any]]]:
    return (root / path).as_uri(), [{"range": rng(sl, sc, el, ec), "newText": text}]


@pytest.fixture
async def stub(tmp_path: Path, index_writer: IndexWriter):
    """``await stub(script, handle=True, status=READY)`` → ``Rig``（``rig.handle.client.requests`` に送った要求が残る）。"""
    root = copy_poly(tmp_path)
    ws = Workspace(workspace_id=WS, root=root)

    def make(
        script: dict[str, Any] | None = None, *, handle: bool = True, status: LspStatus = LspStatus.READY
    ) -> Rig:
        client = StubClient(root, script or {})
        h = StubHandle(ws, client) if handle else None
        return build_rig(tmp_path, StubPool(h, status), h, ws, index_writer)

    return make


def def_position(rig: Rig) -> tuple[int, int]:
    p = rig.pos(PAY, "process_charge")
    return p.line, p.col


def rename_edit(root: Path, *extra_files: str) -> Callable[[Path, Any], dict[str, Any]]:
    """payment.py の定義と routes.py の呼び出しを `charge_order` にする正しい編集（`extra_files` は先頭に足す文書の編集）。"""

    def build(_root: Path, _params: Any) -> dict[str, Any]:
        pay = (root / PAY).read_text(encoding="utf-8").split("\n")
        routes = (root / ROUTES).read_text(encoding="utf-8").split("\n")
        pl = next(i for i, t in enumerate(pay) if "def process_charge" in t)
        rl = next(i for i, t in enumerate(routes) if "process_charge" in t)
        return {
            "changes": {
                (root / PAY).as_uri(): [
                    {
                        "range": rng(
                            pl, pay[pl].index("process_charge"), pl, pay[pl].index("process_charge") + 14
                        ),
                        "newText": "charge_order",
                    }
                ],
                (root / ROUTES).as_uri(): [
                    {
                        "range": rng(
                            rl,
                            routes[rl].index("process_charge"),
                            rl,
                            routes[rl].index("process_charge") + 14,
                        ),
                        "newText": "charge_order",
                    }
                ],
            }
        }

    return build


async def test_stub_happy_path_goes_through_prepare_rename_then_rename(stub):
    rig = stub(
        {
            "textDocument/prepareRename": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            }
        }
    )
    rig.handle.client.script["textDocument/rename"] = rename_edit(rig.root)
    before = snapshot(rig.root)
    pay_before = rig.read(PAY)
    r = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert not r.is_error, r.text
    methods = [m for m, _ in rig.handle.client.requests]
    assert methods == ["textDocument/prepareRename", "textDocument/rename"]
    sent = rig.handle.client.requests[1][1]
    line = pay_before.split("\n")[sent["position"]["line"]]
    assert line[sent["position"]["character"] :].startswith("process_charge")  # `def` ではなく名前の列を送る
    assert sent["newName"] == "charge_order"
    after = snapshot(rig.root)
    assert {p for p in after if after[p] != before[p]} == {PAY, ROUTES}


async def test_stub_prepare_rename_rejection_is_lsp_unavailable_and_writes_nothing(stub):
    rig = stub({"textDocument/prepareRename": None})
    before = snapshot(rig.root)
    r = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert r.is_error and r.structured["error"]["code"] == "E_LSP_UNAVAILABLE"
    assert r.structured["error"]["details"]["reason"] == "prepare_rename_rejected"
    assert snapshot(rig.root) == before
    assert [m for m, _ in rig.handle.client.requests] == ["textDocument/prepareRename"]


async def test_stub_rename_timeout_is_e_timeout(stub):
    rig = stub(
        {
            "textDocument/prepareRename": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            },
            "textDocument/rename": TimeoutError(),
        }
    )
    before = snapshot(rig.root)
    r = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert r.is_error and r.structured["error"]["code"] == "E_TIMEOUT"
    assert snapshot(rig.root) == before


@pytest.mark.parametrize(
    ("kind", "code", "reason"),
    [
        ("create", "E_UNSUPPORTED", "resource_operation"),
        ("delete", "E_UNSUPPORTED", "resource_operation"),
        ("rename", "E_UNSUPPORTED", "file_rename_not_supported"),
        ("outside", "E_INVALID_ARGUMENT", "edit_outside_workspace"),
        ("overlap", "E_INVALID_ARGUMENT", "overlapping_edits"),
        ("syntax", "E_SYNTAX_INVALID", None),
    ],
)
async def test_stub_unacceptable_edits_write_no_file(
    stub, tmp_path: Path, kind: str, code: str, reason: str | None
):
    """1 つでも受け入れられない編集があれば、正しい編集が同じ応答に混じっていてもどのファイルも書かない（EP-9）。"""
    rig = stub(
        {
            "textDocument/prepareRename": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            }
        }
    )
    good = rename_edit(rig.root)

    def edit(root: Path, params: Any) -> dict[str, Any]:
        we = good(root, params)
        pay = (root / PAY).as_uri()
        if kind == "create":
            return {
                "documentChanges": [{"kind": "create", "uri": (root / "new.py").as_uri()}, *_doc_changes(we)]
            }
        if kind == "delete":
            return {
                "documentChanges": [
                    *_doc_changes(we),
                    {"kind": "delete", "uri": (root / "src/shop/api/routes.py").as_uri()},
                ]
            }
        if kind == "rename":
            return {
                "documentChanges": [
                    *_doc_changes(we),
                    {
                        "kind": "rename",
                        "oldUri": (root / ROUTES).as_uri(),
                        "newUri": (root / "src/shop/api/r2.py").as_uri(),
                    },
                ]
            }
        if kind == "outside":
            we["changes"][(tmp_path / "elsewhere.py").as_uri()] = [{"range": rng(0, 0, 0, 1), "newText": "x"}]
        elif kind == "overlap":
            we["changes"][pay].append({"range": rng(30, 0, 40, 0), "newText": ""})
            we["changes"][pay].append({"range": rng(35, 0, 45, 0), "newText": ""})
        elif kind == "syntax":
            we["changes"][pay] = [{"range": rng(38, 4, 38, 9), "newText": "((("}] + we["changes"][pay]
        return we

    rig.handle.client.script["textDocument/rename"] = edit
    before = snapshot(rig.root)
    r = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert r.is_error, r.text
    err = r.structured["error"]
    assert err["code"] == code, err
    if reason:
        assert err["details"]["reason"] == reason, err
    assert snapshot(rig.root) == before


def _doc_changes(we: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"textDocument": {"uri": uri, "version": None}, "edits": edits}
        for uri, edits in we["changes"].items()
    ]


async def test_stub_external_write_while_computing_is_precondition_failed(stub):
    """LSP が計算している間に対象ファイルが外部で書き換えられたら、外部の変更を保ったまま失敗する（EP-4）。"""
    rig = stub(
        {
            "textDocument/prepareRename": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            }
        }
    )
    good = rename_edit(rig.root)

    def edit(root: Path, params: Any) -> dict[str, Any]:
        we = good(root, params)
        time.sleep(0.05)  # 更新時刻の刻み（Windows は十数 ms）を越えて、要求の開始より後の時刻にする
        with (root / ROUTES).open("ab") as f:
            f.write(b"\n# edited by someone else\n")
        return we

    rig.handle.client.script["textDocument/rename"] = edit
    before = snapshot(rig.root)
    r = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert r.is_error and r.structured["error"]["code"] == "E_PRECONDITION_FAILED"
    after = snapshot(rig.root)
    assert after[PAY] == before[PAY] and after[ROUTES] != before[ROUTES]
    assert rig.read(ROUTES).endswith("# edited by someone else\n")


async def test_stub_empty_workspace_edit_is_lsp_unavailable(stub):
    rig = stub(
        {
            "textDocument/prepareRename": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            },
            "textDocument/rename": {"changes": {}},
        }
    )
    r = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert r.is_error and r.structured["error"]["code"] == "E_LSP_UNAVAILABLE"
    assert r.structured["error"]["details"]["reason"] == "empty_workspace_edit"


async def test_stub_lsp_not_ready_or_missing(stub):
    down = stub(handle=False, status=LspStatus.UNAVAILABLE)
    r = await down.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert r.is_error and r.structured["error"]["code"] == "E_LSP_UNAVAILABLE"
    assert r.structured["error"]["details"]["reason"] == "crashed_3x"

    none = stub(handle=False, status=LspStatus.NOT_APPLICABLE)
    before = snapshot(none.root)
    r = await none.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert r.is_error and r.structured["error"]["code"] == "E_UNSUPPORTED"
    assert r.structured["error"]["details"]["reason"] == "no_lsp_for_language"
    assert snapshot(none.root) == before
