"""T-01 ``get_repo_map`` の先頭の概観（仕様書 T-01 処理規約 3。BUG-0120）。

初めてのリポジトリで呼ぶ側の AI が最初に知りたい「どんなパッケージがあり、入口はどこか」を、ファイルブロックの前の数行で答える。

* **パッケージの根**（1 段目）: トップレベルのディレクトリのうちコードのファイルを持つもの（``.`` で始まる隠しディレクトリは道具の設定
  なので除く）。直下にコードのファイルが無く、コードを持つ子が 1 つだけのディレクトリはその子とつなぐ（``src/`` → ``src/mnemo/``、
  ``web/`` → ``web/src/``）。並びは重要度（ファイルのスコア〈``RepoMapFile.score``〉の和）の降順、同じならコードのファイル数の降順、
  パスの辞書順。``ROOT_MAX`` 件まで（残りは ``+N more``）。
* **サブパッケージ**（2 段目）: 根の直下の、コードを持つディレクトリ（同じくつなぐ）。並びは根の順、同じ根ではコードのファイル数の降順
  （いちばん重要なパッケージの中の形を先に全部見せる。PageRank は API・ツールのような呼ばれない側のパッケージを低く見るので、
  2 段目は大きさで並べる）。
* **入口のモジュール**: ``pyproject.toml`` の ``[project.scripts]`` / ``[project.gui-scripts]`` / ``[tool.poetry.scripts]`` が指すモジュール、
  次に名前が ``main`` / ``__main__`` / ``app`` / ``cli`` か ``*_entry`` のコードのファイル（浅い順）。``ENTRY_MAX`` 件まで。
* コードのファイル: データ・文書（Tier 3）と図（``dot`` / ``mermaid`` / ``openapi``）と ``binary`` / ``secret`` / ``unknown`` を除き、
  Tier 1・2 で本文を解析しなかったもの（圧縮済みの JS など ``lexical_fallback``）も除く。

大きさは ``OVERVIEW_MAX_TOKENS`` までで ``max_tokens`` に依らない（既定の予算 1500 の 1/4）。``max_tokens`` に比例させると、予算を
少し上げたときに概観が 1 項目ぶん伸びて後ろのファイルブロックが 1 つ落ちる帯ができ、予算に対する単調性（仕様書 §5.6 規則 5。
BUG-0067）を破る。予算が足りないときは、先頭のレコードの縮退で概観の行を後ろから外す（``t01_get_repo_map._make_record``）。
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from lucianke.core.model import Confidence, Tier
from lucianke.index import rank

OVERVIEW_MAX_TOKENS = 375
"""概観（トップレベルの行・パッケージ・入口・サブパッケージ）の上限。既定の予算 1500 の 1/4。``max_tokens`` に依らない。"""
CORE_LINES = 2
"""縮退で最後まで残す先頭の行の数（トップレベルの行とパッケージの行）。"""
ROOT_MAX = 10
ENTRY_MAX = 8
PYPROJECT_MAX = 20
PYPROJECT_MAX_BYTES = 256 * 1024
_NON_CODE_LANGUAGES = frozenset({"dot", "mermaid", "openapi", "binary", "secret", "unknown"})
_ENTRY_STEMS = frozenset({"main", "__main__", "app", "cli"})
_SCRIPT_TABLES = (("project", "scripts"), ("project", "gui-scripts"), ("tool", "poetry", "scripts"))


def is_code(f: rank.RepoMapFile) -> bool:
    if f.tier == Tier.T3 or f.language in _NON_CODE_LANGUAGES:
        return False
    return not (f.tier in (Tier.T1, Tier.T2) and f.confidence == Confidence.LEXICAL_FALLBACK.value)


@dataclass(slots=True)
class _Dir:
    path: str  # "a/b/"（ルートは ""）
    files: int = 0  # 配下（再帰）のコードのファイル数
    lines: int = 0
    score: float = 0.0
    direct: int = 0  # 直下のコードのファイル数
    children: dict[str, _Dir] = field(default_factory=dict)


def _tree(code: Sequence[rank.RepoMapFile]) -> _Dir:
    root = _Dir("")
    for f in code:
        parts = f.path.split("/")[:-1]
        node = root
        node.files += 1
        node.lines += f.line_count
        node.score += f.score
        prefix = ""
        for part in parts:
            prefix += part + "/"
            node = node.children.setdefault(part, _Dir(prefix))
            node.files += 1
            node.lines += f.line_count
            node.score += f.score
        node.direct += 1
    return root


def _collapse(node: _Dir) -> _Dir:
    """直下にコードのファイルが無く、コードを持つ子が 1 つだけなら子とつなぐ（``src/`` → ``src/mnemo/``）。"""
    while node.direct == 0 and len(node.children) == 1:
        node = next(iter(node.children.values()))
    return node


def _importance(d: _Dir) -> tuple[float, int, str]:
    return (-d.score, -d.files, d.path)


@dataclass(frozen=True, slots=True)
class Package:
    path: str
    files: int
    lines: int


@dataclass(slots=True)
class Overview:
    """選んだ概観。``lines`` は compact の注記行の本文（``- `` を除く）、``data`` は json の ``data`` に足すもの。"""

    lines: tuple[str, ...]
    roots: list[Package]
    children: dict[str, list[Package]]
    entries: list[str]

    def data(self) -> dict[str, Any]:
        return {
            "packages": [
                {
                    "dir": r.path,
                    "file_count": r.files,
                    "line_count": r.lines,
                    "children": [{"dir": c.path, "file_count": c.files} for c in self.children.get(r.path, [])],
                }
                for r in self.roots
            ],
            "entry_modules": list(self.entries),
        }


def _pyproject_entries(root: Path, paths: Iterable[str], code_paths: set[str]) -> list[str]:
    """``pyproject.toml`` のスクリプトが指すモジュールのファイル（索引にあるコードのファイルだけ）。浅い ``pyproject.toml`` から。"""
    found: list[str] = []
    projects = sorted((p for p in paths if PurePosixPath(p).name == "pyproject.toml"), key=lambda p: (p.count("/"), p))
    for rel in projects[:PYPROJECT_MAX]:
        try:
            raw = (root / rel).read_bytes()
            if len(raw) > PYPROJECT_MAX_BYTES:
                continue
            doc = tomllib.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        base = rel[: -len("pyproject.toml")]
        for keys in _SCRIPT_TABLES:
            table: Any = doc
            for k in keys:
                table = table.get(k) if isinstance(table, dict) else None
            if not isinstance(table, dict):
                continue
            for target in table.values():
                if not isinstance(target, str):
                    continue
                module = target.split(":", 1)[0].strip().replace(".", "/")
                if not module:
                    continue
                for cand in (f"{module}.py", f"{module}/__init__.py", f"src/{module}.py", f"src/{module}/__init__.py"):
                    path = base + cand
                    if path in code_paths:
                        if path not in found:
                            found.append(path)
                        break
    return found


def _entry_modules(root: Path, files: Sequence[rank.RepoMapFile], code: Sequence[rank.RepoMapFile]) -> list[str]:
    code_paths = {f.path for f in code}
    out = _pyproject_entries(root, (f.path for f in files), code_paths)
    named = [
        f
        for f in code
        if (stem := PurePosixPath(f.path).stem) in _ENTRY_STEMS or stem.endswith("_entry")
    ]
    named.sort(key=lambda f: (f.path.count("/"), -f.score, f.path))
    for f in named:
        if f.path not in out:
            out.append(f.path)
    return out[:ENTRY_MAX]


def build(
    files: Sequence[rank.RepoMapFile],
    root: Path,
    tree_line: str | None,
    count: Callable[[str], int],
    *,
    max_tokens: int = OVERVIEW_MAX_TOKENS,
) -> Overview:
    """概観を組み立てる。項目は 根 → 入口 → サブパッケージ（重要度順）の順に、``max_tokens`` に収まるだけ足す。"""
    code = [f for f in files if is_code(f)]
    tree = _tree(code)
    tops = sorted(
        (_collapse(c) for name, c in tree.children.items() if not name.startswith(".")), key=_importance
    )
    subs = [
        (t.path, c)
        for t in tops[:ROOT_MAX]
        for c in sorted((_collapse(c) for c in t.children.values()), key=lambda d: (-d.files, d.path))
    ]
    entries = _entry_modules(root, files, code)

    roots: list[_Dir] = []
    chosen_entries: list[str] = []
    children: dict[str, list[_Dir]] = {}

    def render() -> tuple[str, ...]:
        out: list[str] = [tree_line] if tree_line else []
        if roots:
            parts = [f"{r.path} ({r.files} {'file' if r.files == 1 else 'files'})" for r in roots]
            if len(roots) < len(tops):
                parts.append(f"+{len(tops) - len(roots)} more")
            out.append("packages: " + ", ".join(parts))
        if chosen_entries:
            out.append("entry modules: " + ", ".join(chosen_entries))
        for r in roots:
            kids = children.get(r.path)
            if kids:
                out.append(f"{r.path}: " + ", ".join(f"{c.path} ({c.files})" for c in kids))
        return tuple(out)

    def fits() -> bool:
        return count("\n".join("- " + ln for ln in render())) <= max_tokens

    for t in tops[:ROOT_MAX]:
        roots.append(t)
        if not fits():
            roots.pop()
            break
    for e in entries:
        chosen_entries.append(e)
        if not fits():
            chosen_entries.pop()
            break
    shown = {r.path for r in roots}
    for parent, c in subs:
        if parent not in shown:
            continue
        children.setdefault(parent, []).append(c)
        if not fits():
            children[parent].pop()
            if not children[parent]:
                del children[parent]
    return Overview(
        lines=render(),
        roots=[Package(r.path, r.files, r.lines) for r in roots],
        children={p: [Package(c.path, c.files, c.lines) for c in cs] for p, cs in children.items()},
        entries=chosen_entries,
    )
