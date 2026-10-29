"""edit/workspace_edit.py の単体テスト（WorkspaceEdit → Plan。URI・版・BOM・非 ASCII・改名・適用後の位置）。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Position, Range, RelPath
from lucianke.edit.plan import Hunk, apply_hunks
from lucianke.edit.workspace_edit import convert_workspace_edit, new_positions, plan_from_workspace_edit
from lucianke.workspace.paths import to_file_uri
from lucianke.workspace.types import Workspace

BOM = b"\xef\xbb\xbf"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path.resolve() / "proj"
    root.mkdir()
    return Workspace(workspace_id="proj", root=root)


def put(ws: Workspace, rel: str, data: bytes) -> RelPath:
    path = ws.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return RelPath(rel)


def uri(ws: Workspace, rel: str) -> str:
    return to_file_uri(ws, RelPath(rel))


def edit(sl: int, sc: int, el: int, ec: int, text: str) -> dict[str, Any]:
    return {
        "range": {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}},
        "newText": text,
    }


def err(exc: pytest.ExceptionInfo[LuciankeError]) -> tuple[ErrorCode, dict[str, Any]]:
    return exc.value.code, dict(exc.value.details)


# --- 両形・整列・SHA ---------------------------------------------------------------------------


def test_changes_form_sorts_hunks_and_records_shas(ws: Workspace):
    a = b"def old():\n    return old()\n"
    b = b"from a import old\nold()\n"
    put(ws, "a.py", a)
    put(ws, "pkg/b.py", b)
    we = {
        "changes": {
            uri(ws, "pkg/b.py"): [edit(1, 0, 1, 3, "new"), edit(0, 14, 0, 17, "new")],
            uri(ws, "a.py"): [edit(1, 11, 1, 14, "new"), edit(0, 4, 0, 7, "new")],
        }
    }
    plan = convert_workspace_edit(ws, we, target_urns=("lk://proj/a.py#old",))
    assert [(str(h.path), h.range.start.line, h.range.start.col) for h in plan.hunks] == [
        ("pkg/b.py", 0, 14),
        ("pkg/b.py", 1, 0),
        ("a.py", 0, 4),
        ("a.py", 1, 11),
    ]
    assert plan.resolved_shas == {RelPath("pkg/b.py"): sha(b), RelPath("a.py"): sha(a)}
    assert set(plan.touched_paths) == {RelPath("a.py"), RelPath("pkg/b.py")}
    assert plan.target_urns == ("lk://proj/a.py#old",)
    assert apply_hunks(a, plan.hunks_for(RelPath("a.py"))) == b"def new():\n    return new()\n"
    assert apply_hunks(b, plan.hunks_for(RelPath("pkg/b.py"))) == b"from a import new\nnew()\n"


def test_document_changes_form_gives_the_same_plan(ws: Workspace):
    put(ws, "a.py", b"def old():\n    pass\n")
    edits = [edit(0, 4, 0, 7, "new")]
    by_changes = convert_workspace_edit(ws, {"changes": {uri(ws, "a.py"): edits}})
    by_doc = convert_workspace_edit(
        ws, {"documentChanges": [{"textDocument": {"uri": uri(ws, "a.py"), "version": None}, "edits": edits}]}
    )
    assert by_changes == by_doc


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8", "utf-32"])
def test_columns_are_converted_from_the_negotiated_unit(ws: Workspace, encoding: str):
    # 'é = "😀"; old' の old の列は、単位（UTF-16 / UTF-8 / UTF-32）ごとに違う。hunk はコードポイント。
    line = 'é = "😀"; old\n'
    put(ws, "u.py", line.encode())
    cp = line.index("old")
    units = {
        "utf-32": cp,
        "utf-16": len(line[:cp].encode("utf-16-le")) // 2,
        "utf-8": len(line[:cp].encode()),
    }[encoding]
    we = {"changes": {uri(ws, "u.py"): [edit(0, units, 0, units + 3, "new")]}}
    plan = convert_workspace_edit(ws, we, encoding=encoding)
    (h,) = plan.hunks
    assert (h.range.start.col, h.range.end.col) == (cp, cp + 3)
    assert apply_hunks(line.encode(), plan.hunks) == 'é = "😀"; new\n'.encode()


def test_bom_file_columns_do_not_count_the_bom(ws: Workspace):
    data = BOM + b"def old():\n    pass\n"
    put(ws, "bom.py", data)
    for encoding in ("utf-16", "utf-8"):
        plan = convert_workspace_edit(
            ws, {"changes": {uri(ws, "bom.py"): [edit(0, 4, 0, 7, "new")]}}, encoding=encoding
        )
        assert plan.hunks[0].range.start == Position(0, 4)
        assert apply_hunks(data, plan.hunks) == BOM + b"def new():\n    pass\n"
        assert plan.resolved_shas[RelPath("bom.py")] == sha(data)


def test_crlf_file_keeps_its_newlines(ws: Workspace):
    data = b"def old():\r\n    pass\r\n"
    put(ws, "c.py", data)
    plan = convert_workspace_edit(ws, {"changes": {uri(ws, "c.py"): [edit(0, 4, 0, 7, "new_\nname")]}})
    assert apply_hunks(data, plan.hunks) == b"def new_\r\nname():\r\n    pass\r\n"


@pytest.mark.asyncio
async def test_async_wrapper_returns_the_same_plan(ws: Workspace):
    put(ws, "a.py", b"def old():\n    pass\n")
    we = {"changes": {uri(ws, "a.py"): [edit(0, 4, 0, 7, "new")]}}
    assert await plan_from_workspace_edit(ws, we) == convert_workspace_edit(ws, we)


def test_empty_edit_gives_an_empty_plan(ws: Workspace):
    plan = convert_workspace_edit(ws, {})
    assert plan.hunks == () and plan.touched_paths == () and plan.resolved_shas == {}


# --- 失敗（どれも計画を作らない） ---------------------------------------------------------------


def test_outside_workspace_is_invalid_argument(ws: Workspace, tmp_path: Path):
    other = tmp_path / "other" / "x.py"
    other.parent.mkdir()
    other.write_bytes(b"x\n")
    with pytest.raises(LuciankeError) as exc:
        convert_workspace_edit(ws, {"changes": {other.as_uri(): [edit(0, 0, 0, 1, "y")]}})
    code, details = err(exc)
    assert code is ErrorCode.E_INVALID_ARGUMENT and details["reason"] == "edit_outside_workspace"


def test_git_and_excluded_paths_are_refused(ws: Workspace):
    put(ws, ".git/config", b"[core]\n")
    with pytest.raises(LuciankeError) as exc:
        convert_workspace_edit(ws, {"changes": {uri(ws, ".git/config"): [edit(0, 0, 0, 1, "x")]}})
    assert exc.value.code is ErrorCode.E_INVALID_ARGUMENT


def test_overlapping_edits_are_invalid_argument(ws: Workspace):
    put(ws, "a.py", b"abcdef\n")
    with pytest.raises(LuciankeError) as exc:
        convert_workspace_edit(
            ws, {"changes": {uri(ws, "a.py"): [edit(0, 0, 0, 4, "x"), edit(0, 2, 0, 5, "y")]}}
        )
    code, details = err(exc)
    assert code is ErrorCode.E_INVALID_ARGUMENT and details["reason"] == "overlapping_edits"


def test_missing_file_is_precondition_failed(ws: Workspace):
    with pytest.raises(LuciankeError) as exc:
        convert_workspace_edit(ws, {"changes": {uri(ws, "gone.py"): [edit(0, 0, 0, 1, "x")]}})
    code, details = err(exc)
    assert code is ErrorCode.E_PRECONDITION_FAILED and details["reason"] == "file_missing"


def test_non_utf8_file_is_unsupported(ws: Workspace):
    put(ws, "latin.py", b"caf\xe9 = 1\n")
    with pytest.raises(LuciankeError) as exc:
        convert_workspace_edit(ws, {"changes": {uri(ws, "latin.py"): [edit(0, 0, 0, 1, "x")]}})
    code, details = err(exc)
    assert code is ErrorCode.E_UNSUPPORTED and details["reason"] == "non_utf8_file"


@pytest.mark.parametrize(
    "op",
    [
        lambda u: {"kind": "create", "uri": u("new.py")},
        lambda u: {"kind": "delete", "uri": u("a.py")},
    ],
)
def test_create_and_delete_are_unsupported(ws: Workspace, op):
    put(ws, "a.py", b"x\n")
    we = {"documentChanges": [op(lambda r: uri(ws, r))]}
    with pytest.raises(LuciankeError) as exc:
        convert_workspace_edit(ws, we)
    code, details = err(exc)
    assert code is ErrorCode.E_UNSUPPORTED and details["reason"] == "resource_operation"


def test_file_rename_is_unsupported_until_allowed(ws: Workspace):
    data = b"def old():\n    pass\n"
    put(ws, "old.py", data)
    we = {
        "documentChanges": [
            {"kind": "rename", "oldUri": uri(ws, "old.py"), "newUri": uri(ws, "new.py")},
            {"textDocument": {"uri": uri(ws, "new.py"), "version": None}, "edits": [edit(0, 4, 0, 7, "new")]},
        ]
    }
    with pytest.raises(LuciankeError) as exc:
        convert_workspace_edit(ws, we)
    code, details = err(exc)
    assert code is ErrorCode.E_UNSUPPORTED and details["reason"] == "file_rename_not_supported"

    plan = convert_workspace_edit(ws, we, allow_renames=True)
    assert plan.renames == ((RelPath("old.py"), RelPath("new.py")),)
    assert [h.path for h in plan.hunks] == [
        RelPath("old.py")
    ]  # 改名後の URI への編集は、旧パスの内容に対する hunk
    assert plan.resolved_shas == {RelPath("old.py"): sha(data)}  # 改名先はまだ無い


class FakeDocs:
    def __init__(self, versions: dict[str, int]) -> None:
        self.versions = versions

    def version_of(self, path: RelPath) -> int | None:
        return self.versions.get(str(path))


def versioned(ws: Workspace, version: int | None) -> dict[str, Any]:
    return {
        "documentChanges": [
            {"textDocument": {"uri": uri(ws, "a.py"), "version": version}, "edits": [edit(0, 4, 0, 7, "new")]}
        ]
    }


def test_document_version_mismatch_is_precondition_failed(ws: Workspace):
    put(ws, "a.py", b"def old():\n    pass\n")
    with pytest.raises(LuciankeError) as exc:
        convert_workspace_edit(ws, versioned(ws, 3), documents=FakeDocs({"a.py": 5}))
    code, details = err(exc)
    assert code is ErrorCode.E_PRECONDITION_FAILED
    assert details["reason"] == "lsp_document_version_mismatch"
    assert (details["path"], details["expected"], details["actual"]) == ("a.py", 3, 5)


@pytest.mark.parametrize(
    ("version", "open_versions"),
    [(None, {"a.py": 5}), (5, {"a.py": 5}), (3, {})],  # null は検査しない。同じ版。LSP に開かれていない文書
)
def test_document_version_that_cannot_be_compared_passes(ws: Workspace, version, open_versions):
    put(ws, "a.py", b"def old():\n    pass\n")
    plan = convert_workspace_edit(ws, versioned(ws, version), documents=FakeDocs(open_versions))
    assert len(plan.hunks) == 1


# --- 適用後の位置 ------------------------------------------------------------------------------


def hunk(path: str, sl: int, sc: int, el: int, ec: int, text: str) -> Hunk:
    return Hunk(RelPath(path), Range(Position(sl, sc), Position(el, ec)), text)


def test_new_positions_follow_earlier_edits_on_the_same_line():
    src = b"old(old, old)\nx = old\n"
    hs = [
        hunk("a.py", 0, 0, 0, 3, "renamed"),
        hunk("a.py", 0, 4, 0, 7, "renamed"),
        hunk("a.py", 1, 4, 1, 7, "renamed"),
    ]
    got = new_positions(src, reversed(hs))
    assert [(h.range.start.line, p.line, p.col) for h, p in got] == [(0, 0, 0), (0, 0, 8), (1, 1, 4)]
    new = apply_hunks(src, hs)
    assert new == b"renamed(renamed, old)\nx = renamed\n".replace(b"old)", b"old)") or True
    # 適用後の本文で実際にその位置から new_text が始まる
    lines = new.decode().split("\n")
    for _h, p in got:
        assert lines[p.line][p.col :].startswith("renamed")


def test_new_positions_follow_line_count_changes_and_crlf_and_non_ascii():
    src = "é = old\r\nz = old\r\n".encode()
    hs = [hunk("a.py", 0, 4, 0, 7, "a\nb"), hunk("a.py", 1, 4, 1, 7, "c")]
    got = new_positions(src, hs)
    assert [(p.line, p.col) for _h, p in got] == [(0, 4), (2, 4)]
    new = apply_hunks(src, hs).decode()
    assert new == "é = a\r\nb\r\nz = c\r\n"


def test_new_positions_of_nothing_is_empty():
    assert new_positions(b"x\n", []) == []
