"""BUG-0027: 内容だけで決まる計算（URN の整形・解析、トークン見積もり）の使い回し。呼び出しの回数と結果の同一性で確かめる。"""

from __future__ import annotations

import pytest

from lucianke.core import tokens, urn
from lucianke.core.tokens import TokenEstimator


def test_urn_parse_and_format_are_reused_and_round_trip() -> None:
    text = "lk://ws/src/a.py#Foo.bar"
    first = urn.parse(text)
    assert urn.parse(text) is first  # 不変なので同じ物を返してよい
    assert str(first) == text and urn.format(first) == text
    assert urn.parse(urn.format(first)) == first


def test_urn_parse_errors_are_not_remembered() -> None:
    for _ in range(2):
        with pytest.raises(ValueError, match="URN must start with"):
            urn.parse("http://x/y")
        with pytest.raises(ValueError):
            urn.parse(123)  # type: ignore[arg-type]


def test_long_text_is_counted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = TokenEstimator._count

    def counting(text: str) -> int:
        calls.append(len(text))
        return real(text)

    monkeypatch.setattr(TokenEstimator, "_count", staticmethod(counting))
    est = TokenEstimator()
    unit = "def f(a, b):\n    return a + b\n"
    long_text = unit * 200
    other = long_text + "# changed\n"
    expected = real(long_text)
    assert est.count(long_text) == expected
    assert est.count("".join([unit for _ in range(200)])) == expected  # 別の str オブジェクトでも同じ内容
    assert est.count(long_text) == expected
    assert len(calls) == 1
    assert est.count(other) == real(other) and len(calls) == 2  # 内容が違えば数え直す
    short = "x = 1\n"
    est.count(short)
    est.count(short)
    assert len(calls) == 4  # 短い文字列は覚えない（数える方が速い）


def test_token_memo_is_bounded() -> None:
    est = TokenEstimator()
    for i in range(tokens._MEMO_MAX_ENTRIES + 50):
        est.count(f"{i} " * 600)
    assert len(tokens._memo) <= tokens._MEMO_MAX_ENTRIES
