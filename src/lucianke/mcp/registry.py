"""ツール登録表と ``tools/list`` 用のスキーマ生成（設計書 §7.1.1、§4.3）。

``ToolSpec`` / ``CommonArgs`` / ``ToolContext`` の定義は ``lucianke.tools._base``（``tools`` は ``mcp`` を import できない
ため。設計書 §4.2）。ここは同じ名前を再公開し、``ToolRegistry``（名前 → ``ToolSpec`` の表）と入力スキーマの生成を持つ。
SDK の型には依存しない（``server.py`` が ``ToolDescriptor`` を SDK の ``Tool`` に写す）。
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from lucianke.core.limits import MIN_MAX_TOKENS
from lucianke.tools._base import (
    GRAPH_FORMATS,
    WORKING_TREE,
    CallView,
    CommonArgs,
    FormatName,
    Services,
    ToolContext,
    ToolHandler,
    ToolSpec,
)

__all__ = [
    "GRAPH_FORMATS",
    "WORKING_TREE",
    "CallView",
    "CommonArgs",
    "FormatName",
    "Services",
    "ToolContext",
    "ToolDescriptor",
    "ToolHandler",
    "ToolRegistry",
    "ToolSpec",
    "build_input_schema",
]

MILESTONES = ("M1", "M2", "M3")


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """``tools/list`` の 1 件。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool


class ToolRegistry:
    """名前 → ``ToolSpec``。登録順を保つ。登録時に宣言の不備（共通パラメータの継承漏れなど）を弾く。"""

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool already registered: {spec.name}")
        if not (isinstance(spec.args_model, type) and issubclass(spec.args_model, CommonArgs)):
            raise TypeError(f"{spec.name}: args_model must subclass CommonArgs")
        if spec.args_model.model_config.get("extra") != "forbid":
            raise TypeError(f"{spec.name}: args_model must set extra='forbid'")
        if spec.default_max_tokens < MIN_MAX_TOKENS:
            raise ValueError(f"{spec.name}: default_max_tokens must be >= {MIN_MAX_TOKENS}")
        if spec.milestone not in MILESTONES:
            raise ValueError(f"{spec.name}: unknown milestone {spec.milestone!r}")
        self._specs[spec.name] = spec
        self._schemas[spec.name] = build_input_schema(spec)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    def input_schema(self, name: str) -> dict[str, Any]:
        """``tools/list`` で返す ``inputSchema``（登録時に生成して保持。呼び出しごとにコピーを返す）。"""
        return copy.deepcopy(self._schemas[name])

    def descriptors(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name=s.name,
                description=s.description or s.name,
                input_schema=self.input_schema(s.name),
                read_only=not s.writes,
            )
            for s in self._specs.values()
        ]


# --- スキーマ生成 ---------------------------------------------------------------------------


def _strip_titles(schema: Any) -> None:
    """pydantic が各所に付ける ``title``（プロパティ名の言い換え）を消す。トークンの無駄で、情報が増えない。"""
    if not isinstance(schema, dict):
        return
    if isinstance(schema.get("title"), str):
        del schema["title"]
    for sub in schema.get("properties", {}).values():
        _strip_titles(sub)
    for key in ("anyOf", "allOf", "oneOf"):
        for sub in schema.get(key, []):
            _strip_titles(sub)
    _strip_titles(schema.get("items"))
    for sub in schema.get("$defs", {}).values():
        _strip_titles(sub)


def _unwrap_optional(prop: dict[str, Any]) -> dict[str, Any]:
    """``anyOf: [X, null]`` かつ ``default: null`` を ``X`` にする（省略可能は ``required`` に入れないことで表す）。"""
    options = prop.get("anyOf")
    if not options or prop.get("default", ...) is not None:
        return prop
    non_null = [o for o in options if o != {"type": "null"}]
    if len(non_null) != 1 or len(options) != 2:
        return prop
    out = {k: v for k, v in prop.items() if k not in ("anyOf", "default")}
    out.update(non_null[0])
    return out


def build_input_schema(spec: ToolSpec) -> dict[str, Any]:
    """引数モデルの JSON Schema に、ツールごとの既定値と ``additionalProperties: false`` を反映する。"""
    schema = copy.deepcopy(spec.args_model.model_json_schema())
    _strip_titles(schema)
    schema["type"] = "object"
    schema["additionalProperties"] = False
    props: dict[str, Any] = schema.setdefault("properties", {})
    for name, prop in list(props.items()):
        props[name] = _unwrap_optional(prop)
    props["max_tokens"] = {
        "type": "integer",
        "minimum": MIN_MAX_TOKENS,
        "default": spec.default_max_tokens,
        "description": f"Upper bound of the whole response in tokens (min {MIN_MAX_TOKENS}).",
    }
    formats = ["compact", "json", *(GRAPH_FORMATS if spec.allows_graph else ())]
    props["format"] = {**props["format"], "enum": formats}
    if not spec.accepts_revision:
        props["revision"] = {**props["revision"], "description": "Ignored by this tool (a warning is returned)."}
    return schema
