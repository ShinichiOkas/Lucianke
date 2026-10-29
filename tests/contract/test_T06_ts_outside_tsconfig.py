"""BUG-0047: T-06 の ``exact`` 経路は、tsconfig の ``include`` に入らないファイル（``test/`` など）の参照も返す。

tsserver（typescript-language-server）は ``tsconfig.json`` の ``include`` に入らないファイルを開かれるまでどのプロジェクトにも
入れず、``references`` の結果に出さない。ky（``include: ["source"]``）の ``isHTTPError`` が ``test/`` の参照を 0 件で返していた。
ツールは参照元になりうるファイル（対象の名前を含む tsserver 系のファイル）を先に LSP へ開かせてから聞く。

* 実機: typescript-language-server と ``typescript`` パッケージ（``node_modules/typescript/lib/tsserver.js``）が見つかったときだけ走る
  （``LUCIANKE_LSP_TYPESCRIPT_LANGUAGE_SERVER_CMD`` / ``PATH`` / ``bench/.cache/ky/node_modules``）。無ければ skip。
* 偽サーバーの試験（開く順序と上限）は ``tests/unit/test_lsp_prime.py``。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest
from test_T06_T07_lsp import Env, build_env, close_env

from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import default_server, find_command, servers_for
from lucianke.workspace.types import Workspace

REPO = Path(__file__).resolve().parents[2]
KY = REPO / "bench" / ".cache" / "ky"
WS = "tsmini"
READY_WAIT_MS = 120_000

_TSLS = next((d for d in servers_for("typescript", "types") if d.name == "typescript-language-server"), None)
_TSLS_CMD = find_command(_TSLS, workspace_root=KY, venv=Path(sys.prefix)) if _TSLS else None


def _typescript_package() -> Path | None:
    """``typescript`` パッケージの置き場（tsls の隣の ``node_modules``、無ければ F-TS の ``node_modules``）。"""
    candidates = []
    if _TSLS_CMD:
        candidates.append(Path(_TSLS_CMD[0]).resolve().parent.parent / "typescript")
    candidates.append(KY / "node_modules" / "typescript")
    return next((c for c in candidates if (c / "lib" / "tsserver.js").is_file()), None)


_TS_PKG = _typescript_package()
needs_tsls = pytest.mark.skipif(
    _TSLS_CMD is None or _TS_PKG is None or default_server("typescript", "types") is None,
    reason="typescript-language-server (+ the typescript package) is not available",
)

# tsconfig.json は source/ だけを include する。test/ はどの tsconfig にも入らず、test-d/ は別の tsconfig.test.json に入る（ky と同じ）。
FILES = {
    "package.json": '{ "name": "tsmini", "type": "module" }\n',
    "tsconfig.json": (
        '{ "compilerOptions": { "target": "ES2022", "module": "ES2022", "moduleResolution": "bundler",'
        ' "strict": true, "noEmit": true }, "include": ["source"] }\n'
    ),
    "tsconfig.test.json": '{ "extends": "./tsconfig.json", "include": ["test-d"] }\n',
    "source/guard.ts": (
        "export type Thing = { readonly kind: 'thing' };\n\n"
        "export function isThing(value: unknown): value is Thing {\n"
        "\treturn typeof value === 'object' && value !== null;\n}\n"
    ),
    "source/index.ts": "export {isThing} from './guard.js';\nexport type {Thing} from './guard.js';\n",
    "source/use.ts": "import {isThing} from './guard.js';\n\nexport const ok = isThing({});\n",
    "test/guard.test.ts": (
        "import {isThing} from '../source/index.js';\n\nexport const a = isThing(1);\nexport const b = isThing({});\n"
    ),
    "test/other.test.ts": "import {isThing} from '../source/guard.js';\n\nexport const c = isThing('x');\n",
    "test-d/types.ts": "import {isThing} from '../source/index.js';\n\nexport const d = isThing(2);\n",
}
TEST_FILES = {"test/guard.test.ts", "test/other.test.ts"}
TARGET = {"path": "source/guard.ts", "symbol_name": "isThing"}


def _link_typescript(root: Path) -> None:
    link = root / "node_modules" / "typescript"
    link.parent.mkdir(parents=True, exist_ok=True)
    assert _TS_PKG is not None
    try:
        os.symlink(_TS_PKG, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi  # type: ignore[import-not-found]  # 権限の要らないディレクトリ・ジャンクション

        _winapi.CreateJunction(str(_TS_PKG), str(link))


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


@pytest.fixture
async def tsenv(registry: ProcessRegistry, tmp_path: Path):
    root = tmp_path / "tsmini"
    for rel, text in FILES.items():
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
    env = await build_env(registry, root, cache, pool, ws)
    await pool.ensure_started("typescript", ws)
    assert await pool.wait_ready("typescript", ws, READY_WAIT_MS), pool.status("typescript", ws)
    yield env
    await close_env(env, registry)


async def _exact_refs(env: Env, **args):
    """LSP の起動直後は ``ready`` でも 0 件のことがあり、ツールは structural に降格する。exact になるまで条件待ち。"""
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        res = await env.refs(TARGET, query_type="references", **args)
        assert not res.is_error, res.text
        if res.structured["_resolution_meta"]["confidence_level"] == "exact":
            return res
        assert asyncio.get_running_loop().time() < deadline, res.text
        await asyncio.sleep(0.5)


@needs_tsls
async def test_t06_exact_includes_references_in_files_outside_every_tsconfig(tsenv: Env) -> None:
    res = await _exact_refs(tsenv, include_tests=True)
    meta = res.structured["_resolution_meta"]
    assert "typescript-language-server" in meta["engine_used"] and meta["lsp_status"] == "ready", meta
    data = res.structured["data"]
    by_file = {}
    for r in data["references"]:
        by_file.setdefault(r["path"], []).append(r)
    # source/（tsconfig の中）、test/（どの tsconfig にも入らない）、test-d/（別の tsconfig.test.json）の全部から参照が返る
    assert set(by_file) >= {"source/index.ts", "source/use.ts", "test-d/types.ts", *TEST_FILES}, sorted(
        by_file
    )
    assert len(by_file["test/guard.test.ts"]) == 3  # import + 呼び出し 2
    assert all(r["is_test"] for p in TEST_FILES for r in by_file[p])
    assert data["totals"]["tests"] == sum(len(by_file[p]) for p in TEST_FILES)
    assert {r["confidence_level"] for r in data["references"]} == {"exact"}
    assert not any("were not opened" in w for w in res.structured.get("warnings", []))


@needs_tsls
async def test_t06_include_tests_false_drops_only_the_test_files(tsenv: Env) -> None:
    res = await _exact_refs(tsenv, include_tests=False)
    paths = {r["path"] for r in res.structured["data"]["references"]}
    assert paths and not (paths & TEST_FILES), sorted(paths)
    assert "source/use.ts" in paths and "test-d/types.ts" in paths
    assert res.structured["data"]["totals"]["tests"] == 0


@needs_tsls
async def test_t06_second_call_answers_from_paad_with_the_same_test_references(tsenv: Env) -> None:
    first = await _exact_refs(tsenv, include_tests=True)
    want = [(r["path"], r["line"], r["col"]) for r in first.structured["data"]["references"]]
    await tsenv.stop_lsp()
    second = await tsenv.refs(TARGET, query_type="references", include_tests=True)
    assert not second.is_error, second.text
    meta = second.structured["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["engine_used"] == "lsp-cache+tree-sitter", meta
    assert [(r["path"], r["line"], r["col"]) for r in second.structured["data"]["references"]] == want
    assert {p for p, _, _ in want} >= TEST_FILES


@needs_tsls
@pytest.mark.skipif(
    not (KY / "source" / "utils" / "type-guards.ts").is_file(), reason="F-TS (ky) is not fetched"
)
async def test_t06_ky_is_http_error_returns_the_test_references(
    registry: ProcessRegistry, tmp_path: Path
) -> None:
    """BUG-0047 の再現そのもの（ky @ 3541888 の ``isHTTPError``）。原本には触れず、``node_modules`` 以外を複製する。"""
    root = tmp_path / "ky"
    shutil.copytree(KY, root, ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"))
    _link_typescript(root)
    ws = Workspace(workspace_id="ky", root=root.resolve())
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
    env = await build_env(registry, root, cache, pool, ws)
    try:
        await pool.ensure_started("typescript", ws)
        assert await pool.wait_ready("typescript", ws, READY_WAIT_MS), pool.status("typescript", ws)
        target = {"path": "source/utils/type-guards.ts", "symbol_name": "isHTTPError"}
        deadline = asyncio.get_running_loop().time() + 90
        while True:
            res = await env.refs(target, query_type="references", include_tests=True, max_tokens=20000)
            assert not res.is_error, res.text
            if res.structured["_resolution_meta"]["confidence_level"] == "exact":
                break
            assert asyncio.get_running_loop().time() < deadline, res.text
            await asyncio.sleep(0.5)
        data = res.structured["data"]
        paths = {r["path"] for r in data["references"]}
        assert {"source/core/Ky.ts", "source/index.ts"} <= paths
        assert {"test/errors.ts", "test/hooks.ts", "test/http-error.ts", "test/type-guards.ts"} <= paths, (
            sorted(paths)
        )
        assert data["totals"]["tests"] >= 4
    finally:
        await close_env(env, registry)
