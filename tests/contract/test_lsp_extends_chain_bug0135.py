"""BUG-0135 の残り: ``pyrightconfig.json`` の ``extends`` の先の設定ファイルが LSP の起動の後に変わっても、サーバーに届き、Lucianke の
見る解析の対象（``LspHandle.analysis_scope``）も読み直す。

basedpyright 1.40.2 は ``workspace/didChangeWatchedFiles`` で ``extends`` の先の変更も読み直す（根の下でも根の外でも。実機で確認:
``didChangeConfiguration`` は要らない）。ただし Lucianke の監視は根の下しか見ず、索引に載らない設定ファイルは知らせていなかった。

* 根の下の先（``config/base.json``）: 監視（``IndexerSink``）が ``LspPool.is_config_chain_file`` で設定ファイルとして知らせる。
* 根の外の先（``tmp_path/shared/base.json``）: 監視が見ないので、T-06・T-07 が LSP に聞く前に ``LspPool.sync_config_chain`` が刻みの
  変化を見つけて知らせる。
* 実機: ``.venv`` の basedpyright に F-POLY の複製を読ませる。見つからなければ skip。
"""

# ruff: noqa: F811  (フィクスチャ registry を引数で受ける)

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from _lsp_settle import settle
from test_lsp_ancestor_pyright_config_bug0132 import _opened
from test_lsp_config_change_bug0135 import (
    RELOAD_WAIT_MS,
    _handle,
    _poly,
    _server_refs,
)
from test_T06_T07_lsp import (  # noqa: F401  (フィクスチャ registry)
    PAY,
    PC,
    Env,
    needs_basedpyright,
    registry,
)

from lucianke.core.model import RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.tools import _lsp_exact
from lucianke.tools._runtime import IndexerSink

TESTS = "tests/test_payment.py"


def _include(root: Path, base: Path, names: list[str]) -> list[str]:
    """``base``（``extends`` の先）の ``include``。パスは書いた設定のディレクトリから（根の外の先は絶対パスにする）。"""
    return [(root / n).as_posix() for n in names]


def _setup(tmp_path: Path, where: str, names: list[str]) -> tuple[Path, Path, Callable[[list[str]], None]]:
    """根の ``pyrightconfig.json`` は ``extends`` だけ。先の ``include`` を書き換える関数を返す。"""
    root = _poly(tmp_path, config=None)
    base = root / "config" / "base.json" if where == "in" else tmp_path / "shared" / "base.json"
    base.parent.mkdir()
    ext = "./config/base.json" if where == "in" else str(base)
    (root / "pyrightconfig.json").write_text(json.dumps({"extends": ext}), encoding="utf-8")

    def write(new: list[str]) -> None:
        base.write_text(json.dumps({"include": _include(root, base, new)}), encoding="utf-8")

    write(names)
    return root, base, write


async def _reloaded(env: Env, base: Path, where: str, rel_in_root: str | None, *, narrowed: bool) -> None:
    """先を書き換えた後の届け方（根の下は監視、根の外は T-06 の前の刻みの確認）を行い、サーバーが読み直すのを待つ。"""
    handle = _handle(env)
    if where == "in":
        assert rel_in_root is not None
        sink = IndexerSink(env.ws, env.indexer, env.readers, env.pool)
        await sink.changed(RelPath(rel_in_root))
    else:
        assert await env.pool.sync_config_chain(handle)

    async def server_follows() -> bool:
        return (TESTS not in await _server_refs(env)) == narrowed

    await settle(server_follows, what="basedpyright reloaded the extended config", timeout_s=RELOAD_WAIT_MS / 1000)


@needs_basedpyright
@pytest.mark.parametrize("where", ["in", "out"])
async def test_narrowing_the_extended_config_reaches_the_server_and_scope(
    registry: ProcessRegistry, tmp_path: Path, where: str
) -> None:
    """``extends`` の先の ``include`` を ``[src]`` に狭めると、サーバーは ``tests/`` を探さなくなる。Lucianke の見る対象も狭まり、
    T-06 は ``tests/test_payment.py`` を開かせて ``exact`` の答えに含める（以前は対象を起動のときのままにして開かせず、routes.py の
    2 箇所だけを返した。根の外の先はサーバーにも届かなかった）。"""
    root, base, write = _setup(tmp_path, where, ["src", "tests"])
    async with _opened(registry, tmp_path, root, PAY) as env:
        handle = _handle(env)
        assert handle.analysis_scope is not None and handle.analysis_scope.narrowed
        assert handle.analysis_scope.contains(root / TESTS)
        write(["src"])
        await _reloaded(env, base, where, "config/base.json", narrowed=True)
        assert handle.analysis_scope is not None and not handle.analysis_scope.contains(root / TESTS)
        _lsp_exact.MEMO.clear()
        r = await env.refs(PC)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    refs = r.structured["data"]["references"]
    assert sorted({x["path"] for x in refs}) == ["src/shop/api/routes.py", TESTS], refs
    assert r.structured["data"]["totals"] == {"files": 2, "sites": 3, "tests": 1}


@needs_basedpyright
@pytest.mark.parametrize("where", ["in", "out"])
async def test_widening_the_extended_config_returns_the_widened_scope_at_once(
    registry: ProcessRegistry, tmp_path: Path, where: str
) -> None:
    """``extends`` の先の ``include`` を ``[src]`` → ``[src, tests]`` に広げた直後の T-06 が、広がった対象の参照を返す（根の外の先は
    T-06 がサーバーへ届けてから聞く。サーバーが読み直す前でも、開かせる候補は前の対象でも決めるので取りこぼさない）。"""
    root, base, write = _setup(tmp_path, where, ["src"])
    async with _opened(registry, tmp_path, root, PAY) as env:
        handle = _handle(env)
        assert handle.analysis_scope is not None and not handle.analysis_scope.contains(root / TESTS)
        write(["src", "tests"])
        if where == "in":
            await IndexerSink(env.ws, env.indexer, env.readers, env.pool).changed(RelPath("config/base.json"))
        started = time.perf_counter()
        _lsp_exact.MEMO.clear()
        r = await env.refs(PC)
        assert time.perf_counter() - started < 60
        assert handle.analysis_scope is not None and handle.analysis_scope.contains(root / TESTS)
    assert not r.is_error, r.text
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["is_degraded"] is False, meta
    refs = r.structured["data"]["references"]
    assert sorted({x["path"] for x in refs}) == ["src/shop/api/routes.py", TESTS], refs
