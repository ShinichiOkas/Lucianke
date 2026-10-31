"""BUG-0132: ワークスペースが pyright の設定を持つ親の下にあっても、basedpyright はワークスペースを解析の対象にする。

basedpyright 1.40.2 は、ワークスペースの根の ``pyproject.toml`` に ``[tool.pyright]`` / ``[tool.basedpyright]`` が無いと、その
``pyproject.toml`` が無いものとして親のディレクトリの ``pyrightconfig.json`` / ``pyproject.toml`` を探して採る。親の設定の
``include`` / ``exclude`` がワークスペースのファイルを外すと、呼び出し階層・参照の答えが空になる（または親の木を走査して返らない）。
Lucianke は根に ``pyrightconfig.json`` が無く ``pyproject.toml`` があるとき、その ``pyproject.toml`` を設定ファイルとして明示する
（``basedpyright.analysis.configFilePath``）。親を探させない。

* 実機: ``.venv`` の basedpyright に、``include`` が自分の下の別のディレクトリだけの ``pyrightconfig.json`` を持つ親の下に置いた
  F-POLY の複製（``pyrightconfig.json`` を外す。``pyproject.toml`` には ``[tool.pyright]`` が無い）を読ませる。見つからなければ skip。
* 設定の組み立て（根の設定の有無で ``configFilePath`` を返すか）は ``tests/unit/test_lsp_pyright_config_pin_bug0132.py``。
* 利用者の設定が対象を絞る（根の ``pyrightconfig.json`` が ``include: ["src"]``）と、basedpyright は ``tests/`` の呼び出しを探さない。
  T-06・T-07 は対象の外で名前を含むファイルを先に開かせてから聞き、``tests/`` の呼び出しも ``exact`` で返す（解析の対象の求め方と
  候補は ``tests/unit/test_pyright_scope_bug0132.py``）。
"""

# ruff: noqa: F811  (フィクスチャ registry を引数で受ける)

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from _lsp_settle import first_analysis
from test_T06_T07_lsp import (  # noqa: F401  (フィクスチャ registry)
    _BASEDPYRIGHT,
    _BASEDPYRIGHT_CMD,
    PC,
    POLY,
    READY_WAIT_MS,
    Env,
    needs_basedpyright,
    opened_env,
    registry,
)

from lucianke.core.procs import ProcessRegistry
from lucianke.core.urn import CodeSel, Urn
from lucianke.lsp.pool import LspPool
from lucianke.tools import _lsp_exact
from lucianke.workspace.pyenv import detect_python_env
from lucianke.workspace.types import Workspace

WS = "poly"
LIM = "src/shop/services/limits.py"
PAY = "src/shop/services/payment.py"
GET = {"urn": str(Urn(WS, LIM, CodeSel("LimitTable.get")))}


def _monorepo(tmp: Path) -> Path:
    """``tmp/mono/pyrightconfig.json``（``include: ["libs"]``）の下に F-POLY（設定ファイルなし）を置き、その根を返す。"""
    mono = tmp / "mono"
    (mono / "libs").mkdir(parents=True)
    (mono / "libs" / "other.py").write_text("def other() -> int:\n    return 1\n", encoding="utf-8")
    (mono / "pyrightconfig.json").write_text(json.dumps({"include": ["libs"]}), encoding="utf-8")
    root = mono / "poly"
    shutil.copytree(POLY, root, ignore=shutil.ignore_patterns("__pycache__", "pyrightconfig.json"))
    assert not (root / "pyrightconfig.json").exists()
    assert "[tool.pyright]" not in (root / "pyproject.toml").read_text(encoding="utf-8")
    return root.resolve()


@contextlib.asynccontextmanager
async def _opened(registry: ProcessRegistry, tmp_path: Path, root: Path, first: str) -> AsyncIterator[Env]:
    _lsp_exact.MEMO.clear()
    ws = Workspace(workspace_id=WS, root=root, python_env=detect_python_env(root, sys.executable))
    pool = LspPool(
        registry,
        servers=[_BASEDPYRIGHT],
        resolver=lambda defn, w: list(_BASEDPYRIGHT_CMD),
        init_timeout_ms=READY_WAIT_MS,
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    async with opened_env(registry, root, cache, pool, ws) as env:
        await pool.ensure_started("python", ws)
        assert await pool.wait_ready("python", ws, READY_WAIT_MS), pool.status("python", ws)
        handle = pool.get("python", ws)
        assert handle is not None
        await first_analysis(handle, [first])
        yield env


@pytest.fixture
async def nested(registry: ProcessRegistry, tmp_path: Path):
    async with _opened(registry, tmp_path, _monorepo(tmp_path), LIM) as env:
        yield env


@pytest.fixture
async def narrowed(registry: ProcessRegistry, tmp_path: Path):
    """根の ``pyrightconfig.json`` が ``include: ["src"]`` の F-POLY（``tests/`` は basedpyright の解析の対象の外）。"""
    root = tmp_path / "poly"
    shutil.copytree(POLY, root, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "pyrightconfig.json").write_text(json.dumps({"include": ["src"]}), encoding="utf-8")
    async with _opened(registry, tmp_path, root.resolve(), PAY) as env:
        yield env


@needs_basedpyright
async def test_t07_incoming_under_an_ancestor_pyright_config_finds_the_caller(nested: Env) -> None:
    """親の設定（``include: ["libs"]``）に引きずられず、``LimitTable.get`` の呼び出し元（payment.py の ``_validate_limit``）を
    LSP から ``exact`` で返す（以前は呼び出し元が空、または 10 秒の予算まで返らなかった）。"""
    started = time.perf_counter()
    r = await nested.calls(GET, "incoming", depth=1)
    elapsed = time.perf_counter() - started
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    assert "basedpyright" in meta["engine_used"], meta
    nodes = r.structured["data"]["nodes"]
    assert [(n["name"], n["call_site_path"]) for n in nodes] == [("PaymentProcessor._validate_limit", PAY)], nodes
    assert elapsed < _lsp_exact.EXACT_BUDGET_S, elapsed


@needs_basedpyright
async def test_t06_references_under_an_ancestor_pyright_config_include_the_workspace(nested: Env) -> None:
    r = await nested.refs(GET)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    assert "basedpyright" in meta["engine_used"], meta
    refs = r.structured["data"]["references"]
    text = (nested.root / PAY).read_text(encoding="utf-8").splitlines()
    line = next(i + 1 for i, t in enumerate(text) if "self._limits.get(" in t)
    assert [(x["path"], x["line"]) for x in refs] == [(PAY, line)], refs


@needs_basedpyright
async def test_t06_references_outside_a_narrowed_include_are_exact(narrowed: Env) -> None:
    """``include: ["src"]`` でも、``tests/test_payment.py`` の呼び出しを ``exact`` の答えに含める（以前は routes.py の 2 箇所だけを
    ``exact`` で返し、テストの呼び出しが黙って落ちていた）。"""
    r = await narrowed.refs(PC)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    assert "basedpyright" in meta["engine_used"], meta
    refs = r.structured["data"]["references"]
    assert sorted({x["path"] for x in refs}) == ["src/shop/api/routes.py", "tests/test_payment.py"], refs
    assert r.structured["data"]["totals"] == {"files": 2, "sites": 3, "tests": 1}


@needs_basedpyright
async def test_t07_incoming_outside_a_narrowed_include_is_exact(narrowed: Env) -> None:
    r = await narrowed.calls(PC, "incoming", depth=1)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    assert "basedpyright" in meta["engine_used"], meta
    nodes = {n["name"]: n for n in r.structured["data"]["nodes"]}
    assert set(nodes) == {"create_order", "retry_order", "test_charge_ok"}, nodes
    assert nodes["test_charge_ok"]["call_site_path"] == "tests/test_payment.py"
    assert {n["confidence_level"] for n in nodes.values()} == {"exact"}
