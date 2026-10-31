"""Edit-and-Verify: 診断差分（仕様書 §10.7、設計書 §7.7.6、§10.10 EP-7）。

このモジュールは **DB も LSP も触らない**。トランザクションが集めた「編集前の診断行」「編集後の診断行」「どのファイルが
揃ったか」を受け取り、差分 ``DiagDelta`` を作る。

* 同一性キーは ``(file, source, code, message, enclosing_urn)``。**行番号は使わない**（編集でずれるため）。
  同じキーの診断が複数あれば 1 つにまとめる（キーの辞書。``added`` は後ろの行を、``resolved`` は前の行を代表にする）。
* ``added`` = 編集後にあって編集前に無いキー、``resolved`` = 編集前にあって編集後に無いキー。
  したがって ``added`` はすべて現在の診断に含まれ、``resolved`` はいずれも含まれない（EP-7）。
* ``status``: 対象ファイルがすべて揃えば ``"complete"``、揃わなければ ``"pending"``（揃った分だけ差分に入れる）、
  診断を取りに行かなかったとき（``dry_run``）は ``"not_computed"``。揃っていても、編集前の診断（基準）が不明なサーバーがあれば
  ``"partial"``（下の BUG-0098）。
* **基準が不明なサーバー**（BUG-0098）: 編集の前に、あるサーバーがそのファイルのいまの内容の診断をまだ出していなかったとき
  （``unknown_baseline``）、そのサーバーの編集後の診断は ``added`` に入れず ``present_after`` に入れ、``resolved`` も判定しない
  （表に残っていた古い行を「解消」と言わない）。``checked_files`` の要素に ``unknown_baseline``（サーバー名の並び）を足し、
  ``warnings`` に何をすればよいかを書く。``"*"`` はそのファイルの LSP のサーバーすべて（索引の source〔``tree-sitter``・
  ``lucianke-*``〕は編集の前後で必ず揃うので含めない）。
* LSP 不在のときは Tree-sitter の構文エラー（``ERROR`` / ``MISSING``）の差分だけを返す（``structural_delta``）。
* 診断オブジェクトは T-08 と同形の辞書（行・列は 1 始まり。``diag_object``）。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from lucianke.core.limits import DIRECT_REFERRERS_MAX
from lucianke.core.model import Confidence, RelPath
from lucianke.index.queries import DiagnosticRow
from lucianke.syntax.types import ParsedFile

__all__ = [
    "ANY_LSP_SOURCE",
    "DiagDelta",
    "DiagKey",
    "Snapshot",
    "compute_delta",
    "diag_key",
    "diag_object",
    "is_index_source",
    "not_computed",
    "pending",
    "referrers_note",
    "structural_delta",
    "structural_rows",
]

DiagKey = tuple[str, str, str | None, str, str | None]
"""``(file, source, code, message, enclosing_urn)``。"""

Status = Literal["complete", "partial", "pending", "not_computed"]

ANY_LSP_SOURCE = "*"
"""``unknown_baseline`` / ``Snapshot.baseline`` のキー: そのファイルの LSP のサーバーすべて（サーバー名を区別できないとき）。"""


@dataclass(frozen=True, slots=True)
class DiagDelta:
    """診断差分（設計書 §7.7.6）。``checked_files`` の要素は ``{path, fresh}``（基準が不明なら ``unknown_baseline`` も）。

    ``present_after`` は、基準が不明なサーバーの編集後の診断（増えたかどうか分からないので ``added`` に入れない。BUG-0098）。
    """

    status: Status
    added: tuple[Mapping[str, Any], ...] = ()
    resolved: tuple[Mapping[str, Any], ...] = ()
    checked_files: tuple[Mapping[str, Any], ...] = ()
    confidence: Confidence = Confidence.EXACT
    warnings: tuple[str, ...] = field(default=())
    present_after: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class Snapshot:
    """編集前の診断（Resolved で取る）。``rows`` は対象ファイルと直接の参照元ファイルのパスごとの診断行。"""

    rows: Mapping[RelPath, tuple[DiagnosticRow, ...]] = field(default_factory=dict)
    referrers: tuple[RelPath, ...] = ()
    omitted_referrers: int = 0
    """上限 ``DIRECT_REFERRERS_MAX`` を超えて除いた参照元ファイルの数（``warnings`` に書く）。"""
    baseline: Mapping[RelPath, Mapping[str, bool]] | None = None
    """パス → サーバー名 → 「``rows`` を読む前に、そのサーバーがいまの内容の診断を出して表に書き終えていた」（BUG-0098）。
    ``None`` は基準を確かめていない（LSP 不在・``dry_run``・``diagnostics_wait_ms == 0``）。キー ``"*"`` はサーバーを区別できない口。"""
    failed: bool = False
    """編集前の診断を取れなかった（``rows`` は空）。どのファイルも基準が不明として扱う。"""

    @property
    def paths(self) -> tuple[RelPath, ...]:
        return tuple(self.rows)


def diag_key(row: DiagnosticRow) -> DiagKey:
    return (str(row.path), row.source, row.code, row.message, row.enclosing_urn)


def diag_object(row: DiagnosticRow) -> dict[str, Any]:
    """T-08 の診断オブジェクトと同形（行・列は 1 始まり）。"""
    sl, sc = row.range.start.to_external()
    el, ec = row.range.end.to_external()
    return {
        "path": str(row.path),
        "severity": row.severity,
        "source": row.source,
        "code": row.code,
        "start_line": sl,
        "start_col": sc,
        "end_line": el,
        "end_col": ec,
        "message": row.message,
        "enclosing_urn": row.enclosing_urn,
        "confidence_level": str(row.confidence.value),
    }


def referrers_note(omitted: int) -> str:
    return f"直接の参照元が上限 {DIRECT_REFERRERS_MAX} ファイルを超えたため、{omitted} ファイルは診断差分に含めていない"


def is_index_source(source: str) -> bool:
    """索引が書く ``source``（構文木・アダプタ）。編集の再索引で必ず揃うので、基準が不明になることはない。"""
    return source == SOURCE_TREE_SITTER or source.startswith("lucianke-")


def _is_unknown(source: str, unknown: Collection[str]) -> bool:
    return source in unknown or (ANY_LSP_SOURCE in unknown and not is_index_source(source))


def baseline_note(path: RelPath, unknown: Collection[str], present: int) -> str:
    names = "LSP" if ANY_LSP_SOURCE in unknown else ", ".join(sorted(unknown))
    return (
        f"{path}: {names} は編集前の内容の診断をまだ出していなかった（基準が不明）。編集後のその診断 {present} 件は"
        " added ではなく present_after に入れ、resolved は判定していない。present_after の位置が diff の変更箇所に"
        "掛かるかを見比べるか、get_diagnostics でいまの診断を確かめよ"
    )


def _by_key(rows: Iterable[DiagnosticRow]) -> dict[DiagKey, DiagnosticRow]:
    out: dict[DiagKey, DiagnosticRow] = {}
    for row in rows:
        out[diag_key(row)] = row
    return out


def compute_delta(
    before: Snapshot,
    after: Mapping[RelPath, Sequence[DiagnosticRow]],
    fresh: Mapping[RelPath, bool],
    *,
    confidence: Confidence = Confidence.EXACT,
    warnings: Sequence[str] = (),
    unknown_baseline: Mapping[RelPath, Collection[str]] | None = None,
) -> DiagDelta:
    """編集前 ``before`` と編集後 ``after`` の差分。

    ``fresh[path]`` が真のファイルだけを比べる（偽のファイルは古い診断のままなので差分に入れない）。
    ``unknown_baseline[path]`` のサーバー（``"*"`` は LSP すべて）の行は比べず、編集後の行を ``present_after`` に入れる（BUG-0098）。
    1 つでも ``fresh`` が偽なら ``"pending"``、揃っていて基準が不明なサーバーがあれば ``"partial"``、それ以外は ``"complete"``。
    """
    paths = list(dict.fromkeys([*before.rows, *fresh]))
    added: list[Mapping[str, Any]] = []
    resolved: list[Mapping[str, Any]] = []
    present: list[Mapping[str, Any]] = []
    checked: list[Mapping[str, Any]] = []
    notes = list(warnings)
    partial = False
    for path in paths:
        is_fresh = bool(fresh.get(path, False))
        unknown = frozenset((unknown_baseline or {}).get(path, ())) if is_fresh else frozenset()
        if ANY_LSP_SOURCE in unknown:
            unknown = frozenset({ANY_LSP_SOURCE})
        entry: dict[str, Any] = {"path": str(path), "fresh": is_fresh}
        if unknown:
            entry["unknown_baseline"] = sorted(unknown)
        checked.append(entry)
        if not is_fresh:
            continue
        old = {
            k: r for k, r in _by_key(before.rows.get(path, ())).items() if not _is_unknown(r.source, unknown)
        }
        new: dict[DiagKey, DiagnosticRow] = {}
        shown = 0
        for k, r in _by_key(after.get(path, ())).items():
            if _is_unknown(r.source, unknown):
                present.append(diag_object(r))
                shown += 1
            else:
                new[k] = r
        added.extend(diag_object(new[k]) for k in new if k not in old)
        resolved.extend(diag_object(old[k]) for k in old if k not in new)
        if unknown:
            partial = True
            notes.append(baseline_note(path, unknown, shown))
    if before.omitted_referrers:
        notes.append(referrers_note(before.omitted_referrers))
    status: Status = (
        "pending" if not all(c["fresh"] for c in checked) else "partial" if partial else "complete"
    )
    return DiagDelta(
        status, tuple(added), tuple(resolved), tuple(checked), confidence, tuple(notes), tuple(present)
    )


def not_computed(*, confidence: Confidence = Confidence.EXACT) -> DiagDelta:
    """診断を取りに行かなかった（``dry_run``）。"""
    return DiagDelta("not_computed", confidence=confidence)


def pending(
    paths: Iterable[RelPath], *, confidence: Confidence = Confidence.EXACT, warnings: Sequence[str] = ()
) -> DiagDelta:
    """待たずに応答する（``diagnostics_wait_ms == 0`` ほか）。エージェントは ``get_diagnostics`` で後追いする。"""
    checked = tuple({"path": str(p), "fresh": False} for p in dict.fromkeys(paths))
    return DiagDelta("pending", (), (), checked, confidence, tuple(warnings))


# --- LSP 不在: Tree-sitter の構文エラー差分 ---------------------------------------------------

_SNIPPET = 40
SOURCE_TREE_SITTER = "tree-sitter"


def structural_rows(path: RelPath, parsed: ParsedFile | None) -> list[DiagnosticRow]:
    """``parsed`` の ``ERROR`` / ``MISSING`` ノードを診断行にする（``parsed`` が ``None`` なら空）。"""
    if parsed is None:
        return []
    out: list[DiagnosticRow] = []
    for rng in parsed.error_ranges:
        a = parsed.line_index.position_to_offset(rng.start).value
        b = parsed.line_index.position_to_offset(rng.end).value
        text = parsed.source[a:b].decode("utf-8", "replace").strip()
        first = text.splitlines()[0] if text else ""
        snippet = first[:_SNIPPET] + ("..." if len(first) > _SNIPPET else "")
        message = f"syntax error near {snippet!r}" if snippet else "syntax error"
        out.append(
            DiagnosticRow(
                path=path,
                severity="error",
                source=SOURCE_TREE_SITTER,
                code=None,
                range=rng,
                message=message,
                enclosing_urn=None,
                confidence=Confidence.STRUCTURAL,
            )
        )
    return out


def structural_delta(
    changes: Mapping[RelPath, tuple[ParsedFile | None, ParsedFile | None]],
    *,
    warnings: Sequence[str] = (),
) -> DiagDelta:
    """LSP 不在のときの差分。``changes[path] = (編集前の解析, 編集後の解析)``。対象ファイルだけを比べ、常に ``complete``。"""
    before = Snapshot({p: tuple(structural_rows(p, old)) for p, (old, _new) in changes.items()})
    after = {p: structural_rows(p, new) for p, (_old, new) in changes.items()}
    fresh = dict.fromkeys(changes, True)
    return compute_delta(before, after, fresh, confidence=Confidence.STRUCTURAL, warnings=warnings)
