"""S-2 補足: 新しいプロセスでの import と Language 生成の所要時間（コールドスタートへの影響）を測る。

使い方: uv run python spikes/s02_treesitter/run_coldstart.py
出力: tree_sitter 本体と 18 言語それぞれの import 時間・Language 生成時間・合計。子プロセスで 5 回測って中央値を出す。
"""

import json
import statistics
import subprocess
import sys
import pathlib

CHILD = r'''
import json, sys, time, importlib
sys.path.insert(0, sys.argv[1])
t = time.perf_counter(); import tree_sitter; t_core = time.perf_counter() - t
from snippets import LANGUAGES
out = {"tree_sitter": t_core}
seen = {}
for lid, (mod, fn) in LANGUAGES.items():
    t = time.perf_counter(); m = importlib.import_module(mod); ti = time.perf_counter() - t
    t = time.perf_counter(); lang = tree_sitter.Language(getattr(m, fn)()); tl = time.perf_counter() - t
    out[lid] = ti + tl
print(json.dumps(out))
'''

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    here = str(pathlib.Path(__file__).parent)
    runs = []
    for _ in range(5):
        r = subprocess.run([sys.executable, "-c", CHILD, here], capture_output=True, text=True, check=True)
        runs.append(json.loads(r.stdout))
    keys = list(runs[0])
    print("| 対象 | import + Language 生成（中央値 ms, 5 プロセス） |")
    print("|---|---|")
    total = 0.0
    for k in keys:
        v = statistics.median(r[k] for r in runs) * 1000
        total += v
        print(f"| {k} | {v:.1f} |")
    print(f"| 合計（tree_sitter + 18 言語） | {total:.1f} |")
