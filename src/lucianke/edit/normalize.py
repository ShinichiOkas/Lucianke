"""新規コード（``new_code`` / ``rewrite``）の正規化（仕様書 §10.9・T-10 処理規約 4、設計書 §7.7.3）。

手順（``normalize_new_code``）:

1. 改行を ``"\\n"`` に直し、前後の空行を取り除く（末尾は改行 1 つで終わる）。
2. 空行を除く全行の先頭空白の最長共通接頭辞を除く。
3. タブとスペースが混在していれば ``E_INVALID_ARGUMENT``（``details.param="new_code"``、``details.reason="mixed_indent"``）。
4. ``new_code`` 内のインデント単位（タブ、またはスペース幅）を推定し、各行の先頭空白を「深さ」に直して、
   対象ファイルのインデント単位に付け替える。
5. 基準列（``base_column``）ぶんのインデントを前置する。
6. 改行コードを対象ファイルの多数派に合わせる。

インデントの半端な余り（継続行の位置合わせなど）は、スペースのまま深さの後ろに残す。空白だけの行は空行にする。
"""

from __future__ import annotations

import math
import re
from typing import Literal

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.textpos import IndentSpec, detect_indent, detect_newline
from lucianke.syntax.support import get_support

__all__ = ["blank_lines_between", "default_indent", "normalize_new_code"]

_SPLIT = re.compile(r"\r\n|\r|\n")
_LEAD = re.compile(r"[ \t]*")
_WELL_FORMED_LEAD = re.compile(r"\t* *")  # タブ → スペースの順だけ許す（位置合わせのスペース）
_FALLBACK_INDENT = IndentSpec("space", 4)


def default_indent(language_id: str) -> IndentSpec:
    """言語の慣例のインデント単位（Python 4、JS/TS 2、Rust 4、Go タブ）。未登録の言語は 4 スペース。"""
    support = get_support(language_id)
    return support.indent_default if support is not None else _FALLBACK_INDENT


def blank_lines_between(language_id: str, *, toplevel: bool) -> int:
    """定義と定義の間に置く空行の数（Python のトップレベルは 2、それ以外は 1。仕様書 T-10 処理規約 4(e)）。"""
    return 2 if language_id == "python" and toplevel else 1


def _bad_new_code(message: str, **details: object) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, message, details={"param": "new_code", **details})


def _mixed_indent(line: int) -> LuciankeError:
    return _bad_new_code(
        f"new_code mixes tabs and spaces in the indentation (line {line})",
        reason="mixed_indent",
        line=line,
    )


def _is_blank(line: str) -> bool:
    return not line.strip(" \t")


def _common_prefix(items: list[str]) -> str:
    lo, hi = min(items), max(items)
    n = 0
    for a, b in zip(lo, hi, strict=False):
        if a != b:
            break
        n += 1
    return lo[:n]


def normalize_new_code(
    new_code: str,
    *,
    target_source: bytes,
    base_column: int,
    language_id: str,
    mode: Literal["replace", "insert"],
) -> str:
    """``new_code`` を対象ファイルの流儀に合わせて整える。

    * ``target_source``: 対象ファイルの内容。インデント単位（``detect_indent``。推定できなければ言語の慣例）と
      改行コード（``detect_newline``）をここから決める。
    * ``base_column``: 基準列（コードポイント）。先頭空白のぶんだけ、ファイルの単位の文字（タブのファイルならタブ、
      それ以外はスペース）を ``base_column`` 個前置する。
    * ``mode="replace"``: 置換範囲が基準列から始まる（行頭の空白は元のまま残る）ので、**最初の行には基準インデントを付けない**。
      ``mode="insert"``: 行頭に差し込むので、最初の行にも付ける。空行には付けない。
    * 戻り値は対象ファイルの改行で区切り、**改行 1 つで終わる**（``replace`` で範囲が行末までなら、呼び出し側が末尾の改行を落とす）。
    * 空（空白だけ）なら ``E_INVALID_ARGUMENT``（``details.param="new_code"``、``details.reason="empty"``）。
    """
    if base_column < 0:
        raise ValueError(f"base_column must be non-negative: {base_column}")
    if mode not in ("replace", "insert"):
        raise ValueError(f"unknown mode: {mode!r}")

    lines = _SPLIT.split(new_code.removeprefix("\ufeff"))
    while lines and _is_blank(lines[0]):
        lines.pop(0)
    while lines and _is_blank(lines[-1]):
        lines.pop()
    if not lines:
        raise _bad_new_code(
            "new_code is empty",
            reason="empty",
            hint="削除は operation: delete_symbol を使う",
        )

    newline = detect_newline(target_source)
    file_indent = detect_indent(target_source) or default_indent(language_id)

    # 行ごとの (先頭空白, 本文)。空行は None
    split: list[tuple[str, str] | None] = []
    for number, line in enumerate(lines, 1):
        if _is_blank(line):
            split.append(None)
            continue
        lead = _LEAD.match(line).group()  # type: ignore[union-attr]
        if not _WELL_FORMED_LEAD.fullmatch(lead):
            raise _mixed_indent(number)
        split.append((lead, line[len(lead) :]))
    parts = [p for p in split if p is not None]
    uses_tab = any(lead.startswith("\t") for lead, _ in parts)
    if uses_tab:
        for number, p in enumerate(split, 1):
            if p is not None and p[0].startswith(" "):
                raise _mixed_indent(number)

    # 共通先頭空白を除く
    prefix = _common_prefix([lead for lead, _ in parts])
    rel_leads = [lead[len(prefix) :] for lead, _ in parts]

    # new_code のインデント単位（スペースのとき幅。推定できなければ先頭空白の長さの最大公約数）
    src_width = 1
    counts = [len(r) for r in rel_leads if r]
    if not uses_tab and counts:
        probe = "\n".join(r + body for r, (_, body) in zip(rel_leads, parts, strict=True))
        spec = detect_indent(probe.encode("utf-8", "surrogateescape"))
        src_width = spec.width if spec is not None and spec.kind == "space" else math.gcd(*counts)

    def convert(rel: str) -> str:
        if not rel:
            return ""
        if uses_tab:
            depth = len(rel) - len(rel.lstrip("\t"))
            rest = len(rel) - depth  # 位置合わせのスペース
        else:
            depth, rest = divmod(len(rel), src_width)
        return file_indent.unit * depth + " " * rest

    base = ("\t" if file_indent.kind == "tab" else " ") * base_column
    out: list[str] = []
    k = 0  # 空行でない行の通し番号
    for i, p in enumerate(split):
        if p is None:
            out.append("")
            continue
        head = "" if (mode == "replace" and i == 0) else base
        out.append(head + convert(rel_leads[k]) + p[1])
        k += 1
    return newline.join(out) + newline
