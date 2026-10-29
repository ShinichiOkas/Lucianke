"""T-06 ``find_references_and_implementations`` と T-07 ``query_call_hierarchy`` の契約テスト（structural 版）。

ワークスペースは ``tests/fixtures/poly``。シンボルは実ファイルの ``analyze_file`` で取り、``relations`` の行は **手で入れる**
（``syntax/relations.py`` の抽出器の出来に左右されないため。呼び出し位置はフィクスチャの実際の行から探す）。
``tmp_path`` の DB に ``ingest_file`` で取り込み、ツールを ``Pipeline`` 経由で呼ぶ（``db_path`` は ``services`` から渡す約束）。

事後条件（仕様書 §11 T-06 の 2〜5、T-07 の 1・3・4・5）を確かめる。事後条件 1 は LSP が要るので対象外。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.app import build_services
from lucianke.core.model import Confidence, Kind, RelPath, SymbolRecord, Tier
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import CodeSel, Urn
from lucianke.index.connection import open_writer
from lucianke.index.ingest import FileFacts, RelationFact, ingest_file, upsert_workspace
from lucianke.index.migrate import ensure_schema
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import t06_find_references_and_implementations as t06
from lucianke.tools import t07_query_call_hierarchy as t07
from lucianke.tools._services import ToolServices, analyze_file

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS = "poly"
PAY = "src/shop/services/payment.py"
LIM = "src/shop/services/limits.py"
ROUTES = "src/shop/api/routes.py"
TESTS = "tests/test_payment.py"
RST = "docs/api.rst"  # Tier 3。process_charge の語を含む（include_lexical_in_other_files 用）
FILES = (PAY, LIM, ROUTES, TESTS)
PROCESS_CHARGE = "PaymentProcessor.process_charge"
GUARD_CREATE = "req.amount > 0 and not paid"
GUARD_RETRY = "attempt < 3"


def U(path: str, qualified: str | None = None) -> str:
    return str(Urn(WS, path, CodeSel(qualified) if qualified else None))


EXT_DECIMAL = str(Urn.external("decimal", None, "Decimal"))
EXT_EXCEPTION = str(Urn.external("builtins", None, "Exception"))


def lines_of(path: str) -> list[str]:
    return (POLY / path).read_text(encoding="utf-8").splitlines()


def find_line(path: str, needle: str, nth: int = 0) -> int:
    """``needle`` を含む行のうち ``nth`` 番目の行番号（1 始まり）。"""
    hits = [i + 1 for i, text in enumerate(lines_of(path)) if needle in text]
    return hits[nth]


@dataclass(frozen=True)
class Edge:
    path: str
    needle: str
    source: str | None  # 囲みの qualified。None ならモジュール直下
    target: str  # URN
    kind: str = "CALLS"
    use: str | None = None
    guard: str | None = None
    nth: int = 0
    of: str | None = None  # 位置を取る語（needle の中のどこに列を置くか。省略は needle の先頭）


def fact(e: Edge) -> RelationFact:
    line = find_line(e.path, e.needle, e.nth)
    text = lines_of(e.path)[line - 1]
    col = text.index(e.of or e.needle)
    return RelationFact(
        source_urn=U(e.path, e.source),
        target_urn=e.target,
        relation_kind=e.kind,
        call_site_line=line - 1,
        call_site_col=col,
        use_kind=e.use,
        guard_condition=e.guard,
        confidence=Confidence.STRUCTURAL,
    )


def hand_edges() -> list[Edge]:
    pc = U(PAY, PROCESS_CHARGE)
    validate = U(PAY, "PaymentProcessor._validate_limit")
    return [
        # process_charge の呼び出し元（routes に 2 箇所、テストに 1 箇所）
        Edge(
            ROUTES,
            "await processor.process_charge(req)",
            "create_order",
            pc,
            use="call",
            guard=GUARD_CREATE,
            of="process_charge",
        ),
        Edge(
            ROUTES,
            "await processor.process_charge(req)",
            "retry_order",
            pc,
            use="call",
            guard=GUARD_RETRY,
            nth=1,
            of="process_charge",
        ),
        Edge(TESTS, "await p.process_charge(", "test_charge_ok", pc, use="call", of="process_charge"),
        # その先
        Edge(
            PAY,
            "self._validate_limit(req.amount)",
            PROCESS_CHARGE,
            validate,
            use="call",
            of="_validate_limit",
        ),
        Edge(
            PAY,
            "raise PaymentDeclined(",
            PROCESS_CHARGE,
            U(PAY, "PaymentDeclined"),
            use="call",
            of="PaymentDeclined",
        ),
        Edge(
            PAY,
            "self._limits.get(self.currency)",
            "PaymentProcessor._validate_limit",
            U(LIM, "LimitTable.get"),
            use="call",
            of="get",
        ),
        Edge(
            PAY,
            "raise LimitExceeded(",
            "PaymentProcessor._validate_limit",
            U(LIM, "LimitExceeded"),
            use="call",
            of="LimitExceeded",
        ),
        # use_kind を NULL で入れて、既定（relation_kind から）を確かめる
        Edge(
            PAY,
            "from shop.services.limits import",
            None,
            U(LIM, "LimitExceeded"),
            kind="IMPORTS",
            of="LimitExceeded",
        ),
        Edge(
            PAY,
            "class PaymentDeclined(Exception)",
            "PaymentDeclined",
            EXT_EXCEPTION,
            kind="INHERITS",
            of="Exception",
        ),
        Edge(
            LIM,
            "class LimitExceeded(Exception)",
            "LimitExceeded",
            EXT_EXCEPTION,
            kind="INHERITS",
            of="Exception",
        ),
        # routes
        Edge(
            ROUTES,
            "req = PaymentRequest(order_id=order_id",
            "create_order",
            U(PAY, "PaymentRequest"),
            use="call",
            nth=0,
            of="PaymentRequest",
        ),
        Edge(
            ROUTES,
            "processor = PaymentProcessor(LimitTable())",
            None,
            U(PAY, "PaymentProcessor"),
            use="call",
            of="PaymentProcessor",
        ),
        Edge(
            TESTS,
            "p = PaymentProcessor(LimitTable())",
            "test_charge_ok",
            U(PAY, "PaymentProcessor"),
            use="call",
            of="PaymentProcessor",
        ),
        # 循環（retry ⇄ schedule）
        Edge(
            LIM,
            "return schedule(attempt + 1)",
            "retry",
            U(LIM, "schedule"),
            use="call",
            guard=None,
            of="schedule",
        ),
        Edge(LIM, "return retry(attempt)", "schedule", U(LIM, "retry"), use="call", of="retry"),
    ]


def decimal_edges(symbols: dict[str, Sequence[SymbolRecord]]) -> list[RelationFact]:
    """``Decimal`` を含む実際の行から、外部クラス ``Decimal`` への関係を作る（ページネーションと ×N の確認用）。"""
    out: list[RelationFact] = []
    for path in FILES:
        spans = [
            (s.range.line_span_external(), s)
            for s in symbols[path]
            if s.kind not in (Kind.VARIABLE, Kind.CONSTANT, Kind.FIELD)
        ]
        for i, text in enumerate(lines_of(path)):
            m = re.search(r"\bDecimal\b", text)
            if m is None:
                continue
            inside = [(b - a, s) for (a, b), s in spans if a <= i + 1 <= b]
            enclosing = min(inside, key=lambda t: t[0])[1] if inside else None
            kind = (
                "IMPORTS"
                if text.lstrip().startswith(("from ", "import "))
                else ("CALLS" if "Decimal(" in text else "TYPE_USES")
            )
            src = str(enclosing.urn) if enclosing is not None and kind != "IMPORTS" else U(path)
            out.append(
                RelationFact(
                    source_urn=src,
                    target_urn=EXT_DECIMAL,
                    relation_kind=kind,
                    call_site_line=i,
                    call_site_col=m.start(),
                    use_kind=None,
                    confidence=Confidence.STRUCTURAL,
                )
            )
    return out


# --- 索引の組み立て ---------------------------------------------------------------------------


def build_db(db: Path) -> None:
    svc = build_services(lambda: [str(POLY)])
    ws = svc.workspace()

    async def analyse() -> dict[str, Any]:
        return {p: await analyze_file(svc, ws, p) for p in FILES}

    analysed = asyncio.run(analyse())
    by_file = {p: an.symbols for p, an in analysed.items()}
    relations: dict[str, list[RelationFact]] = {p: [] for p in FILES}
    for e in hand_edges():
        relations[e.path].append(fact(e))
    for rf in decimal_edges(by_file):
        relations[str(parse_path(rf.source_urn))].append(rf)

    conn = open_writer(db)
    try:
        ensure_schema(conn)
        tx(conn, lambda c: upsert_workspace(c, {"workspace_id": WS, "root_path": str(POLY)}))
        for p in FILES:
            an = analysed[p]
            facts = FileFacts(
                workspace_id=WS,
                path=an.rel,
                language=an.language_id,
                tier=an.tier,
                line_count=an.line_count,
                byte_size=len(an.data),
                sha256=an.sha256,
                is_test=p.startswith("tests/"),
                confidence=Confidence.STRUCTURAL,
                symbols=an.symbols,
                relations=relations[p],
            )
            tx(conn, lambda c, f=facts: ingest_file(c, f))
        rst = (POLY / RST).read_bytes()
        rst_facts = FileFacts(
            workspace_id=WS,
            path=RelPath(RST),
            language="rst",
            tier=Tier.T3,
            line_count=LineIndex(rst).content_line_count,
            byte_size=len(rst),
            sha256=hashlib.sha256(rst).hexdigest(),
            is_test=False,
            confidence=Confidence.LEXICAL_FALLBACK,
        )
        tx(conn, lambda c: ingest_file(c, rst_facts))
    finally:
        conn.close()


def parse_path(urn: str) -> str:
    return urn.removeprefix("lk://").partition("/")[2].partition("#")[0]


def tx(conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], Any]) -> Any:
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(conn)
        conn.execute("UPDATE _meta SET data_version = data_version + 1")
        conn.execute("COMMIT")
        return result
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("paad") / "paad.sqlite"
    build_db(path)
    return path


@pytest.fixture
def pipeline(db_path: Path) -> Pipeline:
    registry = ToolRegistry()
    registry.register(t06.TOOL_SPEC)
    registry.register(t07.TOOL_SPEC)
    base = build_services(lambda: [str(POLY)])

    @dataclass(slots=True)
    class Services(ToolServices):
        db_path: Path | None = None

    svc = Services(roots=base.roots, trees=base.trees, lines=base.lines, db_path=db_path)
    return Pipeline(registry, services=svc)


async def refs(pipeline: Pipeline, target: dict[str, Any], fmt: str = "json", **args: Any):
    return await pipeline.call_tool(
        "find_references_and_implementations", {"target": target, "format": fmt, **args}
    )


async def calls(pipeline: Pipeline, target: dict[str, Any], direction: str, fmt: str = "json", **args: Any):
    return await pipeline.call_tool(
        "query_call_hierarchy", {"target": target, "direction": direction, "format": fmt, **args}
    )


PC = {"urn": U(PAY, PROCESS_CHARGE)}


# --- T-06 ---------------------------------------------------------------------------------------


async def test_tools_are_registered(pipeline: Pipeline) -> None:
    names = {d.name for d in pipeline.registry.descriptors()}
    assert {"find_references_and_implementations", "query_call_hierarchy"} <= names


async def test_t06_references_of_process_charge(pipeline: Pipeline) -> None:
    """process_charge の参照は routes.py の 2 箇所（use=call）と tests/test_payment.py の 1 箇所。"""
    r = await refs(pipeline, PC)
    assert not r.is_error, r.text
    data = r.structured["data"]
    got = [(x["path"], x["line"], x["use"], x["enclosing_name"]) for x in data["references"]]
    assert got == [
        (ROUTES, find_line(ROUTES, "await processor.process_charge(req)", 0), "call", "create_order"),
        (ROUTES, find_line(ROUTES, "await processor.process_charge(req)", 1), "call", "retry_order"),
        (TESTS, find_line(TESTS, "p.process_charge("), "call", "test_charge_ok"),
    ]
    assert data["totals"] == {"files": 2, "sites": 3, "tests": 1}
    assert data["target"]["qualified_name"] == PROCESS_CHARGE
    first = data["references"][0]
    assert first["snippet"] == "receipt = await processor.process_charge(req)"
    assert first["col"] == lines_of(ROUTES)[first["line"] - 1].index("process_charge") + 1
    assert first["confidence_level"] == "structural" and first["is_test"] is False
    assert data["references"][2]["is_test"] is True
    assert data["implementations"] == [] and data["type_hierarchy"] is None


async def test_t06_compact_matches_the_grammar(pipeline: Pipeline) -> None:
    r = await refs(pipeline, {"path": PAY, "symbol_name": "process_charge"}, "compact")
    assert not r.is_error and g.validate(r.text) == [], g.validate(r.text)
    text = r.text
    assert f"target: [Method] {U(PAY, PROCESS_CHARGE)}" in text
    assert "implementations: (none; concrete method)" in text
    assert "type_hierarchy: (n/a)" in text
    assert "references: 2 files, 3 sites (tests: 1)" in text
    line = find_line(ROUTES, "await processor.process_charge(req)", 0)
    assert (
        f"{ROUTES}:{line}:" in text
        and "[call] in create_order  receipt = await processor.process_charge(req)" in text
    )
    assert "! DEGRADED: confidence=structural" in text  # LSP が無いので降格


async def test_t06_postcondition_2_use_is_one_of_seven(pipeline: Pipeline) -> None:
    """use は 7 値のいずれか。NULL の use_kind は relation_kind から決める（IMPORTS→import、INHERITS→impl）。"""
    uses = set()
    for target in (PC, {"urn": U(LIM, "LimitExceeded")}, {"urn": EXT_DECIMAL}, {"urn": EXT_EXCEPTION}):
        r = await refs(pipeline, target)
        assert not r.is_error, r.text
        uses |= {x["use"] for x in r.structured["data"]["references"]}
    assert uses <= {"r", "w", "rw", "call", "type", "import", "impl"}
    assert {"call", "type", "import", "impl"} <= uses
    imp = await refs(pipeline, {"urn": U(LIM, "LimitExceeded")}, use_kinds=["import"])
    assert [x["use"] for x in imp.structured["data"]["references"]] == ["import"]


async def test_t06_postcondition_3_include_tests_false(pipeline: Pipeline) -> None:
    r = await refs(pipeline, PC, include_tests=False)
    data = r.structured["data"]
    assert [x["path"] for x in data["references"]] == [ROUTES, ROUTES]
    assert not any(x["is_test"] for x in data["references"]) and data["totals"]["tests"] == 0


async def test_t06_postcondition_4_5_pages_have_no_duplicates_and_fit_the_budget(pipeline: Pipeline) -> None:
    full = await refs(pipeline, {"urn": EXT_DECIMAL}, max_tokens=8000)
    expected = [(x["path"], x["line"], x["col"]) for x in full.structured["data"]["references"]]
    assert len(expected) >= 10 and len(set(expected)) == len(expected)
    seen: list[tuple[str, int, int]] = []
    cursor: str | None = None
    pages = 0
    while True:
        args: dict[str, Any] = {"max_tokens": 200}
        if cursor is not None:
            args["cursor"] = cursor
        r = await refs(pipeline, {"urn": EXT_DECIMAL}, **args)
        assert not r.is_error, r.text
        env = r.structured
        assert env["budget"]["used_tokens_est"] <= 200
        seen += [(x["path"], x["line"], x["col"]) for x in env["data"]["references"]]
        pages += 1
        cursor = env["page"]["next_cursor"]
        if cursor is None:
            break
        assert pages < 50
    assert pages >= 2
    assert len(seen) == len(set(seen)), "同じ (path, line, col) が複数のページに現れた"
    assert seen == expected  # ページを足すと全体と等しく、並びも (path, line, col) の順


async def test_t06_target_forms_and_errors(pipeline: Pipeline) -> None:
    line = find_line(PAY, "return Receipt(")
    for target in (
        {"urn": U(PAY, PROCESS_CHARGE)},
        {"path": PAY, "symbol_name": "PaymentProcessor.process_charge"},
        {"path": PAY, "symbol_name": "process_charge"},
        {"path": PAY, "line": line},
    ):
        r = await refs(pipeline, target)
        assert not r.is_error, (target, r.text)
        assert r.structured["data"]["target"]["qualified_name"] == PROCESS_CHARGE
    missing = await refs(pipeline, {"path": PAY, "symbol_name": "NoSuchThing"})
    assert missing.is_error and missing.structured["error"]["code"] == "E_TARGET_NOT_FOUND"
    bad_use = await refs(pipeline, PC, use_kinds=["bogus"])
    assert bad_use.is_error and bad_use.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    outside = await refs(pipeline, PC, scope_paths=["../elsewhere"])
    assert outside.is_error and outside.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    other_rev = await refs(pipeline, PC, revision="HEAD")
    assert other_rev.is_error and other_rev.structured["error"]["code"] == "E_UNSUPPORTED"


async def test_t06_scope_paths_and_declaration(pipeline: Pipeline) -> None:
    only_tests = await refs(pipeline, PC, scope_paths=["tests"])
    assert [x["path"] for x in only_tests.structured["data"]["references"]] == [TESTS]
    r = await refs(pipeline, PC, include_declaration=True)
    sites = [(x["path"], x["line"]) for x in r.structured["data"]["references"]]
    decl = (PAY, find_line(PAY, "async def process_charge"))
    assert decl in sites and len(sites) == 4
    no_snippet = await refs(pipeline, PC, "compact", context_snippet=False)
    assert g.validate(no_snippet.text) == []
    assert "process_charge(req)" not in no_snippet.text.split("references:", 1)[1]


async def test_t06_type_hierarchy_and_implementations(pipeline: Pipeline) -> None:
    r = await refs(pipeline, {"urn": EXT_EXCEPTION}, query_type="type_hierarchy")
    subs = r.structured["data"]["type_hierarchy"]["subtypes"]
    assert [s["qualified_name"] for s in subs] == [
        "LimitExceeded",
        "PaymentDeclined",
    ]  # パスの順（limits < payment）
    sup = await refs(pipeline, {"urn": U(LIM, "LimitExceeded")}, query_type="type_hierarchy")
    assert [s["qualified_name"] for s in sup.structured["data"]["type_hierarchy"]["supertypes"]] == [
        "Exception"
    ]
    assert sup.structured["data"]["references"] == []  # references は要求していない
    text = (await refs(pipeline, {"urn": EXT_EXCEPTION}, "compact")).text
    assert g.validate(text) == [] and "type_hierarchy: supertypes (none); subtypes 2: LimitExceeded @" in text


# --- グラフ（T-06・T-07 共通） --------------------------------------------------------------------


async def test_t06_graph_edges_merge_into_times_n(pipeline: Pipeline) -> None:
    """同じ囲みからの複数参照は 1 エッジに ×N（apply_discount → Decimal の型注釈 2 行）。"""
    dot = await refs(pipeline, {"urn": EXT_DECIMAL}, "dot")
    assert not dot.is_error, dot.text
    assert dot.text.startswith("// # find_references_and_implementations ok")
    assert "digraph" in dot.text and "type ×2" in dot.text
    assert f'"{U(PAY, "PaymentProcessor.apply_discount")}" -> "{EXT_DECIMAL}"' in dot.text
    mm = await refs(pipeline, PC, "mermaid")
    assert not mm.is_error and mm.text.startswith("%% # find_references_and_implementations ok")
    assert "flowchart LR" in mm.text and '-->|"call"|' in mm.text


# --- T-07 ---------------------------------------------------------------------------------------


async def test_t07_incoming_depth1_has_when_strings(pipeline: Pipeline) -> None:
    """process_charge の incoming に create_order（when: req.amount > 0 and not paid）。"""
    r = await calls(pipeline, PC, "incoming", depth=1)
    assert not r.is_error, r.text
    nodes = r.structured["data"]["nodes"]
    by_name = {n["name"]: n for n in nodes}
    assert set(by_name) == {"create_order", "retry_order", "test_charge_ok"}
    assert by_name["create_order"]["when"] == GUARD_CREATE
    assert by_name["retry_order"]["when"] == GUARD_RETRY
    assert by_name["test_charge_ok"]["when"] is None and by_name["test_charge_ok"]["is_test"] is True
    assert by_name["create_order"]["call_site_path"] == ROUTES
    assert by_name["create_order"]["call_site_line"] == find_line(
        ROUTES, "await processor.process_charge(req)", 0
    )
    assert all(n["depth"] == 1 and n["parent_urn"] == U(PAY, PROCESS_CHARGE) for n in nodes)
    assert r.structured["data"]["root"]["qualified_name"] == PROCESS_CHARGE
    c = await calls(pipeline, PC, "incoming", "compact", depth=1)
    assert g.validate(c.text) == [], g.validate(c.text)
    line = find_line(ROUTES, "await processor.process_charge(req)", 0)
    assert f"<- create_order @{ROUTES}:{line}  when: {GUARD_CREATE}" in c.text
    assert f"root: [Method] {U(PAY, PROCESS_CHARGE)} direction=incoming depth=1" in c.text


async def test_t07_postcondition_1_depth_bound_and_preorder(pipeline: Pipeline) -> None:
    target = {"urn": U(PAY, "PaymentProcessor._validate_limit")}
    for depth in (1, 2, 3):
        r = await calls(pipeline, target, "incoming", depth=depth)
        nodes = r.structured["data"]["nodes"]
        assert max(n["depth"] for n in nodes) <= depth
    r = await calls(pipeline, target, "incoming", depth=2)
    nodes = r.structured["data"]["nodes"]
    assert [(n["name"], n["depth"]) for n in nodes] == [
        ("PaymentProcessor.process_charge", 1),
        ("create_order", 2),
        ("retry_order", 2),
        ("test_charge_ok", 2),
    ]  # 深さ優先の前順（親の直後に子）
    c = await calls(pipeline, target, "incoming", "compact", depth=2)
    assert g.validate(c.text) == []
    body = [ln for ln in c.text.splitlines() if "<-" in ln]
    assert body[0].startswith("<- PaymentProcessor.process_charge @") and body[1].startswith(
        "  <- create_order @"
    )


async def test_t07_postcondition_3_cycle_is_shown_once_and_not_expanded(pipeline: Pipeline) -> None:
    for direction in ("outgoing", "incoming"):
        r = await calls(pipeline, {"urn": U(LIM, "retry")}, direction, depth=5)
        nodes = r.structured["data"]["nodes"]
        assert [(n["name"], n["depth"], n["cycle"]) for n in nodes] == [
            ("schedule", 1, False),
            ("retry", 2, True),
        ]
        cyc = nodes[1]
        assert not any(n["depth"] > cyc["depth"] for n in nodes)  # (cycle) の下に子はいない
    c = await calls(pipeline, {"urn": U(LIM, "retry")}, "outgoing", "compact", depth=5)
    assert g.validate(c.text) == []
    assert re.search(r"^  -> retry @src/shop/services/limits.py:\d+ \(cycle\)$", c.text, re.M), c.text
    # 同じ経路上で同じ URN は 2 回以上展開されない
    both = await calls(pipeline, {"urn": U(LIM, "schedule")}, "both", depth=5)
    for direction in ("incoming", "outgoing"):
        path_urns = [
            n["urn"]
            for n in both.structured["data"]["nodes"]
            if n["direction"] == direction and not n["cycle"]
        ]
        assert len(path_urns) == len(set(path_urns)) and U(LIM, "schedule") not in path_urns


async def test_t07_options(pipeline: Pipeline) -> None:
    no_guard = await calls(pipeline, PC, "incoming", "compact", depth=1, include_guard_conditions=False)
    assert "when:" not in no_guard.text
    no_tests = await calls(pipeline, PC, "incoming", depth=1, include_tests=False)
    assert {n["name"] for n in no_tests.structured["data"]["nodes"]} == {"create_order", "retry_order"}
    few = await calls(pipeline, PC, "incoming", "compact", depth=1, max_children=2)
    assert (
        "children: incoming 1 more" in few.text
        and len([ln for ln in few.text.splitlines() if ln.startswith("<-")]) == 2
    )
    ext = await calls(pipeline, {"urn": U(ROUTES, "create_order")}, "outgoing", "compact", depth=3)
    assert g.validate(ext.text) == [] and "-> Decimal (external) @" in ext.text
    ext_json = (await calls(pipeline, {"urn": U(ROUTES, "create_order")}, "outgoing", depth=3)).structured[
        "data"
    ]["nodes"]
    leaf = next(n for n in ext_json if n["is_external"])
    assert leaf["urn"] == EXT_DECIMAL and not any(n["parent_urn"] == EXT_DECIMAL for n in ext_json)


async def test_t07_class_target_starts_from_constructor_calls(pipeline: Pipeline) -> None:
    r = await calls(pipeline, {"urn": U(PAY, "PaymentProcessor")}, "incoming", depth=1)
    assert not r.is_error, r.text
    names = [n["name"] for n in r.structured["data"]["nodes"]]
    assert "test_charge_ok" in names and any("(module)" in n for n in names)


async def test_t07_errors(pipeline: Pipeline) -> None:
    var = await calls(pipeline, {"urn": U(ROUTES, "processor")}, "incoming")
    assert var.is_error and var.structured["error"]["code"] == "E_UNSUPPORTED"
    no_direction = await pipeline.call_tool("query_call_hierarchy", {"target": PC, "format": "json"})
    assert no_direction.is_error and no_direction.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    too_deep = await calls(pipeline, PC, "incoming", depth=6)
    assert too_deep.is_error and too_deep.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    bad_children = await calls(pipeline, PC, "incoming", max_children=0)
    assert bad_children.is_error and bad_children.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    missing = await calls(pipeline, {"path": PAY, "symbol_name": "nope"}, "incoming")
    assert missing.is_error and missing.structured["error"]["code"] == "E_TARGET_NOT_FOUND"


async def test_t07_postcondition_5_budget(pipeline: Pipeline) -> None:
    for depth in (1, 3, 5):
        r = await calls(pipeline, PC, "both", depth=depth, max_tokens=200)
        assert r.structured["budget"]["used_tokens_est"] <= 200
    again = await calls(pipeline, PC, "both", depth=5, max_tokens=200)
    cursor = again.structured["page"]["next_cursor"]
    seen = [n["urn"] + str(n["call_site_line"]) + n["direction"] for n in again.structured["data"]["nodes"]]
    while cursor:
        nxt = await calls(pipeline, PC, "both", depth=5, max_tokens=200, cursor=cursor)
        assert nxt.structured["budget"]["used_tokens_est"] <= 200
        seen += [
            n["urn"] + str(n["call_site_line"]) + n["direction"] for n in nxt.structured["data"]["nodes"]
        ]
        cursor = nxt.structured["page"]["next_cursor"]
    full = await calls(pipeline, PC, "both", depth=5, max_tokens=8000)
    assert len(seen) == len(full.structured["data"]["nodes"])


async def test_t07_graph_output_draws(pipeline: Pipeline) -> None:
    dot = await calls(pipeline, PC, "incoming", "dot", depth=1)
    assert not dot.is_error, dot.text
    assert dot.text.startswith("// # query_call_hierarchy ok") and "digraph" in dot.text
    assert f'"{U(ROUTES, "create_order")}" -> "{U(PAY, PROCESS_CHARGE)}"' in dot.text
    assert f"when: {GUARD_CREATE}" in dot.text
    mm = await calls(pipeline, {"urn": U(LIM, "retry")}, "outgoing", "mermaid", depth=3)
    assert not mm.is_error and "flowchart LR" in mm.text and "CALLS" in mm.text
    # 循環はグラフでは 2 ノード・2 エッジの輪になる
    assert mm.text.count("-->") == 2


async def test_t06_lexical_refs_in_other_files_are_marked_and_mixed(pipeline: Pipeline) -> None:
    """include_lexical_in_other_files: Tier 3 のファイルの単語一致が lexical_fallback で加わる（応答は mixed）。"""
    plain = await refs(pipeline, PC)
    assert not any(x["path"] == RST for x in plain.structured["data"]["references"])
    r = await refs(pipeline, PC, include_lexical_in_other_files=True)
    assert not r.is_error, r.text
    rows = r.structured["data"]["references"]
    lex = [x for x in rows if x["path"] == RST]
    assert len(lex) == 1 and lex[0]["confidence_level"] == "lexical_fallback"
    assert lex[0]["line"] == find_line(RST, "process_charge") and lex[0]["use"] == "r"
    assert "process_charge" in lex[0]["snippet"]
    meta = r.structured["_resolution_meta"]
    assert meta["mixed"] is True and meta["confidence_level"] == "lexical_fallback"
    assert meta["confidence_breakdown"] == {"structural": 3, "lexical_fallback": 1}
    assert len(rows) == 4 and rows == sorted(rows, key=lambda x: (x["path"], x["line"], x["col"]))
    c = await refs(pipeline, PC, "compact", include_lexical_in_other_files=True)
    assert g.validate(c.text) == [], g.validate(c.text)
    assert re.search(rf"^{RST}:\d+:\d+  \[r\] .*  conf=lexical_fallback$", c.text, re.M), c.text
