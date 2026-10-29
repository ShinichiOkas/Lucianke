"""ワークスペース層の共有型（設計書 §7.3）。他の層（syntax・lsp・edit・tools）はこの型だけを受け取る。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True, kw_only=True)
class PythonEnv:
    interpreter: str | None  # 実行ファイルの絶対パス。無ければ None
    version: str | None = None
    manager: str | None = None  # "venv" | "uv" | "poetry" | "conda" | "path" | "explicit"


@dataclass(frozen=True, slots=True, kw_only=True)
class SubProject:
    path: str  # ルート相対（'/' 区切り）
    marker: str  # "pyproject.toml" など


@dataclass(frozen=True, slots=True, kw_only=True)
class Workspace:
    """登録済みのワークスペース 1 つ（仕様書 §2、§7.2）。"""

    workspace_id: str
    root: Path  # 実体パス（realpath 済み）
    exclude_globs: tuple[str, ...] = ()
    python_env: PythonEnv | None = None
    default_base_ref: str = "HEAD"
    sub_projects: tuple[SubProject, ...] = field(default_factory=tuple)
    case_insensitive: bool = False  # ファイルシステムが大文字小文字を区別しないか
