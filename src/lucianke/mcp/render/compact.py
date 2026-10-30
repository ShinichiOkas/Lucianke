"""Compact DSL のレンダラ（仕様書 §6.1、§5.4.2、§8.4、設計書 §7.2.4）。

行種ごとの組み立て関数（``symbol_line`` ・``ref_line`` ・``file_block_header`` など）の実体は
``lucianke.core.compact_lines``（``syntax`` の骨格と共有するため ``core`` に置いた）。このモジュールは同じ名前で
再公開するので、``from lucianke.mcp.render.compact import symbol_line`` や、合成ルートが ``tools`` へ注入する
``lines`` モジュールとしての ``compact.symbol_line(...)`` は従来どおり使える。

ツールと骨格（``syntax``）は文法を直接書かず、これらの関数を呼んで ``Record.lines`` を作る（設計書 §7.2.4）。
文法への適合は ``tests/contract/compact_grammar.py`` の検査器で確かめる。規約（1 始まり・改行の畳み込み・200 文字の省略・語彙）は
``core/compact_lines.py`` の docstring を参照。
"""

from __future__ import annotations

from lucianke.core.compact_lines import (
    ATTR_KEYS,
    KINDS,
    LINE_LIMIT,
    MODIFIERS,
    SEVERITIES,
    USES,
    attr_line,
    banner,
    banner_lines,
    candidate_line,
    cap_signature,
    code_block,
    diag_line,
    error_header_line,
    file_block_header,
    header_line,
    import_line,
    kv_line,
    note,
    path_header,
    ref_line,
    step_line,
    symbol_line,
    table,
    trailer_more,
    trailer_summary,
    tree_line,
    urn_lines,
)
from lucianke.mcp.budget import trailer_lines
from lucianke.mcp.envelope import collect_warnings
from lucianke.mcp.types import Rendered, RenderInput

__all__ = [
    "ATTR_KEYS",
    "KINDS",
    "LINE_LIMIT",
    "MODIFIERS",
    "SEVERITIES",
    "USES",
    "attr_line",
    "banner",
    "banner_lines",
    "candidate_line",
    "cap_signature",
    "code_block",
    "diag_line",
    "error_header_line",
    "file_block_header",
    "header_line",
    "import_line",
    "kv_line",
    "note",
    "path_header",
    "ref_line",
    "render",
    "step_line",
    "symbol_line",
    "table",
    "trailer_more",
    "trailer_summary",
    "tree_line",
    "urn_lines",
]


# --- レンダラ ---------------------------------------------------------------------------------


def _trailer_lines(inp: RenderInput) -> list[str]:
    """``# summary:`` と ``# more:``。続きがあるときは ``budget.trailer_lines`` の出力そのまま。

    パッカーはその関数の出力でトレーラを数えて ``Page.used_tokens_est`` を決めるので、文言を変えない
    （変えると予算の保証が崩れる）。続きが無いのに ``summary`` があるときだけ、要約行を単独で出す。
    """
    page = inp.page
    if page is None:
        return []
    if page.next_cursor:
        return list(trailer_lines(page.summary, page.next_cursor, page.remaining))
    return [trailer_summary(page.summary)] if page.summary else []


def _record_lines(inp: RenderInput) -> list[str]:
    if inp.page is None:
        return []
    return [line for rec in inp.page.records for line in rec.lines]


def render(inp: RenderInput) -> Rendered:
    """compact 形式に描画する（仕様書 §5.4.2。設計書 §7.2.4）。

    成功: ヘッダ → 降格の警告行 → ``warnings`` の ``! `` 行 → ``head_kv`` の kv 行 → レコードの ``lines`` →
    ``# summary:`` → ``# more:``。警告は ``inp.warnings`` と ``inp.page.warnings`` を重複なしで併合する。
    失敗: ``# <tool> error <CODE>`` → ``! <message>`` → ``E_TARGET_AMBIGUOUS`` なら候補（1 行 1 件）→ 警告。
    ``out`` と ``page`` が一緒に渡されたとき（``E_TIMEOUT`` の部分結果）は、続けてレコードとトレーラも出す。
    """
    call = inp.call
    warnings = [line for w in collect_warnings(inp) for line in banner(w)]
    if inp.error is not None:
        lines = [error_header_line(call.tool, inp.error.code), *banner(inp.error.message)]
        lines += [candidate_line(c) for c in inp.error.candidates]
        lines += warnings
        if inp.out is not None and inp.page is not None:
            lines += _record_lines(inp) + _trailer_lines(inp)
        return Rendered(text="\n".join(lines) + "\n", is_error=True)
    used = inp.page.used_tokens_est if inp.page is not None else 0
    lines = [
        header_line(
            call.tool,
            conf=inp.meta.confidence_level,
            lsp=inp.meta.lsp_status,
            rev=call.revision,
            ws=call.workspace_id,
            used_tokens=used,
            max_tokens=call.max_tokens,
            lsp_progress=inp.meta.lsp_progress,
        ),
        *banner_lines(inp.meta),
        *warnings,
    ]
    if inp.out is not None:
        lines += [kv_line(k, v) for k, v in inp.out.head_kv]
    lines += _record_lines(inp) + _trailer_lines(inp)
    return Rendered(text="\n".join(lines) + "\n")
