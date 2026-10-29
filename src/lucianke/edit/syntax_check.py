"""編集の構文検証（仕様書 §10.3 段 4・§10.10 EP-5・§11 T-10 処理規約 3、設計書 §7.7.4）。

編集前後のバイト列を Tree-sitter で解析し、**編集によって新しい構文エラー（ERROR / MISSING ノード）が生じたか** を判定する。
キャッシュ（``TreeCache``）は使わず、``parse_source`` で一時解析する（書き込みが成功するまで、どの版も登録しない）。

判定規則（``check_after``）:

* 言語に文法が無ければ（Tier 4 など）``"skipped"``。
* 編集前にエラーノードが 0 なら、編集後は **ファイル全体で** 0 を要求する。
* 編集前にエラーがあったなら、**編集範囲の前後 1 行を含む窓** のエラー数を前後で比べ、増えていたら失敗。窓の外のエラー
  （もともとあった別の箇所の壊れ）は問わない。編集前の窓は、編集前後の共通の先頭・末尾を手がかりに、編集後の窓から写す
  （呼び手が hunk を渡さなくても足りるよう、座標の対応は 2 つのバイト列だけから求める）。

限界: Tree-sitter は壊れた箇所を 1 個の粗い ERROR にまとめることがある（Python は特に）。そのため比べるのは **窓の中の個数** で、
ERROR の位置や中身の一致ではない。もともと壊れていた箇所を直しつつ、すぐ隣に別の壊れを足すと、個数が同じなので見逃す。

位置は ``Range``（0 始まり・コードポイント。BOM は位置に現れない）。外部に出すときの行は 1 始まり（``to_error_details``）。
BOM は両方のバイト列から外して解析する（BOM の有無は構文の問題ではない）。
"""

from __future__ import annotations

import textwrap
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

import tree_sitter

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Position, Range
from lucianke.core.textpos import LineIndex, strip_bom
from lucianke.syntax.parsers import ERROR_RANGES_MAX, parse_source
from lucianke.syntax.support import get_support
from lucianke.syntax.types import ParsedFile

__all__ = [
    "MAX_REPORTED_ERRORS",
    "SyntaxCheckResult",
    "SyntaxIssue",
    "check_after",
    "check_fragment",
    "to_error_details",
]

MAX_REPORTED_ERRORS = 10  # ``new_errors`` に載せる最大件数（設計書 §7.7.4 項 6）
_SNIPPET_CHARS = 40  # ERROR ノードのメッセージに添える断片の最大文字数

Status = Literal["ok", "skipped", "failed"]
Wrap = Literal["none", "class_body", "module"]


class SyntaxIssue(NamedTuple):
    """構文エラー 1 件。``range`` は 0 始まり・コードポイント（Tree-sitter の ERROR / MISSING ノードの範囲）。"""

    range: Range
    message: str


@dataclass(frozen=True, slots=True)
class SyntaxCheckResult:
    """構文検証の結果。

    ``new_errors`` は ``status == "failed"`` のときだけ入る（最大 ``MAX_REPORTED_ERRORS`` 件）。
    ``before_error_count`` / ``after_error_count`` は **ファイル全体** のエラーノード数（``skipped`` のときは 0）。
    """

    status: Status
    new_errors: list[SyntaxIssue] = field(default_factory=list)
    before_error_count: int = 0
    after_error_count: int = 0


@dataclass(frozen=True, slots=True)
class _Err:
    """木から集めたエラーノード 1 件。``first_line`` / ``last_line`` は 0 始まりの行（``last_line`` は含む）。"""

    kind: Literal["ERROR", "MISSING"]
    node_type: str
    first_line: int
    last_line: int
    range: Range
    text: str
    message: str

    @property
    def signature(self) -> tuple[str, str, str]:
        return (self.kind, self.node_type, self.text)


# --- 木の走査 ------------------------------------------------------------------------------


def _walk_errors(root: tree_sitter.Node) -> Iterator[tree_sitter.Node]:
    """ERROR / MISSING ノードを先頭から順に返す（ERROR の中へは降りない。MISSING は幅 0）。"""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            yield node
        elif node.has_error:
            stack.extend(reversed(node.children))


def _collect(parsed: ParsedFile) -> list[_Err]:
    src = parsed.source
    idx = parsed.line_index
    out: list[_Err] = []
    for node in _walk_errors(parsed.tree.root_node):
        a, b = node.start_byte, node.end_byte
        start = idx.offset_to_position(a).value
        end = idx.offset_to_position(b).value
        last = idx.line_of_offset(b - 1) if b > a else start.line  # 末尾の改行を含む ERROR を次の行に数えない
        if node.is_missing:
            kind: Literal["ERROR", "MISSING"] = "MISSING"
            text = ""
            message = f'syntax error: missing "{node.type}"'
        else:
            kind = "ERROR"
            text = src[a:b].decode("utf-8", "replace")
            first = text.strip().splitlines()[0] if text.strip() else ""
            snippet = first[:_SNIPPET_CHARS] + ("..." if len(first) > _SNIPPET_CHARS else "")
            message = f"syntax error near {snippet!r}" if snippet else "syntax error"
        out.append(_Err(kind, node.type, start.line, max(last, start.line), Range(start, end), text, message))
        if len(out) >= ERROR_RANGES_MAX:
            break
    return out


@dataclass(frozen=True, slots=True)
class _Analysis:
    parsed: ParsedFile
    errors: list[_Err]
    timed_out: bool


def _has_grammar(language_id: str) -> bool:
    spec = get_support(language_id)
    return spec is not None and callable(getattr(spec, "grammar", None))


def _analyze(language_id: str, source: bytes) -> _Analysis | None:
    """``source`` を解析してエラーノードを集める。文法が無ければ ``None``。"""
    if not _has_grammar(language_id):
        return None
    try:
        parsed, timed_out = parse_source(source, language_id, "")
    except LuciankeError as e:
        if e.code is ErrorCode.E_UNSUPPORTED:
            return None
        raise
    return _Analysis(parsed, _collect(parsed), timed_out)


# --- check_after ---------------------------------------------------------------------------


def _common_prefix_len(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    if a[:n] == b[:n]:
        return n
    lo, hi = 0, n  # a[:lo] == b[:lo]、a[:hi] != b[:hi]
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid
    return lo


def _common_suffix_len(a: bytes, b: bytes, limit: int) -> int:
    """末尾の共通部分の長さ（最大 ``limit``。先頭の共通部分と重ならないようにするための上限）。"""
    n = min(len(a), len(b), limit)
    if n == 0 or a[len(a) - n :] == b[len(b) - n :]:
        return n
    lo, hi = 0, n
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if a[len(a) - mid :] == b[len(b) - mid :]:
            lo = mid
        else:
            hi = mid
    return lo


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _windows(
    before: bytes,
    after: bytes,
    before_idx: LineIndex,
    after_idx: LineIndex,
    edited_ranges_after: Sequence[Range],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """``(編集後の窓, 編集前の窓)``（0 始まりの行、両端を含む）。

    編集後の窓 = 編集範囲と実際に変わった行を覆う区間の前後 1 行。編集前の窓は、窓より前の行は同じ番号、窓より後ろの行は
    行数の差だけずらして写す（編集前後で変わらない先頭・末尾の行は、この写しが正確）。
    """
    p = _common_prefix_len(before, after)
    s = _common_suffix_len(before, after, min(len(before), len(after)) - p)
    changed_lo = after_idx.line_of_offset(p)
    changed_hi = after_idx.line_of_offset(len(after) - s)
    lo, hi = changed_lo, changed_hi
    for r in edited_ranges_after:
        lo = min(lo, r.start.line)
        hi = max(hi, r.end.line)
    n_after, n_before = after_idx.line_count, before_idx.line_count
    a_lo, a_hi = _clamp(lo - 1, 0, n_after - 1), _clamp(hi + 1, 0, n_after - 1)
    delta = n_after - n_before
    b_lo = _clamp(a_lo, 0, n_before - 1)
    b_hi = _clamp(a_hi - delta, b_lo, n_before - 1)
    return (a_lo, a_hi), (b_lo, b_hi)


def _in_window(errors: Sequence[_Err], window: tuple[int, int]) -> list[_Err]:
    lo, hi = window
    return [e for e in errors if e.first_line <= hi and e.last_line >= lo]


def _issues(errors: Sequence[_Err]) -> list[SyntaxIssue]:
    return [SyntaxIssue(e.range, e.message) for e in errors[:MAX_REPORTED_ERRORS]]


def check_after(
    language_id: str,
    before: bytes,
    after: bytes,
    *,
    edited_ranges_after: Sequence[Range] = (),
) -> SyntaxCheckResult:
    """編集前後のバイト列を比べ、編集で新しい構文エラーが生じたかを判定する（モジュールの docstring が規則）。

    ``edited_ranges_after`` は編集後の座標での編集範囲（空の範囲も可。挿入は空の範囲）。空なら、2 つのバイト列の違いから
    窓を決める。``failed`` のときの ``new_errors`` は、編集後のエラーのうち編集前に同じものが無かったもの（窓の外は含まない）。
    """
    before, after = strip_bom(before), strip_bom(after)
    after_an = _analyze(language_id, after)
    if after_an is None:
        return SyntaxCheckResult("skipped")
    after_errors = after_an.errors
    if before == after:  # 恒等編集: 何も変わらない
        n = len(after_errors)
        return SyntaxCheckResult("ok", [], n, n)

    before_an = _analyze(language_id, before)
    assert before_an is not None  # 同じ言語なので、文法があることは上で確かめた
    before_errors = before_an.errors
    n_before = len(before_errors) + (1 if before_an.timed_out else 0)
    n_after = len(after_errors) + (1 if after_an.timed_out else 0)

    if after_an.timed_out:  # 解析が期限で届かなかった: 検証できない = 通せない
        tail = after_an.parsed.error_ranges[-1:]
        rng = tail[0] if tail else Range(Position(0, 0), Position(0, 0))
        return SyntaxCheckResult(
            "failed",
            [SyntaxIssue(rng, "syntax could not be verified: parse deadline exceeded")],
            n_before,
            n_after,
        )

    if n_before == 0:
        if n_after == 0:
            return SyntaxCheckResult("ok", [], 0, 0)
        return SyntaxCheckResult("failed", _issues(after_errors), 0, n_after)

    win_after, win_before = _windows(
        before, after, before_an.parsed.line_index, after_an.parsed.line_index, edited_ranges_after
    )
    in_after = _in_window(after_errors, win_after)
    in_before = _in_window(before_errors, win_before)
    if len(in_after) <= len(in_before):
        return SyntaxCheckResult("ok", [], n_before, n_after)
    known = Counter(e.signature for e in in_before)
    fresh: list[_Err] = []
    for e in in_after:  # 編集前の窓に同じ（種別・型・中身）のものがあれば、それを新規とは数えない
        if known[e.signature] > 0:
            known[e.signature] -= 1
        else:
            fresh.append(e)
    return SyntaxCheckResult("failed", _issues(fresh or in_after), n_before, n_after)


# --- check_fragment ------------------------------------------------------------------------

# 言語 -> (ヘッダ行, フッタ行)。断片は ``indent`` だけ字下げして間に入れる（indent が "" なら字下げしない）。
_CLASS_BODY: dict[str, tuple[str, str, str]] = {
    "python": ("class _W:", "", "    "),
    "typescript": ("class _W {", "}", ""),
    "tsx": ("class _W {", "}", ""),
    "javascript": ("class _W {", "}", ""),
    "rust": ("impl _W {", "}", ""),
    "c_sharp": ("class _W {", "}", ""),
    "cpp": ("class _W {", "};", ""),
}
_MODULE: dict[str, tuple[str, str, str]] = {
    "typescript": ("namespace _W {", "}", ""),
    "tsx": ("namespace _W {", "}", ""),
    "rust": ("mod _w {", "}", ""),
    "c_sharp": ("namespace _W {", "}", ""),
}


def _has_code(text: str, language_id: str) -> bool:
    comment = "#" if language_id == "python" else "//"
    return any(ln.strip() and not ln.strip().startswith(comment) for ln in text.splitlines())


def check_fragment(language_id: str, new_code: str, *, wrap: Wrap = "none") -> SyntaxCheckResult:
    """``new_code`` 単体の構文を検証する（T-10 処理規約 3）。エラーが 0 なら ``ok``、あれば ``failed``。

    共通先頭空白を除いてから解析する。``wrap`` は断片を入れる容器:

    * ``"none"``: そのまま（モジュール直下の断片、``insert_before`` / ``insert_after``）。
    * ``"class_body"``: クラス本体の断片（``insert_member`` の親がクラス・構造体・impl）。Python は ``class _W:`` + 字下げ、
      TypeScript / JavaScript / C# は ``class _W { … }``、Rust は ``impl _W { … }``、C++ は ``class _W { … };``。
    * ``"module"``: モジュール内の断片（``insert_member`` の親がモジュール）。TypeScript は ``namespace``、Rust は ``mod``、
      C# は ``namespace`` で包む。Python・JavaScript・Go などモジュール直下がそのまま容器の言語は包まない。

    容器を持たない言語に ``class_body`` / ``module`` を指定したときは、包まずに解析する。位置は断片の座標に戻して返す。
    文法が無い言語は ``skipped``。
    """
    code = textwrap.dedent(new_code.replace("\r\n", "\n").replace("\r", "\n")).strip("\n")
    table = {"class_body": _CLASS_BODY, "module": _MODULE}.get(wrap, {})
    header, footer, indent = table.get(language_id, ("", "", ""))
    if header:
        body = textwrap.indent(code, indent) if indent else code
        lines = [header, body]
        if language_id == "python" and not _has_code(code, language_id):
            # 本体が空のクラスは Python では構文エラー。コメントだけの断片で誤検知しない
            lines.append(f"{indent}pass")
        if footer:
            lines.append(footer)
        source = "\n".join(lines) + "\n"
        header_lines = 1
    else:
        source = code + "\n"
        indent, header_lines = "", 0

    an = _analyze(language_id, source.encode("utf-8"))
    if an is None:
        return SyntaxCheckResult("skipped")
    if not an.errors and not an.timed_out:
        return SyntaxCheckResult("ok")
    n_lines = max(len(code.splitlines()), 1)

    def to_fragment(p: Position) -> Position:
        line = _clamp(p.line - header_lines, 0, n_lines - 1)
        col = max(0, p.col - len(indent)) if p.line >= header_lines else 0
        return Position(line, col)

    issues: list[SyntaxIssue] = []
    for e in an.errors[:MAX_REPORTED_ERRORS]:
        a, b = to_fragment(e.range.start), to_fragment(e.range.end)
        issues.append(SyntaxIssue(Range(a, max(a, b)), e.message))
    if an.timed_out:
        issues.append(
            SyntaxIssue(
                Range(Position(0, 0), Position(0, 0)), "syntax could not be verified: parse deadline exceeded"
            )
        )
    return SyntaxCheckResult(
        "failed", issues[:MAX_REPORTED_ERRORS], 0, len(an.errors) + (1 if an.timed_out else 0)
    )


# --- E_SYNTAX_INVALID の details ----------------------------------------------------------


def to_error_details(result: SyntaxCheckResult) -> dict[str, Any]:
    """``E_SYNTAX_INVALID`` の ``details``: ``{"errors": [{"line": 1 始まりの行, "message": ...}, ...]}``（仕様書 §5.8）。"""
    return {
        "errors": [
            {"line": issue.range.start.line + 1, "message": issue.message} for issue in result.new_errors
        ],
    }
