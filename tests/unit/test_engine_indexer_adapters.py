"""``engine.indexer`` が Tier 3 / ipynb のデータ形式アダプタのシンボルを索引に入れるかを見る（設計書 §7.6.2、§7.4.8）。

``tests/fixtures/poly`` を ``tmp_path`` にコピーしてワークスペースにし、実 DB を作って ``scan`` する。JSON / YAML / TOML / XML は最上位の節が
``symbols`` 行になり（URN の式は同じアダプタの ``select`` で解ける）、JSONL・ipynb・壊れたファイル・大きすぎるファイルは ``files`` 行だけになる。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.urn import parse as parse_urn
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.syntax.adapters._symbols import MAX_SYMBOL_SOURCE_BYTES
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS_ID = "poly"


@dataclass
class Env:
    root: Path
    indexer: Indexer
    writer: IndexWriter
    readers: ReaderPool

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def symbols_of(self, path: str) -> list[tuple[str, str, str]]:
        """``(qualified_name, kind, urn)``（行の順）。"""
        return self.rows(
            "SELECT s.qualified_name, s.kind, s.urn FROM symbols s JOIN files f USING(file_id)"
            " WHERE f.path = ? ORDER BY s.start_line, s.qualified_name",
            path,
        )

    def file_row(self, path: str) -> tuple[Any, ...] | None:
        rows = self.rows("SELECT language, tier, confidence_level FROM files WHERE path = ?", path)
        return rows[0] if rows else None


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root)
    writer = IndexWriter(tmp_path / "db" / "paad.sqlite")
    readers = ReaderPool(tmp_path / "db" / "paad.sqlite", size=2)
    ws = Workspace(workspace_id=WS_ID, root=root.resolve())
    indexer = Indexer(ws, writer, readers, TreeCache())
    yield Env(root, indexer, writer, readers)
    await indexer.aclose()
    readers.close()
    writer.close()


def _write(env: Env, rel: str, data: bytes) -> None:
    p = env.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.mark.parametrize(
    ("path", "kind", "names", "urn_tails"),
    [
        (
            "conf/settings.json",
            "Key",
            ["service", "limits", "features"],
            ["jsonpath:$.service", "jsonpath:$.limits", "jsonpath:$.features"],
        ),
        ("conf/app.yaml", "Key", ["service", "retry"], ["yaml:service", "yaml:retry"]),
        ("pyproject.toml", "Key", ["project", "tool.ruff"], ["toml:project", "toml:tool.ruff"]),
        (
            "reports/junit.xml",
            "Tag",
            ["testsuites", "testsuites.testsuite"],
            ["xpath:/testsuites", "xpath:/testsuites/testsuite"],
        ),
    ],
)
async def test_data_files_index_top_level_sections(
    env: Env, path: str, kind: str, names: list[str], urn_tails: list[str]
) -> None:
    report = await env.indexer.scan()
    assert not report.errors, report.errors

    syms = env.symbols_of(path)
    assert [s[0] for s in syms] == names
    assert {s[1] for s in syms} == {kind}
    for (_, _, urn), tail in zip(syms, urn_tails, strict=True):
        # セレクタの式は URN の中で百分率符号化される（``/`` は ``%2F``）ので、解いた形で比べる
        parsed = parse_urn(urn)
        assert (parsed.workspace_id, parsed.path) == (WS_ID, path), urn
        assert f"{parsed.selector.kind}:{parsed.selector.expr}" == tail, urn
    # files 行はアダプタのシンボルがあれば structural
    row = env.file_row(path)
    assert row is not None and row[1] == 3 and row[2] == "structural", row


async def test_jsonl_ipynb_have_only_a_files_row(env: Env) -> None:
    _write(env, "data/events.jsonl", b'{"a": 1}\n{"a": 2}\n{"b": 3}\n')
    await env.indexer.scan()

    for path, language in [("data/events.jsonl", "jsonl"), ("notebooks/eda.ipynb", "ipynb")]:
        row = env.file_row(path)
        assert row is not None, path
        assert row[0] == language
        assert env.symbols_of(path) == [], path
        assert row[2] == "lexical_fallback", (path, row)  # シンボルが無いので構造の信頼度は付けない


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("broken/a.json", b'{"a": '),
        ("broken/a.yaml", b"a: [1, 2\nb: {x\n"),
        ("broken/a.toml", b"a = \n["),
        ("broken/a.xml", b"<a><b></a>"),
        ("broken/binary.json", b"\xff\xfe\x00\x01"),
    ],
)
async def test_broken_files_have_only_a_files_row(env: Env, path: str, source: bytes) -> None:
    _write(env, path, source)
    report = await env.indexer.scan()

    assert not report.errors, report.errors  # 壊れたデータファイルは走査の失敗にしない
    assert env.file_row(path) is not None
    assert env.symbols_of(path) == []


async def test_files_over_the_symbol_size_limit_have_only_a_files_row(env: Env) -> None:
    pad = "x" * (MAX_SYMBOL_SOURCE_BYTES + 1)
    _write(env, "big/large.json", json.dumps({"a": pad}).encode("utf-8"))
    _write(env, "big/small.json", json.dumps({"a": pad[: MAX_SYMBOL_SOURCE_BYTES // 2]}).encode("utf-8"))
    await env.indexer.scan()

    assert env.file_row("big/large.json") is not None
    assert env.symbols_of("big/large.json") == []  # 128 KiB 超は索引にシンボルを出さない
    assert [s[0] for s in env.symbols_of("big/small.json")] == ["a"]  # 境界の内側は出す
