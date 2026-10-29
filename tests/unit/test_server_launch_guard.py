"""実プロセスのサーバーの起動を ``python -m lucianke`` に揃える見張り（BUG-0030）。

``uv run ... lucianke``（コンソールスクリプトの ``lucianke.exe``）で起こしたサーバーが動いている間に ``pyproject.toml``
が変わると、Windows では全員の ``uv run`` が exe を消せず（os error 32）テストの前に落ちる。テスト・ベンチは
``tests/_server_cmd.py`` / ``bench/_stdio.py`` の ``server_argv``（``sys.executable -m lucianke``）で起こす。
ここは (1) その形を固定し、(2) tests/ と bench/ に ``uv run`` 経由の起動が戻ってきたら落とし、
(3) ホストの設定 ``.mcp.json`` が ``lucianke`` のエントリーポイントを指し続けることを確かめる（縦切りの e2e が以前担っていた確認）。
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
    "uv_argv の定義（廃止した名前。呼び出しは AttributeError で落ちる）": re.compile(r"\bdef uv" + r"_argv\b"),
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
    assert not offenders, "実プロセスのサーバーは server_argv() で起こす（BUG-0030）:\n" + "\n".join(offenders)


def test_bench_server_argv_matches_tests() -> None:
    sys.path.insert(0, str(REPO / "bench"))
    try:
        import _stdio
    finally:
        sys.path.remove(str(REPO / "bench"))
    assert _stdio.server_argv() == server_argv()


def test_host_config_points_at_the_console_script_entry() -> None:
    """ホスト（``.mcp.json``）は ``uv run`` でコンソールスクリプトを起こす。エントリーポイントが ``python -m`` と同じ ``main`` であること。"""
    spec = json.loads((REPO / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["lucianke"]
    assert spec["command"] == "uv" and spec["args"][-1] == "lucianke"
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["lucianke"] == "lucianke.__main__:main"
