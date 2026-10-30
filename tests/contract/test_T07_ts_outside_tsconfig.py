"""T-07 の ``incoming`` の ``exact`` 経路も、tsconfig の ``include`` に入らないファイル（``test/`` など）の呼び出し元を返す（20e。BUG-0047 の T-07 版）。

tsserver は ``include`` に入らないファイルを、開かれるまでどのプロジェクトにも入れず、``callHierarchy`` の呼び元に出さない。
T-06 と同じ部品（``prime_candidates`` / ``prime_documents``）で、対象の名前を含む tsserver 系のファイルを先に LSP へ開かせてから聞く。

* 実機: ``tests/contract/test_T06_ts_outside_tsconfig.py`` と同じ条件（typescript-language-server と ``typescript`` パッケージ）で走る。無ければ skip。
* 偽サーバーの試験（開く順序・上限・永続化）は ``tests/contract/test_T06_T07_exact_marks.py``。
* ``test/guard.test.ts`` の呼び出しはモジュール直下なので呼び元の関数が無く、incoming の木に出ない（T-07 は関数を数える）。
  そのため呼び出しを関数に包んだ ``test/calls.test.ts`` を足し、その関数 ``check`` が incoming に出ることを確かめる。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_T06_T07_lsp import Env, opened_env
from test_T06_ts_outside_tsconfig import (
    _TSLS,
    _TSLS_CMD,
    FILES,
    READY_WAIT_MS,
    TARGET,
    _link_typescript,
    needs_tsls,
    registry,  # noqa: F401  (フィクスチャ)
)

from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.pool import LspPool
from lucianke.workspace.types import Workspace

WS = "tsmini7"
CALLS = "test/calls.test.ts"
CALLS_SRC = "import {isThing} from '../source/guard.js';\n\nexport function check(): boolean {\n\treturn isThing(3);\n}\n"


@pytest.fixture
async def tsenv7(registry: ProcessRegistry, tmp_path: Path):  # noqa: F811
    root = tmp_path / WS
    for rel, text in {**FILES, CALLS: CALLS_SRC}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _link_typescript(root)
    ws = Workspace(workspace_id=WS, root=root.resolve())
    assert _TSLS is not None and _TSLS_CMD is not None
    pool = LspPool(
        registry,
        servers=[_TSLS],
        resolver=lambda defn, w: list(_TSLS_CMD),
        init_timeout_ms=READY_WAIT_MS,
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    async with opened_env(registry, root, cache, pool, ws) as env:  # 準備で落ちても閉じる（BUG-0076）
        await pool.ensure_started("typescript", ws)
        assert await pool.wait_ready("typescript", ws, READY_WAIT_MS), pool.status("typescript", ws)
        yield env


async def _exact_incoming(env: Env):
    """LSP の起動直後は ``ready`` でも 0 件のことがあり、ツールは structural に降格する。exact になるまで条件待ち。"""
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        res = await env.calls(TARGET, "incoming", depth=1)
        assert not res.is_error, res.text
        if res.structured["_resolution_meta"]["confidence_level"] == "exact":
            return res
        assert asyncio.get_running_loop().time() < deadline, res.text
        await asyncio.sleep(0.5)


@needs_tsls
async def test_t07_incoming_exact_includes_a_caller_in_a_file_outside_every_tsconfig(tsenv7: Env) -> None:
    res = await _exact_incoming(tsenv7)
    meta = res.structured["_resolution_meta"]
    assert "typescript-language-server" in meta["engine_used"] and meta["lsp_status"] == "ready", meta
    nodes = res.structured["data"]["nodes"]
    by_name = {n["name"]: n for n in nodes}
    assert "check" in by_name, sorted(by_name)  # test/ の関数（どの tsconfig にも入らない）
    assert by_name["check"]["call_site_path"] == CALLS and by_name["check"]["is_test"] is True
    assert {n["confidence_level"] for n in nodes} == {"exact"}
    assert not any("were not opened" in w for w in res.structured.get("warnings", []))


@needs_tsls
async def test_t07_incoming_second_call_answers_from_paad_with_the_same_test_caller(tsenv7: Env) -> None:
    first = await _exact_incoming(tsenv7)
    want = [(n["name"], n["call_site_path"], n["call_site_line"]) for n in first.structured["data"]["nodes"]]
    await tsenv7.stop_lsp()
    second = await tsenv7.calls(TARGET, "incoming", depth=1)
    assert not second.is_error, second.text
    meta = second.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["engine_used"] == "lsp-cache+tree-sitter", meta
    got = [(n["name"], n["call_site_path"], n["call_site_line"]) for n in second.structured["data"]["nodes"]]
    assert got == want and ("check", CALLS) in {(n, p) for n, p, _ in got}
