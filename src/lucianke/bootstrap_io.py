"""stdout の隔離（設計書 ADR-3、仕様書 §12.1、スパイク S-1）。

MCP の stdio では ``stdout`` に JSON-RPC 以外を 1 バイトも書けない（NFR-OPS-1）。依存ライブラリや C 拡張が
fd 1 へ書く経路を排除するため、**起動の最初に**（重い import より前に）次を行う:

* 本物の stdin / stdout を ``os.dup`` で退避する（退避 fd は非継承。子プロセスには渡らない）。
* fd 1 を stderr に重ねる（``print``・``os.write(1, …)``・C の ``printf``・stdout を継承した子もすべて stderr へ）。
* fd 0 を NUL に差し替える（子プロセスがプロトコルの入力を横取りしない）。
* ``sys.stdout`` を ``sys.stderr`` にする。

トランスポートへは退避 fd から作ったテキストストリームを **明示的に** 渡す（``open_transport_streams``）。
引数なしの ``stdio_server()`` は呼ばない（fd 0 が NUL なので入力 EOF と見て即終了する。S-1 の naive 構成）。

このモジュールは標準ライブラリだけを import する。import 時の副作用は無い（``bootstrap_io()`` を呼んだときだけ動く）。
"""

from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StdioFds:
    """退避した本物の標準入出力の fd。"""

    real_in: int
    real_out: int


_FDS: StdioFds | None = None


def bootstrap_io() -> StdioFds:
    """fd 1 を stderr に重ね、本物の stdin / stdout を退避して返す。2 回目以降は最初の結果を返す（冪等）。"""
    global _FDS
    if _FDS is not None:
        return _FDS
    real_out = os.dup(1)
    real_in = os.dup(0)
    # CPython 3.12 + UCRT の dup2 は Win32 の標準ハンドルも付け替える（S-1 で実測。SetStdHandle は不要）。
    os.dup2(2, 1)
    nul = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(nul, 0)
    finally:
        os.close(nul)
    sys.stdout = sys.stderr
    _FDS = StdioFds(real_in=real_in, real_out=real_out)
    return _FDS


def open_transport_streams(fds: StdioFds) -> tuple[io.TextIOWrapper, io.TextIOWrapper]:
    """退避 fd からトランスポート用の ``(stdin, stdout)`` テキストストリームを作る。

    ``newline="\\n"`` を忘れると Windows で行末が CRLF になる（S-1）。``closefd=False`` なので fd の寿命は呼び出し側が持つ。
    """
    stdin = io.TextIOWrapper(
        os.fdopen(fds.real_in, "rb", closefd=False), encoding="utf-8", errors="replace", newline="\n"
    )
    stdout = io.TextIOWrapper(os.fdopen(fds.real_out, "wb", closefd=False), encoding="utf-8", newline="\n")
    return stdin, stdout
