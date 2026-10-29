"""``THIRD_PARTY_NOTICES.md`` が生成物として最新であること（仕様書 §16.1 L-7・§16.3、設計書 §10.5）。

ネットワーク不要: ``uv.lock`` の依存木と、導入済みの配布情報（``pip-licenses``）だけを使う。

* 依存の集合（名前と版）は ``uv.lock`` だけから決まるので、どの OS・どの extras の環境でも検査する。
* 本文まで含む完全一致は、閉包の全部がこの環境に入っているとき（``uv sync --all-extras``）だけ検査する。
  足りなければ skip する（CI の license ジョブは ``--all-extras`` で ``gen_notices.py --check`` も走らせる）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass が自分のモジュールを引くため
    spec.loader.exec_module(mod)
    return mod


gen = _load("gen_notices")
check_licenses = _load("check_licenses")

NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
DEV_ONLY = {
    "pytest",
    "pytest-asyncio",
    "pluggy",
    "iniconfig",
    "pygments",
    "colorama",
    "pip-licenses",
    "prettytable",
}


def _table_rows(text: str) -> dict[str, tuple[str, str, str]]:
    """一覧の表 → ``{正規化名: (版, SPDX, 提供元)}``。"""
    rows: dict[str, tuple[str, str, str]] = {}
    for m in re.finditer(
        r"^\| ([^|\n]+) \| ([^|\n]+) \| ([^|\n]+) \| ([^|\n]+) \| [^\n]+ \|$", text, re.MULTILINE
    ):
        name, version, spdx, scope = (g.strip() for g in m.groups())
        if name in {"Package", "---"}:
            continue
        rows[gen.norm(name)] = (version, spdx, scope)
    return rows


@pytest.fixture(scope="module")
def committed() -> str:
    assert NOTICES.exists(), "THIRD_PARTY_NOTICES.md is missing: run `uv run python tools/gen_notices.py`"
    return NOTICES.read_text(encoding="utf-8")


def test_notices_list_exactly_the_lock_closure(committed: str):
    expected = {k: v for k, (v, _scope) in gen.lock_scopes().items()}
    actual = {k: v[0] for k, v in _table_rows(committed).items()}
    assert actual == expected, "stale THIRD_PARTY_NOTICES.md: run `uv run python tools/gen_notices.py`"


def test_notices_exclude_dev_dependencies(committed: str):
    leaked = DEV_ONLY & set(_table_rows(committed))
    assert not leaked, f"dev-only packages must not be listed: {sorted(leaked)}"


def test_notices_record_scope_of_extras(committed: str):
    rows = _table_rows(committed)
    assert rows["mcp"][2] == "core"
    assert rows["ruff"][2] == "extra: python"
    assert rows["tree-sitter-rust"][2] == "extra: langs"
    assert rows["lxml"][2] == "extra: xml"


def test_dulwich_is_listed_with_the_permissive_choice(committed: str):
    version, spdx, _scope = _table_rows(committed)["dulwich"]
    assert spdx == "Apache-2.0"
    assert "## デュアルライセンスの選択" in committed
    assert re.search(r"\*\*dulwich\*\* \S+: .*Apache-2\.0", committed)


def test_no_strong_or_unknown_license_in_notices(committed: str):
    for name, (_version, spdx, _scope) in _table_rows(committed).items():
        verdict = check_licenses.classify(name, spdx)
        assert verdict == "ok", f"{name}: {spdx!r} classified as {verdict}"


def test_every_entry_has_a_license_text_or_an_explicit_note(committed: str):
    sections = re.split(r"^### ", committed.split("## ライセンス本文", 1)[1], flags=re.MULTILINE)[1:]
    assert len(sections) == len(_table_rows(committed))
    for section in sections:
        title = section.splitlines()[0]
        has_text = "\n```" in section
        has_note = "配布物にライセンス本文が含まれていません" in section or "転記しない" in section
        assert has_text or has_note, f"{title}: neither license text nor a note"


def test_committed_file_matches_regeneration(committed: str):
    result = gen.generate()
    if result.problems:
        pytest.skip("environment lacks part of the dependency closure: " + "; ".join(result.problems[:3]))
    assert committed == result.text, "stale THIRD_PARTY_NOTICES.md:\n" + "\n".join(
        gen.diff_summary(result.text, committed)
    )


def test_staleness_is_detected():
    """陽性対照: 内容が 1 文字でも違えば差分を拾う。"""
    fresh = "a\nb\n"
    assert gen.diff_summary(fresh, fresh) == []
    assert gen.diff_summary(fresh, "a\nB\n")


def test_closure_follows_markers_and_extras_on_dependencies():
    """pyjwt[crypto] の cryptography（依存先の extra）と、Windows 専用の pywin32（sys_platform 条件）を拾う。emscripten 専用は拾わない。"""
    scopes = gen.lock_scopes()
    assert "cryptography" in scopes
    assert "pywin32" in scopes
    assert "httpx2-jsfetch" not in scopes
