"""T-06 / T-07: テストの土台・フィクスチャのクラスを import して使うファイルが、参照と呼び出し階層に出る（BUG-0082）。

pytest がテストのディレクトリを ``sys.path`` に足すので通る兄弟モジュールの import（``from test_shared import Env``）と、
テストフィクスチャの中の絶対 import（``from app.engine import Packer``）を、索引が外部（``lk://_ext/``）でなく
ワークスペースのファイルに結ぶ（``syntax.imports.python_import_roots``）。小さなワークスペースを ``tmp_path`` に作って索引する。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.app import build_services
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

WS = "import_roots"
SHARED = "tests/contract/test_shared.py"
USER = "tests/contract/test_user.py"
HARNESS = "tests/contract/_harness.py"
RIG_USER = "tests/contract/test_rig.py"
ENGINE = "tests/fixtures/proj/app/engine.py"
SERVICE = "tests/fixtures/proj/app/service.py"

FILES = {
    SHARED: "class Env:\n    pass\n\n\ndef build_env() -> Env:\n    return Env()\n",
    USER: (
        "from test_shared import Env, build_env\n"
        "\n"
        "\n"
        "def test_uses_env():\n"
        "    env: Env = build_env()\n"
        "    assert env\n"
    ),
    HARNESS: "class Rig:\n    def pos(self) -> int:\n        return 1\n",
    RIG_USER: "from _harness import Rig\n\n\ndef test_rig():\n    rig = Rig()\n    assert rig.pos() == 1\n",
    "tests/fixtures/proj/app/__init__.py": "",
    ENGINE: "class Packer:\n    def pack(self):\n        return 1\n",
    SERVICE: "from app.engine import Packer\n\n\ndef known():\n    return Packer().pack()\n",
}


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("roots")
    root = base / WS  # ワークスペース ID はフォルダ名
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
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


async def _refs(pipeline: Pipeline, path: str, name: str) -> list[dict[str, Any]]:
    r = await pipeline.call_tool(
        "find_references_and_implementations",
        {"target": {"path": path, "symbol_name": name}, "query_type": "references", "format": "json"},
    )
    assert not r.is_error, r.text
    return list(r.structured["data"]["references"])


async def _incoming(pipeline: Pipeline, path: str, name: str) -> set[tuple[str, str]]:
    r = await pipeline.call_tool(
        "query_call_hierarchy",
        {"target": {"path": path, "symbol_name": name}, "direction": "incoming", "format": "json"},
    )
    assert not r.is_error, r.text
    nodes = r.structured["data"]["nodes"]
    return {(n.get("path") or "", n.get("name") or "") for n in nodes if n.get("depth", 0) > 0}


async def test_t06_references_to_shared_test_helper_include_importing_test(pipeline: Pipeline) -> None:
    """票の再現: ``Env`` の参照に、``from test_shared import Env`` と型注釈の使用（``test_user.py``）が出る。"""
    refs = _refs(pipeline, SHARED, "Env")
    got = {(x["path"], x["line"], x["use"]) for x in await refs}
    assert (USER, 1, "import") in got
    assert (USER, 5, "type") in got


async def test_t06_references_to_harness_class_include_its_users(pipeline: Pipeline) -> None:
    got = {(x["path"], x["line"]) for x in await _refs(pipeline, HARNESS, "Rig")}
    assert {(RIG_USER, 1), (RIG_USER, 5)} <= got
    got = {(x["path"], x["line"]) for x in await _refs(pipeline, HARNESS, "Rig.pos")}
    assert (RIG_USER, 6) in got


async def test_t06_references_inside_a_fixture_workspace(pipeline: Pipeline) -> None:
    got = {(x["path"], x["line"]) for x in await _refs(pipeline, ENGINE, "Packer")}
    assert {(SERVICE, 1), (SERVICE, 5)} <= got


async def test_t07_incoming_calls_to_shared_helper(pipeline: Pipeline) -> None:
    """``build_env`` の呼び出し元に ``test_user.py`` の ``test_uses_env`` が出る（外部の葉に結ばれていない）。"""
    got = await _incoming(pipeline, SHARED, "build_env")
    assert (USER, "test_uses_env") in got
