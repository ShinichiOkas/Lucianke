"""``engine.indexer`` が作る ``relations`` の目標（設計書 §7.4.6 項 3、仕様書 §9.3、T-06・T-07 の要求）。

小さなワークスペースを ``tmp_path`` に作って索引し、SQL で見る。外部シンボル（``lk://_ext/``）、組み込み名、
オーバーロードへの目標、外部シンボルの ``kind`` を確かめる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

WS_ID = "mini"

FILES = {
    "pkg/__init__.py": "",
    "pkg/errors.py": "class AppError(Exception):\n    pass\n",
    "pkg/svc.py": (
        "import os\n"
        "from decimal import Decimal\n"
        "from pkg.errors import AppError\n"
        "\n"
        "\n"
        "class Failure(AppError):\n"
        "    pass\n"
        "\n"
        "\n"
        "def run(a: str):\n"
        "    if a:\n"
        "        os.getcwd()\n"
        "    raise ValueError(Decimal(a).quantize(Decimal('1')))\n"
    ),
    "web/shapes.ts": (
        'import { Base } from "./base";\n'
        'import { debounce } from "lodash";\n'
        "\n"
        "export interface Drawable { draw(): void; }\n"
        "export class Circle extends Base implements Drawable {\n"
        "  draw(): void { debounce(() => 1); }\n"
        "}\n"
        "export class Boom extends Error {}\n"
    ),
    "web/base.ts": "export class Base {}\n",
    "web/client.ts": (
        "export class Client {\n"
        "  charge(req: string): string;\n"
        "  charge(req: string, retries: number): string;\n"
        "  charge(req: string, retries = 0): string { return req + retries; }\n"
        "}\n"
    ),
    "web/app.ts": (
        'import { Client } from "./client";\n'
        "const client = new Client();\n"
        "export function checkout(): string {\n"
        '  return client.charge("x");\n'
        "}\n"
    ),
}


@dataclass
class Env:
    indexer: Indexer
    readers: ReaderPool

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "mini"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache())
    report = await indexer.scan()
    assert not report.errors, report.errors
    yield Env(indexer, readers)
    await indexer.aclose()
    readers.close()
    writer.close()


def edges(env: Env, kind: str) -> set[tuple[str, str]]:
    return {
        (s.removeprefix(f"lk://{WS_ID}/"), t.removeprefix(f"lk://{WS_ID}/"))
        for s, t in env.rows("SELECT source_urn, target_urn FROM relations WHERE relation_kind = ?", kind)
    }


async def test_unresolved_imports_become_external_symbols(env: Env) -> None:
    assert {
        ("pkg/svc.py", "lk://_ext/os"),
        ("pkg/svc.py", "lk://_ext/decimal#Decimal"),
        ("web/shapes.ts", "lk://_ext/lodash#debounce"),
    } <= edges(env, "IMPORTS")
    rows = env.rows("SELECT file_id, is_external FROM symbols WHERE urn = ?", "lk://_ext/decimal#Decimal")
    assert rows == [(None, 1)]


async def test_inherits_and_implements_rows(env: Env) -> None:
    assert edges(env, "INHERITS") >= {
        ("pkg/errors.py#AppError", "lk://_ext/builtins#Exception"),
        ("pkg/svc.py#Failure", "pkg/errors.py#AppError"),
        ("web/shapes.ts#Circle", "web/base.ts#Base"),
        ("web/shapes.ts#Boom", "lk://_ext/globals#Error"),
    }
    assert ("web/shapes.ts#Circle", "web/shapes.ts#Drawable") in edges(env, "IMPLEMENTS")


async def test_calls_to_external_functions_classes_and_members(env: Env) -> None:
    run = "pkg/svc.py#run"
    calls = edges(env, "CALLS")
    assert {
        (run, "lk://_ext/os#getcwd"),
        (run, "lk://_ext/builtins#ValueError"),
        (run, "lk://_ext/decimal#Decimal"),
        (run, "lk://_ext/decimal#Decimal.quantize"),
        ("web/shapes.ts#Circle.draw", "lk://_ext/lodash#debounce"),
    } <= calls
    guard = env.rows(
        "SELECT guard_condition FROM relations WHERE target_urn = ? AND relation_kind = 'CALLS'",
        "lk://_ext/os#getcwd",
    )
    assert guard == [("a",)]


async def test_external_symbol_kinds(env: Env) -> None:
    kinds = dict(env.rows("SELECT urn, kind FROM symbols WHERE is_external = 1"))
    assert kinds["lk://_ext/os"] == "Module"
    assert kinds["lk://_ext/decimal#Decimal"] == "Class"
    assert kinds["lk://_ext/decimal#Decimal.quantize"] == "Method"
    assert kinds["lk://_ext/builtins#Exception"] == "Class"
    assert kinds["lk://_ext/builtins#ValueError"] == "Class"
    assert kinds["lk://_ext/os#getcwd"] == "Function"
    assert kinds["lk://_ext/lodash#debounce"] == "Function"
    assert kinds["lk://_ext/globals#Error"] == "Class"


async def test_call_to_an_overloaded_method_points_at_the_implementation(env: Env) -> None:
    """``Client.charge`` はオーバーロード宣言が 2 つと実装が 1 つ。arity なしの目標は、最後の宣言（実装）の URN に直る。"""
    targets = [
        t.removeprefix(f"lk://{WS_ID}/")
        for (t,) in env.rows(
            "SELECT target_urn FROM relations WHERE source_urn = ? AND relation_kind = 'CALLS'",
            f"lk://{WS_ID}/web/app.ts#checkout",
        )
    ]
    assert len(targets) == 1
    impl = env.rows(
        "SELECT urn FROM symbols WHERE qualified_name = 'Client.charge' ORDER BY start_line DESC LIMIT 1"
    )[0][0]
    assert targets[0] == impl.removeprefix(f"lk://{WS_ID}/")
    assert env.rows("SELECT COUNT(*) FROM symbols WHERE urn = ?", impl) == [(1,)]
