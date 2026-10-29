"""``engine.indexer`` が作る ``relations`` の目標（設計書 §7.4.6 項 3、仕様書 §9.3、T-06・T-07 の要求）。

小さなワークスペースを ``tmp_path`` に作って索引し、SQL で見る。外部シンボル（``lk://_ext/``）、組み込み名、
オーバーロードへの目標、外部シンボルの ``kind`` を確かめる。
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

WS_ID = "mini"

FILES = {
    "pkg/__init__.py": "",
    "pkg/errors.py": "class AppError(Exception):\n    pass\n",
    "pkg/svc.py": (
        "import os\n"
        "from decimal import Decimal\n"
        "from pkg.errors import AppError\n"
        "\n"
        "\n"
        "class Failure(AppError):\n"
        "    pass\n"
        "\n"
        "\n"
        "def run(a: str):\n"
        "    if a:\n"
        "        os.getcwd()\n"
        "    raise ValueError(Decimal(a).quantize(Decimal('1')))\n"
    ),
    "web/shapes.ts": (
        'import { Base } from "./base";\n'
        'import { debounce } from "lodash";\n'
        "\n"
        "export interface Drawable { draw(): void; }\n"
        "export class Circle extends Base implements Drawable {\n"
        "  draw(): void { debounce(() => 1); }\n"
        "}\n"
        "export class Boom extends Error {}\n"
    ),
    "web/base.ts": "export class Base {}\n",
    "web/client.ts": (
        "export class Client {\n"
        "  charge(req: string): string;\n"
        "  charge(req: string, retries: number): string;\n"
        "  charge(req: string, retries = 0): string { return req + retries; }\n"
        "}\n"
    ),
    "web/app.ts": (
        'import { Client } from "./client";\n'
        "const client = new Client();\n"
        "export function checkout(): string {\n"
        '  return client.charge("x");\n'
        "}\n"
    ),
}


@dataclass
class Env:
    indexer: Indexer
    readers: ReaderPool

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "mini"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache())
    report = await indexer.scan()
    assert not report.errors, report.errors
    yield Env(indexer, readers)
    await indexer.aclose()
    readers.close()
    writer.close()


def edges(env: Env, kind: str) -> set[tuple[str, str]]:
    return {
        (s.removeprefix(f"lk://{WS_ID}/"), t.removeprefix(f"lk://{WS_ID}/"))
        for s, t in env.rows("SELECT source_urn, target_urn FROM relations WHERE relation_kind = ?", kind)
    }


async def test_unresolved_imports_become_external_symbols(env: Env) -> None:
    assert {
        ("pkg/svc.py", "lk://_ext/os"),
        ("pkg/svc.py", "lk://_ext/decimal#Decimal"),
        ("web/shapes.ts", "lk://_ext/lodash#debounce"),
    } <= edges(env, "IMPORTS")
    rows = env.rows("SELECT file_id, is_external FROM symbols WHERE urn = ?", "lk://_ext/decimal#Decimal")
    assert rows == [(None, 1)]


async def test_inherits_and_implements_rows(env: Env) -> None:
    assert edges(env, "INHERITS") >= {
        ("pkg/errors.py#AppError", "lk://_ext/builtins#Exception"),
        ("pkg/svc.py#Failure", "pkg/errors.py#AppError"),
        ("web/shapes.ts#Circle", "web/base.ts#Base"),
        ("web/shapes.ts#Boom", "lk://_ext/globals#Error"),
    }
    assert ("web/shapes.ts#Circle", "web/shapes.ts#Drawable") in edges(env, "IMPLEMENTS")


async def test_calls_to_external_functions_classes_and_members(env: Env) -> None:
    run = "pkg/svc.py#run"
    calls = edges(env, "CALLS")
    assert {
        (run, "lk://_ext/os#getcwd"),
        (run, "lk://_ext/builtins#ValueError"),
        (run, "lk://_ext/decimal#Decimal"),
        (run, "lk://_ext/decimal#Decimal.quantize"),
        ("web/shapes.ts#Circle.draw", "lk://_ext/lodash#debounce"),
    } <= calls
    guard = env.rows(
        "SELECT guard_condition FROM relations WHERE target_urn = ? AND relation_kind = 'CALLS'",
        "lk://_ext/os#getcwd",
    )
    assert guard == [("a",)]


async def test_external_symbol_kinds(env: Env) -> None:
    kinds = dict(env.rows("SELECT urn, kind FROM symbols WHERE is_external = 1"))
    assert kinds["lk://_ext/os"] == "Module"
    assert kinds["lk://_ext/decimal#Decimal"] == "Class"
    assert kinds["lk://_ext/decimal#Decimal.quantize"] == "Method"
    assert kinds["lk://_ext/builtins#Exception"] == "Class"
    assert kinds["lk://_ext/builtins#ValueError"] == "Class"
    assert kinds["lk://_ext/os#getcwd"] == "Function"
    assert kinds["lk://_ext/lodash#debounce"] == "Function"
    assert kinds["lk://_ext/globals#Error"] == "Class"


async def test_call_to_an_overloaded_method_points_at_the_implementation(env: Env) -> None:
    """``Client.charge`` はオーバーロード宣言が 2 つと実装が 1 つ。arity なしの目標は、最後の宣言（実装）の URN に直る。"""
    targets = [
        t.removeprefix(f"lk://{WS_ID}/")
        for (t,) in env.rows(
            "SELECT target_urn FROM relations WHERE source_urn = ? AND relation_kind = 'CALLS'",
            f"lk://{WS_ID}/web/app.ts#checkout",
        )
    ]
    assert len(targets) == 1
    impl = env.rows(
        "SELECT urn FROM symbols WHERE qualified_name = 'Client.charge' ORDER BY start_line DESC LIMIT 1"
    )[0][0]
    assert targets[0] == impl.removeprefix(f"lk://{WS_ID}/")
    assert env.rows("SELECT COUNT(*) FROM symbols WHERE urn = ?", impl) == [(1,)]


# --- 後から作られた・消えたファイルを import している側の関係（BUG-0025。engine/reresolve.py） -------------


@dataclass
class Bare:
    """空のワークスペース。ファイルを書いて ``index_file`` / ``remove`` / ``scan`` を好きな順に呼ぶ。"""

    indexer: Indexer
    readers: ReaderPool
    root: Path

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def rows(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        with self.readers.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def edges(self, source: str, kind: str | None = None) -> set[tuple[str, str]]:
        """``source``（``path`` または ``path#sym``）が出す行を ``(relation_kind, target)`` で返す（ワークスペース内は相対表記）。"""
        out = set()
        for k, t in self.rows(
            "SELECT relation_kind, target_urn FROM relations WHERE source_urn = ?", f"lk://{WS_ID}/{source}"
        ):
            if kind is None or k == kind:
                out.add((k, t.removeprefix(f"lk://{WS_ID}/")))
        return out

    def version(self) -> int:
        return int(self.rows("SELECT data_version FROM _meta")[0][0])


@pytest.fixture
async def bare(tmp_path: Path) -> AsyncIterator[Bare]:
    root = tmp_path / "bare"
    root.mkdir()
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id=WS_ID, root=root.resolve()), writer, readers, TreeCache())
    yield Bare(indexer, readers, root)
    await indexer.aclose()
    readers.close()
    writer.close()


A_PY = "from pkg.b import helper\n\n\ndef run():\n    return helper()\n"
B_PY = "def helper():\n    return 1\n"


async def _seed_a_before_b(bare: Bare) -> None:
    bare.write("pkg/__init__.py", "")
    bare.write("pkg/a.py", A_PY)
    await bare.indexer.index_file("pkg/__init__.py")
    await bare.indexer.index_file("pkg/a.py")


async def test_new_file_resolves_importers_that_were_external(bare: Bare) -> None:
    """票の再現: a.py（``from pkg.b import helper``）を先に取り込み、後から b.py を作る。a.py の行が b.py のシンボルに結ばれる。"""
    await _seed_a_before_b(bare)
    assert bare.edges("pkg/a.py", "IMPORTS") == {("IMPORTS", "lk://_ext/pkg#b.helper")}
    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "lk://_ext/pkg#b.helper")}
    version = bare.version()

    bare.write("pkg/b.py", B_PY)
    await bare.indexer.index_file("pkg/b.py")

    assert bare.edges("pkg/a.py", "IMPORTS") == {("IMPORTS", "pkg/b.py#helper")}
    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "pkg/b.py#helper")}
    assert bare.rows(
        "SELECT target_file_id = (SELECT file_id FROM files WHERE path = 'pkg/b.py') FROM relations"
        " WHERE source_urn = ? AND relation_kind = 'CALLS'",
        f"lk://{WS_ID}/pkg/a.py#run",
    ) == [(1,)]
    assert bare.version() == version + 1  # b.py の取り込みと a.py の解き直しは 1 つのトランザクション


async def test_removed_file_returns_importers_to_external(bare: Bare) -> None:
    """b.py を消すと、b.py のシンボルを指していた a.py の行は外部（``lk://_ext/pkg#b.helper``）に戻る。"""
    bare.write("pkg/__init__.py", "")
    bare.write("pkg/b.py", B_PY)
    bare.write("pkg/a.py", A_PY)
    for rel in ("pkg/__init__.py", "pkg/b.py", "pkg/a.py"):
        await bare.indexer.index_file(rel)
    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "pkg/b.py#helper")}
    version = bare.version()

    (bare.root / "pkg/b.py").unlink()
    assert await bare.indexer.remove("pkg/b.py") is True

    assert bare.edges("pkg/a.py", "IMPORTS") == {("IMPORTS", "lk://_ext/pkg#b.helper")}
    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "lk://_ext/pkg#b.helper")}
    assert bare.version() == version + 1


async def test_rename_moves_importers_between_modules(bare: Bare) -> None:
    """b.py → c.py の改名（remove + index_file）。b を import していた a.py は外部へ、c を import していた d.py は c.py へ結ばれる。"""
    bare.write("pkg/__init__.py", "")
    bare.write("pkg/b.py", B_PY)
    bare.write("pkg/a.py", A_PY)
    bare.write("pkg/d.py", "from pkg.c import helper\n\n\ndef go():\n    return helper()\n")
    for rel in ("pkg/__init__.py", "pkg/b.py", "pkg/a.py", "pkg/d.py"):
        await bare.indexer.index_file(rel)
    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "pkg/b.py#helper")}
    assert bare.edges("pkg/d.py#go", "CALLS") == {("CALLS", "lk://_ext/pkg#c.helper")}

    (bare.root / "pkg/b.py").rename(bare.root / "pkg/c.py")
    await bare.indexer.remove("pkg/b.py")
    await bare.indexer.index_file("pkg/c.py")

    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "lk://_ext/pkg#b.helper")}
    assert bare.edges("pkg/d.py#go", "CALLS") == {("CALLS", "pkg/c.py#helper")}


async def test_typescript_relative_import_resolves_when_the_file_appears(bare: Bare) -> None:
    """未解決の相対 import は行そのものが無い（外部にもならない）。後からファイルができたら IMPORTS / CALLS が現れ、消えたら無くなる。"""
    bare.write("web/app.ts", 'import { helper } from "./util";\nexport function run() { return helper(); }\n')
    await bare.indexer.index_file("web/app.ts")
    assert bare.edges("web/app.ts") == set()
    assert bare.edges("web/app.ts#run") == set()

    bare.write("web/util.ts", "export function helper() { return 1; }\n")
    await bare.indexer.index_file("web/util.ts")

    assert bare.edges("web/app.ts", "IMPORTS") == {("IMPORTS", "web/util.ts#helper")}
    assert bare.edges("web/app.ts#run", "CALLS") == {("CALLS", "web/util.ts#helper")}

    (bare.root / "web/util.ts").unlink()
    await bare.indexer.remove("web/util.ts")
    assert bare.edges("web/app.ts#run") == set()  # 相対 import は解決できなければ行を持たない


async def test_python_relative_import_resolves_when_the_file_appears(bare: Bare) -> None:
    bare.write("pkg/__init__.py", "")
    bare.write("pkg/a.py", "from .b import helper\n\n\ndef run():\n    return helper()\n")
    await bare.indexer.index_file("pkg/a.py")
    assert bare.edges("pkg/a.py#run") == set()

    bare.write("pkg/b.py", B_PY)
    await bare.indexer.index_file("pkg/b.py")

    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "pkg/b.py#helper")}


async def test_file_already_on_disk_but_not_indexed_yet(bare: Bare) -> None:
    """b.py がディスクにはあるが索引にまだ無いうちに a.py を取り込むと、行は一度捨てられる。b.py を取り込んだら結ばれる。"""
    bare.write("pkg/__init__.py", "")
    bare.write("pkg/a.py", A_PY)
    bare.write("pkg/b.py", B_PY)
    await bare.indexer.index_file("pkg/a.py")
    assert bare.edges("pkg/a.py#run") == set()

    await bare.indexer.index_file("pkg/b.py")

    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "pkg/b.py#helper")}


async def test_body_edit_does_not_reingest_importers(bare: Bare) -> None:
    """既存ファイルの編集では、import している側を取り込み直さない（編集のたびに解析し直さないため）。"""
    bare.write("pkg/__init__.py", "")
    bare.write("pkg/b.py", B_PY)
    bare.write("pkg/a.py", A_PY)
    for rel in ("pkg/__init__.py", "pkg/b.py", "pkg/a.py"):
        await bare.indexer.index_file(rel)
    before = bare.indexer.ingest_calls
    importer_rows = bare.rows("SELECT relation_id FROM relations WHERE source_urn LIKE 'lk://mini/pkg/a.py%'")

    bare.write("pkg/b.py", "def helper():\n    return 2\n")
    await bare.indexer.index_file("pkg/b.py")

    assert bare.indexer.ingest_calls == before + 1
    assert (
        bare.rows("SELECT relation_id FROM relations WHERE source_urn LIKE 'lk://mini/pkg/a.py%'")
        == importer_rows
    )


# --- 既存ファイルのシンボルの集合が変わったとき（BUG-0034。engine/reresolve.py の ``symbols_changed``） -----------------

B_PY_WITHOUT_HELPER = "def other():\n    return 0\n"
B_PY_WITH_HELPER = B_PY + "\n\n" + B_PY_WITHOUT_HELPER
UTIL_TS_WITHOUT_HELPER = "export function other() { return 0; }\n"
UTIL_TS_WITH_HELPER = "export function helper() { return 1; }\n" + UTIL_TS_WITHOUT_HELPER
APP_TS = 'import { helper } from "./util";\nexport function run() { return helper(); }\n'


@dataclass(frozen=True)
class Pair:
    """定義元（``helper`` を持つ・持たない版）とそれを import する側。"""

    definer: str
    importer: str
    importer_text: str
    without_helper: str
    with_helper: str

    @property
    def run(self) -> str:
        return f"{self.importer}#run"


PAIRS = {
    "python": Pair("pkg/b.py", "pkg/a.py", A_PY, B_PY_WITHOUT_HELPER, B_PY_WITH_HELPER),
    "typescript": Pair("web/util.ts", "web/app.ts", APP_TS, UTIL_TS_WITHOUT_HELPER, UTIL_TS_WITH_HELPER),
}


async def _seed_importer_over_incomplete_definer(bare: Bare, language: str) -> Pair:
    """``helper`` を持たない定義元を取り込み、そのあとで ``helper`` を import する側を取り込む（import 元の行は捨てられる）。"""
    pair = PAIRS[language]
    if language == "python":
        bare.write("pkg/__init__.py", "")
        await bare.indexer.index_file("pkg/__init__.py")
    bare.write(pair.definer, pair.without_helper)
    bare.write(pair.importer, pair.importer_text)
    await bare.indexer.index_file(pair.definer)
    await bare.indexer.index_file(pair.importer)
    return pair


@pytest.mark.parametrize("language", ["python", "typescript"])
async def test_symbol_added_later_restores_importer_relations(bare: Bare, language: str) -> None:
    """票の再現: 定義元に ``helper`` が無いうちに import 元を取り込む（行は捨てられ ``_ext`` にもならない）。あとで ``helper`` を足すと結ばれる。"""
    pair = await _seed_importer_over_incomplete_definer(bare, language)
    assert bare.edges(pair.importer) == set()
    assert bare.edges(pair.run) == set()
    version = bare.version()
    assert bare.indexer.reingested_importers == 0

    bare.write(pair.definer, pair.with_helper)
    await bare.indexer.index_file(pair.definer)

    assert bare.edges(pair.importer, "IMPORTS") == {("IMPORTS", f"{pair.definer}#helper")}
    assert bare.edges(pair.run, "CALLS") == {("CALLS", f"{pair.definer}#helper")}
    assert bare.rows(
        "SELECT target_file_id = (SELECT file_id FROM files WHERE path = ?) FROM relations"
        " WHERE source_urn = ? AND relation_kind = 'CALLS'",
        pair.definer,
        f"lk://{WS_ID}/{pair.run}",
    ) == [(1,)]
    assert bare.indexer.reingested_importers == 1
    assert bare.version() == version + 1  # 定義元の取り込みと import 元の解き直しは 1 つのトランザクション


@pytest.mark.parametrize("language", ["python", "typescript"])
async def test_symbol_removed_drops_importer_relations(bare: Bare, language: str) -> None:
    """定義元から ``helper`` を消すと、import 元の行は捨てられる（最初から ``helper`` が無かったときと同じ。``_ext`` には戻らない）。

    定義元のファイルそのものが無くなったときだけ ``_ext`` に戻る（``test_removed_file_returns_importers_to_external``）。
    """
    pair = await _seed_importer_over_incomplete_definer(bare, language)
    bare.write(pair.definer, pair.with_helper)
    await bare.indexer.index_file(pair.definer)
    assert bare.edges(pair.run, "CALLS") == {("CALLS", f"{pair.definer}#helper")}
    before = bare.indexer.reingested_importers
    version = bare.version()

    bare.write(pair.definer, pair.without_helper)
    await bare.indexer.index_file(pair.definer)

    assert bare.edges(pair.importer) == set()
    assert bare.edges(pair.run) == set()
    assert bare.indexer.reingested_importers == before + 1  # 集合が変わったので import 元を取り込み直した
    assert bare.version() == version + 1


@pytest.mark.parametrize("language", ["python", "typescript"])
async def test_body_only_edit_does_not_reingest_importers_when_symbols_are_the_same(
    bare: Bare, language: str
) -> None:
    """シンボルの集合が同じ（本文だけの編集）なら、import している側を取り込み直さない（数えるのは取り込み直した import 元の数）。"""
    pair = await _seed_importer_over_incomplete_definer(bare, language)
    bare.write(pair.definer, pair.with_helper)
    await bare.indexer.index_file(pair.definer)  # helper を足した（import 元の取り込み直しはここで 1 回）
    importer_rows = bare.rows(
        "SELECT relation_id FROM relations WHERE source_urn LIKE ?", f"lk://{WS_ID}/{pair.importer}%"
    )
    assert importer_rows
    before, ingests = bare.indexer.reingested_importers, bare.indexer.ingest_calls
    version = bare.version()

    bare.write(pair.definer, pair.with_helper.replace("return 1", "return 2").replace("return 0", "return 3"))
    await bare.indexer.index_file(pair.definer)

    assert bare.indexer.ingest_calls == ingests + 1
    assert bare.indexer.reingested_importers == before  # 取り込み直していない
    assert (
        bare.rows(
            "SELECT relation_id FROM relations WHERE source_urn LIKE ?", f"lk://{WS_ID}/{pair.importer}%"
        )
        == importer_rows
    )
    assert bare.edges(pair.run, "CALLS") == {("CALLS", f"{pair.definer}#helper")}
    assert bare.version() == version + 1


async def test_symbol_added_with_no_importers_re_ingests_nothing_else(bare: Bare) -> None:
    """import している側が無ければ、シンボルを足しても取り込み直すものは無い。"""
    bare.write("pkg/__init__.py", "")
    bare.write("pkg/b.py", B_PY_WITHOUT_HELPER)
    await bare.indexer.index_file("pkg/__init__.py")
    await bare.indexer.index_file("pkg/b.py")
    version = bare.version()

    bare.write("pkg/b.py", B_PY_WITH_HELPER)
    await bare.indexer.index_file("pkg/b.py")

    assert bare.indexer.reingested_importers == 0
    assert bare.version() == version + 1


async def test_incremental_scan_resolves_importers_when_a_symbol_was_added(bare: Bare) -> None:
    """起動時の差分走査: 定義元に ``helper`` が足されていた（import 元は変わっていない）。走査の最後に import 元の行が結ばれ、消えていれば外れる。"""
    pair = await _seed_importer_over_incomplete_definer(bare, "python")
    bare.write(pair.definer, pair.with_helper)

    report = await bare.indexer.scan()

    assert report.indexed == 1 and report.created == 0, report
    assert bare.edges(pair.run, "CALLS") == {("CALLS", f"{pair.definer}#helper")}
    assert bare.indexer.reingested_importers == 1

    bare.write(pair.definer, pair.without_helper)
    report = await bare.indexer.scan()
    assert report.indexed == 1, report
    assert bare.edges(pair.run) == set()
    assert bare.indexer.reingested_importers == 2


async def test_excluded_file_is_not_resurrected_by_its_own_probes(bare: Bare) -> None:
    """``__init__.py`` は自分の親ディレクトリの語幹を調べる。除外に変わって ``remove`` されたとき、自分自身を取り込み直さない。"""
    bare.write("pkg/__init__.py", "from . import b\n")
    bare.write("pkg/b.py", B_PY)
    await bare.indexer.index_file("pkg/b.py")
    await bare.indexer.index_file("pkg/__init__.py")

    assert await bare.indexer.remove("pkg/__init__.py") is True  # 実体は残したまま索引から外す

    assert bare.rows("SELECT COUNT(*) FROM files WHERE path = 'pkg/__init__.py'") == [(0,)]


async def test_incremental_scan_resolves_importers_that_were_not_touched(bare: Bare) -> None:
    """起動時の差分走査: a.py は変わっていない（取り込まれない）が、走査の間に b.py ができていたら a.py の行が結ばれる。消えたら外部へ戻る。"""
    await _seed_a_before_b(bare)
    bare.write("pkg/b.py", B_PY)

    report = await bare.indexer.scan()

    assert report.created == 1 and report.unchanged >= 2, report
    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "pkg/b.py#helper")}

    (bare.root / "pkg/b.py").unlink()
    report = await bare.indexer.scan()
    assert report.removed == 1, report
    assert bare.edges("pkg/a.py#run", "CALLS") == {("CALLS", "lk://_ext/pkg#b.helper")}


async def test_import_probes_are_replaced_on_reingest_and_cascade_on_remove(bare: Bare) -> None:
    await _seed_a_before_b(bare)

    def stems(path: str) -> set[str]:
        return {
            s
            for (s,) in bare.rows(
                "SELECT p.stem FROM _import_probes p JOIN files f ON f.file_id = p.file_id WHERE f.path = ?",
                path,
            )
        }

    assert {"pkg/b", "src/pkg/b"} <= stems("pkg/a.py")
    bare.write("pkg/a.py", "import os\n")
    await bare.indexer.index_file("pkg/a.py")
    assert "pkg/b" not in stems("pkg/a.py")
    assert "os" in stems("pkg/a.py")
    (bare.root / "pkg/a.py").unlink()
    await bare.indexer.remove("pkg/a.py")
    assert bare.rows(
        "SELECT COUNT(*) FROM _import_probes WHERE file_id NOT IN (SELECT file_id FROM files)"
    ) == [(0,)]


@pytest.mark.parametrize(
    ("path", "stem"),
    [
        ("pkg/b.py", "pkg/b"),
        ("pkg/b/__init__.py", "pkg/b"),
        ("pkg/b.pyi", "pkg/b"),
        ("web/util.ts", "web/util"),
        ("web/util/index.tsx", "web/util"),
        ("util.js", "util"),
        ("__init__.py", ""),
        ("src/pkg/__init__.py", "src/pkg"),
        ("README", "README"),
    ],
)
def test_stem_of(path: str, stem: str) -> None:
    from lucianke.engine.reresolve import stem_of

    assert stem_of(path) == stem
