import subprocess
import sys

import lucianke
from lucianke.__main__ import main


def test_version_attr():
    assert lucianke.__version__


def test_cli_version_writes_nothing_to_stdout():
    proc = subprocess.run([sys.executable, "-m", "lucianke", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout == ""  # NFR-OPS-1: stdout は JSON-RPC 専用
    assert "lucianke" in proc.stderr


def test_unknown_subcommand():
    assert main(["no-such-subcommand"]) == 2
