"""T-14 ``find_code_paths``（仕様書 §11.6 T-14、M2）。入口だけ（呼ぶと ``E_UNSUPPORTED``。``tools/_unsupported.py``）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from lucianke.tools._base import CommonArgs
from lucianke.tools._unsupported import planned_tool
from lucianke.tools.t05_get_symbol_bodies import TargetRefArg

__all__ = ["SPEC", "FindCodePathsArgs", "PathEndpoint"]


class PathEndpoint(TargetRefArg):
    """TargetRef（§5.3）に ``variable_name`` を加えたもの。"""

    variable_name: str | None = Field(
        default=None, description="Argument or local variable name of that symbol (data flow only)."
    )


class FindCodePathsArgs(CommonArgs):
    source: PathEndpoint = Field(description="Start symbol (a TargetRef, plus optional variable_name).")
    sink: PathEndpoint = Field(description="End symbol (a TargetRef, plus optional variable_name).")
    flow_type: Literal["control", "data", "both"] = Field(
        default="both", description="control | data | both."
    )
    max_hops: int = Field(default=8, ge=1, le=20, description="Longest path in hops (1-20).")
    max_paths: int = Field(
        default=20, ge=1, le=200, description="Paths to return (1-200); the rest is only counted."
    )
    required_intermediate_filter: str | None = Field(
        default=None,
        description='Symbol name. "name": only paths through it. "!name": only paths that avoid it.',
    )
    include_guards: bool = Field(
        default=True, description="Add the branch condition (when:) around each step."
    )
    timeout_ms: int = Field(default=500, ge=50, le=5000, description="Search time limit (50-5000 ms).")


SPEC = planned_tool(
    name="find_code_paths",
    tid="T-14",
    args_model=FindCodePathsArgs,
    milestone="M2",
    default_max_tokens=2500,
    purpose=(
        "List the control-flow or data-flow paths from a source symbol to a sink symbol, e.g. to confirm that "
        "unsanitized input reaches a sink or to trace how a state variable is rewritten."
    ),
    hint="query_call_hierarchy walks the call chain one direction at a time.",
    allows_graph=True,
    page_unit="path",
    records_key="paths",
)
