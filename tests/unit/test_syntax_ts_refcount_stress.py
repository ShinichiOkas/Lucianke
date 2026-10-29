"""構文木のノードを保持したまま、別の解析（``check_fragment``）と木の走査を繰り返しても落ちないこと（BUG-0051）。

BUG-0003 は py-tree-sitter 0.26.0 の ``Point.row`` / ``.column`` が int の参照を余計に手放す不具合だった。BUG-0051 は
「node を保持したまま ``check_fragment`` を呼ぶとアクセス違反が 1 回だけ起きた」という報告で、同じ型の不具合を疑って
木の API を 1 つずつ数千回ずつ当てた。落ちる組み合わせは **``read_callback`` で解析した木の ``Node.text``** だった
（``bytes`` で解析した木では落ちない。``start_point`` / ``type`` / ``parent`` などは単独では落ちない）。
``Node.text`` はコールバックを呼び直し、その戻り値の ``bytes`` を **返す前に解放して使っている**（解放後の読み取り）。
大きな塊（16 KiB）は解放で OS に返ることがあり、木の走査で確保・解放が続くと、あるとき落ちる。
Lucianke の解析は常に ``read_callback``（``syntax.parsers._parse_with_deadline``）なので、戻り値を木の寿命の間
つなぎとめて避ける。

クラッシュ（Windows のアクセス違反）を検出するため、実際の処理は **子プロセス**（``-X faulthandler`` つき）で回し、
親は終了コードと標準エラーだけを見る。子プロセスは、このファイルを ``--worker`` つきで実行したもの。
回数は既定で少なめ（直す前は 10 回前後で落ちた）。``LUCIANKE_STRESS=1`` で 3,000 回にする。
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
_FILES = ("src/lucianke/edit/syntax_check.py", "src/lucianke/syntax/skeleton.py", "src/lucianke/syntax/symbols.py")
_ITERATIONS = 3000 if os.environ.get("LUCIANKE_STRESS") else 60

_FRAGMENTS = (
    ("python", "def f(x):\n    return x + 1\n", "none"),
    ("python", "def f(x:\n    return x +\n", "none"),
    ("python", "def g(self):\n    return 1\n", "class_body"),
    ("typescript", "foo(): number { return 1 }", "class_body"),
    ("rust", "fn a() -> i32 { 1 }", "module"),
    ("rust", "fn a( -> i32 { 1 ", "module"),
)


def _collect(root):  # type: ignore[no-untyped-def]
    out, stack = [], [root]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


def _read_attributes(nodes) -> int:  # type: ignore[no-untyped-def]
    """保持した node の属性を読む。行・列は ``point[0]`` / ``point[1]``（BUG-0003）。"""
    total = 0
    for n in nodes:
        sp, ep = n.start_point, n.end_point
        total += sp[0] + sp[1] + ep[0] + ep[1]
        total += len(n.text or b"")
        total += len(n.children) + len(n.named_children) + len(n.type)
        parent = n.parent
        if parent is not None:
            total += parent.start_byte
        sibling = n.next_sibling
        if sibling is not None:
            total += sibling.end_byte
    return total


def _worker(iterations: int, collect_garbage: bool) -> None:
    sys.path.insert(0, str(_SRC))
    from lucianke.edit.syntax_check import check_fragment
    from lucianke.syntax.parsers import parse_source

    parsed = [parse_source((_ROOT / rel).read_bytes(), "python", "")[0] for rel in _FILES]
    for i in range(iterations):
        held = _collect(parsed[i % len(parsed)].tree.root_node)  # 木から取り直した node を保持する
        sample = held[:: max(1, len(held) // 300)]
        for language, code, wrap in _FRAGMENTS:
            check_fragment(language, code, wrap=wrap)  # type: ignore[arg-type]
            _read_attributes(sample)
        again = parse_source(parsed[i % len(parsed)].source, "python", "")[0]  # 同じ Parser で別の木
        _read_attributes(_collect(again.tree.root_node)[:200])
        _read_attributes(sample)
        if collect_garbage and i % 5 == 0:
            gc.collect()
    print("DONE", flush=True)


@pytest.mark.parametrize("collect_garbage", [False, True], ids=["nogc", "gc"])
def test_holding_nodes_while_parsing_and_check_fragment_does_not_crash(collect_garbage: bool) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "faulthandler",
            str(Path(__file__).resolve()),
            "--worker",
            str(_ITERATIONS),
            "gc" if collect_garbage else "nogc",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=600,
    )
    assert proc.returncode == 0 and "DONE" in proc.stdout, (
        f"exit={proc.returncode}\n--- stdout ---\n{proc.stdout[-500:]}\n--- stderr ---\n{proc.stderr[-1500:]}"
    )


if __name__ == "__main__":
    if "--worker" in sys.argv:
        _worker(int(sys.argv[sys.argv.index("--worker") + 1]), sys.argv[-1] == "gc")
