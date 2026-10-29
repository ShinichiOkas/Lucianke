"""編集ツールの ``plan_builder``（設計書 §7.7.7）。トランザクションに渡す「計画を作る関数」の最小の実装。

いまは T-10 の ``replace_body`` だけ。他の操作（``insert_*`` / ``delete_symbol``）とツール本体は後の担当が足す。

* 範囲は ``syntax.boundaries.definition_range``（デコレータ・属性・直前の Doc コメント・末尾の同一行コメントを含む）。
* 本文は ``normalize_new_code``（インデント・改行の正規化）。範囲は行末までなので、末尾の改行 1 つを落として 1 hunk にする。
* ``replace_body`` は **シグネチャを含む定義全体** を渡す（仕様書 §10.9-4）。本体の文だけを渡したときは ``E_SYNTAX_INVALID``。
  Python と Rust は定義が必ず開始語（``def`` / ``class`` / ``fn`` など）で始まるので、開始語が無ければ構文検証を待たずに拒否する。
  それ以外の言語は ``new_code`` 単体の構文検証（``check_fragment``）で判定し、失敗したときに ``details.hint`` を添える。
* 文法の無いファイル（Tier 4）の範囲決定は ctags とブロック推定（``syntax/heuristic``）の仕事で、ここでは未対応（``E_UNSUPPORTED``）。
"""

from __future__ import annotations

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import NEW_CODE_MAX_BYTES
from lucianke.core.model import TargetRef
from lucianke.core.textpos import detect_newline
from lucianke.edit.normalize import normalize_new_code
from lucianke.edit.plan import Hunk, Plan
from lucianke.edit.syntax_check import check_fragment, to_error_details
from lucianke.edit.transaction import PlanBuilder, PlanContext
from lucianke.syntax.boundaries import definition_range

__all__ = ["REPLACE_BODY_HINT", "replace_body_builder"]

REPLACE_BODY_HINT = "`operation: replace_body` はシグネチャを含む定義全体を渡す"

# 定義の先頭に必ず現れる開始語（装飾・コメントの行は読み飛ばす）。キーが無い言語は構文検証だけで判定する。
_DEFINITION_STARTS: dict[str, tuple[str, ...]] = {
    "python": ("def ", "async def ", "class "),
    "rust": (
        "fn ",
        "pub ",
        "async fn ",
        "const fn ",
        "unsafe fn ",
        "extern ",
        "struct ",
        "enum ",
        "impl",
        "trait ",
        "type ",
        "mod ",
        "union ",
    ),
}
_SKIPPED_LINE_STARTS = ("@", "#", "//", "/*", "*", '"""')


def _starts_definition(new_code: str, language_id: str) -> bool | None:
    """先頭の意味のある行が定義の開始語で始まるか。判定できない言語は ``None``。"""
    starts = _DEFINITION_STARTS.get(language_id)
    if starts is None:
        return None
    for raw in new_code.splitlines():
        line = raw.strip()
        if not line or line.startswith(_SKIPPED_LINE_STARTS):
            continue
        return line.startswith(starts)
    return False


def _syntax_error(message: str, **details: object) -> LuciankeError:
    return LuciankeError(ErrorCode.E_SYNTAX_INVALID, message, details={"hint": REPLACE_BODY_HINT, **details})


def replace_body_builder(target: TargetRef, new_code: str) -> PlanBuilder:
    """T-10 ``replace_body``: ``target`` の定義ノード全体を ``new_code`` で置き換える 1 hunk の計画を作る。"""
    del target  # 解決は EditTransaction が済ませ、PlanContext.symbol / node に入っている

    async def build(ctx: PlanContext) -> Plan:
        if len(new_code.encode("utf-8")) > NEW_CODE_MAX_BYTES:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT,
                f"new_code is larger than {NEW_CODE_MAX_BYTES} bytes",
                details={"param": "new_code", "reason": "too_large"},
            )
        if ctx.symbol is None or ctx.node is None or ctx.parsed is None:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"replace_body needs a syntax tree and a resolved symbol: {ctx.path}",
                details={"reason": "no_symbol_range", "path": str(ctx.path), "language_id": ctx.language_id},
            )
        rng = definition_range(ctx.node, ctx.parsed, language_id=ctx.language_id)
        text = normalize_new_code(
            new_code,
            target_source=ctx.source,
            base_column=rng.start.col,
            language_id=ctx.language_id,
            mode="replace",
        )
        if _starts_definition(new_code, ctx.language_id) is False:
            raise _syntax_error(
                "new_code does not start with a definition (pass the whole definition, signature included)",
                path=str(ctx.path),
            )
        fragment = check_fragment(ctx.language_id, new_code)
        if fragment.status == "failed":
            raise _syntax_error(
                "new_code is not valid syntax", path=str(ctx.path), **to_error_details(fragment)
            )
        newline = detect_newline(ctx.source)
        text = text.removesuffix(newline)  # 範囲は行末までなので、行の終端は元のものが残る
        return Plan(
            hunks=(Hunk(ctx.path, rng, text, "replace_body"),),
            touched_paths=(ctx.path,),
            resolved_shas={ctx.path: ctx.sha256},
            target_urns=(str(ctx.symbol.urn),),
            expects_syntax=True,
        )

    return build
