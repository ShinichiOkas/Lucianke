"""run_perf.py の「プロセス 4 本で並列解析」比較用ワーカー（子プロセスとして起動される）。

使い方: uv run python spikes/s02_treesitter/procwork.py <ファイル> <回数> <開始エポック秒>
動作: 開始時刻まで待ち、<ファイル> を <回数> 回解析して、終了エポック秒を標準出力に出す（起動・import の時間は含まない）。
"""

import pathlib
import sys
import time

import tree_sitter as ts
from snippets import get_language

if __name__ == "__main__":
    path, k, start_at = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    src = pathlib.Path(path).read_bytes()
    p = ts.Parser(get_language("python"))
    p.parse(src)
    while time.time() < start_at:
        time.sleep(0.001)
    for _ in range(k):
        p.parse(src)
    print(time.time())
