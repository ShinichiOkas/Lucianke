"""`lucianke` コマンドの入口。

引数なし: ホストから子プロセスとして起動され、stdio で MCP を話す（設計書 ADR-16）。
補助サブコマンド: index / doctor / bench（M1 の後半で実装）。
"""

from __future__ import annotations

import sys

from lucianke import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] in (["--version"], ["-V"]):
        print(f"lucianke {__version__}", file=sys.stderr)
        return 0
    if args:
        print(f"lucianke: unknown subcommand {args[0]!r} (not implemented yet)", file=sys.stderr)
        return 2
    from lucianke.app import run_stdio  # 遅延 import: --version を軽く保つ

    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
