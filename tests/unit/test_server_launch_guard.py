"""実プロセスのサーバーの起動を ``python -m lucianke`` に揃える見張り（BUG-0030）。

``uv run ... lucianke``（コンソールスクリプトの ``lucianke.exe``）で起こしたサーバーが動いている間に ``pyproject.toml``
が変わると、Windows では全員の ``uv run`` が exe を消せず（os error 32）テストの前に落ちる。テスト・ベンチは
``tests/_server_cmd.py`` / ``bench/_stdio.py`` の ``server_argv``（``sys.executable -P -m lucianke``）で起こす。
ホストの設定 ``.mcp.json`` も同じ形（``uv run --directory . python -P -m lucianke``。``uv run ... lucianke`` ではない。
``--directory`` に絶対パスを焼き込むと、その置き先の無い PC では uv が os error 3 で落ちてつながらない。BUG-0126）。
ここは (1) その形を固定し、(2) tests/ と bench/ に ``uv run`` 経由の起動が戻ってきたら落とし、
(3) ``.mcp.json`` が ``lucianke.exe`` を握らない形で、``python -m lucianke`` の入口（``[project.scripts] lucianke`` と同じ ``main``）を指し続けることを確かめる
（縦切りの e2e が以前担っていた確認）。
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
from _server_cmd import server_argv  # noqa: E402

# 組み立てて書く（この見張り自身が検出されないように）
_FORBIDDEN = {
    "uv run ... lucianke": re.compile(r'"run"[^\n]*"' + "lucianke" + r'"'),
    "uv_argv の定義（廃止した名前。呼び出しは AttributeError で落ちる）": re.compile(
        r"\bdef uv" + r"_argv\b"
    ),
    "uv を command にして起こす": re.compile(r'command\s*=\s*"' + r'uv"'),
}
_SCAN_DIRS = ("tests", "bench")
_SKIP_PARTS = {"fixtures", "out", "results", "__pycache__"}


def test_server_argv_is_python_dash_m() -> None:
    argv = server_argv()
    assert argv[0] == sys.executable
    assert argv[-2:] == ["-m", "lucianke"] and "-P" in argv  # cwd（ワークスペース）を sys.path に足さない
    assert not any("uv" == Path(a).stem.lower() or a.lower().endswith("lucianke.exe") for a in argv)
    assert server_argv("doctor")[-1] == "doctor"


def test_no_test_or_bench_launches_the_server_through_uv_run() -> None:
    offenders: list[str] = []
    for d in _SCAN_DIRS:
        for path in sorted((REPO / d).rglob("*.py")):
            if _SKIP_PARTS & set(path.relative_to(REPO).parts) or path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for why, rx in _FORBIDDEN.items():
                for m in rx.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    offenders.append(f"{path.relative_to(REPO).as_posix()}:{line}: {why}")
    assert not offenders, "実プロセスのサーバーは server_argv() で起こす（BUG-0030）:\n" + "\n".join(
        offenders
    )


def test_bench_server_argv_matches_tests() -> None:
    sys.path.insert(0, str(REPO / "bench"))
    try:
        import _stdio
    finally:
        sys.path.remove(str(REPO / "bench"))
    assert _stdio.server_argv() == server_argv()


def test_host_config_launches_python_dash_m_not_the_console_script() -> None:
    """ホスト（``.mcp.json``）は ``uv run --directory . python -P -m lucianke``。``lucianke.exe``（コンソールスクリプト）を握らない。

    ``uv run`` の同期は ``pyproject.toml`` が変わると ``Scripts/lucianke.exe`` を消して作り直すが、握られていると Windows では
    消せない（os error 32）。``python`` 以降はテスト・ベンチの ``server_argv`` と同じ形（``-P`` で cwd を ``sys.path`` に足さない）。
    """
    spec = json.loads((REPO / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["lucianke"]
    args = spec["args"]
    assert spec["command"] == "uv" and args[0] == "run"
    assert args[1] == "--directory"
    # 作業ディレクトリは「.」（ホストがプロジェクトの根で起動する）。マシン固有の絶対パスを焼き込むと別の PC で起動に失敗し、
    # MCP がつながらない（2026-10-31: S:/work/develop/Lucianke が入っていて C: の作業ツリーで CONNECTION_CLOSED）
    assert args[2] == ".", f".mcp.json must not bake a machine-specific path: {args[2]!r}"
    assert args[3:] == [
        "python",
        "-P",
        "-m",
        "lucianke",
    ]  # コンソールスクリプトの ``lucianke`` を直接起こさない
    assert args[3:] == ["python", *server_argv()[1:]]  # テスト・ベンチと同じ形
    # ``python -m lucianke`` が実行する ``__main__`` は、コンソールスクリプトと同じ ``main`` を呼ぶ
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["lucianke"] == "lucianke.__main__:main"
    main_src = (REPO / "src" / "lucianke" / "__main__.py").read_text(encoding="utf-8")
    assert "def main(" in main_src and re.search(r'if __name__ == "__main__":', main_src)
