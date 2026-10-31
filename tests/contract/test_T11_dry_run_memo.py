"""T-11 ``rename_symbol`` の dry_run の結果を覚える（BUG-0128 の残り。``t11_rename_symbol.DRY_RUNS``）。

同じ LSP の起動・同じ対象・同じ新しい名前（同じ引数）の dry_run は、索引の版（``data_version``）・編集するファイルの内容・LSP が
開いている文書が覚えたときと同じなら、LSP に rename を聞き直さず、書き換えた後の内容も解析し直さずに同じ結果を返す。
書き込む呼び出し（``dry_run=false``）はいつも LSP に聞く。偽 LSP（``test_T11_rename.py`` の ``StubClient``）で、送った要求の数を
数えて確かめる。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from _t11_harness import (
    PAY,
    PROCESS_CHARGE,
    ROUTES,
    TESTS,
    U,
    index_writer_fixture,  # noqa: F401  (フィクスチャ index_writer)
    snapshot,
)
from test_T11_rename import rename_edit, stub  # noqa: F401  (フィクスチャ stub)

from lucianke.tools import t11_rename_symbol as t11

PREPARE = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}


@pytest.fixture(autouse=True)
def _fresh_memo():
    t11.DRY_RUNS.clear()
    yield
    t11.DRY_RUNS.clear()


def _rig(stub: Any, version: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    rig = stub({"textDocument/prepareRename": PREPARE})
    rig.handle.client.script["textDocument/rename"] = rename_edit(rig.root)
    rig.handle.started_at = "2026-10-31T00:00:00Z"  # 本物の LspHandle は起動の時刻を持つ（無いハンドルは覚えない）
    monkeypatch.setattr(t11, "_data_version", lambda svc, ws: version["v"])  # 試験の束には索引の版が無い
    return rig


def _renames(rig: Any) -> int:
    return sum(1 for m, _ in rig.handle.client.requests if m == "textDocument/rename")


def _edits(r: Any) -> Any:
    data = r.structured["data"]
    return data.get("changes") or data.get("edits") or data


async def test_same_dry_run_is_answered_without_asking_the_lsp_again(stub, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    version = {"v": 7}
    rig = _rig(stub, version, monkeypatch)
    before = snapshot(rig.root)
    first = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order", dry_run=True)
    assert not first.is_error, first.text
    second = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_order", dry_run=True)
    assert not second.is_error, second.text
    assert _renames(rig) == 1  # 2 回目は覚えた答え
    assert _edits(second) == _edits(first)
    assert snapshot(rig.root) == before  # dry_run は書かない

    other = await rig.rename({"urn": U(PAY, PROCESS_CHARGE)}, "charge_it", dry_run=True)  # 新しい名前が違う
    assert not other.is_error, other.text
    assert _renames(rig) == 2


async def test_a_new_index_version_or_changed_content_asks_the_lsp_again(stub, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    version = {"v": 7}
    rig = _rig(stub, version, monkeypatch)
    target = {"urn": U(PAY, PROCESS_CHARGE)}
    assert not (await rig.rename(target, "charge_order", dry_run=True)).is_error
    version["v"] = 8  # どれかのファイルの取り込みで索引の版が進んだ
    assert not (await rig.rename(target, "charge_order", dry_run=True)).is_error
    assert _renames(rig) == 2

    # 版は同じでも、編集するファイルの内容が覚えたときと違えば（監視が取り込む前の書き換え）聞き直す
    with (rig.root / ROUTES).open("ab") as f:
        f.write(b"\n# edited\n")
    r = await rig.rename(target, "charge_order", dry_run=True)
    assert not r.is_error, r.text
    assert _renames(rig) == 3
    assert not (await rig.rename(target, "charge_order", dry_run=True)).is_error
    assert _renames(rig) == 3  # 聞き直した結果を覚え直した

    # 名前を含まない文書を LSP が新しく開いても答えは変わらない（別の対象の呼び出しが開いたファイルなど）
    limits = "src/shop/services/limits.py"
    rig.handle.documents.did_open(t11.RelPath(limits), (rig.root / limits).read_text(encoding="utf-8"), "python")
    assert not (await rig.rename(target, "charge_order", dry_run=True)).is_error
    assert _renames(rig) == 3
    # 名前を含む文書を新しく開いた（ほかのツールが別のプロジェクトのファイルを開かせた）・開いていた文書を閉じたら聞き直す
    rig.handle.documents.did_open(t11.RelPath(TESTS), (rig.root / TESTS).read_text(encoding="utf-8"), "python")
    assert not (await rig.rename(target, "charge_order", dry_run=True)).is_error
    assert _renames(rig) == 4
    rig.handle.documents.did_close(t11.RelPath(limits))
    assert not (await rig.rename(target, "charge_order", dry_run=True)).is_error
    assert _renames(rig) == 5


async def test_a_real_rename_always_asks_the_lsp(stub, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    version = {"v": 7}
    rig = _rig(stub, version, monkeypatch)
    target = {"urn": U(PAY, PROCESS_CHARGE)}
    before = snapshot(rig.root)
    assert not (await rig.rename(target, "charge_order", dry_run=True)).is_error
    r = await rig.rename(target, "charge_order", dry_run=False)
    assert not r.is_error, r.text
    assert _renames(rig) == 2  # 書き込む呼び出しは覚えた答えを使わない
    after = snapshot(rig.root)
    assert {p for p in after if after[p] != before[p]} == {PAY, ROUTES}


async def test_without_an_index_version_nothing_is_remembered(stub) -> None:  # noqa: F811
    rig = stub({"textDocument/prepareRename": PREPARE})
    rig.handle.client.script["textDocument/rename"] = rename_edit(rig.root)
    rig.handle.started_at = "2026-10-31T00:00:00Z"
    target = {"urn": U(PAY, PROCESS_CHARGE)}
    for _ in range(2):
        assert not (await rig.rename(target, "charge_order", dry_run=True)).is_error
    assert _renames(rig) == 2


def test_data_version_reads_the_runtime_of_the_workspace() -> None:
    ws = SimpleNamespace(workspace_id="w1")
    asked: list[Any] = []

    def runtime(workspace_id: Any = None) -> Any:
        asked.append(workspace_id)
        return SimpleNamespace(data_version=lambda: 42)

    assert t11._data_version(SimpleNamespace(runtime=runtime), ws) == 42 and asked == ["w1"]  # type: ignore[arg-type]
    assert t11._data_version(SimpleNamespace(runtime=lambda workspace_id=None: None), ws) is None  # type: ignore[arg-type]
    assert t11._data_version(SimpleNamespace(), ws) is None  # type: ignore[arg-type]
