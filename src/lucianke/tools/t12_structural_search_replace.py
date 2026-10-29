"""T-12 ``structural_search_replace``（仕様書 §11.5 T-12、M2）。入口だけ（呼ぶと ``E_UNSUPPORTED``。``tools/_unsupported.py``）。"""

from __future__ import annotations

from pydantic import Field

from lucianke.core.model import Confidence
from lucianke.tools._base import CommonArgs
from lucianke.tools._unsupported import planned_tool

__all__ = ["SPEC", "StructuralSearchReplaceArgs"]


class StructuralSearchReplaceArgs(CommonArgs):
    language: str = Field(
        description="Language id (python, typescript, rust, go, ...). Languages without a grammar are E_UNSUPPORTED."
    )
    pattern: str = Field(
        description="ast-grep pattern. $NAME matches one node, $$$NAME matches zero or more nodes."
    )
    rewrite: str | None = Field(
        default=None,
        description="Rewrite template that may use the same meta variables. Omit to search only.",
    )
    constraints: dict[str, str] | None = Field(
        default=None, description="Meta variable name -> regex; only nodes whose text matches are taken."
    )
    scope_paths: list[str] | None = Field(
        default=None,
        description="Directories or files to search. Default: every indexed file of the language.",
    )
    max_matches: int = Field(
        default=200, ge=1, description="Upper bound of matches. Above it nothing is written (applied: false)."
    )
    dry_run: bool = Field(
        default=True, description="Do not write; return the diff and the syntax check only."
    )
    allow_heuristic_mutation: bool = Field(
        default=False, description="Accepted for compatibility; degraded files are never matched."
    )
    expected_file_sha256s: dict[str, str] | None = Field(
        default=None,
        description="path -> sha256 of the files as you read them (64 hex, or a prefix of 12+). "
        "Mismatch is E_PRECONDITION_FAILED.",
    )
    diagnostics_wait_ms: int = Field(
        default=2000,
        ge=0,
        le=10000,
        description="How long to wait for the language server's diagnostics after the write.",
    )


SPEC = planned_tool(
    name="structural_search_replace",
    tid="T-12",
    args_model=StructuralSearchReplaceArgs,
    milestone="M2",
    default_max_tokens=2000,
    purpose=(
        "Search the workspace with syntax-tree patterns ($VAR, $$$ARGS code templates) and optionally rewrite every "
        "match in one codemod. dry_run is true by default."
    ),
    hint="edit_symbol_body replaces one symbol at a time.",
    writes=True,
    max_confidence=Confidence.STRUCTURAL,
    page_unit="match",
    records_key="matches",
)
