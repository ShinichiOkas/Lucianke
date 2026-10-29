"""Compact DSL のレンダラ（仕様書 §6.1、§5.4.2）。

【仮実装】署名は本実装と同じ。本実装（行種ごとの組み立て関数を含む）は M1 タスク 4c の役が置き換える。
"""

from __future__ import annotations

from lucianke.mcp.types import Rendered, RenderInput


def render(inp: RenderInput) -> Rendered:
    c = inp.call
    if inp.error is not None:
        head = f"# {c.tool} error {inp.error.code}"
        return Rendered(text=f"{head}\n! {inp.error.message}\n", is_error=True)
    used = inp.page.used_tokens_est if inp.page else 0
    head = (f"# {c.tool} ok conf={inp.meta.confidence_level} lsp={inp.meta.lsp_status} "
            f"rev={c.revision} ws={c.workspace_id or '-'} ~{used}/{c.max_tokens}tok")
    lines = [head]
    if inp.out is not None:
        lines += [f"{k}: {v}" for k, v in inp.out.head_kv]
    if inp.page is not None:
        for r in inp.page.records:
            lines += list(r.lines)
    return Rendered(text="\n".join(lines) + "\n")
