"""エラー分類（仕様書 §5.8）と ``LuciankeError``（設計書 §5、§9.1）。

サービス層は §5.8 のコードを持つ ``LuciankeError`` を送出する。それ以外の例外は送出元のバグとみなされ、
パイプラインが ``E_INTERNAL`` に変換する。core の純粋なパーサ（``urn.parse`` など）は ``ValueError`` を送出する。
それを ``E_INVALID_ARGUMENT`` に包むのは、引数を受ける側（ツール層）の仕事である。
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lucianke.core.records import ToolOutput


class ErrorCode(StrEnum):
    """仕様書 §5.8 のエラーコード 14 種。値はそのまま外部に出る文字列。"""

    E_INVALID_ARGUMENT = "E_INVALID_ARGUMENT"
    E_WORKSPACE_NOT_CONFIGURED = "E_WORKSPACE_NOT_CONFIGURED"
    E_REVISION_NOT_FOUND = "E_REVISION_NOT_FOUND"
    E_TARGET_NOT_FOUND = "E_TARGET_NOT_FOUND"
    E_TARGET_AMBIGUOUS = "E_TARGET_AMBIGUOUS"
    E_LSP_UNAVAILABLE = "E_LSP_UNAVAILABLE"
    E_PRECONDITION_FAILED = "E_PRECONDITION_FAILED"
    E_SYNTAX_INVALID = "E_SYNTAX_INVALID"
    E_MUTATION_BLOCKED = "E_MUTATION_BLOCKED"
    E_QUERY_REJECTED = "E_QUERY_REJECTED"
    E_CURSOR_EXPIRED = "E_CURSOR_EXPIRED"
    E_TIMEOUT = "E_TIMEOUT"
    E_UNSUPPORTED = "E_UNSUPPORTED"
    E_INTERNAL = "E_INTERNAL"


class LuciankeError(Exception):
    """仕様書 §5.8 のエラー。

    Attributes:
        code: エラーコード。
        message: 人（と LLM）が読む説明。
        details: コードごとの付随情報（``details.param`` など。§5.8 の「付随情報」列）。
        candidates: ``E_TARGET_AMBIGUOUS`` の候補（``{urn, signature, path, line}``、最大 10 件）。
        partial: ``E_TIMEOUT`` の部分結果。パイプラインが ``data`` に詰める（設計書 §9.1）。
            ``to_json()`` には含めない。
    """

    code: ErrorCode
    message: str
    details: dict[str, Any]
    candidates: list[dict[str, Any]]
    partial: ToolOutput | None

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        candidates: Sequence[Mapping[str, Any]] | None = None,
        partial: ToolOutput | None = None,
    ) -> None:
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = message
        self.details = dict(details) if details else {}
        self.candidates = [dict(c) for c in candidates] if candidates else []
        self.partial = partial

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def to_json(self) -> dict[str, Any]:
        """仕様書 §5.8 のエラーオブジェクト ``{code, message, details, candidates}``。"""
        return {
            "code": self.code.value,
            "message": self.message,
            "details": dict(self.details),
            "candidates": [dict(c) for c in self.candidates],
        }
