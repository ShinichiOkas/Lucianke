"""位置の単位変換（``LineIndex``、設計書 ADR-14）と、改行・BOM・インデントの検出（設計書 §9.5・§9.6、仕様書 §10.9）。

位置の単位は 3 つ:

* ``Unit.UTF8``: UTF-8 のバイト数（Tree-sitter の ``Point.column`` と ``start_byte`` が使う）
* ``Unit.CODEPOINT``: Unicode コードポイント数（Lucianke の内部表現と外部表現。仕様書 §5.10）
* ``Unit.UTF16``: UTF-16 コードユニット数（LSP の既定）

値は LSP の ``positionEncodingKind``（``utf-8`` / ``utf-16`` / ``utf-32``）と同じ文字列で、交渉結果をそのまま使える。

``LineIndex`` の約束:

* 行の区切りは ``\\r\\n``、``\\n``、``\\r``（LSP の定義）。``\\r\\n`` は 1 つの区切り。行の内容は区切りを含まない。
  末尾に区切りがあれば、その後ろに空の行がある（``b"a\\n"`` は 2 行）。空のバイト列は 1 行。
* 行末を越える列、文字の途中を指す列は **エラーにせず**、行末または直前の文字境界に丸めて ``clamped=True`` を返す。
  範囲外の **行** と範囲外のバイトオフセットは ``ValueError``。
* BOM（``EF BB BF``）は位置に現れない: 行 0 の内容は BOM の後ろから始まり、コードポイント列・UTF-16 列には数えない。
  ただし ``Unit.UTF8`` の列とバイトオフセットは **渡されたバイト列の先頭から** 数える（Tree-sitter に同じバイト列を
  渡したときの値とそのまま合う）。推奨は、BOM を ``strip_bom`` で剥がしてから ``LineIndex`` と Parser に渡し、
  BOM の有無は ``has_bom`` で別に記録して書き込み時に戻すこと（その場合、この段落の例外は起きない）。
* UTF-8 として不正なバイトは 1 バイト = 1 文字として数える（``surrogateescape``）。
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NamedTuple

from lucianke.core.model import Position

BOM = b"\xef\xbb\xbf"

Newline = Literal["\n", "\r\n", "\r"]


class Unit(StrEnum):
    """列の単位。値は LSP の ``positionEncodingKind``。"""

    UTF8 = "utf-8"
    UTF16 = "utf-16"
    CODEPOINT = "utf-32"


class Clamped[T](NamedTuple):
    """変換結果。``clamped`` は、行末を越えていた・文字の途中を指していたため丸めたことを示す。"""

    value: T
    clamped: bool


_TERMINATOR_RE = re.compile(rb"\r\n|\n|\r")


class LineIndex:
    """UTF-8 のバイト列から作る、行頭バイトオフセット表と位置変換。

    行ごとの変換は、その行のバイト列から必要になったときに計算する（3 つの単位の表を全行ぶん持たない）。
    ASCII だけの行は 3 つの単位がすべて等しいので、デコードせずに答える。
    """

    __slots__ = ("_data", "_ends", "_starts", "bom_len")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.bom_len = len(BOM) if data.startswith(BOM) else 0
        starts = [0]
        ends: list[int] = []
        for m in _TERMINATOR_RE.finditer(data):
            ends.append(m.start())
            starts.append(m.end())
        ends.append(len(data))
        self._starts = starts
        self._ends = ends

    @classmethod
    def from_bytes(cls, data: bytes) -> LineIndex:
        return cls(data)

    # --- 行 ---------------------------------------------------------------------------

    @property
    def data(self) -> bytes:
        return self._data

    @property
    def line_count(self) -> int:
        """行数（区切りの数 + 1。LSP・エディタの数え方）。空のバイト列は 1。"""
        return len(self._starts)

    @property
    def content_line_count(self) -> int:
        """``wc -l`` 流の行数。末尾の区切りの後ろの空行は数えず、空のバイト列は 0。"""
        if not self._data:
            return 0
        return self.line_count - 1 if self._starts[-1] == len(self._data) else self.line_count

    def _check_line(self, line: int) -> None:
        if not 0 <= line < len(self._starts):
            raise ValueError(f"line {line} out of range (0..{len(self._starts) - 1})")

    def line_start(self, line: int) -> int:
        """行の内容の先頭バイトオフセット（行 0 は BOM の後ろ）。"""
        self._check_line(line)
        return self.bom_len if line == 0 else self._starts[line]

    def line_end(self, line: int) -> int:
        """行の内容の終端バイトオフセット（区切りの手前）。"""
        self._check_line(line)
        return self._ends[line]

    def line_bytes(self, line: int) -> bytes:
        return self._data[self.line_start(line) : self._ends[line]]

    def line_text(self, line: int) -> str:
        return self.line_bytes(line).decode("utf-8", "surrogateescape")

    def line_of_offset(self, offset: int) -> int:
        """バイトオフセットを含む行（区切りの中のオフセットは、その区切りで終わる行）。"""
        if not 0 <= offset <= len(self._data):
            raise ValueError(f"offset {offset} out of range (0..{len(self._data)})")
        return bisect_right(self._starts, offset) - 1

    def line_length(self, line: int, unit: Unit = Unit.CODEPOINT) -> int:
        """行の内容の長さ（``unit`` 単位。``Unit.UTF8`` は行 0 の BOM を含む = 行の終端の列）。"""
        raw = self.line_bytes(line)
        if unit is Unit.UTF8:
            return len(raw) + (self.bom_len if line == 0 else 0)
        if raw.isascii():
            return len(raw)
        text = raw.decode("utf-8", "surrogateescape")
        return _utf16_units(text) if unit is Unit.UTF16 else len(text)

    # --- 列の変換 ---------------------------------------------------------------------

    def convert_col(self, line: int, col: int, src: Unit, dst: Unit) -> Clamped[int]:
        """``line`` の列 ``col``（``src`` 単位）を ``dst`` 単位にする。

        行末を越えた列は行末に、文字（UTF-8 の複数バイト、UTF-16 のサロゲートペア）の途中を指す列は
        その文字の先頭に丸め、``clamped=True`` を返す。``line`` が範囲外、``col`` が負なら ``ValueError``。
        """
        if col < 0:
            raise ValueError(f"column must be non-negative: {col}")
        raw = self.line_bytes(line)  # 範囲外の行はここで ValueError
        skip = self.bom_len if line == 0 else 0  # 行頭から内容の開始位置まで（UTF8 の列だけが BOM を数える）

        if raw.isascii():
            limit = len(raw)
            if src is Unit.UTF8:
                col -= skip
                if col < 0:
                    return Clamped(skip if dst is Unit.UTF8 else 0, True)
            value = min(col, limit)
            clamped = col > limit
            return Clamped(value + (skip if dst is Unit.UTF8 else 0), clamped)

        text = raw.decode("utf-8", "surrogateescape")
        clamped = False

        # src → コードポイント位置
        if src is Unit.CODEPOINT:
            cp = col
            if cp > len(text):
                cp, clamped = len(text), True
        elif src is Unit.UTF8:
            b = col - skip
            if b < 0:
                b, clamped = 0, True
            if b > len(raw):
                b, clamped = len(raw), True
            while 0 < b < len(raw) and (raw[b] & 0xC0) == 0x80:  # 継続バイト = 文字の途中
                b -= 1
                clamped = True
            cp = len(raw[:b].decode("utf-8", "surrogateescape"))
        else:  # UTF16
            cp, clamped = _utf16_to_cp(text, col)

        # コードポイント位置 → dst
        if dst is Unit.CODEPOINT:
            value = cp
        elif dst is Unit.UTF8:
            value = len(text[:cp].encode("utf-8", "surrogateescape")) + skip
        else:
            value = _utf16_units(text[:cp])
        return Clamped(value, clamped)

    # --- バイトオフセット ↔ Position ----------------------------------------------------

    def offset_to_position(self, offset: int) -> Clamped[Position]:
        """バイトオフセット → ``Position``（0 始まりの行、コードポイント列）。

        区切りの途中や BOM の中を指すオフセットは、その行の末尾 / 先頭に丸めて ``clamped=True``。
        範囲外のオフセットは ``ValueError``。
        """
        line = self.line_of_offset(offset)
        col = self.convert_col(line, offset - self._starts[line], Unit.UTF8, Unit.CODEPOINT)
        return Clamped(Position(line, col.value), col.clamped)

    def position_to_offset(self, pos: Position) -> Clamped[int]:
        """``Position``（コードポイント列）→ バイトオフセット。行末を越える列は行末に丸める。"""
        col = self.convert_col(pos.line, pos.col, Unit.CODEPOINT, Unit.UTF8)
        return Clamped(self._starts[pos.line] + col.value, col.clamped)

    # --- 外部（LSP）との橋渡し ---------------------------------------------------------

    def to_unit(self, pos: Position, dst: Unit) -> Clamped[tuple[int, int]]:
        """内部の ``Position`` → ``(行, dst 単位の列)``（どちらも 0 始まり。LSP へ送る値）。"""
        col = self.convert_col(pos.line, pos.col, Unit.CODEPOINT, dst)
        return Clamped((pos.line, col.value), col.clamped)

    def from_unit(self, line: int, col: int, src: Unit) -> Clamped[Position]:
        """``(行, src 単位の列)``（0 始まり。LSP から受けた値）→ 内部の ``Position``。"""
        cp = self.convert_col(line, col, src, Unit.CODEPOINT)
        return Clamped(Position(line, cp.value), cp.clamped)


def _utf16_units(text: str) -> int:
    if text.isascii() or max(text, default="") <= "￿":
        return len(text)
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _utf16_to_cp(text: str, units: int) -> tuple[int, bool]:
    """UTF-16 の列 → コードポイント位置と、丸めたか。サロゲートペアの途中は直前の文字境界へ。"""
    if max(text, default="") <= "￿":
        if units > len(text):
            return len(text), True
        return units, False
    u = 0
    for i, ch in enumerate(text):
        if u >= units:
            return i, False
        w = 2 if ch > "￿" else 1
        if u + w > units:
            return i, True  # ペアの途中
        u += w
    return len(text), units > u


# --- 検出（設計書 §9.5・§9.6、仕様書 §10.9） ---------------------------------------------


def has_bom(data: bytes) -> bool:
    """UTF-8 の BOM（``EF BB BF``）で始まるか。"""
    return data.startswith(BOM)


def strip_bom(data: bytes) -> bytes:
    """先頭の UTF-8 BOM を取り除く（無ければそのまま）。"""
    return data[len(BOM) :] if data.startswith(BOM) else data


def has_final_newline(data: bytes) -> bool:
    """末尾が改行（``\\n`` または ``\\r``）か。空のバイト列は ``False``。"""
    return data.endswith((b"\n", b"\r"))


_CRLF_ONLY = re.compile(rb"\r\n")
_LF_ONLY = re.compile(rb"(?<!\r)\n")
_CR_ONLY = re.compile(rb"\r(?!\n)")


def detect_newline(data: bytes) -> Newline:
    """ファイルの改行コード。最も多いものを返し、同数なら先に現れたもの。改行が無ければ ``"\\n"``。"""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    counts: dict[Newline, int] = {"\r\n": crlf, "\n": lf, "\r": cr}
    top = max(counts.values())
    if top == 0:
        return "\n"
    finders: dict[Newline, re.Pattern[bytes]] = {"\r\n": _CRLF_ONLY, "\n": _LF_ONLY, "\r": _CR_ONLY}
    first: dict[Newline, int] = {}
    for kind, n in counts.items():
        if n == top:
            m = finders[kind].search(data)
            assert m is not None
            first[kind] = m.start()
    return min(first, key=lambda k: first[k])


@dataclass(frozen=True, slots=True)
class IndentSpec:
    """インデント単位。``kind == "tab"`` のとき ``width`` は 1（タブ 1 文字 = 1 段。表示幅は意味を持たない）。"""

    kind: Literal["space", "tab"]
    width: int

    def __post_init__(self) -> None:
        if self.kind == "tab":
            if self.width != 1:
                raise ValueError("tab indent has width 1")
        elif self.kind == "space":
            if self.width < 1:
                raise ValueError(f"space indent width must be >= 1: {self.width}")
        else:
            raise ValueError(f"unknown indent kind: {self.kind!r}")

    @property
    def unit(self) -> str:
        """1 段ぶんの文字列（``"\\t"`` または半角スペース ``width`` 個）。"""
        return "\t" if self.kind == "tab" else " " * self.width

    @property
    def label(self) -> str:
        """``indent_unit`` の表記（仕様書 T-10: ``"4sp"``、``"tab"``）。"""
        return "tab" if self.kind == "tab" else f"{self.width}sp"


_INDENT_SAMPLE_BYTES = 1 << 20
_SPACE_WIDTHS = (2, 4, 8)


def detect_indent(data: bytes) -> IndentSpec | None:
    """行頭空白の増分の多数派からインデント単位を推定する。推定できなければ ``None``。

    先頭 1 MiB を標本にし、空白だけの行は飛ばす。連続する（空でない）行で行頭空白が増えた箇所を 1 票とする:

    * タブで始まる行でタブが増えた → タブの票
    * スペースで始まる行でスペースが増えた幅が 2 / 4 / 8 → その幅の票（3 や 7 のような継続行のずれは無視）

    タブの票とスペースの票の総数の多い方を採り、同数なら先に票が入った方。スペースの幅は票の多い幅
    （同数なら小さい方）。タブとスペースが行の途中で混ざる行は、最初の文字で種別を決める。
    """
    sample = data[:_INDENT_SAMPLE_BYTES]
    prev_kind: str | None = None
    prev_n = 0
    tab_votes = 0
    space_votes: Counter[int] = Counter()
    first_tab: int | None = None
    first_space: int | None = None
    for idx, line in enumerate(sample.splitlines()):
        body = line.lstrip(b" \t")
        if not body:
            continue
        lead = line[: len(line) - len(body)]
        if lead[:1] == b"\t":
            kind: str | None = "tab"
            n = len(lead) - len(lead.lstrip(b"\t"))
        elif lead[:1] == b" ":
            kind = "space"
            n = len(lead) - len(lead.lstrip(b" "))
        else:
            kind, n = None, 0
        if kind == "tab" and prev_kind in ("tab", None):
            if n > (prev_n if prev_kind == "tab" else 0):
                tab_votes += 1
                if first_tab is None:
                    first_tab = idx
        elif kind == "space" and prev_kind in ("space", None):
            delta = n - (prev_n if prev_kind == "space" else 0)
            if delta in _SPACE_WIDTHS:
                space_votes[delta] += 1
                if first_space is None:
                    first_space = idx
        prev_kind, prev_n = kind, n
    total_space = sum(space_votes.values())
    if tab_votes == 0 and total_space == 0:
        return None
    use_tab = tab_votes > total_space
    if tab_votes == total_space:
        assert first_tab is not None and first_space is not None
        use_tab = first_tab < first_space
    if use_tab:
        return IndentSpec("tab", 1)
    best = max(space_votes.values())
    width = min(w for w, n in space_votes.items() if n == best)
    return IndentSpec("space", width)
