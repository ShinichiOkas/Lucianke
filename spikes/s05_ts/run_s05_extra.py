"""S-5 追加実測（使い捨て）: run_s05.py が測っていない箇所だけを測る。

測るもの: (1) definition の生の形（Location / LocationLink）、(2) tsserver の所在の渡し方
（既定・ワークスペースの node_modules/typescript・tsdk 設定・initializationOptions）、(3) クリーンなファイルで
診断が「届かない」か「空で届く」か（直した後に空配列が来るか）、(4) organizeImports の kind 指定、
(5) 終了後に node 孫プロセスが残らないか、(6) Windows の .bin シム探索（shutil.which）。

使い方: uv run python spikes/s05_ts/run_s05_extra.py     結果: spikes/s05_ts/out/extra.json
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "s04_lsp"))

from lsp_probe import LspClient, norm_uri, path_to_uri  # noqa: E402
from probe_features import Target, tdp  # noqa: E402

FIX = HERE / "fixture_ts"
CACHE = HERE / ".cache"
NM = CACHE / "node_modules"
TS6 = CACHE / "ts6" / "node_modules" / "typescript"
WS = CACHE / "ws_ts6"  # node_modules/typescript が（junction で）TS 6 を指すワークスペース
NODE = shutil.which("node") or "node"
VTSLS = [NODE, str(NM / "@vtsls" / "language-server" / "bin" / "vtsls.js"), "--stdio"]
TSLS = [NODE, str(NM / "typescript-language-server" / "lib" / "cli.mjs"), "--stdio"]


def make_ws() -> None:
    WS.mkdir(exist_ok=True)
    for f in ("app.ts", "shapes.ts", "tsconfig.json"):
        shutil.copy(FIX / f, WS / f)
    (WS / "node_modules").mkdir(exist_ok=True)
    link = WS / "node_modules" / "typescript"
    if not link.exists():
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(TS6)], check=True, capture_output=True)


def node_procs() -> list[str]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.CommandLine }"],
        capture_output=True, text=True).stdout
    return [ln.strip() for ln in out.splitlines() if "s05_ts" in ln]


def uri_of(p: Path) -> str:
    return norm_uri(path_to_uri(p))


async def diag_wait(c: LspClient, path: Path, start: int, timeout: float = 6.0) -> dict[str, Any] | None:
    t0 = c.now()
    n = await c.wait_for("textDocument/publishDiagnostics", lambda p: norm_uri(p["uri"]) == uri_of(path),
                         timeout=timeout, start=start)
    if n is None:
        return None
    return {"dt_ms": round((n.t - t0) * 1000), "n": len(n.params["diagnostics"]),
            "version": n.params.get("version", "<absent>"),
            "codes": [d.get("code") for d in n.params["diagnostics"]]}


def msgs(c: LspClient) -> list[str]:
    return [str(n.params.get("message")) for n in c.notifications
            if n.method in ("window/logMessage", "window/showMessage")
            and any(k in str(n.params.get("message")) for k in ("sserver", "ypescript", "rror", "not find", "ersion"))][:6]


async def scenario(label: str, cmd: list[str], root: Path, *, init_options: dict[str, Any] | None = None,
                   cfg: Any = None, push_cfg: dict[str, Any] | None = None, deep: bool = False) -> dict[str, Any]:
    R: dict[str, Any] = {"label": label, "root": root.name}
    c = LspClient(name=label, cmd=cmd, cwd=root, config_provider=cfg)
    await c.start()
    try:
        res = await c.initialize(init_options=init_options)
        R["serverInfo"] = res.get("serverInfo")
        R["positionEncoding_in_response"] = res["capabilities"].get("positionEncoding", "<absent>")
        if push_cfg is not None:
            c.notify("workspace/didChangeConfiguration", {"settings": push_cfg})
        app = root / "app.ts"
        i0 = len(c.notifications)
        c.did_open(root / "shapes.ts", "typescript")
        c.did_open(app, "typescript")
        R["first_diag_app"] = await diag_wait(c, app, i0, 10.0)
        R["log"] = msgs(c)
        if deep:
            # (1) definition の生の形
            raw = await c.request("textDocument/definition", tdp(root, Target("app.ts", "const c = new Circle(2.0)", 14), "utf-16"))
            R["definition_raw"] = json.dumps(raw, ensure_ascii=False)[:700]
            # (3) クリーンなファイル shapes.ts: 開いて届くか、壊して直して空が届くか
            shapes = root / "shapes.ts"
            text = shapes.read_text(encoding="utf-8")
            R["clean_file_on_open"] = [n.params.get("version", "<absent>") for n in c.diag_for(shapes)] or "no publish"
            i1 = len(c.notifications)
            c.did_change(shapes, 2, text + "\nexport const bad: number = 'x';\n")
            R["break_v2"] = await diag_wait(c, shapes, i1)
            i2 = len(c.notifications)
            c.did_change(shapes, 3, text)
            R["fix_v3"] = await diag_wait(c, shapes, i2)
            # app.ts: 全エラーを消したとき空配列が来るか
            app_text = app.read_text(encoding="utf-8")
            i3 = len(c.notifications)
            fixed = app_text.replace('const count: number = "three";', "const count: number = 3;").replace(
                "import { Circle, Shape, describe }", "import { Circle, describe }")
            c.did_change(app, 2, fixed)
            R["app_all_fixed_v2"] = await diag_wait(c, app, i3)
            # (4) organizeImports の kind 指定（app.ts を元に戻してから）
            c.did_change(app, 3, app_text)
            await asyncio.sleep(1.0)
            oi: dict[str, Any] = {}
            for only in (["source.organizeImports"], ["source.organizeImports.ts"], ["source.removeUnusedImports.ts"],
                         ["source.fixAll.ts"]):
                r = await c.request("textDocument/codeAction", {
                    "textDocument": {"uri": path_to_uri(app)},
                    "range": {"start": {"line": 0, "character": 0}, "end": {"line": 18, "character": 0}},
                    "context": {"diagnostics": [], "only": only}})
                oi[",".join(only)] = [{"title": a.get("title"), "kind": a.get("kind"), "edit": "edit" in a,
                                       "command": (a.get("command") or {}).get("command")} for a in (r or [])]
            R["codeAction_only"] = oi
    except Exception as e:  # noqa: BLE001
        R["error"] = repr(e)[:400]
        R["log"] = msgs(c)
        R["stderr"] = [s for _, s in c.stderr_lines][:6]
    R["shutdown"] = await c.shutdown()
    await asyncio.sleep(0.8)
    R["node_procs_left"] = node_procs()
    return R


async def main() -> None:
    make_ws()
    out: dict[str, Any] = {}
    out["env"] = {"node": subprocess.run([NODE, "--version"], capture_output=True, text=True).stdout.strip(),
                  "which_vtsls_in_bin": shutil.which("vtsls", path=str(NM / ".bin")),
                  "which_tsls_in_bin": shutil.which("typescript-language-server", path=str(NM / ".bin")),
                  "bin_files": sorted(p.name for p in (NM / ".bin").iterdir() if "vtsls" in p.name),
                  "ts7_has_tsserver": (NM / "typescript" / "lib" / "tsserver.js").exists()}
    out["procs_before"] = node_procs()
    ws_tsdk = str(WS / "node_modules" / "typescript" / "lib")
    runs = [
        ("vtsls/default(bundled TS)", VTSLS, FIX, {"deep": True, "cfg": lambda it: {}}),
        ("vtsls/ws-autoUseWorkspaceTsdk", VTSLS, WS, {"cfg": lambda it: {"vtsls": {"autoUseWorkspaceTsdk": True}}}),
        ("vtsls/cfg-tsdk(path)", VTSLS, FIX, {"cfg": lambda it: {"typescript": {"tsdk": str(TS6 / "lib")}}}),
        ("vtsls/push-didChangeConfiguration-tsdk", VTSLS, FIX,
         {"cfg": lambda it: {}, "push_cfg": {"typescript": {"tsdk": str(TS6 / "lib")}}}),
        ("vtsls/initOptions-tsdk", VTSLS, FIX,
         {"cfg": lambda it: {}, "init_options": {"typescript": {"tsdk": str(TS6 / "lib")}}}),
        ("tsls/default(no path, only TS7 in node_modules)", TSLS, FIX, {"cfg": lambda it: {}}),
        ("tsls/ws-node_modules-typescript(TS6)", TSLS, WS, {"cfg": lambda it: {}}),
        ("tsls/initOptions-tsserver.path", TSLS, FIX,
         {"cfg": lambda it: {}, "deep": True,
          "init_options": {"tsserver": {"path": str(TS6 / "lib" / "tsserver.js")}}}),
        ("tsls/initOptions-tsserver.path(ws)", TSLS, WS,
         {"cfg": lambda it: {}, "init_options": {"tsserver": {"path": str(Path(ws_tsdk) / "tsserver.js")}}}),
    ]
    out["runs"] = []
    for label, cmd, root, kw in runs:
        r = await scenario(label, cmd, root, **kw)
        out["runs"].append(r)
        print(json.dumps({k: r.get(k) for k in ("label", "first_diag_app", "log", "error", "node_procs_left")},
                         ensure_ascii=False)[:900], flush=True)
    (HERE / "out").mkdir(exist_ok=True)
    (HERE / "out" / "extra.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
