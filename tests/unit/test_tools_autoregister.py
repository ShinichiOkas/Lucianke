"""ツールの自動登録（``lucianke.tools.register_tools``）。

* 存在する ``tNN_*`` は全部 import でき、``SPEC`` か ``TOOL_SPEC`` を持つ（各ツールの役が ``__init__.py`` を触らない代わりの検査）。
* 全部が登録表に載り、名前順に並ぶ。
* import に失敗するモジュール・``SPEC`` の無いモジュール・名前が重複するモジュールがあっても、他は載り、例外を出さない。
"""

from __future__ import annotations

import importlib
import logging
import types

import pytest

from lucianke import tools
from lucianke.mcp.registry import ToolRegistry


def test_every_tool_module_imports_and_declares_a_spec() -> None:
    names = tools.discover_tool_modules()
    assert {"t02_get_file_skeleton", "t05_get_symbol_bodies", "t19_get_project_db_schema", "t20_query_project_db"} <= set(names)
    assert names == sorted(names)
    for name in names:
        module = importlib.import_module(f"lucianke.tools.{name}")  # 失敗したらそのモジュールが票の対象
        spec = tools.spec_of(module)
        assert spec is not None, f"{name} must declare SPEC (or TOOL_SPEC)"
        assert spec.name and spec.tid.startswith("T-"), name


def test_register_tools_registers_all_in_name_order() -> None:
    registry = ToolRegistry()
    registered = tools.register_tools(registry)
    assert registered == tools.discover_tool_modules()
    assert len(registry) == len(registered)
    assert "get_project_db_schema" in registry and "query_project_db" in registry  # TOOL_SPEC 側も載る


def test_bad_modules_are_skipped_with_a_log(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    real_names = tools.discover_tool_modules()
    real_import = importlib.import_module
    no_spec = types.ModuleType("lucianke.tools.t97_no_spec")
    duplicate = types.ModuleType("lucianke.tools.t96_duplicate")
    first = real_import(f"lucianke.tools.{real_names[0]}")
    duplicate.SPEC = tools.spec_of(first)  # 先に載るモジュールと同じ名前

    def fake_import(name: str, package: str | None = None):
        if name == "lucianke.tools.t98_broken":
            raise SyntaxError("half-written module")
        if name == "lucianke.tools.t97_no_spec":
            return no_spec
        if name == "lucianke.tools.t96_duplicate":
            return duplicate
        return real_import(name, package)

    monkeypatch.setattr(tools, "discover_tool_modules", lambda: [*real_names, "t96_duplicate", "t97_no_spec", "t98_broken"])
    monkeypatch.setattr(tools.importlib, "import_module", fake_import)
    registry = ToolRegistry()
    with caplog.at_level(logging.ERROR, logger="lucianke.tools"):
        registered = tools.register_tools(registry)  # 例外を出さない
    assert registered == real_names
    assert len(registry) == len(real_names)
    text = caplog.text
    assert "t98_broken" in text and "t97_no_spec" in text and "t96_duplicate" in text
