"""tests/conftest.py の赤い報告の記録（BUG-0088）。

出力の末尾だけを見ても（``| tail -3``）理由とファイルのパスが分かり、ファイルには全文と ``tmp_path`` の根が残ることを、
``tests/conftest.py`` を写した入れ子の pytest で確かめる。一時ディレクトリは ``tmp_path`` の中に向ける（本物の記録を汚さない）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "conftest.py"

SETUP_ERROR = """
import pytest


@pytest.fixture
def broken(tmp_path):
    raise OSError(f"shared thing vanished under {tmp_path}")


def test_a_name_long_enough_to_push_the_reason_out_of_an_eighty_column_summary_line(broken):
    pass


def test_green():
    pass
"""

GREEN = """
def test_green():
    pass
"""


def run(tmp_path: Path, source: str) -> tuple[list[str], Path]:
    (tmp_path / "pytest.ini").write_bytes(b"[pytest]\n")
    (tmp_path / "conftest.py").write_bytes(GUARD.read_bytes())
    (tmp_path / "test_it.py").write_bytes(source.encode("utf-8"))
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "PYTEST_DEBUG_TEMPROOT")}
    env.update(PYTHONUTF8="1", TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp))
    proc = subprocess.run(  # noqa: S603  固定の引数列
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "test_it.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,  # パイプなので要約の行は 80 桁で切られる
        timeout=300,
        check=False,
    )
    return proc.stdout.strip().splitlines(), temp / "lucianke-test-errors"


def test_a_setup_error_names_its_reason_and_log_in_the_last_lines(tmp_path: Path) -> None:
    lines, logs = run(tmp_path, SETUP_ERROR)
    tail = "\n".join(lines[-3:])
    assert "1 red report(s) written in full to" in lines[-1], tail
    assert "[setup]: OSError: shared thing vanished under" in lines[-1], tail
    (log,) = logs.glob("*.log")
    assert str(log) in lines[-1]
    text = log.read_text(encoding="utf-8")
    basetemp = next(line for line in text.splitlines() if line.startswith("basetemp="))
    assert str(tmp_path / "temp") in basetemp and "basetemp=None" not in basetemp, text
    assert (
        "test_a_name_long_enough_to_push_the_reason_out_of_an_eighty_column_summary_line [setup] failed"
        in text
    )
    assert "raise OSError" in text and "OSError: shared thing vanished under" in text


def test_a_green_run_writes_nothing(tmp_path: Path) -> None:
    lines, logs = run(tmp_path, GREEN)
    assert "red report" not in "\n".join(lines)
    assert not logs.exists()
