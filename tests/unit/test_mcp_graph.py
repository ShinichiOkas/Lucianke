"""``mcp/render/graph.py``（dot / mermaid のレンダラ）の単体テスト（仕様書 §5.4.3、§6.3、§6.4、T-06・T-07・T-20）。

入力は ``ToolOutput.data_static["graph"]``、無ければ ``Record.data`` の ``source_urn`` / ``target_urn`` / ``relation_kind`` 列。
dot の構文の妥当性は、コメントを除いた本文の括弧と二重引用符の対応（と各文の ``;``）で簡易に検査する。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice, Tier
from lucianke.core.records import Record, ResolutionMeta, ToolOutput
from lucianke.mcp.render import compact
from lucianke.mcp.render.graph import MAX_EDGES, MAX_NODES, render_dot, render_mermaid
from lucianke.mcp.types import CallInfo, Page, Rendered, RenderInput

TOOL = "query_call_hierarchy"
A = "lk://app/src/a.py#f"
B = "lk://app/src/b.py#B.g/2~abcd1234"
C = "lk://app/src/c.py#h"


def meta(*, degraded: bool = False, level: Confidence = Confidence.EXACT) -> ResolutionMeta:
    return ResolutionMeta(
        confidence_level=level,
        is_degraded=degraded,
        tier=Tier.T1,
        detected_language="python",
        engine_used="pyright+tree-sitter",
        lsp_status="ready",
        accuracy_warning="構文木のみで解決しました。" if degraded else None,
    )


def call(fmt: str) -> CallInfo:
    return CallInfo(tool=TOOL, revision="WORKING_TREE", workspace_id="app", format=fmt, max_tokens=2000)


def tool_output(
    *,
    graph: Mapping[str, Any] | None = None,
    records: Sequence[Record] = (),
    max_conf: Confidence = Confidence.EXACT,
    head_kv: Sequence[tuple[str, str]] = (),
    extra_static: Mapping[str, Any] | None = None,
) -> ToolOutput:
    static: dict[str, Any] = dict(extra_static or {})
    if graph is not None:
        static["graph"] = graph
    return ToolOutput(
        records=records,
        max_confidence=max_conf,
        engine=EngineChoice(engine="pyright+tree-sitter", confidence=max_conf),
        page_unit="edge",
        head_kv=head_kv,
        data_static=static,
    )


def make_input(
    fmt: str,
    out: ToolOutput | None,
    *,
    page: Page | None = None,
    m: ResolutionMeta | None = None,
    error: LuciankeError | None = None,
    warnings: Sequence[str] = (),
) -> RenderInput:
    return RenderInput(call=call(fmt), meta=m or meta(), out=out, page=page, error=error, warnings=warnings)


def row(
    source: str, target: str, kind: str | None = None, *, conf: Confidence = Confidence.EXACT, **more: Any
) -> Record:
    data: dict[str, Any] = {"source_urn": source, "target_urn": target, **more}
    if kind is not None:
        data["relation_kind"] = kind
    return Record(kind="row", key=f"{source}>{target}", priority=0, lines=(), data=data, confidence=conf)


def small_graph() -> dict[str, Any]:
    return {
        "nodes": [
            {"id": A, "label": "f", "path": "src/a.py", "conf": "exact"},
            {"id": B, "label": "B.g", "path": "src/b.py", "conf": "exact"},
        ],
        "edges": [{"source": A, "target": B, "label": "CALLS", "count": 1}],
    }


def dot(out: ToolOutput | None, **kw: Any) -> Rendered:
    return render_dot(make_input("dot", out, **kw))


def mermaid(out: ToolOutput | None, **kw: Any) -> Rendered:
    return render_mermaid(make_input("mermaid", out, **kw))


HEADER = "# query_call_hierarchy ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~0/2000tok"


def check_dot_syntax(text: str) -> None:
    """コメントを除いた本文で、``{}`` ``[]`` の対応、二重引用符の対応（``\\"`` は文字）、各文の ``;`` を確かめる。"""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    braces = brackets = 0
    in_str = False
    i = 0
    while i < len(body):
        c = body[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            assert c != "\n", "引用符の中に生の改行がある"
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            braces += 1
        elif c == "}":
            braces -= 1
        elif c == "[":
            brackets += 1
        elif c == "]":
            brackets -= 1
        assert braces >= 0 and brackets >= 0, f"閉じ括弧が先に来た: {body[: i + 1]!r}"
        i += 1
    assert not in_str, "二重引用符が閉じていない"
    assert braces == 0 and brackets == 0
    lines = body.splitlines()
    assert lines[0].startswith("digraph ") and lines[0].endswith("{")
    assert lines[-1] == "}"
    for line in lines[1:-1]:
        assert line.endswith(";"), f"文が ; で終わっていない: {line!r}"


# --- 期待文字列との一致 -----------------------------------------------------------------------


def test_dot_small_graph_exact() -> None:
    r = dot(tool_output(graph=small_graph()))
    assert not r.is_error and r.structured is None
    assert r.text == (
        f"// {HEADER}\n"
        'digraph "query_call_hierarchy" {\n'
        "  rankdir=LR;\n"
        "  node [shape=box];\n"
        f'  "{A}" [label="f" tooltip="src/a.py"];\n'
        f'  "{B}" [label="B.g" tooltip="src/b.py"];\n'
        f'  "{A}" -> "{B}" [label="CALLS"];\n'
        "}\n"
    )
    check_dot_syntax(r.text)


def test_mermaid_small_graph_exact() -> None:
    r = mermaid(tool_output(graph=small_graph()))
    assert not r.is_error and r.structured is None
    # ノード ID は短い別名。ラベルは qualified_name、URN の末尾が違う（アリティ・sighash つき）ときだけ 2 行目に出す
    assert r.text == (
        f'%% {HEADER}\nflowchart LR\n  n1["f"]\n  n2["B.g<br/>B.g/2~abcd1234"]\n  n1 -->|"CALLS"| n2\n'
    )


def test_header_is_the_compact_header_line() -> None:
    inp = make_input("dot", tool_output(graph=small_graph()))
    first_compact = compact.render(inp).text.splitlines()[0]
    assert dot(tool_output(graph=small_graph())).text.splitlines()[0] == f"// {first_compact}"
    assert mermaid(tool_output(graph=small_graph())).text.splitlines()[0] == f"%% {first_compact}"


# --- エスケープ -------------------------------------------------------------------------------


def test_dot_escapes_quote_backslash_hash_japanese_newline() -> None:
    graph = {
        "nodes": [{"id": 'lk://app/x.py#a"b', "label": 'say "hi" \\ #1 注文\n処理', "path": 'dir\\x "y".py'}],
        "edges": [{"source": 'lk://app/x.py#a"b', "target": "lk://app/日本語.py#関数", "label": 'CA"LLS'}],
    }
    r = dot(tool_output(graph=graph))
    check_dot_syntax(r.text)
    lines = r.text.splitlines()
    assert (
        '  "lk://app/x.py#a\\"b" [label="say \\"hi\\" \\\\ #1 注文 処理" tooltip="dir\\\\x \\"y\\".py"];'
        in lines
    )
    assert '  "lk://app/x.py#a\\"b" -> "lk://app/日本語.py#関数" [label="CA\\"LLS"];' in lines
    # 端点がノード表に無ければ URN から補う（ラベルはセレクタ、パスは URN のパス）
    assert '  "lk://app/日本語.py#関数" [label="関数" tooltip="日本語.py"];' in lines


def test_mermaid_escapes_and_aliases() -> None:
    graph = {
        "nodes": [{"id": 'lk://app/x.py#a"b', "label": 'say "hi" #1 <b>&% `x` 注文', "path": "x.py"}],
        "edges": [
            {
                "source": 'lk://app/x.py#a"b',
                "target": "lk://app/日本語.py#関数",
                "label": 'C|A"LLS',
                "count": 3,
            }
        ],
    }
    r = mermaid(tool_output(graph=graph))
    lines = r.text.splitlines()
    assert lines[1] == "flowchart LR"
    assert lines[2] == ('  n1["say #quot;hi#quot; #35;1 #lt;b#gt;#amp;#37; #96;x#96; 注文<br/>a#quot;b"]')
    assert lines[3] == '  n2["関数"]'
    assert lines[4] == '  n1 -->|"C|A#quot;LLS ×3"| n2'
    # ID は英数の別名だけ。URN の記号は ID に現れない
    ids = set(re.findall(r"^  (n\d+)[\[ ]", r.text, flags=re.MULTILINE))
    assert ids == {"n1", "n2"}
    assert "lk://" not in "\n".join(lines[1:])


# --- 降格 -------------------------------------------------------------------------------------


def test_degraded_node_is_marked_in_dot_and_mermaid() -> None:
    graph = {
        "nodes": [
            {"id": A, "label": "f", "path": "src/a.py", "conf": "exact"},
            {"id": C, "label": "h", "path": "src/c.py", "conf": "heuristic"},
        ],
        "edges": [{"source": A, "target": C, "label": "CALLS", "conf": "heuristic"}],
    }
    d = dot(tool_output(graph=graph), m=meta(degraded=True, level=Confidence.HEURISTIC))
    check_dot_syntax(d.text)
    assert f'  "{A}" [label="f" tooltip="src/a.py"];' in d.text
    assert f'  "{C}" [label="h" tooltip="src/c.py" conf="heuristic" style=dashed];' in d.text
    assert f'  "{A}" -> "{C}" [label="CALLS" conf="heuristic" style=dashed];' in d.text
    assert "// ! DEGRADED: confidence=heuristic engine=pyright+tree-sitter lang=python" in d.text
    assert "// ! Warning: 構文木のみで解決しました。" in d.text

    m = mermaid(tool_output(graph=graph), m=meta(degraded=True, level=Confidence.HEURISTIC))
    assert '  n1["f"]\n' in m.text
    assert '  n2["h"]:::degraded\n' in m.text
    assert '  n1 -.->|"CALLS"| n2\n' in m.text
    assert m.text.rstrip().endswith("classDef degraded stroke-dasharray:5 5,stroke:#b00020,color:#b00020")
    assert "%% ! DEGRADED:" in m.text


def test_no_degraded_class_when_nothing_is_degraded() -> None:
    assert "classDef" not in mermaid(tool_output(graph=small_graph())).text
    assert "conf=" not in dot(tool_output(graph=small_graph())).text.split("digraph", 1)[1]


def test_degraded_is_relative_to_tool_max_confidence() -> None:
    graph = {"nodes": [{"id": A, "label": "f", "conf": "structural"}], "edges": []}
    at_structural = tool_output(graph=graph, max_conf=Confidence.STRUCTURAL)
    assert "style=dashed" not in dot(at_structural).text
    assert ":::degraded" not in mermaid(at_structural).text
    at_exact = tool_output(graph=graph, max_conf=Confidence.EXACT)
    assert 'conf="structural" style=dashed' in dot(at_exact).text
    assert ":::degraded" in mermaid(at_exact).text


# --- ×N とガード ------------------------------------------------------------------------------


def test_count_and_duplicate_edges_become_one_edge_with_times_n() -> None:
    graph = {
        "nodes": [],
        "edges": [
            {"source": A, "target": B, "label": "CALLS"},
            {"source": A, "target": B, "label": "CALLS", "count": 2},
            {"source": A, "target": B, "label": "REFERENCES"},
        ],
    }
    d = dot(tool_output(graph=graph))
    assert f'  "{A}" -> "{B}" [label="CALLS ×3"];' in d.text
    assert f'  "{A}" -> "{B}" [label="REFERENCES"];' in d.text
    assert d.text.count(" -> ") == 2
    m = mermaid(tool_output(graph=graph))
    assert '  n1 -->|"CALLS ×3"| n2\n' in m.text
    assert '  n1 -->|"REFERENCES"| n2\n' in m.text


def test_guard_goes_to_second_label_line() -> None:
    graph = {
        "nodes": [],
        "edges": [
            {"source": A, "target": B, "label": "CALLS", "attrs": {"guard": 'req.amount > 0 && s == "x"'}}
        ],
    }
    d = dot(tool_output(graph=graph))
    check_dot_syntax(d.text)
    assert f'  "{A}" -> "{B}" [label="CALLS\\nwhen: req.amount > 0 && s == \\"x\\""];' in d.text
    m = mermaid(tool_output(graph=graph))
    assert '  n1 -->|"CALLS<br/>when: req.amount #gt; 0 #amp;#amp; s == #quot;x#quot;"| n2\n' in m.text


def test_edge_without_label_has_no_label_attribute() -> None:
    graph = {"nodes": [], "edges": [{"source": A, "target": B}]}
    assert f'  "{A}" -> "{B}";' in dot(tool_output(graph=graph)).text
    assert "  n1 --> n2\n" in mermaid(tool_output(graph=graph)).text


def test_dot_passes_only_safe_visual_attrs() -> None:
    graph = {
        "nodes": [
            {
                "id": A,
                "label": "f",
                "attrs": {
                    "shape": "ellipse",
                    "color": 'red"]; evil [',
                    "label": "x",
                    "penwidth": 2,
                    "bogus": "1",
                },
            }
        ],
        "edges": [],
    }
    d = dot(tool_output(graph=graph))
    check_dot_syntax(d.text)
    assert f'  "{A}" [label="f" tooltip="src/a.py" penwidth=2 shape=ellipse];' in d.text


# --- Record.data から組み立てる（T-20） --------------------------------------------------------


def test_graph_from_record_data() -> None:
    recs = [
        row(A, B, "CALLS"),
        row(A, B, "CALLS"),
        row(B, C, "IMPLEMENTS", conf=Confidence.LEXICAL_FALLBACK),
        row(C, A),
    ]
    out = tool_output(records=recs)
    d = dot(out)
    check_dot_syntax(d.text)
    assert d.text == (
        f"// {HEADER}\n"
        'digraph "query_call_hierarchy" {\n'
        "  rankdir=LR;\n"
        "  node [shape=box];\n"
        f'  "{A}" [label="f" tooltip="src/a.py"];\n'
        f'  "{B}" [label="B.g/2~abcd1234" tooltip="src/b.py"];\n'
        f'  "{C}" [label="h" tooltip="src/c.py"];\n'
        f'  "{A}" -> "{B}" [label="CALLS ×2"];\n'
        f'  "{B}" -> "{C}" [label="IMPLEMENTS" conf="lexical_fallback" style=dashed];\n'
        f'  "{C}" -> "{A}";\n'
        "}\n"
    )
    m = mermaid(out)
    assert m.text == (
        f"%% {HEADER}\n"
        "flowchart LR\n"
        '  n1["f"]\n'
        '  n2["B.g/2~abcd1234"]\n'
        '  n3["h"]\n'
        '  n1 -->|"CALLS ×2"| n2\n'
        '  n2 -.->|"IMPLEMENTS"| n3\n'
        "  n3 --> n1\n"
    )


def test_graph_from_page_records_when_page_is_given() -> None:
    out = tool_output(records=[row(A, B, "CALLS"), row(B, C, "CALLS")])
    page = Page(
        records=(row(A, B, "CALLS"),),
        next_cursor=None,
        summary=None,
        total_estimate=2,
        remaining=1,
        used_tokens_est=50,
        max_tokens=2000,
        truncated=True,
    )
    d = dot(out, page=page)
    assert d.text.count(" -> ") == 1
    assert "~50/2000tok" in d.text


def test_static_graph_wins_over_records() -> None:
    out = tool_output(graph=small_graph(), records=[row(B, C, "CALLS")])
    assert f'"{C}"' not in dot(out).text


# --- 入力が無い・失敗 --------------------------------------------------------------------------


@pytest.mark.parametrize(("render", "prefix"), [(render_dot, "// "), (render_mermaid, "%% ")])
def test_no_graph_data_is_invalid_argument(render: Any, prefix: str) -> None:
    kv = Record(kind="kv", key="k", priority=0, lines=("k: v",), data={"i": 1}, confidence=Confidence.EXACT)
    half = Record(
        kind="row", key="r", priority=0, lines=(), data={"source_urn": A}, confidence=Confidence.EXACT
    )
    for out in (tool_output(), tool_output(records=[kv]), tool_output(records=[half])):
        r = render(make_input("dot", out))
        assert r.is_error
        lines = r.text.splitlines()
        assert lines[0] == f"{prefix}# {TOOL} error E_INVALID_ARGUMENT"
        assert lines[1].startswith(f"{prefix}! ")
        assert all(line.startswith(prefix) for line in lines), "失敗応答はコメントだけ"


def test_no_output_at_all_is_invalid_argument() -> None:
    r = dot(None)
    assert r.is_error and r.text.startswith(f"// # {TOOL} error E_INVALID_ARGUMENT\n")


def test_empty_result_with_edge_columns_is_an_empty_graph() -> None:
    out = tool_output(
        extra_static={"columns": [{"name": "source_urn", "decl_type": "TEXT"}, {"name": "target_urn"}]}
    )
    d = dot(out)
    assert not d.is_error
    check_dot_syntax(d.text)
    assert d.text.endswith("  node [shape=box];\n}\n")
    assert mermaid(out).text.endswith("flowchart LR\n")
    # 列が足りなければ失敗
    assert dot(tool_output(extra_static={"columns": [{"name": "source_urn"}]})).is_error


@pytest.mark.parametrize(("render", "prefix"), [(render_dot, "// "), (render_mermaid, "%% ")])
def test_tool_error_is_rendered_as_comments(render: Any, prefix: str) -> None:
    err = LuciankeError(
        ErrorCode.E_TARGET_AMBIGUOUS,
        "候補が複数あります",
        candidates=[{"urn": A, "signature": "(x)", "path": "src/a.py", "line": 3}],
    )
    r = render(make_input("dot", None, error=err, warnings=["w1"]))
    assert r.is_error
    assert r.text == (
        f"{prefix}# {TOOL} error E_TARGET_AMBIGUOUS\n"
        f"{prefix}! 候補が複数あります\n"
        f"{prefix}! candidate: {A}  (x)  @src/a.py:3\n"
        f"{prefix}! w1\n"
    )


# --- ヘッダ・警告・続き ------------------------------------------------------------------------


def test_head_kv_warnings_and_page_trailers_are_comments() -> None:
    page = Page(
        records=(),
        next_cursor="eyJzaXRlIjozfQ",
        summary="depth 3: 42 more callers in 9 files",
        total_estimate=60,
        remaining=42,
        used_tokens_est=380,
        max_tokens=2000,
        truncated=True,
        warnings=("page-warning",),
    )
    out = tool_output(graph=small_graph(), head_kv=[("root", "[Method] " + A)])
    d = dot(out, page=page, warnings=["call-warning"])
    lines = d.text.splitlines()
    assert lines[:4] == [
        "// # query_call_hierarchy ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~380/2000tok",
        "// ! call-warning",
        "// ! page-warning",
        f"// root: [Method] {A}",
    ]
    assert lines[-2:] == [
        "// # summary: depth 3: 42 more callers in 9 files",
        "// # more: cursor=eyJzaXRlIjozfQ remaining≈42",
    ]
    check_dot_syntax(d.text)
    m = mermaid(out, page=page).text.splitlines()
    assert m[-1] == "%% # more: cursor=eyJzaXRlIjozfQ remaining≈42"
    assert m[-2] == "%% # summary: depth 3: 42 more callers in 9 files"


# --- 上限 -------------------------------------------------------------------------------------


def test_node_cap_drops_nodes_and_their_edges_and_reports_remaining() -> None:
    n = MAX_NODES + 5
    urns = [f"lk://app/m{i}.py#f{i}" for i in range(n)]
    graph = {
        "nodes": [{"id": u, "label": f"f{i}"} for i, u in enumerate(urns)],
        "edges": [{"source": urns[i], "target": urns[i + 1], "label": "CALLS"} for i in range(n - 1)],
    }
    d = dot(tool_output(graph=graph))
    check_dot_syntax(d.text)
    assert d.text.count("[label=") == MAX_NODES + (MAX_NODES - 1)  # ノード 500 + エッジ 499（端点が残るもの）
    assert d.text.count(" -> ") == MAX_NODES - 1
    last = d.text.splitlines()[-1]
    assert last == (
        f"// # summary: graph truncated at {MAX_NODES} nodes / {MAX_EDGES} edges: 5 more nodes, 5 more edges omitted"
    )
    m = mermaid(tool_output(graph=graph))
    assert m.text.count("-->") == MAX_NODES - 1
    assert m.text.splitlines()[-1].startswith("%% # summary: graph truncated at 500 nodes")


def test_edge_cap() -> None:
    nodes = [{"id": f"lk://app/m{i}.py#f{i}"} for i in range(50)]
    edges = [
        {"source": f"lk://app/m{i}.py#f{i}", "target": f"lk://app/m{j}.py#f{j}", "label": f"K{i}"}
        for i in range(50)
        for j in range(50)
        if i != j
    ]
    assert len(edges) > MAX_EDGES
    d = dot(tool_output(graph={"nodes": nodes, "edges": edges}))
    assert d.text.count(" -> ") == MAX_EDGES
    assert f"0 more nodes, {len(edges) - MAX_EDGES} more edges omitted" in d.text


def test_no_truncation_comment_for_small_graph() -> None:
    assert "truncated" not in dot(tool_output(graph=small_graph())).text


# --- dot の構文（括弧と引用符）を全ケースで ----------------------------------------------------


def test_malformed_graph_input_raises_value_error() -> None:
    with pytest.raises(ValueError, match="source / target"):
        dot(tool_output(graph={"nodes": [], "edges": [{"source": A}]}))
    with pytest.raises(ValueError, match="id"):
        dot(tool_output(graph={"nodes": [{"label": "x"}], "edges": []}))
