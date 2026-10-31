"""PaaD へのファイル単位の取り込み（設計書 §7.6.2、ADR-6、ADR-8。仕様書 §9.3、§9.4）。

このモジュールの関数はどれも **呼び出し側が開いたトランザクションの中で** 実行する前提で、``COMMIT`` /
``ROLLBACK`` / ``BEGIN`` を発行しない（``IndexWriter.submit(fn, bumps_version=..., label=...)`` に渡す関数の
中で呼ぶ）。トランザクションの外で呼ぶと ``RuntimeError``（半更新を読み手に見せないため。仕様書 §9.4 の MUST）。

``index`` は ``core`` 以外を import しない。解析結果は ``FileFacts`` に詰めて引数で受け取る。

位置の表記: ``FileFacts`` / ``RelationFact`` / ``DiagnosticFact`` が運ぶ位置は **内部表現（0 始まりの行・
0 始まりのコードポイント列。ADR-14）**。PaaD の列は公開面（``query_project_db``）なので外部表現
（1 始まり。仕様書 §5.10）で持ち、このモジュールが境界で変換する。シンボルの行範囲は ``start_line`` から
``end_line``（両端を含む）で、``SymbolRecord.to_json`` の ``range`` と同じ（``Range.line_span_external``）。
``sel_end_col`` は排他的な終端（``to_json`` の ``selection_range.end_col`` と同じ）。

ファイル内容が変わったときの ``relations`` の扱い（ADR-8）: そのファイルを ``source_file_id`` とする行は
``exact`` も含めて全削除して ``structural`` を入れ直す。``exact`` は次に LSP が答えたときに戻る。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from lucianke.core.model import Confidence, Kind, RelPath, SymbolRecord, Tier
from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, SCHEME, Urn, parse

__all__ = [
    "AttrDefFact",
    "DiagnosticFact",
    "FileFacts",
    "IngestStats",
    "NameCallFact",
    "ReexportFact",
    "RelationFact",
    "SuperRefFact",
    "SymbolExtra",
    "SyntaxErrorFact",
    "VarTypeFact",
    "TREE_SITTER_SOURCE",
    "SEVERITIES",
    "ensure_external_symbol",
    "ingest_file",
    "remove_file",
    "replace_lsp_diagnostics",
    "store_hover",
    "upsert_exact_relations",
    "upsert_workspace",
]

TREE_SITTER_SOURCE: Final = "tree-sitter"
SEVERITIES: Final = ("error", "warning", "information", "hint")
_LSP_SEVERITY: Final = {1: "error", 2: "warning", 3: "information", 4: "hint"}

SyntaxErrorFact = tuple[int, int, int, int, str]
"""``(start_line, start_col, end_line, end_col, message)``。内部表現（0 始まり）。"""

_WORKSPACE_COLUMNS: Final = (
    "workspace_id",
    "root_path",
    "python_env",
    "active_branch",
    "head_sha",
    "default_base_ref",
    "exclude_globs_json",
    "lsp_status_json",
    "indexed_at",
    "confidence_level",
)
_SYMBOL_COLUMNS: Final = (
    "parent_urn",
    "name",
    "qualified_name",
    "kind",
    "modifiers_json",
    "start_line",
    "end_line",
    "sel_line",
    "sel_start_col",
    "sel_end_col",
    "signature",
    "return_type",
    "doc_summary",
    "doc_full",
    "declared_in",
    "complexity",
    "scip_symbol",
    "body_sha256",
    "confidence_level",
)
_TYPE_RELATIONS: Final = frozenset({"INHERITS", "IMPLEMENTS", "TYPE_USES"})
# 外部シンボルの kind の強さ（強い証拠が勝つ。順序に依らず同じ結果にするため、後から来た強い証拠で昇格する）
_EXTERNAL_KIND_RANK: Final = {
    Kind.MODULE.value: 0,
    Kind.VARIABLE.value: 1,
    Kind.CONSTANT.value: 2,
    Kind.PROPERTY.value: 2,
    Kind.FUNCTION.value: 3,
    Kind.METHOD.value: 3,
    Kind.CLASS.value: 4,
}


# --- 入力の型 ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolExtra:
    """``SymbolRecord`` が持たない ``symbols`` の列（``body_sha256``・``doc_full``・``complexity``）。"""

    body_sha256: str | None = None
    doc_full: str | None = None
    complexity: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RelationFact:
    """``relations`` の 1 行分の事実。URN は ``Urn`` でも正準形の文字列でもよい。位置は 0 始まり。"""

    source_urn: Urn | str
    target_urn: Urn | str
    relation_kind: str
    call_site_line: int | None = None
    call_site_col: int | None = None
    use_kind: str | None = None
    guard_condition: str | None = None
    confidence: Confidence = Confidence.STRUCTURAL


@dataclass(frozen=True, slots=True, kw_only=True)
class NameCallFact:
    """内部表 ``_name_calls`` の 1 行分: 受け手の型が分からない属性呼び出し ``x.name(...)``（BUG-0071）と属性の読み書き
    ``x.name`` / ``x.name = v`` / ``x.name += 1``（``use_kind`` が ``r`` / ``w`` / ``rw``。BUG-0095）。位置は 0 始まり。"""

    source_urn: Urn | str
    name: str
    call_site_line: int
    call_site_col: int
    guard_condition: str | None = None
    use_kind: str = "call"


@dataclass(frozen=True, slots=True, kw_only=True)
class AttrDefFact:
    """内部表 ``_attr_defs`` の 1 行分: インスタンス属性の定義（``self.x = ...`` / ``this.x = ...``。シンボルにはならない）。
    型の分からない受け手の属性の参照を名前で結ぶときの候補（BUG-0095）。位置は 0 始まり。"""

    class_urn: str
    name: str
    line: int
    col: int


@dataclass(frozen=True, slots=True, kw_only=True)
class VarTypeFact:
    """内部表 ``_var_types`` の 1 行分: モジュール直下の変数の構文から分かる型（``settings = Settings()``。BUG-0094）。"""

    var_urn: str
    type_urn: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ReexportFact:
    """内部表 ``_reexports`` の 1 行分: Python のモジュール直下の import が束ねた名前（BUG-0100。``index/reexports.py``）。

    ``kind``: ``import``（``name`` を ``target_urn`` に束ねた。モジュールは ``#`` なしの URN、外部は ``lk://_ext/...``）・``star``
    （``from x import *`` のワークスペースの先。``name`` は ``*``、``target_urn`` は ``x`` のモジュール）・``all``（``__all__`` の名前。
    ``target_urn`` は空）。``ord`` は書いた順（同じ名前は後のものが勝つ）。
    """

    name: str
    target_urn: str
    kind: str
    ord: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SuperRefFact:
    """内部表 ``_member_refs`` の ``via_super = 1`` の 1 行分: ``super().name``（``super(C, self).name``・TS / JS の ``super.name``）の
    参照・呼び出し（BUG-0081）。``class_urn`` は ``C``（探し始めるのは ``C`` の C3 線形化の次のクラスから）。位置は 0 始まり。

    先は関係を整える段（``index/inherited.resolve_member_refs``）が決めて ``relations`` に行を作る。
    """

    source_urn: Urn | str
    class_urn: str
    name: str
    relation_kind: str
    use_kind: str | None
    call_site_line: int
    call_site_col: int
    guard_condition: str | None = None
    confidence: Confidence = Confidence.STRUCTURAL


@dataclass(frozen=True, slots=True, kw_only=True)
class DiagnosticFact:
    """``diagnostics`` の 1 行分（LSP の ``publishDiagnostics`` 用）。位置は 0 始まり。

    ``severity`` は ``error`` / ``warning`` / ``information`` / ``hint``。LSP の整数（1〜4）も受ける。
    """

    severity: str | int
    source: str
    message: str
    start_line: int
    start_col: int
    end_line: int | None = None
    end_col: int | None = None
    code: str | None = None
    quickfix_action_id: str | None = None
    confidence: Confidence = Confidence.EXACT


@dataclass(frozen=True, slots=True, kw_only=True)
class FileFacts:
    """取り込む 1 ファイル分の事実。``symbols`` の URN は ``workspace_id`` / ``path`` のファイルのものに限る。

    ``line_count`` は ``LineIndex.content_line_count`` の意味（空ファイル 0、末尾改行が無くても最終行を数える）。
    ``symbol_extras`` は URN の正準形の文字列をキーに ``body_sha256`` などを渡す。
    """

    workspace_id: str
    path: RelPath
    language: str
    tier: Tier
    line_count: int
    byte_size: int
    sha256: str
    is_test: bool
    confidence: Confidence
    symbols: Sequence[SymbolRecord] = field(default_factory=tuple)
    relations: Sequence[RelationFact] = field(default_factory=tuple)
    syntax_errors: Sequence[SyntaxErrorFact] = field(default_factory=tuple)
    symbol_extras: Mapping[str, SymbolExtra] = field(default_factory=dict)
    import_stems: Sequence[str] = field(default_factory=tuple)
    """import の解決で調べたパスの語幹（``engine/reresolve.py``）。後からファイルが作られた・消えたとき、これを import している側を引く。"""
    name_calls: Sequence[NameCallFact] = field(default_factory=tuple)
    """受け手の型が分からない属性呼び出し（``_name_calls``。BUG-0071）。``ingest_file`` がファイルごとに全置換する。"""
    super_refs: Sequence[SuperRefFact] = field(default_factory=tuple)
    """``super()`` 経由の参照（``_member_refs`` の ``via_super = 1``。BUG-0081）。``ingest_file`` がファイルごとに全置換する。"""
    attr_defs: Sequence[AttrDefFact] = field(default_factory=tuple)
    """インスタンス属性の定義（``_attr_defs``。BUG-0095）。``ingest_file`` がファイルごとに全置換する。"""
    var_types: Sequence[VarTypeFact] = field(default_factory=tuple)
    """モジュール直下の変数の型（``_var_types``。BUG-0094）。``ingest_file`` がファイルごとに全置換する。"""
    reexports: Sequence[ReexportFact] = field(default_factory=tuple)
    """Python のモジュール直下の import が束ねた名前・``*`` の先・``__all__``（``_reexports``。BUG-0100）。ファイルごとに全置換する。"""


@dataclass(frozen=True, slots=True)
class IngestStats:
    """``ingest_file`` が何を変えたか（ログと受入検査用）。"""

    file_id: int
    file_created: bool
    symbols_added: int = 0
    symbols_updated: int = 0
    symbols_unchanged: int = 0
    symbols_removed: int = 0
    symbols_duplicate: int = 0  # 同じ URN が 2 件以上あったため捨てた数（先に出たものを採る）
    relations_removed: int = 0
    relations_added: int = 0
    external_symbols_added: int = 0
    diagnostics_added: int = 0
    path: str = ""
    # 「公開する名前」が変わった名前（``*`` はすべて。``index/reexports.export_changes``）。それをたどって整えた側を取り込み直す（BUG-0100）
    export_names_changed: frozenset[str] = frozenset()

    @property
    def symbols_changed(self) -> bool:
        return bool(self.symbols_added or self.symbols_updated or self.symbols_removed)

    @property
    def symbol_set_changed(self) -> bool:
        """URN の集合が変わったか（足された・消えた）。import している側の ``relations`` を解き直す条件（``engine/reresolve.py``）。"""
        return bool(self.symbols_added or self.symbols_removed)


# --- 共通 -------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError(
            "ingest functions must run inside a transaction (call them from an IndexWriter job)"
        )


def _urn_str(urn: Urn | str) -> str:
    return urn if isinstance(urn, str) else str(urn)


def _line1(value: int | None) -> int | None:
    return None if value is None else value + 1


def _severity(value: str | int) -> str:
    if isinstance(value, int):
        try:
            return _LSP_SEVERITY[value]
        except KeyError:
            raise ValueError(f"unknown LSP severity: {value!r}") from None
    if value not in SEVERITIES:
        raise ValueError(f"unknown severity: {value!r}")
    return value


def _depth(qualified_name: str) -> int:
    return qualified_name.count(".")


def _innermost(spans: Iterable[tuple[str, int, int, int]], line1: int) -> str | None:
    """``(urn, start_line, end_line, depth)`` のうち ``line1``（1 始まり）を含む最内のものの URN。"""
    best: tuple[int, int, int] | None = None
    best_urn: str | None = None
    for urn, start, end, depth in spans:
        if start <= line1 <= end:
            key = (end - start, -start, -depth)
            if best is None or key < best:
                best, best_urn = key, urn
    return best_urn


def _file_id(conn: sqlite3.Connection, workspace_id: str, path: str) -> int | None:
    row = conn.execute(
        "SELECT file_id FROM files WHERE workspace_id = ? AND path = ?", (workspace_id, path)
    ).fetchone()
    return None if row is None else int(row[0])


def _symbol_spans(conn: sqlite3.Connection, file_id: int) -> list[tuple[str, int, int, int]]:
    rows = conn.execute(
        "SELECT urn, start_line, end_line, qualified_name FROM symbols WHERE file_id = ?", (file_id,)
    ).fetchall()
    return [(u, s, e, _depth(q)) for u, s, e, q in rows]


# --- workspaces -------------------------------------------------------------------------


def upsert_workspace(conn: sqlite3.Connection, ws_row: Mapping[str, object]) -> None:
    """``workspaces`` の行を作る・更新する。``workspace_id`` と ``root_path`` が必須。

    与えたキーだけを更新する（与えなかった列は既存の値を保つ）。未知のキーは ``ValueError``。
    """
    unknown = set(ws_row) - set(_WORKSPACE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown workspaces columns: {sorted(unknown)}")
    for key in ("workspace_id", "root_path"):
        if key not in ws_row:
            raise ValueError(f"workspaces row needs {key!r}")
    cols = [c for c in _WORKSPACE_COLUMNS if c in ws_row]
    updates = [c for c in cols if c != "workspace_id"]
    sql = f"INSERT INTO workspaces ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    sql += " ON CONFLICT(workspace_id) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in updates)
    conn.execute(sql, [ws_row[c] for c in cols])


# --- 外部シンボル -----------------------------------------------------------------------


def external_kind(qualified: str | None, relation_kinds: Iterable[str]) -> Kind:
    """外部シンボルの ``kind`` を、名前の形と使われ方（``relation_kind`` の集合）から決める。行の到着順に依らない。

    * セレクタ無し（``lk://_ext/os``）は ``Module``。
    * 型の位置で使われる（``INHERITS`` / ``IMPLEMENTS`` / ``TYPE_USES``）なら ``Class``。
    * 名前（修飾名の最後の語）が大文字で始まる: 呼ばれる（``CALLS``）か、全部が大文字でなければ ``Class``
      （``Decimal``、``Path``）。全部が大文字（``MAX_SIZE``）で呼ばれないなら ``Constant``。
    * それ以外: 呼ばれるなら、持ち主（修飾名の手前の語）が大文字で始まれば ``Method``（``Decimal.quantize``）、
      そうでなければ ``Function``（``os.getcwd``）。呼ばれないなら ``Property`` / ``Variable``。
    """
    if qualified is None:
        return Kind.MODULE
    kinds = set(relation_kinds)
    owner, _, name = qualified.rpartition(".")
    if kinds & _TYPE_RELATIONS:
        return Kind.CLASS
    called = "CALLS" in kinds
    owner_is_class = owner.rpartition(".")[2][:1].isupper()
    if name[:1].isupper():
        if called or not name.isupper():
            return Kind.CLASS
        return Kind.CONSTANT
    if called:
        return Kind.METHOD if owner_is_class else Kind.FUNCTION
    return Kind.PROPERTY if owner_is_class else Kind.VARIABLE


def ensure_external_symbol(conn: sqlite3.Connection, target_urn: str, relation_kinds: Iterable[str]) -> int:
    """``_ensure_external`` の公開名（関係を整える段が外部の型のメンバーに結ぶとき。``index/inherited.py``。BUG-0094）。"""
    return _ensure_external(conn, target_urn, relation_kinds)


def _ensure_external(conn: sqlite3.Connection, target_urn: str, relation_kinds: Iterable[str]) -> int:
    """``lk://_ext/...`` の URN に最小の ``symbols`` 行を作る（仕様書 §9.3）。作ったら 1、既存・対象外なら 0。

    ``kind`` は ``external_kind`` の規則。すでに行があるときは、より強い証拠（``Class`` > ``Function`` / ``Method`` >
    ``Constant`` / ``Property`` > ``Variable`` > ``Module``）が来たときだけ昇格する（ファイルの取り込み順に依らない）。
    """
    if not target_urn.startswith(f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"):
        return 0
    try:
        urn = parse(target_urn)
    except ValueError:
        return 0
    selector = urn.selector
    qualified = getattr(selector, "qualified", None)
    if isinstance(qualified, str):
        name = qualified.rpartition(".")[2]
    else:
        name = qualified = urn.path
        selector = None
    kind = external_kind(qualified if selector is not None else None, relation_kinds)
    cur = conn.execute(
        "INSERT OR IGNORE INTO symbols (urn, file_id, is_external, parent_urn, name, qualified_name, kind,"
        " start_line, end_line, confidence_level) VALUES (?, NULL, 1, NULL, ?, ?, ?, 1, 1, 'heuristic')",
        (target_urn, name, qualified, kind.value),
    )
    if cur.rowcount > 0:
        return 1
    row = conn.execute("SELECT kind FROM symbols WHERE urn = ? AND is_external = 1", (target_urn,)).fetchone()
    if row is not None and _EXTERNAL_KIND_RANK.get(kind.value, 0) > _EXTERNAL_KIND_RANK.get(row[0], 0):
        conn.execute(
            "UPDATE symbols SET kind = ? WHERE urn = ? AND is_external = 1", (kind.value, target_urn)
        )
    return 0


# --- relations --------------------------------------------------------------------------


def _insert_relations(
    conn: sqlite3.Connection,
    facts: Iterable[RelationFact],
    *,
    source_file_id: int | None,
    force_confidence: Confidence | None = None,
    resolve_source_file: bool = False,
) -> tuple[int, int]:
    """``relations`` に行を入れる。``(入れた行数, 作った外部シンボル数)``。完全に同じ行は 1 つにまとめる。"""
    target_files: dict[str, int | None] = {}
    source_files: dict[str, int | None] = {}
    seen: set[tuple[object, ...]] = set()
    external = 0
    rows: list[tuple[object, ...]] = []
    facts = list(facts)
    kinds_of_target: dict[str, set[str]] = {}  # 外部シンボルの kind を使われ方の全体から決めるため
    for fact in facts:
        tgt_text = _urn_str(fact.target_urn)
        if tgt_text.startswith(f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"):
            kinds_of_target.setdefault(tgt_text, set()).add(fact.relation_kind)
    for fact in facts:
        src, tgt = _urn_str(fact.source_urn), _urn_str(fact.target_urn)
        conf = force_confidence or fact.confidence
        line, col = _line1(fact.call_site_line), _line1(fact.call_site_col)
        key = (src, tgt, fact.relation_kind, line, col)
        if key in seen:
            continue
        seen.add(key)
        if tgt not in target_files:
            external += _ensure_external(conn, tgt, kinds_of_target.get(tgt, (fact.relation_kind,)))
            row = conn.execute("SELECT file_id FROM symbols WHERE urn = ?", (tgt,)).fetchone()
            target_files[tgt] = None if row is None else row[0]
        sfid = source_file_id
        if resolve_source_file:
            if src not in source_files:
                source_files[src] = _file_of_urn(conn, src)
            sfid = source_files[src]
        rows.append(
            (
                src,
                tgt,
                fact.relation_kind,
                sfid,
                target_files[tgt],
                line,
                col,
                fact.use_kind,
                fact.guard_condition,
                conf.value,
            )
        )
    conn.executemany(
        "INSERT INTO relations (source_urn, target_urn, relation_kind, source_file_id, target_file_id,"
        " call_site_line, call_site_col, use_kind, guard_condition, confidence_level)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows), external


def _file_of_urn(conn: sqlite3.Connection, urn: str) -> int | None:
    """URN の持ち主のファイル。``symbols`` に行があればその ``file_id``、無ければ URN の path から引く。"""
    row = conn.execute("SELECT file_id FROM symbols WHERE urn = ?", (urn,)).fetchone()
    if row is not None and row[0] is not None:
        return int(row[0])
    try:
        parsed = parse(urn)
    except ValueError:
        return None
    if parsed.is_external:
        return None
    return _file_id(conn, parsed.workspace_id, parsed.path)


def upsert_exact_relations(conn: sqlite3.Connection, rows: Sequence[RelationFact]) -> int:
    """LSP が答えた関係を ``exact`` で入れる（仕様書 §9.4、ADR-6）。入れた行数を返す。

    ``rows`` に現れる ``(source_urn, target_urn, relation_kind)`` ごとに、既にある行を水準を問わず消してから
    入れる（``structural`` を ``exact`` が置き換える。LSP の再取得で ``exact`` 行が重複しない）。つまり
    ``rows`` は、その組については LSP が返した完全な集合として扱う。``source_file_id`` は ``source_urn`` の
    持ち主のファイルにする（そのファイルの再取り込みで ``exact`` も消えるように。ADR-8）。
    ``rows`` の ``confidence`` に関わらず ``exact`` で保存する。``data_version`` は呼び出し側のジョブが進める。
    """
    _require_transaction(conn)
    triples = {(_urn_str(r.source_urn), _urn_str(r.target_urn), r.relation_kind) for r in rows}
    for src, tgt, kind in triples:
        conn.execute(
            "DELETE FROM relations WHERE source_urn = ? AND target_urn = ? AND relation_kind = ?",
            (src, tgt, kind),
        )
    added, _ = _insert_relations(
        conn, rows, source_file_id=None, force_confidence=Confidence.EXACT, resolve_source_file=True
    )
    return added


# --- diagnostics ------------------------------------------------------------------------


def _insert_diagnostics(
    conn: sqlite3.Connection, file_id: int, diags: Iterable[DiagnosticFact], observed_at: str
) -> int:
    spans = _symbol_spans(conn, file_id)
    rows = []
    for d in diags:
        rows.append(
            (
                file_id,
                _innermost(spans, d.start_line + 1),
                _severity(d.severity),
                d.source,
                d.code,
                d.start_line + 1,
                d.start_col + 1,
                _line1(d.end_line),
                _line1(d.end_col),
                d.message,
                d.quickfix_action_id,
                observed_at,
                d.confidence.value,
            )
        )
    conn.executemany(
        "INSERT INTO diagnostics (file_id, enclosing_urn, severity, source, code, start_line, start_col,"
        " end_line, end_col, message, quickfix_action_id, observed_at, confidence_level)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def replace_lsp_diagnostics(
    conn: sqlite3.Connection,
    workspace_id: str,
    path: str,
    diags: Sequence[DiagnosticFact],
    *,
    source: str | None = None,
    now: str | None = None,
) -> int:
    """LSP の ``publishDiagnostics`` を反映する。``source`` が ``tree-sitter`` 以外の行を全置換して、入れた行数を返す。

    ``source`` を与えると、その ``source`` の行だけを置換する（pyright と ruff が別々に発行するとき、
    片方がもう片方の診断を消さないように）。ファイルが ``files`` に無ければ何もしない（0）。
    診断だけの更新なので ``data_version`` を進めない（ジョブは ``bumps_version=False`` で投入する。ADR-8）。
    """
    _require_transaction(conn)
    file_id = _file_id(conn, workspace_id, path)
    if file_id is None:
        return 0
    if source is None:
        conn.execute(
            "DELETE FROM diagnostics WHERE file_id = ? AND source != ?", (file_id, TREE_SITTER_SOURCE)
        )
    else:
        if source == TREE_SITTER_SOURCE:
            raise ValueError("tree-sitter diagnostics are replaced by ingest_file")
        conn.execute("DELETE FROM diagnostics WHERE file_id = ? AND source = ?", (file_id, source))
    kept = [d for d in diags if d.source != TREE_SITTER_SOURCE and (source is None or d.source == source)]
    return _insert_diagnostics(conn, file_id, kept, now or _now())


# --- symbols ----------------------------------------------------------------------------


def _parent_urns(records: Sequence[SymbolRecord]) -> dict[str, str | None]:
    """修飾名（``A.B.c``）の親（``A.B``）を、同じファイルの中から探す。無ければ ``None``。

    同じ修飾名が複数あるとき（オーバーロード・再定義）は、子の範囲を含むもののうち最も狭いもの、
    無ければ子より前で最も近いもの、それも無ければ最初のものを選ぶ。
    """
    by_qualified: dict[str, list[SymbolRecord]] = {}
    for rec in records:
        by_qualified.setdefault(rec.qualified_name, []).append(rec)
    out: dict[str, str | None] = {}
    for rec in records:
        parent_q = rec.qualified_name.rpartition(".")[0]
        urn = str(rec.urn)
        candidates = [c for c in by_qualified.get(parent_q, ()) if c.urn != rec.urn] if parent_q else []
        if not candidates:
            out[urn] = None
            continue
        containing = [c for c in candidates if c.range.contains_range(rec.range)]
        if containing:
            chosen = max(containing, key=lambda c: c.range.start)
        else:
            before = [c for c in candidates if c.range.start <= rec.range.start]
            chosen = max(before, key=lambda c: c.range.start) if before else candidates[0]
        out[urn] = str(chosen.urn)
    return out


def _symbol_row(rec: SymbolRecord, parent_urn: str | None, extra: SymbolExtra | None) -> tuple[object, ...]:
    start_line, end_line = rec.range.line_span_external()
    sel = rec.selection
    extra = extra or SymbolExtra()
    return (
        parent_urn,
        rec.name,
        rec.qualified_name,
        rec.kind.value,
        json.dumps(list(rec.modifiers), ensure_ascii=False),
        start_line,
        end_line,
        None if sel is None else sel.start.line + 1,
        None if sel is None else sel.start.col + 1,
        None if sel is None else sel.end.col + 1,
        rec.signature,
        rec.return_type,
        rec.doc_summary,
        extra.doc_full,
        rec.declared_in,
        extra.complexity,
        rec.scip_symbol,
        extra.body_sha256,
        rec.confidence.value,
    )


def _check_symbol_belongs(facts: FileFacts, rec: SymbolRecord) -> None:
    if rec.urn.workspace_id != facts.workspace_id or rec.urn.path != str(facts.path):
        raise ValueError(f"symbol {rec.urn} does not belong to {facts.workspace_id}:{facts.path}")


def _sync_symbols(conn: sqlite3.Connection, file_id: int, facts: FileFacts) -> tuple[int, int, int, int, int]:
    """``symbols`` を URN で突き合わせる。``(追加, 更新, 変化なし, 削除, 重複)``。"""
    unique: dict[str, SymbolRecord] = {}
    duplicates = 0
    for rec in facts.symbols:
        _check_symbol_belongs(facts, rec)
        key = str(rec.urn)
        if key in unique:
            duplicates += 1
            continue
        unique[key] = rec
    records = list(unique.values())
    parents = _parent_urns(records)
    new_rows = {
        urn: _symbol_row(rec, parents[urn], facts.symbol_extras.get(urn)) for urn, rec in unique.items()
    }

    select_cols = ", ".join(_SYMBOL_COLUMNS)
    existing: dict[str, tuple[object, ...]] = {
        r[0]: tuple(r[1:])
        for r in conn.execute(f"SELECT urn, {select_cols} FROM symbols WHERE file_id = ?", (file_id,))
    }

    removed = [u for u in existing if u not in new_rows]
    if removed:
        removed_set = set(removed)
        # 残る子が消える親を指したままだと、親の DELETE の CASCADE で巻き込まれる。先に外す。
        for urn, row in existing.items():
            if urn not in removed_set and row[0] in removed_set:
                conn.execute("UPDATE symbols SET parent_urn = NULL WHERE urn = ?", (urn,))
        # relations には symbols への FK が無い。消える URN を指す行は明示的に消す（設計書 §7.6.2）。
        for urn in removed:
            conn.execute("DELETE FROM relations WHERE target_urn = ? OR source_urn = ?", (urn, urn))
            conn.execute("DELETE FROM symbols WHERE urn = ?", (urn,))

    added_urns = sorted(
        (u for u in new_rows if u not in existing),
        key=lambda u: (_depth(unique[u].qualified_name), unique[u].range.start),
    )
    insert_sql = (
        f"INSERT INTO symbols (urn, file_id, is_external, {select_cols})"
        f" VALUES (?, ?, 0, {', '.join('?' * len(_SYMBOL_COLUMNS))})"
    )
    for urn in added_urns:  # 親が先（修飾名の '.' が少ない順）。parent_urn の FK を満たす
        conn.execute(insert_sql, (urn, file_id, *new_rows[urn]))
        # 先に取り込まれていた他ファイルからの関係の target_file_id を埋める
        conn.execute(
            "UPDATE relations SET target_file_id = ? WHERE target_urn = ? AND target_file_id IS NOT ?",
            (file_id, urn, file_id),
        )

    update_sql = "UPDATE symbols SET " + ", ".join(f"{c} = ?" for c in _SYMBOL_COLUMNS) + " WHERE urn = ?"
    updated = unchanged = 0
    for urn, row in new_rows.items():
        if urn in existing:
            if existing[urn] == row:
                unchanged += 1
            else:
                conn.execute(update_sql, (*row, urn))
                updated += 1
    return len(added_urns), updated, unchanged, len(removed), duplicates


# --- ファイル ---------------------------------------------------------------------------


def ingest_file(conn: sqlite3.Connection, facts: FileFacts, *, now: str | None = None) -> IngestStats:
    """1 ファイル分の事実を PaaD に反映する。呼び出し側のトランザクションの中で呼ぶ（モジュールの docstring）。

    1. ``files`` を upsert する（``file_id`` を保つ。``pagerank_score`` と ``last_modified_commit`` は触らない）。
    2. ``symbols`` を URN で突き合わせる: 新規は INSERT、変化したものは UPDATE（``relations`` を保つ）、
       消えたものは DELETE（``parent_urn`` の子は CASCADE。その URN を ``target_urn`` に持つ ``relations`` も消す）。
    3. ``source_file_id`` がこのファイルの ``relations`` を ``exact`` も含めて全削除し、``structural`` を入れ直す。
       ``lk://_ext/`` の目標には外部シンボルの最小の行を作る。
    4. ``diagnostics`` のうち ``source = 'tree-sitter'`` の行をこのファイルについて全置換する。
    """
    _require_transaction(conn)
    stamp = now or _now()
    path = str(facts.path)

    file_id = _file_id(conn, facts.workspace_id, path)
    created = file_id is None
    values = (
        facts.language,
        int(facts.tier),
        facts.line_count,
        facts.byte_size,
        facts.sha256,
        int(facts.is_test),
        stamp,
        facts.confidence.value,
    )
    if file_id is None:
        cur = conn.execute(
            "INSERT INTO files (workspace_id, path, language, tier, line_count, byte_size, sha256, is_test,"
            " indexed_at, confidence_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (facts.workspace_id, path, *values),
        )
        file_id = int(cur.lastrowid or 0)
    else:
        conn.execute(
            "UPDATE files SET language = ?, tier = ?, line_count = ?, byte_size = ?, sha256 = ?, is_test = ?,"
            " indexed_at = ?, confidence_level = ? WHERE file_id = ?",
            (*values, file_id),
        )

    # 循環 import を避けるため遅延（reexports が ingest を使う）
    from lucianke.index.reexports import export_changes

    exports_changed = export_changes(
        conn, None if created else file_id, (rec.qualified_name for rec in facts.symbols), facts.reexports
    )
    added, updated, unchanged, removed, duplicate = _sync_symbols(conn, file_id, facts)

    relations_removed = conn.execute("DELETE FROM relations WHERE source_file_id = ?", (file_id,)).rowcount
    relations_added, external = _insert_relations(conn, facts.relations, source_file_id=file_id)

    conn.execute("DELETE FROM diagnostics WHERE file_id = ? AND source = ?", (file_id, TREE_SITTER_SOURCE))
    syntax = [
        DiagnosticFact(
            severity="error",
            source=TREE_SITTER_SOURCE,
            message=message,
            start_line=sl,
            start_col=sc,
            end_line=el,
            end_col=ec,
            confidence=Confidence.STRUCTURAL,
        )
        for sl, sc, el, ec, message in facts.syntax_errors
    ]
    diagnostics_added = _insert_diagnostics(conn, file_id, syntax, stamp)

    conn.execute("DELETE FROM _import_probes WHERE file_id = ?", (file_id,))
    if facts.import_stems:
        conn.executemany(
            "INSERT OR IGNORE INTO _import_probes (file_id, stem) VALUES (?, ?)",
            [(file_id, stem) for stem in facts.import_stems],
        )

    # 継承したメンバーへの参照の退避行（BUG-0075。index/inherited.py）は relations と一緒に作り直す（整えで退避し直す）
    conn.execute("DELETE FROM _member_refs WHERE file_id = ?", (file_id,))
    if facts.super_refs:  # super() 経由の参照（BUG-0081）: 先は整えで基底の C3 線形化から決める
        conn.executemany(
            "INSERT INTO _member_refs (file_id, source_urn, class_urn, name, relation_kind, use_kind,"
            " call_site_line, call_site_col, guard_condition, confidence_level, via_super)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            [
                (
                    file_id,
                    _urn_str(r.source_urn),
                    r.class_urn,
                    r.name,
                    r.relation_kind,
                    r.use_kind,
                    r.call_site_line + 1,
                    r.call_site_col + 1,
                    r.guard_condition,
                    r.confidence.value,
                )
                for r in facts.super_refs
            ],
        )
    conn.execute("DELETE FROM _name_calls WHERE file_id = ?", (file_id,))
    if facts.name_calls:
        conn.executemany(
            "INSERT INTO _name_calls (file_id, source_urn, name, call_site_line, call_site_col, guard_condition, use_kind)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    file_id,
                    _urn_str(c.source_urn),
                    c.name,
                    c.call_site_line + 1,
                    c.call_site_col + 1,
                    c.guard_condition,
                    c.use_kind,
                )
                for c in facts.name_calls
            ],
        )
    conn.execute("DELETE FROM _attr_defs WHERE file_id = ?", (file_id,))
    if facts.attr_defs:
        conn.executemany(
            "INSERT INTO _attr_defs (file_id, class_urn, name, line, col) VALUES (?, ?, ?, ?, ?)",
            [(file_id, a.class_urn, a.name, a.line + 1, a.col + 1) for a in facts.attr_defs],
        )
    conn.execute("DELETE FROM _var_types WHERE file_id = ?", (file_id,))
    if facts.var_types:
        conn.executemany(
            "INSERT OR REPLACE INTO _var_types (file_id, var_urn, type_urn) VALUES (?, ?, ?)",
            [(file_id, v.var_urn, v.type_urn) for v in facts.var_types],
        )
    conn.execute("DELETE FROM _reexports WHERE file_id = ?", (file_id,))
    if facts.reexports:
        conn.executemany(
            "INSERT INTO _reexports (file_id, name, target_urn, kind, ord) VALUES (?, ?, ?, ?, ?)",
            [(file_id, r.name, r.target_urn, r.kind, r.ord) for r in facts.reexports],
        )
    # 再び公開した名前をたどって引いた (ファイル, 名前) は、関係と一緒に作り直す（整えが足す。index/reexports.py）
    conn.execute("DELETE FROM _reexport_deps WHERE file_id = ?", (file_id,))

    return IngestStats(
        file_id=file_id,
        path=path,
        file_created=created,
        symbols_added=added,
        symbols_updated=updated,
        symbols_unchanged=unchanged,
        symbols_removed=removed,
        symbols_duplicate=duplicate,
        relations_removed=max(relations_removed, 0),
        relations_added=relations_added,
        external_symbols_added=external,
        diagnostics_added=diagnostics_added,
        export_names_changed=exports_changed,
    )


def remove_file(conn: sqlite3.Connection, workspace_id: str, path: str) -> bool:
    """ファイルを PaaD から消す（ファイル削除・除外への変更）。消したら ``True``、元から無ければ ``False``。

    ``files`` の DELETE の CASCADE で ``symbols``・``diagnostics``・``source_file_id`` がこのファイルの
    ``relations`` が落ちる。FK の無い ``relations.target_urn`` がこのファイルのシンボルを指す行は先に明示的に消す。
    """
    _require_transaction(conn)
    file_id = _file_id(conn, workspace_id, path)
    if file_id is None:
        return False
    urns = [r[0] for r in conn.execute("SELECT urn FROM symbols WHERE file_id = ?", (file_id,))]
    for urn in urns:
        conn.execute("DELETE FROM relations WHERE target_urn = ? OR source_urn = ?", (urn, urn))
    conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
    return True


# --- _hover_cache -----------------------------------------------------------------------


def store_hover(
    conn: sqlite3.Connection, urn: str, sha256: str, hover_text: str, *, now: str | None = None
) -> None:
    """LSP の hover 文を ``_hover_cache`` に書く（T-04。読みは ``queries.cached_hover``）。

    同じ ``urn`` の古い版（``sha256`` が違う行）は消してから入れる。内部テーブルへの書き込みなので、呼び出し側の
    ジョブは ``bumps_version=False`` で出す（``data_version`` を進めない）。
    """
    _require_transaction(conn)
    conn.execute("DELETE FROM _hover_cache WHERE urn = ? AND sha256 <> ?", (urn, sha256))
    conn.execute(
        "INSERT OR REPLACE INTO _hover_cache(urn, sha256, hover_text, cached_at) VALUES (?, ?, ?, ?)",
        (urn, sha256, hover_text, now or _now()),
    )
