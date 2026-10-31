"""BUG-0132: basedpyright の解析の対象（``lsp/pyright_scope``）と、Python の参照元を開かせる候補（``_lsp_exact.prime_set``）。

実機の basedpyright で「対象の外のファイルは開くと探される」ことは ``tests/contract/test_lsp_ancestor_pyright_config_bug0132.py``。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from lucianke.core.model import Confidence, FileInfo, RelPath, Tier
from lucianke.lsp import pyright_scope as ps
from lucianke.tools import _lsp_exact as lx
from lucianke.workspace.types import Workspace


@pytest.fixture(autouse=True)
def _fresh_memo():
    ps._MEMO.clear()
    yield
    ps._MEMO.clear()


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def inside(root: Path, *rels: str) -> list[bool]:
    scope = ps.pyright_scope(root)
    assert scope is not None
    return [scope.contains(root / r) for r in rels]


# --- 解析の対象 -------------------------------------------------------------------------------------


def test_without_config_the_root_is_in_scope_minus_the_default_excludes(tmp_path: Path) -> None:
    assert inside(
        tmp_path,
        "a.py",
        "src/pkg/b.py",
        "tests/test_b.py",
        ".github/scripts/x.py",
        "node_modules/m/x.py",
        "pkg/__pycache__/x.py",
        "__editable__.shop-0.1/x.py",
    ) == [True, True, True, False, False, False, False]
    assert ps.pyright_scope(tmp_path).config is None  # type: ignore[union-attr]
    assert ps.pyright_scope(tmp_path).narrowed is False  # type: ignore[union-attr]
    assert not ps.pyright_scope(tmp_path).contains(tmp_path.parent / "elsewhere.py")  # type: ignore[union-attr]


def test_a_pyproject_without_a_pyright_section_is_no_config(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n')
    assert inside(tmp_path, "src/a.py", "tests/t.py") == [True, True]


def test_pyrightconfig_include_narrows_the_scope(tmp_path: Path) -> None:
    write(tmp_path, "pyrightconfig.json", json.dumps({"include": ["src"]}))
    assert inside(tmp_path, "src/shop/a.py", "tests/test_a.py", "scripts/run.py", "srcx/a.py") == [
        True,
        False,
        False,
        False,  # 接頭辞の一致ではなく階層の一致
    ]
    assert ps.pyright_scope(tmp_path).config == str(tmp_path / "pyrightconfig.json")  # type: ignore[union-attr]
    assert ps.pyright_scope(tmp_path).narrowed is True  # type: ignore[union-attr]


def test_a_config_without_include_or_exclude_is_not_narrowed(tmp_path: Path) -> None:
    write(tmp_path, "pyrightconfig.json", json.dumps({"typeCheckingMode": "strict"}))
    scope = ps.pyright_scope(tmp_path)
    assert scope is not None and scope.narrowed is False and scope.config is not None
    ps._MEMO.clear()
    write(tmp_path, "pyrightconfig.json", json.dumps({"exclude": ["build"]}))
    assert ps.pyright_scope(tmp_path).narrowed is True  # type: ignore[union-attr]


def test_user_excludes_are_added_to_the_defaults(tmp_path: Path) -> None:
    write(tmp_path, "pyrightconfig.json", json.dumps({"include": ["src", "tests"], "exclude": ["tests/fixtures"]}))
    assert inside(tmp_path, "tests/test_a.py", "tests/fixtures/poly/x.py", "src/.hidden/x.py") == [
        True,
        False,
        False,  # 既定の **/.* は利用者の exclude があっても足される（basedpyright 1.40.2）
    ]


def test_wildcards_follow_pyright(tmp_path: Path) -> None:
    write(
        tmp_path,
        "pyrightconfig.json",
        json.dumps({"include": ["src/**/api", "tools/*.py"], "exclude": ["**/gen_?.py"]}),
    )
    assert inside(
        tmp_path,
        "src/api/r.py",  # ** は 0 階層も
        "src/shop/v1/api/r.py",
        "src/shop/x.py",
        "tools/run.py",
        "tools/sub/run.py",  # * は階層を越えない
        "src/api/gen_1.py",
        "src/api/gen_10.py",  # ? は 1 文字
    ) == [True, True, False, True, False, False, True]


def test_jsonc_comments_and_trailing_commas_are_read(tmp_path: Path) -> None:
    write(
        tmp_path,
        "pyrightconfig.json",
        '{\n  // only the package\n  "include": ["src", /* and */ "lib",],\n  "exclude": ["src/legacy//x"],\n}\n',
    )
    assert inside(tmp_path, "src/a.py", "lib/b.py", "tests/t.py") == [True, True, False]


@pytest.mark.parametrize("section", ["basedpyright", "pyright"])
def test_a_pyproject_section_narrows_the_scope(tmp_path: Path, section: str) -> None:
    write(tmp_path, "pyproject.toml", f'[project]\nname = "x"\n\n[tool.{section}]\ninclude = ["src"]\n')
    assert inside(tmp_path, "src/a.py", "tests/t.py") == [True, False]


def test_a_root_pyrightconfig_wins_over_the_pyproject_section(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", '[tool.basedpyright]\ninclude = ["src"]\n')
    write(tmp_path, "pyrightconfig.json", json.dumps({"include": ["tests"]}))
    assert inside(tmp_path, "src/a.py", "tests/t.py") == [False, True]


def test_extends_paths_are_relative_to_the_config_that_writes_them(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write(tmp_path, "base.json", json.dumps({"include": ["proj/pkg"], "exclude": ["proj/pkg/old"]}))
    write(root, "pyrightconfig.json", json.dumps({"extends": "../base.json", "typeCheckingMode": "strict"}))
    assert inside(root, "pkg/a.py", "pkg/old/b.py", "tests/t.py") == [True, False, False]
    # 派生した側が書けば、その値が勝つ（パスは派生した側から）
    write(root, "pyrightconfig.json", json.dumps({"extends": "../base.json", "include": ["tests"]}))
    assert inside(root, "pkg/a.py", "tests/t.py") == [False, True]


def test_a_missing_or_circular_extends_keeps_what_was_read(tmp_path: Path) -> None:
    write(tmp_path, "pyrightconfig.json", json.dumps({"extends": "./nope.json", "include": ["src"]}))
    assert inside(tmp_path, "src/a.py", "tests/t.py") == [True, False]
    ps._MEMO.clear()
    write(tmp_path, "pyrightconfig.json", json.dumps({"extends": "./pyrightconfig.json", "include": ["src"]}))
    assert inside(tmp_path, "src/a.py", "tests/t.py") == [True, False]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pyrightconfig.json", "{ not json"),
        ("pyrightconfig.json", json.dumps({"include": "src"})),
        ("pyrightconfig.json", json.dumps(["src"])),
        ("pyproject.toml", "[tool.pyright\n"),
        ("pyproject.toml", '[tool.pyright]\ninclude = ["a"]\n[tool.basedpyright]\ninclude = ["b"]\n'),
    ],
)
def test_an_unreadable_config_is_not_judged(tmp_path: Path, name: str, text: str) -> None:
    write(tmp_path, name, text)
    assert ps.pyright_scope(tmp_path) is None


def test_the_answer_follows_edits_of_the_root_config(tmp_path: Path) -> None:
    assert inside(tmp_path, "tests/t.py") == [True]
    write(tmp_path, "pyrightconfig.json", json.dumps({"include": ["src"]}))
    assert inside(tmp_path, "tests/t.py") == [False]
    write(tmp_path, "pyrightconfig.json", json.dumps({"include": ["src", "tests"]}))
    assert inside(tmp_path, "tests/t.py") == [True]
    (tmp_path / "pyrightconfig.json").unlink()
    assert inside(tmp_path, "tests/t.py") == [True]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only (case-insensitive paths)")
def test_windows_paths_match_without_case(tmp_path: Path) -> None:
    write(tmp_path, "pyrightconfig.json", json.dumps({"include": ["Src"]}))
    scope = ps.pyright_scope(tmp_path)
    assert scope is not None
    assert scope.contains(str(tmp_path / "src" / "a.py").upper())
    assert scope.contains(os.fspath(tmp_path / "SRC" / "a.py").replace("\\", "/"))


# --- Python の参照元を開かせる候補 -------------------------------------------------------------------


def info(path: str, *, language: str = "python", tier: Tier = Tier.T1, test: bool = False) -> FileInfo:
    return FileInfo(
        file_id=1,
        workspace_id="w",
        path=RelPath(path),
        language=language,
        tier=tier,
        sha256="",
        line_count=1,
        byte_size=1,
        is_test=test,
        confidence=Confidence.STRUCTURAL,
    )


@pytest.fixture
def pyws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    files = {
        "src/shop/pay.py": "def charge():\n    pass\n",
        "src/shop/api.py": "from shop.pay import charge\ncharge()\n",
        "tests/test_pay.py": "from shop.pay import charge\ncharge()\n",
        "tests/test_other.py": "def other():\n    pass\n",
        "scripts/run.py": "charge()\n",
        "web/app.ts": "charge()\n",
    }
    for rel, text in files.items():
        write(tmp_path, rel, text)
    listing = [
        info("src/shop/pay.py"),
        info("src/shop/api.py"),
        info("tests/test_pay.py", test=True),
        info("tests/test_other.py", test=True),
        info("scripts/run.py", tier=Tier.T2),
        info("web/app.ts", language="typescript"),
    ]
    monkeypatch.setattr(lx.queries, "files_in_scope", lambda conn, ws_id, globs=(), **kw: list(listing))
    return Workspace(workspace_id="w", root=tmp_path.resolve())


def candidates(ws: Workspace, **kw) -> list[str]:
    """``prime_set`` の候補。解析の対象は LSP の起動のときに覚えたもの（``LspHandle.analysis_scope``）の代わりにここで引く。"""
    kw.setdefault("scope", ps.pyright_scope(ws.root))
    got = lx.prime_set(None, ws, "python", name="charge", exclude="src/shop/pay.py", is_open=lambda rel: False, **kw)  # type: ignore[arg-type]
    return [str(p) for p, _ in got.files]


def test_python_primes_only_files_outside_the_lsp_scope_that_mention_the_name(pyws: Workspace) -> None:
    write(pyws.root, "pyrightconfig.json", json.dumps({"include": ["src"]}))
    assert candidates(pyws) == ["tests/test_pay.py", "scripts/run.py"]  # テストが先。src は LSP が探す。TS は混ぜない
    got = lx.prime_set(
        None,  # type: ignore[arg-type]
        pyws,
        "python",
        name="charge",
        exclude="src/shop/pay.py",
        is_open=lambda rel: False,
        scope=ps.pyright_scope(pyws.root),
    )
    assert {lid for _, lid in got.files} == {"python"}


def test_python_follows_the_scope_the_server_loaded_not_later_edits(pyws: Workspace) -> None:
    """候補は渡された（サーバーが読んだ）対象で決め、ディスクの設定を読み直さない（サーバーに届いていない編集に先回りしない。
    届けたときは ``LspPool.notify_watched`` がハンドルの対象を引き直す。BUG-0135）。"""
    write(pyws.root, "pyrightconfig.json", json.dumps({"include": ["src"]}))
    loaded = ps.pyright_scope(pyws.root)
    write(pyws.root, "pyrightconfig.json", json.dumps({"include": ["src", "tests", "scripts"]}))
    assert candidates(pyws, scope=loaded) == ["tests/test_pay.py", "scripts/run.py"]
    assert candidates(pyws, scope=None) == []  # 対象が分からなければ開かせない


def test_python_primes_nothing_when_the_whole_workspace_is_in_scope(pyws: Workspace) -> None:
    assert candidates(pyws) == []


def test_python_primes_nothing_and_lists_nothing_without_user_include_or_exclude(
    pyws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定の除外だけ（``.github/`` などのドットのディレクトリ）なら開かせず、ファイルの一覧も引かない（速さ）。"""
    write(pyws.root, ".github/run.py", "charge()\n")
    write(pyws.root, "pyrightconfig.json", json.dumps({"typeCheckingMode": "strict"}))

    def boom(*a, **kw):
        raise AssertionError("files_in_scope must not be called")

    monkeypatch.setattr(lx.queries, "files_in_scope", boom)
    assert candidates(pyws) == []


def test_python_primes_nothing_when_the_config_is_unreadable(pyws: Workspace) -> None:
    write(pyws.root, "pyrightconfig.json", "{ broken")
    assert candidates(pyws) == []


def test_python_priming_skips_open_files_and_caps(pyws: Workspace) -> None:
    write(pyws.root, "pyrightconfig.json", json.dumps({"include": ["src"]}))
    got = lx.prime_set(
        None,  # type: ignore[arg-type]
        pyws,
        "python",
        name="charge",
        exclude="src/shop/pay.py",
        is_open=lambda rel: str(rel) == "tests/test_pay.py",
        scope=ps.pyright_scope(pyws.root),
    )
    assert [str(p) for p, _ in got.files] == ["scripts/run.py"]
    assert candidates(pyws, limit=1) == ["tests/test_pay.py"]


def test_other_languages_are_not_primed(pyws: Workspace) -> None:
    got = lx.prime_set(None, pyws, "rust", name="charge", exclude="x.rs", is_open=lambda rel: False)  # type: ignore[arg-type]
    assert got.files == () and got.skipped == 0
