"""データ形式アダプタの索引用シンボル（``DataAdapter.symbols``。骨格の最上位の節だけ）と ``skeleton_record`` の単体テスト。

URN のセレクタは同じアダプタの ``select`` で解けること、壊れたファイル・JSONL・根が配列のファイルはシンボルを持たないこと、
同じ URN は 1 つだけ残ること、上限があることを見る。フィクスチャは ``tests/fixtures/poly``。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lucianke.core.model import Confidence, Kind
from lucianke.syntax.adapters._symbols import MAX_INDEX_SYMBOLS, MAX_SYMBOL_SOURCE_BYTES, selector_expr
from lucianke.syntax.support import ADAPTERS, language_for_extension
from lucianke.syntax.types import SkeletonOptions

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS = "poly"


def adapter_of(rel: str):
    lid = language_for_extension(Path(rel).suffix)
    assert lid is not None
    return ADAPTERS[lid]


def symbols(rel: str, source: bytes | None = None):
    src = (POLY / rel).read_bytes() if source is None else source
    return adapter_of(rel).symbols(src, rel=rel, workspace_id=WS)


def resolve(rel: str, sym, source: bytes | None = None):
    """シンボルの URN のセレクタを ``select`` で解く。"""
    src = (POLY / rel).read_bytes() if source is None else source
    sel = sym.urn.selector
    return adapter_of(rel).select(src, f"{sel.kind}:{sel.expr}")


@pytest.mark.parametrize(
    ("rel", "kind", "names"),
    [
        ("conf/settings.json", Kind.KEY, ["service", "limits", "features"]),
        ("conf/app.yaml", Kind.KEY, ["service", "retry"]),
        ("pyproject.toml", Kind.KEY, ["project", "tool.ruff"]),
        ("reports/junit.xml", Kind.TAG, ["testsuites", "testsuites.testsuite"]),
    ],
)
def test_top_level_sections_become_symbols(rel: str, kind: Kind, names: list[str]) -> None:
    syms = symbols(rel)
    assert [s.qualified_name for s in syms] == names
    for s in syms:
        assert s.kind is kind and s.confidence is Confidence.STRUCTURAL
        assert s.path == rel and s.urn.workspace_id == WS and s.urn.path == rel
        assert (
            s.name == s.qualified_name.rpartition(".")[2] or kind is Kind.KEY
        )  # TOML の暗黙の表は名前が経路そのもの


@pytest.mark.parametrize(
    "rel", ["conf/settings.json", "conf/app.yaml", "pyproject.toml", "reports/junit.xml"]
)
def test_every_symbol_urn_resolves_to_its_own_range(rel: str) -> None:
    syms = symbols(rel)
    assert syms
    for s in syms:
        found = resolve(rel, s)
        assert found, s.urn
        assert found[0].range.start.line + 1 == s.range.line_span_external()[0], (s.urn, found[0].range)


def test_xml_symbols_cover_root_and_direct_children_only() -> None:
    src = b"<a>\n  <b>\n    <c/>\n  </b>\n  <b/>\n  <d/>\n</a>\n"
    got = [(s.qualified_name, s.range.line_span_external()) for s in symbols("x.xml", src)]
    assert got == [("a", (1, 7)), ("a.b", (2, 5)), ("a.d", (6, 6))]  # 同名の兄弟は 1 つ。孫（c）は出さない


def test_jsonl_and_non_object_roots_have_no_symbols() -> None:
    assert symbols("a.jsonl", b'{"a": 1}\n{"a": 2}\n') == []
    assert symbols("a.json", b'[{"a": 1}, {"a": 2}]') == []
    assert symbols("a.yaml", b"- a\n- b\n") == []
    assert symbols("a.json", b"") == [] and symbols("a.yaml", b"") == [] and symbols("a.toml", b"") == []


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        ("a.json", b'{"a": '),
        ("a.yaml", b"a: [1, 2\nb: {x\n"),
        ("a.yaml", b"a: *undefined\n"),
        ("a.toml", b"a = \n["),
        ("a.xml", b"<a><b></a>"),
        ("a.json", b"\xff\xfe\x00"),
    ],
)
def test_broken_files_have_no_symbols_and_do_not_raise(rel: str, source: bytes) -> None:
    assert symbols(rel, source) == []


def test_ipynb_has_no_symbols_but_a_skeleton_record() -> None:
    nb = (POLY / "notebooks/eda.ipynb").read_bytes()
    a = ADAPTERS["ipynb"]
    assert a.symbols(nb, rel="notebooks/eda.ipynb", workspace_id=WS) == []
    rec = a.skeleton_record(
        nb, SkeletonOptions(), rel="notebooks/eda.ipynb", sha256=hashlib.sha256(nb).hexdigest()
    )
    assert (
        rec.data["ok"] is True
        and rec.data["language"] == "ipynb"
        and rec.data["path"] == "notebooks/eda.ipynb"
    )


def test_tricky_keys_resolve_or_are_dropped() -> None:
    doc = {
        "plain": 1,
        "with space": {"x": 1},
        "dot.ted": 2,
        "dash-ed": 3,
        "日本語": 4,
        'q"uote': 5,
        "back" + chr(92) + "slash": 6,
        "*": 7,
        "0": 8,
    }
    src = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
    syms = symbols("t.json", src)
    names = {s.name for s in syms}
    assert {"plain", "with space", "dot.ted", "dash-ed", "日本語"} <= names
    assert (
        "*" not in names and 'q"uote' not in names and "back" + chr(92) + "slash" not in names
    )  # 式にできない名前は落とす
    for s in syms:
        found = resolve("t.json", s, src)
        assert found and found[0].range.start.line + 1 == s.range.line_span_external()[0], s.urn
    ydoc = b"plain: 1\nwith space:\n  x: 1\n'dot.ted': 2\ndash-ed: 3\n'*': 4\n"
    ysyms = symbols("t.yaml", ydoc)
    assert {s.name for s in ysyms} == {"plain", "with space", "dot.ted", "dash-ed", "*"}
    for s in ysyms:
        found = resolve("t.yaml", s, ydoc)
        assert len(found) == 1 and found[0].range.start.line + 1 == s.range.line_span_external()[0], s.urn
    tdoc = b'plain = 1\n"with space" = 2\ndash-ed = 3\n[sec.sub]\nx = 1\n'
    tsyms = symbols("t.toml", tdoc)
    assert {s.name for s in tsyms} == {"plain", "with space", "dash-ed", "sec.sub"}
    for s in tsyms:
        found = resolve("t.toml", s, tdoc)
        assert found and found[0].range.start.line + 1 == s.range.line_span_external()[0], s.urn


def test_xml_names_that_cannot_be_xpath_are_dropped() -> None:
    src = b'<root xmlns:p="urn:x">\n  <p:item/>\n  <plain/>\n</root>\n'
    got = [s.qualified_name for s in symbols("n.xml", src)]
    assert got == ["root", "root.plain"]


def test_symbols_are_capped_per_file() -> None:
    src = json.dumps({f"k{i}": i for i in range(MAX_INDEX_SYMBOLS + 50)}).encode("utf-8")
    syms = symbols("big.json", src)
    assert len(syms) == MAX_INDEX_SYMBOLS
    assert len({str(s.urn) for s in syms}) == MAX_INDEX_SYMBOLS


def test_large_files_have_no_symbols() -> None:
    pad = "x" * (MAX_SYMBOL_SOURCE_BYTES + 1)
    big = json.dumps({"a": pad}).encode("utf-8")
    assert len(big) > MAX_SYMBOL_SOURCE_BYTES and symbols("big.json", big) == []
    ok = json.dumps({"a": pad[: MAX_SYMBOL_SOURCE_BYTES // 2]}).encode("utf-8")
    assert [s.name for s in symbols("ok.json", ok)] == ["a"]


def test_selector_expr_shapes() -> None:
    assert selector_expr("jsonpath", ["a"]) == "$.a"
    assert selector_expr("jsonpath", ["a b"]) == '$."a b"'
    assert selector_expr("jsonpath", ['a"b']) is None
    assert selector_expr("yaml", ["a.b"]) == '"a.b"'
    assert selector_expr("yaml", ["a", "b"]) == "a.b"
    assert selector_expr("toml", ["tool.ruff"]) == "tool.ruff"
    assert selector_expr("toml", ['a"b']) is None
    assert selector_expr("xpath", ["a", "b"]) == "/a/b"
    assert selector_expr("xpath", ["p:a"]) is None
    assert selector_expr("jq", ["a"]) is None
    assert selector_expr("yaml", []) is None
