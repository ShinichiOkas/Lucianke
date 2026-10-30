"""core.compact_lines（Compact DSL の行の組み立て関数。mcp と syntax が共有する）のテスト。

文法の網羅は ``test_mcp_render.py`` が受け持つ。ここは「移した先から呼べること」「再公開が同一の関数であること」
「代表例」だけを確かめる。
"""

from __future__ import annotations

import pytest

from lucianke.core import compact_lines as cl
from lucianke.core.limits import SEARCH_SIGNATURE_MAX_CHARS
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
    # モジュールごとにまとめた項目を "; " で区切る（仕様書 §6.1 規約 10。BUG-0069）
    assert cl.import_line(["os", "collections.abc: Callable, Mapping"]) == (
        "imports: [os; collections.abc: Callable, Mapping]"
    )
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


# --- BUG-0069: 導ける urn: 行を出さない・検索ヒットのシグネチャを切る -----------------------------------------


def test_urn_lines_omits_a_urn_derivable_from_path_and_qualified_name() -> None:
    """``lk://<ws>/<path>#<qualified>`` は行のパスと修飾名から導けるので出さない（仕様書 §6.1 規約 2）。"""
    assert cl.urn_lines("lk://app/src/a.py#C.m", path="src/a.py", qualified="C.m", indent=0) == []
    assert cl.urn_lines("lk://app/src/a.py", path="src/a.py", qualified=None, indent=0) == []  # [Module] <path>
    # ヘッダの ws= と違うワークスペースなら導けない
    assert cl.urn_lines("lk://app/src/a.py#C.m", path="src/a.py", qualified="C.m", indent=0, workspace_id="other") == [
        "  urn: lk://app/src/a.py#C.m"
    ]


@pytest.mark.parametrize(
    ("urn", "path", "qualified"),
    [
        ("lk://app/src/a.py#f/2", "src/a.py", "f"),  # arity（オーバーロードの区別）
        ("lk://app/src/a.py#f~0a1b2c3d", "src/a.py", "f"),  # sighash（同名の再定義の区別）
        ("lk://app/src/a.py#lex:f@L3", "src/a.py", "f"),  # lex: セレクタ（Tier 4 の grep）
        ("lk://app/my%20dir/a.py#f", "my dir/a.py", "f"),  # パーセントエンコード
        ("lk://app/src/a.py#Outer.f", "src/a.py", "f"),  # 行の修飾名と違う
    ],
)
def test_urn_lines_keeps_a_urn_that_is_not_derivable(urn: str, path: str, qualified: str) -> None:
    assert cl.urn_lines(urn, path=path, qualified=qualified, indent=1) == [f"    urn: {urn}"]


def test_cap_signature_keeps_short_signatures_and_the_sig_inline_shape() -> None:
    assert cl.cap_signature(None) is None
    short = "(self, x: int) -> Receipt"
    assert cl.cap_signature(short) == short
    long_params = "(self, " + ", ".join(f"arg{i}: Callable[[int], str] | None = None" for i in range(20)) + ") -> Page"
    capped = cl.cap_signature(long_params)
    assert capped is not None
    assert len(capped) <= SEARCH_SIGNATURE_MAX_CHARS < len(long_params)
    assert capped.startswith("(self, arg0") and capped.endswith("…) -> Page")  # 戻り値は残る
    # symbol_line に渡しても sig-inline の形（"(" ... ")" [" -> " ...]）を保つ
    line = cl.symbol_line(Kind.METHOD, "a.py#C.m", sig=capped, start_line=1, end_line=2)
    assert line.endswith("…) -> Page @L1-2")
    # ( で始まらない形（lex: のヒットの行テキスト）は末尾を切る
    text = "x" * 300
    assert cl.cap_signature(text, 50) == "x" * 49 + "…"
