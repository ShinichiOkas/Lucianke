"""索引の途中でも呼び出しが待たされない（BUG-0112）: 実プロセスの ``lucianke`` を stdio でつなぎ、初回の索引の間ずっと
``configure_workspace status`` と読むツールを呼び、どの呼び出しも数秒で答えることを確かめる。

ワークスペースは tmp に作る: Python のモジュール 250 本（クラスの継承と呼び出し・import）と、関数 24,000 本・約 1 MB の JS
（非 ASCII の文字列・入れ子の即時関数・約 100 KB の 1 行。大きなワークスペースで索引を数十分止めていた形。1 行に 1 関数なので
圧縮済み〈BUG-0119〉とはみなされず解析される）。Git の作業ツリーにして（dulwich）、
``status`` が実際に Git の状態を調べるようにする。

* 走査の間の ``status`` は、どれも ``STATUS_LIMIT_S`` 以内に答える（修正前は、同じ形のワークスペースで 2 分を超えた）。
* ``jobs[].progress`` は減らず、最後は ``done`` / 100。走査の途中の読むツールは進みの警告（``index building:``）を添える。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))  # 共通の起動コマンド _server_cmd
from _server_cmd import server_argv  # noqa: E402

STATUS_LIMIT_S = 5.0
SCAN_LIMIT_S = 300.0  # 走査が終わるまでの上限（修正後は 30 秒前後）
MODULES = 250
BUNDLE_FUNCTIONS = 24_000  # 約 1 MB の JS


def _module(i: int) -> str:
    prev = f"m{i - 1:03d}"
    base = f"from pkg.{prev} import C{i - 1:03d} as Base, helper_{i - 1:03d}\n\n\n" if i else "class Base:\n    pass\n\n\n"
    body = "".join(
        f"    def method_{k}(self, x: int) -> int:\n        return helper_{i:03d}(x) + self.method_{(k + 1) % 8}(x)\n\n"
        for k in range(8)
    )
    return (
        f'"""module {i}"""\n{base}def helper_{i:03d}(x: int) -> int:\n    return x + {i}\n\n\n'
        f"class C{i:03d}(Base):\n{body}\n"
        f"def run_{i:03d}() -> int:\n    c = C{i:03d}()\n    return c.method_0({i}) + {'helper_' + format(i - 1, '03d') + '(1)' if i else '0'}\n"
    )


def _bundle() -> str:
    out = ['!function(e){"use strict";var t="héllo wörld ✓ \U0001f600";']
    for i in range(BUNDLE_FUNCTIONS):
        if i % 200 == 0:
            out.append("(function(){")
        out.append(f"\nfunction a{i}(n){{return n+t.length+a{(i * 7) % BUNDLE_FUNCTIONS}.length}}")
        if i % 200 == 199:
            out.append("})();")
    out.append("\nvar long=[" + ",".join(f'"é{i}"' for i in range(12_000)) + "];")  # 約 100 KB の 1 行（非 ASCII）
    out.append("\n}(this);\n")
    return "".join(out)


def _make_workspace(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    for i in range(MODULES):
        (pkg / f"m{i:03d}.py").write_text(_module(i), encoding="utf-8", newline="\n")
    (root / "vendor").mkdir()
    (root / "vendor" / "bundle.js").write_text(_bundle(), encoding="utf-8", newline="\n")
    (root / "pyproject.toml").write_text('[project]\nname = "responsive"\nversion = "0"\n', encoding="utf-8", newline="\n")
    from dulwich import porcelain

    repo = porcelain.init(str(root))
    porcelain.add(repo, paths=None)
    porcelain.commit(repo, message=b"init", author=b"e2e <e2e@example.invalid>", committer=b"e2e <e2e@example.invalid>")


def _params(ws: Path, cache: Path) -> StdioServerParameters:
    argv = server_argv()
    env = {**os.environ, "PYTHONUTF8": "1", "LUCIANKE_CACHE_DIR": str(cache)}
    return StdioServerParameters(command=argv[0], args=argv[1:], env=env, cwd=str(ws))


async def _timed(c: Client, tool: str, args: dict[str, Any]) -> tuple[float, Any]:
    t0 = time.perf_counter()
    r = await c.call_tool(tool, args)
    return time.perf_counter() - t0, r


async def test_every_call_answers_quickly_while_a_large_scan_runs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_workspace(ws)
    status_s: list[float] = []
    progresses: list[int] = []
    building_warned = False
    read_s: list[float] = []

    async with Client(_params(ws, tmp_path / "cache"), mode="legacy", cache=None) as c:
        t0 = time.monotonic()
        done = False
        while time.monotonic() - t0 < SCAN_LIMIT_S:
            dt, r = await _timed(c, "configure_workspace", {"action": "status", "format": "json"})
            assert not r.is_error, r
            status_s.append(dt)
            jobs = [j for j in r.structured_content["data"]["jobs"] if j["kind"] == "reindex"]
            if jobs:
                job = jobs[0]
                progresses.append(int(job["progress"]))
                if job["state"] in ("done", "failed"):
                    assert job["state"] == "done", job
                    done = True
                    break
            if len(status_s) % 4 == 0:  # 読むツールも、索引の途中で待たされない
                dt, r = await _timed(c, "search_workspace_symbols", {"query": "helper", "limit": 3, "format": "json"})
                read_s.append(dt)
                building_warned |= any("index building:" in w for w in (r.structured_content or {}).get("warnings", []))
            await asyncio.sleep(0.25)

    assert done, (
        f"the scan did not finish within {SCAN_LIMIT_S:.0f} s (last progress {progresses[-1:]};"
        f" slowest status {max(status_s, default=0):.2f} s over {len(status_s)} calls)"
    )
    assert len(status_s) >= 3, status_s  # 走査の途中を何度も見た（終わってからの 1 回だけではない）
    assert max(status_s) <= STATUS_LIMIT_S, f"slowest status {max(status_s):.2f} s: {[round(s, 2) for s in status_s]}"
    assert not read_s or max(read_s) <= STATUS_LIMIT_S, read_s
    assert progresses == sorted(progresses) and progresses[-1] == 100, progresses
    if read_s:
        assert building_warned
