"""BUG-0046: 起動直後の最初の呼び出しが ``edit_symbol_body``（T-10）でも、「未対応」（``E_UNSUPPORTED``）で落ちない。

編集の構文妥当性ベンチの stdio 標本で、F-TS（ky）の編集 6/20 が ``E_UNSUPPORTED`` になった（プロセス内では成功）。再現しなかった
ので原因は断定していないが、T-10 の経路で「待てば直る」状態を ``E_UNSUPPORTED``（その操作を提供しない）として返す箇所は
``index_not_open``（束が開く前）だけだった。実プロセスを起こして最初の 1 呼び出しから TS の編集を投げ、

* 成功する（入口の ``ready_gate`` が束の準備を待つ）か、
* 失敗するなら再試行を促す ``E_TIMEOUT``（``details.reason == "index_opening"``）で、再試行すると成功する

ことを確かめる。待ちは条件待ち（タイムアウトつき）。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
from _server_cmd import server_argv  # noqa: E402

POLY = REPO / "tests" / "fixtures" / "poly"
RETRY_LIMIT_S = 60.0


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(POLY, root)
    return root


async def _edit(c: Client, **args: Any) -> tuple[bool, dict[str, Any]]:
    r = await c.call_tool("edit_symbol_body", {"format": "json", "diagnostics_wait_ms": 0, "dry_run": True, **args})
    return r.is_error, dict(r.structured_content or {})


async def test_first_ts_edit_after_start_is_never_unsupported(ws: Path, tmp_path: Path) -> None:
    argv = server_argv()
    params = StdioServerParameters(
        command=argv[0], args=argv[1:], env={"PYTHONUTF8": "1", "LUCIANKE_CACHE_DIR": str(tmp_path / "cache")}, cwd=str(ws)
    )
    target = {"path": "web/src/payment.ts", "symbol_name": "PaymentRequest"}
    async with Client(params, mode="legacy", cache=None) as c:
        deadline = time.monotonic() + RETRY_LIMIT_S
        retried = 0
        while True:
            is_error, env = await _edit(c, target=target, operation="delete_symbol")
            if not is_error:
                break
            err = env.get("error") or {}
            # 「未対応」ではない。待てば直る状態は再試行を促す E_TIMEOUT（index_opening）
            assert err.get("code") == "E_TIMEOUT" and (err.get("details") or {}).get("reason") == "index_opening", env
            assert time.monotonic() < deadline, f"still not ready after {RETRY_LIMIT_S:.0f} s: {env}"
            retried += 1
            await asyncio.sleep(0.5)
        data = env["data"]
        assert data["applied"] is False  # dry_run
        assert data["files_changed"][0]["path"] == "web/src/payment.ts"
        # 続けて同じ種類の呼び出しは全部成功する（束は開いている）
        for op, tgt in (("delete_symbol", {"path": "web/src/payment.ts", "symbol_name": "PaymentClient"}),):
            is_error, env2 = await _edit(c, target=tgt, operation=op)
            assert not is_error, (retried, env2)
