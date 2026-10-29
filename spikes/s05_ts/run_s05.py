"""S-5: TypeScript の LSP（vtsls / typescript-language-server）の起動と主要要求を実測する。使い捨て。

使い方: uv run python spikes/s05_ts/run_s05.py vtsls | vtsls-cmd | tsls     （事前: npm install --prefix spikes/s05_ts/.cache @vtsls/language-server typescript-language-server）
結果: spikes/s05_ts/out/<名前>.json（gitignore 済み）と標準出力の要約。`vtsls-cmd` は node_modules/.bin/vtsls.cmd を直接起動する版（Windows のシム越し）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "s04_lsp"))

from probe_features import Spec, Target, dump, run_suite  # noqa: E402

ROOT = HERE / "fixture_ts"
CACHE = HERE / ".cache" / "node_modules"


def spec_for(which: str) -> Spec:
    node = shutil.which("node") or "node"
    if which == "vtsls":
        cmd = [node, str(CACHE / "@vtsls" / "language-server" / "bin" / "vtsls.js"), "--stdio"]
    elif which == "vtsls-cmd":
        cmd = [str(CACHE / ".bin" / "vtsls.cmd"), "--stdio"]
    elif which == "tsls":
        cmd = [node, str(CACHE / "typescript-language-server" / "lib" / "cli.mjs"), "--stdio"]
    else:
        raise SystemExit(f"unknown server: {which}")
    return Spec(
        name=which, cmd=cmd, root=ROOT, language_id="typescript", files=["shapes.ts", "app.ts"],
        config_provider=lambda item: {},
        init_options=None if which != "tsls" else {
            "tsserver": {"path": str(HERE / ".cache" / "ts6" / "node_modules" / "typescript" / "lib" / "tsserver.js")}},
        definition=Target("app.ts", "const c = new Circle(2.0)", 14),
        references=Target("shapes.ts", "area(): number", 0),
        hovers=[Target("app.ts", "radii: number[]", 0), Target("app.ts", "const c = new Circle", 6),
                Target("app.ts", "totalArea([1.0", 0), Target("app.ts", "new Circle(r)", 4)],
        rename=(Target("shapes.ts", "function describe", 9), "describeShape"),
        call_hierarchy=Target("shapes.ts", "function describe", 9),
        code_action_files=["app.ts"], organize_imports_file="app.ts",
        change_file="app.ts", change_old='const count: number = "three";', change_new="const count: number = 3;",
        change_text_extra="\nconsole.log(undefinedNameXyz);\n",
        settle_s=4.0, comment="//",
    )


async def main(which: str) -> None:
    R = await run_suite(spec_for(which))
    dump(R, HERE / "out" / f"{which}.json")
    print(json.dumps(R, ensure_ascii=False, indent=1)[:6000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
