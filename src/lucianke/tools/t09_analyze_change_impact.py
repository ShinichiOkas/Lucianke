"""T-09 ``analyze_change_impact``（仕様書 §11.4 T-09、M2）。入口だけ（呼ぶと ``E_UNSUPPORTED``。``tools/_unsupported.py``）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from lucianke.tools._base import CommonArgs
from lucianke.tools._unsupported import planned_tool
from lucianke.tools.t05_get_symbol_bodies import TargetRefArg

__all__ = ["SPEC", "AnalyzeChangeImpactArgs"]


class AnalyzeChangeImpactArgs(CommonArgs):
    target: TargetRefArg = Field(
        description="The symbol you plan to change (urn, or path + symbol_name, or path + line)."
    )
    change_kind: Literal["signature_change", "behavior_change", "deletion"] = Field(
        default="signature_change", description="signature_change | behavior_change | deletion."
    )
    max_depth: int = Field(default=3, ge=1, le=6, description="Depth of the transitive dependents (1-6).")
    include_tests: bool = Field(default=True, description="Compute affected_test_targets.")
    test_globs: list[str] | None = Field(
        default=None,
        description="Patterns that mark test files (replaces the server default: **/tests/**, **/test_*.py, "
        "**/*_test.py, **/*.spec.*, **/*.test.*).",
    )
    include_co_change: bool = Field(
        default=True, description="Add files that change together with the target in Git history."
    )
    min_coupling_ratio: float = Field(
        default=0.3, ge=0.0, le=1.0, description="Lower bound of the co-change coupling ratio (0.0-1.0)."
    )


SPEC = planned_tool(
    name="analyze_change_impact",
    tid="T-09",
    args_model=AnalyzeChangeImpactArgs,
    milestone="M2",
    default_max_tokens=2500,
    purpose=(
        "Before changing or deleting a symbol, list the call sites that break directly, the transitively affected "
        "modules, the minimal tests to run, and the files that always change with it (co-change coupling)."
    ),
    hint="find_references_and_implementations and query_call_hierarchy list the callers.",
    allows_graph=True,
    page_unit="site",
    records_key="direct_breakage_candidates",
)
