"""S-4: basedpyright と ruff server の起動と主要要求を実測する。使い捨て。

使い方: uv run python spikes/s04_lsp/run_s04.py basedpyright | ruff | interp
結果: spikes/s04_lsp/out/<名前>.json（gitignore 済み）と標準出力の要約。LSP は必ず shutdown → exit まで行う。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from lsp_probe import REPO, LspClient, find_venv_exe, path_to_uri
from probe_features import Spec, Target, dump, run_suite

HERE = Path(__file__).parent
ROOT = HERE / "fixture_py"
VENV_PY = str(REPO / ".venv" / "Scripts" / "python.exe")


def py_config(item: dict) -> object:
    sec = item.get("section")
    if sec == "python":
        return {"pythonPath": VENV_PY}
    if sec in ("basedpyright.analysis", "python.analysis"):
        return {"typeCheckingMode": "standard", "diagnosticMode": "openFilesOnly"}
    return {}


def spec_basedpyright() -> Spec:
    return Spec(
        name="basedpyright",
        cmd=[find_venv_exe("basedpyright-langserver") or "basedpyright-langserver", "--stdio"],
        root=ROOT, language_id="python", files=["shapes.py", "app.py", "report.py", "messy.py"],
        config_provider=py_config,
        definition=Target("app.py", "c = Circle(2.0)", 4),
        references=Target("shapes.py", "def area", 4),
        hovers=[Target("app.py", "radii: list[float]", 0), Target("app.py", "c = Circle(2.0)", 0),
                Target("app.py", "total_area([1.0", 0), Target("app.py", "Circle(r)", 0)],
        rename=(Target("shapes.py", "def describe", 4), "describe_shape"),
        call_hierarchy=Target("shapes.py", "def describe", 4),
        code_action_files=["app.py", "shapes.py"], organize_imports_file="messy.py",
        change_file="app.py", change_old='count: int = "three"', change_new="count: int = 3",
        change_text_extra="\nprint(undefined_name_xyz)\n",
    )


def spec_ruff() -> Spec:
    return Spec(
        name="ruff",
        cmd=[find_venv_exe("ruff") or "ruff", "server"],
        root=ROOT, language_id="python", files=["messy.py", "shapes.py"],
        config_provider=lambda item: {},
        code_action_files=["messy.py", "shapes.py"], organize_imports_file="messy.py",
        change_file="messy.py", change_old="import sys\n", change_new="",
        change_text_extra="\nimport sys\n",
    )


async def interp() -> dict:
    """利用者の環境に basedpyright が無い想定: PATH から .venv を外し、pythonPath の渡し方で import 解決が変わるか。"""
    import os
    clean = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME")}
    clean["PATH"] = os.pathsep.join(p for p in clean["PATH"].split(os.pathsep)
                                    if ".venv" not in p.lower() and "uv" not in p.lower().split(os.sep)[-1:])
    probe = ROOT / "uses_pkg.py"
    probe.write_text('import dulwich\nimport mcp\nprint(dulwich, mcp)\n', encoding="utf-8")
    out: dict = {}
    try:
        for label, provider in (("pythonPath なし", lambda it: {}),
                                ("workspace/configuration で pythonPath", py_config)):
            c = LspClient("basedpyright", [find_venv_exe("basedpyright-langserver") or "x", "--stdio"], ROOT,
                          config_provider=provider, env=clean)
            await c.start()
            try:
                await c.initialize()
                c.did_open(probe, "python")
                n = await c.wait_for("textDocument/publishDiagnostics", timeout=60)
                await asyncio.sleep(2)
                last = [x for x in c.notifications if x.method == "textDocument/publishDiagnostics"][-1]
                out[label] = {"diagnostics": [f"{d.get('code')}: {d['message'][:80]}" for d in last.params["diagnostics"]],
                              "sections_asked": [it.get("section") for r in c.server_requests
                                                 if r.method == "workspace/configuration" for it in r.params["items"]]}
            finally:
                await c.shutdown()
    finally:
        probe.unlink(missing_ok=True)
    return out


async def main(which: str) -> None:
    if which == "interp":
        R = await interp()
    else:
        R = await run_suite({"basedpyright": spec_basedpyright, "ruff": spec_ruff}[which]())
    dump(R, HERE / "out" / f"{which}.json")
    print(json.dumps(R, ensure_ascii=False, indent=1)[:12000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
