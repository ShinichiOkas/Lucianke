"""``core/mro.py``（C3 線形化。BUG-0075）の単体テスト。"""

from __future__ import annotations

from lucianke.core.mro import MAX_MRO_DEPTH, TRANSPARENT_EXTERNAL_BASES, c3_linearize


def _lin(graph: dict[str, list[str]], start: str, **kw: int) -> list[str] | None:
    return c3_linearize(start, lambda c: graph.get(c, []), **kw)


def test_single_chain_and_no_bases() -> None:
    assert _lin({"C": ["B"], "B": ["A"]}, "C") == ["C", "B", "A"]
    assert _lin({}, "A") == ["A"]


def test_diamond_follows_c3_not_depth_first() -> None:
    """``D(B, C)``・``B(A)``・``C(A)``: C3 は D, B, C, A（深さ優先なら D, B, A, C で ``C`` の上書きを見落とす）。"""
    assert _lin({"D": ["B", "C"], "B": ["A"], "C": ["A"]}, "D") == ["D", "B", "C", "A"]


def test_python_documented_example() -> None:
    """Python の MRO の説明に出てくる例（O は object）。"""
    graph = {
        "A": ["O"], "B": ["O"], "C": ["O"], "D": ["O"], "E": ["O"],
        "K1": ["A", "B", "C"], "K2": ["D", "B", "E"], "K3": ["D", "A"],
        "Z": ["K1", "K2", "K3"],
    }  # fmt: skip
    assert _lin(graph, "Z") == ["Z", "K1", "K2", "K3", "D", "A", "B", "C", "E", "O"]


def test_inconsistent_order_and_cycles_give_none() -> None:
    assert _lin({"X": ["A", "B"], "Y": ["B", "A"], "Z": ["X", "Y"]}, "Z") is None
    assert _lin({"A": ["B"], "B": ["A"]}, "A") is None


def test_repeated_and_self_bases_are_ignored() -> None:
    assert _lin({"C": ["B", "B", "C"], "B": []}, "C") == ["C", "B"]


def test_depth_limit() -> None:
    chain = {f"c{i}": [f"c{i + 1}"] for i in range(MAX_MRO_DEPTH + 5)}
    assert _lin(chain, "c0") is None
    assert _lin(chain, "c0", max_depth=100) is not None


def test_transparent_bases_are_urns_of_bases_without_user_methods() -> None:
    assert "lk://_ext/abc#ABC" in TRANSPARENT_EXTERNAL_BASES
    assert "lk://_ext/builtins#object" in TRANSPARENT_EXTERNAL_BASES
    assert "lk://_ext/threading#Thread" not in TRANSPARENT_EXTERNAL_BASES
