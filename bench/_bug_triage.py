"""bugs/ の全票を git log と突き合わせ、解消コミットの有無で未処理を洗い出す。

判定の根拠は「コミットメッセージに BUG-00xx が現れるか」だけ（票に状態欄は無い。HANDOFF の方針）。
見たことだけ: コミットの有無と件数・直近の subject を出す。診断はしない。

Run: PYTHONUTF8=1 python bench/_bug_triage.py
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUGS = REPO / "bugs"


def git(args: list[str]) -> str:
    p = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=False
    )
    return p.stdout


def main() -> int:
    ids = sorted(
        {m.group(0) for f in BUGS.glob("BUG-*.md") if (m := re.match(r"BUG-\d{4}", f.name))}
    )
    log = git(["log", "--oneline", "--no-decorate"])
    unresolved: list[str] = []
    resolved: list[tuple[str, int, str]] = []
    for bug in ids:
        hits = [ln for ln in log.splitlines() if bug in ln]
        if hits:
            resolved.append((bug, len(hits), hits[0]))
        else:
            unresolved.append(bug)
    print(f"total={len(ids)} resolved={len(resolved)} unresolved={len(unresolved)}")
    print("\n--- UNRESOLVED (no commit mentions this BUG) ---")
    for bug in unresolved:
        title = ""
        for f in BUGS.glob(f"{bug}-*.md"):
            title = f.name[len(bug) + 1 : -3]
            break
        print(f"{bug}\t{title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
