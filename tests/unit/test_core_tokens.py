"""core.tokens（トークン数の推定器。mcp のパッカーと syntax の骨格が共有する）のテスト。

係数の精度は ``test_mcp_budget.py`` とベンチ（``bench/tokens/``）が受け持つ。ここは代表例と再公開を確かめる。
"""

from __future__ import annotations

from fractions import Fraction

from lucianke.core import tokens
from lucianke.core.tokens import TOKENS_PER_CHAR, TokenEstimator
from lucianke.mcp import budget
from lucianke.syntax import skeleton

EST = TokenEstimator()


def test_representative_counts() -> None:
    assert EST.count("") == 0
    assert EST.count("abcd") == 1  # ASCII 英数字は 1/4
    assert EST.count("abcde") == 2  # 切り上げ
    assert EST.count("!!") == 1  # ASCII 記号は 1/2
    assert EST.count("あい") == 2  # 全角は 1/1.5 → 1.33 を切り上げ
    assert EST.count("あいう") == 2  # 3 文字 = ちょうど 2 トークン
    assert EST.count("αβ") == 1  # その他は 1/2


def test_coefficients_are_exact_fractions() -> None:
    assert set(TOKENS_PER_CHAR) == {"ascii_word", "ascii_symbol", "wide", "other"}
    assert all(isinstance(v, Fraction) for v in TOKENS_PER_CHAR.values())
    assert TOKENS_PER_CHAR["wide"] == Fraction(2, 3)


def test_reexported_from_budget_and_skeleton() -> None:
    assert budget.TokenEstimator is tokens.TokenEstimator
    assert budget.TOKENS_PER_CHAR is tokens.TOKENS_PER_CHAR
    sample = "def f(x):\n    return x + 1  # 戻り値\n"
    assert skeleton.default_estimate(sample) == EST.count(sample)
    assert budget.Packer().estimator.count(sample) == EST.count(sample)
