"""S-1 の 5（HTTP 系依存の import とコールドスタート）。
使い方: uv run --frozen python spikes/s01_mcp/probe_import.py [回数(既定7)]
  A: python -X importtime の木からパッケージ別の累積時間 / B: サーバー内で実際にロードされた HTTP 系モジュール
  C: 起動 → initialize 応答までの時間（素の python / import のみ / 本サーバー / HTTP 系スタブ版 / SDK Client）
"""

from __future__ import annotations

import asyncio
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters
from rawclient import PY, REPO, SERVER, RawServer

N = int(sys.argv[1]) if len(sys.argv) > 1 else 7
PKGS = ["pydantic", "mcp_types", "starlette", "uvicorn", "sse_starlette", "httpx2", "httpcore2", "jwt", "cryptography",
        "python_multipart", "opentelemetry", "win32api", "anyio", "jsonschema", "mcp.client", "mcp.server.auth",
        "mcp.server.streamable_http", "mcp.server.mcpserver"]  # fmt: skip
LINE = re.compile(r"import time:\s+(\d+) \|\s+(\d+) \| (\s*)(\S+)")


def importtime_tree() -> dict[str, float]:
    code = "import mcp.server.lowlevel, mcp.server.stdio"
    best: dict[str, float] = {}
    total_best = 1e18
    for _ in range(3):
        p = subprocess.run([PY, "-X", "importtime", "-c", code], capture_output=True, text=True, encoding="utf-8")
        rows = []
        for ln in p.stderr.splitlines():
            m = LINE.match(ln)
            if m:
                rows.append((int(m.group(2)), (len(m.group(3)) - 1) // 2, m.group(4)))  # (累積us, 深さ, 名前)
        cost: dict[str, float] = {}
        for i, (cum, depth, name) in enumerate(rows):
            parent = next((rows[j][2] for j in range(i + 1, len(rows)) if rows[j][1] == depth - 1), "")
            for pkg in PKGS:
                inside = name == pkg or name.startswith(pkg + ".")
                parent_inside = parent == pkg or parent.startswith(pkg + ".")
                if inside and not parent_inside:
                    cost[pkg] = cost.get(pkg, 0) + cum / 1000
        total = rows[-1][0] / 1000
        if total < total_best:
            total_best, best = total, {**cost, "(total)": total}
    return best


def stat(xs: list[float]) -> str:
    return f"median {statistics.median(xs):6.0f} ms / min {min(xs):6.0f} / max {max(xs):6.0f}  (n={len(xs)})"


def time_cmd(args: list[str]) -> list[float]:
    out = []
    for _ in range(N):
        t = time.perf_counter()
        subprocess.run(args, capture_output=True, check=True)
        out.append((time.perf_counter() - t) * 1000)
    return out


def time_init(extra: list[str]) -> list[float]:
    out = []
    for _ in range(N):
        t = time.perf_counter()
        s = RawServer(["--divert=manual", *extra])
        s.initialize()
        out.append((time.perf_counter() - t) * 1000)
        s.close()
    return out


async def time_sdk_client() -> list[float]:
    out = []
    for _ in range(N):
        t = time.perf_counter()
        async with Client(StdioServerParameters(command=PY, args=[SERVER, "--divert=manual"], env={"PYTHONUTF8": "1"}),
                          mode="legacy", cache=None):  # fmt: skip
            out.append((time.perf_counter() - t) * 1000)
    return out


def main() -> None:
    print("== A: import mcp.server.lowlevel + mcp.server.stdio の累積時間（-X importtime の木、親が別パッケージのものだけ合算、3 回の最良）")
    for k, v in importtime_tree().items():
        print(f"  {k:28s} {v:7.0f} ms")
    print("== B: サーバー内でロードされた HTTP 系モジュール（通常 / --stub-http）")
    for extra in ([], ["--stub-http"]):
        s = RawServer(["--divert=manual", *extra])
        s.initialize()
        st = s.call("stats")["result"]["structuredContent"]
        s.close()
        print(f"  {' '.join(extra) or '通常':12s} sys.modules={st['n_sys_modules']} boot={st['boot_ms']}")
        print("    ロード済み:", [k for k, v in st["loaded"].items() if v])
        print("    未ロード  :", [k for k, v in st["loaded"].items() if not v])
    print(f"== C: 起動 → initialize 応答（{N} 回。OS のファイルキャッシュは温まった状態）")
    print("  素の python -c pass                      ", stat(time_cmd([PY, "-c", "pass"])))
    print("  uv run --frozen python -c pass           ", stat(time_cmd(["uv", "run", "--frozen", "--project", str(REPO), "python", "-c", "pass"])))
    print("  python -c 'import mcp.server.lowlevel'   ", stat(time_cmd([PY, "-c", "import mcp.server.lowlevel, mcp.server.stdio"])))
    print("  本サーバー(raw)                          ", stat(time_init([])))
    print("  本サーバー + --stub-http(raw)            ", stat(time_init(["--stub-http"])))
    print("  本サーバー(SDK Client, legacy)           ", stat(asyncio.run(time_sdk_client())))


if __name__ == "__main__":
    main()
