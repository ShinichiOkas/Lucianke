"""不変条件: ``tests/`` の試験ファイルの名前（basename）は重ならない。

pytest は既定の import の方式（``prepend``・``__init__.py`` なし）で動くので、別のディレクトリに同じ名前の試験ファイルが
2 つあると、全体を集める段階で ``import file mismatch`` の収集エラーになる。並行の直す役が関係する試験だけを流して
push すると、この重なりは全体テストまで見えない（2026-10-31: BUG-0135 の単体試験の名前が ``tests/contract`` と重なり、
``3f8eba2`` 単独の main は全体テストが collection error になった。``23c63a8`` で改名）。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"fixtures", "__pycache__"}  # pyproject.toml の norecursedirs（tests/fixtures）と同じ


def test_test_module_basenames_are_unique() -> None:
    seen: dict[str, list[str]] = defaultdict(list)
    for path in TESTS.rglob("test_*.py"):
        rel = path.relative_to(TESTS)
        if SKIP_DIRS.intersection(rel.parts[:-1]):
            continue
        seen[path.name].append(rel.as_posix())
    dupes = {name: sorted(paths) for name, paths in seen.items() if len(paths) > 1}
    assert not dupes, f"rename one of each pair (pytest cannot collect both): {dupes}"
