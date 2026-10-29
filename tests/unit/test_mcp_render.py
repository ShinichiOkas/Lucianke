"""MCP 層のレンダラ（compact・json）と ``build_meta`` の単体テスト。

行種ごとの組み立て関数の出力は、すべて ``tests/contract/compact_grammar.py`` の検査器に通す（設計書 §10.2）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice, Kind, Tier
from lucianke.core.records import Record, ResolutionMeta, ToolOutput
from lucianke.mcp.envelope import build_meta, collect_warnings
from lucianke.mcp.render import compact as c
from lucianke.mcp.render import json_out
from lucianke.mcp.types import CallInfo, Page, RenderInput

_GRAMMAR_PATH = Path(__file__).resolve().parents[1] / "contract" / "compact_grammar.py"
_spec = importlib.util.spec_from_file_location("lucianke_test_compact_grammar", _GRAMMAR_PATH)
assert _spec is not None and _spec.loader is not None
grammar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grammar)

HEADER = "# t ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~1/100tok"


def _check(*lines: str, header: bool = True) -> None:
    """``lines`` を文書にして検査器に通す。"""
    body = "\n".join(([HEADER] if header else []) + [x for line in lines for x in ([line] if isinstance(line, str) else line)])
    assert grammar.validate(body + "\n") == [], body


# --- 行種ごとの組み立て関数 ----------------------------------------------------------------------


def test_header_lines():
    h = c.header_line("get_file_skeleton", conf=Confidence.EXACT, lsp="ready", rev="WORKING_TREE",
                      ws="app", used_tokens=310, max_tokens=2000)
    assert h == "# get_file_skeleton ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~310/2000tok"
    assert grammar.validate(h + "\n") == []
    h2 = c.header_line("t", conf="heuristic", lsp="indexing", rev="HEAD", ws=None, used_tokens=1,
                       max_tokens=200, lsp_progress=40)
    assert h2 == "# t ok conf=heuristic lsp=indexing(40%) rev=HEAD ws=- ~1/200tok"
    assert grammar.validate(h2 + "\n") == []
    assert c.error_header_line("t", "E_TIMEOUT") == "# t error E_TIMEOUT"
    with pytest.raises(ValueError):
        c.error_header_line("t", "timeout")
    with pytest.raises(ValueError):
        c.header_line("t", conf="wild", lsp="ready", rev="HEAD", ws="a", used_tokens=1, max_tokens=2)


def test_kv_note_trailers():
    assert c.kv_line("target", "lk://app/a.py#f") == "target: lk://app/a.py#f"
    assert c.kv_line("rows", "line1\nline2") == "rows: line1 line2"
    with pytest.raises(ValueError):
        c.kv_line("two words", "x")
    assert c.note("a\nb") == "- a b"
    assert c.trailer_summary("38 more files") == "# summary: 38 more files"
    assert c.trailer_more("eyJvZmZzZXQiOjEyfQ") == "# more: cursor=eyJvZmZzZXQiOjEyfQ"
    assert c.trailer_more("abc", 34) == "# more: cursor=abc remaining≈34"
    with pytest.raises(ValueError):
        c.trailer_more("a b")
    _check(c.kv_line("target", "x"), c.note("n"), c.trailer_summary("s"), c.trailer_more("abc", 3))


def test_file_block_header_and_imports():
    assert c.file_block_header("src/a.py", lines=245, skeleton_lines=21, sha12="3f9c1a2b4c5d") == (
        "=== src/a.py (245 lines, skeleton: 21 lines, sha=3f9c1a2b4c5d) ==="
    )
    nb = c.file_block_header("n.ipynb", lines=310, cells=12, skeleton_lines=9, sha12="4F1E9A0B7C2D" + "0" * 52)
    assert nb == "=== n.ipynb (310 lines, 12 cells, skeleton: 9 lines, sha=4f1e9a0b7c2d) ==="
    assert c.file_block_header("a.py", lines=1) == "=== a.py (1 lines) ==="
    with pytest.raises(ValueError):
        c.file_block_header("a.py", lines=1, sha12="xyz")
    assert c.import_line(["Decimal from decimal", "OrderRepository"]) == "imports: [Decimal from decimal, OrderRepository]"
    assert c.import_line([]) == "imports: []"
    _check(nb, c.import_line(["a", "b"]), c.file_block_header("a.py", lines=1))


def test_symbol_line():
    line = c.symbol_line("Method", "process_charge", indent=1, modifiers=["pub", "async"],
                         sig="(self, req: PaymentRequest) -> Receipt", start_line=40, end_line=115, fold_lines=75)
    assert line == "  [Method] pub async process_charge(self, req: PaymentRequest) -> Receipt @L40-115 { ...75 lines omitted }"
    assert c.symbol_line(Kind.CLASS, "C", start_line=12, end_line=18) == "[Class] C @L12-18"
    assert c.symbol_line("Class", "C", start_line=5) == "[Class] C @L5"
    assert c.symbol_line("Class", "C", start_line=5, end_line=5) == "[Class] C @L5"
    # sig は括弧付きでも括弧なしでもよい。ret は sig に " -> " が無いときだけ足す
    assert c.symbol_line("Function", "f", sig="x: int", ret="str", start_line=1) == "[Function] f(x: int) -> str @L1"
    assert c.symbol_line("Function", "f", sig="(x) -> int", ret="int", start_line=1) == "[Function] f(x) -> int @L1"
    assert c.symbol_line("Function", "f", sig="", start_line=1) == "[Function] f() @L1"
    for bad in (
        dict(kind="Widget", name="w", start_line=1),
        dict(kind="Class", name="w", start_line=0),
        dict(kind="Class", name="w", start_line=5, end_line=4),
        dict(kind="Class", name="w", start_line=1, modifiers=["public"]),
        dict(kind="Class", name="w", start_line=1, ret="int"),
    ):
        with pytest.raises(ValueError):
            c.symbol_line(**bad)
    _check(line, c.symbol_line("Cell", "3 code (exec_count: 12)", start_line=10, end_line=48),
           c.symbol_line("Method", "pub", modifiers=("priv", "static"), sig="()", start_line=2, indent=2))


def test_attr_line():
    assert c.attr_line("urn", "lk://app/a.py#C", indent=0) == "  urn: lk://app/a.py#C"
    assert c.attr_line("doc", "x", indent=1) == "    doc: x"
    for key in sorted(grammar.ATTR_KEYS):
        _check("[Class] C @L1", c.attr_line(key, "v", indent=0))
    for bad in ("name", "Doc", "", "doc ", "raisez"):
        with pytest.raises(ValueError):
            c.attr_line(bad, "v", indent=0)
    # 検査器の語彙とビルダの語彙は一致している
    assert set(c.ATTR_KEYS) == set(grammar.ATTR_KEYS)
    assert set(c.KINDS) == set(grammar.KINDS)
    assert set(c.USES) == set(grammar.USES)
    assert set(c.SEVERITIES) == set(grammar.SEVERITIES)


def test_ref_line():
    line = c.ref_line("src/api/routes.py", 88, 20, use="call", enclosing="create_order",
                      snippet="receipt = await processor.process_charge(req)")
    assert line == "src/api/routes.py:88:20  [call] in create_order  receipt = await processor.process_charge(req)"
    assert c.ref_line("a.py", 3, snippet="x") == "a.py:3  x"
    assert c.ref_line("a.py", 3, snippet="x", indent=1) == "  a.py:3  x"
    assert c.ref_line("a.py", 3, enclosing="f", snippet="x") == "a.py:3  in f  x"
    for use in c.USES:
        _check(c.ref_line("a.py", 3, 4, use=use, snippet="x"))
    _check(line, c.ref_line("a.py", 3, snippet="x\ny"))
    for kwargs in (dict(line=0), dict(line=1, col=0), dict(line=1, use="calls")):
        with pytest.raises(ValueError):
            c.ref_line("a.py", snippet="x", **kwargs)


def test_diag_line():
    line = c.diag_line("E", "src/p.py", 58, 16, source="pyright", code="reportArgumentType",
                       message='"str" は "Decimal" に割り当てできません', fix="qf_7a1c")
    assert line == 'E src/p.py:58:16 [pyright:reportArgumentType] "str" は "Decimal" に割り当てできません  fix=qf_7a1c'
    assert c.diag_line("H", "a.py", 1, message="m") == "H a.py:1 m"
    for sev in c.SEVERITIES:
        _check(c.diag_line(sev, "a.py", 2, 3, source="ruff", code="F401", message="unused"))
    _check(line, c.diag_line("W", "a.py", 1, message="plain"))
    for bad in (
        lambda: c.diag_line("X", "a.py", 1, message="m"),
        lambda: c.diag_line("E", "a.py", 0, message="m"),
        lambda: c.diag_line("E", "a.py", 1, source="ruff", message="m"),
        lambda: c.diag_line("E", "a.py", 1, source="ru ff", code="F", message="m"),
        lambda: c.diag_line("E", "a.py", 1, message="m", fix="a b"),
    ):
        with pytest.raises(ValueError):
            bad()


def test_tree_path_step_lines():
    t = c.tree_line("<-", "create_order", "src/api/routes.py", 88, indent=0, when="req.amount > 0")
    assert t == "<- create_order @src/api/routes.py:88  when: req.amount > 0"
    cyc = c.tree_line("<-", "retry_order", "src/api/routes.py", 140, indent=1, cycle=True)
    assert cyc == "  <- retry_order @src/api/routes.py:140 (cycle)"
    full = c.tree_line("->", "callee", "a.py", 3, indent=2, when="w", conf=Confidence.HEURISTIC, cycle=True)
    assert full == "    -> callee @a.py:3 (cycle)  when: w  conf=heuristic"
    assert c.path_header(1, 3) == "path 1 (3 hops)"
    s = c.step_line(2, "build_query", "src/services/order.py", 40, var="req.note", when="req.note is not None")
    assert s == "  2. build_query @src/services/order.py:40  var=req.note  when: req.note is not None"
    assert c.step_line(1, "f", "a.py", 1, indent=0) == "1. f @a.py:1"
    _check(t, cyc, full, c.path_header(1, 3), s, c.step_line(1, "f", "a.py", 1))
    for bad in (
        lambda: c.tree_line("=>", "n", "a.py", 1, indent=0),
        lambda: c.tree_line("<-", "n", "a.py", 0, indent=0),
        lambda: c.tree_line("<-", "n", "a.py", 1, indent=0, conf="sure"),
        lambda: c.tree_line("<-", "n", "a.py", 1, indent=-1),
        lambda: c.step_line(1, "n", "a.py", 0),
    ):
        with pytest.raises(ValueError):
            bad()


def test_code_block():
    out = c.code_block(["def f():", "    return 1"], lang="python", start_line=116)
    assert out == ["```python", "@L116: def f():", "@L117:     return 1", "```"]
    assert c.code_block("a\nb") == ["```", "a", "b", "```"]
    assert c.code_block(["a", "b"], start_line=5, with_line_numbers=False) == ["```", "a", "b", "```"]
    assert c.code_block(["a"], with_line_numbers=True) == ["```", "a", "```"]  # start_line が無ければ付けない
    # コード内の ``` はフェンスを延長してそのまま出す
    ext = c.code_block(["x = '''", "```", "'''"], lang="md", start_line=1)
    assert ext[0] == "````md" and ext[-1] == "````" and ext[2] == "@L2: ```"
    five = c.code_block(["`````x"])
    assert five[0] == "``````" and five[-1] == "``````"
    # 行の長さの省略はコードブロックに適用しない
    long = "y" * 500
    assert c.code_block([long])[1] == long
    _check(out, ext, five, c.code_block(["```", "# summary: not a trailer"], lang="text"))
    with pytest.raises(ValueError):
        c.code_block(["x"], lang="a b")


def test_table():
    assert c.table([["urn", "n"], ["lk://app/a.py#f", 7], ["lk://app/b.py#g", None]]) == [
        "| urn | n |",
        "| lk://app/a.py#f | 7 |",
        "| lk://app/b.py#g | NULL |",
    ]
    assert c.table([["a|b", "c\nd"]]) == ["| a\\|b | c d |"]
    assert c.table([]) == []
    _check(c.table([["x", "y"], ["a|b", ""], ["", ""]]))


# --- 規約 6: 200 文字の省略 ---------------------------------------------------------------------


@pytest.mark.parametrize("n", [150, 199, 200, 201, 500, 5000])
def test_truncation_keeps_lines_within_limit(n):
    text = "あ" * n
    lines = {
        "ref": c.ref_line("src/api/routes.py", 88, 20, use="call", enclosing="create_order", snippet=text),
        "ref-indented": c.ref_line("a.py", 1, snippet=text, indent=2),
        "doc": c.attr_line("doc", text, indent=1),
        "when": c.attr_line("when", text, indent=0),
        "tree": c.tree_line("<-", "f", "a.py", 1, indent=1, when=text),
        "tree+conf": c.tree_line("<-", "f", "a.py", 1, indent=1, when=text, conf="exact", cycle=True),
        "step": c.step_line(1, "f", "a.py", 1, var="v", when=text),
    }
    for label, line in lines.items():
        assert len(line) <= c.LINE_LIMIT, label
        if n >= 201:
            assert "…" in line, label
            if label != "tree+conf":
                assert line.endswith("…"), label
    if n <= 100:
        assert all(text in line for line in lines.values())
    _check(*lines.values())


def test_truncation_exact_boundary_and_markers():
    prefix = "a.py:1  "
    fits = prefix + "x" * (200 - len(prefix))
    assert c.ref_line("a.py", 1, snippet="x" * (200 - len(prefix))) == fits and len(fits) == 200
    over = c.ref_line("a.py", 1, snippet="x" * (201 - len(prefix)))
    assert len(over) == 200 and over.endswith("x…") and over.startswith(prefix)
    tree = c.tree_line("<-", "f", "a.py", 1, indent=0, when="w" * 400, conf="exact")
    assert tree.endswith("…  conf=exact") and len(tree) == 200
    # sig は省略しない
    sig = "(" + "a, " * 200 + "b)"
    assert len(c.symbol_line("Function", "f", sig=sig, start_line=1)) > 200
    assert c.attr_line("sig", "s" * 300, indent=0).endswith("s" * 300)


# --- バナー ---------------------------------------------------------------------------------


def _meta(level=Confidence.EXACT, *, degraded=False, warning=None, engine="pyright+tree-sitter",
          lang="python", lsp="ready", progress=None):
    return ResolutionMeta(confidence_level=level, is_degraded=degraded, tier=Tier.T1, detected_language=lang,
                          engine_used=engine, lsp_status=lsp, lsp_progress=progress, accuracy_warning=warning)


def test_banner_lines():
    assert c.banner_lines(_meta()) == []
    degraded = _meta(Confidence.HEURISTIC, degraded=True, engine="ctags", lang="unknown (.cob)",
                     warning="Tree-sitter grammar unavailable.\nSymbol ranges are estimated.")
    assert c.banner_lines(degraded) == [
        "! DEGRADED: confidence=heuristic engine=ctags lang=unknown(.cob)",
        "! Warning: Tree-sitter grammar unavailable. Symbol ranges are estimated.",
    ]
    assert len(c.banner_lines(_meta(Confidence.STRUCTURAL, degraded=True))) == 1  # 警告文が無ければ DEGRADED 行だけ
    _check(c.banner_lines(degraded))
    assert c.banner("a\n\n b ") == ["! a", "! b"]


# --- render（compact）-----------------------------------------------------------------------


def _call(**kw):
    base = dict(tool="find_references_and_implementations", revision="WORKING_TREE", workspace_id="app",
                format="compact", max_tokens=2000, timing_ms=7)
    return CallInfo(**{**base, **kw})


def _record(i, conf=Confidence.EXACT):
    return Record(kind="ref", key=f"r{i}", priority=i,
                  lines=(c.ref_line(f"src/m{i}.py", i + 1, 5, use="call", enclosing="f", snippet=f"call_{i}()"),),
                  data={"path": f"src/m{i}.py", "line": i + 1}, confidence=conf)


def _out(records, *, head_kv=(), engine=None, max_confidence=Confidence.EXACT, static=None):
    return ToolOutput(records=records, max_confidence=max_confidence,
                      engine=engine or EngineChoice(engine="pyright+tree-sitter", confidence=Confidence.EXACT,
                                                    lsp_status="ready"),
                      page_unit="reference", head_kv=head_kv, data_static=static or {},
                      tier=Tier.T1, detected_language="python")


def _page(records, *, cursor=None, summary=None, remaining=0, total=None, used=420, truncated=False, warnings=()):
    return Page(records=tuple(records), next_cursor=cursor, summary=summary,
                total_estimate=len(records) if total is None else total, remaining=remaining,
                used_tokens_est=used, max_tokens=2000, truncated=truncated, warnings=tuple(warnings))


def test_render_success_order_and_grammar():
    recs = [_record(0), _record(1)]
    out = _out(recs, head_kv=[("target", "lk://app/a.py#f"), ("references", "2 files, 2 sites")])
    page = _page(recs, cursor="eyJzaXRlIjoyfQ", summary="3 more sites (src: 3)", remaining=3, total=5)
    meta = build_meta(out, page)
    r = c.render(RenderInput(call=_call(), meta=meta, out=out, page=page, warnings=["w1", "w2"]))
    assert not r.is_error and r.structured is None
    assert r.text == (
        "# find_references_and_implementations ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~420/2000tok\n"
        "! w1\n! w2\n"
        "target: lk://app/a.py#f\nreferences: 2 files, 2 sites\n"
        "src/m0.py:1:5  [call] in f  call_0()\nsrc/m1.py:2:5  [call] in f  call_1()\n"
        "# summary: 3 more sites (src: 3)\n"
        "# more: cursor=eyJzaXRlIjoyfQ remaining≈3\n"
    )
    assert grammar.validate(r.text) == []


def test_render_success_without_trailer_and_without_workspace():
    out = _out([])
    page = _page([], used=10)
    r = c.render(RenderInput(call=_call(workspace_id=None), meta=build_meta(out, page), out=out, page=page))
    assert r.text == "# find_references_and_implementations ok conf=exact lsp=ready rev=WORKING_TREE ws=- ~10/2000tok\n"
    assert grammar.validate(r.text) == []


def test_render_degraded_has_banner_after_header_before_warnings():
    engine = EngineChoice(engine="ctags", confidence=Confidence.HEURISTIC, lsp_status="n/a",
                          warning="Tree-sitter grammar unavailable. Symbol ranges are estimated from indentation.")
    recs = [_record(0, Confidence.HEURISTIC)]
    out = _out(recs, head_kv=[("target", "x")], engine=engine)
    page = _page(recs)
    meta = build_meta(out, page, detected_language="unknown (.cob)")
    assert meta.is_degraded and meta.confidence_level is Confidence.HEURISTIC
    r = c.render(RenderInput(call=_call(), meta=meta, out=out, page=page, warnings=["extra"]))
    lines = r.text.splitlines()
    assert lines[0].startswith("# find_references_and_implementations ok conf=heuristic lsp=n/a ")
    assert lines[1] == "! DEGRADED: confidence=heuristic engine=ctags lang=unknown(.cob)"
    assert lines[2] == "! Warning: Tree-sitter grammar unavailable. Symbol ranges are estimated from indentation."
    assert lines[3] == "! extra" and lines[4] == "target: x"
    assert grammar.validate(r.text) == []


def test_render_indexing_progress_in_header():
    engine = EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL, lsp_status="indexing",
                          lsp_progress=40)
    out = _out([_record(0, Confidence.STRUCTURAL)], engine=engine)
    page = _page(out.records)
    r = c.render(RenderInput(call=_call(), meta=build_meta(out, page), out=out, page=page))
    assert " lsp=indexing(40%) " in r.text.splitlines()[0]
    assert grammar.validate(r.text) == []


def test_render_merges_page_warnings_without_duplicates():
    recs = [_record(0)]
    out = _out(recs)
    page = _page(recs, warnings=["budget", "dup"])
    inp = RenderInput(call=_call(), meta=build_meta(out, page), out=out, page=page, warnings=["dup", "own"])
    assert collect_warnings(inp) == ["dup", "own", "budget"]
    assert c.render(inp).text.count("! dup") == 1
    assert json_out.render(inp).structured["warnings"] == ["dup", "own", "budget"]


def test_render_error():
    err = LuciankeError(ErrorCode.E_TARGET_NOT_FOUND, "target not found\nsecond line")
    r = c.render(RenderInput(call=_call(), meta=build_meta(None, None), error=err))
    assert r.is_error
    assert r.text == "# find_references_and_implementations error E_TARGET_NOT_FOUND\n! target not found\n! second line\n"
    assert grammar.validate(r.text) == []


def test_render_error_ambiguous_lists_candidates():
    cands = [
        {"urn": "lk://app/a.py#A.login", "signature": "(self, u: str) -> bool", "path": "a.py", "line": 10},
        {"urn": "lk://app/b.py#B.login", "signature": "(self) -> None", "path": "b.py", "line": 22},
    ]
    err = LuciankeError("E_TARGET_AMBIGUOUS", "symbol_name 'login' は 2 件に一致します", candidates=cands)
    r = c.render(RenderInput(call=_call(), meta=build_meta(None, None), error=err, warnings=["w"]))
    assert r.text.splitlines() == [
        "# find_references_and_implementations error E_TARGET_AMBIGUOUS",
        "! symbol_name 'login' は 2 件に一致します",
        "! candidate: lk://app/a.py#A.login  (self, u: str) -> bool  @a.py:10",
        "! candidate: lk://app/b.py#B.login  (self) -> None  @b.py:22",
        "! w",
    ]
    assert grammar.validate(r.text) == []
    # 欠けた欄があっても落ちない
    sparse = LuciankeError("E_TARGET_AMBIGUOUS", "m", candidates=[{"urn": "lk://app/a.py#A"}])
    assert c.render(RenderInput(call=_call(), meta=build_meta(None, None), error=sparse)).text.splitlines()[2] == (
        "! candidate: lk://app/a.py#A"
    )


def test_render_error_with_partial_result_appends_records():
    err = LuciankeError("E_TIMEOUT", "grep timed out")
    recs = [_record(0)]
    out = _out(recs)
    page = _page(recs, cursor="abc", remaining=4)
    r = c.render(RenderInput(call=_call(), meta=build_meta(out, page), out=out, page=page, error=err))
    assert r.is_error
    assert r.text.splitlines() == [
        "# find_references_and_implementations error E_TIMEOUT",
        "! grep timed out",
        "src/m0.py:1:5  [call] in f  call_0()",
        "# more: cursor=abc remaining≈4",
    ]
    assert grammar.validate(r.text) == []


# --- json --------------------------------------------------------------------------------


ENVELOPE_KEYS = {"ok", "tool", "_resolution_meta", "data", "page", "budget", "timing_ms", "warnings", "error"}


def test_json_success_shape():
    recs = [_record(0), _record(1)]
    out = _out(recs, static={"target": {"urn": "lk://app/a.py#f"}, "totals": {"sites": 2}})
    page = _page(recs, cursor="abc", remaining=3, total=5, used=420, truncated=True)
    inp = RenderInput(call=_call(format="json", timing_ms=9), meta=build_meta(out, page), out=out, page=page,
                      warnings=["w"], records_key="references")
    r = json_out.render(inp)
    env = json.loads(r.text)
    assert set(env) == ENVELOPE_KEYS
    assert r.structured == env and not r.is_error
    assert env["ok"] is True and env["tool"] == "find_references_and_implementations"
    assert env["data"] == {"target": {"urn": "lk://app/a.py#f"}, "totals": {"sites": 2},
                           "references": [{"path": "src/m0.py", "line": 1}, {"path": "src/m1.py", "line": 2}]}
    assert env["page"] == {"next_cursor": "abc", "returned": 2, "total_estimate": 5}
    assert env["budget"] == {"max_tokens": 2000, "used_tokens_est": 420, "truncated": True}
    assert env["timing_ms"] == 9 and env["warnings"] == ["w"] and env["error"] is None
    assert env["_resolution_meta"] == inp.meta.to_json()
    assert env["_resolution_meta"]["confidence_level"] == "exact"


def test_json_default_records_key_and_non_ascii():
    recs = [Record(kind="note", key="k", priority=0, lines=("- x",), data={"msg": "決済"}, confidence=Confidence.EXACT)]
    out = _out(recs)
    page = _page(recs)
    r = json_out.render(RenderInput(call=_call(), meta=build_meta(out, page), out=out, page=page))
    assert "決済" in r.text and "\\u" not in r.text
    assert json.loads(r.text)["data"] == {"items": [{"msg": "決済"}]}


def test_json_failure_keeps_every_key():
    err = LuciankeError("E_TARGET_AMBIGUOUS", "m", details={"param": "target"},
                        candidates=[{"urn": "lk://app/a.py#A", "signature": "()", "path": "a.py", "line": 1}])
    r = json_out.render(RenderInput(call=_call(max_tokens=1500), meta=build_meta(None, None), error=err))
    env = json.loads(r.text)
    assert r.is_error and set(env) == ENVELOPE_KEYS
    assert env["ok"] is False and env["data"] is None
    assert env["error"] == err.to_json()
    assert env["page"] == {"next_cursor": None, "returned": 0, "total_estimate": 0}
    assert env["budget"] == {"max_tokens": 1500, "used_tokens_est": 0, "truncated": False}
    assert env["_resolution_meta"]["confidence_level"] == "exact"
    assert env["warnings"] == []


def test_json_error_with_partial_result_keeps_data():
    recs = [_record(0)]
    out = _out(recs, static={"totals": {"sites": 1}})
    page = _page(recs)
    err = LuciankeError("E_TIMEOUT", "t")
    env = json.loads(json_out.render(RenderInput(call=_call(), meta=build_meta(out, page), out=out, page=page,
                                                 error=err, records_key="references")).text)
    assert env["ok"] is False and env["data"]["references"] == [{"path": "src/m0.py", "line": 1}]


# --- build_meta ---------------------------------------------------------------------------


def test_build_meta_exact_and_mixed():
    recs = [_record(0), _record(1, Confidence.LEXICAL_FALLBACK), _record(2, Confidence.EXACT)]
    out = _out(recs)
    meta = build_meta(out, _page(recs))
    assert meta.confidence_level is Confidence.LEXICAL_FALLBACK and meta.is_degraded and meta.mixed
    assert meta.confidence_breakdown == {Confidence.EXACT: 2, Confidence.LEXICAL_FALLBACK: 1}
    assert meta.accuracy_warning == (
        "未知の言語のため単語境界 grep に降格しました。同名の誤検知と動的参照の見落としが含まれます。"
    )
    assert meta.tier is Tier.T1 and meta.detected_language == "python"
    assert meta.engine_used == "pyright+tree-sitter" and meta.lsp_status == "ready" and meta.lsp_progress is None
    clean = build_meta(_out([_record(0)]), _page([_record(0)]))
    assert not clean.is_degraded and not clean.mixed and clean.accuracy_warning is None


def test_build_meta_overrides_and_failure():
    out = _out([_record(0)])
    meta = build_meta(out, _page([_record(0)]), tier=Tier.T3, detected_language="toml")
    assert meta.tier is Tier.T3 and meta.detected_language == "toml"
    failed = build_meta(None, None)
    assert (failed.confidence_level, failed.is_degraded, failed.engine_used, failed.lsp_status) == (
        Confidence.EXACT, False, "n/a", "n/a")
    assert failed.tier is Tier.T1 and failed.detected_language == "n/a" and failed.accuracy_warning is None


@pytest.mark.parametrize(("level", "needle"), [
    (Confidence.STRUCTURAL, "構文木のみ"),
    (Confidence.HEURISTIC, "ctags とブロック推定"),
    (Confidence.LEXICAL_FALLBACK, "単語境界 grep"),
])
def test_build_meta_degraded_templates_and_engine_warning(level, needle):
    engine = EngineChoice(engine="e", confidence=level, lsp_status="unavailable")
    out = _out([_record(0, level)], engine=engine)
    meta = build_meta(out, _page(out.records))
    assert meta.is_degraded and needle in (meta.accuracy_warning or "")
    custom = EngineChoice(engine="e", confidence=level, warning="エンジン固有の注意")
    out2 = _out([_record(0, level)], engine=custom)
    assert build_meta(out2, _page(out2.records)).accuracy_warning == "エンジン固有の注意"


def test_build_meta_structural_tool_is_not_degraded():
    # 最高水準が structural のツール（T-12 など）は structural で降格ではない（仕様書 §8.3）
    engine = EngineChoice(engine="ast-grep", confidence=Confidence.STRUCTURAL)
    out = _out([_record(0, Confidence.STRUCTURAL)], engine=engine, max_confidence=Confidence.STRUCTURAL)
    meta = build_meta(out, _page(out.records))
    assert meta.confidence_level is Confidence.STRUCTURAL and not meta.is_degraded
    assert meta.accuracy_warning is None


def test_build_meta_zero_records_uses_engine_level_and_missing_page():
    engine = EngineChoice(engine="internal_ripgrep_word_boundary", confidence=Confidence.LEXICAL_FALLBACK)
    out = _out([], engine=engine)
    meta = build_meta(out, _page([]))
    assert meta.confidence_level is Confidence.LEXICAL_FALLBACK and meta.is_degraded
    assert meta.confidence_breakdown == {} and not meta.mixed
    # page が無いときは out.records 全体から集計する
    out2 = _out([_record(0), _record(1, Confidence.HEURISTIC)])
    assert build_meta(out2, None).confidence_level is Confidence.HEURISTIC


def test_build_meta_lsp_progress_only_when_indexing():
    idx = EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL, lsp_status="indexing", lsp_progress=140)
    out = _out([_record(0, Confidence.STRUCTURAL)], engine=idx)
    assert build_meta(out, _page(out.records)).lsp_progress == 100
    ready = EngineChoice(engine="x", confidence=Confidence.EXACT, lsp_status="ready", lsp_progress=40)
    assert build_meta(_out([_record(0)], engine=ready), None).lsp_progress is None


# --- 予算との整合（パッカーの数え方とレンダラの出力が一致すること）-------------------------------------


@pytest.mark.parametrize("max_tokens", [200, 260, 400, 800, 2000])
def test_render_output_within_budget_when_packed(max_tokens):
    from lucianke.mcp.budget import Packer, TokenEstimator

    est = TokenEstimator().count
    recs = [
        Record(kind="ref", key=f"r{i}", priority=i, group=f"src/m{i % 3}.py", confidence=Confidence.EXACT,
               lines=(c.ref_line(f"src/m{i % 3}.py", i + 1, 5, use="call", enclosing=f"fn_{i}",
                                 snippet=f"結果 = await processor.process_charge(request_{i}, retry={i})"),),
               data={"line": i + 1})
        for i in range(60)
    ]
    out = _out(recs, head_kv=[("target", "lk://app/src/services/payment.py#PaymentProcessor.process_charge")])
    meta = build_meta(out, _page(recs))
    warnings = ["引数 x は無視されました"]
    # 呼び手（パイプライン）がやるとおり、ヘッダ・警告・head_kv を最大幅で描いて reserved に数える
    probe = c.render(RenderInput(call=_call(max_tokens=max_tokens), meta=meta, out=out,
                                 page=_page([], used=max_tokens), warnings=warnings))
    page = Packer().pack(out, max_tokens=max_tokens, reserved_tokens=est(probe.text),
                         cursor_factory=lambda off: f"cursor{off}")
    r = c.render(RenderInput(call=_call(max_tokens=max_tokens), meta=meta, out=out, page=page, warnings=warnings))
    assert est(r.text) <= max_tokens
    assert page.used_tokens_est <= max_tokens
    assert grammar.validate(r.text) == []
    if page.next_cursor:
        assert page.remaining > 0 and r.text.splitlines()[-1].startswith("# more: cursor=cursor")
        assert r.text.splitlines()[-1].endswith(f"remaining≈{page.remaining}")
    else:
        assert "# more:" not in r.text
