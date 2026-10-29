"""json 形式のレンダラ（仕様書 §5.4.1）。

【仮実装】署名は本実装と同じ。本実装は M1 タスク 4c の役が置き換える。
"""

from __future__ import annotations

import json

from lucianke.mcp.types import Rendered, RenderInput


def render(inp: RenderInput) -> Rendered:
    env = {"ok": inp.error is None, "tool": inp.call.tool, "_resolution_meta": inp.meta.to_json(),
           "data": None, "page": None, "budget": None, "timing_ms": inp.call.timing_ms,
           "warnings": list(inp.warnings), "error": inp.error.to_json() if inp.error else None}
    return Rendered(text=json.dumps(env, ensure_ascii=False), structured=env, is_error=inp.error is not None)
