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

* **super()**（BUG-0081）: ``super().save()``（``super(C, self).save()``・TS / JS の ``super.save()``）は取り込み（``ingest_file``）が
  ``via_super = 1`` の行として直接入れる（``class_urn`` は ``C``）。結ぶときは ``C`` の線形化の **次のクラスから** 探す（``C`` 自身の
  上書きには結ばない）。外部・クラスでない基底に先に当たったときの扱いは上と同じ。
* **結べない理由**（``blocked_member_refs``）: 先に当たって止めた基底（中身の分からない外部の基底・クラスでない基底）の後ろに
  対象のクラスがあれば、その参照は対象に届くかもしれない。T-06 / T-07 が「結ばなかった」と理由つきで書く（名前の一致の
  「型不明・曖昧」には回さない）。
* **import した変数の受け手**（BUG-0094）: ``from conf import settings`` の ``settings.reload()`` の先は ``conf.py#settings.reload``
  （変数のメンバーという、``symbols`` に無い URN）。「クラスの部分」がワークスペースのモジュール直下の変数・定数なら、同じく
  ``_member_refs`` に退避する（``class_urn`` は変数）。結ぶときは定義元の変数の型（内部表 ``_var_types``。``settings = Settings()``・
  ``settings: Settings = ...``）の線形化で探す（``structural``）。型が外部（``lock = threading.Lock()``）なら外部のメンバー
  （``lk://_ext/threading#Lock.acquire``）。型が構文から分からなければ、その位置を名前だけの呼び出し・参照（``_name_calls``）にして
  T-06 / T-07 の名前の一致に任せる（黙って捨てない。無い URN にも結ばない）。変数のファイル・型のクラス（とその基底）のファイルを
  整えるたびに結び直す（型が分かるようになれば ``_name_calls`` の行を消して ``relations`` に、分からなくなれば逆に）。

* **型の別名の受け手**（BUG-0108）: ``o: Options``（``type Options = Base & { extra: string }``）の ``o.retry`` の先は
  ``types.ts#Options.retry``。「クラスの部分」がワークスペースの型の別名（``kind = 'Type'``）なら同じく退避し、別名の構成（内部表
  ``_type_aliases``。抽出器が書く）から探す（``MemberResolver.lookup_alias``）: 別名そのもの・交差は書いた順に最初に持つ構成、合併は
  持つ構成がちょうど 1 つのときだけ。別名自身の型リテラルのメンバーはシンボルがあればそれ、無ければ結ばない。中身の分からない構成が
  あって決まらなければ名前だけ（``_name_calls``）。C3 の線形化の途中の別名（``interface Options extends KyOptions``）も構成から探す。
  別名のファイル・構成の型のファイルを整えるたびに結び直す（再帰 CTE が ``_type_aliases`` を逆にたどる）。
  別名自身の型リテラルのメンバーはシンボル（``KyOptions.retry``。BUG-0114）で、交差ではほかの構成より先に見る。

* **メンバーの型の受け手**（BUG-0113）: ``r: Opts['retry']`` の ``r.limit``・``o.retry.limit`` の先は ``types.ts#Opts.retry.limit``
  （抽出器が注釈を先頭の名前 ``Opts`` に縮めず、受け手の型をメンバー ``Opts.retry`` の型とした）。シンボル（型リテラルの入れ子の
  メンバー）ならそのまま、無ければ「クラスの部分」のメンバー ``Opts.retry`` を受け手に退避し、メンバーの型注釈の構成（``_type_aliases``
  の ``alias_urn`` がメンバー。抽出器が書く）から別名と同じく探す。メンバーがシンボルでない（``Options.hooks`` の ``hooks`` が基底から
  継承した）ときは、受け手を途中のシンボル ``Options`` にし、名前を点つきの道 ``hooks.beforeRequest`` にしてたどる。型の分からない
  メンバー（``limit: number``・メソッド）・決まらない道は名前だけ（``_name_calls``。名前は道の最後）。メンバー・その型のファイルの
  整えで結び直す（再帰 CTE が ``_type_aliases`` を逆にたどり、型の変わったメンバーの持ち主〈``parent_urn``〉も足す）。

``_member_refs`` はファイルの取り込み（``ingest_file``）で全置換され、ファイルの削除で CASCADE で消える。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from lucianke.core.model import Kind
from lucianke.core.mro import TRANSPARENT_EXTERNAL_BASES, c3_linearize
from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, SCHEME, CodeSel, parse
from lucianke.index.ingest import ensure_external_symbol

__all__ = [
    "CLASS_KINDS",
    "VAR_KINDS",
    "BlockedRef",
    "MemberResolver",
    "blocked_member_refs",
    "park_member_refs",
    "resolve_member_refs",
]

CLASS_KINDS: Final = frozenset(
    {Kind.CLASS.value, Kind.INTERFACE.value, Kind.STRUCT.value, Kind.TRAIT.value, Kind.ENUM.value}
)
"""受け手・基底になれるシンボルの種別。"""

VAR_KINDS: Final = frozenset({Kind.VARIABLE.value, Kind.CONSTANT.value})
"""受け手になれるモジュール直下の変数の種別（型は ``_var_types``。BUG-0094）。"""

ALIAS_KIND: Final = Kind.TYPE.value
"""受け手になれる型の別名の種別（構成は ``_type_aliases``。BUG-0108）。"""

TYPED_MEMBER_KINDS: Final = frozenset({Kind.PROPERTY.value, Kind.FIELD.value})
"""型注釈の構成（``_type_aliases`` の ``alias_urn`` がメンバー）から型の分かるメンバーの種別（受け手 ``r: Opts['retry']``・
``o.retry.limit`` の ``Opts.retry``。BUG-0113）。"""

MEMBER_RECEIVER_KINDS: Final = TYPED_MEMBER_KINDS | {Kind.METHOD.value}
"""受け手の型がメンバーの型であるときの、そのメンバーの種別（メソッドの型〈関数〉のメンバーは分からないので名前だけ。BUG-0113）。"""

_EXTERNAL_PREFIX: Final = f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"
_PARKED_KINDS: Final = ("CALLS", "REFERENCES")
_RECEIVER_KINDS: Final = CLASS_KINDS | VAR_KINDS | {ALIAS_KIND} | MEMBER_RECEIVER_KINDS
_RECEIVER_KIND_LIST: Final = ", ".join(f"'{k}'" for k in sorted(CLASS_KINDS | {ALIAS_KIND} | TYPED_MEMBER_KINDS))
_TYPED_MEMBER_KIND_LIST: Final = ", ".join(f"'{k}'" for k in sorted(TYPED_MEMBER_KINDS))
_MAX_ALIAS_DEPTH: Final = 8  # 別名の構成をたどる深さ（循環した別名は止める）
_VAR_KIND_LIST: Final = ", ".join(f"'{k}'" for k in sorted(VAR_KINDS))


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


def _external_member(type_urn: str, name: str) -> str | None:
    """外部の型 ``lk://_ext/threading#Lock`` のメンバー ``lk://_ext/threading#Lock.acquire``。"""
    try:
        urn = parse(type_urn)
    except ValueError:
        return None
    sel = urn.selector
    qualified = f"{sel.qualified}.{name}" if isinstance(sel, CodeSel) else name
    try:
        return str(urn.with_selector(CodeSel(qualified)))
    except ValueError:
        return None


def park_member_refs(conn: sqlite3.Connection, file_id: int) -> int:
    """``file_id`` の行のうち、目標が無く、目標のクラスの部分がワークスペースのクラス・モジュール直下の変数（BUG-0094）である行を
    ``_member_refs`` に移す。移した行数。

    目標のクラスの部分がシンボルでないが、そのファイルは索引にある行（再び公開した名前で、定義元までたどれなかったもの。たどれるものは
    先に ``index/reexports.redirect_reexports`` が定義元に書き換えている。BUG-0100）は、受け手の型が分からないのと同じなので、名前だけの
    呼び出し・参照（``_name_calls``）にする（黙って捨てない。BUG-0094）。
    """
    marks = ", ".join("?" * len(_PARKED_KINDS))
    rows = conn.execute(
        "SELECT relation_id, source_urn, target_urn, relation_kind, call_site_line, call_site_col, use_kind,"
        " guard_condition, confidence_level FROM relations"
        f" WHERE source_file_id = ? AND relation_kind IN ({marks}) AND instr(target_urn, '#') > 0"
        " AND target_urn NOT IN (SELECT urn FROM symbols)",
        (file_id, *_PARKED_KINDS),
    ).fetchall()
    # 目標のクラスの部分 → ("member"〈退避〉/ "name"〈名前だけ〉/ ""〈触らない〉, 受け手, 受け手からメンバーまでの途中の名前)
    receivers: dict[str, tuple[str, str, str]] = {}
    parked: list[tuple[object, ...]] = []
    named: list[tuple[object, ...]] = []
    ids: list[tuple[int]] = []
    for relation_id, source, target, kind, line, col, use_kind, guard, confidence in rows:
        split = _split_member(target)
        if split is None or line is None or col is None:
            continue
        owner, name = split
        if owner not in receivers:
            receivers[owner] = _receiver_class(conn, owner)
        verdict, anchor, rest = receivers[owner]
        if verdict == "member":
            # 受け手がメンバーの型の途中（``Options.hooks.beforeRequest`` で ``Options.hooks`` がシンボルでない）なら、名前は
            # 点つきの道（``hooks.beforeRequest``）にし、受け手はシンボルの ``Options``（BUG-0113）
            member = f"{rest}.{name}" if rest else name
            parked.append((file_id, source, anchor, member, kind, use_kind, line, col, guard, confidence))
        elif verdict == "name":
            named.append((file_id, source, name, line, col, guard, use_kind or ("call" if kind == "CALLS" else "r")))
        else:
            continue
        ids.append((relation_id,))
    if parked:
        conn.executemany(
            "INSERT INTO _member_refs (file_id, source_urn, class_urn, name, relation_kind, use_kind,"
            " call_site_line, call_site_col, guard_condition, confidence_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            parked,
        )
    if named:
        conn.executemany(
            "INSERT INTO _name_calls (file_id, source_urn, name, call_site_line, call_site_col, guard_condition, use_kind)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            named,
        )
    if ids:
        conn.executemany("DELETE FROM relations WHERE relation_id = ?", ids)
    return len(parked)


def _receiver_class(conn: sqlite3.Connection, owner: str) -> tuple[str, str, str]:
    """目標のクラスの部分 ``owner`` の扱い ``(判定, 受け手, 途中の名前)``: ワークスペースのクラス・モジュール直下の変数・型の別名・
    メンバー（メンバーの型の受け手。BUG-0113）なら ``"member"``、シンボルでないがそのファイルが索引にある（再び公開した名前など）
    なら ``"name"``、それ以外（中身の分からないファイル・関数など）は ``""``。

    ``owner`` がシンボルでなく、その途中（``Options.hooks`` の ``Options``）が受け手になれるシンボルなら、受け手をそれにし、残りを
    途中の名前（``hooks``）にする（``o.hooks.beforeRequest`` の ``hooks`` が基底から継承したメンバー。BUG-0113）。"""
    hit = _workspace_kind(conn, owner)
    if hit is not None:
        return ("member", owner, "") if hit in _RECEIVER_KINDS else ("", owner, "")
    split = _anchor(conn, owner)
    if split is not None:
        return ("member", *split)
    try:
        urn = parse(owner)
    except ValueError:
        return ("", owner, "")
    indexed = conn.execute(
        "SELECT 1 FROM files WHERE workspace_id = ? AND path = ? LIMIT 1", (urn.workspace_id, urn.path)
    ).fetchone()
    return ("name" if indexed is not None else "", owner, "")


def _workspace_kind(conn: sqlite3.Connection, urn: str) -> str | None:
    row = conn.execute("SELECT kind FROM symbols WHERE urn = ? AND file_id IS NOT NULL", (urn,)).fetchone()
    return None if row is None else str(row[0])


def _anchor(conn: sqlite3.Connection, path_urn: str) -> tuple[str, str] | None:
    """シンボルでないメンバーの道 ``P#A.b.c`` の、いちばん長い受け手になれるシンボルの途中 ``P#A.b`` と残り ``c``（BUG-0113）。
    途中に受け手になれないシンボル（関数・名前空間など）が先に見つかる・どれもシンボルでなければ ``None``。"""
    try:
        urn = parse(path_urn)
    except ValueError:
        return None
    sel = urn.selector
    if urn.is_external or not isinstance(sel, CodeSel) or sel.arity is not None or sel.sighash is not None:
        return None
    parts = sel.qualified.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            prefix = str(urn.with_selector(CodeSel(".".join(parts[:i]))))
        except ValueError:
            return None
        kind = _workspace_kind(conn, prefix)
        if kind is not None:
            return (prefix, ".".join(parts[i:])) if kind in _RECEIVER_KINDS else None
    return None


@dataclass(frozen=True, slots=True)
class _Lookup:
    """C3 の順に探した結果。``blocker`` は先に当たって探すのをやめた基底（中身の分からない外部の基底・クラスでない基底）。

    ``untyped`` は受け手がモジュール直下の変数で、その型が構文から分からない（名前だけの呼び出し・参照にする。BUG-0094）。受け手が
    型の別名で、構成からメンバーが決まらない（中身の分からない構成・合併で持つ構成が 1 つでない。BUG-0108）ときも。
    ``inline`` はメンバーが型の別名自身の型リテラルにあり、シンボルが無い（結ぶ先が無い。別の型の同名にも結ばない。BUG-0108）。
    """

    found: str | None
    blocker: str | None
    order: tuple[str, ...]
    untyped: bool = False
    inline: bool = False


class _Resolver:
    """C3 の順にメンバーを探す（1 回の整えの中で、基底・メンバー・線形化を覚えておく）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._bases: dict[str, list[str]] = {}
        self._kinds: dict[str, str | None] = {}
        self._orders: dict[str, list[str] | None] = {}
        self._found: dict[tuple[str, str, bool], _Lookup] = {}
        self._var_types: dict[str, str | None] = {}
        self._aliases: dict[str, tuple[str, list[list[tuple[str, str]]]] | None] = {}
        self._alias_found: dict[tuple[str, str], _Lookup] = {}

    def _kind(self, urn: str) -> str | None:
        if urn not in self._kinds:
            row = self._conn.execute("SELECT kind FROM symbols WHERE urn = ?", (urn,)).fetchone()
            self._kinds[urn] = None if row is None else str(row[0])
        return self._kinds[urn]

    def kind(self, urn: str) -> str | None:
        """``urn`` のシンボルの種別（``symbols`` に無ければ ``None``）。"""
        return self._kind(urn)

    def is_var(self, urn: str) -> bool:
        """``urn`` がワークスペースのモジュール直下の変数・定数か（受け手の型は ``_var_types``。BUG-0094）。"""
        return not urn.startswith(_EXTERNAL_PREFIX) and self._kind(urn) in VAR_KINDS

    def is_alias(self, urn: str) -> bool:
        """``urn`` がワークスペースの型の別名か（受け手の型は ``_type_aliases`` の構成。BUG-0108）。"""
        return not urn.startswith(_EXTERNAL_PREFIX) and self._kind(urn) == ALIAS_KIND

    def is_member(self, urn: str) -> bool:
        """``urn`` がワークスペースのメンバー（プロパティ・フィールド・メソッド）か（受け手の型はそのメンバーの型。BUG-0113）。"""
        return not urn.startswith(_EXTERNAL_PREFIX) and self._kind(urn) in MEMBER_RECEIVER_KINDS

    def by_name(self, cls: str, name: str) -> bool:
        """受け手 ``cls`` のメンバー ``name`` が決まらないとき、名前だけの呼び出し・参照にするか（変数・型の別名・メンバーの型・
        メンバーの道の受け手。BUG-0094・BUG-0108・BUG-0113）。"""
        return "." in name or self.is_var(cls) or self.is_alias(cls) or self.is_member(cls)

    def anchor(self, urn: str) -> tuple[str, str] | None:
        """シンボルでないメンバーの道 ``P#A.b.c`` の受け手になれる途中と残り（``_anchor``）。シンボルなら ``None``。"""
        if urn.startswith(_EXTERNAL_PREFIX) or self._kind(urn) is not None:
            return None
        return _anchor(self._conn, urn)

    def alias_parts(self, urn: str) -> tuple[str, list[list[tuple[str, str]]]] | None:
        """型の別名 ``urn`` の ``(op, 構成ごとの (kind, target_urn) の列)``。構成が書かれていなければ ``None``。"""
        if urn not in self._aliases:
            try:
                rows = self._conn.execute(
                    "SELECT op, ord, kind, target_urn FROM _type_aliases WHERE alias_urn = ? ORDER BY ord, rowid", (urn,)
                ).fetchall()
            except sqlite3.OperationalError as exc:  # v12 より前の DB（移行前に読んだ）。構成は分からないとする
                if "no such" not in str(exc):
                    raise
                rows = []
            if not rows:
                self._aliases[urn] = None
            else:
                groups: dict[int, list[tuple[str, str]]] = {}
                for _op, ord_, kind, target in rows:
                    groups.setdefault(int(ord_), []).append((str(kind), str(target)))
                self._aliases[urn] = (str(rows[0][0]), [groups[k] for k in sorted(groups)])
        return self._aliases[urn]

    def var_type(self, urn: str) -> str | None:
        """変数 ``urn`` の構文から分かる型（``_var_types``）。分からなければ ``None``。"""
        if urn not in self._var_types:
            try:
                row = self._conn.execute("SELECT type_urn FROM _var_types WHERE var_urn = ?", (urn,)).fetchone()
            except sqlite3.OperationalError as exc:  # v9 より前の DB（移行前に読んだ）。型は分からないとする
                if "no such" not in str(exc):
                    raise
                row = None
            self._var_types[urn] = None if row is None else str(row[0])
        return self._var_types[urn]

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

    def resolve(self, cls: str, name: str, *, after: bool = False) -> str | None:
        """``cls`` の線形化で最初に ``name`` を持つクラスのメンバー。``after`` なら ``cls`` 自身は探さない（``super()``）。"""
        return self.lookup(cls, name, after=after).found

    def lookup(self, cls: str, name: str, *, after: bool = False) -> _Lookup:
        key = (cls, name, after)
        if key not in self._found:
            self._found[key] = _Lookup(None, None, (cls,), untyped=True)  # 循環の見張り（メンバーの型が自分の道を指す）
            if "." in name:
                self._found[key] = self._lookup_path(cls, name)
            elif self.is_var(cls):
                self._found[key] = self._lookup_var(cls, name)
            elif self.is_alias(cls):
                self._found[key] = self.lookup_alias(cls, name)
            elif self.is_member(cls):
                self._found[key] = self._lookup_member(cls, name)
            else:
                self._found[key] = self._lookup(cls, name, after)
        return self._found[key]

    def _lookup_member(self, member: str, name: str) -> _Lookup:
        """受け手の型がメンバー ``member`` の型（``r: Opts['retry']`` の ``Opts.retry``）: メンバーの型注釈の構成（``_type_aliases`` の
        ``alias_urn`` がメンバー）から別名と同じく探す（BUG-0113）。構成が無い（``limit: number``・注釈が無い）・メソッドは ``untyped``。"""
        if self._kind(member) not in TYPED_MEMBER_KINDS:
            return _Lookup(None, None, (member,), untyped=True)
        return self.lookup_alias(member, name)

    def _lookup_path(self, anchor: str, path: str) -> _Lookup:
        """受け手 ``anchor`` からメンバーの道 ``a.b.name`` をたどる（``o.hooks.beforeRequest`` の ``hooks`` が ``anchor`` のシンボルに
        無い〈基底から継承した〉とき。BUG-0113）。途中のメンバーが決まらない・型の分かるメンバーでなければ ``untyped``。"""
        *heads, last = path.split(".")
        cur = anchor
        for seg in heads:
            found = self.lookup(cur, seg).found
            if found is None or found.startswith(_EXTERNAL_PREFIX) or self._kind(found) not in TYPED_MEMBER_KINDS:
                return _Lookup(None, None, (anchor,), untyped=True)
            cur = found
        sub = self.lookup(cur, last)
        return _Lookup(sub.found, None, (anchor,), untyped=sub.untyped or sub.blocker is not None, inline=sub.inline)

    def _in_type(self, tp: str, name: str, depth: int = 0) -> _Lookup:
        """型 ``tp``（別名の構成・変数の型）のメンバー ``name``。クラスは C3、別名・型の分かるメンバーは構成、シンボルでないメンバーの
        道（``Options.hooks``）はたどる（BUG-0113）。それ以外は ``untyped``。"""
        if tp.startswith(_EXTERNAL_PREFIX):
            return _Lookup(_external_member(tp, name), None, (tp,))
        kind = self._kind(tp)
        if kind == ALIAS_KIND or kind in TYPED_MEMBER_KINDS:
            return self.lookup_alias(tp, name, depth + 1)
        if kind in CLASS_KINDS:
            return self._lookup(tp, name, False)
        split = self.anchor(tp) if kind is None else None
        if split is not None:
            anchor, rest = split
            return self.lookup(anchor, f"{rest}.{name}")
        return _Lookup(None, None, (tp,), untyped=True)

    def _lookup_var(self, var: str, name: str) -> _Lookup:
        """受け手がモジュール直下の変数: その型の線形化で探す。型が外部なら外部のメンバー、分からなければ ``untyped``。"""
        tp = self.var_type(var)
        if tp is None:
            return _Lookup(None, None, (), untyped=True)
        if tp.startswith(_EXTERNAL_PREFIX):
            return _Lookup(_external_member(tp, name), None, (tp,))
        if self.is_alias(tp):
            return self.lookup_alias(tp, name)
        if self._kind(tp) in TYPED_MEMBER_KINDS or self.anchor(tp) is not None:
            return self._in_type(tp, name)  # ``const r: Opts['retry'] = ...``（型がメンバーの型。BUG-0113）
        return self._lookup(tp, name, False)

    def lookup_alias(self, alias: str, name: str, depth: int = 0) -> _Lookup:
        """受け手が型の別名 ``alias``（型注釈の構成を書いたメンバーも同じ。BUG-0113）: 構成（``_type_aliases``）からメンバー ``name`` を
        探す（BUG-0108）。

        * ``and``（別名そのもの・交差）: 別名自身の型リテラルのメンバー（シンボル。v13 から。BUG-0114。無ければ ``inline``）が先、
          無ければ書いた順に、最初にメンバーを持つ構成のもの（ワークスペースのクラス・インターフェースは C3 の順、別名はさらにその
          構成）。どれにも無ければ、外部の型の構成があれば最初のもののメンバー、中身の分からない構成があれば ``untyped``（名前の一致に
          任せる）、無ければ見つからない。
        * ``or``（合併）: メンバーを持つ構成がちょうど 1 つで、中身の分からない構成（外部の型を含む）が無いときだけそれ。持つ構成が
          2 つ以上・中身の分からない構成があれば ``untyped``。
        * 構成が書かれていない（Python の別名・v12 より前の取り込み）・深すぎる・循環は ``untyped``。
        """
        key = (alias, name)
        if key in self._alias_found:
            return self._alias_found[key]
        self._alias_found[key] = _Lookup(None, None, (alias,), untyped=True)  # 循環の見張り（たどる途中で自分に戻ったら分からない）
        parts = self.alias_parts(alias) if depth <= _MAX_ALIAS_DEPTH else None
        if parts is None:
            return self._alias_found[key]
        op, groups = parts
        states = [self._alias_part(group, name, depth) for group in groups]
        order = (alias,)
        result = _Lookup(None, None, order)
        if op == "or":
            # 同じシンボルは 1 つに数える（合併の別々の型リテラルの同名のメンバー ``{ kind: 'a' } | { kind: 'b' }`` の ``<別名>.kind`` は
            # どの構成からも素の URN〈最後の構成のもの〉を指す。抽出器がそれに直接結ぶのと揃える。BUG-0114）
            distinct = {
                st[1] if st[0] == "found" else str(i): st for i, st in enumerate(states) if st[0] in ("found", "inline")
            }
            holders = list(distinct.values())
            if any(st[0] in ("unknown", "external") for st in states) or len(holders) > 1:
                result = _Lookup(None, None, order, untyped=True)
            elif holders:
                state, urn = holders[0]
                result = _Lookup(urn, None, order) if state == "found" else _Lookup(None, None, order, inline=True)
        else:
            # 別名自身の型リテラルのメンバー（シンボル。BUG-0114）を先に（抽出器が ``o.headers`` を ``InternalOptions.headers`` に直接
            # 結ぶのと揃える。クラス自身のメンバーが基底より先なのと同じ）、無ければ書いた順に最初に持つ構成
            own = (st for st, group in zip(states, groups, strict=True) if any(k == "member" for k, _ in group))
            hit = next((st for st in own if st[0] in ("found", "inline")), None) or next(
                (st for st in states if st[0] in ("found", "inline")), None
            )
            external = next((st[1] for st in states if st[0] == "external" and st[1] is not None), None)
            if hit is not None:
                result = _Lookup(hit[1], None, order) if hit[0] == "found" else _Lookup(None, None, order, inline=True)
            elif external is not None:
                result = _Lookup(external, None, order)
            elif any(st[0] == "unknown" for st in states):
                result = _Lookup(None, None, order, untyped=True)
        self._alias_found[key] = result
        return result

    def _alias_part(self, group: list[tuple[str, str]], name: str, depth: int) -> tuple[str, str | None]:
        """別名の構成 1 つの答え: ``("found", URN)`` / ``("inline", None)``（別名自身の型リテラルのメンバー。シンボルなし）/
        ``("external", 外部のメンバーの URN)`` / ``("unknown", None)``（中身が分からない）/ ``("absent", None)``（持たないと分かる）。"""
        for kind, target in group:
            if kind == "member" and target.rpartition(".")[2] == name:
                return ("found", target) if self._kind(target) is not None else ("inline", None)
        if any(kind == "opaque" for kind, _ in group):
            return ("unknown", None)
        tp = next((target for kind, target in group if kind == "type"), None)
        if tp is None:
            return ("absent", None)
        if tp.startswith(_EXTERNAL_PREFIX):
            return ("external", _external_member(tp, name))
        # 別名はさらにその構成、クラスは C3、型の分かるメンバー（添字の型 ``B['k']`` の ``B.k``）はその型の構成（BUG-0113）。型でない
        # シンボル・索引に無い型（たどれなかった再び公開した名前など）は分からない
        sub = self._in_type(tp, name, depth)
        if sub.found is not None:
            return ("external", sub.found) if sub.found.startswith(_EXTERNAL_PREFIX) else ("found", sub.found)
        if sub.inline:
            return ("inline", None)
        if sub.untyped or sub.blocker is not None:
            return ("unknown", None)
        return ("absent", None)

    def order(self, cls: str) -> list[str] | None:
        if cls not in self._orders:
            self._orders[cls] = c3_linearize(cls, self.bases)
        return self._orders[cls]

    def has_member(self, cls: str, name: str) -> bool:
        return not cls.startswith(_EXTERNAL_PREFIX) and self._member(cls, name) is not None

    def _lookup(self, cls: str, name: str, after: bool) -> _Lookup:
        order = self.order(cls)
        if order is None:
            return _Lookup(None, None, ())  # 循環・矛盾した並び
        for c in order[1:] if after else order:
            if c.startswith(_EXTERNAL_PREFIX):
                if c in TRANSPARENT_EXTERNAL_BASES:
                    continue
                return _Lookup(None, c, tuple(order))  # 中身の分からない外部の基底: 同名を定義しているかもしれない
            if self._kind(c) == ALIAS_KIND and c != cls:
                # 型の別名を継承したインターフェース（``interface Options extends KyOptions``）: 別名の構成から探す（BUG-0108）
                sub = self.lookup_alias(c, name)
                if sub.found is not None:
                    return _Lookup(sub.found, None, tuple(order))
                if sub.inline:
                    return _Lookup(None, None, tuple(order), inline=True)
                if sub.untyped:
                    return _Lookup(None, c, tuple(order))  # 構成の中身が分からない: 同名を持つかもしれない
                continue
            if self._kind(c) not in CLASS_KINDS:
                return _Lookup(None, c, tuple(order))  # クラスでない基底（別名など）: 実体が分からない
            hit = self._member(c, name)
            if hit is not None:
                return _Lookup(hit, None, tuple(order))
        return _Lookup(None, None, tuple(order))


MemberResolver = _Resolver
"""C3 の順にメンバーを探す（名前の一致がインスタンス属性を基底のものにまとめるのにも使う。``tools/_name_calls.py``。BUG-0095）。"""


def resolve_member_refs(conn: sqlite3.Connection, file_ids: Sequence[int]) -> int:
    """``file_ids`` の退避行と、``file_ids`` のクラスとその派生クラスを受け手にする退避行、``file_ids`` の変数・その型が上のクラスである
    変数を受け手にする退避行（BUG-0094）を結び直す。差し替えた行数。"""
    if not file_ids:
        return 0
    ids = json.dumps(sorted({int(i) for i in file_ids}))
    # 結び直す受け手: 整えるファイルのクラス・別名・型の分かるメンバーから、派生クラス（INHERITS を逆に）、構成にそれを持つ別名・
    # メンバー（_type_aliases を逆に）、型の変わったメンバーの持ち主（メンバーの道 ``hooks.beforeRequest`` の受け手。BUG-0113）
    rows = conn.execute(
        "WITH RECURSIVE"
        "  seed(urn) AS (SELECT urn FROM symbols WHERE file_id IN (SELECT value FROM json_each(:ids))"
        f"   AND kind IN ({_RECEIVER_KIND_LIST})),"
        "  sub(urn) AS (SELECT urn FROM seed UNION"
        "   SELECT r.source_urn FROM relations r JOIN sub ON r.target_urn = sub.urn WHERE r.relation_kind = 'INHERITS'"
        "   UNION SELECT a.alias_urn FROM _type_aliases a JOIN sub ON a.target_urn = sub.urn WHERE a.kind = 'type'"
        "   UNION SELECT s.parent_urn FROM symbols s JOIN sub ON s.urn = sub.urn"
        f"    WHERE s.parent_urn IS NOT NULL AND s.kind IN ({_TYPED_MEMBER_KIND_LIST})),"
        "  vars(urn) AS (SELECT urn FROM symbols WHERE file_id IN (SELECT value FROM json_each(:ids))"
        f"   AND kind IN ({_VAR_KIND_LIST})"
        "   UNION SELECT var_urn FROM _var_types WHERE type_urn IN (SELECT urn FROM sub))"
        " SELECT rowid, file_id, source_urn, class_urn, name, relation_kind, use_kind, call_site_line, call_site_col,"
        "  guard_condition, confidence_level, resolved_urn, via_super FROM _member_refs"
        " WHERE file_id IN (SELECT value FROM json_each(:ids)) OR class_urn IN (SELECT urn FROM sub)"
        "  OR class_urn IN (SELECT urn FROM vars)",
        {"ids": ids},
    ).fetchall()
    resolver = _Resolver(conn)
    changed = 0
    for rowid, file_id, source, cls, name, kind, use_kind, line, col, guard, confidence, old, via_super in rows:
        lk = resolver.lookup(str(cls), str(name), after=bool(via_super))
        new = lk.found
        site = (file_id, source, kind, line, col)
        touched = False
        if resolver.by_name(str(cls), str(name)):
            # 型の分からない変数の受け手・構成からメンバーの決まらない型の別名の受け手（BUG-0108）・型の分からないメンバーの型の
            # 受け手（BUG-0113）は名前だけの呼び出し・参照にする（前に書いた行は位置で消して書き直す。BUG-0094）
            leaf = str(name).rpartition(".")[2]
            had = conn.execute(
                "DELETE FROM _name_calls WHERE file_id = ? AND source_urn = ? AND name = ?"
                " AND call_site_line = ? AND call_site_col = ?",
                (file_id, source, leaf, line, col),
            ).rowcount
            if lk.untyped:
                conn.execute(
                    "INSERT INTO _name_calls (file_id, source_urn, name, call_site_line, call_site_col, guard_condition,"
                    " use_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (file_id, source, leaf, line, col, guard, use_kind or ("call" if kind == "CALLS" else "r")),
                )
            touched = bool(had) != lk.untyped
        if new is not None and new.startswith(_EXTERNAL_PREFIX):
            ensure_external_symbol(conn, new, (str(kind),))
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


@dataclass(frozen=True, slots=True)
class BlockedRef:
    """結べなかった ``_member_refs`` の行のうち、対象のクラスに届くかもしれないもの（``blocked_member_refs``）。位置は 1 始まり。"""

    source_urn: str
    class_urn: str  # 受け手のクラス（super() なら super を書いたクラス）
    blocker: str  # 線形化で対象のクラスより先に当たって探すのをやめた基底
    reason: str  # ``external_base``（中身の分からない外部の基底）| ``unknown_base``（クラスでない基底）
    via_super: bool
    relation_kind: str
    use_kind: str | None
    path: str
    line: int
    col: int
    is_test: bool


def blocked_member_refs(conn: sqlite3.Connection, workspace_id: str, target_urn: str) -> list[BlockedRef]:
    """対象のメンバー ``target_urn``（``path#Cls.name``）に届くかもしれないのに結ばなかった参照（BUG-0081）。

    ``_member_refs`` の結べなかった行（``resolved_urn IS NULL``）のうち、名前が同じで、受け手のクラスの線形化で ``Cls`` が
    探すのをやめた基底（外部・クラスでない）の後ろにあり、その間に同名を持つワークスペースのクラスが無いもの。並びは（パス、行、列）。
    """
    split = _split_member(target_urn)
    if split is None:
        return []
    owner, name = split
    try:
        rows = conn.execute(
            "SELECT mr.source_urn, mr.class_urn, mr.via_super, mr.relation_kind, mr.use_kind, mr.call_site_line,"
            " mr.call_site_col, f.path, f.is_test FROM _member_refs mr JOIN files f ON f.file_id = mr.file_id"
            " WHERE f.workspace_id = ? AND mr.name = ? AND mr.resolved_urn IS NULL"
            " ORDER BY f.path, mr.call_site_line, mr.call_site_col",
            (workspace_id, name),
        ).fetchall()
    except sqlite3.OperationalError as exc:  # v7 より前の DB（移行前に読んだ）。無いものとして答える
        if "no such" in str(exc):
            return []
        raise
    resolver = _Resolver(conn)
    out: list[BlockedRef] = []
    for source, cls, via_super, kind, use_kind, line, col, path, is_test in rows:
        lk = resolver.lookup(str(cls), name, after=bool(via_super))
        if lk.blocker is None or owner not in lk.order:
            continue
        start, end = lk.order.index(lk.blocker) + 1, lk.order.index(owner)
        if end < start or any(resolver.has_member(c, name) for c in lk.order[start:end]):
            continue
        out.append(
            BlockedRef(
                source_urn=str(source),
                class_urn=str(cls),
                blocker=lk.blocker,
                reason="external_base" if lk.blocker.startswith(_EXTERNAL_PREFIX) else "unknown_base",
                via_super=bool(via_super),
                relation_kind=str(kind),
                use_kind=None if use_kind is None else str(use_kind),
                path=str(path),
                line=int(line),
                col=int(col),
                is_test=bool(is_test),
            )
        )
    return out
