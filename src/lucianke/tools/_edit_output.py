"""編集ツール（T-10 ``edit_symbol_body``・T-11 ``rename_symbol``・T-13 ``apply_code_action``）の応答の部品。

3 つのツールが同じ形で出す部分（diff の ``code`` レコード、診断の ``diag`` レコードと診断 1 行、短い sha256）をここに置く。
出力の形（compact の行・json の ``data``）を変えるときは、3 つのツールの契約テスト
（``tests/contract/test_T10_*.py``・``test_T11_*.py``・``test_T13_*.py``）が見ている。

* ``diff_record``: unified diff を 1 つの ``code`` レコードにする。ハンクが 2 つ以上あるときは、予算に収まる最大の数の
  先頭ハンクだけに縮められる（``shrink``。省いた数を ``diff truncated`` の note と ``omitted_hunks`` で伝える）。
  ツールは診断のレコードの **後ろ** に置く: パッカーは縮退版をページの最後の 1 件にする（仕様書 §5.6-5、BUG-0067）ので、
  diff が前にあると、縮んだ diff の後ろの診断が次ページ（編集ツールでは辿れない）に回る。
* ``diag_records``: ``added:`` / ``resolved:`` の見出しの後ろに診断の行を並べる ``diag`` レコード（0 件なら出さない）。
* ``diag_line``: 診断 1 件の compact の行（T-11 は ``head_kv`` の値として使う）。
* ``diagnostics_kv`` / ``diagnostics_json``: ``new_diagnostics:`` 行の値と json の ``data.new_diagnostics``。基準が不明なサーバーの
  編集後の診断（``present_after``。BUG-0098）は件数を ``?N`` で書き、``present_after:`` の見出しの下に並べる。
* ``short_sha``: 画面に出す sha256 の短縮（先頭 12 桁 + ``…``）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from lucianke.core.model import Confidence
from lucianke.core.records import Record
from lucianke.edit.verify import DiagDelta
from lucianke.tools._services import ToolServices, estimate_tokens

__all__ = [
    "SHA_SHOWN",
    "diag_line",
    "diag_records",
    "diagnostics_json",
    "diagnostics_kv",
    "diff_record",
    "short_sha",
]

SHA_SHOWN: Final = 12
_SEVERITY_LETTER: Final = {"error": "E", "warning": "W", "information": "I", "info": "I", "hint": "H"}


def short_sha(sha: str) -> str:
    return sha[:SHA_SHOWN] + "…"


def diag_line(svc: ToolServices, obj: Mapping[str, Any]) -> str:
    """診断 1 件（``path`` / ``start_line`` / ``start_col`` / ``message`` と、あれば ``severity`` / ``source`` / ``code``）の行。"""
    sev = _SEVERITY_LETTER.get(str(obj.get("severity", "")).lower(), "I")
    source, code = obj.get("source"), obj.get("code")
    pair: dict[str, Any] = {}
    if source:
        pair = {"source": str(source), "code": str(code) if code else "-"}
    return svc.lines.diag_line(
        sev,
        str(obj["path"]),
        int(obj["start_line"]),
        int(obj["start_col"]),
        message=str(obj["message"]),
        **pair,
    )


def diagnostics_kv(delta: DiagDelta) -> str:
    """``new_diagnostics:`` 行の値。``partial (+0 -1 ?2) checked=1 files`` の ``?2`` は ``present_after`` の件数（無ければ書かない）。"""
    if delta.status == "not_computed":
        return "not_computed"
    unknown = f" ?{len(delta.present_after)}" if delta.present_after else ""
    return f"{delta.status} (+{len(delta.added)} -{len(delta.resolved)}{unknown}) checked={len(delta.checked_files)} files"


def diagnostics_json(delta: DiagDelta) -> dict[str, Any]:
    """json の ``data.new_diagnostics``（仕様書 §11.5.0 の共通出力）。"""
    return {
        "status": delta.status,
        "added": [dict(o) for o in delta.added],
        "resolved": [dict(o) for o in delta.resolved],
        "present_after": [dict(o) for o in delta.present_after],
        "checked_files": [dict(o) for o in delta.checked_files],
    }


def diff_record(svc: ToolServices, diff: str, confidence: Confidence, priority: int = 0) -> Record:
    rows = diff.rstrip("\n").split("\n")
    hunk_starts = [i for i, row in enumerate(rows) if row.startswith("@@")]

    def make(count: int | None) -> Record:
        shown = rows if count is None or count >= len(hunk_starts) else rows[: hunk_starts[count]]
        lines = svc.lines.code_block(shown, lang="diff", with_line_numbers=False)
        omitted = 0 if count is None else max(0, len(hunk_starts) - count)
        if omitted:
            lines.append(svc.lines.note(f"diff truncated: {omitted} of {len(hunk_starts)} hunks omitted"))
        data: dict[str, Any] = {"section": "diff", "hunks": len(hunk_starts), "omitted_hunks": omitted}
        return Record(
            kind="code", key="diff", priority=priority, lines=tuple(lines), data=data, confidence=confidence, group="diff",
            shrink=shrink if count is None and len(hunk_starts) > 1 else None,
        )  # fmt: skip

    def shrink(budget: int) -> Record | None:
        for count in range(len(hunk_starts) - 1, 0, -1):  # 入る最大の数の先頭ハンクを残す
            rec = make(count)
            if estimate_tokens("\n".join(rec.lines)) <= budget:
                return rec
        return None

    return make(None)


def diag_records(
    svc: ToolServices, name: str, items: Sequence[Mapping[str, Any]], priority: int, confidence: Confidence
) -> list[Record]:
    if not items:
        return []
    objs = [dict(o) for o in items]
    lines = (
        f"{name}:",
        *(diag_line(svc, o) for o in objs),
    )  # 値のない kv 行（"added:"）の後ろに診断行が続く
    return [
        Record(
            kind="diag",
            key=name,
            priority=priority,
            lines=lines,
            data={"section": name, "count": len(objs)},
            confidence=confidence,
            group=name,
        )  # fmt: skip
    ]
