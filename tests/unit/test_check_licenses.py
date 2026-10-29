"""ライセンス検査器の陽性対照（違反を本当に拾えるか）。"""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_licenses", Path(__file__).resolve().parents[2] / "tools" / "check_licenses.py"
)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_permissive_ok():
    for text in ["MIT", "MIT License", "BSD-3-Clause", "Apache Software License", "Apache-2.0 WITH LLVM-exception",
                 "ISC", "Unlicense OR MIT", "CC0-1.0", "Python Software Foundation License"]:
        assert mod.classify("x", text) == "ok", text


def test_strong_copyleft_fails():
    for text in ["GPL-2.0-only", "GNU General Public License v3 (GPLv3)", "AGPL-3.0"]:
        assert mod.classify("x", text) == "strong", text


def test_weak_copyleft_detected():
    for text in ["MPL-2.0", "Mozilla Public License 2.0 (MPL 2.0)", "LGPL-3.0", "EPL-2.0"]:
        assert mod.classify("x", text) == "weak", text


def test_dual_with_permissive_ok():
    assert mod.classify("x", "Apache-2.0 OR GPL-2.0-or-later") == "ok"
    assert mod.classify("dulwich", "GPL-2.0-or-later") == "ok"  # DUAL_CHOICES で寛容側を選択済み


def test_unknown_is_not_ok():
    assert mod.classify("x", "UNKNOWN") == "unknown"
    assert mod.classify("x", "") == "unknown"
