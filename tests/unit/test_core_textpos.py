"""core.textpos のテスト（設計書 ADR-14、§9.5、§9.6）。"""

from __future__ import annotations

import random

import pytest

from lucianke.core.model import Position
from lucianke.core.textpos import (
    BOM,
    Clamped,
    IndentSpec,
    LineIndex,
    Unit,
    detect_indent,
    detect_newline,
    has_bom,
    has_final_newline,
    strip_bom,
)

B, U16, CP = Unit.UTF8, Unit.UTF16, Unit.CODEPOINT


def idx(text: str) -> LineIndex:
    return LineIndex(text.encode("utf-8"))


# --- 行表 ---------------------------------------------------------------------------------


def test_ascii_lines() -> None:
    li = idx("hello\nworld\n")
    assert li.line_count == 3  # 末尾の改行の後ろに空行がある
    assert li.content_line_count == 2
    assert [li.line_text(i) for i in range(3)] == ["hello", "world", ""]
    assert [li.line_start(i) for i in range(3)] == [0, 6, 12]
    assert [li.line_end(i) for i in range(3)] == [5, 11, 12]
    assert li.line_of_offset(0) == 0
    assert li.line_of_offset(5) == 0  # 区切りの手前
    assert li.line_of_offset(6) == 1
    assert li.line_of_offset(12) == 2
    # ASCII は 3 つの単位がすべて等しい
    for unit_a in Unit:
        for unit_b in Unit:
            assert li.convert_col(0, 3, unit_a, unit_b) == Clamped(3, False)


def test_ascii_clamps_to_line_end() -> None:
    li = idx("hello\nworld")
    assert li.convert_col(0, 5, CP, B) == Clamped(5, False)  # 行末ちょうど
    assert li.convert_col(0, 6, CP, B) == Clamped(5, True)  # 行末を越える → 丸める
    assert li.convert_col(0, 999, B, U16) == Clamped(5, True)
    assert li.convert_col(1, 5, U16, CP) == Clamped(5, False)


def test_fullwidth() -> None:
    li = idx("あいうえお = 1\n")
    # あ: UTF-8 3 バイト、UTF-16 1 ユニット、コードポイント 1
    assert li.convert_col(0, 2, CP, B) == Clamped(6, False)
    assert li.convert_col(0, 2, CP, U16) == Clamped(2, False)
    assert li.convert_col(0, 6, B, CP) == Clamped(2, False)
    assert li.convert_col(0, 2, U16, B) == Clamped(6, False)
    assert li.line_length(0, CP) == 9
    assert li.line_length(0, B) == 5 * 3 + 4
    assert li.line_length(0, U16) == 9
    # 文字の途中のバイト列 → 文字の先頭へ丸め
    assert li.convert_col(0, 4, B, CP) == Clamped(1, True)
    assert li.convert_col(0, 5, B, CP) == Clamped(1, True)


def test_emoji_surrogate_pair() -> None:
    li = idx("a😀b\n")
    assert li.line_length(0, CP) == 3
    assert li.line_length(0, U16) == 4  # 絵文字は UTF-16 で 2
    assert li.line_length(0, B) == 6  # UTF-8 で 4
    # 😀 の後ろ（b の位置）
    assert li.convert_col(0, 2, CP, U16) == Clamped(3, False)
    assert li.convert_col(0, 2, CP, B) == Clamped(5, False)
    assert li.convert_col(0, 3, U16, CP) == Clamped(2, False)
    assert li.convert_col(0, 5, B, CP) == Clamped(2, False)
    # 😀 の先頭
    assert li.convert_col(0, 1, CP, U16) == Clamped(1, False)
    # ペアの途中（UTF-16 の 2）、UTF-8 の途中（2〜4）→ 😀 の先頭（コードポイント 1）へ
    assert li.convert_col(0, 2, U16, CP) == Clamped(1, True)
    for mid in (2, 3, 4):
        assert li.convert_col(0, mid, B, CP) == Clamped(1, True)
    # 行末越え
    assert li.convert_col(0, 99, U16, CP) == Clamped(3, True)


def test_combining_characters_count_code_points() -> None:
    # 結合文字: "é" = e + U+0301 は 2 コードポイント（書記素ではなくコードポイント単位。仕様書 §5.10）
    li = idx("e\u0301x")
    assert li.line_length(0, CP) == 3
    assert li.convert_col(0, 2, CP, B) == Clamped(3, False)  # e(1) + U+0301(2)
    assert li.convert_col(0, 2, CP, U16) == Clamped(2, False)
    assert li.convert_col(0, 3, B, CP) == Clamped(2, False)
    # NFC の "é" は 1 コードポイント・2 バイト
    nfc = idx("\u00e9x")
    assert nfc.convert_col(0, 1, CP, B) == Clamped(2, False)


def test_crlf_line_breaks() -> None:
    li = LineIndex(b"ab\r\ncd\r\n")
    assert li.line_count == 3
    assert li.content_line_count == 2
    assert [li.line_bytes(i) for i in range(3)] == [b"ab", b"cd", b""]
    assert [li.line_start(i) for i in range(3)] == [0, 4, 8]
    assert li.offset_to_position(2) == Clamped(Position(0, 2), False)  # \r の位置 = 行末
    assert li.offset_to_position(3) == Clamped(Position(0, 2), True)  # \r と \n の間 → 行末へ丸め
    assert li.offset_to_position(4) == Clamped(Position(1, 0), False)
    assert li.offset_to_position(8) == Clamped(Position(2, 0), False)
    assert li.position_to_offset(Position(0, 9)) == Clamped(2, True)  # 行末越え → \r の手前
    assert li.line_of_offset(3) == 0


def test_lone_cr_is_a_line_break() -> None:
    li = LineIndex(b"a\rb\nc\r\nd")
    assert li.line_count == 4
    assert [li.line_text(i) for i in range(4)] == ["a", "b", "c", "d"]


def test_empty_file() -> None:
    li = LineIndex(b"")
    assert li.line_count == 1
    assert li.content_line_count == 0
    assert li.line_bytes(0) == b""
    assert li.offset_to_position(0) == Clamped(Position(0, 0), False)
    assert li.position_to_offset(Position(0, 0)) == Clamped(0, False)
    assert li.position_to_offset(Position(0, 5)) == Clamped(0, True)
    assert li.convert_col(0, 3, CP, U16) == Clamped(0, True)


def test_no_final_newline() -> None:
    li = LineIndex(b"a\nb")
    assert li.line_count == 2
    assert li.content_line_count == 2
    assert li.offset_to_position(3) == Clamped(Position(1, 1), False)  # EOF
    assert li.position_to_offset(Position(1, 1)) == Clamped(3, False)


def test_single_newline_only() -> None:
    li = LineIndex(b"\n")
    assert li.line_count == 2
    assert li.content_line_count == 1


def test_out_of_range_line_and_offset_raise() -> None:
    li = idx("ab\ncd")
    with pytest.raises(ValueError):
        li.line_text(2)
    with pytest.raises(ValueError):
        li.line_text(-1)
    with pytest.raises(ValueError):
        li.convert_col(5, 0, CP, B)
    with pytest.raises(ValueError):
        li.convert_col(0, -1, CP, B)
    with pytest.raises(ValueError):
        li.offset_to_position(6)
    with pytest.raises(ValueError):
        li.offset_to_position(-1)
    with pytest.raises(ValueError):
        li.position_to_offset(Position(2, 0))


def test_bom_is_invisible_in_positions() -> None:
    li = LineIndex(BOM + b"abc\ndef")
    assert li.bom_len == 3
    assert li.line_start(0) == 3
    assert li.line_text(0) == "abc"
    assert li.line_count == 2
    # コードポイント列・UTF-16 列に BOM は数えない
    assert li.offset_to_position(3) == Clamped(Position(0, 0), False)
    assert li.offset_to_position(5) == Clamped(Position(0, 2), False)
    assert li.position_to_offset(Position(0, 0)) == Clamped(3, False)
    assert li.position_to_offset(Position(0, 2)) == Clamped(5, False)
    # UTF-8 の列は渡されたバイト列の先頭から数える（Tree-sitter の Point.column と合う）
    assert li.convert_col(0, 5, B, CP) == Clamped(2, False)
    assert li.convert_col(0, 2, CP, B) == Clamped(5, False)
    # BOM の中を指すバイト列は 0 に丸める
    assert li.convert_col(0, 1, B, CP) == Clamped(0, True)
    assert li.offset_to_position(1) == Clamped(Position(0, 0), True)
    # 2 行目は影響を受けない
    assert li.offset_to_position(li.line_start(1) + 2) == Clamped(Position(1, 2), False)
    assert li.line_length(0, CP) == 3
    assert li.line_length(0, B) == 6  # BOM を含む行末の列


def test_bom_with_non_ascii_first_line() -> None:
    li = LineIndex(BOM + "あい\n".encode())
    assert li.line_text(0) == "あい"
    assert li.convert_col(0, 1, CP, B) == Clamped(3 + 3, False)
    assert li.convert_col(0, 6, B, CP) == Clamped(1, False)
    assert li.convert_col(0, 2, B, CP) == Clamped(0, True)  # BOM の中
    assert li.convert_col(0, 4, B, CP) == Clamped(0, True)  # あ の途中


def test_bom_only_file() -> None:
    li = LineIndex(BOM)
    assert li.line_count == 1
    assert li.line_bytes(0) == b""
    assert li.offset_to_position(3) == Clamped(Position(0, 0), False)


def test_invalid_utf8_counts_one_byte_per_character() -> None:
    li = LineIndex(b"a\xffb\n")
    assert li.line_length(0, CP) == 3
    assert li.convert_col(0, 2, CP, B) == Clamped(2, False)
    assert li.convert_col(0, 2, B, CP) == Clamped(2, False)
    assert li.convert_col(0, 3, CP, U16) == Clamped(3, False)


def test_to_unit_and_from_unit() -> None:
    li = idx("x😀y\n")
    assert li.to_unit(Position(0, 2), U16) == Clamped((0, 3), False)
    assert li.to_unit(Position(0, 2), B) == Clamped((0, 5), False)
    assert li.from_unit(0, 3, U16) == Clamped(Position(0, 2), False)
    assert li.from_unit(0, 99, U16) == Clamped(Position(0, 3), True)


def test_roundtrips_against_python_reference() -> None:
    rng = random.Random(1029)
    alphabet = ["a", "b", " ", "あ", "い", "é", "e\u0301", "😀", "🇯🇵", "\U00010000", "\t", "ß"]
    newlines = ["\n", "\r\n", "\r"]
    for _ in range(300):
        nl = rng.choice(newlines)
        lines = ["".join(rng.choices(alphabet, k=rng.randint(0, 12))) for _ in range(rng.randint(1, 5))]
        text = nl.join(lines)
        if rng.random() < 0.5:
            text += nl
        li = idx(text)
        expected_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        assert li.line_count == len(expected_lines)
        for n, line_text in enumerate(expected_lines):
            assert li.line_text(n) == line_text
            for col in range(len(line_text) + 1):
                prefix = line_text[:col]
                u8 = len(prefix.encode("utf-8"))
                u16 = len(prefix.encode("utf-16-le")) // 2
                assert li.convert_col(n, col, CP, B) == Clamped(u8, False)
                assert li.convert_col(n, col, CP, U16) == Clamped(u16, False)
                assert li.convert_col(n, u8, B, CP) == Clamped(col, False)
                assert li.convert_col(n, u16, U16, CP) == Clamped(col, False)
                assert li.convert_col(n, u8, B, U16) == Clamped(u16, False)
                assert li.convert_col(n, u16, U16, B) == Clamped(u8, False)
                off = li.position_to_offset(Position(n, col))
                assert off.clamped is False
                assert li.data[off.value - len(prefix.encode("utf-8")) : off.value] == prefix.encode("utf-8")
                assert li.offset_to_position(off.value) == Clamped(Position(n, col), False)


# --- 長い行（BUG-0112） --------------------------------------------------------------------


class _LineByLine(LineIndex):
    """形を覚えない ``LineIndex``（変換のたびに行を切り出してデコードする、従来の変換）。長い行の答えの基準にする。"""

    __slots__ = ()

    def _shape(self, line: int) -> None:
        return None


def test_long_lines_answer_the_same_as_the_line_by_line_conversion() -> None:
    """``SHAPE_MIN_BYTES`` 以上の行は形を覚えて二分探索で答える。列のすべて（行末の先・文字の途中・サロゲートペアの途中・
    BOM・UTF-8 として不正なバイトを含む行）で、従来の変換と同じ答え・同じ丸めの印になる。"""
    rng = random.Random(1031)
    pieces = ["a", "b", " ", "é", "あ", "😀", "\U00010348", "xyz", "ß", "é"]
    bad = [b"\xff", b"\x80", b"\xc3", b"\xed\xa0\x80"]
    for _ in range(40):
        lines = []
        for _ in range(rng.randint(1, 3)):
            line = "".join(rng.choice(pieces) for _ in range(rng.choice([5, 120, 300]))).encode("utf-8")
            if rng.random() < 0.25:
                pos = rng.randrange(len(line) + 1)
                line = line[:pos] + rng.choice(bad) + line[pos:]
            lines.append(line)
        data = (BOM if rng.random() < 0.3 else b"") + rng.choice([b"\n", b"\r\n"]).join(lines)
        fast, slow = LineIndex(data), _LineByLine(data)
        for n in range(fast.line_count):
            for unit in Unit:
                assert fast.line_length(n, unit) == slow.line_length(n, unit)
            end = slow.line_length(n, B) + 3
            cols = {*range(min(end, 40)), *range(max(0, end - 40), end), *rng.sample(range(end), min(end, 120))}
            for src in Unit:
                for dst in Unit:
                    for col in sorted(cols):
                        assert fast.convert_col(n, col, src, dst) == slow.convert_col(n, col, src, dst), (n, col, src, dst)
        for off in range(0, len(data) + 1, 3):
            assert fast.offset_to_position(off) == slow.offset_to_position(off)


def test_long_line_conversions_do_not_rescan_the_line() -> None:
    """圧縮した JS の 1 行（数 MB・非 ASCII を含む）で位置の変換を何万回しても、行の長さに比例して遅くならない。
    従来は変換のたびに行全体を切り出してデコードし、1 ファイルの取り込みが数十分かかった（BUG-0112）。"""
    import time

    chunk = 'function f(t){return g(t)+"é😀"}'.encode()
    line = chunk * (2_000_000 // len(chunk))
    li = LineIndex(b"// header\n" + line + b"\n")
    start = li.line_start(1)
    offsets = [start + (i * 7919) % len(line) for i in range(100_000)]
    t0 = time.perf_counter()
    for off in offsets:
        li.offset_to_position(off)
    assert time.perf_counter() - t0 < 10.0  # 実測 0.3 秒前後。従来の変換では 100 秒を超える
    last = li.offset_to_position(start + len(line))
    assert last == Clamped(Position(1, len(line.decode("utf-8"))), False)


# --- 検出 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", "\n"),
        (b"no newline", "\n"),
        (b"a\nb\nc\n", "\n"),
        (b"a\r\nb\r\nc\r\n", "\r\n"),
        (b"a\rb\rc\r", "\r"),
        (b"a\r\nb\r\nc\n", "\r\n"),  # 多数派
        (b"a\nb\nc\r\n", "\n"),
        (b"a\r\nb\nc", "\r\n"),  # 同数 → 先に現れた方
        (b"a\nb\r\nc", "\n"),
        (b"a\rb\nc\r\nd", "\r"),  # 3 者同数 → 先に現れた方
        (b"a\nb\r\nc\r\nd\ne\r", "\n"),  # LF 2・CRLF 2・CR 1: 同数の 2 者のうち先に現れた LF
        (b"a\rb\nc\nd\r\ne\r\n", "\n"),  # 最初の改行は CR だが 1 票。LF 2 と CRLF 2 のうち先の LF
    ],
)
def test_detect_newline(data: bytes, expected: str) -> None:
    assert detect_newline(data) == expected


def test_bom_and_final_newline_helpers() -> None:
    assert has_bom(BOM + b"x")
    assert not has_bom(b"x")
    assert not has_bom(b"")
    assert strip_bom(BOM + b"x") == b"x"
    assert strip_bom(b"x") == b"x"
    assert has_final_newline(b"a\n")
    assert has_final_newline(b"a\r\n")
    assert has_final_newline(b"a\r")
    assert not has_final_newline(b"a")
    assert not has_final_newline(b"")
    assert has_final_newline(b"\n")


PY4 = b"""class A:
    def f(self):
        if x:
            return 1
        return 2

    def g(self):
        pass
"""

JS2 = b"""function f() {
  if (x) {
    return 1;
  }
  return 2;
}
"""

GO_TAB = b"func f() {\n\tif x {\n\t\treturn 1\n\t}\n\treturn 2\n}\n"


def test_detect_indent_basic() -> None:
    assert detect_indent(PY4) == IndentSpec("space", 4)
    assert detect_indent(JS2) == IndentSpec("space", 2)
    assert detect_indent(GO_TAB) == IndentSpec("tab", 1)
    assert detect_indent(b"if x:\n        y\n        if z:\n                w\n") == IndentSpec("space", 8)


def test_detect_indent_none_when_unknown() -> None:
    assert detect_indent(b"") is None
    assert detect_indent(b"a\nb\nc\n") is None
    assert detect_indent(b"   \n\t\n\n") is None
    assert detect_indent(b"a\n b\n") is None  # 増分 1 は票にならない
    assert detect_indent(b"a\n   b\n") is None  # 増分 3 も


def test_detect_indent_crlf_and_blank_lines() -> None:
    data = PY4.replace(b"\n", b"\r\n")
    assert detect_indent(data) == IndentSpec("space", 4)
    spaced = b"a:\n    b\n\n\n    c\n   \n    d\n"
    assert detect_indent(spaced) == IndentSpec("space", 4)


def test_detect_indent_ignores_continuation_noise() -> None:
    src = (
        b"def f(a,\n      b):\n    x = 1\n    if x:\n        y = 2\n    return call(a,\n                b)\n"
    )
    # 増分: 6(無視) → 4(票) ← 実際は a→b の継続行 6、def 内 4 … 多数派は 4
    assert detect_indent(src) == IndentSpec("space", 4)


def test_detect_indent_majority_tab_vs_space() -> None:
    tabs = b"a\n\tb\n\t\tc\n\t\t\td\n"
    mostly_tabs = tabs + b"x\n    y\n"
    assert detect_indent(mostly_tabs) == IndentSpec("tab", 1)
    spaces = b"a\n  b\n    c\n      d\n"
    mostly_spaces = spaces + b"x\n\ty\n"
    assert detect_indent(mostly_spaces) == IndentSpec("space", 2)


def test_detect_indent_tie_prefers_first_seen() -> None:
    assert detect_indent(b"a\n\tb\nc\n    d\n") == IndentSpec("tab", 1)
    assert detect_indent(b"a\n    b\nc\n\td\n") == IndentSpec("space", 4)


def test_detect_indent_width_tie_prefers_smaller() -> None:
    assert detect_indent(b"a\n  b\nc\n    d\n") == IndentSpec("space", 2)


def test_indent_spec() -> None:
    assert IndentSpec("space", 4).unit == "    "
    assert IndentSpec("space", 4).label == "4sp"
    assert IndentSpec("tab", 1).unit == "\t"
    assert IndentSpec("tab", 1).label == "tab"
    with pytest.raises(ValueError):
        IndentSpec("tab", 4)
    with pytest.raises(ValueError):
        IndentSpec("space", 0)
    with pytest.raises(ValueError):
        IndentSpec("mixed", 2)  # type: ignore[arg-type]
