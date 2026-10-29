"""T-03 ``search_workspace_symbols`` と T-04 ``resolve_symbols`` の契約テスト（仕様書 §11 T-03・T-04 の事後条件）。

``tests/fixtures/poly`` をワークスペースにし、``tmp_path`` の PaaD に取り込んでから、パイプライン経由でツールを呼ぶ。
構文木と索引だけ（``structural``）の範囲。LSP による ``exact`` は対象外。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.core.model import Confidence, RelPath
from lucianke.core.textpos import LineIndex
from lucianke.index.connection import ReaderPool, open_writer
from lucianke.index.ingest import FileFacts, SymbolExtra, ingest_file, upsert_workspace
from lucianke.index.migrate import ensure_schema
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t03_search_workspace_symbols as t03
from lucianke.tools import t04_resolve_symbols as t04
from lucianke.tools import t05_get_symbol_bodies as t05
from lucianke.tools._services import SYMBOL_LANGUAGES, ToolServices, analyze_file
from lucianke.workspace.languages import HEAD_BYTES, detect, is_test

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAY_PY = "src/shop/services/payment.py"
ROUTES_PY = "src/shop/api/routes.py"
PAY_TS = "web/src/payment.ts"
WS = "poly"


@dataclass(slots=True)
class _Svc(ToolServices):
    """``build_services`` の入れ物に ``reader_pool`` を足したもの（合成ルートが同じ形で組む想定）。"""

    reader_pool: ReaderPool | None = None


async def _ingest_poly(svc: ToolServices, db: Path) -> None:
    ws = svc.workspace()
    conn = open_writer(db)
    ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    upsert_workspace(conn, {"workspace_id": ws.workspace_id, "root_path": str(POLY)})
    for path in sorted(p for p in POLY.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        rel = path.relative_to(POLY).as_posix()
        data = path.read_bytes()
        det = detect(RelPath(rel), data[:HEAD_BYTES])
        if not det.index_content:
            continue
        symbols: tuple[Any, ...] = ()
        extras: dict[str, SymbolExtra] = {}
        if det.language_id in SYMBOL_LANGUAGES:
            an = await analyze_file(svc, ws, rel)
            symbols = an.symbols
        ingest = FileFacts(
            workspace_id=ws.workspace_id,
            path=RelPath(rel),
            language=det.language_id,
            tier=det.tier,
            line_count=LineIndex(data).content_line_count,
            byte_size=len(data),
            sha256=__import__("hashlib").sha256(data).hexdigest(),
            is_test=is_test(rel),
            confidence=Confidence.STRUCTURAL if symbols else Confidence.LEXICAL_FALLBACK,
            symbols=symbols,
            symbol_extras=extras,
        )
        ingest_file(conn, ingest)
    conn.execute("UPDATE _meta SET data_version = data_version + 1")
    conn.execute("COMMIT")
    conn.close()


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pipeline]:
    db = tmp_path_factory.mktemp("paad") / "paad.sqlite"
    svc = _Svc(roots=lambda: [str(POLY)], trees=TreeCache(), lines=compact)
    asyncio.run(_ingest_poly(svc, db))
    pool = ReaderPool(db)
    svc.reader_pool = pool
    registry = ToolRegistry()
    registry.register(t03.TOOL_SPEC)
    registry.register(t04.TOOL_SPEC)
    registry.register(t05.SPEC)
    yield Pipeline(registry, services=svc)
    pool.close()


async def search(pipeline: Pipeline, fmt: str = "json", **args: Any):
    return await pipeline.call_tool("search_workspace_symbols", {"format": fmt, **args})


async def resolve(pipeline: Pipeline, fmt: str = "json", **args: Any):
    return await pipeline.call_tool("resolve_symbols", {"format": fmt, **args})


def names(r: Any) -> list[str]:
    return [s["qualified_name"] for s in r.structured["data"]["symbols"]]


def urns(r: Any) -> list[str]:
    return [s["urn"] for s in r.structured["data"]["symbols"]]


# --- T-03 ---------------------------------------------------------------------------------------


async def test_t03_in_tools_list(pipeline: Pipeline) -> None:
    listed = [d.name for d in pipeline.registry.descriptors()]
    assert "search_workspace_symbols" in listed and "resolve_symbols" in listed


async def test_t03_fuzzy_process_charge_ranks_the_method_first(pipeline: Pipeline) -> None:
    r = await search(pipeline, query="process_charge")
    assert not r.is_error, r.text
    syms = r.structured["data"]["symbols"]
    assert syms[0]["qualified_name"] == "PaymentProcessor.process_charge"
    assert syms[0]["urn"] == f"lk://{WS}/{PAY_PY}#PaymentProcessor.process_charge"
    assert r.structured["data"]["query"] == "process_charge" and r.structured["data"]["match"] == "fuzzy"
    assert r.structured["data"]["hits_total"] == len(syms) and r.structured["data"]["fallback_used"] is False
    assert r.structured["page"]["total_estimate"] == len(syms)
    assert all("score" in s for s in syms) and syms[0]["score"] == 1.0


async def test_t03_compact_shape(pipeline: Pipeline) -> None:
    r = await search(pipeline, "compact", query="process_charge")
    assert g.validate(r.text) == [], r.text
    lines = r.text.splitlines()
    assert lines[0].startswith("# search_workspace_symbols ok conf=structural")
    assert 'query: "process_charge" match=fuzzy hits=' in r.text
    head = next(i for i, ln in enumerate(lines) if ln.startswith("[Method] "))
    assert lines[head].startswith(
        f"[Method] {PAY_PY}#PaymentProcessor.process_charge(self, req: PaymentRequest) -> Receipt @L"
    )
    assert lines[head + 1] == f"  urn: lk://{WS}/{PAY_PY}#PaymentProcessor.process_charge"


async def test_t03_exact_postcondition_1(pipeline: Pipeline) -> None:
    """事後条件 1: match=exact のとき返す全シンボルの name は query と等しい。"""
    r = await search(pipeline, query="PaymentProcessor", match="exact")
    syms = r.structured["data"]["symbols"]
    assert syms and all(s["name"] == "PaymentProcessor" for s in syms)
    r2 = await search(pipeline, query="charge", match="exact")  # 部分文字列は exact では当たらない
    assert r2.structured["data"]["symbols"] == [] or all(
        s["name"] == "charge" for s in r2.structured["data"]["symbols"]
    )


async def test_t03_exact_compares_name_only(pipeline: Pipeline) -> None:
    """exact は name の等価比較。限定名（ドット付き）では当たらない。限定名で探すときは fuzzy（完全一致が先頭）。"""
    ex = await search(pipeline, query="PaymentProcessor.process_charge", match="exact", fallback_grep=False)
    assert not ex.is_error and names(ex) == [] and ex.structured["data"]["fallback_used"] is False
    fz = await search(pipeline, query="PaymentProcessor.process_charge")
    assert (
        names(fz)[0] == "PaymentProcessor.process_charge"
        and fz.structured["data"]["symbols"][0]["score"] == 1.0
    )


async def test_t03_limit_postcondition_2(pipeline: Pipeline) -> None:
    """事後条件 2: page.returned <= limit。総数は total_estimate。"""
    r = await search(pipeline, query="pay", limit=2)
    assert not r.is_error, r.text
    assert r.structured["page"]["returned"] <= 2
    assert r.structured["page"]["total_estimate"] > 2  # 3 件以上ある（Payment*、test_ 系）
    assert r.structured["data"]["hits_total"] == r.structured["page"]["total_estimate"]


async def test_t03_kind_filter_postcondition_3(pipeline: Pipeline) -> None:
    """事後条件 3: symbol_kinds 指定時、返す全シンボルの kind はその集合に含まれる。"""
    r = await search(pipeline, query="pay", symbol_kinds=["class"])
    syms = r.structured["data"]["symbols"]
    assert syms and {s["kind"] for s in syms} == {"Class"}
    r2 = await search(pipeline, query="pay", symbol_kinds=["Method", "function"])
    assert {s["kind"] for s in r2.structured["data"]["symbols"]} <= {"Method", "Function"}


async def test_t03_unknown_kind_is_invalid(pipeline: Pipeline) -> None:
    r = await search(pipeline, query="pay", symbol_kinds=["klass"])
    assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT"
    assert r.structured["error"]["details"]["param"] == "symbol_kinds"


@pytest.mark.parametrize("query", ["", "   "])
async def test_t03_empty_query_is_invalid(pipeline: Pipeline, query: str) -> None:
    r = await search(pipeline, query=query)
    assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


@pytest.mark.parametrize("limit", [0, 201])
async def test_t03_limit_out_of_range_is_invalid(pipeline: Pipeline, limit: int) -> None:
    r = await search(pipeline, query="pay", limit=limit)
    assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


async def test_t03_ranking_exact_then_prefix_then_partial(pipeline: Pipeline) -> None:
    r = await search(pipeline, query="PaymentRequest")
    syms = r.structured["data"]["symbols"]
    assert syms[0]["name"] == "PaymentRequest" and syms[0]["score"] == 1.0
    scores = [s["score"] for s in syms]
    assert scores == sorted(scores, reverse=True)
    r2 = await search(pipeline, query="Payment")  # 前方一致が部分一致より先
    tiers = [s["name"].startswith("Payment") for s in r2.structured["data"]["symbols"]]
    assert tiers == sorted(tiers, reverse=True)


async def test_t03_prefix_mode(pipeline: Pipeline) -> None:
    r = await search(pipeline, query="process", match="prefix")
    assert "PaymentProcessor.process_charge" in names(r)


async def test_t03_regex_mode(pipeline: Pipeline) -> None:
    r = await search(pipeline, query=r"^process_\w+$", match="regex")
    assert not r.is_error, r.text
    assert "PaymentProcessor.process_charge" in names(r)
    assert all(re.search(r"^process_\w+$", s["name"]) for s in r.structured["data"]["symbols"])


async def test_t03_invalid_regex(pipeline: Pipeline) -> None:
    r = await search(pipeline, query="(unclosed", match="regex")
    assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


async def test_t03_scope_paths_and_include_tests(pipeline: Pipeline) -> None:
    all_hits = await search(pipeline, query="charge")
    assert any(s["path"].startswith("tests/") for s in all_hits.structured["data"]["symbols"])
    no_tests = await search(pipeline, query="charge", include_tests=False)
    assert no_tests.structured["data"]["symbols"]
    assert not any(s["path"].startswith("tests/") for s in no_tests.structured["data"]["symbols"])
    scoped = await search(pipeline, query="charge", scope_paths=["src/shop/services"])
    assert scoped.structured["data"]["symbols"]
    assert all(s["path"].startswith("src/shop/services/") for s in scoped.structured["data"]["symbols"])
    out = await search(pipeline, query="charge", scope_paths=["../outside"])
    assert out.is_error and out.structured["error"]["code"] == "E_INVALID_ARGUMENT"


async def test_t03_type_signature_filter(pipeline: Pipeline) -> None:
    both = await search(pipeline, query="process", type_signature_filter="PaymentRequest -> Receipt")
    assert names(both) == ["PaymentProcessor.process_charge"]
    ret_only = await search(pipeline, query="process", type_signature_filter="-> Receipt")
    assert "PaymentProcessor.process_charge" in names(ret_only)
    none = await search(pipeline, query="process", type_signature_filter="Receipt -> PaymentRequest")
    assert names(none) == []


async def test_t03_fallback_grep_postcondition_4(pipeline: Pipeline) -> None:
    """事後条件 4: fallback_used の応答は is_degraded=true で、compact はヘッダ直後に ! 行を持つ。"""
    r = await search(pipeline, query="COMPUTE-TAX")
    data = r.structured["data"]
    assert data["fallback_used"] is True and data["symbols"]
    meta = r.structured["_resolution_meta"]
    assert meta["is_degraded"] is True and meta["confidence_level"] == "lexical_fallback"
    assert meta["engine_used"] == "internal_ripgrep_word_boundary" and meta["accuracy_warning"]
    first = data["symbols"][0]
    assert first["kind"] == "Section" and first["path"] == "legacy/calc.cob"
    assert re.fullmatch(rf"lk://{WS}/legacy/calc\.cob#lex:COMPUTE-TAX@L\d+", first["urn"])
    assert first["range"]["start_line"] == first["range"]["end_line"]
    c = await search(pipeline, "compact", query="COMPUTE-TAX")
    assert g.validate(c.text) == [], c.text
    lines = c.text.splitlines()
    assert lines[1].startswith("! DEGRADED") and any(ln.startswith("! Warning") for ln in lines[:4])
    assert any(ln.startswith("[Section] legacy/calc.cob#COMPUTE-TAX") for ln in lines)


async def test_t03_fallback_off_returns_empty(pipeline: Pipeline) -> None:
    r = await search(pipeline, query="COMPUTE-TAX", fallback_grep=False)
    assert not r.is_error
    assert r.structured["data"]["symbols"] == [] and r.structured["data"]["fallback_used"] is False
    assert r.structured["_resolution_meta"]["is_degraded"] is False


async def test_t03_no_hit_anywhere(pipeline: Pipeline) -> None:
    r = await search(pipeline, query="zzz_nothing_like_this_zzz")
    assert not r.is_error
    assert r.structured["data"]["symbols"] == [] and r.structured["page"]["returned"] == 0


async def test_t03_budget_postcondition_5(pipeline: Pipeline) -> None:
    """事後条件 5: budget.used_tokens_est <= max_tokens。小さい予算ではカーソルで続きを取る。"""
    r = await search(pipeline, query="pay", max_tokens=200, limit=50)
    assert not r.is_error
    assert r.structured["budget"]["used_tokens_est"] <= 200
    first = urns(r)
    cursor = r.structured["page"]["next_cursor"]
    assert cursor and first
    r2 = await search(pipeline, query="pay", max_tokens=200, limit=50, cursor=cursor)
    assert r2.structured["budget"]["used_tokens_est"] <= 200
    assert not set(first) & set(urns(r2))  # ページが重ならない


async def test_t03_default_budget(pipeline: Pipeline) -> None:
    r = await search(pipeline, "compact", query="a", match="fuzzy")
    assert g.validate(r.text) == [], r.text
    j = await search(pipeline, query="a")
    assert j.structured["budget"]["used_tokens_est"] <= j.structured["budget"]["max_tokens"] == 1500


async def test_t03_revision_other_than_working_tree(pipeline: Pipeline) -> None:
    r = await search(pipeline, query="pay", revision="HEAD")
    assert r.is_error and r.structured["error"]["code"] == "E_UNSUPPORTED"


async def test_t03_without_index_is_workspace_not_configured() -> None:
    svc = _Svc(roots=lambda: [str(POLY)], trees=TreeCache(), lines=compact)  # reader_pool も db_path も無い
    registry = ToolRegistry()
    registry.register(t03.TOOL_SPEC)
    r = await Pipeline(registry, services=svc).call_tool(
        "search_workspace_symbols", {"query": "x", "format": "json"}
    )
    assert r.is_error and r.structured["error"]["code"] == "E_WORKSPACE_NOT_CONFIGURED"


async def test_t03_db_path_is_enough(tmp_path: Path) -> None:
    """``reader_pool`` が無くても ``db_path``（T-19 / T-20 と同じ約束）から読める。"""
    db = tmp_path / "paad.sqlite"
    svc = _Svc(roots=lambda: [str(POLY)], trees=TreeCache(), lines=compact)
    await _ingest_poly(svc, db)
    holder = _DbSvc(roots=svc.roots, trees=svc.trees, lines=compact, db_path=db)
    registry = ToolRegistry()
    registry.register(t03.TOOL_SPEC)
    r = await Pipeline(registry, services=holder).call_tool(
        "search_workspace_symbols", {"query": "process_charge", "format": "json"}
    )
    assert not r.is_error and names(r)[0] == "PaymentProcessor.process_charge"


@dataclass(slots=True)
class _DbSvc(ToolServices):
    db_path: Path | None = None


# --- T-04 ---------------------------------------------------------------------------------------

METHOD_URN = f"lk://{WS}/{PAY_PY}#PaymentProcessor.process_charge"
METHOD = {"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"}
IMPORTED = {"path": ROUTES_PY, "symbol_name": "PaymentProcessor"}


def items(r: Any) -> list[dict[str, Any]]:
    return r.structured["data"]["items"]


async def test_t04_resolves_an_imported_name_to_its_definition(pipeline: Pipeline) -> None:
    """routes.py の PaymentProcessor（import された名前）は services/payment.py の定義に解決される。"""
    r = await resolve(pipeline, targets=[IMPORTED])
    assert not r.is_error, r.text
    item = items(r)[0]
    assert item["ok"] is True
    assert item["symbol"]["urn"] == f"lk://{WS}/{PAY_PY}#PaymentProcessor"
    assert item["definition"]["path"] == PAY_PY and item["definition"]["declared_in"] == "source"
    assert item["resolved_via"] and ROUTES_PY in item["resolved_via"][0]


@pytest.mark.parametrize(
    "ref",
    [
        METHOD,
        {"path": PAY_PY, "symbol_name": "process_charge"},  # 末尾一致
        {"path": PAY_PY, "line": 45},  # その行を含む最も内側のシンボル
        {"urn": METHOD_URN},
    ],
)
async def test_t04_target_forms(pipeline: Pipeline, ref: dict[str, Any]) -> None:
    r = await resolve(pipeline, targets=[ref])
    assert not r.is_error, r.text
    assert items(r)[0]["symbol"]["qualified_name"] == "PaymentProcessor.process_charge"


async def test_t04_line_on_an_import_statement(pipeline: Pipeline) -> None:
    lines = (POLY / ROUTES_PY).read_text(encoding="utf-8").splitlines()
    line = next(i for i, ln in enumerate(lines, 1) if ln.startswith("from shop.services.payment import"))
    col = lines[line - 1].index("PaymentRequest") + 2
    r = await resolve(pipeline, targets=[{"path": ROUTES_PY, "line": line, "character": col}])
    assert not r.is_error, r.text
    assert items(r)[0]["symbol"]["qualified_name"] == "PaymentRequest"
    assert items(r)[0]["symbol"]["path"] == PAY_PY


async def test_t04_typescript_import_alias(pipeline: Pipeline) -> None:
    r = await resolve(pipeline, targets=[{"path": "web/src/app.ts", "symbol_name": "PaymentClient"}])
    assert not r.is_error, r.text
    assert items(r)[0]["symbol"]["urn"] == f"lk://{WS}/{PAY_TS}#PaymentClient"


async def test_t04_postcondition_1_definition_range_equals_range_returned(pipeline: Pipeline) -> None:
    """事後条件 1: definition.range は、同じ URN を get_symbol_bodies（context_lines=0）に渡したときの range_returned と一致する。"""
    refs = [
        METHOD,
        {"path": PAY_PY, "symbol_name": "PaymentRequest"},  # デコレータつき
        {"path": PAY_PY, "symbol_name": "PaymentProcessor"},
        {"path": PAY_TS, "symbol_name": "PaymentClient"},
        {"path": PAY_TS, "symbol_name": "PaymentRequest"},
        IMPORTED,
    ]
    r = await resolve(pipeline, targets=refs)
    assert not r.is_error, r.text
    for item in items(r):
        assert item["ok"], item
        b = await pipeline.call_tool(
            "get_symbol_bodies",
            {"format": "json", "targets": [{"urn": item["symbol"]["urn"]}], "context_lines": 0},
        )
        assert not b.is_error, b.text
        rr = b.structured["data"]["items"][0]["range_returned"]
        assert item["definition"]["range"] == rr, item["symbol"]["urn"]


async def test_t04_postcondition_2_type_definitions_resolve(pipeline: Pipeline) -> None:
    """事後条件 2: type_definitions[].urn を resolve_symbols に渡すと E_TARGET_NOT_FOUND にならない。"""
    r = await resolve(pipeline, targets=[METHOD])
    tdefs = items(r)[0]["type_definitions"]
    assert {d["name"]: d["urn"] for d in tdefs} == {
        "PaymentRequest": f"lk://{WS}/{PAY_PY}#PaymentRequest",
        "Receipt": f"lk://{WS}/src/shop/models/receipt.py#Receipt",  # import をたどった先
    }
    again = await resolve(pipeline, targets=[{"urn": d["urn"]} for d in tdefs])
    assert not again.is_error, again.text
    assert all(i["ok"] for i in items(again))
    assert [i["symbol"]["urn"] for i in items(again)] == [d["urn"] for d in tdefs]
    assert tdefs[0]["range"]["start_line"] <= tdefs[0]["range"]["end_line"] and tdefs[0]["path"] == PAY_PY


async def test_t04_type_definitions_off(pipeline: Pipeline) -> None:
    r = await resolve(pipeline, targets=[METHOD], resolve_type_definitions=False)
    assert items(r)[0]["type_definitions"] == []


async def test_t04_postcondition_3_ambiguous_item_only(pipeline: Pipeline) -> None:
    """事後条件 3: 曖昧な項目は E_TARGET_AMBIGUOUS と 10 件以下の candidates を持ち、他の項目は ok: true。"""
    targets = [METHOD, {"path": PAY_TS, "symbol_name": "charge"}, {"path": PAY_PY, "symbol_name": "Nope"}]
    r = await resolve(pipeline, targets=targets)
    assert not r.is_error, r.text
    ok, amb, missing = items(r)
    assert ok["ok"] is True
    assert amb["ok"] is False and amb["error"]["code"] == "E_TARGET_AMBIGUOUS"
    assert 2 <= len(amb["error"]["candidates"]) <= 10
    assert all(
        c["urn"].startswith(f"lk://{WS}/{PAY_TS}#PaymentClient.charge") for c in amb["error"]["candidates"]
    )
    assert missing["ok"] is False and missing["error"]["code"] == "E_TARGET_NOT_FOUND"
    pick = amb["error"]["candidates"][0]["urn"]  # 候補のどれを渡しても解決できる
    again = await resolve(pipeline, targets=[{"urn": pick}])
    assert items(again)[0]["ok"] is True


async def test_t04_all_failed_is_an_error(pipeline: Pipeline) -> None:
    r = await resolve(
        pipeline, targets=[{"path": PAY_PY, "symbol_name": "Nope"}, {"path": "missing.py", "line": 1}]
    )
    assert r.is_error and r.structured["error"]["code"] == "E_TARGET_NOT_FOUND"
    assert [i["error"]["code"] for i in r.structured["error"]["details"]["items"]] == [
        "E_TARGET_NOT_FOUND"
    ] * 2


async def test_t04_doc_decorators_and_signature(pipeline: Pipeline) -> None:
    item = items(await resolve(pipeline, targets=[METHOD]))[0]
    assert item["decorators"] == ["@transactional"]
    assert item["doc_full"].startswith("注文に対する決済を実行し領収書を発行する。")
    assert "Raises:" in item["doc_full"] and "PaymentDeclined" in item["doc_full"]
    assert (
        item["signature_expanded"] == item["symbol"]["signature"] == "(self, req: PaymentRequest) -> Receipt"
    )
    assert item["confidence_level"] == "structural"


async def test_t04_typescript_jsdoc(pipeline: Pipeline) -> None:
    item = items(await resolve(pipeline, targets=[{"path": PAY_TS, "line": 14}]))[
        0
    ]  # オーバーロード実装の本体の中
    assert item["symbol"]["qualified_name"] == "PaymentClient.charge"
    assert item["doc_full"] == "決済を要求する。amount は 0 より大きいこと。"


async def test_t04_include_doc_full_off(pipeline: Pipeline) -> None:
    item = items(await resolve(pipeline, targets=[METHOD], include_doc_full=False))[0]
    assert "doc_full" not in item


async def test_t04_compact_shape(pipeline: Pipeline) -> None:
    r = await resolve(
        pipeline, "compact", targets=[METHOD, IMPORTED, {"path": PAY_PY, "symbol_name": "Nope"}]
    )
    assert not r.is_error and g.validate(r.text) == [], r.text
    lines = r.text.splitlines()
    assert lines[0].startswith("# resolve_symbols ok conf=structural")
    head = next(i for i, ln in enumerate(lines) if ln.startswith("[Method] "))
    assert re.fullmatch(
        rf"\[Method\] {re.escape(PAY_PY)}#PaymentProcessor\.process_charge @L\d+-\d+", lines[head]
    )
    block = []
    for ln in lines[head + 1 :]:
        if not ln.startswith("  "):
            break
        block.append(ln.strip())
    attrs = {a.split(":", 1)[0]: a.split(": ", 1)[1] for a in block}
    assert attrs["urn"] == METHOD_URN
    assert attrs["sig"] == "(self, req: PaymentRequest) -> Receipt"
    assert attrs["type"].startswith(f"PaymentRequest=lk://{WS}/{PAY_PY}#PaymentRequest Receipt=lk://{WS}/")
    assert re.fullmatch(r"注文に対する決済を実行し領収書を発行する。 \(doc_lines=\d+\)", attrs["doc"])
    assert attrs["decorators"] == "@transactional"
    assert f"! {PAY_PY}#Nope: E_TARGET_NOT_FOUND" in r.text
    assert any(
        ln.startswith(f"[Class] {PAY_PY}#PaymentProcessor @L") for ln in lines
    )  # 別ファイルの定義へ解決


async def test_t04_external_package_is_null_definition_not_an_error(pipeline: Pipeline) -> None:
    """ワークスペース外のモジュールの名前は E_TARGET_NOT_FOUND ではなく definition: null と warnings。"""
    r = await resolve(pipeline, targets=[{"path": ROUTES_PY, "symbol_name": "Decimal"}, METHOD])
    assert not r.is_error, r.text
    ext = items(r)[0]
    assert ext["ok"] is True and ext["definition"] is None and ext["symbol"] is None
    assert "decimal" in ext["warnings"][0] and "outside the workspace" in ext["warnings"][0]
    c = await resolve(pipeline, "compact", targets=[{"path": ROUTES_PY, "symbol_name": "Decimal"}, METHOD])
    assert g.validate(c.text) == [], c.text
    assert any(ln.startswith(f"! {ROUTES_PY}#Decimal:") for ln in c.text.splitlines())


async def test_t04_follow_aliases_off(pipeline: Pipeline) -> None:
    r = await resolve(pipeline, targets=[IMPORTED], follow_aliases=False)
    assert r.is_error and r.structured["error"]["code"] == "E_TARGET_NOT_FOUND"


async def test_t04_unsupported_selector_and_bad_urn(pipeline: Pipeline) -> None:
    targets = [
        METHOD,
        {"path": PAY_PY, "symbol_name": "xpath:/a"},
        {"urn": "not a urn"},
        {"urn": f"lk://{WS}/{PAY_PY}#Nope"},
    ]
    r = await resolve(pipeline, targets=targets)
    assert not r.is_error
    codes = [i["error"]["code"] if not i["ok"] else "ok" for i in items(r)]
    assert codes == ["ok", "E_UNSUPPORTED", "E_INVALID_ARGUMENT", "E_TARGET_NOT_FOUND"]


async def test_t04_ignored_forms_are_reported(pipeline: Pipeline) -> None:
    ref = {"urn": METHOD_URN, "path": PAY_PY, "symbol_name": "PaymentRequest"}
    item = items(await resolve(pipeline, targets=[ref]))[0]
    assert item["symbol"]["qualified_name"] == "PaymentProcessor.process_charge"
    assert "ignored" in item["warnings"][0]


@pytest.mark.parametrize("n", [0, 21])
async def test_t04_target_count_bounds(pipeline: Pipeline, n: int) -> None:
    r = await resolve(pipeline, targets=[METHOD] * n)
    assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


async def test_t04_budget(pipeline: Pipeline) -> None:
    targets = [METHOD, IMPORTED, {"path": PAY_TS, "symbol_name": "PaymentClient"}]
    for max_tokens in (200, 400, 2000):
        r = await resolve(pipeline, targets=targets, max_tokens=max_tokens)
        assert not r.is_error, r.text
        assert r.structured["budget"]["used_tokens_est"] <= max_tokens


async def test_t04_without_index_is_workspace_not_configured() -> None:
    svc = _Svc(roots=lambda: [str(POLY)], trees=TreeCache(), lines=compact)
    registry = ToolRegistry()
    registry.register(t04.TOOL_SPEC)
    r = await Pipeline(registry, services=svc).call_tool(
        "resolve_symbols", {"targets": [METHOD], "format": "json"}
    )
    assert r.is_error and r.structured["error"]["code"] == "E_WORKSPACE_NOT_CONFIGURED"


async def test_t04_revision_other_than_working_tree(pipeline: Pipeline) -> None:
    r = await resolve(pipeline, targets=[METHOD], revision="HEAD")
    assert r.is_error and r.structured["error"]["code"] == "E_UNSUPPORTED"
