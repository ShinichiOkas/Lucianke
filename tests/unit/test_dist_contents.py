"""配布物（wheel / sdist）が ``LICENSE`` と ``THIRD_PARTY_NOTICES.md`` を含むこと（仕様書 M1-8、L-7）。

* 設定の検査（常に走る）: ``pyproject.toml`` が ``license-files`` と sdist の許可リストでそれらを保証している。
* 検査器の陽性対照（常に走る）: 合成した wheel / sdist で、欠けや食い違い・作業用ファイルの混入を拾える。
* 実ビルド（``LUCIANKE_TEST_BUILD=1`` のときだけ。``uv build`` は 20 秒ほどかかり、初回はネットワークが要る）。
  CI の license ジョブは毎回 ``uv build`` → ``tools/check_dist.py dist`` を走らせる。
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


check_dist = _load("check_dist")

LICENSE_TEXT = "MIT License\n\nCopyright (c) test\n"
NOTICES_TEXT = "# Third-Party Notices\n\n| a | 1 |\n"


def make_wheel(
    path: Path, *, license_file: bool = True, notices: str | None = NOTICES_TEXT, metadata_files: bool = True
) -> Path:
    info = "lucianke-0.0.1.dist-info"
    lines = ["Metadata-Version: 2.5", "Name: lucianke", "License-Expression: MIT"]
    if metadata_files:
        lines += ["License-File: LICENSE", "License-File: THIRD_PARTY_NOTICES.md"]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("lucianke/__init__.py", "")
        z.writestr(f"{info}/METADATA", "\n".join(lines) + "\n")
        if license_file:
            z.writestr(f"{info}/licenses/LICENSE", LICENSE_TEXT)
        if notices is not None:
            z.writestr(f"{info}/licenses/THIRD_PARTY_NOTICES.md", notices)
    return path


def make_sdist(path: Path, files: list[str]) -> Path:
    with tarfile.open(path, "w:gz") as t:
        for name in files:
            data = b"x"
            ti = tarfile.TarInfo(f"lucianke-0.0.1/{name}")
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return path


def wheel_problems(path: Path) -> list[str]:
    return check_dist.check_wheel(path, notices=NOTICES_TEXT, license_text=LICENSE_TEXT)


# --- 設定 -------------------------------------------------------------------------------


def test_pyproject_guarantees_license_files_in_wheel_and_sdist():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["project"]["license"] == "MIT"
    assert {"LICENSE", "THIRD_PARTY_NOTICES.md"} <= set(cfg["project"]["license-files"])
    sdist = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert {"/LICENSE", "/THIRD_PARTY_NOTICES.md", "/src", "/pyproject.toml"} <= set(sdist)
    assert (ROOT / "LICENSE").is_file() and (ROOT / "THIRD_PARTY_NOTICES.md").is_file()


def test_ci_license_job_builds_and_checks_the_distribution():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "tools/gen_notices.py --check" in ci
    assert "uv build" in ci and "tools/check_dist.py" in ci


# --- 検査器の陽性対照 -------------------------------------------------------------------


def test_good_wheel_passes(tmp_path: Path):
    assert wheel_problems(make_wheel(tmp_path / "ok.whl")) == []


def test_wheel_without_notices_fails(tmp_path: Path):
    problems = wheel_problems(make_wheel(tmp_path / "w.whl", notices=None))
    assert any("THIRD_PARTY_NOTICES.md is missing" in p for p in problems)


def test_wheel_without_license_fails(tmp_path: Path):
    problems = wheel_problems(make_wheel(tmp_path / "w.whl", license_file=False))
    assert any("licenses/LICENSE is missing" in p for p in problems)


def test_wheel_with_stale_notices_fails(tmp_path: Path):
    problems = wheel_problems(make_wheel(tmp_path / "w.whl", notices="# old\n"))
    assert any("differs from the repository file" in p for p in problems)


def test_wheel_metadata_must_declare_license_files(tmp_path: Path):
    problems = wheel_problems(make_wheel(tmp_path / "w.whl", metadata_files=False))
    assert any("License-File: THIRD_PARTY_NOTICES.md" in p for p in problems)


def test_good_sdist_passes(tmp_path: Path):
    sdist = make_sdist(
        tmp_path / "ok.tar.gz", ["LICENSE", "THIRD_PARTY_NOTICES.md", "src/lucianke/__init__.py"]
    )
    assert check_dist.check_sdist(sdist) == []


def test_sdist_missing_notices_fails(tmp_path: Path):
    problems = check_dist.check_sdist(make_sdist(tmp_path / "s.tar.gz", ["LICENSE", "src/x.py"]))
    assert any("THIRD_PARTY_NOTICES.md is missing" in p for p in problems)


def test_sdist_with_working_files_fails(tmp_path: Path):
    files = ["LICENSE", "THIRD_PARTY_NOTICES.md", ".claude/settings.json", ".pair-agent/x.json", ".mcp.json"]
    problems = check_dist.check_sdist(make_sdist(tmp_path / "s.tar.gz", files))
    assert any("working files" in p for p in problems)


def test_empty_dist_dir_fails(tmp_path: Path):
    assert check_dist.check_dir(tmp_path)


# --- 実ビルド（任意） -------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("LUCIANKE_TEST_BUILD") != "1" or shutil.which("uv") is None,
    reason="set LUCIANKE_TEST_BUILD=1 (needs uv and takes ~20 s); CI's license job always builds",
)
def test_real_build_carries_license_and_notices(tmp_path: Path):
    subprocess.run(["uv", "build", "--out-dir", str(tmp_path)], cwd=ROOT, check=True, capture_output=True)  # noqa: S603, S607
    assert check_dist.check_dir(tmp_path) == []
