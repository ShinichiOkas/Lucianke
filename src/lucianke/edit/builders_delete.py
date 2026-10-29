"""T-10 ``delete_symbol`` の ``plan_builder``（仕様書 T-10 処理規約 2・3、§10.8、設計書 §7.7.7）。

``replace_body`` / 挿入のビルダー（``builders.py`` / ``builders_insert.py``）とは別のモジュール。

削除範囲の規則（決定的）:

1. 核は ``syntax.boundaries.definition_range``（デコレータ・属性・``export`` などの修飾子・直前の Doc コメント・
   直前の空行で隔てられていない通常コメント・末尾の同一行コメントを含む。T-05 が ``context_lines: 0`` で返す範囲と同じ）。
2. 削除は **行単位**。核の最初の行から最後の行までを、行の終端ごと取り除く。
3. 空行: 核の **直前** の連続空行を一緒に取り除く。直前に空行が無い（ファイルや本体の先頭のメンバー）ときだけ、
   **直後** の連続空行を取り除く。これで隣り合う定義の間の空行は削除前と同じ数のまま残る
   （Python のトップレベルの 2 行、クラス内の 1 行。「言語慣習で余剰になる分」だけが消える）。
4. 取り除く範囲がファイルの終端に届き、かつファイルが最終改行で終わらない（``has_final_newline`` が偽）ときは、
   手前の行の行末から取り除く（最終改行を勝手に足さない。仕様書 T-10 処理規約 4(d)）。
5. 核が行の途中から始まる（同じ行に別のコードが先行する。``class A { foo() {} }`` の 1 行）ときは行単位にせず、
   核の範囲だけを取り除く（先行する空白は一緒に取り除く）。結果の構文が壊れるなら構文検証が拒否する。

Python で親の本体が空になる削除（クラスや関数の唯一の文を消す）は ``E_SYNTAX_INVALID``。サーバーは ``pass`` を補わない
（仕様書 T-10 処理規約 3、MUST NOT）。``details.hint`` に理由を書く。波括弧の言語は空の本体が妥当なので、そのまま消す。

CRLF・BOM は ``LineIndex`` の行境界で切るのでそのまま保たれる（``apply_hunks`` が BOM を飛ばす）。
文法の無いファイル（Tier 4）の範囲決定は ctags とブロック推定の仕事で、ここでは未対応（``E_UNSUPPORTED``）。
"""

from __future__ import annotations

from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Position, Range, TargetRef
from lucianke.core.textpos import has_final_newline
from lucianke.edit.plan import Hunk, Plan
from lucianke.edit.transaction import PlanBuilder, PlanContext
from lucianke.syntax.boundaries import definition_range
from lucianke.syntax.types import ParsedFile

__all__ = ["EMPTY_BODY_HINT", "delete_symbol_builder"]

EMPTY_BODY_HINT = (
    "`delete_symbol` は親の本体を空にする。Python では空の本体が構文エラーになり、サーバーは `pass` を補わない。"
    "親ごと削除するか、本体に別の文を残す"
)

# Python で、定義ノードを包んで 1 つの文として本体に並べる節点（核はこの外側）。
_PYTHON_WRAPPERS = frozenset({"decorated_definition"})


def _empty_body_error(ctx: PlanContext) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_SYNTAX_INVALID,
        "deleting the symbol would leave its parent body empty",
        details={
            "hint": EMPTY_BODY_HINT,
            "reason": "parent_body_would_be_empty",
            "path": str(ctx.path),
            "target_urn": str(ctx.symbol.urn) if ctx.symbol is not None else None,
        },
    )


def _python_body_becomes_empty(node: Any) -> bool:
    """``node``（Python の定義ノード）が本体（``block``）の唯一の文なら真。コメントは文に数えない。"""
    core = node
    while core.parent is not None and core.parent.type in _PYTHON_WRAPPERS:
        core = core.parent
    parent = core.parent
    if parent is None or parent.type != "block":
        return False  # モジュール直下は空でもよい
    return not [c for c in parent.named_children if c.id != core.id and c.type != "comment"]


def _is_blank(parsed: ParsedFile, line: int) -> bool:
    return not parsed.line_index.line_bytes(line).strip()


def _line_end(parsed: ParsedFile, line: int) -> Position:
    return Position(line, parsed.line_index.line_length(line))


def deletion_range(parsed: ParsedFile, core: Range) -> Range:
    """核 ``core``（``definition_range`` の結果）から、実際に取り除く範囲を決める（モジュールの docstring が規則）。"""
    li = parsed.line_index
    first, last = core.start.line, core.end.line
    line_start = li.line_start(first)
    prefix = parsed.source[line_start : li.position_to_offset(core.start).value]
    if prefix.strip():  # 規則 5: 同じ行に別のコードが先行する
        keep = len(prefix.rstrip().decode("utf-8", "surrogateescape"))
        return Range(Position(first, keep), core.end)

    total = li.content_line_count  # 実在する行の数（最終改行の後ろの空の行は数えない）
    a = first
    while a > 0 and _is_blank(parsed, a - 1):
        a -= 1
    b = last + 1
    if a == first:  # 直前に空行が無い: 直後の空行を取り除く
        while b < total and _is_blank(parsed, b):
            b += 1

    if b >= total and a > 0 and not has_final_newline(parsed.source):
        return Range(_line_end(parsed, a - 1), _line_end(parsed, total - 1))  # 規則 4
    return Range(Position(a, 0), Position(b, 0))


def delete_symbol_builder(target: TargetRef) -> PlanBuilder:
    """T-10 ``delete_symbol``: ``target`` の定義ノードを取り除く 1 hunk の計画を作る。"""
    del target  # 解決は EditTransaction が済ませ、PlanContext.symbol / node に入っている

    async def build(ctx: PlanContext) -> Plan:
        if ctx.symbol is None or ctx.node is None or ctx.parsed is None:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"delete_symbol needs a syntax tree and a resolved symbol: {ctx.path}",
                details={"reason": "no_symbol_range", "path": str(ctx.path), "language_id": ctx.language_id},
            )
        if ctx.language_id == "python" and _python_body_becomes_empty(ctx.node):
            raise _empty_body_error(ctx)
        core = definition_range(ctx.node, ctx.parsed, language_id=ctx.language_id)
        return Plan(
            hunks=(Hunk(ctx.path, deletion_range(ctx.parsed, core), "", "delete_symbol"),),
            touched_paths=(ctx.path,),
            resolved_shas={ctx.path: ctx.sha256},
            target_urns=(str(ctx.symbol.urn),),
            expects_syntax=True,
        )

    return build
