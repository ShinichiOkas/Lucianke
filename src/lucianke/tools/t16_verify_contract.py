"""T-16 ``verify_contract``（仕様書 §11.6 T-16、M3）。入口だけ（呼ぶと ``E_UNSUPPORTED``。``tools/_unsupported.py``）。"""

from __future__ import annotations

from pydantic import Field

from lucianke.tools._base import CommonArgs
from lucianke.tools._unsupported import planned_tool
from lucianke.tools.t05_get_symbol_bodies import TargetRefArg

__all__ = ["SPEC", "VerifyContractArgs"]


class VerifyContractArgs(CommonArgs):
    target: TargetRefArg = Field(
        description="The function or method to verify (urn, or path + symbol_name, or path + line)."
    )
    postconditions: list[str] = Field(
        min_length=1,
        description="1 or more constraint expressions that must hold on return (grammar: spec T-16).",
    )
    preconditions: list[str] | None = Field(
        default=None,
        description="Assumed on entry. Omitted: the PRE contracts of extract_code_contracts are used.",
    )
    invariants: list[str] | None = Field(default=None, description="Loop and class invariants.")
    check_implicit_safety: bool = Field(
        default=True,
        description="Also check division by zero, out-of-range index, null dereference and integer overflow.",
    )
    loop_unroll_depth: int = Field(
        default=3, ge=0, le=10, description="How many times for / while loops are unrolled (0-10)."
    )
    timeout_ms: int = Field(default=500, ge=50, le=5000, description="Solver time limit (50-5000 ms).")
    assume_callees: dict[str, list[str]] | None = Field(
        default=None,
        description="Callee name or URN -> postcondition expressions, to model calls the tool cannot see into.",
    )


SPEC = planned_tool(
    name="verify_contract",
    tid="T-16",
    args_model=VerifyContractArgs,
    milestone="M3",
    default_max_tokens=2000,
    purpose=(
        "Decide with an SMT solver whether a function satisfies given postconditions (and invariants) under its "
        "preconditions, and return a concrete counterexample input when it does not."
    ),
    page_unit="result",
    records_key="results",
)
