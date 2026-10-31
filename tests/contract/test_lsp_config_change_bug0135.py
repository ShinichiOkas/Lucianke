"""BUG-0135: LSP の起動の後にワークスペースの根の設定（``pyrightconfig.json`` / ``pyproject.toml``）を作る・書き換える・消すと、
監視の経路（``IndexerSink`` → ``LspPool.notify_watched``）で basedpyright に届き、Lucianke の見る解析の対象と設定ファイルの明示の判断も
引き直す。

basedpyright 1.40.2 は ``workspace/didChangeWatchedFiles`` で根の設定の変更を読み直す（``include`` を狭めた・``pyproject.toml`` に節を
足した）。ただし設定ファイルを明示（``analysis.configFilePath``。BUG-0132）している間は根に ``pyrightconfig.json`` を作っても読まず、
根の ``pyrightconfig.json`` を消すと（明示していないので）親のディレクトリの設定を探して採る。明示の判断が変わったときは
``workspace/configuration`` の設定を差し替えて ``workspace/didChangeConfiguration`` を送る（サーバーが設定を引き直して読み直す）。

* 実機: ``.venv`` の basedpyright に F-POLY の複製を読ませる。見つからなければ skip。設定の変更は監視が拾ったときと同じく
  ``IndexerSink.changed`` / ``removed`` に渡す（監視の時間に依らない）。サーバーが読み直した合図は、開いている文書の診断の再発行。
* 偽のサーバーでの送る順序・判断は ``tests/unit/test_lsp_config_notify_bug0135.py``。
"""

# ruff: noqa: F811  (フィクスチャ registry を引数で受ける)

from __future__ import annotations

import contextlib
import json
import shutil
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from _lsp_settle import settle
from test_lsp_ancestor_pyright_config_bug0132 import GET, LIM, _monorepo, _opened
from test_T06_T07_lsp import (  # noqa: F401  (フィクスチャ registry)
    PAY,
    PC,
    POLY,
    Env,
    needs_basedpyright,
    registry,
)

from lucianke.core.model import Position, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.lsp import features
from lucianke.lsp.pool import LspHandle
from lucianke.tools import _lsp_exact
from lucianke.tools._runtime import IndexerSink

RELOAD_WAIT_MS = 60_000
"""サーバーが設定を読み直して開いた文書の診断を出し直すまでの上限（条件待ち。届けば即戻る）。"""
TYPO = "src/shop/typo.py"


def _poly(tmp_path: Path, *, config: dict[str, object] | None) -> Path:
    """F-POLY の複製。``config`` が ``None`` なら根に ``pyrightconfig.json`` を置かない（``pyproject.toml`` を明示する起動）。"""
    root = tmp_path / "poly"
    shutil.copytree(POLY, root, ignore=shutil.ignore_patterns("__pycache__", "pyrightconfig.json"))
    if config is not None:
        (root / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")
    return root.resolve()


def _handle(env: Env) -> LspHandle:
    handle = env.pool.get("python", env.ws)
    assert handle is not None
    return handle


async def _change(env: Env, rel: str, write: Callable[[Path], None], *, reloaded: str) -> None:
    """``rel`` を書き換え（``write``）、監視が拾ったときと同じく ``IndexerSink`` に渡す。サーバーが読み直して ``reloaded``（開いて
    いる文書）の診断を出し直すまで待つ（出し直した後の 1 秒の静止も待つ）。"""
    handle = _handle(env)
    sink = IndexerSink(env.ws, env.indexer, env.readers, env.pool)
    sent_at = time.perf_counter()
    write(env.root / rel)
    if (env.root / rel).exists():
        await sink.changed(RelPath(rel))
    else:
        await sink.removed(RelPath(rel))
    assert await handle.diagnostics.wait_for(
        RelPath(reloaded), min_version=None, sent_at=sent_at, timeout_ms=RELOAD_WAIT_MS, settle_ms=1000
    ), f"basedpyright did not re-publish the diagnostics of {reloaded} after {rel} changed (it did not reload the config)"


async def _server_refs(env: Env) -> set[str]:
    """``process_charge`` の参照（LSP に直接。文書は開かせない）のファイル。"""
    text = (env.root / PAY).read_text(encoding="utf-8").splitlines()
    line = next(i for i, t in enumerate(text) if "async def process_charge" in t)
    refs = await features.references(
        _handle(env), RelPath(PAY), Position(line, text[line].index("process_charge")), include_declaration=False
    )
    return {str(r.rel) for r in refs}


def _write_json(config: dict[str, object]) -> Callable[[Path], None]:
    return lambda p: p.write_text(json.dumps(config), encoding="utf-8")


def _append_section(p: Path) -> None:
    p.write_text(p.read_text(encoding="utf-8") + '\n[tool.basedpyright]\ninclude = ["src"]\n', encoding="utf-8")


# --- 起動の後に対象を狭める: サーバーは読み直す。Lucianke の見る対象も合わせて、対象の外の呼び出しを開かせる -----------------


@contextlib.asynccontextmanager
async def _narrowed_after_start(
    registry: ProcessRegistry, tmp_path: Path, config_file: str
) -> AsyncIterator[Env]:
    """``include`` を書かずに起こし（``pyrightconfig.json`` は ``typeCheckingMode`` だけ、または ``pyproject.toml`` を明示）、起動の後に
    ``include: ["src"]`` に狭める。サーバーが ``tests/`` を探さなくなった（直接の参照から消えた）のを見てから渡す。"""
    root = _poly(tmp_path, config={"typeCheckingMode": "standard"} if config_file == "pyrightconfig.json" else None)
    async with _opened(registry, tmp_path, root, PAY) as env:
        handle = _handle(env)
        assert handle.analysis_scope is not None and not handle.analysis_scope.narrowed
        if config_file == "pyrightconfig.json":
            write = _write_json({"typeCheckingMode": "standard", "include": ["src"]})
        else:
            write = _append_section
        await _change(env, config_file, write, reloaded=PAY)

        async def narrowed() -> bool:
            return "tests/test_payment.py" not in await _server_refs(env)

        await settle(narrowed, what="basedpyright narrowed its analysis to src/", timeout_s=60)
        yield env


@needs_basedpyright
@pytest.mark.parametrize("config_file", ["pyrightconfig.json", "pyproject.toml"])
async def test_t06_after_narrowing_the_root_config_still_includes_tests(
    registry: ProcessRegistry, tmp_path: Path, config_file: str
) -> None:
    """起動の後に ``include: ["src"]`` に狭めても、``tests/test_payment.py`` の呼び出しを ``exact`` の答えに含める（以前は起動のときの
    対象〔狭めていない〕で開かせる候補を決めたので、tests を開かせず routes.py の 2 箇所だけを ``exact`` で返した）。"""
    async with _narrowed_after_start(registry, tmp_path, config_file) as env:
        r = await env.refs(PC)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    refs = r.structured["data"]["references"]
    assert sorted({x["path"] for x in refs}) == ["src/shop/api/routes.py", "tests/test_payment.py"], refs
    assert r.structured["data"]["totals"] == {"files": 2, "sites": 3, "tests": 1}


@needs_basedpyright
async def test_t07_incoming_after_narrowing_the_root_config_still_includes_tests(
    registry: ProcessRegistry, tmp_path: Path
) -> None:
    async with _narrowed_after_start(registry, tmp_path, "pyrightconfig.json") as env:
        r = await env.calls(PC, "incoming", depth=1)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    nodes = {n["name"]: n for n in r.structured["data"]["nodes"]}
    assert set(nodes) == {"create_order", "retry_order", "test_charge_ok"}, nodes
    assert nodes["test_charge_ok"]["call_site_path"] == "tests/test_payment.py"


# --- 明示の判断が変わる: 設定を差し替えて didChangeConfiguration ------------------------------------------------------------


@needs_basedpyright
async def test_deleting_the_root_pyrightconfig_under_an_ancestor_config_keeps_the_workspace(
    registry: ProcessRegistry, tmp_path: Path
) -> None:
    """``include: ["libs"]`` の ``pyrightconfig.json`` を持つ親の下で、根の ``pyrightconfig.json`` を起動の後に消しても、親の設定を
    採らない（根の ``pyproject.toml`` を明示し直す）。以前は消したことだけが届き、サーバーが親の設定を採ってワークスペースを解析の
    対象から外し、T-07 は ``structural`` に降格した。"""
    root = _monorepo(tmp_path)
    (root / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    async with _opened(registry, tmp_path, root, LIM) as env:
        assert _handle(env).pinned_config is None
        await _change(env, "pyrightconfig.json", lambda p: p.unlink(), reloaded=LIM)
        assert _handle(env).pinned_config == root / "pyproject.toml"
        _lsp_exact.MEMO.clear()
        started = time.perf_counter()
        r = await env.calls(GET, "incoming", depth=1)
        elapsed = time.perf_counter() - started
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    nodes = r.structured["data"]["nodes"]
    assert [(n["name"], n["call_site_path"]) for n in nodes] == [("PaymentProcessor._validate_limit", PAY)], nodes
    assert elapsed < _lsp_exact.EXACT_BUDGET_S, elapsed


@needs_basedpyright
async def test_creating_a_root_pyrightconfig_over_the_pinned_pyproject_reaches_the_server(
    registry: ProcessRegistry, tmp_path: Path
) -> None:
    """``pyproject.toml`` を明示して起こした後に根の ``pyrightconfig.json`` を作ると、サーバーはそれを読む（明示を外す）。以前は明示した
    ``pyproject.toml`` を読み続け、作った設定（ここでは ``reportAssignmentType: "none"``）が診断に効かなかった。"""
    root = _poly(tmp_path, config=None)
    (root / TYPO).write_text('value: int = "text"\n', encoding="utf-8")
    async with _opened(registry, tmp_path, root, TYPO) as env:
        handle = _handle(env)
        assert handle.pinned_config == root / "pyproject.toml"
        entry = handle.diagnostics.get(RelPath(TYPO))
        assert entry is not None and any(d.get("code") == "reportAssignmentType" for d in entry.diagnostics), entry
        await _change(env, "pyrightconfig.json", _write_json({"reportAssignmentType": "none"}), reloaded=TYPO)
        assert handle.pinned_config is None
        entry = handle.diagnostics.get(RelPath(TYPO))
        assert entry is not None and not any(d.get("code") == "reportAssignmentType" for d in entry.diagnostics), entry
