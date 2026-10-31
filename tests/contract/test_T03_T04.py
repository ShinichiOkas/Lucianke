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
from lucianke.syntax.support import get_adapter
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


async def _ingest_poly(svc: ToolServices, db: Path, *, data_symbols: bool = False, root: Path = POLY) -> None:
    """``data_symbols`` は Tier 3 のデータファイルの Key / Tag も索引に入れる（``Indexer`` と同じく ``get_adapter(...).symbols``）。"""
    ws = svc.workspace()
    conn = open_writer(db)
    ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    upsert_workspace(conn, {"workspace_id": ws.workspace_id, "root_path": str(root)})
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        det = detect(RelPath(rel), data[:HEAD_BYTES])
        if not det.index_content:
            continue
        symbols: tuple[Any, ...] = ()
        extras: dict[str, SymbolExtra] = {}
        if det.language_id in SYMBOL_LANGUAGES:
            an = await analyze_file(svc, ws, rel)
            symbols = an.symbols
        elif data_symbols and (adapter := get_adapter(det.language_id)) is not None:
            symbols = tuple(adapter.symbols(data, rel=rel, workspace_id=ws.workspace_id))
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


def _make_pipeline(tmp_path_factory: pytest.TempPathFactory, *, data_symbols: bool) -> Iterator[Pipeline]:
    db = tmp_path_factory.mktemp("paad") / "paad.sqlite"
    svc = _Svc(roots=lambda: [str(POLY)], trees=TreeCache(), lines=compact)
    asyncio.run(_ingest_poly(svc, db, data_symbols=data_symbols))
    pool = ReaderPool(db)
    svc.reader_pool = pool
    registry = ToolRegistry()
    registry.register(t03.TOOL_SPEC)
    registry.register(t04.TOOL_SPEC)
    registry.register(t05.SPEC)
    yield Pipeline(registry, services=svc)
    pool.close()


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pipeline]:
    yield from _make_pipeline(tmp_path_factory, data_symbols=False)


@pytest.fixture(scope="module")
def pipeline_with_data(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pipeline]:
    """``pipeline`` に、データファイル（json / yaml / toml / xml）の Key / Tag も入れたもの。"""
    yield from _make_pipeline(tmp_path_factory, data_symbols=True)


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
    # URN は lk://<ws>/<path>#<qualified> で行から導けるので urn: 行は出さない（仕様書 §6.1 規約 2。BUG-0069）
    assert not any(ln.strip().startswith("urn: ") for ln in lines), r.text


async def test_t03_signature_only_matches_rank_after_name_matches(pipeline: Pipeline) -> None:
    """名前・限定名に当たるヒットが先、シグネチャ・Doc の文字列だけに当たるヒットは後（BUG-0069）。"""
    r = await search(pipeline, query="PaymentRequest", limit=200)
    syms = r.structured["data"]["symbols"]
    q = "paymentrequest"
    by_name = [q in s["name"].casefold() or q in s["qualified_name"].casefold() for s in syms]
    assert True in by_name and False in by_name, names(r)  # process_charge(self, req: PaymentRequest) はシグネチャだけの一致
    first = by_name.index(False)
    assert not any(by_name[first:]), names(r)
    assert "PaymentProcessor.process_charge" in names(r)[first:]
    assert max(s["score"] for s in syms[first:]) < min(s["score"] for s in syms[:first])


def test_t03_compact_caps_long_signatures_but_json_keeps_them() -> None:
    """compact のヒットのシグネチャは 160 文字で ``…)`` に切る（戻り値は残す）。json の signature は全文（BUG-0069）。"""
    from types import SimpleNamespace

    from lucianke.core.limits import SEARCH_SIGNATURE_MAX_CHARS
    from lucianke.core.model import Kind, Range, SymbolRecord
    from lucianke.core.urn import CodeSel, Urn

    sig = "(self, " + ", ".join(f"p{i}: Callable[[int], str] | None = None" for i in range(15)) + ") -> None"
    sym = SymbolRecord(
        urn=Urn(WS, "src/m.py", CodeSel("Pipeline.__init__")), path=RelPath("src/m.py"), name="__init__",
        qualified_name="Pipeline.__init__", kind=Kind.CONSTRUCTOR, range=Range.from_line_span_external(3, 9),
        signature=sig, confidence=Confidence.STRUCTURAL,
    )  # fmt: skip
    rec = t03._record(SimpleNamespace(lines=compact), t03._Hit(sym, 3, 0.1, 0.0, None), 0)  # type: ignore[arg-type]
    assert len(rec.lines) == 1  # 導ける URN なので urn: 行は無い
    shown = rec.lines[0].removeprefix("[Constructor] src/m.py#Pipeline.__init__").split(" @L")[0]
    assert len(shown) <= SEARCH_SIGNATURE_MAX_CHARS < len(sig)
    assert shown.startswith("(self, p0: Callable[[int], str] | None = None") and shown.endswith("…) -> None")
    assert g.validate("\n".join(rec.lines), require_header=False) == []
    assert rec.data["signature"] == sig


async def test_compact_symbols_are_addressable_from_what_is_shown(pipeline: Pipeline) -> None:
    """compact に出た記号は、行から導いた URN（``lk://<ws>/<path>#<qualified>``）でも ``path`` + ``symbol_name`` でも、
    ``urn:`` 行があればその URN でも、T-04・T-05 で同じ記号に届く（仕様書 §6.1 規約 2・§5.3。BUG-0069）。"""
    r = await search(pipeline, "compact", query="charge", limit=50)
    assert not r.is_error and g.validate(r.text) == [], r.text
    lines = r.text.splitlines()
    ws = re.search(r" ws=(\S+) ", lines[0])
    assert ws is not None
    shown: list[tuple[str, str, str | None]] = []  # (path, qualified, urn: 行の値)
    for i, ln in enumerate(lines):
        m = re.match(r"^\[\w+\] (\S+?)#([^(\s@]+)", ln)
        if m is None:
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        shown.append((m[1], m[2], nxt.removeprefix("urn: ") if nxt.startswith("urn: ") else None))
    assert any(u is None for _p, _q, u in shown), r.text
    assert any(u is not None for _p, _q, u in shown), r.text  # TS のオーバーロード（charge/1・charge/2~…）は導けない
    for path, qualified, urn_line in shown:
        urn = urn_line or f"lk://{ws[1]}/{path}#{qualified}"
        j = await resolve(pipeline, targets=[{"urn": urn}])
        assert not j.is_error and items(j)[0]["ok"], (urn, j.text)
        assert items(j)[0]["symbol"]["urn"] == urn
        b = await pipeline.call_tool("get_symbol_bodies", {"format": "json", "targets": [{"urn": urn}]})
        assert not b.is_error and b.structured["data"]["items"][0]["ok"], (urn, b.text)
        if urn_line is None:  # 導ける記号は path + symbol_name（修飾名）でも一意に届く
            j2 = await resolve(pipeline, targets=[{"path": path, "symbol_name": qualified}])
            assert not j2.is_error and items(j2)[0]["symbol"]["urn"] == urn, (path, qualified, j2.text)


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
    # json は封筒（約 170 トークン）と data の全項目（1 件約 120 トークン）を数える。最小の 200 では 1 件も入らない（その応答は
    # レコード 0 件・警告に必要な max_tokens の目安。別の票）ので、数件ずつのページになる予算で確かめる（BUG-0017）
    r = await search(pipeline, query="pay", max_tokens=500, limit=50)
    assert not r.is_error
    assert r.structured["budget"]["used_tokens_est"] <= 500
    first = urns(r)
    cursor = r.structured["page"]["next_cursor"]
    assert cursor and first
    r2 = await search(pipeline, query="pay", max_tokens=500, limit=50, cursor=cursor)
    assert r2.structured["budget"]["used_tokens_est"] <= 500
    assert not set(first) & set(urns(r2))  # ページが重ならない


async def test_t03_default_budget(pipeline: Pipeline) -> None:
    r = await search(pipeline, "compact", query="a", match="fuzzy")
    assert g.validate(r.text) == [], r.text
    j = await search(pipeline, query="a")
    assert j.structured["budget"]["used_tokens_est"] <= j.structured["budget"]["max_tokens"] == 1500


async def test_t03_revision_other_than_working_tree(pipeline: Pipeline) -> None:
    # poly は Git のワーキングコピーではないので HEAD は解決できない（BUG-0018。E_UNSUPPORTED は test_revision_reads.py）
    r = await search(pipeline, query="pay", revision="HEAD")
    assert r.is_error and r.structured["error"]["code"] == "E_REVISION_NOT_FOUND"


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


_INSTANCE_LOCK_PY = """class InstanceLock:
    def __init__(self, run_dir: str) -> None:
        self.run_dir = run_dir

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        pass


class DirInstanceLock(InstanceLock):
    def holder_pid(self) -> int:
        return 0
"""


async def test_t03_query_without_dot_matches_own_name_not_members_of_a_matching_class(tmp_path: Path) -> None:
    """``.`` の無い query は自分の名前に当て、限定名（``InstanceLock.acquire``）では当てない。メンバーは
    ``InstanceLock.acq`` のように ``.`` 付きで探す（BUG-0121。能力ベンチの T02 で適合率 0.5 だった）。"""
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "locks.py").write_bytes(_INSTANCE_LOCK_PY.encode("utf-8"))
    db = tmp_path / "paad.sqlite"
    svc = _Svc(roots=lambda: [str(root)], trees=TreeCache(), lines=compact)
    await _ingest_poly(svc, db, root=root)
    holder = _DbSvc(roots=svc.roots, trees=svc.trees, lines=compact, db_path=db)
    registry = ToolRegistry()
    registry.register(t03.TOOL_SPEC)
    pipe = Pipeline(registry, services=holder)

    async def found(**args: Any) -> list[str]:
        r = await pipe.call_tool("search_workspace_symbols", {"format": "json", **args})
        assert not r.is_error, r.text
        return names(r)

    got = await found(query="InstanceLock")
    assert got[0] == "InstanceLock"  # 完全一致が先
    assert set(got) == {"InstanceLock", "DirInstanceLock"}, got  # メンバー（acquire・__init__ など）は出ない
    assert set(await found(query="instancelock")) == {"InstanceLock", "DirInstanceLock"}  # 大文字小文字は無視
    assert await found(query="InstanceLock", match="prefix") == ["InstanceLock"]
    assert (await found(query="InstanceLock.acq"))[0] == "InstanceLock.acquire"  # "." があれば限定名に当てる
    assert (await found(query="InstanceLock.acq", match="prefix")) == ["InstanceLock.acquire"]
    assert "DirInstanceLock.holder_pid" in await found(query="holder_pid")  # 名前で当たるメンバーは今までどおり


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
    assert "urn" not in attrs  # METHOD_URN は見出しの path#qualified から導ける（§6.1 規約 2。BUG-0069）
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
    for max_tokens in (400, 700, 2000):  # json は封筒と data の全項目を数える。200 では 0 件の応答（警告つき）も収まらない（BUG-0017）
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
    assert r.is_error and r.structured["error"]["code"] == "E_REVISION_NOT_FOUND"  # 同上


async def test_t03_over_limit_issues_next_cursor_and_pages_do_not_overlap(pipeline: Pipeline) -> None:
    """BUG-0008: 一致が limit を超えるなら next_cursor を出す。カーソルで続きを辿ると重複も欠落も無い。"""
    full = await search(pipeline, query="pay", limit=200, max_tokens=8000)  # json は全項目を数えるので、既定の 1500 では全件が入らない
    all_urns = urns(full)
    total = full.structured["data"]["hits_total"]
    assert len(all_urns) == total > 2 and full.structured["page"]["next_cursor"] is None

    first = await search(pipeline, query="pay", limit=2)
    assert first.structured["page"]["returned"] == 2 and first.structured["page"]["total_estimate"] == total
    cursor = first.structured["page"]["next_cursor"]
    assert cursor, first.text
    seen = urns(first)
    pages = 1
    while cursor:
        nxt = await search(pipeline, query="pay", limit=2, cursor=cursor)
        assert not nxt.is_error, nxt.text
        assert nxt.structured["page"]["returned"] <= 2
        assert not set(seen) & set(urns(nxt))  # ページが重ならない
        seen += urns(nxt)
        cursor = nxt.structured["page"]["next_cursor"]
        pages += 1
        assert pages <= total, "did not terminate"
    assert seen == all_urns and pages == -(-total // 2)

    compact_first = await search(pipeline, "compact", query="pay", limit=2)
    assert g.validate(compact_first.text) == [], compact_first.text
    assert f"remaining≈{total - 2}" in compact_first.text and "# more: cursor=" in compact_first.text


async def test_t03_code_symbols_come_before_data_keys_of_the_same_match_quality(
    pipeline_with_data: Pipeline,
) -> None:
    """データファイルの Key / Tag は名前検索の対象だが、同じ一致の質ならコードのシンボルの後ろに並ぶ（``conf/`` は ``src/`` より辞書順で先でも）。"""
    r = await search(pipeline_with_data, query="retry", match="exact")
    assert not r.is_error, r.text
    got = [(s["kind"], s["path"]) for s in r.structured["data"]["symbols"]]
    assert got == [("Function", "src/shop/services/limits.py"), ("Key", "conf/app.yaml")]

    # Key / Tag は symbol_kinds で明示すれば出る。既定（全種別）の順位は決定的で、同じ呼び出しは同じ順序を返す
    only_keys = await search(pipeline_with_data, query="retry", match="exact", symbol_kinds=["key"])
    assert [s["path"] for s in only_keys.structured["data"]["symbols"]] == ["conf/app.yaml"]
    again = await search(pipeline_with_data, query="retry", match="exact")
    assert urns(again) == urns(r)

    # 一致の質（完全 > 前方 > 部分）が先で、その中でだけコード > データ。どの query でも「データのあとにコード」は同順位内に現れない
    for query in ("service", "limits", "test", "ruff"):
        res = await search(pipeline_with_data, query=query, limit=200)
        assert not res.is_error, res.text
        syms = res.structured["data"]["symbols"]
        for tier_score in {s["score"] for s in syms}:
            kinds = [s["kind"] in ("Key", "Tag") for s in syms if s["score"] == tier_score]
            assert kinds == sorted(kinds), (query, tier_score, [s["qualified_name"] for s in syms])
