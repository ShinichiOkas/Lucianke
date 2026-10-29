"""ベンチのフィクスチャを bench/.cache/<name>/ に SHA 固定で浅く clone する（仕様書 §14.2）。

使い方:
    uv run --no-project python bench/fetch_fixtures.py            # fixtures.toml の全件
    uv run --no-project python bench/fetch_fixtures.py F-PY       # 1 件だけ
    uv run --no-project python bench/fetch_fixtures.py --install  # install コマンド（npm ci など）も実行
    uv run --no-project python bench/fetch_fixtures.py --list     # 取得状況だけ表示

標準ライブラリだけで動く（tomllib は 3.11 以降）。既に固定 SHA が checkout 済みなら何もしない。
取得は公開リポジトリの clone だけで、外部へ送るものは無い。
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

BENCH = Path(__file__).resolve().parent
CACHE = BENCH / ".cache"
TOML = BENCH / "fixtures.toml"


def load_fixtures() -> dict[str, dict[str, object]]:
    data = tomllib.loads(TOML.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if k != "meta" and isinstance(v, dict)}


def fixture_dir(fx: dict[str, object]) -> Path:
    return CACHE / str(fx["name"])


def git(args: list[str], cwd: Path | None = None) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def head_sha(d: Path) -> str | None:
    if not (d / ".git").exists():
        return None
    try:
        return git(["rev-parse", "HEAD"], cwd=d)
    except RuntimeError:
        return None


def fetch(fx: dict[str, object]) -> Path:
    d = fixture_dir(fx)
    sha = str(fx["sha"])
    if head_sha(d) == sha:
        return d
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").exists():
        git(["init", "-q"], cwd=d)
        git(["remote", "add", "origin", str(fx["url"])], cwd=d)
    git(["fetch", "-q", "--depth", "1", "origin", sha], cwd=d)
    git(["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=d)
    got = head_sha(d)
    if got != sha:
        raise RuntimeError(f"{fx['name']}: expected {sha}, got {got}")
    return d


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="F-PY / F-TS など（省略で全件）")
    ap.add_argument("--install", action="store_true", help="fixtures.toml の install コマンドも実行する")
    ap.add_argument("--list", action="store_true", help="取得状況だけ表示する")
    args = ap.parse_args(argv)
    fxs = load_fixtures()
    names = args.names or list(fxs)
    unknown = [n for n in names if n not in fxs]
    if unknown:
        print(f"unknown fixture: {unknown}（候補: {list(fxs)}）", file=sys.stderr)
        return 2
    for n in names:
        fx = fxs[n]
        d = fixture_dir(fx)
        if args.list:
            state = "ok" if head_sha(d) == fx["sha"] else "missing"
            print(f"{n}\t{fx['name']}\t{fx['sha'][:10]}\t{state}\t{d}")  # type: ignore[index]
            continue
        d = fetch(fx)
        print(f"{n}\t{fx['name']}\t{str(fx['sha'])[:10]}\t{d}")
        install = str(fx.get("install", "")).strip()
        if args.install and install:
            print(f"  install: {install}")
            r = subprocess.run(shlex.split(install), cwd=d, shell=sys.platform == "win32", check=False)
            if r.returncode != 0:
                print(f"  install failed ({r.returncode})", file=sys.stderr)
                return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
