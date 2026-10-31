"""BUG-0135（TypeScript 側）: TS の LSP（typescript-language-server）が動いている間に ``tsconfig.json`` を書き換える・作る・消しても、
T-06・T-07 の ``exact`` の答えは変わらず正しい（tsconfig の外のファイルを先に開かせる BUG-0047 の準備が、設定の変更の後も効く）。

``tsconfig.json`` の変更は、索引に載らなくても ``IndexerSink`` → ``LspPool.notify_watched``（``workspace/didChangeWatchedFiles``）で
LSP に届く（``servers.is_lsp_config``）。tsserver は届けなくても自分で設定を読み直す（実機 typescript-language-server 4.3.3 / TypeScript 5.6.3 では、
届けても届けなくても 1 秒以内に ``include`` の変更に答えが従う）ので、ここでは **届いたこと**（``notify_watched`` が受けた変更）と、
**サーバーが変更に従った後の答えが正しいこと** を確かめる。Lucianke の側に ``tsconfig`` から覚えるもの（解析の対象など）は無い。

* 実機: ``tests/contract/test_T06_ts_outside_tsconfig.py`` と同じ条件（typescript-language-server と ``typescript`` パッケージ。
  ``LUCIANKE_LSP_TYPESCRIPT_LANGUAGE_SERVER_CMD`` に ``bun --bun <...>/node_modules/typescript-language-server/lib/cli.mjs --stdio`` を
  置けば、node の無い PC でも走る）。無ければ skip。
* 変更は監視が拾ったときと同じく ``IndexerSink.changed`` / ``removed`` に渡す（監視の時間に依らない）。
"""

# ruff: noqa: F811  (フィクスチャ registry・tsenv を引数で受ける)

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import pytest
from test_T06_T07_lsp import Env
from test_T06_ts_outside_tsconfig import (
    TARGET,
    needs_tsls,
    registry,  # noqa: F401  (フィクスチャ)
    tsenv,  # noqa: F401  (フィクスチャ)
)

from lucianke.core.model import Position, RelPath
from lucianke.lsp import features
from lucianke.tools._runtime import IndexerSink

COMPILER = {"target": "ES2022", "module": "ES2022", "moduleResolution": "bundler", "strict": True, "noEmit": True}
TEST_FILES = {"test/guard.test.ts", "test/other.test.ts"}
CONVERGE_S = 30.0
"""サーバーが設定の変更に従うまでの上限（条件待ち。従えば即戻る）。"""


def _config(include: list[str]) -> str:
    return json.dumps({"compilerOptions": COMPILER, "include": include}) + "\n"


async def _server_refs(env: Env) -> set[str]:
    """``isThing`` の参照（LSP に直接。ほかのファイルは開かせない）のファイル。tsserver が今のプロジェクトで見つけたもの。"""
    handle = env.pool.get("typescript", env.ws)
    assert handle is not None
    text = (env.root / "source/guard.ts").read_text(encoding="utf-8").splitlines()
    line = next(i for i, t in enumerate(text) if "function isThing" in t)
    refs = await features.references(
        handle, RelPath("source/guard.ts"), Position(line, text[line].index("isThing")), include_declaration=False
    )
    return {str(r.rel) for r in refs}


async def _server_follows(env: Env, ok: Callable[[set[str]], bool], what: str) -> None:
    """サーバーの答え（``_server_refs``）が ``ok`` になるまで待つ。変更が tsserver に効いた証拠（効かなければこの試験は落ちる）。"""
    deadline = time.monotonic() + CONVERGE_S
    got: set[str] = set()
    while time.monotonic() < deadline:
        got = await _server_refs(env)
        if ok(got):
            return
        await asyncio.sleep(0.3)
    raise AssertionError(f"typescript-language-server did not follow the tsconfig change ({what}): {sorted(got)}")


class _Spy:
    """``LspPool.notify_watched`` が受けた変更（``(rel, kind)``）を残す。"""

    def __init__(self, env: Env) -> None:
        self.seen: list[tuple[str, int]] = []
        original = env.pool.notify_watched

        async def spy(ws: Any, changes: Any) -> float:
            changes = list(changes)
            self.seen += [(str(rel), int(kind)) for rel, kind in changes]
            return await original(ws, changes)

        env.pool.notify_watched = spy  # type: ignore[method-assign]


async def _apply(env: Env, rel: str, text: str | None) -> None:
    """``rel`` を書く（``text`` が ``None`` なら消す）。監視が拾ったときと同じく ``IndexerSink`` に渡す。"""
    path = env.root / rel
    sink = IndexerSink(env.ws, env.indexer, env.readers, env.pool)
    if text is None:
        path.unlink()
        await sink.removed(RelPath(rel))
    else:
        path.write_text(text, encoding="utf-8", newline="\n")
        await sink.changed(RelPath(rel))


async def _exact(env: Env, ask: Callable[[], Any]) -> dict[str, Any]:
    """LSP の起動直後は ``ready`` でも 0 件のことがあり、ツールは structural に降格する。exact になるまで条件待ち。"""
    deadline = time.monotonic() + 60
    while True:
        res = await ask()
        assert not res.is_error, res.text
        if res.structured["_resolution_meta"]["confidence_level"] == "exact":
            return res.structured
        assert time.monotonic() < deadline, res.text
        await asyncio.sleep(0.5)


async def _t06(env: Env) -> dict[str, Any]:
    return await _exact(env, lambda: env.refs(TARGET, query_type="references", include_tests=True))


async def _t07(env: Env) -> dict[str, Any]:
    return await _exact(env, lambda: env.calls(TARGET, "incoming", depth=1))


def _t06_sites(out: dict[str, Any]) -> list[tuple[str, int, int]]:
    return [(r["path"], r["line"], r["col"]) for r in out["data"]["references"]]


def _t07_callers(out: dict[str, Any]) -> list[tuple[str, str, int]]:
    return sorted((n["name"], n["call_site_path"], n["call_site_line"]) for n in out["data"]["nodes"])


def _no_gap_warning(out: dict[str, Any]) -> bool:
    return not any("were not opened" in w or "left out" in w for w in out.get("warnings", []))


def _expected_sites(env: Env) -> dict[str, int]:
    """``isThing`` の参照（宣言の ``source/guard.ts`` 以外で名前の現れる回数）。構文木に依らず、ファイルの本文から数えた真値。"""
    out: dict[str, int] = {}
    for path in sorted(env.root.rglob("*.ts")):
        rel = path.relative_to(env.root).as_posix()
        if rel.startswith("node_modules/") or rel == "source/guard.ts":
            continue
        n = path.read_text(encoding="utf-8").count("isThing")
        if n:
            out[rel] = n
    return out


MODULE_CALLERS = {"source/use.ts (module)", "test/guard.test.ts (module)", "test/other.test.ts (module)", "test-d/types.ts (module)"}


async def _check_answers(env: Env, scenario: str) -> None:
    """T-06 は本文から数えた参照の全部（tsconfig の外の test/・別の tsconfig の test-d/ も）を ``exact`` で、T-07 incoming は全部の
    モジュール直下の呼び元を ``exact`` で返す。"""
    out06, out07 = await _t06(env), await _t07(env)
    got: dict[str, int] = {}
    for p, _, _ in _t06_sites(out06):
        got[p] = got.get(p, 0) + 1
    assert got == _expected_sites(env), (scenario, got)
    assert {r["confidence_level"] for r in out06["data"]["references"]} == {"exact"}
    callers = {n for n, _, _ in _t07_callers(out07)}
    assert callers == MODULE_CALLERS, (scenario, sorted(callers))
    assert {n["confidence_level"] for n in out07["data"]["nodes"]} == {"exact"}
    assert _no_gap_warning(out06) and _no_gap_warning(out07), (out06.get("warnings"), out07.get("warnings"))


async def _warm(env: Env) -> None:
    """サーバーを温め、最初の答え（tsconfig の include が source だけ）を確かめる。T-06・T-07 の前（開かせる準備の前）に見る。"""
    await _server_follows(env, lambda r: "source/use.ts" in r and not (r & TEST_FILES), "initial include: source")


@needs_tsls
@pytest.mark.parametrize("scenario", ["widen", "narrow", "delete", "delete_then_create"])
async def test_t06_t07_stay_exact_and_complete_after_a_tsconfig_change(tsenv: Env, scenario: str) -> None:
    env = tsenv
    spy = _Spy(env)
    await _warm(env)
    if scenario == "widen":  # test/ が tsconfig のプロジェクトに入る（開かれなくても参照に出る）
        await _apply(env, "tsconfig.json", _config(["source", "test"]))
        await _server_follows(env, lambda r: TEST_FILES <= r, "include widened to test/")
    elif scenario == "narrow":  # source/use.ts が tsconfig のプロジェクトから外れる（tsserver の参照から消える）
        await _apply(env, "tsconfig.json", _config(["source/guard.ts"]))
        await _server_follows(env, lambda r: "source/use.ts" not in r, "include narrowed to source/guard.ts")
    elif scenario == "delete":  # tsconfig が無くなる（開いたファイルだけの推定プロジェクト）
        await _apply(env, "tsconfig.json", None)
        await _server_follows(env, lambda r: "source/use.ts" not in r, "tsconfig.json deleted")
    else:
        await _apply(env, "tsconfig.json", None)
        await _server_follows(env, lambda r: "source/use.ts" not in r, "tsconfig.json deleted")
        await _apply(env, "tsconfig.json", _config(["source", "test"]))
        await _server_follows(env, lambda r: TEST_FILES <= r, "tsconfig.json created again with test/")
    assert [c for c in spy.seen if c[0] == "tsconfig.json"], spy.seen  # LSP に届けた
    await _check_answers(env, scenario)


@needs_tsls
async def test_t06_t07_after_a_tsconfig_is_created_where_there_was_none(tsenv: Env) -> None:
    """tsconfig.json の無いワークスペース（開いたファイルだけの推定プロジェクト）に後から作っても、T-06・T-07 は全部返す。"""
    env = tsenv
    await _warm(env)
    await _apply(env, "tsconfig.json", None)
    await _server_follows(env, lambda r: "source/use.ts" not in r, "tsconfig.json deleted")
    await _apply(env, "tsconfig.json", _config(["source"]))
    await _server_follows(env, lambda r: "source/use.ts" in r and not (r & TEST_FILES), "tsconfig.json created")
    await _check_answers(env, "create")
