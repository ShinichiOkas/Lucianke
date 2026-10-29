"""json 形式のレンダラ（仕様書 §5.4.1、設計書 §7.2.4）。

エンベロープの全キー（``ok``, ``tool``, ``_resolution_meta``, ``data``, ``page``, ``budget``, ``timing_ms``,
``warnings``, ``error``）は成功・失敗の両方で必ず出す。失敗時に null になるのは ``data`` と（成功時の）``error`` だけ。
"""

from __future__ import annotations

import json
from typing import Any

from lucianke.mcp.envelope import collect_warnings
from lucianke.mcp.types import Rendered, RenderInput


def build_envelope(inp: RenderInput) -> dict[str, Any]:
    """§5.4.1 のエンベロープ（dict）を作る。

    * ``data`` は ``out.data_static`` に ``inp.records_key`` をキーとして ``[r.data for r in page.records]`` を加えたもの。
      失敗時は ``None``。ただし ``E_TIMEOUT`` の部分結果のように、``error`` と一緒に ``out`` ・``page`` が渡されたときは
      それを ``data`` に入れる（仕様書 §5.8、設計書 §9.1）。
    * ``page`` は ``{next_cursor, returned, total_estimate}``、``budget`` は ``{max_tokens, used_tokens_est, truncated}``。
      ページが無い（失敗）ときは ``returned = 0`` ・``total_estimate = 0`` ・``used_tokens_est = 0`` で埋める（MUST 項目のため）。
    """
    call = inp.call
    page = inp.page
    data: dict[str, Any] | None = None
    if inp.out is not None and page is not None:
        data = {**inp.out.data_static, inp.records_key: [dict(r.data) for r in page.records]}
    return {
        "ok": inp.error is None,
        "tool": call.tool,
        "_resolution_meta": inp.meta.to_json(),
        "data": data,
        "page": {
            "next_cursor": page.next_cursor if page is not None else None,
            "returned": len(page.records) if page is not None else 0,
            "total_estimate": page.total_estimate if page is not None else 0,
        },
        "budget": {
            "max_tokens": call.max_tokens,
            "used_tokens_est": page.used_tokens_est if page is not None else 0,
            "truncated": page.truncated if page is not None else False,
        },
        "timing_ms": call.timing_ms,
        "warnings": collect_warnings(inp),
        "error": inp.error.to_json() if inp.error is not None else None,
    }


def render(inp: RenderInput) -> Rendered:
    """json 形式に描画する。``text`` と ``structured`` は同じ dict から作る（``ensure_ascii=False``、区切りは詰める）。"""
    env = build_envelope(inp)
    text = json.dumps(env, ensure_ascii=False, separators=(",", ":"), default=str)
    return Rendered(text=text, structured=env, is_error=inp.error is not None)
