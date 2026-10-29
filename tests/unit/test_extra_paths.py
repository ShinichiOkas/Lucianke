"""src レイアウトの import の根（``PythonEnv.extra_paths``）の検出と、LSP の設定への載せ方（20d）。

検出は決定的で、``pyproject.toml`` の宣言（setuptools・hatch・poetry・pdm・uv_build）と ``src/<pkg>/__init__.py`` の形だけを見る。
利用者の ``pyrightconfig.json`` / ``[tool.pyright]`` / ``[tool.basedpyright]`` が ``extraPaths`` か ``executionEnvironments`` を
持つときは何も足さない（利用者の設定が先）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest

from lucianke.lsp.servers import SERVERS, build_settings
from lucianke.workspace.pyenv import detect_extra_paths, detect_python_env

REPO = Path(__file__).resolve().parents[2]
POLY = REPO / "tests" / "fixtures" / "poly"


def make(root: Path, *files: str, pyproject: str | None = None, pyrightconfig: str | None = None) -> Path:
    for rel in files:
        path = root / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    if pyproject is not None:
        (root / "pyproject.toml").write_text(dedent(pyproject), encoding="utf-8")
    if pyrightconfig is not None:
        (root / "pyrightconfig.json").write_text(dedent(pyrightconfig), encoding="utf-8")
    return root


# --- 宣言: pyproject.toml -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pyproject", "dirs", "want"),
    [
        pytest.param(
            '[tool.setuptools]\npackage-dir = {"" = "src"}\n', ["src/"], ("src",), id="setuptools-package-dir"
        ),
        pytest.param(
            '[tool.setuptools]\npackage-dir = {"" = "lib"}\n', ["lib/"], ("lib",), id="setuptools-other-dir"
        ),
        pytest.param(
            '[tool.setuptools.package-dir]\nshop = "python/shop"\n',
            ["python/"],
            ("python",),
            id="setuptools-named",
        ),
        pytest.param(
            '[tool.setuptools.package-dir]\n"shop.api" = "py/shop/api"\n',
            ["py/"],
            ("py",),
            id="setuptools-dotted",
        ),
        pytest.param(
            '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
            ["src/"],
            ("src",),
            id="setuptools-find-where",
        ),
        pytest.param(
            '[tool.hatch.build.targets.wheel]\npackages = ["src/shop", "lib/other"]\n',
            ["src/", "lib/"],
            ("lib", "src"),
            id="hatch-wheel-packages",
        ),
        pytest.param(
            '[tool.hatch.build]\npackages = ["python/shop"]\n',
            ["python/"],
            ("python",),
            id="hatch-build-packages",
        ),
        pytest.param(
            '[tool.hatch.build.targets.wheel]\nsources = ["src"]\n',
            ["src/"],
            ("src",),
            id="hatch-sources-list",
        ),
        pytest.param(
            '[tool.hatch.build.targets.wheel.sources]\n"src" = ""\n',
            ["src/"],
            ("src",),
            id="hatch-sources-table",
        ),
        pytest.param(
            '[tool.poetry]\npackages = [{ include = "shop", from = "src" }, { include = "x" }]\n',
            ["src/"],
            ("src",),
            id="poetry-from",
        ),
        pytest.param('[tool.pdm.build]\npackage-dir = "src"\n', ["src/"], ("src",), id="pdm"),
        pytest.param(
            '[tool.uv.build-backend]\nmodule-root = "python"\n', ["python/"], ("python",), id="uv-build"
        ),
        pytest.param(
            '[tool.setuptools]\npackage-dir = {"" = "src"}\n', [], (), id="declared-but-missing-dir"
        ),
        pytest.param(
            '[tool.setuptools]\npackage-dir = {"" = "../src"}\n', ["src/"], (), id="outside-root-is-ignored"
        ),
        pytest.param('[tool.setuptools]\npackage-dir = {"" = "."}\n', [], (), id="root-itself-adds-nothing"),
        pytest.param(
            '[tool.hatch.build.targets.wheel]\npackages = ["shop"]\n', ["shop/"], (), id="hatch-root-package"
        ),
        pytest.param("[tool.setuptools\n", ["src/"], (), id="broken-toml-is-ignored"),
    ],
)
def test_declared_package_roots(
    tmp_path: Path, pyproject: str, dirs: list[str], want: tuple[str, ...]
) -> None:
    make(tmp_path, *dirs, pyproject=pyproject)
    assert detect_extra_paths(tmp_path) == want


# --- 形: src/<pkg>/__init__.py ------------------------------------------------------------------------------


def test_src_layout_without_any_declaration(tmp_path: Path) -> None:
    """F-POLY の形: pyproject.toml に build の宣言が無くても ``src/<pkg>/__init__.py`` から ``src`` が決まる。"""
    make(tmp_path, "src/shop/__init__.py", "tests/test_payment.py", pyproject='[project]\nname = "shop"\n')
    assert detect_extra_paths(tmp_path) == ("src",)


def test_src_layout_without_pyproject(tmp_path: Path) -> None:
    make(tmp_path, "src/shop/__init__.py")
    assert detect_extra_paths(tmp_path) == ("src",)


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(["src/shop/__init__.py", "shop/__init__.py"], id="same-named-package-at-root"),
        pytest.param(["src/shop/__init__.py", "shop.py"], id="same-named-module-at-root"),
        pytest.param(["src/__init__.py", "src/shop/__init__.py"], id="src-is-itself-a-package"),
        pytest.param(["src/scripts/run.py"], id="no-package-under-src"),
        pytest.param(["src/shop.py"], id="module-only-under-src"),
        pytest.param(["lib/shop/__init__.py"], id="not-named-src"),
        pytest.param([], id="nothing"),
    ],
)
def test_not_a_src_layout(tmp_path: Path, files: list[str]) -> None:
    make(tmp_path, *files)
    assert detect_extra_paths(tmp_path) == ()


def test_one_colliding_package_does_not_hide_a_clean_one(tmp_path: Path) -> None:
    make(tmp_path, "src/shop/__init__.py", "shop/__init__.py", "src/billing/__init__.py")
    assert detect_extra_paths(tmp_path) == ("src",)


def test_declarations_and_shape_are_unioned_sorted_and_deduplicated(tmp_path: Path) -> None:
    make(
        tmp_path,
        "src/shop/__init__.py",
        "lib/",
        pyproject='[tool.setuptools]\npackage-dir = {"" = "src"}\n[tool.pdm.build]\npackage-dir = "lib"\n',
    )
    assert detect_extra_paths(tmp_path) == ("lib", "src")


# --- 利用者の設定が先 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pyrightconfig", "pyproject"),
    [
        pytest.param('{"extraPaths": ["lib"]}', None, id="pyrightconfig-extraPaths"),
        pytest.param('{"extraPaths": []}', None, id="pyrightconfig-empty-extraPaths"),
        pytest.param(
            '{"executionEnvironments": [{"root": "src"}]}', None, id="pyrightconfig-executionEnvironments"
        ),
        pytest.param(
            '{\n  // JSONC\n  "pythonVersion": "3.12",\n  /* 根 */ "extraPaths": ["src",],\n}\n',
            None,
            id="pyrightconfig-jsonc",
        ),
        pytest.param('{"extraPaths": ["src"] oops', None, id="pyrightconfig-unparsable-but-mentions-it"),
        pytest.param(None, '[tool.pyright]\nextraPaths = ["lib"]\n', id="tool-pyright-extraPaths"),
        pytest.param(None, '[tool.basedpyright]\nextraPaths = ["src"]\n', id="tool-basedpyright-extraPaths"),
        pytest.param(
            None,
            '[[tool.pyright.executionEnvironments]]\nroot = "src"\n',
            id="tool-pyright-executionEnvironments",
        ),
    ],
)
def test_the_users_pyright_settings_win(
    tmp_path: Path, pyrightconfig: str | None, pyproject: str | None
) -> None:
    make(tmp_path, "src/shop/__init__.py", pyproject=pyproject, pyrightconfig=pyrightconfig)
    assert detect_extra_paths(tmp_path) == ()


@pytest.mark.parametrize(
    ("pyrightconfig", "pyproject"),
    [
        pytest.param('{"pythonVersion": "3.12"}', None, id="pyrightconfig-without-the-keys"),
        pytest.param(
            '{"pythonVersion": "3.12" // extraPaths を書き忘れ\n}', None, id="comment-is-not-a-setting"
        ),
        pytest.param('{"ignore": ["extraPaths"]}', None, id="a-value-is-not-a-key"),
        pytest.param(None, '[tool.pyright]\npythonVersion = "3.12"\n', id="tool-pyright-without-the-keys"),
        pytest.param(None, '[tool.mypy]\nextraPaths = ["lib"]\n', id="other-tools-do-not-count"),
    ],
)
def test_unrelated_user_settings_do_not_suppress_detection(
    tmp_path: Path, pyrightconfig: str | None, pyproject: str | None
) -> None:
    make(tmp_path, "src/shop/__init__.py", pyproject=pyproject, pyrightconfig=pyrightconfig)
    assert detect_extra_paths(tmp_path) == ("src",)


def test_this_repository_has_its_own_extra_paths_so_nothing_is_added() -> None:
    """Lucianke 自身: hatch が ``src/lucianke`` を宣言し src レイアウトだが、``[tool.basedpyright] extraPaths`` が先。"""
    assert detect_extra_paths(REPO) == ()


def test_the_poly_fixture_is_a_src_layout_with_no_user_settings() -> None:
    assert detect_extra_paths(POLY) == ("src",)


# --- PythonEnv への載り方 ---------------------------------------------------------------------------------------


def test_every_detection_path_carries_extra_paths(tmp_path: Path) -> None:
    make(tmp_path, "src/shop/__init__.py")
    fake_env: dict[str, str] = {"PATH": ""}
    # インタープリタが見つからない経路
    assert detect_python_env(tmp_path, env=fake_env).interpreter is None
    assert detect_python_env(tmp_path, env=fake_env).extra_paths == ("src",)
    # 明示指定の経路
    explicit = detect_python_env(tmp_path, sys.executable, env=fake_env)
    assert explicit.manager == "explicit" and explicit.extra_paths == ("src",)
    # .venv の経路
    exe = (
        tmp_path
        / ".venv"
        / ("Scripts" if sys.platform == "win32" else "bin")
        / ("python.exe" if sys.platform == "win32" else "python")
    )
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    exe.chmod(0o755)
    venv = detect_python_env(tmp_path, env=fake_env)
    assert venv.manager == "venv" and venv.extra_paths == ("src",)


# --- LSP の設定 -------------------------------------------------------------------------------------------------


def server(name: str):
    return next(d for d in SERVERS if d.name == name and d.language_id == "python")


@pytest.mark.parametrize("name", ["basedpyright", "pyright"])
def test_build_settings_puts_extra_paths_under_python_analysis(name: str, tmp_path: Path) -> None:
    src = tmp_path / "src"
    settings = build_settings(server(name), python_path=sys.executable, extra_paths=[src])
    assert settings["python"]["analysis"]["extraPaths"] == [str(src)]
    assert settings["python"]["analysis"]["typeCheckingMode"] == "standard"  # 既存の設定は残る
    assert settings["python"]["pythonPath"] == sys.executable


def test_basedpyright_gets_extra_paths_under_its_own_section_too(tmp_path: Path) -> None:
    settings = build_settings(server("basedpyright"), extra_paths=[tmp_path / "src"])
    assert settings["basedpyright"]["analysis"]["extraPaths"] == [str(tmp_path / "src")]
    assert "basedpyright" not in build_settings(server("pyright"), extra_paths=[tmp_path / "src"])


def test_build_settings_without_extra_paths_is_unchanged() -> None:
    plain = build_settings(server("basedpyright"), python_path=sys.executable)
    assert (
        "extraPaths" not in plain["python"]["analysis"]
        and "extraPaths" not in plain["basedpyright"]["analysis"]
    )
    assert build_settings(server("basedpyright"), extra_paths=[]) == build_settings(server("basedpyright"))


def test_build_settings_does_not_mutate_the_definition(tmp_path: Path) -> None:
    defn = server("basedpyright")
    build_settings(defn, extra_paths=[tmp_path / "src"])
    assert "extraPaths" not in defn.settings["python"]["analysis"]
    assert "extraPaths" not in defn.settings["basedpyright"]["analysis"]


def test_a_definition_that_already_sets_extra_paths_wins(tmp_path: Path) -> None:
    from dataclasses import replace

    defn = replace(server("pyright"), settings={"python": {"analysis": {"extraPaths": ["/mine"]}}})
    assert build_settings(defn, extra_paths=[tmp_path / "src"])["python"]["analysis"]["extraPaths"] == [
        "/mine"
    ]


def test_only_the_python_type_server_gets_extra_paths(tmp_path: Path) -> None:
    others = [d for d in SERVERS if not (d.language_id == "python" and d.role == "types")]
    assert others  # ruff や TypeScript 系の定義がある
    for defn in others:
        assert "extraPaths" not in str(build_settings(defn, extra_paths=[tmp_path / "src"])), defn.name
