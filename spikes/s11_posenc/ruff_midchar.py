"""S-11 補足: ruff（utf-8 交渉）に文字の途中を指す列で増分 didChange を送ると落ちるかの再現。使い捨て。
使い方: uv run python spikes/s11_posenc/ruff_midchar.py [boundary|mid]   （boundary=対照: 文字境界の列 / mid=「本」の途中のバイト）
結果: 終了コードと stderr のパニック行を表示。mid で exit_code=101（パニック）なら再現。
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "s04_lsp"))
sys.path.insert(0, str(HERE))
from lsp_probe import LspClient, find_venv_exe  # noqa: E402
from run_s11 import PY_TEXT  # noqa: E402


async def main(mode: str) -> None:
    lines = PY_TEXT.split("\n")
    bad = next(i for i, s in enumerate(lines) if s.startswith("bad ="))
    head = lines[bad][: lines[bad].index("日本語")]
    col = len(head.encode()) + (3 if mode == "boundary" else 4)  # 「日」の直後（境界）/ 「本」の 2 バイト目（途中）
    c = LspClient("ruff", [find_venv_exe("ruff") or "ruff", "server"], HERE / "fixture_py", config_provider=lambda it: {})
    await c.start()
    await c.initialize(encodings=["utf-8", "utf-16"])
    print("negotiated:", c.server_capabilities.get("positionEncoding"))
    uri = c.did_open(HERE / "fixture_py" / "uni.py", "python", text=PY_TEXT)
    await asyncio.sleep(2)
    p0 = {"line": bad, "character": col}
    c.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": 2},
                                        "contentChanges": [{"range": {"start": p0, "end": p0}, "text": "X"}]})
    await asyncio.sleep(3)
    print("returncode after didChange:", c.proc.returncode)
    print(await c.shutdown())
    print([s for _, s in c.stderr_lines if "panic" in s.lower() or "boundary" in s.lower() or "byte index" in s.lower()][:4])


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "mid"))
