"""T-17 ``get_symbol_history``（仕様書 §11.7 T-17、M2）。入口だけ（呼ぶと ``E_UNSUPPORTED``。``tools/_unsupported.py``）。"""

from __future__ import annotations

from pydantic import Field

from lucianke.tools._base import CommonArgs
from lucianke.tools._unsupported import planned_tool
from lucianke.tools.t05_get_symbol_bodies import TargetRefArg

__all__ = ["SPEC", "GetSymbolHistoryArgs"]


class GetSymbolHistoryArgs(CommonArgs):
    target: TargetRefArg = Field(description="The symbol (urn, or path + symbol_name, or path + line).")
    max_commits: int = Field(default=5, ge=1, le=50, description="Commits to return (1-50).")
    since: str | None = Field(
        default=None, description="ISO 8601 datetime or a revision; older commits are not scanned."
    )
    include_semantic_diff: bool = Field(
        default=True, description="Add the change kind and a one-line summary per commit."
    )
    include_body_diff: bool = Field(
        default=False, description="Add a unified diff limited to the symbol's range per commit."
    )
    include_co_changed_files: bool = Field(default=True, description="Add the co-change hotspots.")
    co_change_min_ratio: float = Field(
        default=0.3, ge=0.0, le=1.0, description="Hide coupling below this ratio (0.0-1.0)."
    )
    co_change_limit: int = Field(default=5, ge=1, le=20, description="Co-change hotspots to return (1-20).")


SPEC = planned_tool(
    name="get_symbol_history",
    tid="T-17",
    args_model=GetSymbolHistoryArgs,
    milestone="M2",
    default_max_tokens=2000,
    purpose=(
        "Return only the commits that changed one symbol, with message, change kind and line counts, plus the files "
        "and symbols that usually change with it. Use it instead of reading git log -p or blame."
    ),
    page_unit="commit",
    records_key="commits",
)
