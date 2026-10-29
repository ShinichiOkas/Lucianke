"""外部シンボル（``lk://_ext/<pkg>#<name>``）への関係の抽出（設計書 §7.4.6 項 3、仕様書 §9.3）。

未解決の import の名前表（``imports.external_name_map``）と、言語の組み込み名（``syntax/builtin_names.py``）を
``extract_relations`` の ``resolve_name`` / ``builtins_package`` に渡したときの目標を確かめる。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

from lucianke.core.model import RelPath
from lucianke.core.textpos import LineIndex
from lucianke.syntax.builtin_names import BUILTINS_PACKAGE, GLOBALS_PACKAGE
from lucianke.syntax.imports import external_name_map, extract_imports
from lucianke.syntax.relations import RelationCandidate, extract_relations
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import ParsedFile

WS = "ws"
_LANGUAGES: dict[str, Callable[[], object]] = {
    "python": tree_sitter_python.language,
    "typescript": tree_sitter_typescript.language_typescript,
}


def _language(language_id: str) -> Language:
    return Language(_LANGUAGES[language_id]())  # type: ignore[arg-type]


def _parsed(language_id: str, source: bytes) -> ParsedFile:
    tree = Parser(_language(language_id)).parse(source)
    return ParsedFile(
        language_id=language_id,
        sha256="0" * 64,
        source=source,
        tree=tree,
        line_index=LineIndex(source),
        has_errors=tree.root_node.has_error,
    )


def externals(language_id: str, text: str) -> dict[str, str]:
    facts = extract_imports(_parsed(language_id, text.encode()), language_id=language_id)
    return external_name_map(facts, language_id=language_id)


def relations(language_id: str, text: str, rel: str) -> list[RelationCandidate]:
    parsed = _parsed(language_id, text.encode())
    language = _language(language_id)
    ext = external_name_map(extract_imports(parsed, language_id=language_id), language_id=language_id)
    symbols = extract(
        parsed,
        language_id=language_id,
        language=language,
        query_source=load_query_source(language_id),
        workspace_id=WS,
        rel=RelPath(rel),
    )
    return extract_relations(
        parsed,
        language_id=language_id,
        language=language,
        symbols=symbols,
        workspace_id=WS,
        rel=RelPath(rel),
        resolve_name=ext.get,
        builtins_package=BUILTINS_PACKAGE if language_id == "python" else GLOBALS_PACKAGE,
    )


def targets(rels: list[RelationCandidate], kind: str) -> list[tuple[str, str | None]]:
    return [(r.source_urn.removeprefix(f"lk://{WS}/"), r.target_urn) for r in rels if r.relation_kind == kind]


# --- 名前表 -----------------------------------------------------------------------------------


def test_python_external_names() -> None:
    got = externals(
        "python",
        "import os\n"
        "import os.path\n"
        "import a.b.c\n"
        "import x.y as z\n"
        "from decimal import Decimal, ROUND_UP as up\n"
        "from os.path import join\n"
        "from . import sibling\n"
        "from ..pkg import other\n"
        "from __future__ import annotations\n"
        "from m import *\n",
    )
    assert got == {
        "os": "lk://_ext/os",
        "a": "lk://_ext/a",
        "z": "lk://_ext/x#y",
        "Decimal": "lk://_ext/decimal#Decimal",
        "up": "lk://_ext/decimal#ROUND_UP",
        "join": "lk://_ext/os#path.join",
    }


def test_typescript_external_names() -> None:
    got = externals(
        "typescript",
        'import React, { useState as useS, type FC } from "react";\n'
        'import * as path from "node:path";\n'
        'import { Observable } from "@scope/rx/operators";\n'
        'import { debounce } from "lodash/fp";\n'
        'import { Base } from "./base";\n'
        'import { up } from "../up";\n'
        'export { re } from "other";\n',
    )
    assert got == {
        "React": "lk://_ext/react",
        "useS": "lk://_ext/react#useState",
        "FC": "lk://_ext/react#FC",
        "path": "lk://_ext/path",
        "Observable": "lk://_ext/@scope/rx#Observable",
        "debounce": "lk://_ext/lodash#debounce",
    }


def test_internal_names_are_not_overridden() -> None:
    parsed = _parsed("python", b"from decimal import Decimal\nimport os\n")
    facts = extract_imports(parsed, language_id="python")
    assert external_name_map(facts, language_id="python", internal={"Decimal"}) == {"os": "lk://_ext/os"}


# --- Python の関係 ----------------------------------------------------------------------------

PY = """
import os
from decimal import Decimal
from typing import Generic, TypeVar

T = TypeVar("T")


class Boom(Exception):
    pass


class Box(Generic[T], dict):
    def total(self, amount: str):
        d = Decimal(amount)
        d.quantize(Decimal("1"))
        os.path.join("a", "b")
        print(len(amount))
        self.value.load()
        raise ValueError("x")


def f(config):
    config.load()
    print("ok")
"""


@pytest.fixture(scope="module")
def py() -> list[RelationCandidate]:
    return relations("python", PY, "m.py")


def test_python_inherits_builtin_and_external_bases(py: list[RelationCandidate]) -> None:
    assert targets(py, "INHERITS") == [
        ("m.py#Boom", "lk://_ext/builtins#Exception"),
        ("m.py#Box", "lk://_ext/typing#Generic"),
        ("m.py#Box", "lk://_ext/builtins#dict"),
    ]


def test_python_calls_resolve_to_external_classes_functions_and_members(py: list[RelationCandidate]) -> None:
    calls = {t for _, t in targets(py, "CALLS")}
    assert {
        "lk://_ext/decimal#Decimal",
        "lk://_ext/decimal#Decimal.quantize",
        "lk://_ext/os#path.join",
        "lk://_ext/builtins#print",
        "lk://_ext/builtins#len",
        "lk://_ext/builtins#ValueError",
        "lk://_ext/typing#TypeVar",
    } <= calls


def test_untyped_receiver_member_is_not_tied_to_an_external_import(py: list[RelationCandidate]) -> None:
    """``config.load()`` / ``self.value.load()`` は、レシーバの型が分からないので外部に結ばない。"""
    loads = [r for r in py if r.target_name == "load"]
    assert len(loads) == 2 and all(r.target_urn is None for r in loads)


def test_imports_point_at_external_symbols(py: list[RelationCandidate]) -> None:
    assert {t for _, t in targets(py, "IMPORTS")} == {
        "lk://_ext/os",
        "lk://_ext/decimal#Decimal",
        "lk://_ext/typing#Generic",
        "lk://_ext/typing#TypeVar",
    }


def test_local_definitions_shadow_builtins_and_imports() -> None:
    rels = relations(
        "python",
        "from decimal import Decimal\n\n\ndef print(x):\n    return x\n\n\ndef g(Decimal):\n    print(1)\n    Decimal(2)\n",
        "s.py",
    )
    assert targets(rels, "CALLS") == [("s.py#g", "lk://ws/s.py#print")]  # 引数の Decimal は参照に数えない


# --- TypeScript の関係 ------------------------------------------------------------------------

TS = """
import React, { useState as useS } from "react";
import * as path from "node:path";
import { Observable } from "@scope/rx";

export class Plain extends Error {}

export class Circle implements Disposable {
  draw(): Promise<void> {
    const p = path.join("a");
    const s = useS(0);
    console.log(p, s);
    JSON.parse("{}");
    return Promise.resolve(new Observable<number>());
  }
}
"""


@pytest.fixture(scope="module")
def ts() -> list[RelationCandidate]:
    return relations("typescript", TS, "m.ts")


def test_typescript_inherits_global_error(ts: list[RelationCandidate]) -> None:
    assert targets(ts, "INHERITS") == [("m.ts#Plain", "lk://_ext/globals#Error")]
    # lib.dom などの型（Disposable）は組み込み名に入れていないので、解決しない
    assert targets(ts, "IMPLEMENTS") == [("m.ts#Circle", None)]


def test_typescript_calls_resolve_to_external_modules_and_globals(ts: list[RelationCandidate]) -> None:
    calls = {t for _, t in targets(ts, "CALLS")}
    assert {
        "lk://_ext/path#join",
        "lk://_ext/react#useState",
        "lk://_ext/globals#console.log",
        "lk://_ext/globals#JSON.parse",
        "lk://_ext/globals#Promise.resolve",
        "lk://_ext/@scope/rx#Observable",
    } <= calls
    assert ("m.ts#Circle.draw", "lk://_ext/globals#Promise") in targets(ts, "TYPE_USES")
