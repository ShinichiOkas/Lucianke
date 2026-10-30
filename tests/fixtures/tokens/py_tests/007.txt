"""core.compact_lines（Compact DSL の行の組み立て関数。mcp と syntax が共有する）のテスト。

文法の網羅は ``test_mcp_render.py`` が受け持つ。ここは「移した先から呼べること」「再公開が同一の関数であること」
「代表例」だけを確かめる。
"""

from __future__ import annotations

import pytest

from lucianke.core import compact_lines as cl
from lucianke.core.model import Kind
from lucianke.mcp.render import compact
from lucianke.syntax import skeleton


def test_compact_reexports_the_same_functions() -> None:
    names = [n for n in compact.__all__ if n != "render"]
    assert names
    for name in names:
        assert getattr(compact, name) is getattr(cl, name), name


def test_skeleton_uses_the_same_functions() -> None:
    for name in ("attr_line", "file_block_header", "import_line", "note", "symbol_line"):
        assert getattr(skeleton, name) is getattr(cl, name), name


def test_representative_lines() -> None:
    assert cl.file_block_header("a/b.py", lines=245, skeleton_lines=21, sha12="3F9C1A2B4C5D00") == (
        "=== a/b.py (245 lines, skeleton: 21 lines, sha=3f9c1a2b4c5d) ==="
    )
    assert cl.import_line(["os", "A from b"]) == "imports: [os, A from b]"
    assert (
        cl.symbol_line(
            Kind.METHOD,
            "m",
            indent=1,
            modifiers=["pub", "async"],
            sig="(self, x: int) -> Receipt",
            start_line=40,
            end_line=115,
            fold_lines=75,
        )
        == "  [Method] pub async m(self, x: int) -> Receipt @L40-115 { ...75 lines omitted }"
    )
    assert cl.note("truncated:\n3 members omitted") == "- truncated: 3 members omitted"
    assert cl.table([["a", None], ["x|y", 1]]) == ["| a | NULL |", r"| x\|y | 1 |"]


def test_vocabulary_and_position_checks() -> None:
    assert "sha" in cl.ATTR_KEYS
    with pytest.raises(ValueError):
        cl.attr_line("nope", "x", indent=0)
    with pytest.raises(ValueError):
        cl.symbol_line(Kind.FUNCTION, "f", start_line=0)  # 行は 1 始まり
    with pytest.raises(ValueError):
        cl.symbol_line("Bogus", "f", start_line=1)


def test_long_doc_is_cut_to_the_line_limit() -> None:
    line = cl.attr_line("doc", "あ" * 300, indent=2)
    assert len(line) == cl.LINE_LIMIT
    assert line.endswith("…")
