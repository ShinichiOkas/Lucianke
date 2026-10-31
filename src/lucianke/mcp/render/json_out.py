"""json 形式のレンダラ（仕様書 §5.4.1、設計書 §7.2.4）。

エンベロープの全キー（``ok``, ``tool``, ``_resolution_meta``, ``data``, ``page``, ``budget``, ``timing_ms``,
``warnings``, ``error``）は成功・失敗の両方で必ず出す。失敗時に null になるのは ``data`` と（成功時の）``error`` だけ。
"""

from __future__ import annotations

import json
from typing import Any

from lucianke.core.records import Record, json_record_text
from lucianke.mcp.envelope import collect_warnings
from lucianke.mcp.types import Rendered, RenderInput


def _dumps(value: Any) -> str:
    """エンベロープ全体と同じ書き方（``ensure_ascii=False``、区切りは詰める、未知の型は ``str``）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def record_text(rec: Record) -> str:
    """json で描かれるときの 1 レコードの本文（``data`` 配列の 1 要素と、続く区切りの ``,``）。

    パッカーが json の応答を見積もるときの単位（仕様書 §5.7: ``used_tokens_est`` は応答テキスト全体）。json は ``Record.lines`` ではなく
    ``Record.data`` を描くので、compact の行で数えると実際の何分の一かに過小になる（BUG-0017）。区切りの ``,`` まで含めるのは、
    compact の行末の改行と同じ理由（レコードごとの数を足した和が、応答全体を数え直した値以上になるようにする）。
    """
    return json_record_text(rec)


def trailer_text(summary: str | None, cursor: str | None, remaining: int) -> str:
    """続きがあるときに json へ増える本文。json に要約の行は無く（``page`` は ``next_cursor`` / ``returned`` /
    ``total_estimate`` だけ）、増えるのは ``next_cursor`` の文字列だけ（``null`` の代わりに入る）。"""
    return _dumps(cursor) if cursor else ""


def build_envelope(inp: RenderInput) -> dict[str, Any]:
    """§5.4.1 のエンベロープ（dict）を作る。

    * ``data`` は ``out.data_static`` に ``inp.records_key`` をキーとして ``[r.data for r in page.records]`` を加えたもの。
      補足の欄のレコード（``Record.section``。BUG-0107）は本体の配列に入れず、``data[section]`` の配列に置く（そのページに
      その欄のレコードがあるときだけ）。失敗時は ``None``。ただし ``E_TIMEOUT`` の部分結果のように、``error`` と一緒に
      ``out`` ・``page`` が渡されたときはそれを ``data`` に入れる（仕様書 §5.8、設計書 §9.1）。
    * 付随行（``Record.attached``。BUG-0136）は入ったレコードの分だけ ``data[キー]`` に連ねる（``data_static`` に同じキーの列が
      あればその後ろ）。
    * ``page`` は ``{next_cursor, returned, total_estimate}``、``budget`` は ``{max_tokens, used_tokens_est, truncated}``。
      ページが無い（失敗）ときは ``returned = 0`` ・``total_estimate = 0`` ・``used_tokens_est = 0`` で埋める（MUST 項目のため）。
    """
    call = inp.call
    page = inp.page
    data: dict[str, Any] | None = None
    if inp.out is not None and page is not None:
        sections: dict[str, list[dict[str, Any]]] = {}
        for r in page.records:
            if r.section is not None:
                sections.setdefault(r.section, []).append(dict(r.data))
        data = {
            **inp.out.data_static,
            inp.records_key: [dict(r.data) for r in page.records if r.section is None],
            **sections,
        }
        attached: dict[str, list[dict[str, Any]]] = {}
        seen: dict[str, set[str]] = {}
        for r in page.records:  # 付随行（``Record.attached``）。同一の行は最初の 1 件だけ
            for key, rows in (r.attached or {}).items():
                for row in rows:
                    dumped = _dumps(row)
                    if dumped in seen.setdefault(key, set()):
                        continue
                    seen[key].add(dumped)
                    attached.setdefault(key, []).append(dict(row))
        for key, rows in attached.items():
            data[key] = [*data.get(key, []), *rows]
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
    return Rendered(text=_dumps(env), structured=env, is_error=inp.error is not None)
