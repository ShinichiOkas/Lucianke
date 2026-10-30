"""Python の絶対 import を探す根（``syntax.imports.python_import_roots``・``make_resolver``。BUG-0082）と、
pytest の設定の読み取り（``workspace.pyenv.detect_pytest_imports``）。

ファイルの存在は集合で与える（``exists``）。pytest の設定は ``tmp_path`` に書いて読む。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lucianke.core.model import Range, RelPath
from lucianke.syntax.imports import (
    ImportFact,
    ImportRoot,
    external_name_map,
    imported_name_map,
    make_resolver,
    python_import_roots,
)
from lucianke.workspace.pyenv import PytestImports, detect_pytest_imports

_R = Range.from_line_span_external(1, 1)


def _exists(*files: str):
    known = set(files)
    return lambda rel: str(rel) in known


def _from(module: str, *names: str) -> ImportFact:
    return ImportFact(module, [(n, None) for n in names], False, 0, _R, "from")


def _import(module: str) -> ImportFact:
    return ImportFact(module, [], False, 0, _R, "import")


def resolve(
    from_rel: str,
    fact: ImportFact,
    *files: str,
    config: PytestImports | dict[str, PytestImports] | None = None,
) -> dict[str, tuple[str, str]]:
    """``from_rel`` の ``fact`` が束ねる名前 → ``(定義元, 元の名前)``（解決できたものだけ）。"""
    if isinstance(config, PytestImports):
        configs = {"": config}
    else:
        configs = config or {}
    resolver = make_resolver(
        from_rel=RelPath(from_rel),
        language_id="python",
        exists=_exists(*files),
        pytest_imports=lambda project: configs.get(project, PytestImports()),
    )
    return {k: (str(p), n) for k, (p, n) in imported_name_map([fact], resolver).items()}


# --- pytest の prepend: テストのディレクトリ -------------------------------------------------------


def test_sibling_test_module_import_resolves_in_test_dir() -> None:
    """票の再現: ``tests/contract/test_b.py`` の ``from test_a import Env`` は兄弟の ``tests/contract/test_a.py``。"""
    got = resolve("tests/contract/test_b.py", _from("test_a", "Env", "build_env"), "tests/contract/test_a.py")
    assert got == {"Env": ("tests/contract/test_a.py", "Env"), "build_env": ("tests/contract/test_a.py", "build_env")}


def test_helper_module_under_tests_also_sees_its_dir() -> None:
    """テストの土台（``_t11_harness.py``。名前は test_ でないが ``tests/`` の下）からの兄弟 import も同じ。"""
    got = resolve("tests/contract/_harness.py", _from("_shared", "Rig"), "tests/contract/_shared.py")
    assert got == {"Rig": ("tests/contract/_shared.py", "Rig")}


def test_test_dir_comes_before_root_and_src() -> None:
    """同名のモジュールがルートにもあれば、テストのディレクトリが先（pytest は先頭に足す）。テストでないファイルは今までどおりルート。"""
    files = ("tests/unit/util.py", "util.py", "src/util.py")
    assert resolve("tests/unit/test_a.py", _import("util"), *files) == {"util": ("tests/unit/util.py", "*")}
    assert resolve("app/main.py", _import("util"), *files) == {"util": ("util.py", "*")}


def test_non_test_file_does_not_import_its_siblings() -> None:
    """テストでないファイルのディレクトリは根にならない（Python 3 に暗黙の相対 import は無い）。"""
    assert resolve("pkg/a.py", _from("b", "x"), "pkg/b.py") == {}


def test_with_init_py_the_package_root_parent_is_the_root() -> None:
    """``tests/unit/__init__.py`` があり ``tests/__init__.py`` が無ければ、pytest が足すのは ``tests``。兄弟の素の名前は届かず、
    ``unit.helpers`` で届く。"""
    files = ("tests/unit/__init__.py", "tests/unit/helpers.py")
    assert resolve("tests/unit/test_a.py", _from("helpers", "X"), *files) == {}
    assert resolve("tests/unit/test_a.py", _from("unit.helpers", "X"), *files) == {
        "X": ("tests/unit/helpers.py", "X")
    }


def test_with_init_py_up_to_root_the_root_is_used() -> None:
    """``tests/__init__.py`` と ``tests/unit/__init__.py`` があれば、pytest が足すのはルート（``tests.unit.helpers``）。"""
    files = ("tests/__init__.py", "tests/unit/__init__.py", "tests/unit/helpers.py")
    roots = python_import_roots("tests/unit/test_a.py", exists=_exists(*files))
    assert roots[0] == ImportRoot("", stdlib=True)
    assert resolve("tests/unit/test_a.py", _from("helpers", "X"), *files) == {}
    assert resolve("tests/unit/test_a.py", _from("tests.unit.helpers", "X"), *files) == {
        "X": ("tests/unit/helpers.py", "X")
    }


def test_non_identifier_dir_is_not_a_package() -> None:
    """名前が識別子でないディレクトリは ``__init__.py`` があってもパッケージとして上に辿らない（pytest と同じ）。"""
    files = ("tests/my-unit/__init__.py", "tests/my-unit/helpers.py")
    assert resolve("tests/my-unit/test_a.py", _from("helpers", "X"), *files) == {
        "X": ("tests/my-unit/helpers.py", "X")
    }


def test_conftest_dirs_are_roots_closest_first() -> None:
    """祖先の ``conftest.py`` のディレクトリも pytest が足す（近いものから）。無ければ届かない。"""
    files = ("tests/conftest.py", "tests/helpers.py")
    assert resolve("tests/unit/test_a.py", _from("helpers", "X"), *files) == {"X": ("tests/helpers.py", "X")}
    assert resolve("tests/unit/test_a.py", _from("helpers", "X"), "tests/helpers.py") == {}
    roots = python_import_roots("tests/unit/test_a.py", exists=_exists("tests/conftest.py", "conftest.py"))
    assert [r.path for r in roots] == ["tests/unit", "tests", "", "src"]


def test_importlib_mode_adds_no_test_dir() -> None:
    """``--import-mode=importlib`` では pytest は ``sys.path`` に触れない（兄弟の import は実行時も通らない）。"""
    config = PytestImports(import_mode="importlib")
    got = resolve("tests/contract/test_b.py", _from("test_a", "Env"), "tests/contract/test_a.py", config=config)
    assert got == {}


# --- 標準ライブラリ・外部の名前 -------------------------------------------------------------------


def test_stdlib_name_next_to_test_stays_external() -> None:
    """``tests/unit/json.py`` があっても ``import json`` は標準ライブラリ（外部）。pytest が根を足す時点で ``json`` は読み込み済み。"""
    fact = _import("json")
    files = ("tests/unit/json.py",)
    resolver = make_resolver(from_rel=RelPath("tests/unit/test_a.py"), language_id="python", exists=_exists(*files))
    names = imported_name_map([fact], resolver)
    assert names == {}
    assert external_name_map([fact], language_id="python", internal=names) == {"json": "lk://_ext/json"}
    assert resolve("tests/unit/test_a.py", _from("json", "loads"), *files) == {}


def test_pytest_itself_is_never_a_local_module() -> None:
    assert resolve("tests/unit/test_a.py", _import("pytest"), "tests/unit/pytest.py") == {}


def test_third_party_name_is_shadowed_by_the_test_dir() -> None:
    """標準ライブラリでない名前は、テストのディレクトリが site-packages より先（prepend）なので手元のファイル。"""
    got = resolve("tests/unit/test_a.py", _from("requests", "get"), "tests/unit/requests.py")
    assert got == {"get": ("tests/unit/requests.py", "get")}


def test_root_level_stdlib_named_module_keeps_previous_behaviour() -> None:
    """ルートの根（``""``）は今までどおり（ルート直下のテストファイルでも、テストのディレクトリがルートと同じなら同じ扱い）。"""
    assert resolve("test_top.py", _import("json"), "json.py") == {"json": ("json.py", "*")}


# --- pytest の pythonpath -----------------------------------------------------------------------


def test_pythonpath_config_is_a_root() -> None:
    config = PytestImports(pythonpath=("lib",))
    assert resolve("tests/test_a.py", _from("mylib", "f"), "lib/mylib.py", config=config) == {
        "f": ("lib/mylib.py", "f")
    }
    assert resolve("tests/test_a.py", _from("mylib", "f"), "lib/mylib.py") == {}


def test_pythonpath_comes_after_test_dir_and_before_root() -> None:
    config = PytestImports(pythonpath=("lib",))
    roots = python_import_roots("tests/unit/test_a.py", exists=_exists(), pytest_imports=lambda _p: config)
    assert roots == (
        ImportRoot("tests/unit", stdlib=False),
        ImportRoot("lib", stdlib=False),
        ImportRoot("", stdlib=True),
        ImportRoot("src", stdlib=True),
    )


def test_pythonpath_never_resolves_stdlib_names() -> None:
    config = PytestImports(pythonpath=("lib",))
    assert resolve("tests/test_a.py", _import("json"), "lib/json.py", config=config) == {}


# --- テストフィクスチャのルート ---------------------------------------------------------------------


def test_fixture_internal_absolute_import_resolves_inside_the_fixture() -> None:
    """票の再現: ``tests/fixtures/untyped_calls/app/service.py`` の ``from app.engine import Packer`` はフィクスチャの中。"""
    files = ("tests/fixtures/untyped_calls/app/__init__.py", "tests/fixtures/untyped_calls/app/engine.py")
    got = resolve("tests/fixtures/untyped_calls/app/service.py", _from("app.engine", "Packer"), *files)
    assert got == {"Packer": ("tests/fixtures/untyped_calls/app/engine.py", "Packer")}


def test_fixture_root_comes_before_the_workspace_root() -> None:
    """ワークスペースのルートにも ``app/engine.py`` があっても、フィクスチャの中のファイルからはフィクスチャが先。"""
    files = ("tests/fixtures/f/app/engine.py", "app/engine.py")
    assert resolve("tests/fixtures/f/app/service.py", _from("app.engine", "Packer"), *files) == {
        "Packer": ("tests/fixtures/f/app/engine.py", "Packer")
    }
    assert resolve("tests/test_x.py", _from("app.engine", "Packer"), *files) == {
        "Packer": ("app/engine.py", "Packer")
    }


def test_fixture_falls_back_to_the_workspace_roots() -> None:
    files = ("src/lucianke/core.py",)
    assert resolve("tests/fixtures/f/app/service.py", _from("lucianke.core", "X"), *files) == {
        "X": ("src/lucianke/core.py", "X")
    }


def test_fixture_src_layout_and_nested_fixtures() -> None:
    """フィクスチャの ``src``、入れ子のフィクスチャ（内側が先）、フィクスチャの中のテストの兄弟 import。"""
    files = (
        "tests/fixtures/poly/src/shop/models/receipt.py",
        "tests/fixtures/a/tests/fixtures/b/app/mod.py",
        "tests/fixtures/a/app/mod.py",
        "tests/fixtures/a/tests/test_one.py",
    )
    assert resolve("tests/fixtures/poly/src/shop/services/payment.py", _from("shop.models.receipt", "R"), *files) == {
        "R": ("tests/fixtures/poly/src/shop/models/receipt.py", "R")
    }
    assert resolve("tests/fixtures/a/tests/fixtures/b/app/use.py", _from("app.mod", "M"), *files) == {
        "M": ("tests/fixtures/a/tests/fixtures/b/app/mod.py", "M")
    }
    assert resolve("tests/fixtures/a/tests/test_two.py", _from("test_one", "T"), *files) == {
        "T": ("tests/fixtures/a/tests/test_one.py", "T")
    }


def test_fixture_reads_its_own_pytest_config() -> None:
    files = ("tests/fixtures/f/vendor/lib.py",)
    configs = {"tests/fixtures/f": PytestImports(pythonpath=("tests/fixtures/f/vendor",))}
    assert resolve("tests/fixtures/f/app/x.py", _from("lib", "y"), *files, config=configs) == {
        "y": ("tests/fixtures/f/vendor/lib.py", "y")
    }


def test_explicit_source_roots_keep_the_old_behaviour() -> None:
    resolver = make_resolver(
        from_rel=RelPath("tests/contract/test_b.py"),
        language_id="python",
        exists=_exists("tests/contract/test_a.py"),
        source_roots=[RelPath(""), RelPath("src")],
    )
    assert imported_name_map([_from("test_a", "Env")], resolver) == {}


def test_resolver_asks_exists_once_per_path() -> None:
    asked: list[str] = []

    def exists(rel: RelPath) -> bool:
        asked.append(str(rel))
        return False

    resolver = make_resolver(from_rel=RelPath("tests/unit/test_a.py"), language_id="python", exists=exists)
    imported_name_map([_from("pkg.mod", "a", "b"), _from("pkg.mod", "c")], resolver)
    assert len(asked) == len(set(asked))


# --- pytest の設定の読み取り ------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["lib", "."]\naddopts = "-q --import-mode=append"\n'),
        ("pyproject.toml", '[tool.pytest]\npythonpath = ["lib", "."]\naddopts = ["-q", "--import-mode", "append"]\n'),
        ("pytest.ini", "[pytest]\npythonpath = lib .\naddopts = -q --import-mode=append\n"),
        ("pytest.toml", '[pytest]\npythonpath = ["lib", "."]\naddopts = ["--import-mode=append"]\n'),
        ("tox.ini", "[tox]\nenvlist = py312\n\n[pytest]\npythonpath =\n    lib\n    .\naddopts = --import-mode=append\n"),
        ("setup.cfg", "[metadata]\nname = x\n\n[tool:pytest]\npythonpath = lib .\naddopts = --import-mode append\n"),
    ],
)
def test_detect_pytest_imports_reads_each_config_file(tmp_path: Path, name: str, text: str) -> None:
    _write(tmp_path, name, text)
    assert detect_pytest_imports(tmp_path) == PytestImports(("lib", ""), "append")


def test_detect_pytest_imports_follows_pytest_file_precedence(tmp_path: Path) -> None:
    """1 つのディレクトリでは pytest.ini が pyproject.toml より先。pytest の表の無い pyproject.toml / tox.ini は飛ばす。"""
    _write(tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["from_pyproject"]\n')
    _write(tmp_path, "pytest.ini", "[pytest]\npythonpath = from_ini\n")
    assert detect_pytest_imports(tmp_path).pythonpath == ("from_ini",)
    (tmp_path / "pytest.ini").unlink()
    assert detect_pytest_imports(tmp_path).pythonpath == ("from_pyproject",)
    _write(tmp_path, "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    _write(tmp_path, "tox.ini", "[tox]\nenvlist = py312\n")
    _write(tmp_path, "setup.cfg", "[tool:pytest]\npythonpath = from_cfg\n")
    assert detect_pytest_imports(tmp_path).pythonpath == ("from_cfg",)


def test_detect_pytest_imports_for_a_fixture_project_and_bad_paths(tmp_path: Path) -> None:
    """``pythonpath`` は設定ファイルの置き場からの相対。ワークスペースの外・絶対パスは捨てる。壊れた設定は既定値。"""
    _write(tmp_path, "tests/fixtures/f/pytest.ini", "[pytest]\npythonpath = src ../shared ../../../.. /abs C:/x\n")
    assert detect_pytest_imports(tmp_path, "tests/fixtures/f") == PytestImports(
        ("tests/fixtures/f/src", "tests/fixtures/shared")
    )
    _write(tmp_path, "pyproject.toml", "[tool.pytest.ini_options\nbroken")
    assert detect_pytest_imports(tmp_path) == PytestImports()
    assert detect_pytest_imports(tmp_path / "missing") == PytestImports()


def test_fingerprint_changes_only_with_the_import_settings() -> None:
    assert PytestImports().fingerprint() == PytestImports().fingerprint()
    assert PytestImports(("lib",)).fingerprint() != PytestImports().fingerprint()
    assert PytestImports(import_mode="importlib").fingerprint() != PytestImports().fingerprint()
