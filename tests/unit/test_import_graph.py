"""設計書 §4.2「依存の方向」を静的に検査する。

各サブパッケージが import してよい lucianke 内のサブパッケージを列挙し、それ以外を禁止する。
トップレベルのモジュール（app, lifecycle, config, logging_setup, bootstrap_io, __main__）は合成ルートであり、
何を import してもよいが、サブパッケージから合成ルートを import してはならない。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "lucianke"

SERVICES = {"workspace", "syntax", "lsp", "index", "edit", "engine", "git", "verify"}

ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "index": {"core"},
    "workspace": {"core"},
    "git": {"core", "workspace"},
    "syntax": {"core", "workspace"},
    "lsp": {"core", "workspace", "index"},
    "engine": {"core", "workspace", "syntax", "lsp", "index"},
    "edit": {"core", "workspace", "syntax", "lsp", "index", "engine"},
    "verify": {"core", "workspace", "syntax", "index"},
    "tools": {"core"} | SERVICES,
    "mcp": {"core", "tools"},
    "cli": {"core"} | SERVICES | {"tools", "mcp"},
}

COMPOSITION_ROOT = {"app", "lifecycle", "config", "logging_setup", "bootstrap_io", "__main__", "__init__"}


def _subpackage_of(path: Path) -> str:
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def _imported_subpackages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相対 import は禁止（明示の絶対 import に統一する）
                found.add(f"<relative import in {path.name}>")
                continue
            names = [node.module or ""]
        for name in names:
            parts = name.split(".")
            if parts[0] == "lucianke" and len(parts) > 1:
                found.add(parts[1])
    return found


def test_dependency_directions():
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        sub = _subpackage_of(path)
        if sub in COMPOSITION_ROOT:
            continue
        allowed = ALLOWED.get(sub)
        assert allowed is not None, f"unknown subpackage {sub!r} ({path}); add it to ALLOWED"
        for imp in _imported_subpackages(path):
            if imp.startswith("<relative"):
                violations.append(f"{path.relative_to(SRC)}: {imp}")
            elif imp == sub:
                continue
            elif imp in COMPOSITION_ROOT:
                violations.append(f"{path.relative_to(SRC)} imports composition root {imp!r}")
            elif imp not in allowed:
                violations.append(f"{path.relative_to(SRC)} ({sub}) imports {imp!r}")
    assert not violations, "\n".join(violations)
