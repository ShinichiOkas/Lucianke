"""トークン数の推定器（仕様書 §5.7、設計書 §7.2.1、ADR-4）。

``TokenEstimator`` は ``o200k_base`` 互換の近似で、文字種別と連（連続した同種の文字）の数に係数をかけて合計し、切り上げる。
MCP のパッカー（``mcp/budget.py``）とファイル骨格の縮退（``syntax/skeleton.py``）が **同じ見積もり** を使うよう、
どの層からも見える ``core`` に置く。

式は S-12（``docs/spikes/S-12.md``）の較正で選んだ ``lean_wide``（8 項）。ASCII の語・記号・インデントは文字数ではなく
**連の数** でトークンになり、日本語は 1.5 文字で 1 トークンより重い（約 0.95 トークン/文字）。旧式（文字種 4 分類）は
日本語で −16%、Compact で −11%、base64 で −43〜−63% の過小だった（過小は「実際の応答が予算を超える」向きで危ない）。
係数は ``bench/tokens/calibrate.py`` で ``o200k_base`` と比べて較正し、回帰テスト
``tests/unit/test_core_tokens_calibration.py`` が種類ごとの合計誤差 ±10% を守る。
"""

from __future__ import annotations

import math
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from fractions import Fraction
from typing import Final

__all__ = ["TOKEN_COEFFICIENTS", "TOKENS_PER_CHAR", "TokenEstimator"]

#: 項ごとの係数（単位あたりのトークン数。S-12 の ``lean_wide``）。単位は項による: ``letters_runs`` ・``symbol_runs`` ・
#: ``space_runs_multi`` は連の個数、ほかは文字数。有理数で持ち、切り上げは整数演算で行う（浮動小数の誤差で切り上げが
#: ずれるのを避ける）。
TOKEN_COEFFICIENTS: Final[Mapping[str, Fraction]] = {
    "letters_runs": Fraction(85, 100),  # ``[A-Za-z]+`` の個数（英単語・識別子の断片）
    "digits_chars": Fraction(90, 100),  # ``[0-9]`` の文字数
    "symbol_runs": Fraction(87, 100),  # ASCII 記号の連の個数
    "space_runs_multi": Fraction(92, 100),  # ``[ \t]{2,}`` の個数（インデント。単一の空白は語に吸収されて 0）
    "nl_chars": Fraction(60, 100),  # ``\r`` と ``\n`` の文字数
    "wide": Fraction(95, 100),  # 仮名・漢字・全角記号の文字数
    "other": Fraction(46, 100),  # 上記のどれでもない文字（ASCII 外の記号・制御文字・ハングルなど）の文字数
    "blob_chars": Fraction(
        63, 100
    ),  # ``[A-Za-z0-9+/=]{40,}`` の文字数（base64・hex。先に数えて本文から除く）
}
#: 旧名（1 文字あたりの係数だった頃の名前）。``mcp/budget.py`` と ``bench/tokens`` が使うので残す。
TOKENS_PER_CHAR: Final = TOKEN_COEFFICIENTS

_DEN: Final = math.lcm(*(w.denominator for w in TOKEN_COEFFICIENTS.values()))
_W: Final = {name: int(w * _DEN) for name, w in TOKEN_COEFFICIENTS.items()}
_W_LETTERS: Final = _W["letters_runs"]
_W_DIGITS: Final = _W["digits_chars"]
_W_SYMBOL: Final = _W["symbol_runs"]
_W_SPACE: Final = _W["space_runs_multi"]
_W_NL: Final = _W["nl_chars"]
_W_WIDE: Final = _W["wide"]
_W_OTHER: Final = _W["other"]
_W_BLOB: Final = _W["blob_chars"]

# 仮名・漢字・全角記号（U+3000–30FF、31F0–31FF、3400–4DBF、4E00–9FFF、F900–FAFF、FF00–FFEF、20000–2FA1F）
_WIDE_RANGES: Final = (
    "\u3000-\u30ff"  # CJK の記号・句読点、ひらがな、カタカナ
    "\u31f0-\u31ff"  # カタカナ拡張
    "\u3400-\u4dbf\u4e00-\u9fff"  # CJK 統合漢字（拡張 A を含む）
    "\uf900-\ufaff"  # CJK 互換漢字
    "\uff00-\uffef"  # 半角・全角形
    "\U00020000-\U0002fa1f"  # CJK 統合漢字拡張 B 以降
)

# 各項は ``findall`` の要素数（連の個数）か、要素の長さの合計（文字数）で数える。Python の文字ごとのループは使わない。
_RE_BLOB: Final = re.compile(r"[A-Za-z0-9+/=]{40,}")
_RE_LETTERS: Final = re.compile(r"[A-Za-z]+")
_RE_DIGITS: Final = re.compile(r"[0-9]+")
_RE_SYMBOL: Final = re.compile(r"[!-/:-@\[-`{-~]+")
_RE_SPACE_MULTI: Final = re.compile(r"[ \t]{2,}")
_RE_WIDE: Final = re.compile(f"[{_WIDE_RANGES}]+")
# ``other`` は「タブ・改行・ASCII 印字可能文字・上記の全角類」のどれでもない文字（ASCII の制御文字を含む）
_RE_OTHER: Final = re.compile(f"[^\t\r\n -~{_WIDE_RANGES}]+")


# 長い文字列の見積もりは 60 KB で 8〜14 ms かかり、1 回の呼び出しの中で同じ（か、前回の呼び出しと同じ）本文を何度も数える
# （パッカーの詰め直し・``used_tokens_est`` の数え直し・縮退段の判定。BUG-0027）。結果は文字列だけで決まるので、
# 長さと内容のハッシュをキーに直近の結果を覚える（本文は持たない）。
_MEMO_MIN_CHARS: Final = 1024  # これより短い文字列は数える方が速い
_MEMO_MAX_ENTRIES: Final = 512
_memo: OrderedDict[tuple[int, int], int] = OrderedDict()
_memo_lock = threading.Lock()


class TokenEstimator:
    """``o200k_base`` 互換の近似（仕様書 §5.7）。文字種別と連の数の係数の合計を切り上げる（S-12 ``lean_wide``）。"""

    def count(self, text: str) -> int:
        if len(text) < _MEMO_MIN_CHARS:
            return self._count(text)
        key = (len(text), hash(text))
        with _memo_lock:
            hit = _memo.get(key)
            if hit is not None:
                _memo.move_to_end(key)
                return hit
        value = self._count(text)
        with _memo_lock:
            _memo[key] = value
            while len(_memo) > _MEMO_MAX_ENTRIES:
                _memo.popitem(last=False)
        return value

    @staticmethod
    def _count(text: str) -> int:
        if not text:
            return 0
        units = 0
        if _RE_BLOB.search(text):
            units += _W_BLOB * sum(map(len, _RE_BLOB.findall(text)))
            text = _RE_BLOB.sub("", text)
        units += _W_LETTERS * len(_RE_LETTERS.findall(text))
        units += _W_DIGITS * sum(map(len, _RE_DIGITS.findall(text)))
        units += _W_SYMBOL * len(_RE_SYMBOL.findall(text))
        units += _W_SPACE * len(_RE_SPACE_MULTI.findall(text))
        units += _W_NL * (text.count("\n") + text.count("\r"))
        if not text.isascii():  # ASCII だけなら ``wide`` は 0。走査を省く
            units += _W_WIDE * sum(map(len, _RE_WIDE.findall(text)))
        units += _W_OTHER * sum(map(len, _RE_OTHER.findall(text)))  # ASCII の制御文字（\t \r \n 以外）も含む
        return -(-units // _DEN)
