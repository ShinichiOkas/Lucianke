"""基底クラスから継承したメンバーへの参照を、索引の ``INHERITS`` 行をたどって結ぶ（BUG-0075。設計書 §7.6.2、仕様書 T-06 処理規約 2・T-07 処理規約 2）。

構文木の抽出器（``syntax/relations.py``）は、受け手の型が分かる属性アクセス（``self.helper()``・``cls.make()``・``s: Sub`` の
``s.helper()``・``Sub().helper()``）の先を ``<受け手のクラス>.helper`` まで決める。そのクラスに ``helper`` が無く、基底クラスが
別のファイルにあると、目標はどの ``symbols`` にも無い URN になり、関係を整える段（``engine/indexer._settle_relations``）が捨てていた。

* **退避**（``park_member_refs``）: 整えるファイルの行のうち、目標が ``symbols`` に無く、その目標の「クラスの部分」
  （``path#Sub.helper`` の ``path#Sub``）がワークスペースのクラス・インターフェースであるもの（``CALLS`` と ``REFERENCES``）を、
  内部表 ``_member_refs``（受け手のクラスとメンバー名と、行を作り直すのに要る列）に移す。
* **結ぶ**（``resolve_member_refs``）: 受け手のクラスの C3 線形化（``core/mro.py``。基底は ``relations`` の ``INHERITS`` 行を
  書いた順に読む）を先頭から見て、最初にそのメンバー名を持つクラスのシンボルを先にした ``relations`` の行（``structural``）を作る。
  派生クラスが上書きしていれば派生クラスのもの（線形化の先頭はクラス自身）。中身の分からない外部の基底（``object``・``ABC``・
  ``Generic``・``Protocol`` 以外の ``lk://_ext/...``）やクラスでない基底（``Base = make_base()`` の別名）に先に当たったら結ばない
  （その基底が同名を定義しているかもしれない。誤って結ぶより落とす）。見つからなければ行を作らない（``_member_refs`` には残す）。
* **追従**: 結ぶ範囲は、整えるファイルの退避行と、整えるファイルのクラスとその（推移的な）派生クラスを受け手にする退避行。
  基底のメンバーを足した・消した・上書きを足した・基底の並びを変えた、はどれもそのクラスのファイルの取り込み → 整えで結び直る
  （import している側の取り込み直しは ``engine/reresolve.py``。直接の派生クラスのファイルは基底を import しているので一緒に整う）。
  前に結んだ先は ``_member_refs.resolved_urn`` に覚え、変わったら ``relations`` の行を差し替える。

``_member_refs`` はファイルの取り込み（``ingest_file``）で全置換され、ファイルの削除で CASCADE で消える。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Final

from lucianke.core.model import Kind
from lucianke.core.mro import TRANSPARENT_EXTERNAL_BASES, c3_linearize
from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, SCHEME, CodeSel, parse

__all__ = ["CLASS_KINDS", "park_member_refs", "resolve_member_refs"]

CLASS_KINDS: Final = frozenset(
    {Kind.CLASS.value, Kind.INTERFACE.value, Kind.STRUCT.value, Kind.TRAIT.value, Kind.ENUM.value}
)
"""受け手・基底になれるシンボルの種別。"""

_EXTERNAL_PREFIX: Final = f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"
_PARKED_KINDS: Final = ("CALLS", "REFERENCES")
_KIND_LIST: Final = ", ".join(f"'{k}'" for k in sorted(CLASS_KINDS))


def _split_member(target: str) -> tuple[str, str] | None:
    """``lk://ws/path#Cls.name`` → ``("lk://ws/path#Cls", "name")``。外部・arity / sighash つき・修飾の無い URN は ``None``。"""
    try:
        urn = parse(target)
    except ValueError:
        return None
    sel = urn.selector
    if urn.is_external or not isinstance(sel, CodeSel) or sel.arity is not None or sel.sighash is not None:
        return None
    owner, dot, name = sel.qualified.rpartition(".")
    if not dot or not owner or not name:
        return None
    try:
        return str(urn.with_selector(CodeSel(owner))), name
    except ValueError:
        return None


def park_member_refs(conn: sqlite3.Connection, file_id: int) -> int:
    """``file_id`` の行のうち、目標が無く、目標のクラスの部分がワークスペースのクラスである行を ``_member_refs`` に移す。移した行数。"""
    marks = ", ".join("?" * len(_PARKED_KINDS))
    rows = conn.execute(
        "SELECT relation_id, source_urn, target_urn, relation_kind, call_site_line, call_site_col, use_kind,"
        " guard_condition, confidence_level FROM relations"
        f" WHERE source_file_id = ? AND relation_kind IN ({marks}) AND instr(target_urn, '#') > 0"
        " AND target_urn NOT IN (SELECT urn FROM symbols)",
        (file_id, *_PARKED_KINDS),
    ).fetchall()
    is_class: dict[str, bool] = {}
    parked: list[tuple[object, ...]] = []
    ids: list[tuple[int]] = []
    for relation_id, source, target, kind, line, col, use_kind, guard, confidence in rows:
        split = _split_member(target)
        if split is None or line is None or col is None:
            continue
        owner, name = split
        if owner not in is_class:
            hit = conn.execute(
                "SELECT kind FROM symbols WHERE urn = ? AND file_id IS NOT NULL", (owner,)
            ).fetchone()
            is_class[owner] = hit is not None and hit[0] in CLASS_KINDS
        if not is_class[owner]:
            continue
        parked.append((file_id, source, owner, name, kind, use_kind, line, col, guard, confidence))
        ids.append((relation_id,))
    if parked:
        conn.executemany(
            "INSERT INTO _member_refs (file_id, source_urn, class_urn, name, relation_kind, use_kind,"
            " call_site_line, call_site_col, guard_condition, confidence_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            parked,
        )
        conn.executemany("DELETE FROM relations WHERE relation_id = ?", ids)
    return len(parked)


class _Resolver:
    """C3 の順にメンバーを探す（1 回の整えの中で、基底・メンバー・線形化を覚えておく）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._bases: dict[str, list[str]] = {}
        self._kinds: dict[str, str | None] = {}
        self._orders: dict[str, list[str] | None] = {}
        self._found: dict[tuple[str, str], str | None] = {}

    def _kind(self, urn: str) -> str | None:
        if urn not in self._kinds:
            row = self._conn.execute("SELECT kind FROM symbols WHERE urn = ?", (urn,)).fetchone()
            self._kinds[urn] = None if row is None else str(row[0])
        return self._kinds[urn]

    def bases(self, cls: str) -> list[str]:
        """``cls`` の直接の基底（``INHERITS`` 行を書いた順）。目標が ``symbols`` に無いワークスペースの行は数えない（整えで消える行）。"""
        if cls.startswith(_EXTERNAL_PREFIX):
            return []
        if cls not in self._bases:
            out: list[str] = []
            for (target,) in self._conn.execute(
                "SELECT target_urn FROM relations WHERE source_urn = ? AND relation_kind = 'INHERITS'"
                " ORDER BY call_site_line, call_site_col, relation_id",
                (cls,),
            ):
                text = str(target)
                if text in out or text == cls:
                    continue
                if text.startswith(_EXTERNAL_PREFIX) or self._kind(text) is not None:
                    out.append(text)
            self._bases[cls] = out
        return self._bases[cls]

    def _member(self, cls: str, name: str) -> str | None:
        urn = parse(cls)
        sel = urn.selector
        if not isinstance(sel, CodeSel):
            return None
        row = self._conn.execute(
            "SELECT s.urn FROM symbols s JOIN files f ON f.file_id = s.file_id"
            " WHERE f.workspace_id = ? AND f.path = ? AND s.qualified_name = ?"
            " ORDER BY s.start_line DESC, s.urn DESC LIMIT 1",  # オーバーロードは最後の宣言（実装）。_redirect_overloads と同じ
            (urn.workspace_id, urn.path, f"{sel.qualified}.{name}"),
        ).fetchone()
        return None if row is None else str(row[0])

    def resolve(self, cls: str, name: str) -> str | None:
        key = (cls, name)
        if key not in self._found:
            self._found[key] = self._resolve(cls, name)
        return self._found[key]

    def _resolve(self, cls: str, name: str) -> str | None:
        if cls not in self._orders:
            self._orders[cls] = c3_linearize(cls, self.bases)
        order = self._orders[cls]
        if order is None:
            return None  # 循環・矛盾した並び
        for c in order:
            if c.startswith(_EXTERNAL_PREFIX):
                if c in TRANSPARENT_EXTERNAL_BASES:
                    continue
                return None  # 中身の分からない外部の基底: 同名を定義しているかもしれない
            if self._kind(c) not in CLASS_KINDS:
                return None  # クラスでない基底（別名など）: 実体が分からない
            hit = self._member(c, name)
            if hit is not None:
                return hit
        return None


def resolve_member_refs(conn: sqlite3.Connection, file_ids: Sequence[int]) -> int:
    """``file_ids`` の退避行と、``file_ids`` のクラスとその派生クラスを受け手にする退避行を結び直す。差し替えた行数。"""
    if not file_ids:
        return 0
    ids = json.dumps(sorted({int(i) for i in file_ids}))
    rows = conn.execute(
        "WITH RECURSIVE"
        "  seed(urn) AS (SELECT urn FROM symbols WHERE file_id IN (SELECT value FROM json_each(:ids))"
        f"   AND kind IN ({_KIND_LIST})),"
        "  sub(urn) AS (SELECT urn FROM seed UNION"
        "   SELECT r.source_urn FROM relations r JOIN sub ON r.target_urn = sub.urn WHERE r.relation_kind = 'INHERITS')"
        " SELECT rowid, file_id, source_urn, class_urn, name, relation_kind, use_kind, call_site_line, call_site_col,"
        "  guard_condition, confidence_level, resolved_urn FROM _member_refs"
        " WHERE file_id IN (SELECT value FROM json_each(:ids)) OR class_urn IN (SELECT urn FROM sub)",
        {"ids": ids},
    ).fetchall()
    resolver = _Resolver(conn)
    changed = 0
    for rowid, file_id, source, cls, name, kind, use_kind, line, col, guard, confidence, old in rows:
        new = resolver.resolve(str(cls), str(name))
        site = (file_id, source, kind, line, col)
        touched = False
        if old is not None and old != new:
            # 前の先の行（LSP が exact で書き戻した行も。先が変わったのでその位置については古い）
            conn.execute(
                "DELETE FROM relations WHERE source_file_id = ? AND source_urn = ? AND relation_kind = ?"
                " AND call_site_line = ? AND call_site_col = ? AND target_urn = ?",
                (*site, old),
            )
            touched = True
        if new is not None and not conn.execute(
            "SELECT 1 FROM relations WHERE source_file_id = ? AND source_urn = ? AND relation_kind = ?"
            " AND call_site_line = ? AND call_site_col = ? AND target_urn = ? LIMIT 1",
            (*site, new),
        ).fetchone():  # 同じ位置・同じ先の行が既にある（LSP の exact が置き換えた）なら足さない
            conn.execute(
                "INSERT INTO relations (source_urn, target_urn, relation_kind, source_file_id, target_file_id,"
                " call_site_line, call_site_col, use_kind, guard_condition, confidence_level)"
                " VALUES (?, ?, ?, ?, (SELECT file_id FROM symbols WHERE urn = ?), ?, ?, ?, ?, ?)",
                (source, new, kind, file_id, new, line, col, use_kind, guard, confidence),
            )
            touched = True
        if new != old:
            conn.execute("UPDATE _member_refs SET resolved_urn = ? WHERE rowid = ?", (new, rowid))
        changed += touched
    return changed
