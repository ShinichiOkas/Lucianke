"""BUG-0018: ``revision`` を受ける読み取りツール（T-02 / T-05）が過去断面・ステージから読む契約。

リポジトリは ``tmp_path`` に dulwich だけで作る。作業ツリー・ステージ・HEAD~1 で ``pay.py`` の中身を変えてあり、
どの ``revision`` でも「その断面の内容」が返ることを確かめる。Git オブジェクトを読むだけで、作業ツリーにも ``.git`` にも書かない。
存在しない参照は ``E_REVISION_NOT_FOUND``。過去断面を扱わないツール（T-03・T-04・T-06・T-07・T-08）は、解決できる参照に
``E_UNSUPPORTED``、解決できない参照に ``E_REVISION_NOT_FOUND`` を返す。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from dulwich import porcelain

from lucianke.app import build_services
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import register_tools

WHO = b"Tester <tester@example.invalid>"
T0 = 1_700_000_000

V1 = 'def charge(amount):\n    """Charge v1."""\n    return amount\n'
V2 = V1 + '\n\ndef refund(amount):\n    """Refund (added in v2)."""\n    return -amount\n'
V3 = V2 + '\n\ndef audit(amount):\n    """Audit (staged only)."""\n    return amount\n'
V4 = V3 + '\n\ndef scratch(amount):\n    """Scratch (working tree only)."""\n    return amount\n'
PYPROJECT = '[project]\nname = "shop"\nversion = "1.0"\n\n[tool.ruff]\nline-length = 100\n'
README = "# Shop\n\nIntro.\n\n## Install\n\n```sh\n# not a heading\n```\n\n## Usage\n\nRun it.\n"
LICENSE = "MIT License\n\nPermission is hereby granted, free of charge.\n"


def _write(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return str(p)


def _commit(repo: Any, root: Path, files: dict[str, str], n: int) -> str:
    paths = [_write(root, rel, text) for rel, text in files.items()]
    porcelain.add(repo, paths=paths)
    sha = porcelain.commit(
        repo,
        message=f"c{n}".encode(),
        author=WHO,
        committer=WHO,
        author_timestamp=T0 + n * 60,
        commit_timestamp=T0 + n * 60,
        author_timezone=0,
        commit_timezone=0,
    )
    return sha.decode("ascii")


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    """HEAD~1 = V1、HEAD = V2、ステージ = V3、作業ツリー = V4。``later.py`` は HEAD で足したファイル。"""
    root = tmp_path / "shop"
    root.mkdir()
    repo = porcelain.init(str(root))
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    _commit(repo, root, {"pay.py": V1, "pyproject.toml": PYPROJECT, "README.md": README, "LICENSE": LICENSE}, 1)
    _commit(repo, root, {"pay.py": V2, "later.py": "def later():\n    return 1\n"}, 2)
    _write(root, "pay.py", V3)
    porcelain.add(repo, paths=[str(root / "pay.py")])
    _write(root, "pay.py", V4)
    repo.close()
    return root


def pipeline_for(root: Path) -> Pipeline:
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=build_services(lambda: [str(root)]))


@pytest.fixture
def pipe(shop: Path) -> Pipeline:
    return pipeline_for(shop)


async def skeleton(pipe: Pipeline, fmt: str = "json", **args: Any):
    return await pipe.call_tool("get_file_skeleton", {"format": fmt, **args})


async def bodies(pipe: Pipeline, fmt: str = "json", **args: Any):
    return await pipe.call_tool("get_symbol_bodies", {"format": fmt, **args})


def names(r: Any) -> list[str]:
    return [s["name"] for s in r.structured["data"]["items"][0]["symbols"]]


def snapshot(root: Path) -> dict[str, str]:
    """作業ツリーと ``.git`` の全ファイルの SHA-256（読み取りが何も書かないことの機械検査）。"""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- T-02 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("revision", "expected"),
    [
        ("HEAD~1", ["charge"]),
        ("HEAD", ["charge", "refund"]),
        ("STAGED", ["charge", "refund", "audit"]),
        ("WORKING_TREE", ["charge", "refund", "audit", "scratch"]),
    ],
)
async def test_t02_reads_each_revision(pipe: Pipeline, revision: str, expected: list[str]) -> None:
    r = await skeleton(pipe, paths=["pay.py"], revision=revision)
    assert not r.is_error, r.text
    assert names(r) == expected
    compact = await skeleton(pipe, "compact", paths=["pay.py"], revision=revision)
    assert f"rev={revision} " in compact.text.splitlines()[0]


async def test_t02_branch_sha_and_engine_name(pipe: Pipeline, shop: Path) -> None:
    from dulwich.repo import Repo

    with Repo(str(shop)) as repo:
        head = repo.head().decode("ascii")
    by_branch = await skeleton(pipe, paths=["pay.py"], revision="main")
    by_short = await skeleton(pipe, paths=["pay.py"], revision=head[:8])
    assert names(by_branch) == names(by_short) == ["charge", "refund"]
    meta = by_short.structured["_resolution_meta"]
    assert meta["engine_used"] == f"tree-sitter@{head[:7]}"
    assert meta["lsp_status"] == "n/a"
    assert any("without touching the working tree" in w for w in by_short.structured["warnings"])


@pytest.mark.parametrize("revision", ["no-such-rev-xyz", "HEAD~9", "HEAD@{1}", "HEAD:pay.py", "main..HEAD", "dead"])
async def test_nonexistent_revision_is_revision_not_found(pipe: Pipeline, revision: str) -> None:
    for call, args in (
        (skeleton, {"paths": ["pay.py"]}),
        (bodies, {"targets": [{"path": "pay.py", "symbol_name": "charge"}]}),
    ):
        r = await call(pipe, revision=revision, **args)
        assert r.is_error, (revision, r.text)
        err = r.structured["error"]
        assert err["code"] == "E_REVISION_NOT_FOUND", (revision, err)
        assert err["details"]["param"] == "revision" and err["details"]["reason"]


async def test_t02_file_missing_at_that_revision_is_an_item_failure(pipe: Pipeline) -> None:
    r = await skeleton(pipe, paths=["pay.py", "later.py"], revision="HEAD~1")
    assert not r.is_error
    ok = {i["path"]: i["ok"] for i in r.structured["data"]["items"]}
    assert ok == {"pay.py": True, "later.py": False}
    failed = r.structured["data"]["items"][1]["error"]
    assert failed["code"] == "E_TARGET_NOT_FOUND" and "HEAD~1" in failed["message"]


@pytest.mark.parametrize("revision", ["WORKING_TREE", "HEAD~1", "STAGED"])
async def test_t02_toml_markdown_and_unknown_files(pipe: Pipeline, revision: str) -> None:
    """BUG-0018 症状 2: TOML・Markdown・拡張子なし（Tier 4）も骨格を返す。"""
    r = await skeleton(pipe, paths=["pyproject.toml", "README.md", "LICENSE"], revision=revision)
    assert not r.is_error, r.text
    items = {i["path"]: i for i in r.structured["data"]["items"]}
    assert all(i["ok"] for i in items.values()), items
    assert [s["name"] for s in items["pyproject.toml"]["symbols"]] == ["project", "tool.ruff"]
    md = items["README.md"]["symbols"]
    assert [s["name"] for s in md] == ["Shop"] and [c["name"] for c in md[0]["children"]] == ["Install", "Usage"]
    assert items["LICENSE"]["confidence_level"] == "lexical_fallback" and items["LICENSE"]["tier"] == 4
    meta = r.structured["_resolution_meta"]
    assert meta["is_degraded"] is True and meta["tier"] == 4
    compact = await skeleton(pipe, "compact", paths=["README.md", "LICENSE"], revision=revision)
    assert "[Section] Shop" in compact.text and "! DEGRADED" in compact.text


# --- T-05 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("revision", "symbol", "needle", "missing"),
    [
        ("HEAD~1", "charge", "Charge v1", "refund"),
        ("HEAD", "refund", "Refund (added in v2)", "audit"),
        ("STAGED", "audit", "Audit (staged only)", "scratch"),
        ("WORKING_TREE", "scratch", "Scratch (working tree only)", None),
    ],
)
async def test_t05_reads_each_revision(
    pipe: Pipeline, revision: str, symbol: str, needle: str, missing: str | None
) -> None:
    r = await bodies(pipe, targets=[{"path": "pay.py", "symbol_name": symbol}], revision=revision)
    assert not r.is_error, r.text
    item = r.structured["data"]["items"][0]
    assert item["ok"] and needle in item["code"]
    assert r.structured["_resolution_meta"]["confidence_level"] == "structural"
    if missing is not None:  # その断面に無いシンボルは見つからない（過去断面の内容だけを見ている）
        miss = await bodies(pipe, targets=[{"path": "pay.py", "symbol_name": missing}], revision=revision)
        assert miss.is_error and miss.structured["error"]["code"] == "E_TARGET_NOT_FOUND"


async def test_t05_sha_is_the_revisions_content_sha(pipe: Pipeline) -> None:
    r = await bodies(pipe, targets=[{"path": "pay.py", "symbol_name": "charge"}], revision="HEAD~1")
    assert r.structured["data"]["items"][0]["file_sha256"] == hashlib.sha256(V1.encode()).hexdigest()


# --- 作業ツリーに触れない ------------------------------------------------------------------


async def test_reads_do_not_change_working_tree_or_git_dir(pipe: Pipeline, shop: Path) -> None:
    await skeleton(pipe, paths=["pay.py"])  # 先に 1 回流す（遅延生成物を落ち着かせる）
    before = snapshot(shop)
    for rev in ("HEAD~1", "HEAD", "STAGED", "main", "no-such-rev-xyz"):
        await skeleton(pipe, paths=["pay.py", "pyproject.toml", "README.md", "LICENSE", "later.py"], revision=rev)
        await bodies(pipe, targets=[{"path": "pay.py", "symbol_name": "charge"}], revision=rev)
    assert snapshot(shop) == before


# --- Git が無い作業ツリー、過去断面を扱わないツール -------------------------------------------


async def test_revision_in_a_directory_without_git(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    pipe = pipeline_for(tmp_path)
    assert not (await skeleton(pipe, paths=["a.py"])).is_error
    r = await skeleton(pipe, paths=["a.py"], revision="HEAD")
    assert r.is_error and r.structured["error"]["code"] == "E_REVISION_NOT_FOUND"
    assert r.structured["error"]["details"]["reason"] == "no_git_repository"


UNSUPPORTED = [
    ("search_workspace_symbols", {"query": "charge"}),
    ("resolve_symbols", {"targets": [{"path": "pay.py", "symbol_name": "charge"}]}),
    ("find_references_and_implementations", {"target": {"path": "pay.py", "symbol_name": "charge"}}),
    ("query_call_hierarchy", {"target": {"path": "pay.py", "symbol_name": "charge"}, "direction": "incoming"}),
    ("get_diagnostics", {}),
]


@pytest.mark.parametrize(("tool", "args"), UNSUPPORTED)
async def test_tools_without_revision_support(pipe: Pipeline, tool: str, args: dict[str, Any]) -> None:
    """M1 で過去断面を扱わないツール: 解決できる参照は E_UNSUPPORTED、存在しない参照は E_REVISION_NOT_FOUND。"""
    ok = await pipe.call_tool(tool, {"format": "json", **args, "revision": "HEAD"})
    assert ok.is_error and ok.structured["error"]["code"] == "E_UNSUPPORTED", ok.text
    assert ok.structured["error"]["details"]["reason"] == "revision_not_supported_until_M2"
    bad = await pipe.call_tool(tool, {"format": "json", **args, "revision": "no-such-rev-xyz"})
    assert bad.is_error and bad.structured["error"]["code"] == "E_REVISION_NOT_FOUND", bad.text
