"""再び公開した名前を定義元までたどる（BUG-0100。設計書 §7.6.2、仕様書 T-06 処理規約 9・T-07 処理規約 11）。

Python のパッケージは公開 API を ``__init__.py`` で再び公開する（``pkg/__init__.py`` の ``from .helpers import flush``）。
``from pkg import flush`` の ``flush()`` を構文木の抽出器（``syntax/relations.py``）は import 表で ``pkg/__init__.py#flush`` に解くが、
``pkg/__init__.py`` は ``flush`` を定義していない（import はシンボルにならない）ので、関係を整える段（``engine/indexer._settle_relations``）が
「``symbols`` に無い目標」として捨てていた。

* **束ねた名前の表**（内部表 ``_reexports``。取り込みがファイルごとに全置換）: Python のファイルのモジュール直下の import が束ねた
  名前とその先（``kind = 'import'``。``from .helpers import flush`` → ``pkg/helpers.py#flush``、``from . import helpers`` → モジュール
  ``pkg/helpers.py``、外部の ``from threading import Lock`` → ``lk://_ext/threading#Lock``）、``from x import *`` のワークスペースの先
  （``kind = 'star'``）、``__all__`` の名前（``kind = 'all'``。文字列のリテラルだけのとき）。
* **たどる**（``ReexportResolver.resolve``）: 目標 ``P#A.rest`` が ``symbols`` に無く、``P#A`` もシンボルでなければ、``P`` が ``A`` を
  束ねた先 ``T`` に置き換えて（``T#…`` なら ``T.rest``、モジュール ``T`` なら ``T#rest``）続ける。束ねていなければ ``P`` の
  ``from x import *`` の先を後ろから見て、``x`` が ``A`` を公開している（``__all__`` があればそこに載っている、無ければ ``_`` で始まらず、
  ``x`` が定義している・束ねている・さらに ``*`` で公開している）ものに置き換える。シンボルに当たる・外部に出る・``P#A`` がシンボル
  （残りはクラス・変数のメンバーで ``index/inherited.py`` が決める）・それ以上たどれない、で止める。段は ``MAX_HOPS``（T-04 と同じ）
  まで、循環したら元の目標のまま。
* **整え**（``redirect_reexports``）: 整えるファイルの ``relations`` の、``symbols`` に無いワークスペースの目標（呼び出し・参照・
  ``INHERITS``・型の使用・import の行すべて）と、``_var_types`` の型（``EST = TokenEstimator()`` の ``TokenEstimator`` が再び公開した
  名前）を、たどった先に書き換える。``_settle_relations`` が ``_member_refs`` に退避する前に呼ぶ（``TokenEstimator().count()`` は
  ``core/tokens.py#TokenEstimator.count`` に書き換わり、継承したメンバーなら退避されて基底から結ばれる）。たどり切れなければ今までどおり
  （素の名前は捨て、メンバーは BUG-0094 の名前の一致に任せる）。
* **追従**（``_reexport_deps``）: たどるときに束ねた名前を引いた ``(ファイル, 名前)`` を、整えたファイルごとに覚える（見つからなかった
  ものも）。あるファイルの取り込み・削除で、そのファイルの「公開する名前」が変わった（最上位のシンボルが増えた・消えた、束ねた名前の
  先が変わった、``*`` の先・``__all__`` が変わった。``export_changes``）ら、その ``(ファイル, 名前)`` に依っていたファイル
  （``dependents``）を取り込み直す（``engine/reresolve.py``）。連鎖の途中のファイルもすべて覚えるので、何段先の変化でも 1 回で引ける。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Final

from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, SCHEME, CodeSel, Urn, parse
from lucianke.index.ingest import ReexportFact, ensure_external_symbol

__all__ = [
    "ALL_NAMES",
    "MAX_HOPS",
    "ReexportResolver",
    "dependents",
    "export_changes",
    "redirect_reexports",
]

MAX_HOPS: Final = 5
"""たどる段の上限（T-04 の ``MAX_HOPS`` と同じ）。"""

ALL_NAMES: Final = "*"
"""``export_changes`` の答えで「どの名前も変わりうる」（``*`` の先・``__all__`` の有無が変わった・ファイルが消えた）。"""

_EXTERNAL_PREFIX: Final = f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"
_STAR_DEPTH: Final = 3  # ``*`` の先がさらに ``*`` で公開しているかを見る深さ
_SQL_CHUNK: Final = 500


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such" in str(exc)


class ReexportResolver:
    """``_reexports`` をたどる（1 回の整えの中で、シンボル・ファイル・束ねた名前を覚えておく）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._symbols: dict[str, bool] = {}
        self._files: dict[tuple[str, str], int | None] = {}
        self._bindings: dict[tuple[int, str], str | None] = {}
        self._stars: dict[int, list[str]] = {}
        self._all: dict[int, frozenset[str] | None] = {}

    # --- 索引の読み（覚えて使い回す） ---

    def _is_symbol(self, urn: str) -> bool:
        hit = self._symbols.get(urn)
        if hit is None:
            hit = self._symbols[urn] = (
                self._conn.execute("SELECT 1 FROM symbols WHERE urn = ? LIMIT 1", (urn,)).fetchone() is not None
            )
        return hit

    def _file_id(self, workspace_id: str, path: str) -> int | None:
        key = (workspace_id, path)
        if key not in self._files:
            row = self._conn.execute(
                "SELECT file_id FROM files WHERE workspace_id = ? AND path = ?", (workspace_id, path)
            ).fetchone()
            self._files[key] = None if row is None else int(row[0])
        return self._files[key]

    def _binding(self, file_id: int, name: str) -> str | None:
        key = (file_id, name)
        if key not in self._bindings:
            try:
                row = self._conn.execute(
                    "SELECT target_urn FROM _reexports WHERE file_id = ? AND kind = 'import' AND name = ?"
                    " ORDER BY ord DESC LIMIT 1",
                    (file_id, name),
                ).fetchone()
            except sqlite3.OperationalError as exc:  # v10 より前の DB（移行前に読んだ）。束ねた名前は分からないとする
                if not _missing_table(exc):
                    raise
                row = None
            self._bindings[key] = None if row is None else str(row[0])
        return self._bindings[key]

    def _star_sources(self, file_id: int) -> list[str]:
        if file_id not in self._stars:
            try:
                rows = self._conn.execute(
                    "SELECT target_urn FROM _reexports WHERE file_id = ? AND kind = 'star' ORDER BY ord", (file_id,)
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if not _missing_table(exc):
                    raise
                rows = []
            self._stars[file_id] = [str(r[0]) for r in rows]
        return self._stars[file_id]

    def _all_names(self, file_id: int) -> frozenset[str] | None:
        if file_id not in self._all:
            try:
                rows = self._conn.execute(
                    "SELECT name FROM _reexports WHERE file_id = ? AND kind = 'all'", (file_id,)
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if not _missing_table(exc):
                    raise
                rows = []
            self._all[file_id] = frozenset(str(r[0]) for r in rows) if rows else None
        return self._all[file_id]

    # --- たどる ---

    def resolve(self, target: str, deps: set[tuple[str, str]] | None = None) -> str:
        """``target`` を再び公開した名前の連鎖の先に置き換えた URN（たどれなければ、たどれたところまで。循環・段の上限なら ``target``）。

        ``deps`` には、束ねた名前を引いた ``(ファイルのパス, 名前)`` を足す（見つからなかったものも。追従に使う）。
        """
        cur = target
        seen = {target}
        for hop in range(MAX_HOPS + 1):
            step = self._step(cur, deps, first=hop == 0)
            if step is None:
                return cur
            if step in seen:
                return target  # 循環: 結ばない（元の目標のまま。整えが今までどおり扱う）
            seen.add(step)
            cur = step
            if cur.startswith(_EXTERNAL_PREFIX):
                return cur
        return target  # 段の上限

    def _step(self, cur: str, deps: set[tuple[str, str]] | None, *, first: bool) -> str | None:
        """1 段: ``cur``（``P#A.rest``）の ``A`` を ``P`` が束ねた先に置き換えた URN。置き換えられなければ ``None``。

        2 段目からは、着いた先がシンボルでも ``(P, A)`` を ``deps`` に足す（終わりのファイルの ``A`` が消えた・足されたときも引ける）。
        1 段目（元の目標）は束ねた名前を引くときだけ足す（継承したメンバーへのただの参照で依存を増やさない）。
        """
        try:
            urn = parse(cur)
        except ValueError:
            return None
        sel = urn.selector
        if urn.is_external or not isinstance(sel, CodeSel) or sel.arity is not None or sel.sighash is not None:
            return None
        head, _, rest = sel.qualified.partition(".")
        if not head:
            return None
        if deps is not None and not first:
            deps.add((urn.path, head))
        if self._is_symbol(cur):
            return None
        if rest and self._is_symbol(_with(urn, head)):
            return None  # ``P#A`` は定義（クラス・変数）。残りのメンバーは index/inherited.py が決める
        file_id = self._file_id(urn.workspace_id, urn.path)
        if file_id is None:
            return None
        if deps is not None:
            deps.add((urn.path, head))
        bound = self._binding(file_id, head)
        if bound is None:
            bound = self._from_star(urn.workspace_id, file_id, head, deps, 0)
        if bound is None:
            return None
        return _compose(bound, rest)

    def _from_star(
        self, workspace_id: str, file_id: int, name: str, deps: set[tuple[str, str]] | None, depth: int
    ) -> str | None:
        """``file_id`` の ``from x import *`` のうち ``name`` を公開しているもの（後ろのものが勝つ）の ``x#name``。"""
        if depth > _STAR_DEPTH:
            return None
        for source in reversed(self._star_sources(file_id)):
            try:
                src = parse(source)
            except ValueError:
                continue
            src_id = self._file_id(src.workspace_id, src.path)
            if src_id is None or src_id == file_id:
                continue
            if deps is not None:
                deps.add((src.path, name))
            listed = self._all_names(src_id)
            if listed is not None:
                provides = name in listed
            else:
                provides = not name.startswith("_") and (
                    self._is_symbol(_with(src, name))
                    or self._binding(src_id, name) is not None
                    or self._from_star(workspace_id, src_id, name, deps, depth + 1) is not None
                )
            if provides:
                return _with(src, name)
        return None


def _with(urn: Urn, qualified: str) -> str:
    return str(urn.with_selector(CodeSel(qualified)))


def _compose(bound: str, rest: str) -> str | None:
    """束ねた先 ``bound`` に残り ``rest`` を足した URN（モジュールなら ``#rest``、シンボルなら ``.rest``）。"""
    if not rest:
        return bound
    try:
        urn = parse(bound)
    except ValueError:
        return None
    sel = urn.selector
    try:
        if sel is None:
            return str(urn.with_selector(CodeSel(rest)))
        if isinstance(sel, CodeSel):
            return str(urn.with_selector(CodeSel(f"{sel.qualified}.{rest}")))
    except ValueError:
        return None
    return None


def redirect_reexports(conn: sqlite3.Connection, file_id: int, resolver: ReexportResolver) -> int:
    """``file_id`` の ``relations`` の ``symbols`` に無いワークスペースの目標と ``_var_types`` の型を、再び公開した名前の連鎖の先に
    書き換える（書き換えた行数）。引いた ``(ファイル, 名前)`` を ``_reexport_deps`` に足す。"""
    deps: set[tuple[str, str]] = set()
    rows = conn.execute(
        "SELECT DISTINCT target_urn FROM relations WHERE source_file_id = ? AND instr(target_urn, '#') > 0"
        " AND substr(target_urn, 1, ?) <> ? AND target_urn NOT IN (SELECT urn FROM symbols)",
        (file_id, len(_EXTERNAL_PREFIX), _EXTERNAL_PREFIX),
    ).fetchall()
    changed = 0
    for (target,) in rows:
        new = resolver.resolve(str(target), deps)
        if new == target:
            continue
        if new.startswith(_EXTERNAL_PREFIX):
            kinds = [
                str(k)
                for (k,) in conn.execute(
                    "SELECT DISTINCT relation_kind FROM relations WHERE source_file_id = ? AND target_urn = ?",
                    (file_id, target),
                )
            ]
            ensure_external_symbol(conn, new, kinds)
        changed += conn.execute(
            "UPDATE relations SET target_urn = ?, target_file_id = (SELECT file_id FROM symbols WHERE urn = ?)"
            " WHERE source_file_id = ? AND target_urn = ?",
            (new, new, file_id, target),
        ).rowcount
    try:
        var_rows = conn.execute(
            "SELECT var_urn, type_urn FROM _var_types WHERE file_id = ? AND substr(type_urn, 1, ?) <> ?"
            " AND type_urn NOT IN (SELECT urn FROM symbols)",
            (file_id, len(_EXTERNAL_PREFIX), _EXTERNAL_PREFIX),
        ).fetchall()
    except sqlite3.OperationalError as exc:  # v9 より前の DB
        if not _missing_table(exc):
            raise
        var_rows = []
    for var_urn, type_urn in var_rows:
        new = resolver.resolve(str(type_urn), deps)
        if new != type_urn:
            conn.execute("UPDATE _var_types SET type_urn = ? WHERE var_urn = ?", (new, var_urn))
            changed += 1
    if deps:
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO _reexport_deps (file_id, path, name) VALUES (?, ?, ?)",
                [(file_id, path, name) for path, name in sorted(deps)],
            )
        except sqlite3.OperationalError as exc:
            if not _missing_table(exc):
                raise
    return changed


def _effective(rows: Iterable[tuple[str, str, str, int]]) -> tuple[dict[str, str], tuple[str, ...], frozenset[str] | None]:
    """``(kind, name, target_urn, ord)`` の行 → （名前 → 勝つ束ね先, ``*`` の先の並び, ``__all__``）。"""
    binds: dict[str, tuple[int, str]] = {}
    stars: list[tuple[int, str]] = []
    listed: set[str] = set()
    has_all = False
    for kind, name, target, ord_ in rows:
        if kind == "import":
            if name not in binds or binds[name][0] <= ord_:
                binds[name] = (ord_, target)
        elif kind == "star":
            stars.append((ord_, target))
        elif kind == "all":
            has_all = True
            listed.add(name)
    return (
        {n: t for n, (_, t) in binds.items()},
        tuple(t for _, t in sorted(stars)),
        frozenset(listed) if has_all else None,
    )


def export_changes(
    conn: sqlite3.Connection,
    file_id: int | None,
    qualified_names: Iterable[str],
    reexports: Sequence[ReexportFact],
) -> frozenset[str]:
    """ファイル ``file_id`` の取り込み済みの状態と、これから取り込む状態（シンボルの修飾名 ``qualified_names`` と ``reexports``）で、
    「公開する名前」が変わった名前（``ALL_NAMES`` はどれも変わりうる）。まだ取り込まれていない（新しい）ファイルは ``ALL_NAMES``
    （連鎖が無かったそのパスで止まった依存を引く。ふつうは 1 件も無い）。

    変わったと数えるもの: 最上位のシンボルの増減、束ねた名前の先（同じ名前が複数あれば後のもの）の違い、``*`` の先の並び・``__all__`` の
    有無の違い（``ALL_NAMES``）、``__all__`` の名前の増減。
    """
    if file_id is None:
        return frozenset({ALL_NAMES})
    old_heads = {
        str(q).split(".", 1)[0]
        for (q,) in conn.execute("SELECT qualified_name FROM symbols WHERE file_id = ?", (file_id,))
    }
    new_heads = {str(q).split(".", 1)[0] for q in qualified_names}
    changed: set[str] = old_heads ^ new_heads
    try:
        old_rows = [
            (str(k), str(n), str(t), int(o))
            for k, n, t, o in conn.execute(
                "SELECT kind, name, target_urn, ord FROM _reexports WHERE file_id = ?", (file_id,)
            )
        ]
    except sqlite3.OperationalError as exc:  # v10 より前の DB: 前の状態が分からないので、どれも変わりうる
        if not _missing_table(exc):
            raise
        return frozenset({ALL_NAMES})
    old_binds, old_stars, old_all = _effective(old_rows)
    new_binds, new_stars, new_all = _effective((r.kind, r.name, r.target_urn, r.ord) for r in reexports)
    changed.update(n for n in old_binds.keys() | new_binds.keys() if old_binds.get(n) != new_binds.get(n))
    if old_stars != new_stars or (old_all is None) != (new_all is None):
        changed.add(ALL_NAMES)
    elif old_all is not None and new_all is not None:
        changed.update(old_all ^ new_all)
    return frozenset(changed)


def dependents(
    conn: sqlite3.Connection,
    workspace_id: str,
    changes: Mapping[str, Collection[str]],
    skip: Collection[int] = (),
) -> list[tuple[int, str]]:
    """``changes``（ファイルのパス → 変わった名前。``ALL_NAMES`` はすべて）の名前をたどって整えたファイルの ``(file_id, path)``。パス順。"""
    found: dict[int, str] = {}
    for path, names in changes.items():
        if not names:
            continue
        try:
            if ALL_NAMES in names:
                rows = conn.execute(
                    "SELECT DISTINCT d.file_id, f.path FROM _reexport_deps d JOIN files f ON f.file_id = d.file_id"
                    " WHERE f.workspace_id = ? AND d.path = ?",
                    (workspace_id, str(path)),
                ).fetchall()
            else:
                ordered = sorted(names)
                rows = []
                for i in range(0, len(ordered), _SQL_CHUNK):
                    chunk = ordered[i : i + _SQL_CHUNK]
                    marks = ",".join("?" * len(chunk))
                    rows.extend(
                        conn.execute(
                            "SELECT DISTINCT d.file_id, f.path FROM _reexport_deps d JOIN files f ON f.file_id = d.file_id"
                            f" WHERE f.workspace_id = ? AND d.path = ? AND d.name IN ({marks})",
                            (workspace_id, str(path), *chunk),
                        ).fetchall()
                    )
        except sqlite3.OperationalError as exc:
            if not _missing_table(exc):
                raise
            return []
        for file_id, dep_path in rows:
            if int(file_id) not in skip:
                found[int(file_id)] = str(dep_path)
    return sorted(found.items(), key=lambda kv: kv[1])
