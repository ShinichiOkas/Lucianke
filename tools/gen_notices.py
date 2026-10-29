"""``THIRD_PARTY_NOTICES.md`` の生成器（仕様書 §16.1 L-7・§16.3、設計書 §10.5）。

    python tools/gen_notices.py           # THIRD_PARTY_NOTICES.md を書き出す
    python tools/gen_notices.py --check   # 生成物が最新か検査する（古ければ 1）

* **対象** は ``uv.lock`` の依存木: 本体の ``dependencies`` と、extras（``all`` を除く）ごとの推移的閉包。dev 依存は含めない。
  依存の条件（``sys_platform`` / Python の版）は、Windows・Linux・macOS × Python 3.12〜3.14 のどれかで要るものをすべて含める
  （どの OS で生成しても同じ内容になる）。
* **ライセンス情報** は導入済みの配布情報から ``pip-licenses`` で読む（ネットワーク不要）。この環境に入らない依存
  （``STATIC_ENTRIES``: 例 Windows 専用の pywin32）は、環境に依らず同じ内容を出すため表の値だけを出す。
* デュアルライセンスの選択は ``check_licenses.DUAL_CHOICES`` を使う（検査器と表示の食い違いを防ぐ）。

足りないものがあれば ``generate()`` は ``problems`` に理由を返す（この環境に導入されていない、版が ``uv.lock`` と違う）。
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "uv.lock"
OUT = ROOT / "THIRD_PARTY_NOTICES.md"

# 依存の条件を評価する環境（どれかで要るなら含める）。
TARGET_ENVS: list[dict[str, str]] = [
    {
        "sys_platform": plat,
        "platform_system": system,
        "os_name": os_name,
        "python_version": f"3.{minor}",
        "python_full_version": f"3.{minor}.0",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }
    for plat, system, os_name in (
        ("win32", "Windows", "nt"),
        ("linux", "Linux", "posix"),
        ("darwin", "Darwin", "posix"),
    )
    for minor in (12, 13, 14)
]

# pip-licenses が返す分類子由来の表記 → SPDX（曖昧なもの "BSD License" は載せない。そのまま出す）。
SPDX_MAP = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "Python Software Foundation License": "PSF-2.0",
}


@dataclass(frozen=True)
class Static:
    """この環境に配布物が無くても同じ内容を出すための固定値（プラットフォーム専用の依存）。"""

    spdx: str
    url: str
    note: str


STATIC_ENTRIES: dict[str, Static] = {
    "pywin32": Static(
        spdx="PSF-2.0",
        url="https://github.com/mhammond/pywin32",
        note="Windows 専用（mcp が ``sys_platform == 'win32'`` で要求する）。ライセンス本文は Windows 以外の環境に配布物が"
        "無く、どの環境で生成しても同じ内容にするため転記しない。上の URL を参照。",
    ),
}

# wheel が他のソフトウェアを同梱するもの（仕様書 §16.2、付録 D）。個別確認の状況もそのまま書く。
BUNDLED_NOTES: dict[str, str] = {
    "lxml": "wheel は libxml2 / libxslt（MIT 系）などを静的に同梱する。同梱物の個別確認は未了（仕様書 付録 D）。",
    "nodejs-wheel-binaries": "wheel は Node.js の実行ファイル（MIT）を同梱する。Node.js の LICENSE には V8・OpenSSL など"
    "同梱物の表示が含まれる: https://github.com/nodejs/node/blob/main/LICENSE",
    "z3-solver": "wheel は Z3 のネイティブライブラリ（MIT）を同梱する。配布物にライセンス本文が無く、メタデータの宣言は MIT。"
    "本文: https://github.com/Z3Prover/z3/blob/master/LICENSE.txt",
    "ast-grep-py": "wheel は ast-grep のネイティブ拡張（MIT）を同梱する。配布物にライセンス本文が無く、メタデータの宣言は MIT。"
    "本文: https://github.com/ast-grep/ast-grep/blob/main/LICENSE",
    "cryptography": "wheel は OpenSSL（Apache-2.0）などを静的に同梱する。pyjwt[crypto] 経由の推移的依存。",
}


def _load_check_licenses() -> Any:
    spec = importlib.util.spec_from_file_location("check_licenses", ROOT / "tools" / "check_licenses.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --- 依存の閉包（uv.lock） --------------------------------------------------------------


def parse_lock(path: Path = LOCK) -> dict[str, dict[str, Any]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {norm(p["name"]): p for p in data["package"]}


def _marker_holds(marker: str | None, cache: dict[str, bool]) -> bool:
    if not marker:
        return True
    if marker not in cache:
        from packaging.markers import Marker

        m = Marker(marker)
        cache[marker] = any(m.evaluate({**env, "extra": ""}) for env in TARGET_ENVS)
    return cache[marker]


def closure(packages: dict[str, dict[str, Any]], starts: list[dict[str, Any]]) -> dict[str, str]:
    """``starts``（lock の依存項目）から、条件を満たす辺だけたどった閉包。 ``{正規化名: 版}``。"""
    cache: dict[str, bool] = {}
    seen: set[tuple[str, tuple[str, ...]]] = set()
    result: dict[str, str] = {}
    stack = list(starts)
    while stack:
        dep = stack.pop()
        if not _marker_holds(dep.get("marker"), cache):
            continue
        key = norm(dep["name"])
        extras = tuple(sorted(dep.get("extra", [])))
        if (key, extras) in seen:
            continue
        seen.add((key, extras))
        pkg = packages[key]
        result[key] = pkg["version"]
        stack.extend(pkg.get("dependencies", []))
        optional = pkg.get("optional-dependencies", {})
        for extra in extras:
            stack.extend(optional.get(extra, []))
    return result


def lock_scopes(packages: dict[str, dict[str, Any]] | None = None) -> dict[str, tuple[str, str]]:
    """``{正規化名: (版, 提供元)}``。提供元は ``core`` または ``extra: langs, python`` のように extras 名。"""
    packages = packages if packages is not None else parse_lock()
    root = packages["lucianke"]
    base = closure(packages, root.get("dependencies", []))
    scopes: dict[str, tuple[str, str]] = {k: (v, "core") for k, v in base.items()}
    in_extras: dict[str, list[str]] = {}
    versions: dict[str, str] = {}
    for extra, deps in sorted(root.get("optional-dependencies", {}).items()):
        if extra == "all":
            continue
        for key, version in closure(packages, [d for d in deps if norm(d["name"]) != "lucianke"]).items():
            if key not in base:
                in_extras.setdefault(key, []).append(extra)
                versions[key] = version
    for key, extras in in_extras.items():
        scopes[key] = (versions[key], "extra: " + ", ".join(extras))
    return scopes


# --- ライセンス情報（pip-licenses） -----------------------------------------------------


def piplicenses_rows() -> dict[str, dict[str, str]]:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    cp = subprocess.run(  # noqa: S603  自分の Python で pip-licenses を走らせる（シェルを介さない）
        [
            sys.executable, "-m", "piplicenses", "--format=json", "--from=mixed", "--with-urls",
            "--with-license-file", "--no-license-path", "--with-notice-file",
        ],  # fmt: skip
        capture_output=True, check=True, env=env, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    return {norm(r["Name"]): r for r in json.loads(cp.stdout.decode("utf-8"))}


def clean_text(text: str | None) -> str | None:
    """本文の改行を LF にし、行末の空白と前後の空行を落とす。無い（``UNKNOWN``）は ``None``。"""
    if text is None or text.strip().upper() in {"", "UNKNOWN"}:
        return None
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


@dataclass
class Entry:
    key: str
    name: str
    version: str
    scope: str
    license: str  # 報告された表記
    spdx: str  # 表示用（デュアルの選択を反映）
    url: str
    text: str | None = None
    notice: str | None = None
    note: str | None = None
    choice: str | None = None
    bundled: str | None = None


@dataclass
class Result:
    text: str
    entries: list[Entry] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def build_entries(
    scopes: dict[str, tuple[str, str]], rows: dict[str, dict[str, str]], names: dict[str, str]
) -> tuple[list[Entry], list[str]]:
    dual = _load_check_licenses().DUAL_CHOICES
    entries: list[Entry] = []
    problems: list[str] = []
    for key, (version, scope) in sorted(scopes.items()):
        display = names.get(key, key)
        if key in STATIC_ENTRIES:
            s = STATIC_ENTRIES[key]
            entries.append(Entry(key, display, version, scope, s.spdx, s.spdx, s.url, note=s.note))
            continue
        row = rows.get(key)
        if row is None:
            problems.append(f"{display} {version}: この環境に導入されていない（uv sync --all-extras）")
            continue
        if row["Version"] != version:
            problems.append(
                f"{display}: 導入済み {row['Version']} が uv.lock の {version} と違う（uv sync --all-extras）"
            )
            continue
        reported = row["License"]
        spdx = SPDX_MAP.get(reported, reported)
        choice = None
        if key in dual:
            choice = dual[key]
            spdx = choice.split("（", 1)[0].strip()
        url = row.get("URL") or ""
        if not url or url.upper() == "UNKNOWN":
            url = f"https://pypi.org/project/{display}/{version}/"
        entries.append(
            Entry(
                key,
                display,
                version,
                scope,
                reported,
                spdx,
                url,
                text=clean_text(row.get("LicenseText")),
                notice=clean_text(row.get("NoticeText")),
                choice=choice,
                bundled=BUNDLED_NOTES.get(key),
            )  # fmt: skip
        )
    for e in entries:
        if e.key in STATIC_ENTRIES and e.key in BUNDLED_NOTES:
            e.bundled = BUNDLED_NOTES[e.key]
    return entries, problems


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render(entries: list[Entry]) -> str:
    entries = sorted(entries, key=lambda e: e.name.lower())
    core = sum(1 for e in entries if e.scope == "core")
    counts = Counter(e.spdx for e in entries)
    out: list[str] = [
        "# Third-Party Notices",
        "",
        "Lucianke 本体は MIT License（`LICENSE`）です。このファイルは、Lucianke が実行時に依存する第三者ソフトウェアの"
        "名前・版・ライセンス・確認 URL・ライセンス本文を示します（仕様書 第 16 章 L-7）。",
        "",
        "**このファイルは生成物です。手で編集しないでください。** `python tools/gen_notices.py` が `uv.lock` と"
        "導入済みの配布情報から生成し、CI が最新であることを検査します（`python tools/gen_notices.py --check`）。",
        "",
        "- 対象: `uv.lock` の依存木。本体の `dependencies`（core）と、extras（`langs` `python` `edit` `verify` `xml`）の"
        "推移的依存。開発用の依存（pytest・pip-licenses など）は含みません。"
        "Windows・Linux・macOS と Python 3.12〜3.14 のどれかで要るものをすべて載せています。",
        f"- 件数: {len(entries)} パッケージ（core {core}、extras のみ {len(entries) - core}）。",
        "- ライセンスの内訳: "
        + "、".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        + "。",
        "- デュアルライセンスは寛容側を選んでいます（下の表）。",
        "- Apache-2.0 の NOTICE: "
        + (
            "`NOTICE:` として各項に転記しています。"
            if any(e.notice for e in entries)
            else "NOTICE ファイルを持つ依存は、配布情報に見つかりませんでした。"
        ),
        "",
        "## 一覧",
        "",
        "| Package | Version | License (SPDX) | Provided by | URL |",
        "|---|---|---|---|---|",
    ]
    for e in entries:
        out.append(f"| {_cell(e.name)} | {e.version} | {_cell(e.spdx)} | {_cell(e.scope)} | {_cell(e.url)} |")

    choices = [e for e in entries if e.choice]
    if choices:
        out += ["", "## デュアルライセンスの選択", ""]
        out += [f"- **{e.name}** {e.version}: 報告された表記は `{e.license}`。{e.choice}。" for e in choices]

    bundled = [e for e in entries if e.bundled]
    if bundled:
        out += ["", "## wheel が同梱する他のソフトウェア", ""]
        out += [f"- **{e.name}** {e.version}: {e.bundled}" for e in bundled]

    out += ["", "## ライセンス本文", ""]
    for e in entries:
        out += [
            f"### {e.name} {e.version}",
            "",
            f"- License: {e.license}" + (f"（{e.spdx} を選択）" if e.choice else ""),
            f"- URL: {e.url}",
            f"- Provided by: {e.scope}",
        ]
        if e.note:
            out += ["", e.note]
        if e.notice:
            fence = _fence(e.notice)
            out += ["", "NOTICE:", "", fence, e.notice, fence]
        if e.text:
            fence = _fence(e.text)
            out += ["", fence, e.text, fence]
        elif not e.note:
            out += ["", "配布物にライセンス本文が含まれていません。上の URL を参照してください。"]
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def generate() -> Result:
    packages = parse_lock()
    scopes = lock_scopes(packages)
    names = {k: p["name"] for k, p in packages.items()}
    entries, problems = build_entries(scopes, piplicenses_rows(), names)
    return Result(render(entries) if not problems else "", entries, problems)


def diff_summary(expected: str, actual: str, limit: int = 30) -> list[str]:
    diff = difflib.unified_diff(
        actual.splitlines(),
        expected.splitlines(),
        "THIRD_PARTY_NOTICES.md (commit)",
        "generated",
        lineterm="",
        n=0,
    )
    return list(diff)[:limit]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate THIRD_PARTY_NOTICES.md from uv.lock and installed metadata."
    )
    parser.add_argument(
        "--check", action="store_true", help="fail (exit 1) if the committed file is not up to date"
    )
    args = parser.parse_args(argv)
    result = generate()
    if result.problems:
        print("cannot generate THIRD_PARTY_NOTICES.md in this environment:", file=sys.stderr)
        for p in result.problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current == result.text:
            print(f"THIRD_PARTY_NOTICES.md is up to date ({len(result.entries)} packages)")
            return 0
        print("THIRD_PARTY_NOTICES.md is stale. Run: uv run python tools/gen_notices.py", file=sys.stderr)
        for line in diff_summary(result.text, current):
            print(line, file=sys.stderr)
        return 1
    OUT.write_text(result.text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.name}: {len(result.entries)} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
