"""`lucianke` コマンドの入口。

引数なし: ホストから子プロセスとして起動され、stdio で MCP を話す（設計書 ADR-16）。
補助サブコマンド: doctor（環境診断。結果は stderr のみ）。index / bench は M1 の後半で実装。
"""

from __future__ import annotations

import sys

from lucianke import __version__

# ``cli.doctor.SELFTEST_SUBCOMMAND`` と同じ綴り（exe には -c を受ける Python が無いので、doctor は exe 自身をこの名前で起こす）
_SELFTEST_SUBCOMMAND = "_doctor-selftest"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] in (["--version"], ["-V"]):
        print(f"lucianke {__version__}", file=sys.stderr)
        return 0
    if args[:1] == ["doctor"]:
        return _run_doctor(args[1:])
    if args[:1] == [_SELFTEST_SUBCOMMAND]:  # doctor が exe 自身を起動する隠しサブコマンド（stdout 隔離の自己診断）
        from lucianke.bootstrap_io import run_stdout_selftest

        return run_stdout_selftest(args[1] if len(args) > 1 else "")
    if args:
        print(f"lucianke: unknown subcommand {args[0]!r} (not implemented yet)", file=sys.stderr)
        return 2
    from lucianke.app import run_stdio  # 遅延 import: --version を軽く保つ

    return run_stdio()


def _run_doctor(args: list[str]) -> int:
    """``lucianke doctor``。stdout には 1 バイトも書かない（隔離してから走らせ、結果は stderr へ）。"""
    from lucianke.bootstrap_io import bootstrap_io

    bootstrap_io()
    from lucianke.cli.doctor import main as doctor_main
    from lucianke.config import load_settings

    return doctor_main(args, cache_root=load_settings().cache_root)


if __name__ == "__main__":
    raise SystemExit(main())
