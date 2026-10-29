"""T-06 ``find_references_and_implementations`` と T-07 ``query_call_hierarchy`` の契約テスト（structural 版）。

ワークスペースは ``tests/fixtures/poly``。シンボルは実ファイルの ``analyze_file`` で取り、``relations`` の行は **手で入れる**
（``syntax/relations.py`` の抽出器の出来に左右されないため。呼び出し位置はフィクスチャの実際の行から探す）。
``tmp_path`` の DB に ``ingest_file`` で取り込み、ツールを ``Pipeline`` 経由で呼ぶ（``db_path`` は ``services`` から渡す約束）。

事後条件（仕様書 §11 T-06 の 2〜6、T-07 の 1・3〜6）を確かめる。T-06 の 1・T-07 の 2 は LSP が要るので ``test_T06_T07_lsp.py``。
番号は仕様書の事後条件の並び（1 始まり）。名前の番号か ``PC:T-06#4`` の印で名乗る（``bench/postcondition_map.py``、BUG-0040）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
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
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool, open_reader, open_writer
from lucianke.index.ingest import FileFacts, RelationFact, ingest_file, upsert_workspace
from lucianke.index.migrate import ensure_schema
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t06_find_references_and_implementations as t06
from lucianke.tools import t07_query_call_hierarchy as t07
from lucianke.tools._services import ToolServices, analyze_file
from lucianke.workspace.types import Workspace

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


async def test_t06_postcondition_4_6_pages_have_no_duplicates_and_fit_the_budget(pipeline: Pipeline) -> None:
    """PC:T-06#4 全ページを通じて同じ (path, line, col) は 1 度だけ。PC:T-06#6 各ページの used_tokens_est は max_tokens 以下。"""
    full = await refs(pipeline, {"urn": EXT_DECIMAL}, max_tokens=8000)
    expected = [(x["path"], x["line"], x["col"]) for x in full.structured["data"]["references"]]
    assert len(expected) >= 10 and len(set(expected)) == len(expected)
    seen: list[tuple[str, int, int]] = []
    cursor: str | None = None
    pages = 0
    while True:
        args: dict[str, Any] = {"max_tokens": 600}  # json は封筒と target・totals など固定部（約 350 トークン）も数える（BUG-0017）
        if cursor is not None:
            args["cursor"] = cursor
        r = await refs(pipeline, {"urn": EXT_DECIMAL}, **args)
        assert not r.is_error, r.text
        env = r.structured
        assert env["budget"]["used_tokens_est"] <= 600
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
    # フィクスチャ（poly）は Git のワーキングコピーではない: HEAD は解決できず E_REVISION_NOT_FOUND（BUG-0018）。
    # 解決できる参照の E_UNSUPPORTED は tests/contract/test_revision_reads.py（実リポジトリ）で確かめる。
    other_rev = await refs(pipeline, PC, revision="HEAD")
    assert other_rev.is_error and other_rev.structured["error"]["code"] == "E_REVISION_NOT_FOUND"


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


async def test_t07_postcondition_3_incoming_depth1_has_when_strings(pipeline: Pipeline) -> None:
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


async def test_t07_postcondition_4_cycle_is_shown_once_and_not_expanded(pipeline: Pipeline) -> None:
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


async def test_t07_postcondition_5_unresolved_import_callee_is_an_external_leaf(pipeline: Pipeline) -> None:
    """解決できない import の呼び先は ``is_external: true`` の葉（``lk://_ext/<pkg>#<name>``）として nodes[] に現れ、子を持たない。"""
    r = await calls(pipeline, {"urn": U(ROUTES, "create_order")}, "outgoing", depth=3)
    assert not r.is_error, r.text
    nodes = r.structured["data"]["nodes"]
    externals = [n for n in nodes if n["is_external"]]
    assert EXT_DECIMAL in {n["urn"] for n in externals}
    assert all(n["urn"].startswith("lk://_ext/") for n in externals)
    ext_urns = {n["urn"] for n in externals}
    assert not any(n["parent_urn"] in ext_urns for n in nodes)  # 外部の葉は展開されない
    assert all(n["cycle"] is False for n in externals)
    c = await calls(pipeline, {"urn": U(ROUTES, "create_order")}, "outgoing", "compact", depth=3)
    assert g.validate(c.text) == [] and "-> Decimal (external) @" in c.text


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


async def test_t07_postcondition_6_budget(pipeline: Pipeline) -> None:
    for depth in (1, 3, 5):
        r = await calls(pipeline, PC, "both", depth=depth, max_tokens=1100)  # json は封筒と root・edges など固定部（約 860 トークン）も数える（BUG-0017）
        assert r.structured["budget"]["used_tokens_est"] <= 1100
    again = await calls(pipeline, PC, "both", depth=5, max_tokens=1100)
    cursor = again.structured["page"]["next_cursor"]
    seen = [n["urn"] + str(n["call_site_line"]) + n["direction"] for n in again.structured["data"]["nodes"]]
    while cursor:
        nxt = await calls(pipeline, PC, "both", depth=5, max_tokens=1100, cursor=cursor)
        assert nxt.structured["budget"]["used_tokens_est"] <= 1100
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


# --- 実索引（Indexer が F-POLY から作った relations）で T-06・T-07 を呼ぶ ----------------------------------
#
# 上のテストは手書きの RelationFact で、抽出器の出来に左右されない。ここは抽出器（syntax/relations.py）と取り込み
# （engine/indexer.py）が、T-06・T-07 の求める relations を実際に作ることを確かめる（7j）。

TS_CLIENT = "web/src/payment.ts"
TS_APP = "web/src/app.ts"


@pytest.fixture(scope="module")
def real_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("real")
    root = base / WS  # ワークスペース ID はフォルダ名（poly）
    shutil.copytree(POLY, root)
    db = base / "db" / "paad.sqlite"

    async def build() -> None:
        writer = IndexWriter(db)
        readers = ReaderPool(db, size=2)
        indexer = Indexer(Workspace(workspace_id=WS, root=root.resolve()), writer, readers, TreeCache())
        try:
            report = await indexer.scan()
            assert not report.errors, report.errors
        finally:
            await indexer.aclose()
            readers.close()
            writer.close()

    asyncio.run(build())
    return db


@pytest.fixture
def real(real_db: Path) -> Pipeline:
    registry = ToolRegistry()
    registry.register(t06.TOOL_SPEC)
    registry.register(t07.TOOL_SPEC)
    base = build_services(lambda: [str(real_db.parent.parent / WS)])

    @dataclass(slots=True)
    class Services(ToolServices):
        db_path: Path | None = None

    svc = Services(roots=base.roots, trees=base.trees, lines=base.lines, db_path=real_db)
    return Pipeline(registry, services=svc)


def sql(db: Path, query: str, *args: object) -> list[tuple[Any, ...]]:
    conn = open_reader(db)
    try:
        return [tuple(r) for r in conn.execute(query, args).fetchall()]
    finally:
        conn.close()


async def test_real_incoming_calls_of_process_charge_have_enclosing_definitions_and_when(
    real: Pipeline,
) -> None:
    """(a)(b): 呼び出し元は最内の定義（関数・メソッド）、when は呼び出しを囲む if の条件。PC:T-07#3（実索引の ``when:``）"""
    r = await calls(real, PC, "incoming", depth=1)
    assert not r.is_error, r.text
    by_name = {n["name"]: n for n in r.structured["data"]["nodes"]}
    assert set(by_name) == {"create_order", "retry_order", "test_charge_ok"}
    assert by_name["create_order"]["when"] == GUARD_CREATE
    assert by_name["retry_order"]["when"] == GUARD_RETRY
    assert by_name["test_charge_ok"]["when"] is None and by_name["test_charge_ok"]["is_test"] is True
    assert by_name["create_order"]["call_site_path"] == ROUTES
    line = find_line(ROUTES, "await processor.process_charge(req)", 0)
    assert by_name["create_order"]["call_site_line"] == line
    c = await calls(real, PC, "incoming", "compact", depth=1)
    assert g.validate(c.text) == [], g.validate(c.text)
    assert f"  when: {GUARD_CREATE}" in c.text


async def test_real_call_hierarchy_create_order_to_process_charge_and_beyond(real: Pipeline) -> None:
    """create_order → process_charge → _validate_limit / PaymentDeclined（when は途中の if の条件）。"""
    r = await calls(real, {"urn": U(ROUTES, "create_order")}, "outgoing", depth=3, exclude_external=False, max_tokens=8000)
    assert not r.is_error, r.text
    nodes = r.structured["data"]["nodes"]
    path = [(n["name"], n["depth"]) for n in nodes if not n["is_external"]]
    assert ("PaymentRequest", 1) in path and (PROCESS_CHARGE, 1) in path
    assert ("PaymentProcessor._validate_limit", 2) in path and ("PaymentDeclined", 2) in path
    assert ("Receipt", 2) in path
    pc = next(n for n in nodes if n["name"] == PROCESS_CHARGE)
    assert pc["when"] == GUARD_CREATE and pc["parent_urn"] == U(ROUTES, "create_order")
    declined = next(n for n in nodes if n["name"] == "PaymentDeclined")
    assert declined["when"] == "not self._validate_limit(req.amount)"
    assert declined["parent_urn"] == U(PAY, PROCESS_CHARGE)


async def test_real_class_target_is_reached_by_constructor_calls(real: Pipeline) -> None:
    """(d): ``Foo(...)`` はクラス URN への CALLS。T-07 のクラス対象はコンストラクタ呼び出しから始まる。"""
    r = await calls(real, {"urn": U(PAY, "PaymentRequest")}, "incoming", depth=1)
    assert not r.is_error, r.text
    assert {n["name"] for n in r.structured["data"]["nodes"]} == {
        "create_order",
        "retry_order",
        "test_charge_ok",
    }
    pp = await calls(real, {"urn": U(PAY, "PaymentProcessor")}, "incoming", depth=1)
    names = [n["name"] for n in pp.structured["data"]["nodes"]]
    assert "test_charge_ok" in names and any(
        "(module)" in n for n in names
    )  # routes.py 直下の processor = ...


def real_db_of(pipeline: Pipeline) -> Path:
    return Path(pipeline.services.db_path)  # type: ignore[attr-defined]


async def test_real_type_hierarchy_from_inherits_rows(real: Pipeline) -> None:
    """(c)(e): ``class PaymentDeclined(Exception)`` は ``lk://_ext/builtins#Exception`` への INHERITS。"""
    r = await refs(real, {"urn": EXT_EXCEPTION}, query_type="type_hierarchy")
    assert not r.is_error, r.text
    subs = r.structured["data"]["type_hierarchy"]["subtypes"]
    assert [s["qualified_name"] for s in subs] == ["LimitExceeded", "PaymentDeclined"]
    sup = await refs(real, {"urn": U(LIM, "LimitExceeded")}, query_type="type_hierarchy")
    supers = sup.structured["data"]["type_hierarchy"]["supertypes"]
    assert [s["qualified_name"] for s in supers] == ["Exception"]
    rows = sql(real_db_of(real), "SELECT kind, is_external FROM symbols WHERE urn = ?", EXT_EXCEPTION)
    assert rows == [("Class", 1)]


async def test_real_external_nodes_have_stable_kinds_and_are_leaves(real: Pipeline) -> None:
    """(e)(f): 未解決の import は ``lk://_ext/<pkg>#<name>``。kind は名前の形と使われ方から決まる。

    PC:T-06#5 外部シンボルが索引に現れ（``is_external``）、それを target にした references は import の使用箇所を返す。
    PC:T-07#5 外部の呼び先は ``is_external: true`` の葉ノード。
    """
    kinds = dict(sql(real_db_of(real), "SELECT urn, kind FROM symbols WHERE is_external = 1"))
    assert kinds[EXT_DECIMAL] == "Class"  # IMPORTS・CALLS・TYPE_USES のどれが先でも
    assert kinds[EXT_EXCEPTION] == "Class"
    assert kinds[str(Urn.external("builtins", None, "ValueError"))] == "Class"
    assert kinds[str(Urn.external("dataclasses", None, "dataclass"))] == "Function"
    assert kinds[str(Urn.external("os"))] == "Module"
    out = await calls(real, {"urn": U(ROUTES, "create_order")}, "outgoing", depth=3)
    ext_json = out.structured["data"]["nodes"]
    leaf = next(n for n in ext_json if n["is_external"])
    assert leaf["urn"] == EXT_DECIMAL and not any(n["parent_urn"] == EXT_DECIMAL for n in ext_json)
    uses = await refs(real, {"urn": EXT_DECIMAL})
    assert not uses.is_error, uses.text
    assert {x["use"] for x in uses.structured["data"]["references"]} >= {"call", "type", "import"}


async def test_real_t06_postcondition_5_unresolved_import_is_an_external_symbol_with_import_sites(
    real: Pipeline,
) -> None:
    """``from decimal import Decimal``（解決できない import）は ``lk://_ext/decimal#Decimal``（``is_external``）として索引に載り、
    それを target にした references は import の使用箇所（``use: import``）を返す。"""
    rows = sql(real_db_of(real), "SELECT kind, is_external FROM symbols WHERE urn = ?", EXT_DECIMAL)
    assert rows == [("Class", 1)]
    r = await refs(real, {"urn": EXT_DECIMAL}, use_kinds=["import"], max_tokens=8000)
    assert not r.is_error, r.text
    got = {(x["path"], x["line"]) for x in r.structured["data"]["references"]}
    assert {x["use"] for x in r.structured["data"]["references"]} == {"import"}
    want = {
        (path, find_line(path, "from decimal import Decimal"))
        for path in (ROUTES, PAY, LIM, TESTS, "src/shop/models/receipt.py")
    }
    assert got == want


async def test_real_typescript_call_through_a_module_variable_reaches_the_overloaded_method(
    real: Pipeline,
) -> None:
    """TS: ``client.charge(...)``（``const client = new PaymentClient()``）→ 実装シグネチャ。``new`` もクラスへの CALLS。"""
    impl = sql(
        real_db_of(real),
        "SELECT urn FROM symbols WHERE qualified_name = 'PaymentClient.charge' ORDER BY start_line DESC LIMIT 1",
    )[0][0]
    r = await calls(real, {"urn": impl}, "incoming", depth=1)
    assert not r.is_error, r.text
    nodes = r.structured["data"]["nodes"]
    assert [(n["name"], n["call_site_path"]) for n in nodes] == [("checkout", TS_APP)]
    cls = await calls(real, {"urn": U(TS_CLIENT, "PaymentClient")}, "incoming", depth=1)
    assert any(n["call_site_path"] == TS_APP for n in cls.structured["data"]["nodes"])


async def test_real_file_created_after_its_importer_is_seen_by_t06_t07(tmp_path: Path) -> None:
    """BUG-0025: 監視（``index_file``）で後から作られたファイルの関数が、すでに索引済みの import 元の呼び出しから見える。

    ``hook.py`` が ``from app.audit import record`` を先に取り込まれ、``audit.py`` は後から作られる。作られる前は
    ``record`` が外部ノード、作られたあとは ``audit.py`` の実体になり、T-06 の参照にも ``hook.py`` の呼び出しが載る。
    """
    root = tmp_path / "live"
    (root / "app").mkdir(parents=True)
    (root / "app/__init__.py").write_text("", encoding="utf-8")
    (root / "app/hook.py").write_text(
        "from app.audit import record\n\n\ndef on_order():\n    return record()\n", encoding="utf-8"
    )
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id="live", root=root.resolve()), writer, readers, TreeCache())
    try:
        await indexer.index_file("app/__init__.py")
        await indexer.index_file("app/hook.py")
        registry = ToolRegistry()
        registry.register(t06.TOOL_SPEC)
        registry.register(t07.TOOL_SPEC)
        base = build_services(lambda: [str(root)])

        @dataclass(slots=True)
        class Services(ToolServices):
            db_path: Path | None = None

        pipe = Pipeline(
            registry, services=Services(roots=base.roots, trees=base.trees, lines=base.lines, db_path=db)
        )
        on_order = {"urn": str(Urn("live", "app/hook.py", CodeSel("on_order")))}

        before = await calls(pipe, on_order, "outgoing", depth=1, exclude_external=False)
        assert not before.is_error, before.text
        assert [(n["name"], n["is_external"]) for n in before.structured["data"]["nodes"]] == [
            ("audit.record", True)
        ]

        (root / "app/audit.py").write_text("def record():\n    return 1\n", encoding="utf-8")
        await indexer.index_file("app/audit.py")  # ファイル監視が届ける差分の取り込み

        after = await calls(pipe, on_order, "outgoing", depth=1, exclude_external=False)
        assert not after.is_error, after.text
        nodes = after.structured["data"]["nodes"]
        assert [(n["name"], n["is_external"]) for n in nodes] == [("record", False)]
        record = {"urn": str(Urn("live", "app/audit.py", CodeSel("record")))}
        incoming = await calls(pipe, record, "incoming", depth=1)
        assert [(n["name"], n["call_site_path"]) for n in incoming.structured["data"]["nodes"]] == [
            ("on_order", "app/hook.py")
        ]
        references = await refs(pipe, record)
        assert not references.is_error, references.text
        assert any(r["path"] == "app/hook.py" for r in references.structured["data"]["references"]), (
            references.text
        )
    finally:
        await indexer.aclose()
        readers.close()
        writer.close()


async def test_real_t06_use_after_a_function_local_import_is_a_reference(tmp_path: Path) -> None:
    """BUG-0048: 関数の中の ``from app.errors import AppError`` で束縛した名前の使用（``pytest.raises(AppError)``）も参照に載る。

    同名を別のモジュールから別の関数の中で import していても、そちらの使用は巻き込まない。
    """
    root = tmp_path / "scoped"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app/__init__.py").write_text("", encoding="utf-8")
    (root / "app/errors.py").write_text("class AppError(Exception):\n    pass\n", encoding="utf-8")
    (root / "app/other.py").write_text("class AppError:\n    pass\n", encoding="utf-8")
    test_text = (
        "import pytest\n"
        "\n"
        "\n"
        "def test_raises():\n"
        "    from app.errors import AppError\n"
        "\n"
        "    with pytest.raises(AppError):\n"
        "        raise AppError()\n"
        "\n"
        "\n"
        "def test_other():\n"
        "    from app.other import AppError\n"
        "\n"
        "    assert AppError()\n"
    )
    (root / "tests/test_errors.py").write_text(test_text, encoding="utf-8")
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id="scoped", root=root.resolve()), writer, readers, TreeCache())
    try:
        report = await indexer.scan()
        assert not report.errors, report.errors
        registry = ToolRegistry()
        registry.register(t06.TOOL_SPEC)
        base = build_services(lambda: [str(root)])

        @dataclass(slots=True)
        class Services(ToolServices):
            db_path: Path | None = None

        pipe = Pipeline(
            registry, services=Services(roots=base.roots, trees=base.trees, lines=base.lines, db_path=db)
        )

        errors = await refs(
            pipe, {"path": "app/errors.py", "symbol_name": "AppError"}, include_declaration=True
        )
        assert not errors.is_error, errors.text
        in_tests = sorted(
            x["line"] for x in errors.structured["data"]["references"] if x["path"] == "tests/test_errors.py"
        )
        assert in_tests == [5, 7, 8], errors.text  # import の行、pytest.raises(AppError)、raise AppError()

        other = await refs(
            pipe, {"path": "app/other.py", "symbol_name": "AppError"}, include_declaration=True
        )
        assert not other.is_error, other.text
        other_in_tests = sorted(
            x["line"] for x in other.structured["data"]["references"] if x["path"] == "tests/test_errors.py"
        )
        assert other_in_tests == [12, 14], other.text
    finally:
        await indexer.aclose()
        readers.close()
        writer.close()
