"""トークン数の推定器（仕様書 §5.7、設計書 §7.2.1、ADR-4）。

``TokenEstimator`` は ``o200k_base`` 互換の近似で、文字種別の係数の合計を切り上げる。MCP のパッカー（``mcp/budget.py``）と
ファイル骨格の縮退（``syntax/skeleton.py``）が **同じ見積もり** を使うよう、どの層からも見える ``core`` に置く。
係数 ``TOKENS_PER_CHAR`` はベンチ（``bench/tokens/``）で ``o200k_base`` と比べて較正する。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from fractions import Fraction
from typing import Final

__all__ = ["TOKENS_PER_CHAR", "TokenEstimator"]

#: 1 文字あたりのトークン数（仕様書 §5.7、設計書 §7.2.1）。ベンチで ``o200k_base`` と比べて ±10% に調整する値。
#: 有理数で持ち、切り上げは整数演算で行う（浮動小数の誤差で ``ceil`` がずれるのを避ける）。
TOKENS_PER_CHAR: Final[Mapping[str, Fraction]] = {
    "ascii_word": Fraction(1, 4),  # ASCII 英数字と空白
    "ascii_symbol": Fraction(1, 2),  # ASCII 記号（句読点・演算子など）
    "wide": Fraction(1) / Fraction(3, 2),  # 漢字・かな・全角（1 トークン = 1.5 文字）
    "other": Fraction(1, 2),  # 上記以外（制御文字、ギリシャ・キリル文字、絵文字など）
}

_DEN: Final = math.lcm(*(w.denominator for w in TOKENS_PER_CHAR.values()))
_W_WORD: Final = int(TOKENS_PER_CHAR["ascii_word"] * _DEN)
_W_SYMBOL: Final = int(TOKENS_PER_CHAR["ascii_symbol"] * _DEN)
_W_WIDE: Final = int(TOKENS_PER_CHAR["wide"] * _DEN)
_W_OTHER: Final = int(TOKENS_PER_CHAR["other"] * _DEN)

# 連続した同種の文字を 1 マッチにまとめる（``+``）。findall の要素数が少なく、1 万文字でも 1 ms を大きく下回る。
_RE_ASCII_WORD: Final = re.compile(r"[0-9A-Za-z \t\n\r\f\v]+")
_RE_ASCII_SYMBOL: Final = re.compile(r"[!-/:-@\[-`{-~]+")
_RE_WIDE: Final = re.compile(
    "[　-ヿ"  # CJK の記号・句読点、ひらがな、カタカナ
    "ㇰ-ㇿ"  # カタカナ拡張
    "㐀-䶿一-鿿"  # CJK 統合漢字（拡張 A を含む）
    "가-힯"  # ハングル音節
    "豈-﫿"  # CJK 互換漢字
    "＀-￯"  # 半角・全角形
    "\U00020000-\U0002fa1f]+"  # CJK 統合漢字拡張 B 以降
)


class TokenEstimator:
    """``o200k_base`` 互換の近似（仕様書 §5.7）。文字種別の係数の合計を切り上げる。"""

    def count(self, text: str) -> int:
        if not text:
            return 0
        word = sum(map(len, _RE_ASCII_WORD.findall(text)))
        symbol = sum(map(len, _RE_ASCII_SYMBOL.findall(text)))
        wide = 0 if text.isascii() else sum(map(len, _RE_WIDE.findall(text)))
        other = len(text) - word - symbol - wide
        units = word * _W_WORD + symbol * _W_SYMBOL + wide * _W_WIDE + other * _W_OTHER
        return -(-units // _DEN)
