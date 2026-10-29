"""syntax.skeleton を本物のファイル（``src/`` の全 ``.py``）に当てるテスト（BUG-0003）。

``parse_source`` → ``symbols.extract`` → ``build_file_record`` が、例外もクラッシュも起こさず、出力の各行が
``tests/contract/compact_grammar.py`` の ``validate`` に通ること。

クラッシュ（プロセスごと落ちる。Windows のアクセス違反など）を検出するため、実際の処理は **子プロセス**
（``-X faulthandler`` つき）で回し、親は終了コードと標準出力だけを見る。子プロセスは、このファイルを
``--worker`` つきで実行したもの。

もうひとつ、``src/`` で ``Node.start_point.row`` / ``.column`` を **属性で読んでいない** ことを検査する。
py-tree-sitter 0.26.0（実測: Windows / cp312）の ``Point.row`` / ``Point.column`` は、タプルが持つ int への参照を
1 つ余計に手放す。値が 256 を超える（小さい int の特別扱いに入らない）と、int が早く解放されてヒープを壊し、
あとの木の走査（``_collect_raises`` など）でアクセス違反になる。``point[0]`` / ``point[1]`` は安全。

さらに ``src/`` で Tree-sitter のノードの ``.text`` を **使っていない** ことを検査する（BUG-0020・BUG-0051）。Lucianke の解析は常に
``read_callback`` を使い、py-tree-sitter 0.26.0 の ``Node.text`` はそのたびに読み取り関数を呼び直し（解析の期限の後に呼ぶと空回りして
戻らない）、返された ``bytes`` を解放してから読む（アクセス違反）。ノードの綴りは ``source[node.start_byte : node.end_byte]`` で切り出す。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
_GRAMMAR_PATH = _ROOT / "tests" / "contract" / "compact_grammar.py"


def _worker() -> None:
    """子プロセスの本体: ``src/`` の全 ``.py`` を 1 ファイルずつ処理し、結果を 1 行の json で標準出力へ出す。"""
    from lucianke.syntax.parsers import parse_source
    from lucianke.syntax.skeleton import build_file_record
    from lucianke.syntax.support import SUPPORT
    from lucianke.syntax.symbols import extract, load_query_source
    from lucianke.syntax.types import SkeletonOptions

    option_sets = {
        "default": SkeletonOptions(),
        "urns-all": SkeletonOptions(include_urns="all"),
        "public-only": SkeletonOptions(include_private=False, include_doc_summary=False),
    }
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        print(json.dumps({"start": rel}), flush=True)  # 落ちたとき、どのファイルかを親が分かるように
        data = path.read_bytes()
        parsed, _ = parse_source(data, "python", hashlib.sha256(data).hexdigest())
        symbols = extract(
            parsed,
            language_id="python",
            language=SUPPORT["python"].grammar(),
            query_source=load_query_source("python"),
            workspace_id="app",
            rel=rel,  # type: ignore[arg-type]
        )
        out: dict[str, list[str]] = {}
        for name, opts in option_sets.items():
            rec = build_file_record(parsed, symbols, rel=rel, opts=opts)
            out[name] = list(rec.lines)
            if name == "default" and rec.shrink is not None:
                for budget in (400, 120, 40):  # 縮退版も組み立てる（入らなければ None）
                    shrunk = rec.shrink(budget)
                    if shrunk is not None:
                        out[f"shrink-{budget}"] = list(shrunk.lines)
        print(json.dumps({"rel": rel, "symbols": len(symbols), "variants": out}), flush=True)


def _load_grammar():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("lucianke_test_compact_grammar_realfiles", _GRAMMAR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skeleton_of_every_src_py_file_neither_raises_nor_crashes() -> None:
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", str(Path(__file__).resolve()), "--worker"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**__import__("os").environ, "PYTHONUTF8": "1"},
        timeout=300,
    )
    started: list[str] = []
    results: list[dict] = []  # type: ignore[type-arg]
    for line in proc.stdout.splitlines():
        row = json.loads(line)
        if "start" in row:
            started.append(row["start"])
        else:
            results.append(row)
    last = started[-1] if started else "(none)"
    assert proc.returncode == 0, (
        f"worker exited {proc.returncode} while processing {last}\n--- stderr ---\n{proc.stderr[-3000:]}"
    )
    assert proc.stderr.strip() == "", proc.stderr[-3000:]
    assert len(results) == len(started) > 50  # src/ には 50 本を超える .py がある

    grammar = _load_grammar()
    bad: list[str] = []
    for row in results:
        for variant, lines in row["variants"].items():
            for violation in grammar.validate("\n".join(lines), require_header=False):
                bad.append(f"{row['rel']} [{variant}]: {violation}")
    assert bad == [], "\n".join(bad[:30])


_POINT_ATTR = re.compile(r"\b(?:start|end)_point\.(?:row|column)\b")


def test_src_never_reads_point_row_or_column_attributes() -> None:
    offenders = [
        f"{path.relative_to(_ROOT).as_posix()}:{no}: {line.strip()}"
        for path in sorted(_SRC.rglob("*.py"))
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _POINT_ATTR.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == [], (
        "Point.row / Point.column の属性は使わない（py-tree-sitter 0.26.0 が int の参照を余計に手放す）。"
        "point[0] / point[1] を使う:\n" + "\n".join(offenders)
    )


# --- Node.text の禁止（BUG-0020・BUG-0051） -----------------------------------------------------

# Tree-sitter のノードだけが持つ名前。これを 1 つでも参照するファイルは「ノードを扱うファイル」として ``.text`` を調べる。
_NODE_API_ATTRS = frozenset(
    {
        "start_byte",
        "end_byte",
        "child_by_field_name",
        "named_children",
        "named_child_count",
        "root_node",
        "descendant_for_byte_range",
        "next_sibling",
        "prev_sibling",
        "start_point",
        "end_point",
    }
)
# ノードを扱うファイルの中にある、ノードではない ``.text``（(src からの相対パス, 受け手の名前)）。増やすときは受け手がノードでないことを確かめる。
_NOT_A_NODE = frozenset(
    {
        ("lucianke/tools/t04_resolve_symbols.py", "lift"),  # 持ち上げた型の文字列（_t04_exact の型）
        ("lucianke/tools/t05_get_symbol_bodies.py", "match"),  # 仮想文書で見つけた塊（文字列を持つ自前の型）
    }
)


def _node_text_offenders(source: str, rel: str) -> list[str]:
    """``source``（``src/`` からの相対パスが ``rel`` のファイル）の、ノードの ``.text`` 参照らしき行。

    ノードを扱うファイル（ノードだけの名前を参照している）で、``x.text``（読み取り。呼び出しではない）のうち、``self.text`` と
    ``_NOT_A_NODE`` を除くもの。``getattr(x, "text")`` はファイルを問わず数える。
    """
    tree = ast.parse(source)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    handles_nodes = bool(attrs & _NODE_API_ATTRS)
    out: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == "text" and isinstance(n.ctx, ast.Load) and id(n) not in called:
            recv = n.value.id if isinstance(n.value, ast.Name) else None
            if handles_nodes and recv != "self" and (rel, recv) not in _NOT_A_NODE:
                out.append(f"{rel}:{n.lineno}: {ast.unparse(n)}")
        elif (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "getattr"
            and len(n.args) >= 2
            and isinstance(n.args[1], ast.Constant)
            and n.args[1].value == "text"
        ):
            out.append(f"{rel}:{n.lineno}: {ast.unparse(n)}")
    return out


def test_node_text_detector_flags_node_text_and_spares_look_alikes() -> None:
    node_file = """def f(node, src):
    return node.start_byte, node.text, src[node.start_byte:node.end_byte]
"""
    assert _node_text_offenders(node_file, "x.py") == ["x.py:2: node.text"]
    assert (
        _node_text_offenders(
            """def f(node):
    return node.child_by_field_name('a').text
""",
            "x.py",
        )
        != []
    )
    assert (
        _node_text_offenders(
            """def f(n):
    return n.next_sibling and getattr(n, 'text')
""",
            "x.py",
        )
        != []
    )
    # ノードを扱わないファイルの ``.text``、``self.text``、``obj.text(...)``（呼び出し）、書き込みは対象外
    assert (
        _node_text_offenders(
            """def f(tok):
    return tok.text
""",
            "x.py",
        )
        == []
    )
    assert (
        _node_text_offenders(
            """class A:
    def f(self, n):
        self.text = n.start_byte
        return self.text
""",
            "x.py",
        )
        == []
    )
    assert (
        _node_text_offenders(
            """def f(c, n):
    return c.text(n.start_byte)
""",
            "x.py",
        )
        == []
    )


def test_src_never_reads_node_text() -> None:
    offenders = [
        o
        for path in sorted(_SRC.rglob("*.py"))
        for o in _node_text_offenders(path.read_text(encoding="utf-8"), path.relative_to(_SRC).as_posix())
    ]
    assert offenders == [], (
        "Node.text は使わない（read_callback で解析した木では読み取り関数を呼び直し、解放済みの領域を読みうる。BUG-0020・BUG-0051）。"
        "source[node.start_byte : node.end_byte] で切り出す。ノードでない ``.text`` なら _NOT_A_NODE に足す:\n"
        + "\n".join(offenders)
    )


if __name__ == "__main__":
    if "--worker" in sys.argv:
        _worker()
