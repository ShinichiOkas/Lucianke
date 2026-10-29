"""ツール層。各ツールは ``tNN_<name>.py`` で ``SPEC``（無ければ ``TOOL_SPEC``）に ``ToolSpec`` を宣言し、``register_tools`` が登録表に載せる。

登録は **自動**: ``lucianke.tools`` 配下の ``tNN_*.py`` を名前順に見つけて import する。各ツールの役がこのファイルを
触らずに済む（共有ファイルの排他をなくす）。import に失敗したモジュール（作りかけ、依存の不足）は stderr のログに
エラーを出して飛ばし、起動は続ける。「存在する ``tNN`` は全部 import でき ``SPEC`` を持つ」ことは単体テスト
（``tests/unit/test_tools_autoregister.py``）が確かめる。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from typing import Any, Protocol

_log = logging.getLogger("lucianke.tools")
_TOOL_MODULE = re.compile(r"t\d{2}_\w+")
_SPEC_ATTRS = ("SPEC", "TOOL_SPEC")


class _Registrar(Protocol):
    def register(self, spec: Any, /) -> None: ...


def discover_tool_modules() -> list[str]:
    """``tNN_*`` モジュールの名前（拡張子なし）を名前順に返す。import はしない。"""
    return sorted(m.name for m in pkgutil.iter_modules(__path__) if _TOOL_MODULE.fullmatch(m.name))


def spec_of(module: Any) -> Any | None:
    """モジュールの ``SPEC``（無ければ ``TOOL_SPEC``）。どちらも無ければ ``None``。"""
    for attr in _SPEC_ATTRS:
        spec = getattr(module, attr, None)
        if spec is not None:
            return spec
    return None


def register_tools(registry: _Registrar) -> list[str]:
    """``tNN_*`` を登録表に載せる（開発用の ping は ``app.build_registry`` が条件付きで足す）。載せたモジュール名を返す。

    モジュールの import 失敗・``SPEC`` の欠落・登録の失敗（名前の重複など）は、ログに残してそのモジュールだけ飛ばす。
    """
    registered: list[str] = []
    for name in discover_tool_modules():
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except Exception:  # noqa: BLE001  作りかけの SyntaxError / NameError なども含め、起動を止めない
            _log.exception("tool module %s failed to import; skipped", name, extra={"event": "tool_import_failed"})
            continue
        spec = spec_of(module)
        if spec is None:
            _log.error("tool module %s declares neither SPEC nor TOOL_SPEC; skipped", name,
                       extra={"event": "tool_spec_missing"})  # fmt: skip
            continue
        try:
            registry.register(spec)
        except Exception:  # noqa: BLE001
            _log.exception("tool %s could not be registered; skipped", name, extra={"event": "tool_register_failed"})
            continue
        registered.append(name)
    return registered
