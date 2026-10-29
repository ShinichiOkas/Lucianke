"""``syntax/adapters/ipynb.py``（仮想 Python ファイル・セル表・骨格・``cell:<n>``。仕様書 §8.6 B2・§11 T-02/T-05）の単体テスト。

フィクスチャ ``tests/fixtures/poly/notebooks/eda.ipynb``（出力に ``OUTPUT_SENTINEL_*`` を含む）と、その場で組む小さな Notebook を使う。
骨格の行の形は ``tests/contract/compact_grammar.py`` の検査器に通す。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import tree_sitter_python
from tree_sitter import Language, Parser

from lucianke.core.model import RelPath
from lucianke.core.textpos import LineIndex
from lucianke.syntax.adapters import ipynb
from lucianke.syntax.adapters.ipynb import ADAPTER, CellMap, select, skeleton, to_virtual_python
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import ParsedFile, SkeletonOptions

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "poly" / "notebooks" / "eda.ipynb"
REL = "notebooks/eda.ipynb"
SENTINELS = ("OUTPUT_SENTINEL_STREAM", "OUTPUT_SENTINEL_IMAGE", "OUTPUT_SENTINEL_RESULT")

_spec = importlib.util.spec_from_file_location(
    "lucianke_test_compact_grammar_ipynb", ROOT / "contract" / "compact_grammar.py"
)
assert _spec is not None and _spec.loader is not None
grammar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grammar)

OPTS = SkeletonOptions()


@pytest.fixture(scope="module")
def eda() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def eda_doc(eda: bytes) -> dict[str, Any]:
    return json.loads(eda.decode("utf-8"))


def nb(*cells: dict[str, Any]) -> bytes:
    return json.dumps({"cells": list(cells), "metadata": {}, "nbformat": 4, "nbformat_minor": 5}).encode(
        "utf-8"
    )


def code(src: str | list[str], count: int | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "execution_count": count,
        "metadata": {},
        "outputs": [],
        "source": src,
        **extra,
    }


def md(src: str | list[str]) -> dict[str, Any]:
    return {"cell_type": "markdown", "metadata": {}, "source": src}


def python_symbols(virtual: bytes):
    """仮想 Python ファイルを Tree-sitter で解析して SymbolRecord を返す（本番の呼び出し側と同じ使い方）。"""
    language = Language(tree_sitter_python.language())
    tree = Parser(language).parse(virtual)
    parsed = ParsedFile(
        language_id="python",
        sha256="0" * 64,
        source=virtual,
        tree=tree,
        line_index=LineIndex(virtual),
        has_errors=tree.root_node.has_error,
    )
    return extract(
        parsed,
        language_id="python",
        language=language,
        query_source=load_query_source("python"),
        workspace_id="poly",
        rel=RelPath(REL),
    )


def output_strings(value: Any) -> Iterator[str]:
    """``outputs`` 配列の中の文字列をすべて集める（JSON のキー名は含めない）。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from output_strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from output_strings(v)


def body(rec) -> str:
    return "\n".join(rec.lines)


def rendered(rec) -> str:
    return body(rec) + "\n"


# --- 仮想 Python ファイル ---------------------------------------------------------------


def test_virtual_file_cell_table(eda: bytes, eda_doc: dict[str, Any]) -> None:
    virtual, cmap = to_virtual_python(eda)
    assert isinstance(cmap, CellMap)
    assert len(cmap.cells) == 4  # Markdown も数える
    kinds = [c.kind for c in cmap.cells]
    assert kinds == ["markdown", "code", "code", "code"]
    assert [c.number for c in cmap.cells] == [1, 2, 3, 4]
    assert [c.json_index for c in cmap.cells] == [0, 1, 2, 3]
    assert [c.exec_count for c in cmap.cells] == [None, 3, 12, 13]
    md_cell, c2, c3, c4 = cmap.cells
    # 0 始まり・終了行を含む。セルの間は空行 1 つ。
    assert (c2.start_line, c2.end_line) == (0, 1)
    assert (c3.start_line, c3.end_line) == (3, 10)
    assert (c4.start_line, c4.end_line) == (12, 12)
    assert cmap.line_count == 13
    # Markdown は行を占めず、次のコードセルの開始行に挟まっている。
    assert (md_cell.start_line, md_cell.end_line) == (None, None)
    assert md_cell.anchor_line == 0
    assert c3.span_external == (4, 11)

    lines = virtual.decode("utf-8").split("\n")
    for cell, number in ((c2, 2), (c3, 3), (c4, 4)):
        assert cell.start_line is not None and cell.end_line is not None
        src = "".join(eda_doc["cells"][number - 1]["source"])
        assert "\n".join(lines[cell.start_line : cell.end_line + 1]) == src
    assert lines[2] == "" and lines[11] == ""  # セルの間の空行


def test_virtual_file_is_concatenation_of_code_sources(eda: bytes, eda_doc: dict[str, Any]) -> None:
    virtual, _ = to_virtual_python(eda)
    sources = ["".join(c["source"]) for c in eda_doc["cells"] if c["cell_type"] == "code"]
    assert virtual.decode("utf-8") == "\n".join(s + "\n" for s in sources)
    assert b"[Cell" not in virtual and "売上データ".encode() not in virtual  # Markdown は含めない


def test_virtual_file_has_no_output_strings(eda: bytes, eda_doc: dict[str, Any]) -> None:
    virtual, _ = to_virtual_python(eda)
    text = virtual.decode("utf-8")
    for sentinel in SENTINELS:
        assert sentinel not in text
    # 出力の文字列（短い "4" などは偶然一致するので 8 文字以上）が 1 つも現れない
    outs = [s for cell in eda_doc["cells"] for s in output_strings(cell.get("outputs", [])) if len(s) >= 8]
    assert any("OUTPUT_SENTINEL" in s for s in outs)  # 陽性対照: 集めた中にセンチネルがある
    assert len(outs) >= 3
    for s in outs:
        assert s not in text


def test_virtual_file_does_not_read_outputs_or_metadata() -> None:
    cell = code("x = 1", 1, outputs="not-a-list", metadata=None)
    virtual, cmap = to_virtual_python(nb(cell))
    assert virtual == b"x = 1\n"
    assert cmap.cells[0].exec_count == 1


def test_line_accounting_matches_line_index(eda: bytes) -> None:
    virtual, cmap = to_virtual_python(eda)
    assert LineIndex(virtual).content_line_count == cmap.line_count


def test_virtual_file_edge_cases() -> None:
    data = nb(
        code("a = 1\n", 1),  # 末尾の改行つき: 1 行
        md("# h"),
        code("", None),  # 空のセル: 空行 1 つ
        {"cell_type": "raw", "metadata": {}, "source": "raw text"},
        code(["b = 2\n", "c = 3\n", "\n"], 7),  # 配列の source・末尾の空行は残る
    )
    virtual, cmap = to_virtual_python(data)
    assert virtual == b"a = 1\n\n\n\nb = 2\nc = 3\n\n"
    spans = [(c.kind, c.start_line, c.end_line, c.anchor_line) for c in cmap.cells]
    assert spans == [
        ("code", 0, 0, 0),
        ("markdown", None, None, 2),
        ("code", 2, 2, 2),
        ("raw", None, None, 4),
        ("code", 4, 6, 4),
    ]
    assert cmap.line_count == 7
    assert LineIndex(virtual).content_line_count == 7


def test_no_code_cells_and_trailing_markdown() -> None:
    virtual, cmap = to_virtual_python(nb(md("# only")))
    assert virtual == b"" and cmap.line_count == 0
    assert cmap.cells[0].anchor_line == 0

    virtual, cmap = to_virtual_python(nb(code("x = 1\ny = 2"), md("# tail")))
    assert cmap.cells[1].anchor_line == 1  # 後ろにコードセルが無ければ最終行
    assert cmap.cell_at_line(1) is cmap.cells[0]
    assert cmap.cell_at_line(5) is None


def test_bom_and_str_source_are_accepted() -> None:
    data = b"\xef\xbb\xbf" + nb(code("x = 1", 2))
    virtual, cmap = to_virtual_python(data)
    assert virtual == b"x = 1\n" and cmap.cells[0].exec_count == 2


# --- 壊れた入力 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"{not json",
        b"",
        b"\xff\xfe\x00",
        b"[]",
        b'{"nbformat": 3, "worksheets": []}',  # cells が無い（nbformat 3）
        b'{"cells": {}}',
        b'{"cells": [1]}',
        b'{"cells": [{"cell_type": "code", "source": 5}]}',
        b'{"cells": [{"cell_type": "code", "source": [1]}]}',
        b'{"cells": [{"source": "x"}]}',
    ],
)
def test_broken_notebooks_raise_value_error(data: bytes) -> None:
    with pytest.raises(ValueError):
        to_virtual_python(data)
    with pytest.raises(ValueError):
        skeleton(data, OPTS, rel=REL, sha256="0" * 64)
    with pytest.raises(ValueError):
        select(data, "cell:1")


# --- 骨格 -------------------------------------------------------------------------------


def test_skeleton_shape_without_symbols(eda: bytes) -> None:
    sha = hashlib.sha256(eda).hexdigest()
    rec = skeleton(eda, OPTS, rel=REL, sha256=sha)
    assert rec.kind == "file-block" and rec.key == REL and rec.group == REL
    assert rec.lines[1:] == (
        "[Cell] 1 markdown @L1",
        "  doc: # 売上データの探索",
        "[Cell] 2 code (exec_count: 3) @L1-2",
        "[Cell] 3 code (exec_count: 12) @L4-11",
        "[Cell] 4 code (exec_count: 13) @L13",
    )
    assert rec.lines[0] == f"=== {REL} (13 lines, 4 cells, skeleton: 5 lines, sha={sha[:12]}) ==="
    assert (
        rec.data["skeleton_line_count"] == 5 and rec.data["cell_count"] == 4 and rec.data["line_count"] == 13
    )
    assert rec.data["file_sha256"] == sha and rec.data["language"] == "ipynb" and rec.data["tier"] == 2


def test_skeleton_lines_conform_to_compact_grammar(eda: bytes) -> None:
    sha = hashlib.sha256(eda).hexdigest()
    for symbols in (None, python_symbols):
        rec = skeleton(eda, OPTS, rel=REL, sha256=sha, python_symbols=symbols)
        assert grammar.validate(rendered(rec), require_header=False) == []
    # 陽性対照: 検査器は @L の無い [Cell] 行を落とす
    assert grammar.validate("[Cell] 1 markdown\n", require_header=False) != []


def test_skeleton_has_no_output_strings(eda: bytes, eda_doc: dict[str, Any]) -> None:
    """事後条件: ``.ipynb`` の応答に ``outputs`` の文字列が 1 つも現れない。"""
    sha = hashlib.sha256(eda).hexdigest()
    rec = skeleton(eda, OPTS, rel=REL, sha256=sha, python_symbols=python_symbols)
    text = body(rec) + json.dumps(rec.data, ensure_ascii=False)
    for sentinel in SENTINELS:
        assert sentinel not in text
    outs = [s for cell in eda_doc["cells"] for s in output_strings(cell.get("outputs", [])) if len(s) >= 8]
    assert len(outs) >= 3
    for s in outs:
        assert s not in text
    # 出力に埋め込まれた Base64 画像も出ない
    assert "image/png" not in text and "AAECAwQFBgcICQoL" not in text


def test_skeleton_nests_python_symbols(eda: bytes) -> None:
    rec = skeleton(eda, OPTS, rel=REL, sha256=hashlib.sha256(eda).hexdigest(), python_symbols=python_symbols)
    assert rec.lines[1:] == (
        "[Cell] 1 markdown @L1",
        "  doc: # 売上データの探索",
        "[Cell] 2 code (exec_count: 3) @L1-2",
        "[Cell] 3 code (exec_count: 12) @L4-11",
        "  [Function] pub load_frame(path: str) -> list[dict] @L4-9 { ...5 lines omitted }",
        "    doc: JSON Lines を読む。",
        "  [Variable] pub frame @L11",
        "[Cell] 4 code (exec_count: 13) @L13",
    )
    cells = rec.data["cells"]
    assert [c["number"] for c in cells] == [1, 2, 3, 4]
    assert cells[2]["symbols"][0]["name"] == "load_frame"
    assert cells[2]["symbols"][0]["range"] == {"start_line": 4, "end_line": 9}
    assert cells[0]["start_line"] is None and cells[2]["start_line"] == 4 and cells[2]["end_line"] == 11


def test_skeleton_classes_methods_and_options() -> None:
    src = (
        "class Repo:\n"
        '    """リポジトリ。"""\n'
        "\n"
        "    def get(self, key):\n"
        "        return key\n"
        "\n"
        "    def _hidden(self):\n"
        "        return 1\n"
        "\n"
        "def _helper():\n"
        "    return 2\n"
    )
    data = nb(code("import os", 1), code(src, 2))
    rec = skeleton(data, OPTS, rel="a.ipynb", sha256="a" * 64, python_symbols=python_symbols)
    assert rec.lines[1:] == (
        "[Cell] 1 code (exec_count: 1) @L1",
        "[Cell] 2 code (exec_count: 2) @L3-13",
        "  [Class] pub Repo @L3-10",
        "    doc: リポジトリ。",
        "    [Method] pub get(self, key) @L6-7 { ...1 lines omitted }",
        "    [Method] priv _hidden(self) @L9-10 { ...1 lines omitted }",
        "  [Function] priv _helper() @L12-13 { ...1 lines omitted }",
    )
    assert grammar.validate(rendered(rec), require_header=False) == []

    slim = skeleton(
        data,
        SkeletonOptions(include_private=False, include_doc_summary=False),
        rel="a.ipynb",
        sha256="a" * 64,
        python_symbols=python_symbols,
    )
    assert slim.lines[1:] == (
        "[Cell] 1 code (exec_count: 1) @L1",
        "[Cell] 2 code (exec_count: 2) @L3-13",
        "  [Class] pub Repo @L3-10",
        "    [Method] pub get(self, key) @L6-7 { ...1 lines omitted }",
    )


def test_skeleton_cell_rows_only_without_python_symbols() -> None:
    data = nb(code("def f():\n    pass", 5), code("x = 1"))
    rec = skeleton(data, OPTS, rel="a.ipynb", sha256="b" * 64)
    assert rec.lines[1:] == (
        "[Cell] 1 code (exec_count: 5) @L1-2",
        "[Cell] 2 code @L4",  # 未実行は exec_count を出さない
    )


def test_skeleton_markdown_headings() -> None:
    data = nb(
        md("intro text\n\n```\n# not a heading\n```\n\n## Real heading  \nmore"),
        md("no heading here"),
        {"cell_type": "raw", "metadata": {}, "source": "# raw is not markdown"},
        md("# " + "長い見出し" * 80),
        code("x = 1"),
    )
    rec = skeleton(data, OPTS, rel="a.ipynb", sha256="c" * 64)
    lines = rec.lines[1:]
    assert lines[0] == "[Cell] 1 markdown @L1"
    assert lines[1] == "  doc: ## Real heading"
    assert lines[2] == "[Cell] 2 markdown @L1"
    assert lines[3] == "[Cell] 3 raw @L1"
    assert lines[4] == "[Cell] 4 markdown @L1"
    assert lines[5].startswith("  doc: # 長い見出し") and lines[5].endswith("…") and len(lines[5]) <= 200
    assert lines[6] == "[Cell] 5 code @L1"
    assert grammar.validate(rendered(rec), require_header=False) == []

    quiet = skeleton(data, SkeletonOptions(include_doc_summary=False), rel="a.ipynb", sha256="c" * 64)
    assert not any("doc:" in line for line in quiet.lines)


def test_skeleton_ignores_symbols_outside_cells() -> None:
    data = nb(code("def f():\n    pass"))
    rec = skeleton(
        data,
        OPTS,
        rel="a.ipynb",
        sha256="d" * 64,
        python_symbols=lambda _virtual: python_symbols(b"\n\n\ndef g():\n    pass\n"),
    )
    assert rec.lines[1:] == ("[Cell] 1 code @L1-2",)


# --- cell:<n> ---------------------------------------------------------------------------


def test_select_cell_returns_source_only(eda: bytes, eda_doc: dict[str, Any]) -> None:
    (match,) = select(eda, "cell:3")
    src = "".join(eda_doc["cells"][2]["source"])
    assert "def load_frame" in match.text
    assert match.text == src  # source の連結と一致
    assert match.kind == "Cell" and match.name == "cell:3"
    assert match.range.line_span_external() == (4, 11)  # 仮想ファイル上の行
    assert (
        match.data["json_index"] == 2
        and match.data["exec_count"] == 12
        and match.data["in_virtual_file"] is True
    )
    for sentinel in SENTINELS:
        assert sentinel not in match.text
        assert sentinel not in repr(match)
    for cell in eda_doc["cells"]:
        for s in output_strings(cell.get("outputs", [])):
            if len(s) >= 8:
                assert s not in repr(match)


def test_select_cell_other_cells(eda: bytes) -> None:
    (c2,) = select(eda, "cell:2")
    assert c2.text == "import json\nfrom pathlib import Path" and c2.range.line_span_external() == (1, 2)
    (c4,) = select(eda, "cell:4")
    assert c4.text == "len(frame)" and c4.range.line_span_external() == (13, 13)
    (c1,) = select(eda, " cell:1 ")  # Markdown: source を返し、行は占めない
    assert c1.text.startswith("# 売上データの探索") and c1.data["in_virtual_file"] is False
    assert c1.data["cell_type"] == "markdown"


def test_select_cell_with_trailing_newline_keeps_raw_text() -> None:
    (m,) = select(nb(code("a = 1\n")), "cell:1")
    assert m.text == "a = 1\n" and m.range.line_span_external() == (1, 1)


@pytest.mark.parametrize("selector", ["cell:5", "cell:0", "cell:999", "cell:00000"])
def test_select_out_of_range_raises(eda: bytes, selector: str) -> None:
    with pytest.raises(ValueError, match="out of range"):
        select(eda, selector)


@pytest.mark.parametrize(
    "selector", ["", "cell:", "cell:x", "cell:-1", "cell:1.5", "xpath:/a", "cell:3/load_frame", "Cell:3"]
)
def test_select_bad_syntax_raises(eda: bytes, selector: str) -> None:
    with pytest.raises(ValueError):
        select(eda, selector)


# --- アダプタ登録用 ---------------------------------------------------------------------


def test_adapter_object(eda: bytes) -> None:
    assert ipynb.ADAPTER is ADAPTER
    assert (ADAPTER.language_id, int(ADAPTER.tier), ADAPTER.extensions) == ("ipynb", 2, (".ipynb",))
    (rec,) = ADAPTER.skeleton(eda, OPTS)
    assert rec.kind == "file-block" and f"sha={hashlib.sha256(eda).hexdigest()[:12]}" in rec.lines[0]
    (m,) = ADAPTER.select(eda, "cell:3")
    assert "def load_frame" in m.text
    with pytest.raises(NotImplementedError):
        ADAPTER.replace(eda, "cell:3", b"x")
