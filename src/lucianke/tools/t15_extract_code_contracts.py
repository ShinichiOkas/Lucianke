"""T-15 ``extract_code_contracts``（仕様書 §11.6 T-15、M3）。入口だけ（呼ぶと ``E_UNSUPPORTED``。``tools/_unsupported.py``）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from lucianke.tools._base import CommonArgs
from lucianke.tools._unsupported import planned_tool

__all__ = ["SPEC", "ExtractCodeContractsArgs"]

ContractSource = Literal["TYPE", "GUARD", "DOCSTRING", "OPENAPI", "GHERKIN"]


class ExtractCodeContractsArgs(CommonArgs):
    path: str = Field(
        description="Source file, OpenAPI (YAML/JSON), JSON Schema, Gherkin .feature, "
        "or Markdown (embedded OpenAPI/Gherkin only)."
    )
    symbol_name: str | None = Field(
        default=None, description="Only this symbol of a source file (resolved as in §5.3)."
    )
    sources: list[ContractSource] | None = Field(
        default=None,
        description="Kinds to extract: TYPE | GUARD | DOCSTRING | OPENAPI | GHERKIN. Default: all.",
    )
    include_smt: bool = Field(default=True, description="Also produce SMT-LIB v2 expressions.")
    link_external_specs: bool = Field(
        default=True, description="Link the OpenAPI / Gherkin files of the workspace to source endpoints."
    )


SPEC = planned_tool(
    name="extract_code_contracts",
    tid="T-15",
    args_model=ExtractCodeContractsArgs,
    milestone="M3",
    default_max_tokens=2000,
    purpose=(
        "Extract preconditions, postconditions, invariants and thrown errors from type signatures, guard clauses, "
        "structured doc comments, OpenAPI / JSON Schema and Gherkin, as readable expressions and SMT-LIB."
    ),
    page_unit="contract",
    records_key="contracts",
)
