"""T-18 ``analyze_git_diff_symbols``（仕様書 §11.7 T-18、M2）。入口だけ（呼ぶと ``E_UNSUPPORTED``。``tools/_unsupported.py``）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from lucianke.tools._base import CommonArgs
from lucianke.tools._unsupported import planned_tool

__all__ = ["SPEC", "AnalyzeGitDiffSymbolsArgs"]

ChangeType = Literal["ADDED", "MODIFIED_BODY", "MODIFIED_SIG", "DELETED", "RENAMED"]


class AnalyzeGitDiffSymbolsArgs(CommonArgs):
    base_ref: str = Field(default="HEAD", description='Compare from: "HEAD" | "STAGED" | branch | tag | SHA.')
    target_ref: str = Field(
        default="WORKING_TREE",
        description='Compare to: "WORKING_TREE" | "STAGED" | "HEAD" | branch | tag | SHA.',
    )
    scope_paths: list[str] | None = Field(default=None, description="Paths or globs to diff.")
    change_types: list[ChangeType] | None = Field(
        default=None,
        description="Only these change kinds: ADDED | MODIFIED_BODY | MODIFIED_SIG | DELETED | RENAMED.",
    )
    include_diagnostics: bool = Field(
        default=True, description="Add the current diagnostics that overlap changed lines."
    )
    include_impact_and_tests: bool = Field(
        default=True, description="Compute the impact and the minimal tests to run."
    )
    impact_max_depth: int = Field(
        default=2, ge=1, le=5, description="Depth passed to the impact analysis (1-5)."
    )


SPEC = planned_tool(
    name="analyze_git_diff_symbols",
    tid="T-18",
    args_model=AnalyzeGitDiffSymbolsArgs,
    milestone="M2",
    default_max_tokens=3000,
    purpose=(
        "Turn a Git diff (uncommitted, staged or between refs) into symbol changes (added, deleted, signature or body "
        "changed, renamed) with the impact and the minimal tests to run. Use it to review your own or someone else's "
        "changes."
    ),
    allows_graph=True,
    accepts_revision=False,
    page_unit="symbol",
    records_key="changes",
)
