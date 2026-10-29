"""S-3: ast-grep-py 0.51 の検索・書き換え API を実測する（print($MSG) -> logger.info($MSG) を Python / TypeScript で）。

使い方: uv run python spikes/s03_astgrep/run_astgrep.py
出力: 項目ごとの "[OK] / [NG]" と観測値。NG が 1 つでもあれば終了コード 1。
前提: .venv 同期済み（ast-grep-py 0.51.0）。snippets.py（同じフォルダ）の部品も同時に検証する。
"""

from __future__ import annotations

import concurrent.futures as cf
import pathlib
import statistics
import sys
import sysconfig
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

import ast_grep_py  # noqa: E402
import tree_sitter as ts  # noqa: E402
import tree_sitter_python as tsp  # noqa: E402
from ast_grep_py import SgRoot  # noqa: E402
from snippets import (  # noqa: E402
    PatternError,
    TemplateError,
    UnsupportedLanguage,
    pattern_hint,
    make_root,
    outermost,
    rewrite,
    search,
)

FAILS = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global FAILS
    FAILS += 0 if cond else 1
    print(f"[{'OK' if cond else 'NG'}] {label}" + (f"  -> {detail}" if detail != "" else ""))


from importlib import metadata  # noqa: E402

print(f"ast-grep-py {metadata.version('ast-grep-py')}\n")

print("== 7. 公開 API ==")
check("SgRoot / SgNode / Edit / Pos / Range が公開", all(hasattr(ast_grep_py, n) for n in ("SgRoot", "SgNode", "Edit", "Pos", "Range")))
check("SgNode に find_all / get_match / get_multiple_matches / replace / commit_edits / get_transformed がある",
      all(hasattr(ast_grep_py.SgNode, n) for n in ("find_all", "get_match", "get_multiple_matches", "replace", "commit_edits", "get_transformed")))

PY_SRC = "x = 1\nprint('a')\nprint(f'b {x}')\nfoo(print(1, 2), 3)\n"
root = SgRoot(PY_SRC, "python").root()
ms = root.find_all(pattern="print($MSG)")
check("find_all(pattern='print($MSG)')（引数 1 個の呼び出しだけ一致。print(1, 2) は一致しない）", [m.text() for m in ms] == ["print('a')", "print(f'b {x}')"], [m.text() for m in ms])
check("get_match('MSG') で単一メタ変数、未定義名は None", ms[0].get_match("MSG").text() == "'a'" and ms[0].get_match("NOPE") is None)
multi = root.find_all(pattern="print($$$ARGS)")[2].get_multiple_matches("ARGS")
check("get_multiple_matches('ARGS') は区切りの ',' も 1 ノードとして含む", [n.text() for n in multi] == ["1", ",", "2"], [(n.text(), n.kind()) for n in multi])

print("\n== 一致範囲の単位（全角・絵文字を同じ行に置く） ==")
U_SRC = "s = 'あ😀'; print('日本語😀', x)\nprint('z')\n"
um = SgRoot(U_SRC, "python").root().find_all(pattern="print($$$A)")[0]
r = um.range()
cp_start = U_SRC.index("print")
b_start = len(U_SRC[:cp_start].encode())
check("Pos.column / Pos.index はコードポイント単位（バイトでも UTF-16 でもない）",
      (r.start.line, r.start.column, r.start.index) == (0, cp_start, cp_start) and b_start != cp_start,
      f"ast-grep={r.start.column} / コードポイント={cp_start} / バイト={b_start} / UTF-16={len(U_SRC[:cp_start].encode('utf-16-le'))//2}")
check("Edit.start_pos / end_pos もコードポイント単位", (um.replace("X").start_pos, um.replace("X").end_pos) == (r.start.index, r.end.index))
check("index で str を切ると一致テキストになる", U_SRC[r.start.index : r.end.index] == um.text())
t = ts.Parser(ts.Language(tsp.language())).parse(U_SRC.encode())


def _walk(n):
    yield n
    for c in n.children:
        yield from _walk(c)


call = next(n for n in _walk(t.root_node) if n.type == "call" and n.text.startswith(b"print"))
tb_col = call.start_point.column  # tree-sitter はバイト列
cp_from_ts = len(U_SRC.encode()[call.start_byte - tb_col : call.start_byte].decode())
check("tree-sitter（バイト列）をコードポイントに直した位置と ast-grep の位置が一致", cp_from_ts == r.start.column, (tb_col, cp_from_ts, r.start.column))
r3 = SgRoot("x = 1\r\nprint('a')\r\n", "python").root().find_all(pattern="print($M)")[0].range()
check("CRLF: 行は 0 始まり、index は \\r を 1 文字として数える", (r3.start.line, r3.start.column, r3.start.index, r3.end.index) == (1, 0, 7, 17), (r3.start.line, r3.start.column, r3.start.index, r3.end.index))

print("\n== 置換テキストの作り方 ==")
e = ms[0].replace("logger.info($MSG)")
check("SgNode.replace(text) はメタ変数を展開しない（文字列がそのまま Edit になる）", e.inserted_text == "logger.info($MSG)", repr(e.inserted_text))
check("get_transformed は transform で作った変数用（通常のメタ変数は None）", ms[0].get_transformed("MSG") is None)
naive = root.commit_edits([m.replace("logger.info($MSG)") for m in ms])
check("つまり replace + commit_edits だけでは $MSG が残る（自前展開が必要 = §7.4.7 の想定どおり）", "$MSG" in naive)
out, n = rewrite(PY_SRC, "python", "print($MSG)", "logger.info($MSG)")
check("Python: print($MSG) -> logger.info($MSG)（自前展開 + commit_edits）", n == 2 and out == "x = 1\nlogger.info('a')\nlogger.info(f'b {x}')\nfoo(print(1, 2), 3)\n", repr(out))

TS_SRC = "const a = 1;\nconsole.log('hi', a);\nconsole.log(a);\nprint(a);\nfunction f() { print('x') }\n"
for lang in ("typescript", "tsx", "javascript"):
    out, n = rewrite(TS_SRC, lang, "print($MSG)", "logger.info($MSG)")
    check(f"{lang}: print($MSG) -> logger.info($MSG)", n == 2 and out == "const a = 1;\nconsole.log('hi', a);\nconsole.log(a);\nlogger.info(a);\nfunction f() { logger.info('x') }\n", repr(out))
out, n = rewrite(TS_SRC, "typescript", "console.log($$$ARGS)", "logger.info($$$ARGS)")
check("typescript: console.log($$$ARGS) -> logger.info($$$ARGS)（区切りと空白を保つ）", n == 2 and "logger.info('hi', a);" in out and "logger.info(a);" in out, repr(out))
out, n = rewrite("print()\nprint(1)\n", "python", "print($$$A)", "log($$$A)")
check("$$$A が 0 個一致でも展開できる（空になる）", out == "log()\nlog(1)\n", repr(out))
out, n = rewrite("print(\n    1,\n    2,\n)\n", "python", "print($$$A)", "log($$$A)")
check("複数行の $$$A は末尾の , まで含めて原文のまま（整形はしない）", out == "log(1,\n    2,)\n", repr(out))

print("\n== 複数行・インデント ==")
ML = "def g():\n    if cond:\n        a()\n        b()\n"
out, n = rewrite(ML, "python", "if $C:\n    $$$B", "if not ($C):\n    $$$B")
check("複数行テンプレート + 複数行 $$$B（インデント維持）", out == "def g():\n    if not (cond):\n        a()\n        b()\n", repr(out))
out, n = rewrite(ML, "python", "if $C:\n    $$$B", "with ctx:\n    if $C:\n        $$$B")
check("挿入先が深くなる場合は 2 行目以降をずらす", out == "def g():\n    with ctx:\n        if cond:\n            a()\n            b()\n", repr(out))

print("\n== テンプレート・パターンの検証 ==")
try:
    rewrite(PY_SRC, "python", "print($MSG)", "log($OTHER)")
    check("パターンに無いメタ変数をテンプレートで参照 -> TemplateError", False)
except TemplateError as ex:
    check("パターンに無いメタ変数をテンプレートで参照 -> TemplateError", True, str(ex))
for lang, pat, expect_hint in [("python", "print(", True), ("python", "print($MSG)", False), ("typescript", "console.log(", True), ("typescript", "console.log($$$A)", False), ("python", "def", True)]:
    n = len(search("print(1)\nconsole.log(1)\n", lang, pat) if pat != "print($MSG)" else [1])
    hint = pattern_hint(lang, pat, n)
    check(f"不正パターン {lang} {pat!r}: 0 件 + 警告の手がかり" if expect_hint else f"正常パターン {lang} {pat!r}: 警告なし", (hint is not None) == expect_hint, hint or n)
n = len(search("fn f() { foo(1); }\n", "rust", "foo($M)"))
check("Rust の foo($M) は単独で解析すると ERROR だが正しく一致する（だから ERROR だけでは不正と断定しない）", n == 1 and pattern_hint("rust", "foo($M)", n) is None)
go_src = 'package p\nfunc f() { fmt.Println("a") }\n'
check("Go: 式だけのパターン fmt.Println($M) は一致しない（警告が出る）", search(go_src, "go", "fmt.Println($M)") == [] and pattern_hint("go", "fmt.Println($M)", 0) is not None)
ctx = {"context": "func t() { fmt.Println($M) }", "selector": "call_expression"}
gm = search(go_src, "go", ctx)
check("Go: {context, selector} 形式なら一致し、メタ変数も取れる", [m.text for m in gm] == ['fmt.Println("a")'] and gm[0].single["M"].text == '"a"', [(m.text, m.single) for m in gm])
out, n = rewrite(go_src, "go", ctx, "log.Print($M)")
check("Go: context 形式で書き換え", out == 'package p\nfunc f() { log.Print("a") }\n', repr(out))
c_src = 'int f() { printf("a"); return 0; }\n'
check("C: printf($M) は一致せず、printf($M); と書くと一致する", search(c_src, "c", "printf($M)") == [] and len(search(c_src, "c", "printf($M);")) == 1)
try:
    search("class A { m() { foo(2) } }\n", "javascript", "m() { $$$B }")
    check("単独のノードにならないパターン -> PatternError（RuntimeError cannot get matcher）", False)
except PatternError as ex:
    check("単独のノードにならないパターン -> PatternError（RuntimeError cannot get matcher）", True, str(ex))
raw = SgRoot("print(1)\n", "python").root().find_all(pattern="print(")
check("素の ast-grep は不正パターンを黙って 0 件にする（エラーにならない）", raw == [], len(raw))
try:
    SgRoot("x", "toml")
    check("未対応言語（toml）は例外", False)
except BaseException as ex:  # noqa: BLE001
    check("未対応言語（toml）は PanicException（BaseException の子で Exception ではない）", type(ex).__name__ == "PanicException" and not isinstance(ex, Exception), type(ex).__name__)
try:
    make_root("x", "toml")
    check("make_root は未対応言語を UnsupportedLanguage で弾く", False)
except UnsupportedLanguage:
    check("make_root は未対応言語を UnsupportedLanguage で弾く（toml / xml / rst / sql / markdown_inline は ast-grep に無い）", True)

print("\n== 入れ子・制約・壊れたソース ==")
nest = search("foo(foo(1))\n", "python", "foo($X)")
check("入れ子の一致は外側と内側の両方が返る", [m.text for m in nest] == ["foo(foo(1))", "foo(1)"])
check("outermost() で外側だけにできる", [m.text for m in outermost(nest)] == ["foo(foo(1))"])
raw_root = SgRoot("foo(foo(1))\n", "python").root()
raw_out = raw_root.commit_edits([m.replace("bar") for m in raw_root.find_all(pattern="foo($X)")])
check("重なる Edit を commit_edits に渡しても落ちない（外側が勝つ）が、自前で outermost にするのが安全", raw_out == "bar\n", repr(raw_out))
cs = search("print('a')\nprint(1)\nprint(x)\n", "python", "print($M)", constraints={"M": {"kind": "string"}})
check("constraints（kind）で絞り込める", [m.text for m in cs] == ["print('a')"], [m.text for m in cs])
cs = search("print('a')\nprint(1)\nprint(x)\n", "python", "print($M)", constraints={"M": {"regex": "^[0-9]+$"}})
check("constraints（regex）で絞り込める", [m.text for m in cs] == ["print(1)"], [m.text for m in cs])
bs = search("def f(:\n  print('a')\n", "python", "print($M)")
check("構文エラーを含むソースでも一致を返す", [m.text for m in bs] == ["print('a')"], [m.text for m in bs])
defs = search("def f(a, b):\n    return a\n\nclass A:\n    def m(self): pass\n", "python", "def $F($$$A): $$$B")
check("定義のパターン（def $F($$$A): $$$B）で関数名を取れる", sorted(m.single["F"].text for m in defs) == ["f", "m"])

print("\n== 性能・スレッド（pdb.py 1,987 行） ==")
src = (pathlib.Path(sysconfig.get_paths()["stdlib"]) / "pdb.py").read_text(encoding="utf-8")


def bench(fn, n=30):
    xs = []
    for _ in range(n):
        s = time.perf_counter()
        fn()
        xs.append(time.perf_counter() - s)
    return f"中央値 {statistics.median(xs)*1000:.2f} ms / p95 {sorted(xs)[int(n*.95)-1]*1000:.2f} ms"


big = SgRoot(src, "python").root()
print("- SgRoot(src).root()（解析）:", bench(lambda: SgRoot(src, "python").root()))
print("- find_all('self.$M($$$A)')（183 件）:", bench(lambda: big.find_all(pattern="self.$M($$$A)")))
print("- search()（find_all + 範囲・メタ変数の取り出し）:", bench(lambda: search(src, "python", "self.$M($$$A)"), 10))


def work(_):
    for _ in range(10):
        SgRoot(src, "python").root().find_all(pattern="self.$M($$$A)")


work(0)
t0 = time.perf_counter()
[work(i) for i in range(4)]
ser = time.perf_counter() - t0
with cf.ThreadPoolExecutor(4) as ex:
    list(ex.map(work, range(4)))
    t0 = time.perf_counter()
    list(ex.map(work, range(4)))
    par = time.perf_counter() - t0
print(f"- 4 スレッド: 直列 {ser*1000:.0f} ms / 並列 {par*1000:.0f} ms = {ser/par:.2f} 倍（1 に近い = GIL を解放しない）")
res = []
import threading  # noqa: E402

th = threading.Thread(target=lambda: res.append(len(big.find_all(pattern="print($MSG)"))))
th.start()
th.join()
check("別スレッドで作った SgNode を他スレッドから使える", res == [9], res)

print("\nFAILS =", FAILS)
sys.exit(1 if FAILS else 0)
