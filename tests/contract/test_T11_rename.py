"""T-11 ``rename_symbol`` の契約テスト、偽 LSP の側（仕様書 §11.5 T-11 の事後条件、§10.10 の EP-3・EP-4・EP-9）。

* 偽 LSP: Python の偽のハンドル（台本どおりに答えるクライアント）。prepareRename の拒否、作成・改名・ワークスペース外を含む
  ``WorkspaceEdit``、構文を壊す編集、タイムアウト、計算中の外部書き換えで、どのファイルも書かれないことを確かめる。
  実機の LSP プロセスを起こさないので速い。
* 実機（basedpyright）の側は ``test_T11_rename_real.py``。土台（``Rig`` など）は ``_t11_harness.py`` が両方に提供する。
* ``documents`` / ``diagnostics_hub`` は LSP ハンドル自身のもの（``edit_transaction`` ファクトリ経由）。診断は
  ``diagnostics_wait_ms=0``（``pending``）で見る（診断差分そのものはトランザクションのテストの担当）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _t11_harness import (
    PAY,
    PROCESS_CHARGE,
    ROUTES,
    WS,
    Rig,
    U,
    build_rig,
    copy_poly,
    index_writer_fixture,  # noqa: F401  (フィクスチャ index_writer)
    snapshot,
)

from lucianke.core.model import LspStatus, Position
from lucianke.core.textpos import Unit
from lucianke.index.writer import IndexWriter
from lucianke.lsp.client import to_lsp_position
from lucianke.lsp.documents import DocumentSync
from lucianke.lsp.pool import LspState, PoolStatus
from lucianke.workspace.types import Workspace

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
    """PC:T-11#4 prepareRename の拒否（エラー）のあと、どのファイルの SHA-256 も呼び出し前と同じ。"""
    rig = stub({"textDocument/prepareRename": None})
    before = snapshot(rig.root)
    r = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order")
    assert r.is_error and r.structured["error"]["code"] == "E_LSP_UNAVAILABLE"
    assert r.structured["error"]["details"]["reason"] == "prepare_rename_rejected"
    assert snapshot(rig.root) == before
    assert [m for m, _ in rig.handle.client.requests] == ["textDocument/prepareRename"]


async def test_stub_rename_timeout_is_e_timeout(stub):
    """PC:T-11#4 rename のタイムアウト（エラー）のあと、どのファイルの SHA-256 も呼び出し前と同じ。"""
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
    """1 つでも受け入れられない編集があれば、正しい編集が同じ応答に混じっていてもどのファイルも書かない（EP-9）。PC:T-11#4"""
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


async def test_t11_postcondition_4_applied_false_leaves_every_file_unchanged(stub):
    """``applied: false`` のとき、ワークスペース内のどのファイルの SHA-256 も呼び出し前と同じ。

    ``dry_run``（成功応答の ``applied: false``）と、期待ハッシュの不一致によるブロックを、全ファイルの SHA-256 で比べる。
    """
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
    assert len(before) >= 10
    target = {"urn": U(PAY, PROCESS_CHARGE)}
    dry = await rig.rename(target, "charge_order", dry_run=True)
    assert not dry.is_error, dry.text
    assert dry.structured["data"]["applied"] is False and "charge_order" in dry.structured["data"]["diff"]
    assert snapshot(rig.root) == before
    blocked = await rig.rename(target, "charge_order", expected_file_sha256s={ROUTES: "0" * 64})
    assert blocked.is_error and blocked.structured["error"]["code"] == "E_PRECONDITION_FAILED"
    assert snapshot(rig.root) == before


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
