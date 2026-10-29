"""配布物（wheel / sdist）の中身の検査（仕様書 §15.2 M1-8、第 16 章 L-1・L-7）。

    uv build --out-dir dist
    python tools/check_dist.py dist          # dist/ の wheel と sdist を検査する
    python tools/check_dist.py --build       # 一時ディレクトリで uv build してから検査する

検査すること:

* wheel: ``*.dist-info/licenses/LICENSE``（MIT 全文）と ``*.dist-info/licenses/THIRD_PARTY_NOTICES.md`` が入っている。
  後者はリポジトリの ``THIRD_PARTY_NOTICES.md`` と一致する（古い生成物を配らない）。``METADATA`` に ``License-Expression: MIT`` と
  両方の ``License-File`` がある。
* sdist: 直下に ``LICENSE`` と ``THIRD_PARTY_NOTICES.md`` がある。作業用ファイル（``.claude`` ``.pair-agent`` ``.mcp.json``）を含まない。
"""

from __future__ import annotations

import argparse
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSE_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md")
PRIVATE_NAMES = (".claude", ".pair-agent", ".mcp.json")


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def check_wheel(path: Path, *, notices: str, license_text: str) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        dist_infos = sorted({n.split("/", 1)[0] for n in names if n.split("/", 1)[0].endswith(".dist-info")})
        if len(dist_infos) != 1:
            return [f"{path.name}: expected exactly one .dist-info, found {dist_infos}"]
        info = dist_infos[0]
        for name in LICENSE_FILES:
            if f"{info}/licenses/{name}" not in names:
                problems.append(f"{path.name}: {info}/licenses/{name} is missing")
        if f"{info}/licenses/LICENSE" in names:
            body = _norm(z.read(f"{info}/licenses/LICENSE").decode("utf-8"))
            if body != _norm(license_text) or "MIT License" not in body:
                problems.append(
                    f"{path.name}: licenses/LICENSE differs from the repository LICENSE (or is not MIT)"
                )
        if f"{info}/licenses/THIRD_PARTY_NOTICES.md" in names:
            body = _norm(z.read(f"{info}/licenses/THIRD_PARTY_NOTICES.md").decode("utf-8"))
            if body != _norm(notices):
                problems.append(
                    f"{path.name}: licenses/THIRD_PARTY_NOTICES.md differs from the repository file"
                )
        meta = z.read(f"{info}/METADATA").decode("utf-8") if f"{info}/METADATA" in names else ""
        for line in ("License-Expression: MIT", *(f"License-File: {n}" for n in LICENSE_FILES)):
            if line not in meta.splitlines():
                problems.append(f"{path.name}: METADATA lacks {line!r}")
    return problems


def check_sdist(path: Path) -> list[str]:
    problems: list[str] = []
    with tarfile.open(path) as t:
        names = t.getnames()
    roots = {n.split("/", 1)[0] for n in names}
    if len(roots) != 1:
        return [f"{path.name}: expected a single top-level directory, found {sorted(roots)}"]
    top = roots.pop()
    for name in LICENSE_FILES:
        if f"{top}/{name}" not in names:
            problems.append(f"{path.name}: {top}/{name} is missing")
    leaked = sorted(n for n in names if any(part in PRIVATE_NAMES for part in n.split("/")))
    if leaked:
        problems.append(f"{path.name}: working files must not be distributed: {leaked[:5]}")
    return problems


def check_dir(dist: Path) -> list[str]:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if not wheels:
        return [f"no wheel in {dist}"]
    if not sdists:
        return [f"no sdist in {dist}"]
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    problems: list[str] = []
    for w in wheels:
        problems += check_wheel(w, notices=notices, license_text=license_text)
    for s in sdists:
        problems += check_sdist(s)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that wheel and sdist carry LICENSE and THIRD_PARTY_NOTICES.md."
    )
    parser.add_argument("dist", nargs="?", type=Path, help="directory with the built wheel and sdist")
    parser.add_argument(
        "--build", action="store_true", help="run `uv build` into a temporary directory first"
    )
    args = parser.parse_args(argv)
    if args.build:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["uv", "build", "--out-dir", tmp], cwd=ROOT, check=True)  # noqa: S603, S607
            problems = check_dir(Path(tmp))
    elif args.dist is not None:
        problems = check_dir(args.dist)
    else:
        parser.error("give a dist directory or --build")
    for p in problems:
        print("FAIL", p)
    if not problems:
        print("dist OK: wheel and sdist carry LICENSE and THIRD_PARTY_NOTICES.md")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
