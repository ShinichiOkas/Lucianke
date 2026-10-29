"""S-2 項目 1・6: tree-sitter 0.26 の基本 API・Query/QueryCursor・増分解析・ERROR/MISSING・非 ASCII の列を実測する。

使い方: uv run python spikes/s02_treesitter/run_api.py
出力: 項目ごとの "[OK] / [NG]" と観測値。NG が 1 つでもあれば終了コード 1。
前提: .venv 同期済み。snippets.py（同じフォルダ）の部品も同時に検証する。
"""

from __future__ import annotations

import gc
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

import tree_sitter as ts  # noqa: E402
import tree_sitter_python as tsp  # noqa: E402
from snippets import LineIndex, ParserPool, error_spans, get_language, run_tags  # noqa: E402

FAILS = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global FAILS
    FAILS += 0 if cond else 1
    print(f"[{'OK' if cond else 'NG'}] {label}" + (f"  -> {detail}" if detail != "" else ""))


PY = get_language("python")
parser = ts.Parser(PY)

print("== 1. 基本 API ==")
src = "def f(x):\n    return x\n\nclass A:\n    def m(self): pass\n".encode()
tree = parser.parse(src)
root = tree.root_node
check("Language(mod.language()) / Parser(language) / parse(bytes) / root_node", root.type == "module", root.type)
p2 = ts.Parser()
p2.language = PY
check("p2.language = Language", p2.parse(src).root_node.type == "module")
for bad, label in (("def f(): pass", "str"),):
    try:
        parser.parse(bad)  # type: ignore[arg-type]
        check(f"parse({label}) は拒否される", False, "通ってしまった")
    except TypeError as e:
        check(f"parse({label}) は TypeError（bytes 必須）", True, str(e)[:60])
check("parse(bytearray) 可", parser.parse(bytearray(src)).root_node.type == "module")
check("parse(memoryview) 可", parser.parse(memoryview(src)).root_node.type == "module")
f = root.children[0]
check("start_byte/end_byte/start_point/end_point の型", isinstance(f.start_point, ts.Point) and f.start_point == (0, 0) and f.end_point == (1, 12), (f.start_byte, f.end_byte, tuple(f.start_point), tuple(f.end_point)))
check("Point は row/column 属性と tuple 展開の両方", f.end_point.row == 1 and f.end_point.column == 12 and tuple(f.end_point) == (1, 12))
check("Node.text は bytes（tree が元のバイト列を保持）", f.text == src[f.start_byte : f.end_byte])
del src
gc.collect()
check("元の bytes 変数を del しても Node.text が読める", f.text.startswith(b"def f"))
src2 = b"x = 1\n"
tr_cb = parser.parse(lambda off, pt: src2[off : off + 4] if off < len(src2) else b"")
check("read_callback 版 parse でも Node.text が読める", tr_cb.root_node.children[0].text == b"x = 1", repr(tr_cb.root_node.children[0].text))
check("Node == は同一ノード、hash 可", root.children[0] == tree.root_node.children[0] and len({root, root}) == 1)
try:
    parser.parse(b"x=1\n", None)  # type: ignore[arg-type]
    check("parse(source, None) は通る", True)
except TypeError as e:
    check("parse(source, None) は TypeError（old_tree に None は渡せない。省略する）", True, str(e)[:60])
check("ParserPool.parse（スレッドローカル）", ParserPool().parse("python", b"x=1\n").root_node.type == "module")

print("\n== Query / QueryCursor ==")
Q = "(function_definition name: (identifier) @name) @definition.function\n(class_definition name: (identifier) @name) @definition.class\n"
q = ts.Query(PY, Q)
cur = ts.QueryCursor(q)
caps = cur.captures(root)
mats = cur.matches(root)
check("captures() -> dict[str, list[Node]]", isinstance(caps, dict) and all(isinstance(v, list) for v in caps.values()), {k: len(v) for k, v in caps.items()})
check("matches() -> list[tuple[int, dict[str, list[Node]]]]", isinstance(mats, list) and isinstance(mats[0], tuple) and isinstance(mats[0][1], dict), [(i, {k: [n.text.decode() for n in v] for k, v in d.items() if k == 'name'}) for i, d in mats])
check("pattern_count / capture_count / capture_name", (q.pattern_count, q.capture_count, q.capture_name(0)) == (2, 3, "name"), (q.pattern_count, q.capture_count, q.capture_name(0)))
tags = run_tags("python", Q, tree)
check("snippets.run_tags（matches 経由で name を正しく対応づけ）", sorted((t.kind, t.name) for t in tags) == [("definition.class", "A"), ("definition.function", "f"), ("definition.function", "m")], [(t.kind, t.name) for t in tags])
# 述語
qp = ts.Query(PY, '((function_definition name: (identifier) @n) (#match? @n "^_"))')
check("#match? 述語（組込み）", len(ts.QueryCursor(qp).matches(ts.Parser(PY).parse(b"def _a(): pass\ndef b(): pass\n").root_node)) == 1)
qe = ts.Query(PY, '((identifier) @n (#eq? @n "b"))')
check("#eq? 述語（組込み）", len(ts.QueryCursor(qe).captures(ts.Parser(PY).parse(b"a=1\nb=2\n").root_node).get("n", [])) == 1)
qa = ts.Query(PY, '((identifier) @n (#any-of? @n "a" "b"))')
check("#any-of? 述語（組込み）", len(ts.QueryCursor(qa).captures(ts.Parser(PY).parse(b"a=1\nb=2\nc=3\n").root_node).get("n", [])) == 2)
try:
    ts.Query(PY, "(no_such_node) @x")
    check("存在しないノード型は QueryError", False)
except ts.QueryError as e:
    check("存在しないノード型は QueryError（ValueError のサブクラス）", isinstance(e, ValueError), str(e)[:70])
cur2 = ts.QueryCursor(q)
cur2.set_byte_range(0, 12)
check("QueryCursor.set_byte_range（範囲に交わる一致のみ）", len(cur2.matches(root)) == 1)
check("match_limit 既定 / did_exceed_match_limit", (ts.QueryCursor(q).match_limit, ts.QueryCursor(q).did_exceed_match_limit) == (0xFFFFFFFF, False))

print("\n== ERROR / MISSING ==")
bad_py = b"def f(:\n    x = \nclass\n"
t = ts.Parser(PY).parse(bad_py)
sp = error_spans(t.root_node)
check("Python 構文エラー: root.has_error と ERROR 列挙", t.root_node.has_error and len(sp) > 0, [(s.kind, s.is_missing, s.start_point, s.end_point) for s in sp])
js = get_language("javascript")
t = ts.Parser(js).parse(b"foo(1, 2\nvar x = 1;\n")
sp = error_spans(t.root_node)
miss = [s for s in sp if s.is_missing]
check("JS: MISSING ノード（欠けた ')'）を検出、幅 0", len(miss) >= 1 and miss[0].start_byte == miss[0].end_byte, [(s.kind, s.is_missing, s.start_point) for s in sp])
check("正常な木は error_spans が空・has_error False", not error_spans(ts.Parser(PY).parse(b"x = 1\n").root_node) and not ts.Parser(PY).parse(b"x = 1\n").root_node.has_error)
t = ts.Parser(PY).parse(b"x = 1\xff\ny = 2\n")
check("不正な UTF-8 バイトでも解析は落ちない", t.root_node is not None, ("has_error=%s" % t.root_node.has_error, [(s.kind, s.start_point) for s in error_spans(t.root_node)]))
t = ts.Parser(PY).parse(b"\xef\xbb\xbfx = 1\n")
check("UTF-8 BOM 付き Python", True, ("has_error=%s" % t.root_node.has_error, f"first child={[(c.type, tuple(c.start_point)) for c in t.root_node.children]}"))
t = ts.Parser(PY).parse(b"x = 1\r\ny = 2\r\n")
n0, n1 = t.root_node.children
check("CRLF: 行は \\n だけで数える。\\r は列に含まれない（文の終端は \\r の手前）", n0.end_point == (0, 5) and n1.start_point == (1, 0), (tuple(n0.end_point), tuple(n1.start_point), f"root.end_point={tuple(t.root_node.end_point)}"))
t = ts.Parser(PY).parse(b"x = 1\ry = 2\r")
check("単独 \\r は改行とみなされない（LSP とは不一致）", t.root_node.end_point[0] == 0, (f"root.end_point={tuple(t.root_node.end_point)}", [c.type for c in t.root_node.children], t.root_node.has_error))

print("\n== 6. 非 ASCII の列 ==")
line = "s = 'あ😀' + x  # 全角"
src_u = (line + "\nz = 1\n").encode("utf-8")
tu = ts.Parser(PY).parse(src_u)
li = LineIndex(src_u)
plus = next(n for n in tu.root_node.children[0].children[0].children if n.type == "binary_operator")  # 'あ😀' + x
x_node = plus.child_by_field_name("right")
b_col = x_node.start_point.column
cp_col = line.index("x", line.index("+"))
u16_col = len(line[:cp_col].encode("utf-16-le")) // 2
check("start_point.column はバイト数（コードポイント数ではない）", b_col == len(line[:cp_col].encode()) and b_col != cp_col, f"byte={b_col} codepoint={cp_col} utf16={u16_col}")
check("LineIndex: byte->cp / byte->utf16", li.byte_col_to_cp(0, b_col) == cp_col and li.byte_col_to_utf16(0, b_col) == u16_col, (li.byte_col_to_cp(0, b_col), li.byte_col_to_utf16(0, b_col)))
check("LineIndex: cp->byte / utf16->byte の逆変換", li.cp_col_to_byte(0, cp_col) == b_col and li.utf16_col_to_byte(0, u16_col) == b_col)
check("LineIndex: offset_to_point が start_point と一致", li.offset_to_point(x_node.start_byte) == tuple(x_node.start_point))
check("絵文字は UTF-8 で 4 バイト・UTF-16 で 2 単位・コードポイント 1", (len("😀".encode()), len("😀".encode("utf-16-le")) // 2, len("😀")) == (4, 2, 1))
check("行末コメントの全角も end_point の列はバイト", tu.root_node.end_point == (2, 0) and tu.root_node.children[0].end_point.column == len("s = 'あ😀' + x".encode()), tuple(tu.root_node.children[0].end_point))

print("\n== 増分解析 ==")
base = ("def f(x):\n    a = 1\n    return x\n\n" * 50).encode()
t0 = ts.Parser(PY).parse(base)
row = 41  # "    a = 1" の行
line_start = LineIndex(base).starts[row]
old = b"    a = 1"
new = b"    a = 12345"
assert base[line_start : line_start + len(old)] == old
new_src = base[:line_start] + new + base[line_start + len(old) :]
t1 = t0.copy()
t1.edit(start_byte=line_start, old_end_byte=line_start + len(old), new_end_byte=line_start + len(new),
        start_point=(row, 0), old_end_point=(row, len(old)), new_end_point=(row, len(new)))
inc = ts.Parser(PY).parse(new_src, t1)
full = ts.Parser(PY).parse(new_src)
check("増分解析の結果が全解析と同一（S 式）", str(inc.root_node) == str(full.root_node))
check("changed_ranges が編集行に限られる", len(t1.changed_ranges(inc)) >= 1 and all(abs(r.start_point.row - row) <= 1 for r in t1.changed_ranges(inc)), [(tuple(r.start_point), tuple(r.end_point)) for r in t1.changed_ranges(inc)])
check("edit は呼んだ木を直接書き換える（copy() してから edit すれば元の木は無傷）", t0.root_node.has_changes is False and t1.root_node.has_changes is True, (t0.root_node.has_changes, t1.root_node.has_changes))

print("\n== 結果 ==")
print("FAILS =", FAILS)
sys.exit(1 if FAILS else 0)
