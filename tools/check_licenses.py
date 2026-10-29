"""依存木のライセンス検査（仕様書第 16 章 L-2〜L-4、L-8）。

pip-licenses の出力を許可リストと照合する。
- 寛容（MIT, BSD, Apache-2.0, ISC, Unlicense, CC0, UPL, PSF, Public Domain, MPL 以外の弱い条件なし）: OK
- デュアルで寛容側を選べるもの: OK（選んだ側を DUAL_CHOICES に記録）
- 弱いコピーレフト（MPL, LGPL, EPL）: 推移的依存で同梱しない場合は WARN、直接依存なら FAIL
- 強いコピーレフト（GPL, AGPL）: FAIL
- 判定不能: FAIL（OVERRIDES に一次情報で確認した結果を書いて解消する）
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PERMISSIVE = [
    r"\bMIT\b", r"\bBSD\b", r"Apache", r"\bISC\b", r"Unlicense", r"\bCC0\b", r"\bUPL\b",
    r"Python Software Foundation", r"\bPSF\b", r"Public Domain", r"\bZlib\b", r"\b0BSD\b", r"Historical Permission",
]
WEAK = [r"\bMPL\b", r"Mozilla Public", r"\bLGPL\b", r"Lesser General", r"\bEPL\b", r"Eclipse Public"]
STRONG = [r"(?<!L)GPL", r"(?<!Lesser )General Public License", r"\bAGPL\b", r"Affero"]

# 一次情報で確認したデュアルライセンスの選択（仕様書 §16.2、付録 D）
DUAL_CHOICES = {
    "dulwich": "Apache-2.0（Apache-2.0 OR GPL-2.0-or-later のうち寛容側を選ぶ）",
}
# メタデータが不完全で、一次情報で確認したもの: name -> SPDX
OVERRIDES: dict[str, str] = {}
# 検査対象外（自分自身と、ビルドにだけ使うもの）
IGNORE = {"lucianke", "pip-licenses", "prettytable", "wcwidth", "tomli"}


def _match(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify(name: str, license_text: str) -> str:
    if name in DUAL_CHOICES:
        return "ok"
    text = OVERRIDES.get(name, license_text)
    if not text or text.strip().upper() in {"UNKNOWN", ""}:
        return "unknown"
    alts = re.split(r"\s+OR\s+|;\s*|\s*/\s*", text)
    if any(_match(PERMISSIVE, a) and not _match(STRONG + WEAK, a) for a in alts):
        return "ok"
    if _match(STRONG, text) and not _match(WEAK, text):
        return "strong"
    if _match(WEAK, text):
        return "weak"
    return "unknown"


def direct_dependencies() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = set()
    deps = list(data["project"]["dependencies"])
    for extra in data["project"].get("optional-dependencies", {}).values():
        deps += [d for d in extra if not d.startswith("lucianke")]
    for d in deps:
        names.add(re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0].lower().replace("_", "-"))
    return names


def main() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json", "--from=mixed", "--with-urls"],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = json.loads(out)
    direct = direct_dependencies()
    fail, warn = [], []
    for row in rows:
        name = row["Name"]
        key = name.lower().replace("_", "-")
        if key in IGNORE:
            continue
        verdict = classify(key, row.get("License", ""))
        line = f"{name} {row.get('Version', '')}: {row.get('License', '')}"
        if verdict == "ok":
            continue
        if verdict == "weak" and key not in direct:
            warn.append(line + "  [弱いコピーレフト・推移的依存・同梱しない]")
        else:
            fail.append(line + f"  [{verdict}{'・直接依存' if key in direct else ''}]")
    for w in warn:
        print("WARN", w)
    for f in fail:
        print("FAIL", f)
    print(f"checked {len(rows)} packages: {len(fail)} fail, {len(warn)} warn")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
