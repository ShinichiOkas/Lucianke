"""T-11 の ``_symbols_of`` は、抽出の覚え（``symbols.remembered``）に当たる内容を解析し直さない（BUG-0128）。

``extract_detailed`` は同じ内容の結果を覚えているが、解析した木を受け取るので、以前は覚えから返すときも呼ぶ側が先に解析していた
（T-11 は書き換える前の内容を呼び出しのたびに解析し直し、大きなファイルでは 1 回で数十 ms かかっていた）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lucianke.core.model import RelPath
from lucianke.syntax.support import get_support
from lucianke.syntax.symbols import load_query_source, remembered
from lucianke.tools import t11_rename_symbol as t11
from lucianke.workspace.types import Workspace

SOURCE = b"class A:\n    def run(self):\n        return 1\n\n\ndef helper():\n    return A().run()\n"


def make_ws(tmp_path: Path) -> Workspace:
    return Workspace(workspace_id="w", root=Path(os.path.realpath(tmp_path)), case_insensitive=os.name == "nt")


def test_a_remembered_content_is_not_parsed_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = make_ws(tmp_path)
    rel = RelPath("pkg/m.py")
    first = t11._symbols_of(ws, rel, SOURCE)
    assert first is not None and [s.qualified_name for s in first] == ["A", "A.run", "helper"]
    spec = get_support("python")
    assert spec is not None
    hit = remembered(
        SOURCE, language_id="python", language=spec.grammar(), query_source=load_query_source("python"),  # pyright: ignore[reportAttributeAccessIssue]
        workspace_id="w", rel=rel,
    )  # fmt: skip
    assert hit is not None

    def no_parse(*_a: object, **_k: object) -> None:
        raise AssertionError("parsed a remembered content again")

    monkeypatch.setattr(t11, "parse_source", no_parse)
    again = t11._symbols_of(ws, rel, SOURCE)
    assert again is not None and [str(s.urn) for s in again] == [str(s.urn) for s in first]


def test_a_new_content_or_a_content_with_errors_is_parsed(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    rel = RelPath("pkg/n.py")
    renamed = SOURCE.replace(b"helper", b"helper2")
    out = t11._symbols_of(ws, rel, renamed)
    assert out is not None and [s.qualified_name for s in out] == ["A", "A.run", "helper2"]
    broken = b"def f(:\n    pass\n"
    spec = get_support("python")
    assert spec is not None
    assert t11._symbols_of(ws, rel, broken) is not None
    assert (
        remembered(
            broken, language_id="python", language=spec.grammar(), query_source=load_query_source("python"),  # pyright: ignore[reportAttributeAccessIssue]
            workspace_id="w", rel=rel,
        )  # fmt: skip
        is None
    )  # 構文エラーのある木の結果は覚えない（毎回解析する）
