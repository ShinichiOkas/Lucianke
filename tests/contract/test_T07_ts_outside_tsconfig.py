"""T-07 の ``incoming`` の ``exact`` 経路も、tsconfig の ``include`` に入らないファイル（``test/`` など）の呼び出し元を返す（20e。BUG-0047 の T-07 版）。

tsserver は ``include`` に入らないファイルを、開かれるまでどのプロジェクトにも入れず、``callHierarchy`` の呼び元に出さない。
T-06 と同じ部品（``prime_candidates`` / ``prime_documents``）で、対象の名前を含む tsserver 系のファイルを先に LSP へ開かせてから聞く。

* 実機: ``tests/contract/test_T06_ts_outside_tsconfig.py`` と同じ条件（typescript-language-server と ``typescript`` パッケージ）で走る。無ければ skip。
* 偽サーバーの試験（開く順序・上限・永続化）は ``tests/contract/test_T06_T07_exact_marks.py``。
* 呼び出しを関数に包んだ ``test/calls.test.ts`` の ``check`` と、モジュール直下の呼び出し（``test/guard.test.ts`` など。
  構文木の木と同じくファイルの ``(module)`` のノード）の両方が incoming に出ることを確かめる。
* BUG-0133: tsserver の callHierarchy は対象の既定のプロジェクト（tsconfig の ``include``）しか探さず、名前の無いコールバック
  （``test('x', () => f())``）の中の呼び元をソースファイルの項目で返す。以前は前者で ``test/`` の呼び元が抜け、後者は
  「索引に無い呼び元」として捨てて 0 件になり、索引の呼び出しと食い違って ``lsp_status: indexing`` の構文木に降格していた。
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
HELPER = "test/helper.test.ts"
# 名前の無いコールバックの中だけで呼ばれる補助関数（ky の test/resilience.ts の observe と同じ形）と、関数でない値を持つ変数の中の
# オブジェクトのメソッドからの呼び出し（ky の bodyCases と同じ形。呼び出し階層の項目が無い変数が深さ 2 のノードになる）
HELPER_SRC = (
    "function test(name: string, run: () => void): void {\n"  # 1
    "\trun();\n"  # 2
    "}\n"  # 3
    "\n"  # 4
    "function observe(value: number): number {\n"  # 5
    "\treturn value + 1;\n"  # 6
    "}\n"  # 7
    "\n"  # 8
    "test('a', () => {\n"  # 9
    "\tobserve(1);\n"  # 10
    "});\n"  # 11
    "test('b', () => observe(2));\n"  # 12
    "\n"  # 13
    "export const cases = [\n"  # 14
    "\t{\n"  # 15
    "\t\trun() {\n"  # 16
    "\t\t\treturn observe(3);\n"  # 17
    "\t\t},\n"  # 18
    "\t},\n"  # 19
    "];\n"  # 20
)
OBSERVE = {"path": HELPER, "symbol_name": "observe"}


@pytest.fixture
async def tsenv7(registry: ProcessRegistry, tmp_path: Path):  # noqa: F811
    root = tmp_path / WS
    for rel, text in {**FILES, CALLS: CALLS_SRC, HELPER: HELPER_SRC}.items():
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


async def _exact_incoming(env: Env, target: dict[str, str] = TARGET, depth: int = 1):
    """LSP の起動直後は ``ready`` でも 0 件のことがあり、ツールは structural に降格する。exact になるまで条件待ち。"""
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        res = await env.calls(target, "incoming", depth=depth)
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
    # モジュール直下の呼び出しは、構文木の木と同じくファイルの (module) のノード（source/ の中・test/・別の tsconfig の test-d/）
    modules = {"source/use.ts (module)", "test/guard.test.ts (module)", "test/other.test.ts (module)", "test-d/types.ts (module)"}
    assert modules <= set(by_name), sorted(by_name)
    assert [s["line"] for s in by_name["test/guard.test.ts (module)"]["call_sites"]] == [3, 4]
    assert {n["confidence_level"] for n in nodes} == {"exact"}
    assert not any("were not opened" in w or "left out" in w for w in res.structured.get("warnings", []))


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


@needs_tsls
async def test_t07_incoming_of_a_helper_called_only_in_anonymous_callbacks_is_exact_and_ready(tsenv7: Env) -> None:
    """名前の無いコールバックの中だけで呼ばれる関数（BUG-0133）: tsserver は呼び元をソースファイルの項目で返す。以前は捨てて 0 件になり、
    索引の呼び出しと食い違って ``lsp_status: indexing`` の構文木に降格し、呼ぶたびに LSP に聞き直していた。"""
    res = await _exact_incoming(tsenv7, OBSERVE, depth=2)
    meta = res.structured["_resolution_meta"]
    assert meta["lsp_status"] == "ready" and "typescript-language-server" in meta["engine_used"], meta
    nodes = res.structured["data"]["nodes"]
    by_name = {n["name"]: n for n in nodes}
    assert set(by_name) == {f"{HELPER} (module)", "cases"}, sorted(by_name)
    assert [s["line"] for s in by_name[f"{HELPER} (module)"]["call_sites"]] == [10, 12]
    assert by_name["cases"]["call_site_line"] == 17 and by_name["cases"]["depth"] == 1
    assert {n["confidence_level"] for n in nodes} == {"exact"}
    # 関数でない値を持つ変数 cases には呼び出し階層の項目が無い: references の呼び出し箇所（0 件）で答え、構文木で補わない
    assert by_name["cases"].get("children_confidence_level") in (None, "exact"), by_name["cases"]
    assert not any("could not answer" in w for w in res.structured.get("warnings", [])), res.structured["warnings"]

    again = await tsenv7.calls(OBSERVE, "incoming", depth=2)  # 2 回目は PaaD の exact 行（LSP に聞き直さない）
    meta2 = again.structured["_resolution_meta"]
    assert meta2["confidence_level"] == "exact" and meta2["engine_used"] == "lsp-cache+tree-sitter", meta2
    assert meta2["lsp_status"] == "ready", meta2
