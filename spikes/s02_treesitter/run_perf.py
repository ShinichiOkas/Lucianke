"""S-2 項目 4・5: 解析性能（全解析・増分・クエリ・走査）とスレッド挙動（Parser 共有・Tree 共有・GIL 解放）を実測する。

使い方: uv run python spikes/s02_treesitter/run_perf.py            # 全部。所要 1〜2 分
        uv run python spikes/s02_treesitter/run_perf.py --shared   # 子プロセス用（Parser 共有の安全性試験。単独実行も可）
出力: 中央値・p95 などの実測値。Parser 共有の試験は子プロセスで走らせ、終了コードで結果を判定する（クラッシュしても本体は続行）。
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import pathlib
import platform
import statistics
import subprocess
import sys
import sysconfig
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

import tree_sitter as ts  # noqa: E402
from snippets import ParserPool, get_language, parse_with_deadline, run_tags  # noqa: E402

TAGS_PY = (
    "(function_definition name: (identifier) @name) @definition.function\n"
    "(class_definition name: (identifier) @name) @definition.class\n"
)


def pick_stdlib_file(target_lines: int = 2000) -> pathlib.Path:
    std = pathlib.Path(sysconfig.get_paths()["stdlib"])
    best = None
    for p in std.glob("*.py"):
        try:
            n = p.read_bytes().count(b"\n")
        except OSError:
            continue
        if best is None or abs(n - target_lines) < abs(best[0] - target_lines):
            best = (n, p)
    assert best
    return best[1]


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def fmt(xs: list[float]) -> str:
    return f"中央値 {statistics.median(xs)*1000:.2f} ms / p95 {pct(xs, .95)*1000:.2f} ms / 最小 {min(xs)*1000:.2f} ms (n={len(xs)})"


def bench(fn, n: int = 100) -> list[float]:
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return out


PATH = pick_stdlib_file()
SRC = PATH.read_bytes()
PY = get_language("python")


def tree_sexp_sig(tree: ts.Tree) -> tuple[int, int, int]:
    r = tree.root_node
    return (r.end_byte, r.descendant_count, len(str(r)))


if "--progress" in sys.argv:
    _src = SRC * 3
    _p = ts.Parser(PY)
    _t = _p.parse(lambda off, pt: _src[off : off + 16384], progress_callback=lambda off, has_err: False)
    print("progress_callback ok", _t.root_node.end_byte)
    sys.exit(0)
if "--progress-query" in sys.argv:
    _t = ts.Parser(PY).parse(SRC)
    _q = ts.Query(PY, TAGS_PY)
    _r = ts.QueryCursor(_q).captures(_t.root_node, lambda *a: True, lambda off: False)
    print("query progress_callback ok", sum(len(v) for v in _r.values()))
    sys.exit(0)

# ---- 子プロセス: 1 つの Parser を 4 スレッドで共有して回す ---------------------------------------
if "--shared" in sys.argv:
    shared = ts.Parser(PY)
    expect = tree_sexp_sig(shared.parse(SRC))
    bad = []
    errs = []

    def work(_i: int) -> None:
        for _ in range(30):
            try:
                if tree_sexp_sig(shared.parse(SRC)) != expect:
                    bad.append(1)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")

    ths = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    print(f"shared-parser: 4 threads x 30 parses, wrong_results={len(bad)}, exceptions={len(errs)} {errs[:2]}")
    sys.exit(0 if not bad and not errs else 3)


print(f"環境: {platform.platform()} / Python {platform.python_version()} / CPU {os.cpu_count()} 論理コア / tree-sitter {ts.__name__} LANGUAGE_VERSION={ts.LANGUAGE_VERSION}")
nlines = SRC.count(b"\n")
print(f"対象: {PATH.name}  {nlines} 行  {len(SRC)/1024:.0f} KiB\n")

# ---- 4. 性能 -------------------------------------------------------------------------------------
print("== 4. 性能（Python 3.12・この機械） ==")
t0 = time.perf_counter()
parser = ts.Parser(PY)
first_tree = parser.parse(SRC)
print(f"- 初回解析（Parser 生成込み・プロセス内で最初の 1 回）: {(time.perf_counter()-t0)*1000:.2f} ms")
full = bench(lambda: parser.parse(SRC), 100)
print(f"- 全解析 100 回: {fmt(full)}")
full_cb = bench(lambda: parser.parse(lambda off, _pt: SRC[off : off + 16384]), 100)
print(f"- 全解析（read_callback 16 KiB 版）100 回: {fmt(full_cb)}")
comp = bench(lambda: ts.Query(PY, TAGS_PY), 100)
print(f"- Query のコンパイル（関数・クラスの 2 パターン）100 回: {fmt(comp)}")

lines = SRC.split(b"\n")
mid = nlines // 2
# 編集 A: 中ほどの行の識別子 1 文字を足す（行数不変）。編集 B: 1 行を丸ごと挿入（行数 +1）
while not lines[mid].strip() or lines[mid].lstrip().startswith(b"#"):
    mid += 1
off = sum(len(l) + 1 for l in lines[:mid])
old_line = lines[mid]


def make_edit(kind: str):
    if kind == "A":  # 行末に " + 1" を足す代わりに、先頭のインデント直後へ 1 文字挿入
        indent = len(old_line) - len(old_line.lstrip())
        ins = b"_"
        pos = off + indent
        new_src = SRC[:pos] + ins + SRC[pos:]
        return new_src, dict(start_byte=pos, old_end_byte=pos, new_end_byte=pos + 1,
                             start_point=(mid, indent), old_end_point=(mid, indent), new_end_point=(mid, indent + 1))
    ins = b"    zz = 1\n"
    new_src = SRC[:off] + ins + SRC[off:]
    return new_src, dict(start_byte=off, old_end_byte=off, new_end_byte=off + len(ins),
                         start_point=(mid, 0), old_end_point=(mid, 0), new_end_point=(mid + 1, 0))


for kind, label in (("A", "1 文字挿入（行数不変）"), ("B", "1 行挿入")):
    new_src, e = make_edit(kind)
    times = []
    ok = True
    for _ in range(100):
        t = first_tree.copy()  # edit は木を直接書き換えるので、毎回コピーしてから編集（コピー時間は計測外）
        s = time.perf_counter()
        t.edit(**e)
        inc = parser.parse(new_src, t)
        times.append(time.perf_counter() - s)
    ok = str(inc.root_node) == str(ts.Parser(PY).parse(new_src).root_node)
    print(f"- 増分解析（{label}）edit+parse 100 回: {fmt(times)}  全解析と同一: {ok}")

tags_t = bench(lambda: run_tags("python", TAGS_PY, first_tree), 100)
nt = len(run_tags("python", TAGS_PY, first_tree))
print(f"- タグ風クエリ（関数・クラス {nt} 件）matches→Tag 化 100 回: {fmt(tags_t)}")


def walk_all(tree: ts.Tree) -> int:
    c = tree.walk()
    n = 0
    while True:
        n += 1
        if c.goto_first_child():
            continue
        while not c.goto_next_sibling():
            if not c.goto_parent():
                return n


walk_t = bench(lambda: walk_all(first_tree), 20)
print(f"- 全ノード走査（TreeCursor、{first_tree.root_node.descendant_count} ノード）20 回: {fmt(walk_t)}")

# ---- 5. スレッド ---------------------------------------------------------------------------------
print("\n== 5. スレッド ==")
K = 25
pool = ParserPool()


def parse_k(_i: int) -> float:
    s = time.perf_counter()
    p = pool.parser("python")  # スレッドごとの Parser
    for _ in range(K):
        p.parse(SRC)
    return time.perf_counter() - s


parse_k(0)  # ウォームアップ
serial = []
for _ in range(3):
    s = time.perf_counter()
    for i in range(4):
        parse_k(i)
    serial.append(time.perf_counter() - s)
par = []
with cf.ThreadPoolExecutor(4) as ex:
    list(ex.map(parse_k, range(4)))  # ウォームアップ（各スレッドの Parser 生成）
    for _ in range(3):
        s = time.perf_counter()
        list(ex.map(parse_k, range(4)))
        par.append(time.perf_counter() - s)
print(f"- 4 スレッド（スレッドごとに Parser）で {K} 回ずつ解析: {statistics.median(par)*1000:.0f} ms / 直列 4 本分: {statistics.median(serial)*1000:.0f} ms / 速度比 {statistics.median(serial)/statistics.median(par):.2f}x（4 に近いほど GIL を解放している）")

# GIL を解放するか: 別スレッド（純 Python のスピナー）が止まる最長の時間を、解析の区間内で測る。
def gil_probe(fn) -> tuple[float, float]:
    """fn の実行時間と、その間にスピナーが一度も動けなかった最長の連続時間（秒）を返す。"""
    stop = threading.Event()
    gaps: list[tuple[float, float]] = []

    def spin() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            now = time.perf_counter()
            if now - last > 0.002:
                gaps.append((last, now))
            last = now

    th = threading.Thread(target=spin)
    th.start()
    time.sleep(0.1)
    s0 = time.perf_counter()
    fn()
    e0 = time.perf_counter()
    time.sleep(0.05)
    stop.set()
    th.join()
    worst = max((min(g1, e0) - max(g0, s0) for g0, g1 in gaps if g1 > s0 and g0 < e0), default=0.0)
    return e0 - s0, worst


BIG = SRC * 10
big_parser = ts.Parser(PY)
big_parser.parse(BIG)


def make_reader(chunk: int):
    def read(offset: int, _pt) -> bytes:
        return BIG[offset : offset + chunk]

    return read


probes = [("parse(bytes)", lambda: big_parser.parse(BIG))]
probes += [(f"parse(read_callback, {c//1024} KiB ずつ)", (lambda r=make_reader(c): big_parser.parse(r))) for c in (64 * 1024, 16 * 1024, 4 * 1024)]
for label, fn in probes:
    dur, worst = gil_probe(fn)
    print(f"- GIL 試験 {label}: {len(BIG.splitlines())} 行（{len(BIG)/1024:.0f} KiB）の解析 {dur*1000:.0f} ms の間、他スレッドが動けなかった最長 {worst*1000:.0f} ms"
          f"（≒解析時間なら GIL を握りっぱなし）")
rc = bench(lambda r=make_reader(16 * 1024): big_parser.parse(r), 10)
rb = bench(lambda: big_parser.parse(BIG), 10)
print(f"- 参考: {len(BIG)/1024:.0f} KiB の解析 bytes 版 中央値 {statistics.median(rb)*1000:.0f} ms / read_callback(16 KiB) 版 中央値 {statistics.median(rc)*1000:.0f} ms")
BIG5 = SRC * 72  # 約 5 MB（MAX_FILE_BYTES）
t5 = bench(lambda: ts.Parser(PY).parse(BIG5), 3)
n5 = len(BIG5.splitlines())
print(f"- 参考: 約 {len(BIG5)/1024/1024:.1f} MB（{n5} 行）の解析 中央値 {statistics.median(t5)*1000:.0f} ms")
# progress_callback は子プロセスで試す（0.26.0 / Windows / cp312 でアクセス違反になる）
r = subprocess.run([sys.executable, str(pathlib.Path(__file__)), "--progress"], capture_output=True, text=True, timeout=120, encoding="utf-8")
print(f"- Parser.parse(..., progress_callback=...)（子プロセス）: returncode={r.returncode}"
      f"{'（0xC0000005 = アクセス違反）' if r.returncode in (3221225477, -1073741819, 139, -11) else ''} {(r.stdout.strip() or r.stderr.strip()[:120])}")
r = subprocess.run([sys.executable, str(pathlib.Path(__file__)), "--progress-query"], capture_output=True, text=True, timeout=120, encoding="utf-8")
print(f"- QueryCursor.captures(node, pred, progress_callback)（子プロセス）: returncode={r.returncode}"
      f"{'（アクセス違反）' if r.returncode in (3221225477, -1073741819, 139, -11) else ''} {(r.stdout.strip() or r.stderr.strip()[:120])}")


tm = []
for _ in range(3):
    s0 = time.perf_counter()
    _, to = parse_with_deadline(ts.Parser(PY), BIG5, 0.05)
    tm.append((time.perf_counter() - s0, to))
print(f"- 期限 50 ms の parse_with_deadline（{len(BIG5)/1024/1024:.1f} MB）: 実際 {', '.join(f'{d*1000:.0f} ms(timed_out={t})' for d, t in tm)}")

# Tree/Node を別スレッドから読む
tree = ts.Parser(PY).parse(SRC)
expected = [(t.kind, t.name, tuple(t.node.start_point)) for t in run_tags("python", TAGS_PY, tree)]
results: list = []
errs: list = []


def read_in_thread(_i: int) -> None:
    try:
        got = [(t.kind, t.name, tuple(t.node.start_point)) for t in run_tags("python", TAGS_PY, tree)]
        txt = tree.root_node.children[0].text
        results.append(got == expected and txt is not None)
    except Exception as e:  # noqa: BLE001
        errs.append(f"{type(e).__name__}: {e}")


ths = [threading.Thread(target=read_in_thread, args=(i,)) for i in range(8)]
[t.start() for t in ths]
[t.join() for t in ths]
print(f"- Tree を別スレッドから読む（8 スレッドが同じ Tree・同じ Query で QueryCursor を各自生成して 実行）: 一致 {sum(results)}/8, 例外 {len(errs)} {errs[:1]}")
# 解析スレッドで作った木を別スレッドで読む（ADR-13 の使い方: プールで解析→呼び出しスレッドで読む）
with cf.ThreadPoolExecutor(1) as ex:
    t_other = ex.submit(lambda: pool.parse("python", SRC)).result()
print(f"- 別スレッドで解析した Tree を呼び出し側スレッドで読む: タグ {len(run_tags('python', TAGS_PY, t_other))} 件（期待 {len(expected)}）")

# Parser 共有（子プロセスで）
for i in range(3):
    r = subprocess.run([sys.executable, str(pathlib.Path(__file__)), "--shared"], capture_output=True, text=True, timeout=300, encoding="utf-8")
    print(f"- Parser を 4 スレッドで共有（子プロセス {i+1}/3）: returncode={r.returncode} {r.stdout.strip() or r.stderr.strip()[-200:]}")

# プロセス 4 本との比較（R-6 の参考）。起動・import の時間は含めず、同時刻に開始させて解析ループだけを測る
start_at = time.time() + 2.0
procs = [subprocess.Popen([sys.executable, str(pathlib.Path(__file__).parent / "procwork.py"), str(PATH), str(K), str(start_at)],
                          stdout=subprocess.PIPE, text=True) for _ in range(4)]
ends = [float(p.communicate()[0].strip()) for p in procs]
pp = max(ends) - start_at
print(f"- 参考: 別プロセス 4 本で {K} 回ずつ（起動・import 除く）: {pp*1000:.0f} ms / 直列比 {statistics.median(serial)/pp:.2f}x")
