"""中核ドメインモデル（設計書 §5）。

ここの型はすべて不変（``frozen=True``）で、ツール・サービス間の受け渡しに使う。

位置の規約（ADR-14）: 内部の位置は **0 始まりの行・0 始まりのコードポイント列**。外部（JSON・compact・
ツール引数）は 1 始まり（仕様書 §5.10）。変換は ``to_external()`` / ``from_external()`` と、各型の
``to_json()`` / ``from_json()`` だけで行う。LSP の UTF-16 / UTF-8 単位との変換は ``textpos.LineIndex``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, Literal, NewType

from lucianke.core.urn import Urn, parse

__all__ = [
    "Capability",
    "Confidence",
    "EngineChoice",
    "FileInfo",
    "Kind",
    "LspStatus",
    "Position",
    "Range",
    "RelPath",
    "SymbolRecord",
    "TargetRef",
    "Tier",
    "Urn",
]

RelPath = NewType("RelPath", str)
"""ワークスペースルート相対・``/`` 区切りのパス。

生成してよいのは workspace 層の封じ込め関数（``workspace/paths.py`` の ``confine``）だけ。
core では型としてだけ使い、``RelPath(...)`` で包むのは封じ込め済みの文字列に限る（設計書 §9.4）。
"""


# --- 精度・ティア・能力・種別 -----------------------------------------------------------


class Confidence(StrEnum):
    """精度水準（仕様書 §8.3）。大小比較ができ、``exact`` が最高。

    ``exact > structural > heuristic > lexical_fallback``。``min()`` で最も低い水準が取れる。
    ``str`` としての辞書順ではなく、水準の順序で比較する（``==`` と ``hash`` は ``str`` のまま）。
    """

    EXACT = "exact"
    STRUCTURAL = "structural"
    HEURISTIC = "heuristic"
    LEXICAL_FALLBACK = "lexical_fallback"

    @property
    def rank(self) -> int:
        """大きいほど高精度（lexical_fallback=0 … exact=3）。"""
        return _CONFIDENCE_RANK[self.value]

    def __lt__(self, other: object) -> bool:
        rank = _rank_of(other)
        return NotImplemented if rank is None else self.rank < rank

    def __le__(self, other: object) -> bool:
        rank = _rank_of(other)
        return NotImplemented if rank is None else self.rank <= rank

    def __gt__(self, other: object) -> bool:
        rank = _rank_of(other)
        return NotImplemented if rank is None else self.rank > rank

    def __ge__(self, other: object) -> bool:
        rank = _rank_of(other)
        return NotImplemented if rank is None else self.rank >= rank


def _rank_of(other: object) -> int | None:
    """比較相手の水準の順位。``Confidence`` と、水準の値の文字列（``"exact"`` など）を受け付ける。

    水準でない ``str`` は ``TypeError``（``str`` の辞書順比較に落ちて黙って誤答するのを防ぐ）。
    それ以外の型は ``None``（= ``NotImplemented``）。
    """
    if isinstance(other, Confidence):
        return other.rank
    if isinstance(other, str):
        try:
            return _CONFIDENCE_RANK[other]
        except KeyError:
            raise TypeError(f"{other!r} is not a Confidence level") from None
    return None


_CONFIDENCE_RANK: dict[str, int] = {
    "lexical_fallback": 0,
    "heuristic": 1,
    "structural": 2,
    "exact": 3,
}


class Tier(IntEnum):
    """言語ティア（仕様書 第 8 章）。"""

    T1 = 1
    T2 = 2
    T3 = 3
    T4 = 4


class Capability(StrEnum):
    """エンジン選択の単位（設計書 ADR-10、§7.8.2）。"""

    SKELETON = "skeleton"
    SEARCH = "search"
    DEFINITION = "definition"
    BODY = "body"
    REFERENCES = "references"
    IMPLEMENTATIONS = "implementations"
    CALL_HIERARCHY = "call_hierarchy"
    DIAGNOSTICS = "diagnostics"
    RENAME = "rename"
    CODE_ACTION = "code_action"
    STRUCTURAL_EDIT = "structural_edit"
    SYMBOL_EDIT = "symbol_edit"


class Kind(StrEnum):
    """シンボルの種別（仕様書 §6.1 の ``kind`` 語彙。追加は仕様書の改版で行う）。値は外部表現（先頭大文字）。"""

    MODULE = "Module"
    CLASS = "Class"
    INTERFACE = "Interface"
    STRUCT = "Struct"
    ENUM = "Enum"
    TRAIT = "Trait"
    TYPE = "Type"
    FUNCTION = "Function"
    METHOD = "Method"
    CONSTRUCTOR = "Constructor"
    FIELD = "Field"
    PROPERTY = "Property"
    VARIABLE = "Variable"
    CONSTANT = "Constant"
    TABLE = "Table"
    COLUMN = "Column"
    ENDPOINT = "Endpoint"
    MESSAGE = "Message"
    SERVICE = "Service"
    NODE = "Node"
    EDGE = "Edge"
    CELL = "Cell"
    TAG = "Tag"
    KEY = "Key"
    SECTION = "Section"

    @classmethod
    def parse(cls, text: str) -> Kind:
        """大文字小文字を無視して読む。``T-03`` の ``symbol_kinds`` は小文字形（``"method"``）で来る。未知は ``ValueError``。"""
        try:
            return _KIND_BY_FOLDED[text.casefold()]
        except KeyError:
            raise ValueError(f"unknown symbol kind: {text!r}") from None


_KIND_BY_FOLDED: dict[str, Kind] = {k.value.casefold(): k for k in Kind}


class LspStatus(StrEnum):
    """``_resolution_meta.lsp_status`` の値（仕様書 §5.5）。"""

    READY = "ready"
    INDEXING = "indexing"
    STANDBY = "standby"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "n/a"


# --- 位置 -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class Position:
    """0 始まりの行・0 始まりのコードポイント列（内部表現。ADR-14）。"""

    line: int
    col: int

    def __post_init__(self) -> None:
        if self.line < 0 or self.col < 0:
            raise ValueError(f"Position must be non-negative: ({self.line}, {self.col})")

    def to_external(self) -> tuple[int, int]:
        """外部表現 ``(行, 列)``。どちらも 1 始まり、列はコードポイント（仕様書 §5.10）。"""
        return (self.line + 1, self.col + 1)

    @classmethod
    def from_external(cls, line: int, col: int) -> Position:
        """外部表現（1 始まり）から内部表現へ。0 以下は ``ValueError``。"""
        if line < 1 or col < 1:
            raise ValueError(f"external positions are 1-based: ({line}, {col})")
        return cls(line - 1, col - 1)


@dataclass(frozen=True, slots=True)
class Range:
    """``start`` を含み ``end`` を含まない範囲（LSP と同じ半開区間）。"""

    start: Position
    end: Position

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"Range end precedes start: {self.start} > {self.end}")

    @property
    def is_empty(self) -> bool:
        return self.start == self.end

    def contains(self, pos: Position) -> bool:
        """``pos`` が範囲内か。半開区間で、空の範囲はその 1 点だけを含む。"""
        return self.start <= pos < self.end or pos == self.start

    def contains_range(self, other: Range) -> bool:
        return self.start <= other.start and other.end <= self.end

    def to_external(self) -> dict[str, int]:
        """外部表現 ``{start_line, start_col, end_line, end_col}``（すべて 1 始まり、``end_*`` は終端位置そのもの = 排他的）。"""
        sl, sc = self.start.to_external()
        el, ec = self.end.to_external()
        return {"start_line": sl, "start_col": sc, "end_line": el, "end_col": ec}

    @classmethod
    def from_external(cls, start_line: int, start_col: int, end_line: int, end_col: int) -> Range:
        return cls(Position.from_external(start_line, start_col), Position.from_external(end_line, end_col))

    def line_span_external(self) -> tuple[int, int]:
        """行範囲 ``(開始行, 終了行)``。1 始まり、**両端を含む**（仕様書 §5.10 の ``@L<start>-<end>``）。

        終端が 2 行目以降の 0 列にあるとき（行全体を選ぶ LSP 流の範囲）は、その行を含まない
        （終端は排他的なので、最後の行は 1 つ前）。
        """
        last = self.end.line + 1
        if self.end.col == 0 and self.end.line > self.start.line:
            last = self.end.line
        return (self.start.line + 1, last)

    @classmethod
    def from_line_span_external(cls, start_line: int, end_line: int) -> Range:
        """行だけの範囲（1 始まり・両端を含む）から、行全体を覆う ``Range`` を作る（終端は次行の 0 列）。"""
        if start_line < 1 or end_line < start_line:
            raise ValueError(f"invalid line span: {start_line}-{end_line}")
        return cls(Position(start_line - 1, 0), Position(end_line, 0))


# --- 対象指定 ---------------------------------------------------------------------------

_TARGET_REF_KEYS = ("urn", "path", "symbol_name", "line", "character", "variable_name")


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetRef:
    """対象指定（仕様書 §5.3）。

    ``line`` / ``character`` は **内部表現（0 始まり・コードポイント）** で保持する。外部（JSON）は 1 始まりで、
    ``from_json`` / ``to_json`` が変換する。``urn`` は文字列のまま保持する（解決側が ``urn.parse`` して
    ``E_TARGET_NOT_FOUND`` / ``E_INVALID_ARGUMENT`` を使い分ける）。
    """

    urn: str | None = None
    path: str | None = None
    symbol_name: str | None = None
    line: int | None = None
    character: int | None = None
    variable_name: str | None = None

    def resolution_form(self) -> tuple[Literal["urn", "line", "symbol_name", "path"], tuple[str, ...]]:
        """仕様書 §5.3 規則 4: 使う形と、無視される形。

        戻り値は ``(使う形, 無視される形の名前の列)``。優先順位は ``urn`` > ``line`` > ``symbol_name``。
        どれも無く ``path`` だけなら ``"path"``（ファイル全体）。無視される形は ``warnings[]`` に記録する。
        何も指定が無い、または ``line`` / ``symbol_name`` に ``path`` が無いときは ``ValueError``。
        """
        present: list[Literal["urn", "line", "symbol_name"]] = []
        if self.urn is not None:
            present.append("urn")
        if self.line is not None:
            present.append("line")
        if self.symbol_name is not None:
            present.append("symbol_name")
        if not present:
            if self.path is None:
                raise ValueError("TargetRef needs one of urn, path+symbol_name, path+line, or path")
            return ("path", ())
        form = present[0]
        if form != "urn" and self.path is None:
            raise ValueError(f"TargetRef form {form!r} requires 'path'")
        return (form, tuple(present[1:]))

    def to_json(self) -> dict[str, Any]:
        """仕様書 §5.3 の形。``None`` のフィールドは出さない。``line`` / ``character`` は 1 始まり。"""
        out: dict[str, Any] = {}
        if self.urn is not None:
            out["urn"] = self.urn
        if self.path is not None:
            out["path"] = self.path
        if self.symbol_name is not None:
            out["symbol_name"] = self.symbol_name
        if self.line is not None:
            out["line"] = self.line + 1
        if self.character is not None:
            out["character"] = self.character + 1
        if self.variable_name is not None:
            out["variable_name"] = self.variable_name
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> TargetRef:
        """JSON（``line`` / ``character`` は 1 始まり）から作る。未知のキー・型違い・0 以下の行/列は ``ValueError``。"""
        unknown = set(data) - set(_TARGET_REF_KEYS)
        if unknown:
            raise ValueError(f"unknown TargetRef keys: {sorted(unknown)}")
        for key in ("urn", "path", "symbol_name", "variable_name"):
            value = data.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"TargetRef.{key} must be a non-empty string: {value!r}")
        line = _external_index(data.get("line"), "line")
        character = _external_index(data.get("character"), "character")
        if character is not None and line is None:
            raise ValueError("TargetRef.character requires 'line'")
        return cls(
            urn=data.get("urn"),
            path=data.get("path"),
            symbol_name=data.get("symbol_name"),
            line=line,
            character=character,
            variable_name=data.get("variable_name"),
        )


def _external_index(value: object, name: str) -> int | None:
    """1 始まりの外部値を 0 始まりにする。``None`` はそのまま。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"TargetRef.{name} must be an integer >= 1: {value!r}")
    return value - 1


# --- シンボル・ファイル・エンジン選択 ---------------------------------------------------

_SYMBOL_KEYS = frozenset(
    {
        "urn",
        "scip_symbol",
        "path",
        "name",
        "qualified_name",
        "kind",
        "modifiers",
        "range",
        "selection_range",
        "signature",
        "return_type",
        "doc_summary",
        "confidence_level",
        "declared_in",
    }
)
_SYMBOL_REQUIRED = ("urn", "path", "name", "qualified_name", "kind", "range", "confidence_level")


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolRecord:
    """シンボル 1 件（仕様書 §6.2。設計書 §5）。位置は内部表現（0 始まり・コードポイント）。

    ``range`` は定義全体、``selection`` は名前トークン（1 行に収まる）。``declared_in`` は由来
    （``source`` / ``stub`` / ``cython`` / ``pyo3`` / ``proto`` / ``generated``。仕様書 §9.2）。
    """

    urn: Urn
    path: RelPath
    name: str
    qualified_name: str
    kind: Kind
    modifiers: tuple[str, ...] = ()
    range: Range
    selection: Range | None = None
    signature: str | None = None
    return_type: str | None = None
    doc_summary: str | None = None
    declared_in: str = "source"
    confidence: Confidence
    scip_symbol: str | None = None

    def to_json(self) -> dict[str, Any]:
        """仕様書 §6.2 の SymbolRecord。行・列は 1 始まり、キーは ``confidence_level``。

        * ``range`` は行だけ ``{start_line, end_line}``（両端を含む。``Range.line_span_external``）。
          桁は JSON に出ないため ``from_json`` では行全体の範囲として復元される（JSON → オブジェクト → JSON は不変）。
        * ``selection_range`` は ``{line, start_col, end_col}``（``end_col`` は排他的）。複数行にまたがる選択範囲は ``ValueError``。
        * ``declared_in`` は ``"source"`` のとき出さない（仕様書 §6.2 の例に無いため）。
        """
        start_line, end_line = self.range.line_span_external()
        sel: dict[str, int] | None = None
        if self.selection is not None:
            if self.selection.start.line != self.selection.end.line:
                raise ValueError(f"selection must be on a single line: {self.selection}")
            sel = {
                "line": self.selection.start.line + 1,
                "start_col": self.selection.start.col + 1,
                "end_col": self.selection.end.col + 1,
            }
        out: dict[str, Any] = {
            "urn": str(self.urn),
            "scip_symbol": self.scip_symbol,
            "path": str(self.path),
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind.value,
            "modifiers": list(self.modifiers),
            "range": {"start_line": start_line, "end_line": end_line},
            "selection_range": sel,
            "signature": self.signature,
            "return_type": self.return_type,
            "doc_summary": self.doc_summary,
            "confidence_level": self.confidence.value,
        }
        if self.declared_in != "source":
            out["declared_in"] = self.declared_in
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SymbolRecord:
        """``to_json`` の逆。必須キーの欠落・未知のキー・不正な値は ``ValueError``。"""
        unknown = set(data) - _SYMBOL_KEYS
        if unknown:
            raise ValueError(f"unknown SymbolRecord keys: {sorted(unknown)}")
        missing = [k for k in _SYMBOL_REQUIRED if k not in data]
        if missing:
            raise ValueError(f"missing SymbolRecord keys: {missing}")
        rng = data["range"]
        sel_raw = data.get("selection_range")
        selection: Range | None = None
        if sel_raw is not None:
            line = Position.from_external(sel_raw["line"], sel_raw["start_col"])
            end = Position.from_external(sel_raw["line"], sel_raw["end_col"])
            selection = Range(line, end)
        modifiers = data.get("modifiers") or []
        return cls(
            urn=parse(data["urn"]),
            path=RelPath(data["path"]),
            name=data["name"],
            qualified_name=data["qualified_name"],
            kind=Kind(data["kind"]),
            modifiers=tuple(modifiers),
            range=Range.from_line_span_external(rng["start_line"], rng["end_line"]),
            selection=selection,
            signature=data.get("signature"),
            return_type=data.get("return_type"),
            doc_summary=data.get("doc_summary"),
            declared_in=data.get("declared_in", "source"),
            confidence=Confidence(data["confidence_level"]),
            scip_symbol=data.get("scip_symbol"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FileInfo:
    """``files`` テーブルの 1 行に対応する、ファイルのメタ情報（仕様書 §9.2）。"""

    file_id: int
    workspace_id: str
    path: RelPath
    language: str
    tier: Tier
    sha256: str
    line_count: int
    byte_size: int
    is_test: bool
    confidence: Confidence


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineChoice:
    """``EngineSelector.select`` の結果（設計書 ADR-10）。``lsp_status`` の値は ``LspStatus``。"""

    engine: str
    confidence: Confidence
    lsp_status: str = LspStatus.NOT_APPLICABLE.value
    lsp_progress: int | None = None
    warning: str | None = None
