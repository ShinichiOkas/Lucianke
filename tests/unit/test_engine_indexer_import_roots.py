"""索引器が Python の絶対 import を、プロジェクトが実際に使う根で解く（BUG-0082。``syntax.imports.python_import_roots``）。

* テストの兄弟モジュール（pytest の prepend）・テストフィクスチャの中の絶対 import が、外部（``lk://_ext/``）でなく
  ワークスペースのファイルに結ばれる。
* pytest の設定（``pythonpath``・``--import-mode``）を変えたら、違う設定で解いたファイルだけを解き直す
  （``index_file`` / ``remove`` / 差分の ``scan``。``engine/reresolve.py``）。pytest に関係ない鍵の編集では解き直さない。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

WS_ID = "roots"

SHARED = "class Env:\n    pass\n\n\ndef build_env() -> Env:\n    return Env()\n"
USER = (
    "from test_shared import Env, build_env\n"
    "\n"
    "\n"
    "def test_uses_env():\n"
    "    env: Env = build_env()\n"
    "    assert env\n"
)
ENGINE = "class Packer:\n    def pack(self):\n        return 1\n"
SERVICE = "from app.engine import Packer\n\n\ndef known():\n    return Packer().pack()\n"
USES_LIB = "from mylib import helper\n\n\ndef test_helper():\n    helper()\n"
LIB = "def helper():\n    return 1\n"


@dataclass
class Ws:
    indexer: Indexer
    readers: ReaderPool
    root: Path

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def targets(self, source: str, kind: str) -> set[str]:
        """``source``（``path`` または ``path#sym``）が出す ``kind`` の行の目標（ワークスペース内は相対表記）。"""
        return {
            t.removeprefix(f"lk://{WS_ID}/")
            for (t,) in self.rows(
                "SELECT target_urn FROM relations WHERE source_urn = ? AND relation_kind = ?",
                f"lk://{WS_ID}/{source}",
                kind,
            )
        }


@pytest.fixture
async def ws(tmp_path: Path) -> AsyncIterator[Ws]:
    root = tmp_path / WS_ID
    root.mkdir()
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache())
    yield Ws(indexer, readers, root)
    await indexer.aclose()
    readers.close()
    writer.close()


async def test_sibling_test_module_and_fixture_imports_resolve_in_the_workspace(ws: Ws) -> None:
    """票の再現: 兄弟のテストモジュールの ``Env`` / ``build_env`` と、フィクスチャの ``app.engine.Packer`` がワークスペースの中。"""
    ws.write("tests/contract/test_shared.py", SHARED)
    ws.write("tests/contract/test_user.py", USER)
    ws.write("tests/fixtures/proj/app/__init__.py", "")
    ws.write("tests/fixtures/proj/app/engine.py", ENGINE)
    ws.write("tests/fixtures/proj/app/service.py", SERVICE)
    report = await ws.indexer.scan()
    assert not report.errors, report.errors

    shared = "tests/contract/test_shared.py"
    assert ws.targets("tests/contract/test_user.py", "IMPORTS") == {f"{shared}#Env", f"{shared}#build_env"}
    assert ws.targets("tests/contract/test_user.py#test_uses_env", "CALLS") == {f"{shared}#build_env"}
    assert ws.targets("tests/contract/test_user.py#test_uses_env", "TYPE_USES") == {f"{shared}#Env"}
    engine = "tests/fixtures/proj/app/engine.py"
    assert ws.targets("tests/fixtures/proj/app/service.py", "IMPORTS") == {f"{engine}#Packer"}
    assert f"{engine}#Packer.pack" in ws.targets("tests/fixtures/proj/app/service.py#known", "CALLS")
    assert ws.rows(
        "SELECT COUNT(*) FROM symbols WHERE urn LIKE 'lk://_ext/test_shared%' OR urn LIKE 'lk://_ext/app%'"
    ) == [(0,)]


async def test_sibling_created_later_resolves_the_importer(ws: Ws) -> None:
    """兄弟のモジュールを後から作っても、調べたパスの語幹で import 元を解き直す（BUG-0025 の仕組みがテストのディレクトリにも効く）。"""
    ws.write("tests/contract/test_user.py", USER)
    await ws.indexer.index_file("tests/contract/test_user.py")
    assert ws.targets("tests/contract/test_user.py", "IMPORTS") == {
        "lk://_ext/test_shared#Env",
        "lk://_ext/test_shared#build_env",
    }
    ws.write("tests/contract/test_shared.py", SHARED)
    await ws.indexer.index_file("tests/contract/test_shared.py")
    assert ws.targets("tests/contract/test_user.py", "IMPORTS") == {
        "tests/contract/test_shared.py#Env",
        "tests/contract/test_shared.py#build_env",
    }


async def test_adding_init_py_re_resolves_the_sibling_import(ws: Ws) -> None:
    """``tests/contract/__init__.py`` を作ると、pytest が足すのは ``tests`` になり、素の ``test_shared`` は届かない（外部に戻る）。"""
    ws.write("tests/contract/test_shared.py", SHARED)
    ws.write("tests/contract/test_user.py", USER)
    await ws.indexer.scan()
    assert "tests/contract/test_shared.py#Env" in ws.targets("tests/contract/test_user.py", "IMPORTS")
    ws.write("tests/contract/__init__.py", "")
    await ws.indexer.index_file("tests/contract/__init__.py")
    assert ws.targets("tests/contract/test_user.py", "IMPORTS") == {
        "lk://_ext/test_shared#Env",
        "lk://_ext/test_shared#build_env",
    }


async def test_pytest_pythonpath_edit_re_resolves_only_when_the_setting_changes(ws: Ws) -> None:
    """``pyproject.toml`` に ``pythonpath`` を足す → 解き直して ``lib/mylib.py`` に結ぶ。pytest に関係ない鍵の編集では解き直さない。
    ``pythonpath`` を消す → 外部に戻る。"""
    ws.write("lib/mylib.py", LIB)
    ws.write("tests/test_lib.py", USES_LIB)
    ws.write("pyproject.toml", '[project]\nname = "x"\n')
    await ws.indexer.scan()
    assert ws.targets("tests/test_lib.py", "IMPORTS") == {"lk://_ext/mylib#helper"}

    ws.write("pyproject.toml", '[project]\nname = "x"\n\n[tool.pytest.ini_options]\npythonpath = ["lib"]\n')
    before = ws.indexer.reingested_importers
    await ws.indexer.index_file("pyproject.toml")
    assert ws.targets("tests/test_lib.py", "IMPORTS") == {"lib/mylib.py#helper"}
    assert ws.targets("tests/test_lib.py#test_helper", "CALLS") == {"lib/mylib.py#helper"}
    assert ws.indexer.reingested_importers == before + 1  # 解き直したのは import を解いた Python のファイルだけ（lib/mylib.py は import なし）

    ws.write(
        "pyproject.toml",
        '[project]\nname = "x"\nversion = "1"\n\n[tool.ruff]\nline-length = 90\n\n'
        '[tool.pytest.ini_options]\npythonpath = ["lib"]\n',
    )
    before = ws.indexer.reingested_importers
    await ws.indexer.index_file("pyproject.toml")
    assert ws.indexer.reingested_importers == before  # pytest の import の設定は同じ

    ws.write("pyproject.toml", '[project]\nname = "x"\n')
    await ws.indexer.index_file("pyproject.toml")
    assert ws.targets("tests/test_lib.py", "IMPORTS") == {"lk://_ext/mylib#helper"}


async def test_pytest_ini_created_and_removed(ws: Ws) -> None:
    ws.write("lib/mylib.py", LIB)
    ws.write("tests/test_lib.py", USES_LIB)
    await ws.indexer.scan()
    assert ws.targets("tests/test_lib.py", "IMPORTS") == {"lk://_ext/mylib#helper"}

    ws.write("pytest.ini", "[pytest]\npythonpath = lib\n")
    await ws.indexer.index_file("pytest.ini")
    assert ws.targets("tests/test_lib.py", "IMPORTS") == {"lib/mylib.py#helper"}

    (ws.root / "pytest.ini").unlink()
    await ws.indexer.remove("pytest.ini")
    assert ws.targets("tests/test_lib.py", "IMPORTS") == {"lk://_ext/mylib#helper"}


async def test_config_edit_is_picked_up_by_the_next_diff_scan(ws: Ws) -> None:
    """監視の外（サーバーが止まっている間など）での設定の編集も、次の差分走査で拾う（指紋は索引に残る）。"""
    ws.write("lib/mylib.py", LIB)
    ws.write("tests/test_lib.py", USES_LIB)
    ws.write("setup.cfg", "[metadata]\nname = x\n")
    await ws.indexer.scan()
    assert ws.targets("tests/test_lib.py", "IMPORTS") == {"lk://_ext/mylib#helper"}

    ws.write("setup.cfg", "[metadata]\nname = x\n\n[tool:pytest]\npythonpath = lib\n")
    report = await ws.indexer.scan()
    assert report.indexed == 1  # setup.cfg だけ（tests/test_lib.py は変わっていない）
    assert ws.targets("tests/test_lib.py", "IMPORTS") == {"lib/mylib.py#helper"}


async def test_importlib_mode_turns_sibling_imports_external(ws: Ws) -> None:
    ws.write("tests/contract/test_shared.py", SHARED)
    ws.write("tests/contract/test_user.py", USER)
    await ws.indexer.scan()
    assert "tests/contract/test_shared.py#Env" in ws.targets("tests/contract/test_user.py", "IMPORTS")
    ws.write("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "--import-mode=importlib"\n')
    await ws.indexer.index_file("pyproject.toml")
    assert ws.targets("tests/contract/test_user.py", "IMPORTS") == {
        "lk://_ext/test_shared#Env",
        "lk://_ext/test_shared#build_env",
    }
