"""``lucianke doctor`` の stdout 隔離の自己診断の本体（子プロセスで走る）。

``bootstrap_io`` の後に重い import と各経路の出力を行い、本物の stdout には退避 fd のトランスポートの 1 行（``marker``）だけが
届くことを、親（``doctor.check_stdout_isolation``）が確かめる。

起動のしかたは 2 通りで、本体はこの 1 つ:

* ソースから: ``python -I -c <doctor._SELFTEST_CODE> <src> <marker>``（``-I`` で利用者の site・``PYTHONPATH`` を見ない）
* exe（PyInstaller）から: ``lucianke.exe _doctor-selftest <marker>``（exe には ``-c`` を受ける Python が無いので、隠しサブコマンドで
  exe 自身を起動する。``__main__`` が振り分ける）

この モジュールは import の時点で ``bootstrap_io`` 以外を読まない（重い import は ``bootstrap_io`` の後でないと検査にならない）。
"""

from __future__ import annotations

import os
import sys
from typing import Final

from lucianke.bootstrap_io import bootstrap_io, open_transport_streams

SUBCOMMAND: Final = "_doctor-selftest"  # 隠しサブコマンド（利用者向けではない。``--help`` にも出さない）


def main(marker: str) -> int:
    fds = bootstrap_io()
    try:
        import dulwich.repo  # noqa: F401
        import jsonpath_ng  # noqa: F401
        import mcp.server  # noqa: F401
        import pydantic  # noqa: F401
        import tree_sitter  # noqa: F401
        import watchfiles  # noqa: F401
        import yaml  # noqa: F401
    except ImportError as exc:
        sys.stderr.write(f"import failed: {exc}\n")
        return 3
    print("doctor-noise-print")
    os.write(1, b"doctor-noise-fd1\n")
    assert sys.__stdout__ is not None
    sys.__stdout__.write("doctor-noise-dunder\n")
    sys.__stdout__.flush()
    _, out = open_transport_streams(fds)
    out.write(marker + "\n")
    out.flush()
    return 0
