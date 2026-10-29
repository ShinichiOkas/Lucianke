"""S-5 追加実測その 2（使い捨て）: 診断の時系列（壊す→直す→全部直す）、organizeImports の別経路、
vtsls の tsdk 指定、シャットダウンなしで親を殺したときの孫プロセスの残り方。

使い方: uv run python spikes/s05_ts/run_s05_extra2.py     結果: spikes/s05_ts/out/extra2.json
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "s04_lsp"))

from run_s05_extra import FIX, NM, TS6, TSLS, VTSLS, diag_wait, msgs, node_procs, uri_of  # noqa: E402
from lsp_probe import LspClient, norm_uri, path_to_uri  # noqa: E402


async def seq(c: LspClient, path: Path, start: int, window: float = 2.5) -> list[list[int]]:
    t0 = c.now()
    await asyncio.sleep(window)
    return [[round((n.t - t0) * 1000), len(n.params["diagnostics"])] for n in c.notifications[start:]
            if n.method == "textDocument/publishDiagnostics" and norm_uri(n.params["uri"]) == uri_of(path)]


async def timeline(label: str, cmd: list[str], init_options: dict[str, Any] | None) -> dict[str, Any]:
    R: dict[str, Any] = {"label": label}
    c = LspClient(name=label, cmd=cmd, cwd=FIX, config_provider=lambda it: {})
    await c.start()
    await c.initialize(init_options=init_options)
    shapes, app = FIX / "shapes.ts", FIX / "app.ts"
    c.did_open(shapes, "typescript")
    c.did_open(app, "typescript")
    await asyncio.sleep(3.0)
    st, at = shapes.read_text(encoding="utf-8"), app.read_text(encoding="utf-8")
    i = len(c.notifications)
    c.did_change(shapes, 2, st + "\nexport const bad: number = 'x';\n")
    R["shapes_break_v2 [dt_ms, n]"] = await seq(c, shapes, i)
    i = len(c.notifications)
    c.did_change(shapes, 3, st)
    R["shapes_fix_v3"] = await seq(c, shapes, i)
    i = len(c.notifications)
    fixed = at.replace('const count: number = "three";', "const count: number = 3;").replace(
        "import { Circle, Shape, describe }", "import { Circle, describe }")
    c.did_change(app, 2, fixed)
    R["app_allfixed_v2"] = await seq(c, app, i)
    i = len(c.notifications)
    c.did_change(app, 3, at)
    R["app_restored_v3"] = await seq(c, app, i)
    # organizeImports: only なしで全アクション、executeCommand 経路
    r = await c.request("textDocument/codeAction", {
        "textDocument": {"uri": path_to_uri(app)},
        "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 10}},
        "context": {"diagnostics": []}})
    R["codeAction_no_only"] = [(a.get("kind"), a.get("title")) for a in (r or [])]
    if "tsls" in label:
        j = len(c.server_requests)
        try:
            er = await c.request("workspace/executeCommand",
                                 {"command": "_typescript.organizeImports", "arguments": [str(app)]}, timeout=10)
            R["executeCommand_organizeImports"] = json.dumps(er)[:200]
        except Exception as e:  # noqa: BLE001
            R["executeCommand_organizeImports"] = repr(e)[:200]
        R["server_applyEdit"] = [json.dumps(s.params)[:300] for s in c.server_requests[j:]
                                 if s.method == "workspace/applyEdit"]
    R["shutdown"] = await c.shutdown()
    await asyncio.sleep(0.8)
    R["node_procs_left"] = node_procs()
    return R


async def tsdk_variants() -> list[dict[str, Any]]:
    out = []
    for label, cfg in (
        ("tsdk(abs lib)+autoUseWorkspaceTsdk", {"typescript": {"tsdk": str(TS6 / "lib")}, "vtsls": {"autoUseWorkspaceTsdk": True}}),
        ("tsdk(abs lib) only", {"typescript": {"tsdk": str(TS6 / "lib")}}),
    ):
        c = LspClient(name=label, cmd=VTSLS, cwd=FIX, config_provider=lambda it, cfg=cfg: cfg)
        await c.start()
        await c.initialize()
        i = len(c.notifications)
        c.did_open(FIX / "app.ts", "typescript")
        d = await diag_wait(c, FIX / "app.ts", i, 10.0)
        out.append({"label": label, "log": msgs(c)[:2], "first_diag": d})
        await c.shutdown()
    return out


async def orphan_test(label: str, cmd: list[str]) -> dict[str, Any]:
    c = LspClient(name=label, cmd=cmd, cwd=FIX, config_provider=lambda it: {})
    await c.start()
    await c.initialize()
    i = len(c.notifications)
    c.did_open(FIX / "app.ts", "typescript")
    await diag_wait(c, FIX / "app.ts", i, 10.0)
    before = node_procs()
    c.proc.terminate()  # shutdown なしで親だけ殺す
    await c.proc.wait()
    await asyncio.sleep(1.5)
    left = node_procs()
    for ln in left:  # 後始末（s05_ts を含む node だけ）
        subprocess.run(["taskkill", "/PID", ln.split()[0], "/T", "/F"], capture_output=True)
    await asyncio.sleep(0.5)
    return {"label": label, "node_procs_while_running": len(before),
            "left_after_terminate_parent": [ln[:160] for ln in left], "after_cleanup": len(node_procs())}


async def main() -> None:
    out: dict[str, Any] = {}
    async def stage(name: str, coro: Any) -> Any:
        print("stage", name, flush=True)
        try:
            r = await asyncio.wait_for(coro, timeout=60)
        except Exception as e:  # noqa: BLE001
            r = {"error": repr(e)[:300]}
        print(json.dumps(r, ensure_ascii=False)[:2500], flush=True)
        return r

    out["tsdk"] = await stage("tsdk", tsdk_variants())
    out["timeline"] = [
        await stage("timeline vtsls", timeline("vtsls", VTSLS, None)),
        await stage("timeline tsls", timeline("tsls(tsserver.path=TS6)", TSLS,
                                              {"tsserver": {"path": str(TS6 / "lib" / "tsserver.js")}})),
    ]
    out["orphans"] = [await stage("orphan node", orphan_test("node vtsls.js", VTSLS)),
                      await stage("orphan cmd", orphan_test("vtsls.cmd(shim)", [str(NM / ".bin" / "vtsls.cmd"), "--stdio"]))]
    (HERE / "out" / "extra2.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
