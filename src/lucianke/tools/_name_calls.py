"""型の分からない受け手のメソッド呼び出し（``x.name(...)``）を、名前で対象に結ぶ（BUG-0071。仕様書 §8.3、T-06 / T-07 の処理規約）。

索引器は、受け手の型が構文から読めない属性呼び出し（注釈が無い・``Any``・``self.x`` の型が不明・呼び出しの戻り値など）を
``relations`` に入れられない（先の URN が決まらない）。その位置と名前は内部表 ``_name_calls`` に残っているので、T-06 / T-07 が
問い合わせのときにここで判定する。呼ぶ側の AI にとって、本番の呼び出し元が黙って 0 件に見える方が、印の付いた推定より悪い
（編集の影響を見落とす）ため。

* **候補**: 呼び出し箇所と同じ言語族（Python / TS・JS）のワークスペース内の ``Method`` で、名前が同じもの。テスト以外の定義が
  1 つでもあれば、テストファイルの定義（テストダブル）は数えない（本番のメソッドの写しなので、名前を曖昧にしない）。同じ
  ``(パス, 修飾名)`` のオーバーロードは 1 つに数える。
* **結ぶ**: 候補がちょうど 1 つで、名前が組み込み型のメソッド名（``syntax/builtin_names.py`` の ``PYTHON_BUILTIN_METHODS`` /
  ``JS_BUILTIN_METHODS``。``list.append``・``dict.get``・``Array.push`` など）でないとき。その位置をその候補への呼び出しとして返す
  （``Confidence.HEURISTIC``、json の ``match: "name"``、compact の ``conf=heuristic``）。
* **結ばない**: 候補が 2 つ以上、または組み込み型と同名。どの候補にも足さず、件数と先頭の位置を応答の ``warnings`` と
  ``data.unattributed_calls`` に要約する（全候補に足すと、``run``・``get`` のような名前で参照の一覧が推定で埋まるため）。
* 候補が 0（ワークスペースにその名前のメソッドが無い）なら何もしない（外部・組み込みの呼び出しで、ワークスペースの見落としではない）。
* ``exact``（LSP）の答えには混ぜない。T-06 / T-07 は ``exact`` のとき、結べる（候補が対象 1 つ）のに LSP の答えに無い位置の
  件数と先頭を ``warnings`` に書くだけにする（``Any`` の受け手は LSP も型を決められない）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from lucianke.core.model import Kind, SymbolRecord
from lucianke.index import queries
from lucianke.index.queries import NameCallRow
from lucianke.syntax.builtin_names import JS_BUILTIN_METHODS, PYTHON_BUILTIN_METHODS

__all__ = [
    "MATCH_NAME",
    "NameIndex",
    "Unattributed",
    "Verdict",
    "family_of",
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


def family_of(language: str | None) -> str | None:
    """言語 ID → 言語族（``"python"`` / ``"js"``）。関係を抽出しない言語は ``None``。"""
    return _FAMILIES.get(language or "")


def site_key(row: NameCallRow) -> tuple[str, int, int]:
    """``(パス, 行, 列)``（1 始まり）。T-06 の参照の重複判定と同じキー。"""
    return (str(row.path), row.site.line + 1, row.site.col + 1)


def _label(sym: SymbolRecord) -> str:
    return f"{sym.qualified_name} @{sym.path}:{sym.range.start.line + 1}"


@dataclass(frozen=True, slots=True)
class Verdict:
    """名前 ``name`` の型不明の呼び出しを、どの定義に結べるか。"""

    name: str
    family: str
    candidates: tuple[SymbolRecord, ...]  # 競合する定義（テストダブルの規則の後）。空にはしない
    builtin: bool  # 組み込み型にも同名のメソッドがある
    test_doubles: int = 0  # 数えなかったテストファイルの同名の定義の数

    def only_text(self) -> str:
        """``Packer.pack is the only method named 'pack' in the workspace (besides 1 in test files)``。"""
        owner = self.owner
        assert owner is not None
        besides = f" (besides {self.test_doubles} in test files)" if self.test_doubles else ""
        return f"{owner.qualified_name} is the only method named {self.name!r} in the workspace{besides}"

    @property
    def owner(self) -> SymbolRecord | None:
        """名前で結んでよい唯一の定義。結べないなら ``None``。"""
        return self.candidates[0] if len(self.candidates) == 1 and not self.builtin else None

    def owned_by(self, sym: SymbolRecord) -> bool:
        owner = self.owner
        return owner is not None and str(owner.path) == str(sym.path) and owner.qualified_name == sym.qualified_name

    def reason(self) -> str:
        """結ばない理由（英語。warnings に入る）。"""
        parts: list[str] = []
        n = len(self.candidates)
        if n > 1:
            shown = ", ".join(_label(s) for s in self.candidates[:LABELS_MAX])
            more = f", … {n - LABELS_MAX} more" if n > LABELS_MAX else ""
            parts.append(f"{n} methods share the name ({shown}{more})")
        if self.builtin:
            parts.append("built-in types also have a method of that name")
        return "; ".join(parts)

    def reason_code(self) -> str:
        """``data.unattributed_calls[].reason``: ``ambiguous``（同名が 2 つ以上）か ``builtin_name``（組み込み型と同名）。"""
        return "ambiguous" if len(self.candidates) > 1 else "builtin_name"

    def reason_json(self) -> dict[str, Any]:
        return {
            "candidates": [
                {"qualified_name": s.qualified_name, "path": str(s.path), "line": s.range.start.line + 1}
                for s in self.candidates[:DATA_SAMPLE_MAX]
            ],
            "candidates_total": len(self.candidates),
            "builtin_name": self.builtin,
        }


@dataclass(slots=True)
class Unattributed:
    """結ばなかった型不明の呼び出しの集まり（名前ごと）。位置は重複を除いて（パス、行、列）順。"""

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


def sample_text(rows: Sequence[NameCallRow], limit: int = SAMPLE_MAX) -> str:
    """``a.py:10, b.py:22, … 3 more``（呼び出し箇所の先頭）。"""
    shown = ", ".join(f"{r.path}:{r.site.line + 1}" for r in rows[:limit])
    return shown + (f", … {len(rows) - limit} more" if len(rows) > limit else "")


def sites_json(rows: Sequence[NameCallRow], enclosing: Any) -> list[dict[str, Any]]:
    """``data.unattributed_calls[].sites``（先頭 ``DATA_SAMPLE_MAX`` 件）。``enclosing`` は URN → 囲みの名前。"""
    return [
        {
            "path": str(r.path),
            "line": r.site.line + 1,
            "col": r.site.col + 1,
            "enclosing_name": enclosing(r.source_urn),
            "is_test": r.is_test,
        }
        for r in rows[:DATA_SAMPLE_MAX]
    ]


class NameIndex:
    """1 回の問い合わせの間の、名前 → ``Verdict`` と言語のキャッシュ（同じ読み取り接続のスレッドの中で使う）。"""

    def __init__(self, conn: sqlite3.Connection, workspace_id: str) -> None:
        self.conn, self.ws = conn, workspace_id
        self._verdicts: dict[tuple[str, str], Verdict | None] = {}
        self._languages: dict[str, str | None] = {}

    def family_of_path(self, path: str) -> str | None:
        if path not in self._languages:
            info = queries.file_by_path(self.conn, self.ws, path)
            self._languages[path] = info.language if info is not None else None
        return family_of(self._languages[path])

    def verdict(self, name: str, family: str) -> Verdict | None:
        """``name`` の候補。ワークスペースにその名前のメソッドが無ければ ``None``。"""
        key = (name, family)
        if key not in self._verdicts:
            found = queries.methods_named(self.conn, self.ws, name, _LANGUAGES[family])
            production = [s for s, is_test in found if not is_test]
            chosen = production or [s for s, _ in found]
            unique: dict[tuple[str, str], SymbolRecord] = {}
            for s in chosen:
                unique.setdefault((str(s.path), s.qualified_name), s)
            doubles = {(str(s.path), s.qualified_name) for s, is_test in found if is_test} if production else set()
            self._verdicts[key] = (
                Verdict(
                    name,
                    family,
                    tuple(unique.values()),
                    name in _BUILTIN_METHODS[family],
                    test_doubles=len(doubles),
                )
                if unique
                else None
            )
        return self._verdicts[key]

    def target_verdict(self, sym: SymbolRecord) -> Verdict | None:
        """対象のメソッド ``sym`` の名前の ``Verdict``（``Method`` でない・関係を抽出しない言語なら ``None``）。"""
        if sym.kind is not Kind.METHOD:
            return None
        family = self.family_of_path(str(sym.path))
        return self.verdict(sym.name, family) if family is not None else None

    def sites_named(self, verdict: Verdict, *, include_tests: bool = True) -> list[NameCallRow]:
        return queries.name_calls_named(
            self.conn, self.ws, verdict.name, _LANGUAGES[verdict.family], include_tests=include_tests
        )

    def calls_from(self, urn: str) -> list[NameCallRow]:
        return queries.name_calls_from(self.conn, urn)
