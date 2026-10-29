"""edit/plan.py の単体テスト（Hunk・Plan・apply_hunks・WorkspaceEdit の変換・統一 diff）。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Position, Range, RelPath
from lucianke.core.textpos import LineIndex, strip_bom
from lucianke.edit.plan import Hunk, Plan, apply_hunks, check_no_overlap, from_workspace_edit, unified_diff

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CRLF_BOM = ROOT / "tests" / "fixtures" / "poly" / "src" / "shop" / "models" / "crlf_bom.py"
FIXTURE_NO_FINAL_NL = ROOT / "tests" / "fixtures" / "poly" / "src" / "shop" / "models" / "no_final_newline.py"
PY_SHAPES = RelPath("spikes/s04_lsp/fixture_py/shapes.py")
PY_MESSY = RelPath("spikes/s04_lsp/fixture_py/messy.py")
TS_SHAPES = RelPath("spikes/s05_ts/fixture_ts/shapes.ts")
TS_UNI = RelPath("spikes/s05_ts/fixture_ts/uni.ts")
OUTSIDE_URI = "file:///elsewhere/other.py"


def R(sl: int, sc: int, el: int, ec: int) -> Range:
    return Range(Position(sl, sc), Position(el, ec))


def H(path: str, rng: Range, text: str) -> Hunk:
    return Hunk(RelPath(path), rng, text)


def pos_of(text: str, needle: str, nth: int = 0) -> tuple[int, int]:
    """``text`` の中の ``needle`` の ``nth`` 番目の位置（0 始まりの行・コードポイント列）。独立した検算用。"""
    at = -1
    for _ in range(nth + 1):
        at = text.index(needle, at + 1)
    line = text.count("\n", 0, at)
    return line, at - (text.rfind("\n", 0, at) + 1)


# --- Hunk / Plan -------------------------------------------------------------------------------


def test_plan_accepts_lists_and_normalizes_touched_paths():
    h = H("a.py", R(0, 0, 0, 1), "x")
    plan = Plan(
        hunks=[h],  # type: ignore[arg-type]
        renames=[("old.py", "new.py")],  # type: ignore[list-item]
        target_urns=["lk://x"],  # type: ignore[arg-type]
        resolved_shas={RelPath("a.py"): "ab"},
    )
    assert plan.hunks == (h,)
    assert plan.renames == (("old.py", "new.py"),)
    assert plan.touched_paths == ("a.py", "old.py", "new.py")
    assert plan.target_urns == ("lk://x",)
    assert plan.resolved_shas == {"a.py": "ab"}
    assert plan.hunk_paths == ("a.py",)
    assert plan.hunks_for(RelPath("a.py")) == [h]
    assert plan.expects_syntax is True


def test_plan_is_frozen_and_replaceable():
    plan = Plan(hunks=(H("a.py", R(0, 0, 0, 0), "x"),))
    with pytest.raises(AttributeError):
        plan.hunks = ()  # type: ignore[misc]
    filled = replace(plan, resolved_shas={RelPath("a.py"): "ff"}, target_urns=("lk://t",))
    assert filled.resolved_shas == {"a.py": "ff"}
    assert filled.touched_paths == ("a.py",)


# --- apply_hunks -------------------------------------------------------------------------------


def test_apply_no_hunks_returns_source():
    assert apply_hunks(b"abc", []) == b"abc"


def test_apply_single_replace_insert_and_delete():
    src = b"abc\ndef\n"
    assert apply_hunks(src, [H("p", R(0, 1, 0, 2), "XYZ")]) == b"aXYZc\ndef\n"
    assert apply_hunks(src, [H("p", R(1, 0, 1, 0), "new\n")]) == b"abc\nnew\ndef\n"
    assert apply_hunks(src, [H("p", R(0, 1, 1, 1), "")]) == b"aef\n"


def test_apply_multiple_hunks_in_any_order_use_pre_edit_coordinates():
    src = b"line0\nline1\nline2\nline3\n"
    hunks = [
        H("p", R(3, 0, 3, 5), "L3"),
        H("p", R(0, 0, 0, 5), "L0!!"),
        H("p", R(1, 4, 2, 1), "-"),
    ]
    expected = b"L0!!\nline-ine2\nL3\n"
    assert apply_hunks(src, hunks) == expected
    assert apply_hunks(src, list(reversed(hunks))) == expected


def test_apply_adjacent_hunks_are_not_overlapping():
    assert apply_hunks(b"abcd", [H("p", R(0, 0, 0, 2), "AB"), H("p", R(0, 2, 0, 4), "CD")]) == b"ABCD"


def test_apply_inserts_at_the_same_position_keep_given_order():
    src = b"ab"
    out = apply_hunks(
        src, [H("p", R(0, 1, 0, 1), "1"), H("p", R(0, 1, 0, 1), "2"), H("p", R(0, 1, 0, 1), "3")]
    )
    assert out == b"a123b"


def test_apply_insert_next_to_a_replace_is_fine():
    src = b"abcd"
    assert (
        apply_hunks(src, [H("p", R(0, 1, 0, 3), "X"), H("p", R(0, 1, 0, 1), "<"), H("p", R(0, 3, 0, 3), ">")])
        == b"a<X>d"
    )


@pytest.mark.parametrize(
    "other",
    [
        R(0, 2, 0, 5),  # 一部が重なる
        R(0, 3, 0, 4),  # 含まれる
        R(0, 1, 0, 6),  # 含む
        R(0, 2, 0, 6),  # 同じ
        R(0, 4, 0, 4),  # 範囲の内側への挿入
    ],
)
def test_apply_overlapping_hunks_are_rejected(other):
    src = b"0123456789"
    with pytest.raises(LuciankeError) as ei:
        apply_hunks(src, [H("p", R(0, 2, 0, 6), "X"), H("p", other, "Y")])
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details["reason"] == "overlapping_edits"
    assert ei.value.details["path"] == "p"


def test_check_no_overlap_is_per_file():
    check_no_overlap([H("a", R(0, 0, 0, 3), "x"), H("b", R(0, 1, 0, 2), "y")])
    with pytest.raises(LuciankeError) as ei:
        check_no_overlap([H("a", R(0, 0, 0, 3), "x"), H("a", R(0, 1, 0, 2), "y")])
    assert ei.value.details["reason"] == "overlapping_edits"


def test_apply_rejects_hunks_of_several_files():
    with pytest.raises(ValueError):
        apply_hunks(b"a", [H("a", R(0, 0, 0, 0), "x"), H("b", R(0, 0, 0, 0), "y")])


def test_apply_non_ascii_and_emoji_lines_use_codepoint_columns():
    text = "x = '😀日本語'; y = 1\nz = 'é' + name\n"
    src = text.encode()
    line, col = pos_of(text, "y = 1")
    out = apply_hunks(src, [H("p", R(line, col, line, col + 1), "yy")])
    assert out.decode() == text.replace("y = 1", "yy = 1")
    line2, col2 = pos_of(text, "name")
    out2 = apply_hunks(src, [H("p", R(line2, col2, line2, col2 + 4), "名前😀")])
    assert out2.decode() == text.replace("name", "名前😀")
    # 両方
    out3 = apply_hunks(
        src, [H("p", R(line2, col2, line2, col2 + 4), "N"), H("p", R(line, col, line, col + 1), "Y")]
    )
    assert out3.decode() == text.replace("y = 1", "Y = 1").replace("name", "N")


def test_apply_column_past_line_end_is_clamped_and_line_after_last_is_end_of_file():
    assert apply_hunks(b"ab\ncd", [H("p", R(0, 99, 0, 99), "!")]) == b"ab!\ncd"
    assert apply_hunks(b"ab\ncd", [H("p", R(2, 0, 2, 0), "!")]) == b"ab\ncd!"  # 最終行の次 = 文書末
    with pytest.raises(LuciankeError) as ei:
        apply_hunks(b"ab\ncd", [H("p", R(5, 0, 5, 0), "!")])
    assert ei.value.details["reason"] == "hunk_out_of_range"


def test_apply_new_text_newlines_follow_the_file():
    crlf = b"a\r\nb\r\n"
    assert apply_hunks(crlf, [H("p", R(0, 1, 0, 1), "1\n2\r\n3")]) == b"a1\r\n2\r\n3\r\nb\r\n"
    lf = b"a\nb\n"
    assert apply_hunks(lf, [H("p", R(0, 1, 0, 1), "1\r\n2\r3")]) == b"a1\n2\n3\nb\n"
    cr = b"a\rb\r"
    assert apply_hunks(cr, [H("p", R(0, 1, 0, 1), "1\n2")]) == b"a1\r2\rb\r"


def test_apply_on_bom_crlf_fixture_keeps_bom_and_crlf():
    src = FIXTURE_CRLF_BOM.read_bytes()
    assert src.startswith(b"\xef\xbb\xbf") and b"\r\n" in src
    text = src[3:].decode()
    line, col = pos_of(text, "crlf_function")
    renamed = apply_hunks(src, [H("p", R(line, col, line, col + len("crlf_function")), "renamed")])
    assert renamed == src.replace(b"crlf_function", b"renamed")
    assert renamed.startswith(b"\xef\xbb\xbf")
    # 先頭行（BOM の直後）への編集
    first = apply_hunks(src, [H("p", R(0, 0, 0, 3), "'''")])
    assert first == b"\xef\xbb\xbf'''" + src[3 + 3 :]
    # 複数行の new_text も CRLF になる（裸の LF が増えない）
    line, col = pos_of(text, "return x * 2")
    multi = apply_hunks(src, [H("p", R(line, col, line, col + len("return x * 2")), "y = x\n    return y")])
    assert multi.count(b"\n") == multi.count(b"\r\n")
    assert b"y = x\r\n    return y" in multi


def test_apply_identity_hunks_keep_the_bytes():
    for fixture in (FIXTURE_CRLF_BOM, FIXTURE_NO_FINAL_NL):
        src = fixture.read_bytes()
        idx = LineIndex(strip_bom(src))
        last = idx.line_count - 1
        whole = R(0, 0, last, idx.line_length(last))
        assert apply_hunks(src, [H("p", whole, strip_bom(src).decode())]) == src
        # 行ごとの恒等
        hunks = []
        for n in range(idx.line_count):
            hunks.append(H("p", R(n, 0, n, idx.line_length(n)), idx.line_text(n)))
        assert apply_hunks(src, hunks) == src
        # 空の挿入
        assert apply_hunks(src, [H("p", R(0, 0, 0, 0), "")]) == src


def test_apply_to_file_without_final_newline():
    src = FIXTURE_NO_FINAL_NL.read_bytes()
    assert not src.endswith((b"\n", b"\r"))
    out = apply_hunks(src, [H("p", R(0, 0, 0, 0), "# top\n")])
    assert out == b"# top\n" + src


# --- from_workspace_edit -----------------------------------------------------------------------

_URI_SUFFIXES = {
    "/spikes/s04_lsp/fixture_py/shapes.py": PY_SHAPES,
    "/spikes/s04_lsp/fixture_py/messy.py": PY_MESSY,
    "/spikes/s05_ts/fixture_ts/shapes.ts": TS_SHAPES,
    "/spikes/s05_ts/fixture_ts/uni.ts": TS_UNI,
    "/ws/renamed_shapes.py": RelPath("ws/renamed_shapes.py"),
}


def to_rel(uri: str) -> RelPath | None:
    for suffix, rel in _URI_SUFFIXES.items():
        if uri.endswith(suffix):
            return rel
    return None


def uri_of(rel: RelPath) -> str:
    return "file:///s%3A/work/develop/Lucianke" + next(k for k, v in _URI_SUFFIXES.items() if v == rel)


def index_for(rel: RelPath) -> LineIndex:
    return LineIndex(strip_bom((ROOT / rel).read_bytes()))


def convert(we: dict, encoding: str = "utf-16") -> Plan:
    return from_workspace_edit(we, to_rel=to_rel, line_index_for=index_for, encoding=encoding)


def text_at(rel: RelPath, rng: Range) -> str:
    idx = index_for(rel)
    assert rng.start.line == rng.end.line
    return idx.line_text(rng.start.line)[rng.start.col : rng.end.col]


# S-04（basedpyright）の rename の実測出力 spikes/s04_lsp/out/basedpyright.json の raw_sample をそのまま写したもの
S04_RENAME_RAW = {
    "documentChanges0": {
        "textDocument": {
            "uri": "file:///s%3A/work/develop/Lucianke/spikes/s04_lsp/fixture_py/shapes.py",
            "version": None,
        },
        "edits": [
            {
                "range": {"start": {"line": 24, "character": 4}, "end": {"line": 24, "character": 12}},
                "newText": "describe_shape",
            }
        ],
    }
}
# S-05（typescript-language-server / vtsls）の rename の実測出力 spikes/s05_ts/out/tsls.json の raw_sample
S05_RENAME_RAW = {
    "changes_key": "shapes.ts",
    "edit0": {
        "newText": "describeShape",
        "range": {"start": {"line": 19, "character": 16}, "end": {"line": 19, "character": 24}},
    },
}
# S-04 の basedpyright.organizeimports の実測出力（extra_organize.json の result。複数行の範囲）
S04_ORGANIZE_RESULT = {
    "changes": {
        "file:///s%3A/work/develop/Lucianke/spikes/s04_lsp/fixture_py/messy.py": [
            {
                "range": {"start": {"line": 1, "character": 0}, "end": {"line": 4, "character": 11}},
                "newText": "import json\nimport os\nimport sys\nfrom typing import List",
            }
        ]
    }
}


def test_s04_document_changes_rename_hunk_lands_on_the_identifier():
    we = {"documentChanges": [S04_RENAME_RAW["documentChanges0"]]}
    plan = convert(we)
    assert plan.touched_paths == (PY_SHAPES,)
    assert plan.renames == ()
    assert len(plan.hunks) == 1
    h = plan.hunks[0]
    assert h.path == PY_SHAPES
    assert h.range == R(24, 4, 24, 12)
    assert h.new_text == "describe_shape"
    assert text_at(PY_SHAPES, h.range) == "describe"
    assert plan.resolved_shas == {} and plan.target_urns == ()


def test_s05_changes_rename_hunk_lands_on_the_identifier():
    we = {"changes": {uri_of(TS_SHAPES): [S05_RENAME_RAW["edit0"]]}}
    plan = convert(we)
    assert plan.touched_paths == (TS_SHAPES,)
    (h,) = plan.hunks
    assert h.range == R(19, 16, 19, 24)
    assert text_at(TS_SHAPES, h.range) == "describe"
    assert h.new_text == "describeShape"


def test_changes_and_document_changes_forms_give_the_same_plan():
    edit = S04_RENAME_RAW["documentChanges0"]["edits"][0]
    a = convert({"changes": {uri_of(PY_SHAPES): [edit]}})
    b = convert({"documentChanges": [S04_RENAME_RAW["documentChanges0"]]})
    assert a == b


def test_document_changes_wins_over_changes():
    edit = S04_RENAME_RAW["documentChanges0"]["edits"][0]
    other = {
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
        "newText": "Z",
    }
    we = {"changes": {uri_of(TS_SHAPES): [other]}, "documentChanges": [S04_RENAME_RAW["documentChanges0"]]}
    assert convert(we).touched_paths == (PY_SHAPES,)
    we2 = {"changes": {uri_of(TS_SHAPES): [other]}, "documentChanges": []}
    assert convert(we2).touched_paths == (TS_SHAPES,)
    assert edit  # 参照


def test_s04_organize_imports_multi_line_edit_applies():
    plan = convert(S04_ORGANIZE_RESULT)
    (h,) = plan.hunks
    assert h.range == R(1, 0, 4, 11)
    src = (ROOT / PY_MESSY).read_bytes()
    out = apply_hunks(src, plan.hunks).decode()
    nl = "\r\n" if b"\r\n" in src else "\n"
    lines = out.split(nl)
    assert lines[1:5] == ["import json", "import os", "import sys", "from typing import List"]
    assert lines[0] == src.decode().split(nl)[0]
    assert "import sys" not in lines[5:6]


def _spike_samples(name: str, key: str) -> list[dict]:
    path = ROOT / name
    if not path.exists():  # spikes/**/out/ は git 管理外。手元に実測出力があるときだけ走らせる
        pytest.skip(f"{name} not present")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [v[key] for v in _walk(data) if isinstance(v, dict) and key in v]


def test_live_s04_rename_output_converts_to_an_identifier_range():
    (raw,) = _spike_samples("spikes/s04_lsp/out/basedpyright.json", "documentChanges0")[:1]
    plan = convert({"documentChanges": [raw]})
    for h in plan.hunks:
        assert text_at(h.path, h.range).isidentifier()


def test_live_s05_rename_output_converts_to_an_identifier_range():
    (raw,) = _spike_samples("spikes/s05_ts/out/tsls.json", "edit0")[:1]
    plan = convert({"changes": {uri_of(TS_SHAPES): [raw]}})
    for h in plan.hunks:
        assert text_at(h.path, h.range).isidentifier()


def test_live_s04_organize_imports_output_converts_and_applies():
    results = [
        v["result"]
        for v in _spike_samples(
            "spikes/s04_lsp/out/extra_organize.json", "executeCommand basedpyright.organizeimports"
        )
        if isinstance(v, dict) and "result" in v
    ]
    plan = convert(results[0])
    src = (ROOT / PY_MESSY).read_bytes()
    assert apply_hunks(src, plan.hunks).decode().count("import ") >= 3


def _walk(obj):
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _edits_for_all(rel: RelPath, needle: str, new: str, unit: str) -> list[dict]:
    """``rel`` の ``needle`` をすべて ``new`` にする TextEdit。列は ``unit`` で数える（独立した検算）。"""
    text = strip_bom((ROOT / rel).read_bytes()).decode()
    lines = text.split("\n")
    edits = []
    for n, line in enumerate(lines):
        start = 0
        while (at := line.find(needle, start)) >= 0:

            def width(s: str) -> int:
                if unit == "utf-16":
                    return len(s.encode("utf-16-le")) // 2
                if unit == "utf-8":
                    return len(s.encode("utf-8"))
                return len(s)

            c0 = width(line[:at].rstrip("\r"))
            c1 = c0 + width(needle)
            edits.append(
                {
                    "range": {"start": {"line": n, "character": c0}, "end": {"line": n, "character": c1}},
                    "newText": new,
                }
            )
            start = at + len(needle)
    return edits


@pytest.mark.parametrize("unit", ["utf-16", "utf-8", "utf-32"])
def test_position_units_are_converted_to_codepoints(unit):
    edits = _edits_for_all(TS_UNI, "targetFn", "renamedFn", unit)
    assert len(edits) >= 5
    plan = convert({"changes": {uri_of(TS_UNI): edits}}, encoding=unit)
    src = (ROOT / TS_UNI).read_bytes()
    assert apply_hunks(src, plan.hunks) == src.replace(b"targetFn", b"renamedFn")
    for h in plan.hunks:
        assert text_at(TS_UNI, h.range) == "targetFn"


def test_utf16_columns_differ_from_codepoints_on_emoji_lines():
    # 絵文字 3 つ（UTF-16 で 6 単位、コードポイントで 3）より後ろの識別子
    text = strip_bom((ROOT / TS_UNI).read_bytes()).decode()
    line, cp = pos_of(text, "targetFn(1)")
    utf16 = len(text.split("\n")[line][:cp].encode("utf-16-le")) // 2
    assert utf16 > cp
    we = {
        "changes": {
            uri_of(TS_UNI): [
                {
                    "range": {
                        "start": {"line": line, "character": utf16},
                        "end": {"line": line, "character": utf16 + 8},
                    },
                    "newText": "x",
                }
            ]
        }
    }
    (h,) = convert(we, "utf-16").hunks
    assert h.range == R(line, cp, line, cp + 8)


def test_unknown_encoding_is_rejected():
    with pytest.raises(LuciankeError) as ei:
        convert({"changes": {}}, encoding="ebcdic")
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details["param"] == "encoding"


def test_rename_file_goes_to_renames_and_hunks_use_the_old_path():
    we = {
        "documentChanges": [
            {"textDocument": {"uri": uri_of(PY_MESSY), "version": None}, "edits": []},
            {
                "kind": "rename",
                "oldUri": uri_of(PY_SHAPES),
                "newUri": "file:///s%3A/work/develop/Lucianke/ws/renamed_shapes.py",
            },
            {
                "textDocument": {
                    "uri": "file:///s%3A/work/develop/Lucianke/ws/renamed_shapes.py",
                    "version": None,
                },
                "edits": S04_RENAME_RAW["documentChanges0"]["edits"],
            },
        ]
    }
    plan = convert(we)
    assert plan.renames == ((PY_SHAPES, RelPath("ws/renamed_shapes.py")),)
    assert [h.path for h in plan.hunks] == [PY_SHAPES]
    assert plan.hunks[0].range == R(24, 4, 24, 12)
    assert set(plan.touched_paths) == {PY_SHAPES, RelPath("ws/renamed_shapes.py")}
    assert PY_MESSY not in plan.touched_paths  # 編集の無い TextDocumentEdit は計画に入れない


def test_rename_file_alone():
    we = {
        "documentChanges": [
            {"kind": "rename", "oldUri": uri_of(PY_SHAPES), "newUri": "file:///x/ws/renamed_shapes.py"},
        ]
    }
    plan = convert(we)
    assert plan.hunks == ()
    assert plan.renames == ((PY_SHAPES, RelPath("ws/renamed_shapes.py")),)
    assert plan.touched_paths == (PY_SHAPES, RelPath("ws/renamed_shapes.py"))


@pytest.mark.parametrize("kind", ["create", "delete"])
def test_create_and_delete_are_unsupported(kind):
    we = {"documentChanges": [{"kind": kind, "uri": uri_of(PY_SHAPES)}]}
    with pytest.raises(LuciankeError) as ei:
        convert(we)
    assert ei.value.code is ErrorCode.E_UNSUPPORTED
    assert ei.value.details["operation"] == kind


def test_create_after_valid_edits_still_fails_whole_edit():
    we = {"documentChanges": [S04_RENAME_RAW["documentChanges0"], {"kind": "create", "uri": OUTSIDE_URI}]}
    with pytest.raises(LuciankeError) as ei:
        convert(we)
    assert ei.value.code is ErrorCode.E_UNSUPPORTED


@pytest.mark.parametrize(
    "we",
    [
        {
            "changes": {
                OUTSIDE_URI: [
                    {
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                        "newText": "x",
                    }
                ]
            }
        },
        {"documentChanges": [{"textDocument": {"uri": OUTSIDE_URI, "version": 1}, "edits": []}]},
        {"documentChanges": [{"kind": "rename", "oldUri": uri_of(PY_SHAPES), "newUri": OUTSIDE_URI}]},
    ],
)
def test_edit_outside_workspace_is_rejected(we):
    with pytest.raises(LuciankeError) as ei:
        convert(we)
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details["reason"] == "edit_outside_workspace"
    assert ei.value.details["uri"] == OUTSIDE_URI


def test_new_text_newlines_are_normalized_to_lf():
    edit = {
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
        "newText": "a\r\nb\rc\n",
    }
    (h,) = convert({"changes": {uri_of(PY_SHAPES): [edit]}}).hunks
    assert h.new_text == "a\nb\nc\n"


def test_positions_past_the_end_of_the_document_are_clamped_to_the_end():
    idx = index_for(PY_MESSY)
    last = idx.line_count - 1
    edit = {
        "range": {"start": {"line": 1, "character": 0}, "end": {"line": 10_000, "character": 0}},
        "newText": "",
    }
    (h,) = convert({"changes": {uri_of(PY_MESSY): [edit]}}).hunks
    assert h.range == R(1, 0, last, idx.line_length(last))


def test_hunks_are_sorted_per_file_and_files_keep_first_seen_order():
    def e(line: int, c0: int, c1: int, text: str) -> dict:
        return {
            "range": {"start": {"line": line, "character": c0}, "end": {"line": line, "character": c1}},
            "newText": text,
        }

    we = {
        "changes": {
            uri_of(TS_SHAPES): [e(19, 16, 24, "b"), e(2, 0, 1, "a")],
            uri_of(PY_SHAPES): [e(3, 0, 1, "c")],
        }
    }
    plan = convert(we)
    assert [(h.path, h.range.start.line) for h in plan.hunks] == [
        (TS_SHAPES, 2),
        (TS_SHAPES, 19),
        (PY_SHAPES, 3),
    ]
    assert plan.touched_paths == (TS_SHAPES, PY_SHAPES)


def test_overlapping_text_edits_are_rejected():
    def e(c0: int, c1: int) -> dict:
        return {
            "range": {"start": {"line": 0, "character": c0}, "end": {"line": 0, "character": c1}},
            "newText": "x",
        }

    with pytest.raises(LuciankeError) as ei:
        convert({"changes": {uri_of(PY_SHAPES): [e(0, 5), e(3, 8)]}})
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details["reason"] == "overlapping_edits"
    convert({"changes": {uri_of(PY_SHAPES): [e(0, 3), e(3, 8)]}})  # 隣り合いは可


def test_same_document_in_two_text_document_edits_is_unsupported():
    doc = S04_RENAME_RAW["documentChanges0"]
    with pytest.raises(LuciankeError) as ei:
        convert({"documentChanges": [doc, doc]})
    assert ei.value.code is ErrorCode.E_UNSUPPORTED
    assert ei.value.details["reason"] == "repeated_document_edit"


def test_annotated_text_edit_is_accepted_and_snippet_is_not():
    edit = dict(S04_RENAME_RAW["documentChanges0"]["edits"][0], annotationId="a1")
    doc = {"textDocument": {"uri": uri_of(PY_SHAPES), "version": None}, "edits": [edit]}
    (h,) = convert({"documentChanges": [doc]}).hunks
    assert h.range == R(24, 4, 24, 12)
    snippet = dict(edit, snippet={"kind": "snippet", "value": "$0"})
    with pytest.raises(LuciankeError) as ei:
        convert({"documentChanges": [dict(doc, edits=[snippet])]})
    assert ei.value.code is ErrorCode.E_UNSUPPORTED


@pytest.mark.parametrize(
    "we",
    [
        {"documentChanges": [{"foo": 1}]},
        {
            "documentChanges": [
                {
                    "textDocument": {"uri": "file:///spikes/s04_lsp/fixture_py/shapes.py"},
                    "edits": [{"newText": "x"}],
                }
            ]
        },
        {
            "changes": {
                "file:///spikes/s04_lsp/fixture_py/shapes.py": [
                    {"range": {"start": {"line": 0}}, "newText": "x"}
                ]
            }
        },
    ],
)
def test_malformed_workspace_edit_is_an_internal_error(we):
    with pytest.raises(LuciankeError) as ei:
        convert(we)
    assert ei.value.code is ErrorCode.E_INTERNAL


def test_empty_workspace_edit_gives_an_empty_plan():
    plan = convert({})
    assert plan.hunks == () and plan.touched_paths == () and plan.renames == ()


def test_line_index_is_asked_once_per_file():
    calls: list[RelPath] = []

    def spy(rel: RelPath) -> LineIndex:
        calls.append(rel)
        return index_for(rel)

    edits = _edits_for_all(TS_UNI, "targetFn", "x", "utf-16")
    from_workspace_edit(
        {"changes": {uri_of(TS_UNI): edits}}, to_rel=to_rel, line_index_for=spy, encoding="utf-16"
    )
    assert calls == [TS_UNI]


# --- unified_diff ------------------------------------------------------------------------------


def test_diff_of_identical_bytes_is_empty():
    assert unified_diff("a.py", b"x\n", b"x\n") == ""


def test_diff_basic_shape():
    before = b"a\nb\nc\n"
    after = b"a\nB\nc\n"
    assert unified_diff("src/a.py", before, after) == (
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    )


def test_diff_marks_a_missing_final_newline():
    d = unified_diff("a.txt", b"a\nb", b"a\nb\n")
    assert "-b\n\\ No newline at end of file\n+b\n" in d
    d2 = unified_diff("a.txt", b"a\nb\n", b"a\nc")
    assert "+c\n\\ No newline at end of file\n" in d2


def test_diff_keeps_crlf_and_shows_newline_only_changes():
    d = unified_diff("a.txt", b"a\r\nb\r\n", b"a\r\nB\r\n")
    assert "-b\r\n+B\r\n" in d
    assert unified_diff("a.txt", b"a\r\n", b"a\n") != ""


def test_diff_ignores_bom_and_does_not_split_on_form_feed():
    d = unified_diff("a.py", b"\xef\xbb\xbfx\n\x0cy\n", b"\xef\xbb\xbfx\n\x0cz\n")
    assert "﻿" not in d
    assert "-\x0cy\n+\x0cz\n" in d


def test_diff_removed_line_that_looks_like_a_header_is_still_a_content_line():
    d = unified_diff("a.sql", b"-- note\nx\n", b"x\n")
    assert "--- note\n" in d.split("@@", 1)[1]


def test_diff_for_apply_result_on_crlf_bom_fixture():
    src = FIXTURE_CRLF_BOM.read_bytes()
    text = src[3:].decode()
    line, col = pos_of(text, "crlf_function")
    new = apply_hunks(src, [H("p", R(line, col, line, col + 13), "renamed")])
    d = unified_diff("p", src, new)
    assert "-def crlf_function(x: int) -> int:\r\n+def renamed(x: int) -> int:\r\n" in d
