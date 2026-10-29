"""tools/list のスキーマ（ツール名・description・inputSchema の JSON）のトークン数をツールごとに測る（23a）。

使い方（tiktoken はベンチ専用なので ``--with`` で足す。製品の依存には入れない）:
    uv run --no-project --with tiktoken python bench/schema_tokens.py                  # 既定の tools/list（M1 の分）
    uv run --no-project --with tiktoken python bench/schema_tokens.py --list-planned   # M2/M3 の入口も載せたとき（LUCIANKE_LIST_PLANNED_TOOLS=1）
    uv run --no-project --with tiktoken python bench/schema_tokens.py --json out.json  # 結果を JSON でも保存

サーバーは ``.venv`` の python で ``python -P -m lucianke`` を実プロセスで起こし、``tools/list`` を生の JSON-RPC で引く
（SDK も lucianke も、この台本を動かす環境に要らない）。数え方は ``bench/run_tokens.py`` の
``tools_list_schema_tokens`` と同じ（``{"name","description","inputSchema"}`` を ``ensure_ascii=False`` の JSON にして o200k_base）。
``tests/unit/test_schema_budget.py`` は同じ上限を tiktoken なしで ``core/tokens.py`` の推定器で守る。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import tiktoken

REPO = Path(__file__).resolve().parents[1]
ENC = tiktoken.get_encoding("o200k_base")
PROTOCOL_VERSION = "2025-06-18"


def tok(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def venv_python() -> str:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        p = REPO / rel
        if p.exists():
            return str(p)
    return sys.executable


def fetch_tools(list_planned: bool) -> list[dict[str, Any]]:
    """実プロセスのサーバーに ``tools/list`` を引く。"""
    with tempfile.TemporaryDirectory() as cache:
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        env.pop("LUCIANKE_LIST_PLANNED_TOOLS", None)
        env.pop("LUCIANKE_DEV_TOOLS", None)
        env.update({"PYTHONUTF8": "1", "LUCIANKE_CACHE_DIR": cache})
        if list_planned:
            env["LUCIANKE_LIST_PLANNED_TOOLS"] = "1"
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "schema-tokens", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]  # fmt: skip
        stdin = "".join(json.dumps(m) + "\n" for m in msgs)
        cp = subprocess.run(  # noqa: S603
            [venv_python(), "-P", "-m", "lucianke"], input=stdin.encode(), capture_output=True, env=env,
            cwd=tempfile.gettempdir(), timeout=120, check=False,
        )  # fmt: skip
    for line in cp.stdout.decode("utf-8", "replace").splitlines():
        msg = json.loads(line)
        if msg.get("id") == 2:
            return msg["result"]["tools"]
    raise SystemExit(f"tools/list の応答が無い。stderr: {cp.stderr.decode('utf-8', 'replace')[-800:]}")


def measure(tools: list[dict[str, Any]]) -> dict[str, Any]:
    listing = [{"name": t["name"], "description": t.get("description"), "inputSchema": t["inputSchema"]} for t in tools]
    by_tool = {x["name"]: tok(json.dumps(x, ensure_ascii=False)) for x in listing}
    return {
        "tools": len(listing),
        "total_tokens": tok(json.dumps(listing, ensure_ascii=False)),
        "sum_by_tool": sum(by_tool.values()),
        "by_tool": by_tool,
        "description_tokens": {x["name"]: tok(x["description"] or "") for x in listing},
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list-planned", action="store_true", help="LUCIANKE_LIST_PLANNED_TOOLS=1 で M2/M3 の入口も載せる")
    ap.add_argument("--json", metavar="PATH", help="結果を JSON で保存する")
    args = ap.parse_args()
    res = measure(fetch_tools(args.list_planned))
    res["list_planned"] = args.list_planned
    width = max(len(n) for n in res["by_tool"])
    print(f"{'tool':<{width}}  {'total':>6}  {'desc':>5}")
    for name, n in sorted(res["by_tool"].items(), key=lambda kv: -kv[1]):
        print(f"{name:<{width}}  {n:>6}  {res['description_tokens'][name]:>5}")
    print(f"{'TOTAL (one JSON array)':<{width}}  {res['total_tokens']:>6}   tools={res["tools"]}, sum of per-tool {res['sum_by_tool']}")
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
