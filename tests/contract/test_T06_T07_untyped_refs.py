"""T-06 / T-07: 点つきのモジュールのパスの受け手（BUG-0093）・import した変数の受け手（BUG-0094）・型の分からない受け手の属性の
読み書き（BUG-0095）。

フィクスチャ ``tests/fixtures/untyped_refs``（README に各箇所の意図）を ``Indexer`` で取り込んだ実索引（LSP なしの ``structural``）で
確かめる。黙って落とさない: 結べる参照は正直な水準（``structural`` / ``heuristic``・``match: name``）で出し、結べない参照は件数・候補・
先頭の位置を ``warnings`` と ``data.unattributed_refs`` に書く。型の分からない受け手を ``structural`` にしない（BUG-0085）。
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.app import build_services
from lucianke.core.urn import CodeSel, Urn
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t06_find_references_and_implementations as t06
from lucianke.tools import t07_query_call_hierarchy as t07
from lucianke.tools._services import ToolServices
from lucianke.workspace.types import Workspace

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "untyped_refs"
WS = "untyped_refs"


def U(path: str, qualified: str | None = None) -> str:
    return str(Urn(WS, path, CodeSel(qualified) if qualified else None))


def line_of(path: str, needle: str) -> int:
    lines = (FIXTURE / path).read_bytes().decode("utf-8").splitlines()
    return next(i + 1 for i, text in enumerate(lines) if needle in text)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("untyped_refs")
    root = base / WS
    shutil.copytree(FIXTURE, root)
    path = base / "db" / "paad.sqlite"

    async def build() -> None:
        writer = IndexWriter(path)
        readers = ReaderPool(path, size=2)
        indexer = Indexer(Workspace(workspace_id=WS, root=root.resolve()), writer, readers, TreeCache())
        try:
            report = await indexer.scan()
            assert not report.errors, report.errors
        finally:
            await indexer.aclose()
            readers.close()
            writer.close()

    asyncio.run(build())
    return path


@dataclass(slots=True)
class _Services(ToolServices):
    db_path: Path | None = None


@pytest.fixture
def pipeline(db: Path) -> Pipeline:
    base = build_services(lambda: [str(db.parent.parent / WS)])
    services = _Services(roots=base.roots, trees=base.trees, lines=base.lines, db_path=db)
    registry = ToolRegistry()
    registry.register(t06.TOOL_SPEC)
    registry.register(t07.TOOL_SPEC)
    return Pipeline(registry, services=services)


async def refs(pipeline: Pipeline, path: str, name: str, fmt: str = "json", **args: Any) -> Any:
    r = await pipeline.call_tool(
        "find_references_and_implementations",
        {"target": {"path": path, "symbol_name": name}, "query_type": "references", "format": fmt, **args},
    )
    assert not r.is_error, r.text
    return r


async def calls(pipeline: Pipeline, path: str, name: str, direction: str, **args: Any) -> Any:
    r = await pipeline.call_tool(
        "query_call_hierarchy",
        {"target": {"path": path, "symbol_name": name}, "direction": direction, "format": "json", **args},
    )
    assert not r.is_error, r.text
    return r


def sites(r: Any) -> set[tuple[str, int, str, str, str | None]]:
    return {
        (x["path"], x["line"], x["use"], x["confidence_level"], x.get("match"))
        for x in r.structured["data"]["references"]
    }


# --- BUG-0093 -----------------------------------------------------------------------------------


async def test_t06_module_function_called_through_a_dotted_module_path(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``flush`` の参照に ``pkg.helpers.flush()`` が ``structural`` で出る。``go`` には ``pkg.sub.deep.go()`` と
    ``ps.deep.go()``（``import pkg.sub as ps``）。"""
    r = await refs(pipeline, "pkg/helpers.py", "flush")
    assert sites(r) == {("use_modules.py", line_of("use_modules.py", "pkg.helpers.flush()"), "call", "structural", None)}
    r = await refs(pipeline, "pkg/sub/deep.py", "go")
    assert sites(r) == {
        ("use_modules.py", line_of("use_modules.py", "pkg.sub.deep.go()"), "call", "structural", None),
        ("use_modules.py", line_of("use_modules.py", "ps.deep.go()"), "call", "structural", None),
    }


async def test_t07_outgoing_lists_the_module_functions(pipeline: Pipeline) -> None:
    """PC:T-07#2 ``run`` の outgoing は ``flush``・``go``・名前空間パッケージの ``render``（どれも ``structural``）。"""
    r = await calls(pipeline, "use_modules.py", "run", "outgoing", depth=1)
    got = {(n["urn"], n["confidence_level"]) for n in r.structured["data"]["nodes"]}
    assert got == {
        (U("pkg/helpers.py", "flush"), "structural"),
        (U("pkg/sub/deep.py", "go"), "structural"),
        (U("nsp/tools/fmt.py", "render"), "structural"),
    }


# --- BUG-0094 -----------------------------------------------------------------------------------


async def test_t06_method_called_through_imported_objects(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``Settings.reload`` の参照: ``settings.reload()``・``typed.reload()`` は型が分かるので ``structural``。
    ``loose.reload()``（型が分からない）と再び公開した ``shared.reload()`` は名前の一致（``reload`` はワークスペースで唯一）で
    ``heuristic``・``match: name``。"""
    r = await refs(pipeline, "pkg/conf.py", "Settings.reload")
    assert sites(r) == {
        ("use_modules.py", line_of("use_modules.py", "conf.settings.reload()"), "call", "structural", None),
        ("use_conf.py", line_of("use_conf.py", "    settings.reload()"), "call", "structural", None),
        ("use_conf.py", line_of("use_conf.py", "typed.reload()"), "call", "structural", None),
        ("use_conf.py", line_of("use_conf.py", "loose.reload()"), "call", "heuristic", "name"),
        ("use_conf.py", line_of("use_conf.py", "shared.reload()"), "call", "heuristic", "name"),
    }
    assert r.structured["data"]["totals"]["by_name"] == 2
    assert any(
        "Settings.reload is the only method named 'reload' in the workspace" in w for w in r.structured["warnings"]
    ), r.structured["warnings"]
    debug = await refs(pipeline, "pkg/conf.py", "Settings.debug")
    assert sites(debug) == {
        ("use_conf.py", line_of("use_conf.py", "settings.debug"), "r", "structural", None),
        ("use_modules.py", line_of("use_modules.py", "conf.Settings.debug"), "r", "structural", None),
    }


async def test_t07_incoming_of_a_method_called_through_an_imported_object(pipeline: Pipeline) -> None:
    """PC:T-07#2 ``Settings.reload`` の incoming に ``apply`` と ``via_module``（``structural``。``apply`` には名前だけの
    ``loose.reload()`` もあるがノードは最も高い水準）。"""
    r = await calls(pipeline, "pkg/conf.py", "Settings.reload", "incoming", depth=1)
    got = {(n["urn"], n["confidence_level"], n.get("match")) for n in r.structured["data"]["nodes"]}
    assert got == {
        (U("use_conf.py", "apply"), "structural", None),
        (U("use_modules.py", "via_module"), "structural", None),
    }


# --- BUG-0095 -----------------------------------------------------------------------------------


async def test_t06_unique_field_read_and_written_through_an_untyped_receiver(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``Monitor.stdout_lines`` の参照に ``mon.stdout_lines += 1``（``rw``）が ``heuristic``・``match: name`` で出る
    （唯一の同名のメンバー）。見出しは ``by name: 1``、行末は ``conf=heuristic``。"""
    r = await refs(pipeline, "pkg/monitor.py", "Monitor.stdout_lines")
    assert sites(r) == {
        ("use_attrs.py", line_of("use_attrs.py", "mon.stdout_lines += 1"), "rw", "heuristic", "name"),
    }
    assert r.structured["data"]["totals"]["by_name"] == 1
    assert any(
        w.startswith("1 read/write reference(s) marked conf=heuristic are matched by name only")
        and "Monitor.stdout_lines is the only member named 'stdout_lines' in the workspace" in w
        for w in r.structured["warnings"]
    ), r.structured["warnings"]
    compact = await refs(pipeline, "pkg/monitor.py", "Monitor.stdout_lines", fmt="compact")
    assert g.validate(compact.text) == [], g.validate(compact.text)
    assert "(tests: 0; by name: 1)" in compact.text
    [line] = [ln for ln in compact.text.splitlines() if ln.startswith("use_attrs.py:")]
    assert "[rw]" in line and line.endswith("conf=heuristic")
    only_reads = await refs(pipeline, "pkg/monitor.py", "Monitor.stdout_lines", use_kinds=["r"])
    assert sites(only_reads) == set()


async def test_t06_ambiguous_field_name_is_reported_with_candidates(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``Monitor.total`` は ``mon.total`` を足さず、件数・候補（インスタンス属性 ``Counter.total`` を含む）・位置を
    ``warnings`` と ``data.unattributed_refs`` に書く。"""
    r = await refs(pipeline, "pkg/monitor.py", "Monitor.total")
    assert sites(r) == set()
    line = line_of("use_attrs.py", "if mon.total")
    [w] = [w for w in r.structured["warnings"] if "of .total" in w]
    assert w.startswith("1 read/write reference(s) of .total through a receiver of unknown type are not listed:")
    assert "2 members share the name" in w and "Counter.total (instance attribute) @pkg/counter.py:" in w
    assert f"e.g. use_attrs.py:{line}" in w and w.endswith("(data.unattributed_refs)")
    [u] = r.structured["data"]["unattributed_refs"]
    assert (u["name"], u["total"], u["reason"], u["candidates_total"]) == ("total", 1, "ambiguous", 2)
    assert [(c["qualified_name"], c["kind"]) for c in u["candidates"]] == [
        ("Counter.total", "instance_attribute"),
        ("Monitor.total", "Field"),
    ]
    assert u["sites"] == [
        {"path": "use_attrs.py", "line": line, "col": 12, "enclosing_name": "tick", "is_test": False, "use": "r"}
    ]
    assert "unattributed_calls" not in r.structured["data"]


async def test_t06_instance_attribute_of_a_subclass_counts_as_its_base_attribute(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``Gauge.hits`` の候補は ``Gauge.hits`` と ``Counter.hits`` の 2 つ（``Wide(Counter)`` の ``self.hits`` は
    ``Counter.hits`` と同じ属性。TS の ``Meter.hits`` は言語族が違うので数えない）。"""
    r = await refs(pipeline, "pkg/gauge.py", "Gauge.hits")
    [u] = r.structured["data"]["unattributed_refs"]
    assert [c["qualified_name"] for c in u["candidates"]] == ["Counter.hits", "Gauge.hits"]
    assert [(s["path"], s["use"]) for s in u["sites"]] == [("use_attrs.py", "rw")]


async def test_t06_builtin_attribute_name_is_reported(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``Monitor.name`` は ``mon.name`` を足さず、組み込み型の属性と同名（``builtin_name``）と書く。"""
    r = await refs(pipeline, "pkg/monitor.py", "Monitor.name")
    assert sites(r) == set()
    [u] = r.structured["data"]["unattributed_refs"]
    assert (u["name"], u["reason"], u["builtin_name"]) == ("name", "builtin_name", True)
    assert any("built-in types also have an attribute or method of that name" in w for w in r.structured["warnings"])


async def test_t06_name_shared_with_an_external_type_member_is_reported(pipeline: Pipeline) -> None:
    """PC:T-06#2 ``Monitor.parent`` は唯一の同名のメンバーでも、``p: Path`` の ``p.parent``（外部の ``pathlib.Path.parent``）がある
    ので ``node.parent`` を足さず、``reason: external_name`` と外部のメンバーを書く。"""
    r = await refs(pipeline, "pkg/monitor.py", "Monitor.parent")
    assert sites(r) == set()
    [u] = r.structured["data"]["unattributed_refs"]
    assert (u["name"], u["reason"], u["external_members"]) == ("parent", "external_name", ["pathlib.Path.parent"])
    assert [s["line"] for s in u["sites"]] == [line_of("use_attrs.py", "node.parent")]
    assert any(
        "external types used in the workspace also have a member of that name (pathlib.Path.parent)" in w
        for w in r.structured["warnings"]
    ), r.structured["warnings"]


async def test_t06_typescript_field_through_an_any_receiver(pipeline: Pipeline) -> None:
    """PC:T-06#2 TS の ``Meter.hits``: ``this.hits = 0`` は ``structural``、``m: any`` の ``m.hits += 1``・``m.hits`` は ``heuristic``。"""
    r = await refs(pipeline, "web/meter.ts", "Meter.hits")
    assert sites(r) == {
        ("web/meter.ts", line_of("web/meter.ts", "this.hits = 0"), "w", "structural", None),
        ("web/use.ts", line_of("web/use.ts", "m.hits += 1"), "rw", "heuristic", "name"),
        ("web/use.ts", line_of("web/use.ts", "return m.hits"), "r", "heuristic", "name"),
    }


async def test_t07_does_not_list_attribute_references(pipeline: Pipeline) -> None:
    """PC:T-07#2 ``tick`` の outgoing は呼び出し（``mon.violate()`` → ``Monitor.violate``、``heuristic``）だけで、属性の読み書きは入らない。"""
    r = await calls(pipeline, "use_attrs.py", "tick", "outgoing", depth=1)
    got = {(n["urn"], n["confidence_level"], n.get("match")) for n in r.structured["data"]["nodes"]}
    assert got == {(U("pkg/monitor.py", "Monitor.violate"), "heuristic", "name")}


async def test_t06_untyped_receiver_is_never_structural(pipeline: Pipeline) -> None:
    """PC:T-06#2 BUG-0085 の後戻りなし: 注釈の無い引数 ``x`` の ``x.save()``・``x.note`` は ``structural`` にならず、同じファイルの
    段の名前の一致（``heuristic``・「in the calling file」）で出る。"""
    save = await refs(pipeline, "untyped_bind.py", "Base.save")
    assert sites(save) == {("untyped_bind.py", line_of("untyped_bind.py", "x.save()"), "call", "heuristic", "name")}
    note = await refs(pipeline, "untyped_bind.py", "Base.note")
    assert sites(note) == {("untyped_bind.py", line_of("untyped_bind.py", "return x.note"), "r", "heuristic", "name")}
    assert any("Base.note is the only member named 'note' in the calling file" in w for w in note.structured["warnings"])
