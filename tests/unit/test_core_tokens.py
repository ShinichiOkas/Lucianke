"""core.tokens（トークン数の推定器。mcp のパッカーと syntax の骨格が共有する）のテスト。

式は S-12 の ``lean_wide``（文字種別と連の数の係数）。``o200k_base`` との誤差 ±10% は
``test_core_tokens_calibration.py`` が受け持つ。ここは各項の計数と切り上げ、再公開を確かめる。
"""

from __future__ import annotations

import json
import random
from fractions import Fraction

from lucianke.core import tokens
from lucianke.core.tokens import TOKEN_COEFFICIENTS, TOKENS_PER_CHAR, TokenEstimator
from lucianke.mcp import budget
from lucianke.syntax import skeleton

EST = TokenEstimator()


def test_representative_counts() -> None:
    assert EST.count("") == 0
    assert EST.count("abcd") == 1  # 英字の連 1 個 = 0.85
    assert EST.count("abcdefghij") == 1  # 連の長さに依らない
    assert EST.count("hello world") == 2  # 連 2 個 = 1.7。単一の空白は 0
    assert EST.count("1234") == 4  # 数字は 1 文字 0.9 = 3.6
    assert EST.count("!!") == 1  # 記号の連 1 個 = 0.87
    assert EST.count("a!b!") == 4  # 英字の連 2 + 記号の連 2 = 3.44
    assert EST.count("    x") == 2  # インデント（2 個以上の空白）0.92 + 英字 0.85 = 1.77
    assert EST.count("\n") == 1  # 改行 1 文字 = 0.6
    assert EST.count("\r\n\r\n") == 3  # 4 文字 = 2.4
    assert EST.count("あい") == 2  # 全角 1 文字 0.95 → 1.9
    assert EST.count("あいう") == 3  # 2.85
    assert EST.count("𠀀") == 1  # 拡張 B の漢字も全角の係数
    assert EST.count("αβ") == 1  # その他は 1 文字 0.46 → 0.92
    assert EST.count("\x00") == 1  # ASCII の制御文字（タブと改行を除く）もその他


def test_blob_is_counted_by_chars_and_removed_from_the_body() -> None:
    assert EST.count("A" * 39) == 1  # 39 文字は塊ではなく英字の連 1 個
    assert EST.count("A" * 40) == 26  # 塊 40 文字 × 0.63 = 25.2。本文に英字の連は残らない
    assert EST.count("x " + "A" * 40) == 27  # 0.85 + 25.2 = 26.05
    assert EST.count("0123456789abcdef" * 4) == 41  # SHA-256 風の hex 64 文字 = 40.32


def test_rounding_is_exact_integer_arithmetic() -> None:
    twenty_words = " ".join(
        "abcdefghijklmnopqrst"
    )  # 英字の連 20 個 = ちょうど 17.0（浮動小数だと 18 になりうる）
    assert EST.count(twenty_words) == 17
    assert EST.count(twenty_words + " u") == 18  # 21 個 = 17.85


def test_coefficients_are_exact_fractions() -> None:
    assert set(TOKEN_COEFFICIENTS) == {
        "letters_runs",
        "digits_chars",
        "symbol_runs",
        "space_runs_multi",
        "nl_chars",
        "wide",
        "other",
        "blob_chars",
    }
    assert all(isinstance(v, Fraction) for v in TOKEN_COEFFICIENTS.values())
    assert TOKEN_COEFFICIENTS["wide"] == Fraction(19, 20)
    assert TOKENS_PER_CHAR is TOKEN_COEFFICIENTS  # 旧名（budget.py と bench/tokens が使う）


def test_reexported_from_budget_and_skeleton() -> None:
    assert budget.TokenEstimator is tokens.TokenEstimator
    assert budget.TOKENS_PER_CHAR is tokens.TOKENS_PER_CHAR
    sample = "def f(x):\n    return x + 1  # 戻り値\n"
    assert skeleton.default_estimate(sample) == EST.count(sample)
    assert budget.Packer().estimator.count(sample) == EST.count(sample)


def test_units_add_up_over_lines_and_json_arrays() -> None:
    """``units`` は切り上げ前の値で、行の列は行ごとの和と改行の分、詰めた JSON の配列は要素ごとの和から記号の連 1 つずつを
    引いた値になる（骨格の縮退段の見積もりがこれに頼る。BUG-0078）。"""
    rng = random.Random(78)
    alphabet = "abcXYZ019 _-:,()[]{}\"'=+/@#  \tあ漢αβ"
    for _ in range(200):
        lines = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 50))) + ("A" * rng.choice([0, 0, 45]))
            for _ in range(rng.randint(1, 8))
        ]
        text = "\n".join(lines)
        units = sum(map(TokenEstimator.units, lines)) + TokenEstimator.NEWLINE_UNITS * (len(lines) - 1)
        assert TokenEstimator.units(text) == units
        assert EST.count(text) == -(-units // TokenEstimator.UNIT_DEN)

    def dumps(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    for _ in range(100):
        items = [
            {
                "name": "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 20))),
                "n": rng.randint(0, 999),
            }
            for _ in range(rng.randint(0, 6))
        ]
        whole = TokenEstimator.units(dumps({"a": 1, "items": items, "z": "end"}))
        empty = TokenEstimator.units(dumps({"a": 1, "items": [], "z": "end"}))
        parts = sum(TokenEstimator.units(dumps(i)) - TokenEstimator.SYMBOL_RUN_UNITS for i in items)
        assert whole == empty + parts
