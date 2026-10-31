"""対象のメソッドに届くかもしれないのに結ばなかった継承メンバー・``super()`` の参照を、理由つきで要約する（BUG-0081。仕様書
T-06 処理規約 9、T-07 処理規約 11）。

``super().save()``（BUG-0081）や ``self.helper()``（BUG-0075）は、受け手のクラスの C3 線形化で、中身の分からない外部の基底
（``threading.Thread`` など）やクラスでない基底に先に当たると結ばない（その基底が同名を定義しているかもしれない）。その行は内部表
``_member_refs`` に結べないまま残る（``index/inherited.blocked_member_refs``）。対象のクラスがその基底の後ろにあれば、その呼び出しは
対象に届くかもしれないので、``structural`` の T-06 / T-07 は件数・理由・先頭の位置を ``warnings`` と ``data.unattributed_calls`` に書く
（受け手の型は構文から分かっているので、名前の一致〔BUG-0071〕の「型不明・曖昧」には回さない）。

受け手の型が対象のクラスの基底で、その線形化のどこにも同名のメンバー・インスタンス属性が無い参照（``b: Base`` の ``b.skipped`` で
``skipped`` は派生クラスの ``self.skipped = 0``。``reason = base_receiver``。BUG-0122）も同じく書く。実体が対象のクラスのインスタンスなら
対象に届くが、構文からは分からない（黙って捨てない）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from lucianke.core.model import SymbolRecord
from lucianke.core.urn import CodeSel, parse
from lucianke.index.inherited import BlockedRef, blocked_member_refs

__all__ = ["BlockedNotes", "blocked_notes"]

SAMPLE_MAX = 5  # warnings に並べる位置の数
DATA_SAMPLE_MAX = 20  # data.unattributed_calls[].sites に並べる位置の数

_REASON_TEXT = {
    "external_base": "an external base whose members are unknown",
    "unknown_base": "a base that is not a known class (an alias or an unresolved name)",
    "base_receiver": "the receiver is typed as a base class that has no member or instance attribute of that name",
}


@dataclass(slots=True)
class BlockedNotes:
    warnings: list[str] = field(default_factory=list)
    unattributed: list[dict[str, Any]] = field(default_factory=list)  # data.unattributed_calls の要素


def _short(urn: str) -> str:
    try:
        parsed = parse(urn)
    except ValueError:
        return urn
    sel = parsed.selector
    return sel.qualified if isinstance(sel, CodeSel) else urn


def _use_of(ref: BlockedRef) -> str:
    return ref.use_kind or ("call" if ref.relation_kind == "CALLS" else "r")


def blocked_notes(
    conn: sqlite3.Connection,
    workspace_id: str,
    sym: SymbolRecord,
    enclosing: Callable[[str], str | None],
    *,
    include_tests: bool = True,
    use_kinds: Collection[str] | None = None,
    in_scope: Collection[str] | None = None,
    known: Collection[tuple[str, int, int]] = (),
) -> BlockedNotes:
    """対象 ``sym`` に届くかもしれない、結ばなかった参照の要約（理由ごとに 1 件の警告と ``unattributed_calls`` の要素）。

    ``known`` は答えに既にある ``(パス, 行, 列)``（1 始まり。LSP などが別の経路で答えた位置は数えない）。
    """
    notes = BlockedNotes()
    rows = [
        r
        for r in blocked_member_refs(conn, workspace_id, str(sym.urn))
        if (include_tests or not r.is_test)
        and (not use_kinds or _use_of(r) in use_kinds)
        and (in_scope is None or r.path in in_scope)
        and (r.path, r.line, r.col) not in known
    ]
    groups: dict[str, list[BlockedRef]] = {}
    for r in rows:
        groups.setdefault(r.reason, []).append(r)
    for reason in sorted(groups):
        group = groups[reason]
        what = "call site(s)" if all(r.relation_kind == "CALLS" for r in group) else "site(s)"
        if reason == "base_receiver":
            bases = sorted({_short(r.class_urn) for r in group})
            shown = ", ".join(bases[:3]) + (f", … {len(bases) - 3} more" if len(bases) > 3 else "")
            notes.warnings.append(
                f"{len(group)} {what} of .{sym.name} may reach {sym.qualified_name} but are not attributed: the receiver is"
                f" typed as a base class ({shown}) that has no member or instance attribute {sym.name!r}, so only an"
                f" instance of a subclass can have it; e.g. {_sample(group)} (data.unattributed_calls)"
            )
        else:
            via = sorted({f"{_short(r.class_urn)} -> {_short(r.blocker)}" for r in group})
            shown = ", ".join(via[:3]) + (f", … {len(via) - 3} more" if len(via) > 3 else "")
            notes.warnings.append(
                f"{len(group)} {what} of .{sym.name} (super() or an inherited member) may reach {sym.qualified_name} but"
                f" are not attributed: {_REASON_TEXT.get(reason, reason)} comes first in the receiver class's MRO and may"
                f" define {sym.name!r} ({shown}); e.g. {_sample(group)} (data.unattributed_calls)"
            )
        notes.unattributed.append(
            {
                "name": sym.name,
                "total": len(group),
                "reason": reason,
                "sites": _sites(group, enclosing),
            }
        )
    return notes


def _sample(rows: Sequence[BlockedRef]) -> str:
    shown = ", ".join(f"{r.path}:{r.line}" for r in rows[:SAMPLE_MAX])
    return shown + (f", … {len(rows) - SAMPLE_MAX} more" if len(rows) > SAMPLE_MAX else "")


def _sites(rows: Sequence[BlockedRef], enclosing: Callable[[str], str | None]) -> list[dict[str, Any]]:
    return [
        {
            "path": r.path,
            "line": r.line,
            "col": r.col,
            "enclosing_name": enclosing(r.source_urn),
            "is_test": r.is_test,
            "use": _use_of(r),
            "via_super": r.via_super,
            "receiver_class": _short(r.class_urn),
            "blocked_by": r.blocker,
        }
        for r in rows[:DATA_SAMPLE_MAX]
    ]
