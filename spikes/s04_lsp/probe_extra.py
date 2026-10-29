"""S-4 の追加実測（前の担当の run_s04.py が測っていない分）。使い捨て。

使い方: uv run python spikes/s04_lsp/probe_extra.py <節名>...   （節名なしで全部）
節: organize | locale | pypath | cold | unopened | ruff_edits | proctree | progress_change
結果: spikes/s04_lsp/out/extra_<節名>.json と標準出力。LSP は必ず shutdown → exit → 終了確認まで行う。
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lsp_probe import REPO, LspClient, apply_text_edits, find_venv_exe, norm_uri, path_to_uri

HERE = Path(__file__).parent
ROOT = HERE / "fixture_py"
VENV_PY = str(REPO / ".venv" / "Scripts" / "python.exe")
BP = [find_venv_exe("basedpyright-langserver") or "basedpyright-langserver", "--stdio"]
RUFF = [find_venv_exe("ruff") or "ruff", "server"]


def py_config(item: dict[str, Any]) -> object:
    sec = item.get("section")
    if sec == "python":
        return {"pythonPath": VENV_PY, "analysis": {"typeCheckingMode": "standard", "diagnosticMode": "openFilesOnly"}}
    return {}


def clean_env() -> dict[str, str]:
    """利用者の環境（.venv を PATH に持たない）を模す。"""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME")}
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if ".venv" not in p.lower())
    return env


def pubs(c: LspClient, path: Path | None = None) -> list[dict[str, Any]]:
    out = []
    for n in c.notifications:
        if n.method != "textDocument/publishDiagnostics":
            continue
        if path is not None and norm_uri(n.params["uri"]) != norm_uri(path_to_uri(path)):
            continue
        out.append({"t": round(n.t, 3), "file": Path(n.params["uri"].split("/")[-1]).name,
                    "version": n.params.get("version", "<absent>"), "n": len(n.params["diagnostics"]),
                    "codes": [str(d.get("code")) for d in n.params["diagnostics"]]})
    return out


def prog(c: LspClient) -> list[dict[str, Any]]:
    out = []
    for n in c.notifications:
        if n.method == "$/progress":
            v = n.params["value"]
            out.append({"t": round(n.t, 3), "kind": v.get("kind"), "message": v.get("message")})
    return out


def full_range(text: str) -> dict[str, Any]:
    lines = text.split("\n")
    return {"start": {"line": 0, "character": 0}, "end": {"line": len(lines) - 1, "character": len(lines[-1])}}


def summ_actions(res: Any) -> list[dict[str, Any]]:
    out = []
    for a in res or []:
        out.append({"title": a.get("title"), "kind": a.get("kind"), "has_edit": "edit" in a,
                    "has_command": "command" in a, "command": (a.get("command") or {}).get("command")
                    if isinstance(a.get("command"), dict) else a.get("command"), "has_data": "data" in a})
    return out


async def settle(c: LspClient, quiet_s: float = 1.0, max_s: float = 15.0) -> None:
    """通知が quiet_s 秒止まるまで待つ。"""
    t0 = time.perf_counter()
    last = len(c.notifications)
    t_last = time.perf_counter()
    while time.perf_counter() - t0 < max_s:
        await asyncio.sleep(0.1)
        if len(c.notifications) != last:
            last = len(c.notifications)
            t_last = time.perf_counter()
        elif time.perf_counter() - t_last >= quiet_s:
            return


# ---------------------------------------------------------------- 節
async def sec_organize() -> dict[str, Any]:
    """basedpyright の source.organizeImports。codeAction で出ないなら executeCommand / 設定を試す。"""
    R: dict[str, Any] = {}
    path = ROOT / "messy.py"
    text = path.read_text(encoding="utf-8")
    uri = path_to_uri(path)
    for label, cfg in (("既定", py_config),
                       ("basedpyright.disableOrganizeImports=false", lambda it: (
                           {"disableOrganizeImports": False, "analysis": {}} if it.get("section") == "basedpyright"
                           else py_config(it)))):
        c = LspClient("basedpyright", BP, ROOT, config_provider=cfg)
        applied: list[Any] = []
        orig = c._answer_server_request

        def hook(msg: dict[str, Any], c: LspClient = c, orig: Any = orig, applied: list[Any] = applied) -> None:
            if msg["method"] == "workspace/applyEdit":
                applied.append(msg.get("params"))
            orig(msg)

        c._answer_server_request = hook  # type: ignore[method-assign]
        await c.start()
        try:
            await c.initialize()
            c.did_open(path, "python")
            await settle(c, 1.0)
            r: dict[str, Any] = {"codeActionKinds": c.server_capabilities.get("codeActionProvider"),
                                 "executeCommand": c.server_capabilities.get("executeCommandProvider")}
            for name, ctx, rng in (
                ("only=organizeImports,全文", {"diagnostics": [], "only": ["source.organizeImports"]}, full_range(text)),
                ("only=source,全文", {"diagnostics": [], "only": ["source"]}, full_range(text)),
                ("only なし,先頭 0 幅", {"diagnostics": []}, {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}),
                ("only=organizeImports,0 幅", {"diagnostics": [], "only": ["source.organizeImports"]},
                 {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}),
            ):
                res = await c.request("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": ctx})
                r[name] = summ_actions(res)
            for cmd in ("basedpyright.organizeimports", "pyright.organizeimports", "basedpyright.organizeImports"):
                try:
                    res = await c.request("workspace/executeCommand", {"command": cmd, "arguments": [uri]}, timeout=10)
                    await asyncio.sleep(0.5)
                    r[f"executeCommand {cmd}"] = {"result": res, "applyEdit": applied[:1]}
                except Exception as e:  # noqa: BLE001
                    r[f"executeCommand {cmd}"] = repr(e)[:200]
            R[label] = r
        finally:
            R.setdefault("shutdowns", []).append(await c.shutdown())
    return R


async def sec_locale() -> dict[str, Any]:
    """診断メッセージの言語が initialize の locale / OS ロケールで変わるか。"""
    R: dict[str, Any] = {}
    path = ROOT / "app.py"
    for label, loc in (("locale 指定なし", None), ("locale=en", "en"), ("locale=ja", "ja")):
        c = LspClient("basedpyright", BP, ROOT, config_provider=py_config)
        await c.start()
        try:
            await c.initialize(locale=loc)
            c.did_open(path, "python")
            await settle(c, 1.0)
            last = [p for p in c.notifications if p.method == "textDocument/publishDiagnostics"][-1]
            R[label] = [d["message"][:90] for d in last.params["diagnostics"]]
        finally:
            await c.shutdown()
    return R


async def sec_pypath() -> dict[str, Any]:
    """インタープリタ（pythonPath）の渡し方。.venv を PATH から外した環境で dulwich が解決できるか。"""
    R: dict[str, Any] = {}
    probe = ROOT / "uses_pkg.py"
    probe.write_text("import dulwich\nprint(dulwich)\n", encoding="utf-8")
    cases: list[tuple[str, Any, Any]] = [
        ("なし", None, lambda it: {}),
        ("initializationOptions={pythonPath}", {"pythonPath": VENV_PY}, lambda it: {}),
        ("initializationOptions={python:{pythonPath}}", {"python": {"pythonPath": VENV_PY}}, lambda it: {}),
        ("workspace/configuration python.pythonPath", None, lambda it: {"pythonPath": VENV_PY} if it.get("section") == "python" else {}),
        ("workspace/configuration python.pythonPath + venvPath 不要", None,
         lambda it: {"pythonPath": VENV_PY, "analysis": {"autoSearchPaths": True}} if it.get("section") == "python" else {}),
    ]
    try:
        for label, io, prov in cases:
            c = LspClient("basedpyright", BP, ROOT, config_provider=prov, env=clean_env())
            await c.start()
            try:
                await c.initialize(init_options=io)
                c.did_open(probe, "python")
                await settle(c, 1.5)
                last = [p for p in c.notifications if p.method == "textDocument/publishDiagnostics"
                        and norm_uri(p.params["uri"]).endswith("uses_pkg.py")][-1]
                R[label] = [f"{d.get('code')}" for d in last.params["diagnostics"]] or "解決できた(診断なし)"
                # サーバーが選んだインタープリタ
                R[label + " / log"] = [n.params["message"][:140] for n in c.notifications
                                       if n.method == "window/logMessage" and "ython" in n.params["message"]
                                       and ("pythonPath" in n.params["message"] or "Python interpreter" in n.params["message"]
                                            or "python search paths" in n.params["message"].lower())][:3]
            finally:
                await c.shutdown()
    finally:
        probe.unlink(missing_ok=True)
    return R


async def sec_cold() -> dict[str, Any]:
    """冷えた起動 3 回: spawn → initialize 応答 → didOpen → 最初の publish → 実診断の publish → progress end。
    開いた直後（解析中）に definition/hover を投げたときの応答時間と中身も。"""
    R: dict[str, Any] = {"runs": []}
    app = ROOT / "app.py"
    for i in range(3):
        c = LspClient("basedpyright", BP, ROOT, config_provider=py_config)
        await c.start()
        try:
            await c.initialize()
            t_init = c.timings["initialize_ms"]
            t_open = c.now()
            c.did_open(app, "python")
            # 解析中に即 definition（Circle(2.0) の Circle）
            text = app.read_text(encoding="utf-8")
            line = text.split("\n").index("    c = Circle(2.0)")
            params = {"textDocument": {"uri": path_to_uri(app)}, "position": {"line": line, "character": 10}}
            t = time.perf_counter()
            d = await c.request("textDocument/definition", params, timeout=30)
            dt_def = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            h = await c.request("textDocument/hover", params, timeout=30)
            dt_hov = (time.perf_counter() - t) * 1000
            await settle(c, 1.0)
            ps = pubs(c, app)
            pg = prog(c)
            end = next((x["t"] for x in pg if x["kind"] == "end"), None)
            R["runs"].append({
                "spawn_to_init_resp_ms": round(c.timings["initialize_done"] * 1000, 1), "initialize_ms": t_init,
                "didOpen_at_s": round(t_open, 3),
                "first_publish_s": ps[0]["t"] if ps else None, "first_publish_n": ps[0]["n"] if ps else None,
                "real_publish_s": next((p["t"] for p in ps if p["n"] > 0), None),
                "progress_end_s": end,
                "publishes_app": ps,
                "definition_during_analysis_ms": round(dt_def, 1), "definition_result_n": len(d or []),
                "hover_during_analysis_ms": round(dt_hov, 1), "hover_has_value": bool(h),
            })
        finally:
            await c.shutdown()
    R["median_spawn_to_init_resp_ms"] = statistics.median(r["spawn_to_init_resp_ms"] for r in R["runs"])
    return R


async def sec_unopened() -> dict[str, Any]:
    """開いたのは shapes.py だけ。開いていない app.py / report.py を含む references / rename / callHierarchy が返るか。"""
    R: dict[str, Any] = {}
    shapes = ROOT / "shapes.py"
    text = shapes.read_text(encoding="utf-8")
    line = text.split("\n").index("def describe(shape: Shape) -> str:")
    pos = {"line": line, "character": 5}
    tdp = {"textDocument": {"uri": path_to_uri(shapes)}, "position": pos}
    c = LspClient("basedpyright", BP, ROOT, config_provider=py_config)
    await c.start()
    try:
        await c.initialize()
        c.did_open(shapes, "python")
        await settle(c, 1.0)
        refs = await c.request("textDocument/references", {**tdp, "context": {"includeDeclaration": True}})
        R["references(開いた文書=shapes.pyだけ)"] = sorted({Path(r["uri"]).name if False else r["uri"].split("/")[-1] for r in refs or []})
        t = time.perf_counter()
        ren = await c.request("textDocument/rename", {**tdp, "newName": "describe_shape"})
        R["rename_ms"] = round((time.perf_counter() - t) * 1000, 1)
        dc = ren.get("documentChanges") or []
        R["rename: 編集のあるファイル"] = sorted({d["textDocument"]["uri"].split("/")[-1] for d in dc if "textDocument" in d})
        R["rename: 形"] = [k for k in ("changes", "documentChanges") if k in ren]
        prep = await c.request("callHierarchy/prepareCallHierarchy" if False else "textDocument/prepareCallHierarchy", tdp)
        inc = await c.request("callHierarchy/incomingCalls", {"item": prep[0]})
        R["incomingCalls の呼び元"] = sorted(f"{i['from']['name']}@{i['from']['uri'].split('/')[-1]}" for i in inc or [])
        pubs_other = {p["file"] for p in pubs(c)}
        R["診断が届いたファイル(openFilesOnly)"] = sorted(pubs_other)
    finally:
        R["shutdown"] = await c.shutdown()
    return R


async def sec_ruff_edits() -> dict[str, Any]:
    """ruff の quickfix / organizeImports / fixAll を実際に適用した結果の本文（utf-8 列）。"""
    R: dict[str, Any] = {}
    path = ROOT / "messy.py"
    text = path.read_text(encoding="utf-8")
    uri = path_to_uri(path)
    c = LspClient("ruff", RUFF, ROOT, config_provider=lambda it: {})
    await c.start()
    try:
        await c.initialize()
        R["negotiated_positionEncoding"] = c.server_capabilities.get("positionEncoding")
        c.did_open(path, "python")
        await settle(c, 0.5)
        diags = [d for n in c.notifications if n.method == "textDocument/publishDiagnostics" for d in n.params["diagnostics"]]
        R["diagnostics"] = [{"code": d["code"], "range": d["range"], "sev": d["severity"], "source": d.get("source"),
                             "tags": d.get("tags"), "has_data": "data" in d,
                             "codeDescription": bool(d.get("codeDescription"))} for d in diags]
        rng = full_range(text)

        async def edits_of(label: str, only: list[str] | None, diagnostics: list[Any], select: str) -> None:
            ctx: dict[str, Any] = {"diagnostics": diagnostics}
            if only is not None:
                ctx["only"] = only
            t = time.perf_counter()
            res = await c.request("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": ctx})
            dt = (time.perf_counter() - t) * 1000
            act = next((a for a in res or [] if select in a["title"]), None)
            r: dict[str, Any] = {"codeAction_ms": round(dt, 1), "titles": [a["title"] for a in res or []]}
            if act is None:
                R[label] = r
                return
            r["has_edit_before_resolve"] = "edit" in act
            if "edit" not in act:
                t = time.perf_counter()
                act = await c.request("codeAction/resolve", act)
                r["resolve_ms"] = round((time.perf_counter() - t) * 1000, 1)
            we = act.get("edit") or {}
            r["edit_shape"] = [k for k in ("changes", "documentChanges") if k in we]
            dcs = we.get("documentChanges") or [{"textDocument": {"uri": u, "version": None}, "edits": e}
                                                for u, e in (we.get("changes") or {}).items()]
            r["textDocument.version"] = [d["textDocument"].get("version") for d in dcs]
            r["n_edits"] = sum(len(d["edits"]) for d in dcs)
            new = apply_text_edits(text, dcs[0]["edits"], "utf-8") if dcs else text
            r["result_head"] = new.split("\n")[:8]
            R[label] = r

        f401 = [d for d in diags if d["code"] == "F401"]
        await edits_of("quickfix F401 (診断つき,only=quickfix)", ["quickfix"], f401, "Remove unused import")
        await edits_of("source.organizeImports (only)", ["source.organizeImports"], [], "Organize imports")
        await edits_of("source.fixAll (only)", ["source.fixAll"], [], "Fix all")
    finally:
        R["shutdown"] = await c.shutdown()
    return R


def descendants(pid: int) -> list[dict[str, Any]]:
    ps = ("$all = Get-CimInstance Win32_Process; $ids = @(%d); $out=@(); do { $new = $all | Where-Object { $ids -contains $_.ParentProcessId -and $ids -notcontains $_.ProcessId }; "
          "foreach($n in $new){ $ids += $n.ProcessId; $out += $n } } while($new); "
          "$out | ForEach-Object { [pscustomobject]@{pid=$_.ProcessId; name=$_.Name; ppid=$_.ParentProcessId} } | ConvertTo-Json -Compress") % pid
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
    s = r.stdout.strip()
    if not s:
        return []
    v = json.loads(s)
    return v if isinstance(v, list) else [v]


def alive(pid: int) -> bool:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{'1'}} else {{'0'}}"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() == "1"


async def sec_proctree() -> dict[str, Any]:
    """起動した LSP の子孫プロセスが shutdown → exit のあとに残らないか。"""
    R: dict[str, Any] = {}
    for name, cmd, prov in (("basedpyright", BP, py_config), ("ruff", RUFF, lambda it: {})):
        c = LspClient(name, cmd, ROOT, config_provider=prov)
        await c.start()
        try:
            await c.initialize()
            c.did_open(ROOT / "messy.py", "python")
            await settle(c, 0.5)
            assert c.proc is not None
            top = c.proc.pid
            kids = descendants(top)
            res = await c.shutdown()
            await asyncio.sleep(0.5)
            R[name] = {"launcher_pid": top, "descendants_before_shutdown": kids,
                       "launcher_exit_code": res["exit_code"], "forced": res["forced"],
                       "descendants_alive_after": [k for k in kids if alive(k["pid"])]}
        except BaseException:
            await c.shutdown()
            raise
    return R


async def sec_progress_change() -> dict[str, Any]:
    """didChange のあとに $/progress は出るか（実診断の完了の合図に使えるか）。didOpen 直後の空 publish の有無も。"""
    R: dict[str, Any] = {}
    app = ROOT / "app.py"
    text = app.read_text(encoding="utf-8")
    c = LspClient("basedpyright", BP, ROOT, config_provider=py_config)
    await c.start()
    try:
        await c.initialize()
        c.did_open(app, "python")
        await settle(c, 1.0)
        mark = len(c.notifications)
        t0 = c.now()
        c.did_change(app, 2, text + "\nprint(undefined_zzz)\n")
        await settle(c, 1.0)
        R["after_didChange_notifications"] = [
            {"dt_ms": round((n.t - t0) * 1000, 1), "method": n.method,
             "version": (n.params or {}).get("version", "-") if n.method.endswith("publishDiagnostics") else None,
             "n": len(n.params["diagnostics"]) if n.method.endswith("publishDiagnostics") else None,
             "progress": (n.params["value"].get("kind") if n.method == "$/progress" else None)}
            for n in c.notifications[mark:]]
        R["progress_events_total"] = prog(c)
        R["server_requests_total"] = [r.method for r in c.server_requests]
    finally:
        R["shutdown"] = await c.shutdown()
    return R


async def sec_multiopen() -> dict[str, Any]:
    """4 ファイルを続けて didOpen したとき、実診断の前に空の publish（同じ版）が来るか。3 回。"""
    R: dict[str, Any] = {"runs": []}
    files = ["shapes.py", "app.py", "report.py", "messy.py"]
    for _ in range(3):
        c = LspClient("basedpyright", BP, ROOT, config_provider=py_config)
        await c.start()
        try:
            await c.initialize()
            for f in files:
                c.did_open(ROOT / f, "python")
            await settle(c, 1.5)
            R["runs"].append({"publishes": [(p["t"], p["file"], p["version"], p["n"]) for p in pubs(c)],
                              "progress": prog(c)})
        finally:
            await c.shutdown()
    return R


async def sec_watched() -> dict[str, Any]:
    """ディスク上で shapes.py を書き換えたとき（LSP に didChange なし）、開いている app.py の診断は更新されるか。
    didChangeWatchedFiles を送ると更新されるか。fixture は必ず元に戻す。"""
    R: dict[str, Any] = {}
    shapes, app = ROOT / "shapes.py", ROOT / "app.py"
    orig = shapes.read_bytes()
    c = LspClient("basedpyright", BP, ROOT, config_provider=py_config)
    await c.start()
    try:
        await c.initialize()
        c.did_open(app, "python")
        await settle(c, 1.0)
        R["初回 app.py"] = [(p["version"], p["codes"]) for p in pubs(c, app)]
        mark = len(c.notifications)
        shapes.write_bytes(orig.replace(b"def describe(", b"def describe_renamed("))
        t0 = c.now()
        await settle(c, 3.0, max_s=8.0)
        R["ディスク書き換えのみ(3秒待ち)"] = [(round(p["t"] - t0, 2), p["version"], p["codes"]) for p in pubs(c, app) if p["t"] > t0]
        c.notify("workspace/didChangeWatchedFiles", {"changes": [{"uri": path_to_uri(shapes), "type": 2}]})
        t1 = c.now()
        await settle(c, 2.0, max_s=8.0)
        R["didChangeWatchedFiles 後"] = [(round(p["t"] - t1, 2), p["version"], p["codes"]) for p in pubs(c, app) if p["t"] > t1]
    finally:
        shapes.write_bytes(orig)
        R["shutdown"] = await c.shutdown()
    return R


async def sec_orphan() -> dict[str, Any]:
    """起動口（launcher）だけ terminate したとき、子孫（python.exe → node.exe）が残るか。残ったものはこの節で kill する。"""
    R: dict[str, Any] = {}
    c = LspClient("basedpyright", BP, ROOT, config_provider=py_config)
    await c.start()
    await c.initialize()
    assert c.proc is not None
    kids = descendants(c.proc.pid)
    c.proc.terminate()
    await c.proc.wait()
    await asyncio.sleep(1.0)
    left = [k for k in kids if alive(k["pid"])]
    R = {"launcher_pid": c.proc.pid, "descendants": kids, "alive_after_terminate_of_launcher": left}
    for k in left:  # 自分が起こしたものだけ片付ける
        subprocess.run(["taskkill", "/PID", str(k["pid"]), "/F"], capture_output=True)
    await asyncio.sleep(0.5)
    R["alive_after_cleanup"] = [k for k in left if alive(k["pid"])]
    for t in c._tasks:
        t.cancel()
    return R


SECTIONS = {"organize": sec_organize, "locale": sec_locale, "pypath": sec_pypath, "cold": sec_cold,
            "unopened": sec_unopened, "ruff_edits": sec_ruff_edits, "proctree": sec_proctree,
            "progress_change": sec_progress_change, "multiopen": sec_multiopen,
            "watched": sec_watched, "orphan": sec_orphan}


async def main(names: list[str]) -> None:
    for n in names or list(SECTIONS):
        try:
            R = await SECTIONS[n]()
        except Exception as e:  # noqa: BLE001
            import traceback
            R = {"error": repr(e), "tb": traceback.format_exc()[-1500:]}
        (HERE / "out").mkdir(exist_ok=True)
        (HERE / "out" / f"extra_{n}.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"===== {n}")
        print(json.dumps(R, ensure_ascii=False, indent=1)[:6000])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    asyncio.run(main(sys.argv[1:]))
