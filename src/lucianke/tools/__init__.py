"""ツール層。各ツールは ``tNN_<name>.py`` で ``SPEC``（``ToolSpec``）を宣言し、``register_tools`` が登録表に載せる。"""

from __future__ import annotations

from typing import Any, Protocol


class _Registrar(Protocol):
    def register(self, spec: Any, /) -> None: ...


def register_tools(registry: _Registrar) -> None:
    """実ツールを登録表に載せる（開発用の ping は ``app.build_registry`` が条件付きで足す）。import は遅延させる。"""
    from lucianke.tools import t02_get_file_skeleton

    registry.register(t02_get_file_skeleton.SPEC)
    try:
        from lucianke.tools import t05_get_symbol_bodies
    except ImportError:  # T-05 が未配置の間も T-02 だけで起動できるように
        return
    registry.register(t05_get_symbol_bodies.SPEC)
