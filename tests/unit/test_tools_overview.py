"""T-01 の先頭の概観（``tools._overview``。仕様書 T-01 処理規約 3。BUG-0120）。

索引を使わず ``RepoMapFile`` の並びと ``pyproject.toml`` だけを渡して、パッケージの根・2 段目のサブパッケージ・入口のモジュールの
選び方と、上限（``OVERVIEW_MAX_TOKENS``）を確かめる。
"""

from __future__ import annotations

from pathlib import Path

from lucianke.core.tokens import TokenEstimator
from lucianke.index.rank import RepoMapFile
from lucianke.tools import _overview

EST = TokenEstimator()


def f(path: str, *, language: str = "python", tier: int = 1, score: float = 0.0, conf: str = "structural") -> RepoMapFile:
    return RepoMapFile(path, language, tier, 10, conf, False, score, 0, 0)


FILES = [
    f("src/app/__init__.py", score=0.1),
    f("src/app/main.py", score=0.2),
    f("src/app/core/engine.py", score=3.0),
    f("src/app/core/util.py", score=1.0),
    f("src/app/core/deep/x.py"),
    f("src/app/api/routes.py", score=0.1),
    f("src/app/api/v1/handlers.py"),
    f("src/app/cli/commands.py"),
    f("tools/lint/runner.py"),
    f("tools/lint/rules.py"),
    f("tools/server_entry.py"),
    f("web/src/index.ts", language="typescript"),
    f("web/src/app.ts", language="typescript"),
    f("web/package.json", language="json", tier=3),
    f("docs/guide.md", language="markdown", tier=3),
    f("docs/flow.mmd", language="mermaid", tier=2),
    f("static/plotly.min.js", language="javascript", conf="lexical_fallback"),
    f(".claude/hooks/hook.py"),
    f("pyproject.toml", language="toml", tier=3),
    f("run_entry.py"),
]

PYPROJECT = b"""[project]
name = "app"
[project.scripts]
app = "app.cli.commands:main"
missing = "nowhere.mod:main"
"""


def build(tmp_path: Path, files=FILES, **kw) -> _overview.Overview:
    (tmp_path / "pyproject.toml").write_bytes(PYPROJECT)
    return _overview.build(files, tmp_path, "src/ (8 files), tools/ (3 files)", EST.count, **kw)


def test_roots_collapse_single_child_dirs_and_skip_non_code_and_hidden_dirs(tmp_path: Path) -> None:
    ov = build(tmp_path)
    roots = [r.path for r in ov.roots]
    assert roots[0] == "src/app/"  # src/ には app/ しか無いのでつなぐ。重要度（スコアの和）が先
    assert set(roots) == {"src/app/", "tools/", "web/src/"}  # docs/（文書・図）・static/（圧縮済み）・.claude/（隠し）は根にしない
    by = {r.path: r.files for r in ov.roots}
    assert by == {"src/app/": 8, "tools/": 3, "web/src/": 2}


def test_second_level_packages_are_listed_with_full_paths_by_size(tmp_path: Path) -> None:
    ov = build(tmp_path)
    kids = [c.path for c in ov.children["src/app/"]]
    assert kids == ["src/app/core/", "src/app/api/", "src/app/cli/"]  # 大きさの順。3 段目（core/deep/）は数に入るが行にしない
    assert [c.files for c in ov.children["src/app/"]] == [3, 2, 1]
    assert [c.path for c in ov.children["tools/"]] == ["tools/lint/"]
    line = next(ln for ln in ov.lines if ln.startswith("src/app/: "))
    assert line == "src/app/: src/app/core/ (3), src/app/api/ (2), src/app/cli/ (1)"


def test_entry_modules_scripts_first_then_named_files_by_depth(tmp_path: Path) -> None:
    ov = build(tmp_path)
    assert ov.entries == [
        "src/app/cli/commands.py",  # pyproject の [project.scripts]（src/ 配置。指す先の無い missing は出さない）
        "run_entry.py",  # 名前が *_entry / main / __main__ / app / cli のファイル（浅い順、同じ深さはスコアの順）
        "tools/server_entry.py",
        "src/app/main.py",
        "web/src/app.ts",
    ]
    assert ".claude/hooks/hook.py" not in ov.entries and "src/app/__init__.py" not in ov.entries
    assert any(ln.startswith("entry modules: src/app/cli/commands.py, run_entry.py") for ln in ov.lines), ov.lines


def test_lines_order_and_json(tmp_path: Path) -> None:
    ov = build(tmp_path)
    assert ov.lines[0] == "src/ (8 files), tools/ (3 files)"
    assert ov.lines[1].startswith("packages: src/app/ (8 files), ")
    assert ov.lines[2].startswith("entry modules: ")
    assert ov.lines[3].startswith("src/app/: ")
    data = ov.data()
    assert data["packages"][0]["dir"] == "src/app/" and data["packages"][0]["file_count"] == 8
    assert data["packages"][0]["children"][0] == {"dir": "src/app/core/", "file_count": 3}
    assert data["entry_modules"] == ov.entries


def test_cap_keeps_roots_then_entries_then_children(tmp_path: Path) -> None:
    full = build(tmp_path)
    for cap in (20, 40, 60, 80, 120, _overview.OVERVIEW_MAX_TOKENS):
        ov = build(tmp_path, max_tokens=cap)
        text = "\n".join("- " + ln for ln in ov.lines)
        if len(ov.lines) > 1:  # トップレベルの行だけは今までどおり常に出す
            assert EST.count(text) <= cap, (cap, text)
        assert [r.path for r in ov.roots] == [r.path for r in full.roots][: len(ov.roots)]
        if ov.entries:
            assert len(ov.roots) == len(full.roots)  # 入口は根を全部入れてから
        if ov.children:
            assert ov.entries == full.entries  # サブパッケージは入口を全部入れてから


def test_many_roots_are_capped_with_a_more_marker(tmp_path: Path) -> None:
    files = [f(f"pkg{i:02d}/m{j}.py") for i in range(_overview.ROOT_MAX + 3) for j in range(2)]
    ov = _overview.build(files, tmp_path, None, EST.count)
    assert len(ov.roots) == _overview.ROOT_MAX
    assert ov.lines[0].endswith(", +3 more")


def test_no_code_no_packages(tmp_path: Path) -> None:
    ov = _overview.build([f("docs/a.md", language="markdown", tier=3)], tmp_path, "docs/ (1 file)", EST.count)
    assert ov.lines == ("docs/ (1 file)",) and ov.roots == [] and ov.entries == []
