"""型の分からない受け手のメソッド呼び出し（``x.name(...)``）と属性の読み書き（``x.name``。BUG-0095）を、名前で対象に結ぶ
（BUG-0071 / BUG-0077。仕様書 §8.3、T-06 / T-07 の処理規約）。

索引器は、受け手の型が構文から読めない属性呼び出し（注釈が無い・``Any``・``self.x`` の型が不明・呼び出しの戻り値など）を
``relations`` に入れられない（先の URN が決まらない）。その位置と名前は内部表 ``_name_calls`` に残っているので、T-06 / T-07 が
問い合わせのときにここで判定する。呼ぶ側の AI にとって、本番の呼び出し元が黙って 0 件に見える方が、印の付いた推定より悪い
（編集の影響を見落とす）ため。ただし誤って結ぶのは取りこぼしよりさらに悪い（影響範囲を誤認する）ので、候補は **呼び出し箇所
ごとに、近いところから** 選ぶ（BUG-0077）。

* **候補**（``NameIndex.verdict_for``）: 呼び出し箇所と同じ言語族（Python / TS・JS）のワークスペース内の ``Method`` で、名前が
  同じもの。同じ ``(パス, 修飾名)`` のオーバーロードは 1 つに数える。属性の読み書き（``_name_calls.use_kind`` が ``r`` / ``w`` /
  ``rw``。BUG-0095）の候補は同名の **クラスのメンバー**（``Method``・``Field``〈クラス属性・dataclass のフィールド〉・``Property``・
  クラスの ``Constant``）と **インスタンス属性**（``_attr_defs``。メソッドの中の ``self.x = ...`` / ``this.x = ...``）。インスタンス属性は、
  そのクラスの線形化の基底が同名のシンボルかインスタンス属性を持てば、その基底のものと同じ属性として 1 つに数える（``x.append`` を
  渡すのはメソッドの参照なので、メソッドも候補）。次の順に見て、最初に候補のある段で決める。

  1. **同じファイル**の定義（テストファイルの中のテストダブルも含む）。
  2. 呼び出し箇所を含む**テストフィクスチャ**（``workspace.languages.fixture_roots``: テストディレクトリの配下の
     ``fixtures/<名前>/`` など。入れ子のワークスペースのような見本のプロジェクト）の定義。内側のフィクスチャから外へ。
  3. **ワークスペース**の定義（どのフィクスチャにも入っていないもの）。

  2・3 の段では、その段のルートから見てテストファイルの定義（テストダブル。``fixtures/<名前>/tests/`` の中なども）は数えない
  （同じファイルからの呼び出しだけが 1 の段で結ぶ）。段に同名の定義がテストダブルしか無ければ、そこで「候補なし」として止まる
  （外の段の同名に結ばない）。呼び出し箇所を含まないフィクスチャの定義は見えない（別の見本のプロジェクトのクラス）。
  呼び出しを囲むクラス自身のメソッドは、候補に数えるが唯一の候補でも結ばない（``self.m()`` は構文木の抽出器が結ぶので、型の
  分からない受け手は別のオブジェクト。``def kill(self): self.proc.kill()`` のような委譲。Lucianke 自身で 9 箇所すべてがこれだった）。
  同じファイルの定義が囲みのクラス自身のものだけなら 1 の段は飛ばす。
* **結ぶ**: 決めた段の候補がちょうど 1 つで、名前が組み込み型のメソッド名（``syntax/builtin_names.py`` の
  ``PYTHON_BUILTIN_METHODS`` / ``JS_BUILTIN_METHODS``。``list.append``・``dict.get``・``Array.push`` など。属性の読み書きは組み込み型の
  属性の名前〈``name``・``args``・``length`` など〉と ``__x__`` も）でないとき。その位置をその候補への呼び出し・参照として返す
  （``Confidence.HEURISTIC``、json の ``match: "name"``、compact の ``conf=heuristic``）。属性の読み書きは、外部の型のメンバーにも同名が
  ある（ワークスペースの型の分かる受け手が使った ``lk://_ext/<pkg>#<Class>.name``。``node: Node`` の ``node.parent`` → ``tree_sitter``
  の ``Node.parent``）ときも結ばない（``reason: external_name``）。型の分からない受け手は、その外部の型であることが多いため
  （Lucianke 自身で、名前で結んだ属性の読みの上位は tree-sitter の ``Node.type``・``Node.parent`` を唯一の同名のフィールドに結んでいた）。
* **結ばない**: 候補が 2 つ以上（同じ近さ）、または組み込み型と同名。どの候補にも足さず、対象が候補に入っているときだけ件数と
  先頭の位置を応答の ``warnings`` と ``data.unattributed_calls`` に要約する（全候補に足すと、``run``・``get`` のような名前で参照の
  一覧が推定で埋まるため）。
* 候補なし（その名前のメソッドが無い・見えるのがテストダブルだけ）なら何もしない（外部・組み込みの呼び出しで、ワークスペースの
  見落としではない。``conn.execute()`` をテストの偽物 ``Slow.execute`` に結ばない）。
* ``exact``（LSP）の答えには混ぜない。T-06 / T-07 は ``exact`` のとき、結べる（その箇所の候補が対象 1 つ）のに LSP の答えに無い
  位置の件数と先頭を ``warnings`` に書くだけにする（``Any`` の受け手は LSP も型を決められない）。

ディレクトリの近さ（最も近い共通の祖先）では順位を付けない: Lucianke 自身で測ると、本番の同名メソッドの曖昧さ 127 箇所が近い方の
推定に変わり（多態の受け手を誤認しうる）、テスト同士では隣のテストファイルの偽物（``test_T11_rename.py`` の ``StubPool``）が
``test_T08_diagnostics.py`` の呼び出しを奪った。フィクスチャのルートは「別のプロジェクト」という確かな境界なので、それだけを段にした。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from lucianke.core.model import Confidence, Kind, Position, Range, SymbolRecord
from lucianke.core.mro import TRANSPARENT_EXTERNAL_BASES
from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, SCHEME, CodeSel, parse
from lucianke.index import queries
from lucianke.index.inherited import CLASS_KINDS, MemberResolver
from lucianke.index.queries import CALL_USES, REF_USES, AttrDefRow, NameCallRow
from lucianke.syntax.builtin_names import JS_BUILTIN_METHODS, PYTHON_BUILTIN_METHODS, is_builtin_attribute
from lucianke.syntax.support import get_support
from lucianke.workspace.languages import fixture_roots, is_test

__all__ = [
    "MATCH_NAME",
    "MEMBER_USES",
    "NameIndex",
    "Pool",
    "TargetSites",
    "Unattributed",
    "Verdict",
    "family_of",
    "owned_text",
    "pool_of",
    "sample_text",
    "site_key",
    "sites_json",
]

MATCH_NAME = "name"  # json の ``match`` の値（名前だけで結んだ要素）
SAMPLE_MAX = 5  # warnings に並べる位置の数
DATA_SAMPLE_MAX = 20  # data.unattributed_calls に並べる位置の数
LABELS_MAX = 3  # 「同名のメソッド」として名前を並べる数

_FAMILIES: dict[str, str] = {
    "python": "python",
    "typescript": "js",
    "tsx": "js",
    "javascript": "js",
    "jsx": "js",
}
_LANGUAGES: dict[str, tuple[str, ...]] = {
    "python": ("python",),
    "js": ("typescript", "tsx", "javascript", "jsx"),
}
_BUILTIN_METHODS: dict[str, frozenset[str]] = {"python": PYTHON_BUILTIN_METHODS, "js": JS_BUILTIN_METHODS}

Scope = Literal["file", "fixture", "workspace"]
Pool = Literal["call", "ref"]  # 候補の集まり: 呼び出しはメソッド、属性の読み書きはクラスのメンバーとインスタンス属性（BUG-0095）
MEMBER_USES: tuple[str, ...] = CALL_USES + REF_USES
_EXTERNAL_PREFIX = f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"
_REF_TARGET_KINDS = frozenset({Kind.FIELD, Kind.PROPERTY, Kind.CONSTANT})


def pool_of(use_kind: str) -> Pool:
    """``_name_calls.use_kind`` → 候補の集まり（``call`` はメソッド、``r`` / ``w`` / ``rw`` はメンバーとインスタンス属性）。"""
    return "call" if use_kind == "call" else "ref"


def _test_globs(language: str | None) -> tuple[str, ...]:
    support = get_support(language or "")
    return tuple(getattr(support, "test_path_globs", ()) or ())


# フィクスチャのルートの判定に使うテストディレクトリの名前は、関係を抽出する言語すべてのグロブから（言語に依らず同じルート）
_ALL_TEST_GLOBS: tuple[str, ...] = tuple(sorted({g for lang in _FAMILIES for g in _test_globs(lang)}))


def family_of(language: str | None) -> str | None:
    """言語 ID → 言語族（``"python"`` / ``"js"``）。関係を抽出しない言語は ``None``。"""
    return _FAMILIES.get(language or "")


def site_key(row: NameCallRow) -> tuple[str, int, int]:
    """``(パス, 行, 列)``（1 始まり）。T-06 の参照の重複判定と同じキー。"""
    return (str(row.path), row.site.line + 1, row.site.col + 1)


def _label(sym: SymbolRecord) -> str:
    extra = " (instance attribute)" if _is_instance_attr(sym) else ""
    return f"{sym.qualified_name}{extra} @{sym.path}:{sym.range.start.line + 1}"


def _is_instance_attr(sym: SymbolRecord) -> bool:
    return sym.kind is Kind.FIELD and "instance" in sym.modifiers


def _external_label(urn: str) -> str:
    """``lk://_ext/tree_sitter#Node.parent`` → ``tree_sitter.Node.parent``。"""
    return urn.removeprefix(_EXTERNAL_PREFIX).replace("#", ".", 1)


def _attr_record(row: AttrDefRow) -> SymbolRecord | None:
    """インスタンス属性の定義（``_attr_defs``）を候補として扱うための仮の ``SymbolRecord``（``symbols`` には無い）。"""
    try:
        owner = parse(row.class_urn)
    except ValueError:
        return None
    sel = owner.selector
    if not isinstance(sel, CodeSel):
        return None
    qualified = f"{sel.qualified}.{row.name}"
    try:
        urn = owner.with_selector(CodeSel(qualified))
    except ValueError:
        return None
    end = Position(row.site.line, row.site.col + len(row.name))
    return SymbolRecord(
        urn=urn,
        path=row.path,
        name=row.name,
        qualified_name=qualified,
        kind=Kind.FIELD,
        modifiers=("instance",),
        range=Range(row.site, end),
        selection=Range(row.site, end),
        confidence=Confidence.STRUCTURAL,
    )


def _ident(sym: SymbolRecord) -> tuple[str, str]:
    return (str(sym.path), sym.qualified_name)


@dataclass(frozen=True, slots=True)
class Verdict:
    """1 つの呼び出し箇所の、名前 ``name`` の型不明の呼び出しを、どの定義に結べるか（``NameIndex.verdict_for``）。"""

    name: str
    family: str
    candidates: tuple[SymbolRecord, ...]  # 決めた段で競合する定義。空にはしない
    builtin: bool  # 組み込み型にも同名のメソッドがある
    scope: Scope = "workspace"  # 決めた段: 同じファイル・呼び出し箇所を含むフィクスチャ・ワークスペース
    root: str = ""  # scope が fixture のときそのルート
    test_doubles: int = 0  # 数えなかったテストファイルの同名の定義の数（文言用）
    pool: Pool = "call"  # 呼び出し（メソッド）か属性の読み書き（メンバーとインスタンス属性。BUG-0095）か
    external: tuple[str, ...] = ()  # 同名のメンバーを持つ外部の型（属性の読み書きだけ。``lk://_ext/...`` の URN）

    @property
    def _noun(self) -> str:
        return "method" if self.pool == "call" else "member"

    def _where(self) -> str:
        if self.scope == "file":
            return "in the calling file"
        if self.scope == "fixture":
            return f"in the test fixture {self.root}/ that holds the call"
        return "in the workspace"

    def only_text(self) -> str:
        """``Packer.pack is the only method named 'pack' in the workspace (besides 1 in test files)``。"""
        owner = self.owner
        assert owner is not None
        besides = f" (besides {self.test_doubles} in test files)" if self.test_doubles else ""
        return f"{owner.qualified_name} is the only {self._noun} named {self.name!r} {self._where()}{besides}"

    @property
    def owner(self) -> SymbolRecord | None:
        """名前で結んでよい唯一の定義。結べないなら ``None``。"""
        return self.candidates[0] if len(self.candidates) == 1 and not self.builtin and not self.external else None

    def owned_by(self, sym: SymbolRecord) -> bool:
        owner = self.owner
        return owner is not None and _ident(owner) == _ident(sym)

    def considers(self, sym: SymbolRecord) -> bool:
        """``sym`` が候補に入っている（結べなくても、対象への呼び出しでありうる）。"""
        return any(_ident(c) == _ident(sym) for c in self.candidates)

    def key(self) -> tuple[Any, ...]:
        """結ばなかった呼び出しをまとめるキー（同じ理由・同じ候補の集まり）。"""
        return (
            self.pool,
            self.name,
            self.family,
            self.reason_code(),
            self.scope,
            self.root,
            tuple(_ident(c) for c in self.candidates),
        )

    def reason(self) -> str:
        """結ばない理由（英語。warnings に入る）。"""
        parts: list[str] = []
        n = len(self.candidates)
        if n > 1:
            shown = ", ".join(_label(s) for s in self.candidates[:LABELS_MAX])
            more = f", … {n - LABELS_MAX} more" if n > LABELS_MAX else ""
            where = "" if self.scope == "workspace" else f" {self._where()}"
            parts.append(f"{n} {self._noun}s share the name{where} ({shown}{more})")
        if self.builtin:
            what = "a method" if self.pool == "call" else "an attribute or method"
            parts.append(f"built-in types also have {what} of that name")
        if self.external:
            shown = ", ".join(_external_label(u) for u in self.external[:LABELS_MAX])
            more = f", … {len(self.external) - LABELS_MAX} more" if len(self.external) > LABELS_MAX else ""
            parts.append(f"external types used in the workspace also have a member of that name ({shown}{more})")
        return "; ".join(parts)

    def reason_code(self) -> str:
        """``data.unattributed_calls[].reason``: ``ambiguous``（同名が 2 つ以上）か ``builtin_name``（組み込み型と同名）か
        ``external_name``（外部の型のメンバーと同名。属性の読み書きだけ）。"""
        if len(self.candidates) > 1:
            return "ambiguous"
        return "builtin_name" if self.builtin else "external_name"

    def reason_json(self) -> dict[str, Any]:
        def cand(s: SymbolRecord) -> dict[str, Any]:
            out: dict[str, Any] = {"qualified_name": s.qualified_name, "path": str(s.path), "line": s.range.start.line + 1}
            if self.pool == "ref":  # 属性の読み書きの候補はメンバーの種別まで（インスタンス属性はシンボルではない）
                out["kind"] = "instance_attribute" if _is_instance_attr(s) else s.kind.value
            return out

        return {
            "candidates": [cand(s) for s in self.candidates[:DATA_SAMPLE_MAX]],
            "candidates_total": len(self.candidates),
            "builtin_name": self.builtin,
            **({"external_members": [_external_label(u) for u in self.external[:DATA_SAMPLE_MAX]]} if self.external else {}),
        }


@dataclass(slots=True)
class Unattributed:
    """結ばなかった型不明の呼び出しの集まり（同じ ``Verdict.key()`` ごと）。位置は重複を除いて（パス、行、列）順。"""

    verdict: Verdict
    rows: dict[tuple[str, int, int], NameCallRow] = field(default_factory=dict)

    def add(self, rows: Iterable[NameCallRow]) -> None:
        for r in rows:
            self.rows.setdefault(site_key(r), r)

    def sorted_rows(self) -> list[NameCallRow]:
        return [self.rows[k] for k in sorted(self.rows)]

    def to_json(self, enclosing: Any, reason: str | None = None) -> dict[str, Any]:
        """``data.unattributed_calls[]`` の 1 要素。``reason`` を省くと ``Verdict.reason_code()``。"""
        rows = self.sorted_rows()
        return {
            "name": self.verdict.name,
            "total": len(rows),
            "reason": reason or self.verdict.reason_code(),
            **self.verdict.reason_json(),
            "sites": sites_json(rows, enclosing),
        }


@dataclass(slots=True)
class TargetSites:
    """対象のメソッドの名前の型不明の呼び出しを、箇所ごとの判定で分けたもの（``NameIndex.for_target``）。"""

    owned: list[tuple[NameCallRow, Verdict]] = field(default_factory=list)  # 対象に結んだ箇所
    unattributed: dict[tuple[Any, ...], Unattributed] = field(default_factory=dict)  # 対象が候補だが結べない箇所

    @property
    def owned_rows(self) -> list[NameCallRow]:
        return [r for r, _ in self.owned]


def owned_text(owned: Sequence[tuple[NameCallRow, Verdict]]) -> str:
    """結んだ根拠（``Packer.pack is the only method named 'pack' in the workspace (besides 1 in test files)``）。

    段が混ざるとき（同じファイルの箇所とワークスペースの箇所など）は、根拠ごとに件数を添えて ``; `` でつなぐ。
    """
    counts: dict[str, int] = {}
    for _, v in owned:
        text = v.only_text()
        counts[text] = counts.get(text, 0) + 1
    if len(counts) == 1:
        return next(iter(counts))
    return "; ".join(f"{text} ({n} site(s))" for text, n in counts.items())


def sample_text(rows: Sequence[NameCallRow], limit: int = SAMPLE_MAX) -> str:
    """``a.py:10, b.py:22, … 3 more``（呼び出し箇所の先頭）。"""
    shown = ", ".join(f"{r.path}:{r.site.line + 1}" for r in rows[:limit])
    return shown + (f", … {len(rows) - limit} more" if len(rows) > limit else "")


def sites_json(rows: Sequence[NameCallRow], enclosing: Any) -> list[dict[str, Any]]:
    """``data.unattributed_calls[].sites`` / ``data.unattributed_refs[].sites``（先頭 ``DATA_SAMPLE_MAX`` 件）。``enclosing`` は URN →
    囲みの名前。属性の読み書きの位置には ``use``（``r`` / ``w`` / ``rw``）を添える。"""
    out: list[dict[str, Any]] = []
    for r in rows[:DATA_SAMPLE_MAX]:
        site: dict[str, Any] = {
            "path": str(r.path),
            "line": r.site.line + 1,
            "col": r.site.col + 1,
            "enclosing_name": enclosing(r.source_urn),
            "is_test": r.is_test,
        }
        if r.use_kind != "call":
            site["use"] = r.use_kind
        out.append(site)
    return out


def _unique(syms: Iterable[SymbolRecord]) -> tuple[SymbolRecord, ...]:
    out: dict[tuple[str, str], SymbolRecord] = {}
    for s in syms:
        out.setdefault(_ident(s), s)
    return tuple(out.values())


class NameIndex:
    """1 回の問い合わせの間の、名前の定義・箇所ごとの ``Verdict``・言語のキャッシュ（同じ読み取り接続のスレッドの中で使う）。"""

    def __init__(self, conn: sqlite3.Connection, workspace_id: str) -> None:
        self.conn, self.ws = conn, workspace_id
        self._defs: dict[tuple[str, str, str], list[tuple[SymbolRecord, bool]]] = {}
        self._verdicts: dict[tuple[str, str, str, str, str], Verdict | None] = {}
        self._resolver: MemberResolver | None = None
        self._externals: dict[tuple[str, str], tuple[str, ...]] = {}
        self._languages: dict[str, str | None] = {}
        self._roots: dict[str, tuple[str, ...]] = {}

    def family_of_path(self, path: str) -> str | None:
        return family_of(self._language(path))

    def _language(self, path: str) -> str | None:
        if path not in self._languages:
            info = queries.file_by_path(self.conn, self.ws, path)
            self._languages[path] = info.language if info is not None else None
        return self._languages[path]

    def roots(self, path: str) -> tuple[str, ...]:
        """``path`` を含むテストフィクスチャのルート（外側から内側）。"""
        if path not in self._roots:
            self._roots[path] = fixture_roots(path, _ALL_TEST_GLOBS)
        return self._roots[path]

    def _scope_root(self, path: str) -> str:
        roots = self.roots(path)
        return roots[-1] if roots else ""

    def _is_test_in(self, sym: SymbolRecord, is_test_in_workspace: bool, root: str) -> bool:
        """``root``（``""`` はワークスペース）から見て ``sym`` がテストファイルの定義か。"""
        if not root:
            return is_test_in_workspace
        path = str(sym.path)
        return is_test(path[len(root) + 1 :], _test_globs(self._language(path)))

    def _defs_named(self, name: str, family: str, pool: Pool = "call") -> list[tuple[SymbolRecord, bool]]:
        key = (name, family, pool)
        if key not in self._defs:
            languages = _LANGUAGES[family]
            if pool == "call":
                self._defs[key] = queries.methods_named(self.conn, self.ws, name, languages)
            else:
                members = queries.members_named(self.conn, self.ws, name, languages)
                attrs = self._instance_attrs(queries.attr_defs_named(self.conn, self.ws, name, languages), name)
                self._defs[key] = sorted(
                    [*members, *attrs], key=lambda d: (str(d[0].path), d[0].range.start.line, d[0].qualified_name)
                )
        return self._defs[key]

    def _instance_attrs(self, rows: Sequence[AttrDefRow], name: str) -> list[tuple[SymbolRecord, bool]]:
        """インスタンス属性の定義を候補にする。クラスの線形化の基底（中身の分からない外部の基底より前）が同名のシンボルか
        インスタンス属性を持つものは、その基底のものと同じ属性なので数えない。"""
        if not rows:
            return []
        if self._resolver is None:
            self._resolver = MemberResolver(self.conn)
        resolver = self._resolver
        declared = {r.class_urn for r in rows}
        out: list[tuple[SymbolRecord, bool]] = []
        for r in rows:
            merged = False
            for base in (resolver.order(r.class_urn) or [r.class_urn])[1:]:
                if base.startswith(_EXTERNAL_PREFIX):
                    if base in TRANSPARENT_EXTERNAL_BASES:
                        continue
                    break
                if resolver.kind(base) not in CLASS_KINDS:
                    break
                if base in declared or resolver.has_member(base, name):
                    merged = True
                    break
            rec = None if merged else _attr_record(r)
            if rec is not None:
                out.append((rec, r.is_test))
        return out

    def _external_members(self, name: str, family: str) -> tuple[str, ...]:
        key = (name, family)
        if key not in self._externals:
            self._externals[key] = tuple(
                queries.external_members_named(self.conn, self.ws, name, _LANGUAGES[family])
            )
        return self._externals[key]

    def target_name(self, sym: SymbolRecord) -> tuple[str, str] | None:
        """対象のメソッド ``sym`` の ``(名前, 言語族)``（``Method`` でない・関係を抽出しない言語なら ``None``）。T-07 の呼び出しだけ。"""
        if sym.kind is not Kind.METHOD:
            return None
        family = self.family_of_path(str(sym.path))
        return (sym.name, family) if family is not None else None

    def target_member(self, sym: SymbolRecord) -> tuple[str, str, tuple[str, ...]] | None:
        """T-06 の対象 ``sym`` の ``(名前, 言語族, 名前で結ぶ使用種別)``。メソッドは呼び出しと読み書き（メソッドの参照）、クラスの
        ``Field`` / ``Property`` / ``Constant`` は読み書きだけ（BUG-0095）。それ以外・関係を抽出しない言語は ``None``。"""
        if sym.kind is Kind.METHOD:
            uses: tuple[str, ...] = MEMBER_USES
        elif sym.kind in _REF_TARGET_KINDS and "." in sym.qualified_name:
            uses = REF_USES
        else:
            return None
        family = self.family_of_path(str(sym.path))
        return (sym.name, family, uses) if family is not None else None

    def verdict_for(self, row: NameCallRow) -> Verdict | None:
        """呼び出し・参照箇所 ``row`` の候補（モジュールの docstring の段の順）。候補が無ければ ``None``。"""
        family = family_of(row.language)
        if family is None:
            return None
        site = str(row.path)
        enclosing = row.source_urn.partition("#")[2]  # 囲みの修飾名（モジュール直下なら空）
        pool = pool_of(row.use_kind)
        key = (row.name, family, site, enclosing, pool)
        if key not in self._verdicts:
            self._verdicts[key] = self._decide(row.name, family, site, enclosing, pool)
        return self._verdicts[key]

    def _decide(self, name: str, family: str, site: str, enclosing: str, pool: Pool = "call") -> Verdict | None:
        found = self._defs_named(name, family, pool)
        if not found:
            return None
        builtin = name in _BUILTIN_METHODS[family] if pool == "call" else is_builtin_attribute(name, family)
        external = self._external_members(name, family) if pool == "ref" and not builtin else ()

        def own(s: SymbolRecord) -> bool:
            """呼び出しを囲むクラス自身のメソッド。``self.m()`` は抽出器が結ぶので、型の分からない受け手は別のオブジェクト
            （``def kill(self): self.proc.kill()`` のような委譲）。候補には数えるが、唯一の候補でも結ばない。"""
            owner = s.qualified_name.rpartition(".")[0]
            return bool(owner) and str(s.path) == site and (enclosing == owner or enclosing.startswith(owner + "."))

        chain = ("", *self.roots(site))  # 外側から内側
        visible = [(s, t, self._scope_root(str(s.path))) for s, t in found]
        visible = [v for v in visible if v[2] in chain]
        same = _unique(s for s, _, _ in visible if str(s.path) == site)
        if same and not all(own(s) for s in same):
            if len(same) == 1 and own(same[0]):
                return None
            return Verdict(name, family, same, builtin, scope="file", pool=pool, external=external)
        for root in reversed(chain):  # 内側の段から
            in_scope = [(s, t) for s, t, r in visible if r == root]
            if not in_scope:
                continue
            production = _unique(s for s, t in in_scope if not self._is_test_in(s, t, root))
            if not production or (len(production) == 1 and own(production[0])):
                return None  # この段にはテストダブル（か囲みのクラス自身）しか無い。外の段の同名に結ばない
            if root:
                doubles = len(_unique(s for s, t in in_scope if self._is_test_in(s, t, root)))
                return Verdict(
                    name,
                    family,
                    production,
                    builtin,
                    scope="fixture",
                    root=root,
                    test_doubles=doubles,
                    pool=pool,
                    external=external,
                )
            doubles = len({_ident(s) for s, t in found if t} - {_ident(s) for s in production})
            return Verdict(
                name,
                family,
                production,
                builtin,
                scope="workspace",
                test_doubles=doubles,
                pool=pool,
                external=external,
            )
        return None

    def for_target(self, sym: SymbolRecord, rows: Iterable[NameCallRow]) -> TargetSites:
        """``rows``（対象の名前の型不明の呼び出し）を、対象に結ぶもの・対象が候補だが結べないものに分ける。

        候補に対象が入っていない箇所（もっと近い同名の定義がある・別のフィクスチャの中など）はどちらにも入れない。
        """
        out = TargetSites()
        for r in rows:
            v = self.verdict_for(r)
            if v is None:
                continue
            if v.owned_by(sym):
                out.owned.append((r, v))
            elif v.considers(sym):
                out.unattributed.setdefault(v.key(), Unattributed(v)).add([r])
        return out

    def sites_named(
        self, name: str, family: str, *, include_tests: bool = True, uses: Sequence[str] = CALL_USES
    ) -> list[NameCallRow]:
        return queries.name_calls_named(
            self.conn, self.ws, name, _LANGUAGES[family], include_tests=include_tests, uses=uses
        )

    def calls_from(self, urn: str) -> list[NameCallRow]:
        """``urn`` の中の型不明の呼び出し（T-07 の outgoing。属性の読み書きは入れない）。"""
        return queries.name_calls_from(self.conn, urn, uses=CALL_USES)
