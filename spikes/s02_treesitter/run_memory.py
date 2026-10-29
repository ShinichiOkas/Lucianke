"""S-2 補足: 構文木 1 本あたりのメモリ（ソースのバイト数に対する倍率）を実測する。TREE_CACHE_MAX_BYTES の見積り用。

使い方: uv run python spikes/s02_treesitter/run_memory.py
出力: 言語ごとの「木を 30 本保持したときのヒープ増加 / ソース総バイト数」。tracemalloc（py-tree-sitter は PyMem 経由で確保するので捕捉できる）と、Windows なら作業セットも出す。
"""

import gc
import json
import os
import pathlib
import sys
import sysconfig
import tracemalloc

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

import tree_sitter as ts  # noqa: E402
from snippets import get_language  # noqa: E402


def working_set() -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [(n, ctypes.c_size_t) for n in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

    k = ctypes.WinDLL("kernel32")
    k.GetCurrentProcess.restype = wt.HANDLE
    ps = ctypes.WinDLL("psapi")
    ps.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    ps.GetProcessMemoryInfo.restype = wt.BOOL
    pmc = PMC()
    pmc.cb = ctypes.sizeof(pmc)
    ps.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return pmc.WorkingSetSize


def measure(lang_id: str, srcs: list[bytes], copies: int = 30) -> str:
    p = ts.Parser(get_language(lang_id))
    gc.collect()
    ws0 = working_set()
    tracemalloc.start()
    m0 = tracemalloc.get_traced_memory()[0]
    trees = [p.parse(s) for s in srcs * copies]
    m1 = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    total = sum(len(s) for s in srcs * copies)
    ws1 = working_set()
    out = f"| {lang_id} | {total/1e6:.1f} MB | {(m1-m0)/total:.1f}x |"
    out += f" {(ws1-ws0)/total:.1f}x |" if ws0 is not None else " - |"
    del trees
    return out


if __name__ == "__main__":
    std = pathlib.Path(sysconfig.get_paths()["stdlib"])
    py = [(std / n).read_bytes() for n in ("pdb.py", "typing.py", "argparse.py", "enum.py")]
    js = json.dumps([{"id": i, "name": f"item{i}", "tags": ["a", "b"], "v": {"x": i, "y": None}} for i in range(5000)], indent=2).encode()
    md = [p.read_bytes() for p in pathlib.Path("docs").glob("*.md")]
    print("| 言語 | ソース総量 | tracemalloc 比 | 作業セット比（Windows） |")
    print("|---|---|---|---|")
    print(measure("python", py))
    print(measure("json", [js]))
    if md:
        print(measure("markdown", md, copies=10))
