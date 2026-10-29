"""データ形式アダプタの登録（``syntax.support`` の import の末尾で 1 回。循環しない）。

``support`` を import するだけで ``ADAPTERS`` に json / jsonl / yaml / toml / xml / ipynb が入り、拡張子の逆引きも効く。
import の順序によらず登録されること（循環 import の検査）は、別プロセスで確かめる。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from lucianke.core.model import Tier
from lucianke.syntax import adapters
from lucianke.syntax.support import ADAPTERS, SUPPORT, get_adapter, get_support, language_for_extension

LANGS = ("json", "jsonl", "yaml", "toml", "xml", "ipynb")


def test_support_alone_registers_every_adapter() -> None:
    assert sorted(ADAPTERS) == sorted(LANGS)
    for lid in LANGS:
        assert ADAPTERS[lid].language_id == lid
        assert get_adapter(lid) is ADAPTERS[lid]
    assert ADAPTERS["ipynb"].tier == Tier.T2
    assert all(ADAPTERS[lid].tier == Tier.T3 for lid in LANGS if lid != "ipynb")


def test_registered_matches_the_adapters_package() -> None:
    assert {a.language_id for a in adapters.REGISTERED} == set(LANGS)
    for a in adapters.REGISTERED:
        assert ADAPTERS[a.language_id] is a
    assert all(
        adapters.ADAPTERS[ext] is get_adapter(a.language_id) for a in adapters._ALL for ext in a.extensions
    )


@pytest.mark.parametrize(
    ("ext", "lid"),
    [
        (".json", "json"),
        (".jsonl", "jsonl"),
        (".ndjson", "jsonl"),
        (".yaml", "yaml"),
        (".yml", "yaml"),
        (".toml", "toml"),
        (".xml", "xml"),
        (".xsd", "xml"),
        (".svg", "xml"),
        (".ipynb", "ipynb"),
    ],
)
def test_extension_reverse_lookup_includes_adapters(ext: str, lid: str) -> None:
    assert language_for_extension(ext) == lid


def test_grammar_support_still_wins_in_get_support() -> None:
    """json / yaml / toml / xml は文法つきの登録が ``get_support`` で先に返る。アダプタは ``get_adapter`` で引く。"""
    for lid in ("json", "yaml", "toml", "xml"):
        assert get_support(lid) is SUPPORT[lid]
        assert get_adapter(lid) is not SUPPORT[lid]
    assert get_support("jsonl") is get_adapter("jsonl")
    assert get_support("ipynb") is get_adapter("ipynb")
    assert get_adapter("python") is None


@pytest.mark.parametrize(
    "first",
    [
        "lucianke.syntax.adapters",
        "lucianke.syntax.support",
        "lucianke.syntax.adapters.ipynb",
        "lucianke.syntax.adapters.json_",
        "lucianke.syntax.skeleton",
    ],
)
def test_import_order_does_not_matter(first: str) -> None:
    code = (
        f"import {first}\n"
        "import lucianke.syntax.support as s, lucianke.syntax.adapters as a, lucianke.syntax.adapters.ipynb\n"
        "assert sorted(s.ADAPTERS) == ['ipynb', 'json', 'jsonl', 'toml', 'xml', 'yaml'], sorted(s.ADAPTERS)\n"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
