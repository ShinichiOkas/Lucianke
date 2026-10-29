"""Syntax 層の共有型（設計書 §7.4.1・§7.4.2）。parsers・symbols・boundaries・adapters・heuristic がこの型で繋がる。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from lucianke.core.model import Range
from lucianke.core.textpos import LineIndex


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """解析済みのファイル。``tree`` は ``tree_sitter.Tree``（ノードの位置はバイト単位。S-2）。"""

    language_id: str
    sha256: str
    source: bytes
    tree: Any  # tree_sitter.Tree
    line_index: LineIndex
    has_errors: bool
    error_ranges: tuple[Range, ...] = ()  # ERROR / MISSING ノード（0 始まり・コードポイント）


@dataclass(frozen=True, slots=True, kw_only=True)
class LanguageQueries:
    """言語ごとの Tree-sitter クエリの原文（遅延コンパイル）。無いものは None。"""

    tags: str | None = None  # 定義（@definition.function など）と参照
    skeleton: str | None = None
    imports: str | None = None
    guards: str | None = None
    doc: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SkeletonOptions:
    """T-02 の入力のうち骨格の作り方に効くもの（仕様書 §11 T-02）。"""

    include_private: bool = True
    include_doc_summary: bool = True
    include_urns: Literal["public", "all", "none"] = "public"
    include_imports: bool = True
    max_depth: int = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class Match:
    """セレクタ（xpath: / jsonpath: / jq: / toml: / yaml: / cell:）や構文パターンの一致 1 件。"""

    range: Range  # 0 始まり・コードポイント
    text: str
    kind: str  # "Tag" | "Key" | "Cell" | "Section" など（仕様書 §6.1 の kind 語彙）
    name: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
