"""T-06・T-07 の ``exact`` の答えの補足の欄（BUG-0107）: LSP が型を決められない受け手（``Any``）の呼び出しは LSP の答えに無い。
名前で結べる（候補が対象 1 つだけ）ものは、``references`` / 木には混ぜず、別の欄に位置まで並べる。

偽の LSP サーバー（``test_T06_T07_exact_marks.FAKE_SERVER``）は ``Packer().pack([1])`` の 1 箇所だけを答える（型の分かる受け手）。
``packer: Any`` の ``packer.pack(...)`` 2 箇所と、テストの注釈なしの ``packer.pack(...)`` 1 箇所は LSP の答えに無い。

* compact: 本体の後ろに ``- not in exact answer (matched by name; receiver type unknown to the LSP): N sites`` と
  ``conf=heuristic`` の行。見出しの ``references:`` に ``+ N by name, not confirmed by the LSP``。
* json: ``data.references`` は LSP の答えのまま、``data.name_matched`` に同じ形で、``totals.name_matched``。
  ``_resolution_meta`` は ``exact`` のまま（補足は集計に数えない）。
* ``include_tests``・予算とページネーションが効く。LSP なし（``structural``）の答えは今までどおり（補足の欄は無い）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest
from test_T06_T07_exact_marks import PY_DEF, Fake

from lucianke.core.procs import ProcessRegistry
from lucianke.core.urn import CodeSel, Urn
from lucianke.tools import _lsp_exact

WS = "nm"
FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "class Packer:\n    def pack(self, items):\n        return items\n",
    "pkg/b.py": (
        "from typing import Any\n"
        "\n"
        "from pkg.a import Packer\n"
        "\n"
        "\n"
        "def caller():\n"
        "    return Packer().pack([1])\n"
        "\n"
        "\n"
        "def render(packer: Any, items):\n"
        "    return packer.pack(items)\n"
        "\n"
        "\n"
        "def again(packer: Any):\n"
        "    return packer.pack([3])\n"
    ),
    "tests/test_b.py": "def test_render(packer):\n    assert packer.pack([2])\n",
}
CFG: dict[str, Any] = {
    "name": "pack",
    "target_rel": "pkg/a.py",
    "caller_rel": "pkg/b.py",
    "caller_name": "caller",
    "target_range": [1, 4, 2, 20],
    "target_sel": [1, 8, 1, 12],
    "caller_range": [5, 0, 6, 29],
    "caller_sel": [5, 4, 5, 10],
    "call_site": [6, 20, 6, 24],
}
TARGET = {"urn": str(Urn(WS, "pkg/a.py", CodeSel("Packer.pack")))}
EXACT = ("pkg/b.py", 7, 21)
BY_NAME = [
    ("pkg/b.py", 11, 19, "render"),
    ("pkg/b.py", 15, 19, "again"),
    ("tests/test_b.py", 2, 19, "test_render"),
]
HEADER = "- not in exact answer (matched by name; receiver type unknown to the LSP): {} sites"


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


@pytest.fixture
async def fake(registry: ProcessRegistry, tmp_path: Path):
    _lsp_exact.MEMO.clear()
    f = Fake(registry, tmp_path, ws_id=WS, language="python", defn=PY_DEF, files=FILES, cfg=CFG)
    yield f
    await f.stop()
    _lsp_exact.MEMO.clear()


def ok(res: Any) -> Any:
    assert not res.is_error, res.text
    return res


async def refs(env: Any, fmt: str = "json", **args: Any) -> Any:
    return ok(
        await env.pipeline.call_tool(
            "find_references_and_implementations",
            {"target": TARGET, "query_type": "references", "format": fmt, **args},
        )
    )


def sites(items: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [(x["path"], x["line"], x["col"]) for x in items]


# --- T-06 ---------------------------------------------------------------------------------------------


async def test_t06_exact_json_keeps_references_pure_and_lists_name_matches(fake: Fake) -> None:
    env = await fake.start("publish")
    res = await refs(env)
    meta, data = res.structured["_resolution_meta"], res.structured["data"]
    # 本体は LSP の答えのまま。補足は精度の集計に数えない（降格・mixed に見せない）
    assert (meta["confidence_level"], meta["is_degraded"], meta["mixed"]) == ("exact", False, False), meta
    assert meta["confidence_breakdown"] == {"exact": 1}
    assert sites(data["references"]) == [EXACT]
    assert all("match" not in x for x in data["references"])
    got = [(x["path"], x["line"], x["col"], x["enclosing_name"]) for x in data["name_matched"]]
    assert got == BY_NAME
    for x in data["name_matched"]:
        assert (x["use"], x["confidence_level"], x["match"]) == ("call", "heuristic", "name")
        assert x["target_urn"] == TARGET["urn"] and x["snippet"]
    assert [x["is_test"] for x in data["name_matched"]] == [False, False, True]
    assert data["totals"] == {"files": 1, "sites": 1, "tests": 0, "name_matched": 3}
    assert "unattributed_calls" not in data
    assert any(
        w.startswith("3 call site(s) of .pack() are not in this exact answer because the LSP could not type")
        and "Packer.pack is the only method named 'pack' in the workspace" in w
        and "(data.name_matched)" in w
        for w in res.structured["warnings"]
    ), res.structured["warnings"]


async def test_t06_exact_compact_shows_the_section_after_the_references(fake: Fake) -> None:
    env = await fake.start("publish")
    res = await refs(env, "compact")
    assert g.validate(res.text) == [], (g.validate(res.text), res.text)
    lines = res.text.splitlines()
    assert lines[0].startswith("# find_references_and_implementations ok conf=exact "), lines[0]
    assert "references: 1 files, 1 sites (tests: 0) + 3 by name, not confirmed by the LSP (tests: 1)" in lines
    head = lines.index(HEADER.format(3))
    exact = next(i for i, x in enumerate(lines) if x.startswith("pkg/b.py:7:21  [call] in caller"))
    assert exact < head and "conf=" not in lines[exact]
    body = lines[head + 1 : head + 4]
    assert [x.split("  ")[0] for x in body] == [f"{p}:{line}:{col}" for p, line, col, _ in BY_NAME]
    assert all(x.endswith("  conf=heuristic") for x in body), body
    assert "[call] in render  return packer.pack(items)  conf=heuristic" in body[0]


async def test_t06_exact_section_respects_include_tests(fake: Fake) -> None:
    env = await fake.start("publish")
    res = await refs(env, include_tests=False)
    data = res.structured["data"]
    assert sites(data["references"]) == [EXACT]
    assert [(x["path"], x["line"]) for x in data["name_matched"]] == [
        (p, line) for p, line, _, _ in BY_NAME[:2]
    ]
    assert data["totals"]["name_matched"] == 2
    text = (await refs(env, "compact", include_tests=False)).text
    assert "+ 2 by name, not confirmed by the LSP (tests: 0)" in text and HEADER.format(2) in text
    assert "tests/test_b.py" not in text


USED = re.compile(r" ~(\d+)/(\d+)tok$")
SITE = re.compile(r"^(\S+):(\d+):(\d+)  \[")
RAISE = re.compile(r"raise max_tokens to ~(\d+) or more")
MORE = re.compile(r"^# more: cursor=(\S+) ", re.MULTILINE)


async def test_t06_exact_section_pages_with_the_budget_in_compact(fake: Fake) -> None:
    """compact（既定の形式）でも、全体より小さい予算で本体と補足の欄がページをまたぐ（BUG-0110）。呼ぶ側の AI と同じく、
    1 件も入らなければ警告の「raise max_tokens to ~N」に従って上げ、カーソルで辿る。目安は呼んだ値より大きく、従えば 1 件
    以上が返る。各ページは予算内・exact のまま、全ページで同じ位置は 1 度だけ、順は本体 → 補足。"""
    env = await fake.start("publish")
    full = (await refs(env, "compact")).text
    m = USED.search(full.splitlines()[0])
    assert m, full
    budget = int(m.group(1)) - 1  # 全部は入らない
    seen: list[tuple[str, tuple[Any, ...]]] = []
    cursor: str | None = None
    pages = raised = 0
    while True:
        text = (await refs(env, "compact", max_tokens=budget, **({"cursor": cursor} if cursor else {}))).text
        assert g.validate(text) == [], (g.validate(text), text)
        head = text.splitlines()[0]
        used = USED.search(head)
        assert used and "conf=exact" in head, head
        need = RAISE.search(text)
        if need:  # 1 件も入らない: 目安は呼んだ値より大きい。従って呼び直す
            assert not SITE.search(text) and not MORE.search(text), text
            assert int(need.group(1)) > budget, text
            budget = int(need.group(1))
            raised += 1
            assert raised < 3, text
            continue
        assert int(used.group(1)) <= budget, head
        pages += 1
        for line in text.splitlines()[1:]:
            site = SITE.match(line)
            if site:
                part = "name" if line.endswith("conf=heuristic") else "ref"
                seen.append((part, (site.group(1), int(site.group(2)), int(site.group(3)))))
        more = MORE.search(text)
        if not more:
            break
        cursor = more.group(1)
        assert pages < 10
    assert pages >= 2
    assert seen == [("ref", EXACT)] + [("name", (p, line, col)) for p, line, col, _ in BY_NAME]


async def test_t06_structural_compact_budget_sweep_never_returns_nothing_while_a_page_fits(fake: Fake) -> None:
    """票 BUG-0110 の実測の形（LSP なし・structural・compact）: 0 件のまま「今より小さい max_tokens に上げよ」と言わない。
    各予算で 1 件以上（続きがあればカーソルつき）か、0 件なら目安が呼んだ値より大きく、目安で呼び直すと 1 件以上が返る。
    予算を上げて件数が減らない（BUG-0067）。

    この答え（4 箇所・1 行約 22 トークン）では、カーソルの ``# more:`` 行（約 80 トークン）が残り 3 件より長いので、
    「1 件 + カーソル」のページは全体より大きい。全体の手前では 0 件が正しく、目安は全体（トレーラなしの全部）になる。
    以前は目安が ``# more:`` 行を数えず「前置き + 22」で、呼んだ値より小さかった。"""
    env = await fake.start(None)
    await refs(env, "compact")  # 1 回目は lsp=indexing の注意文。以後は lsp=unavailable で落ち着く
    full = (await refs(env, "compact")).text
    m = USED.search(full.splitlines()[0])
    assert m and sum(1 for line in full.splitlines() if SITE.match(line)) == 4, full
    whole = int(m.group(1))
    counts: list[int] = []
    for budget in range(whole - 120, whole + 1, 3):
        text = (await refs(env, "compact", max_tokens=budget)).text
        n = sum(1 for line in text.splitlines() if SITE.match(line))
        need = RAISE.search(text)
        if n == 0:
            assert need and budget < int(need.group(1)) <= whole, (budget, whole, text)
            again = (await refs(env, "compact", max_tokens=int(need.group(1)))).text
            assert any(SITE.match(line) for line in again.splitlines()), (budget, again)
        else:
            assert need is None, text
            used = USED.search(text.splitlines()[0])
            assert used and int(used.group(1)) <= budget, text
            assert n == 4 or MORE.search(text), (budget, text)  # 全部入らないならカーソルつき
        assert not counts or n >= counts[-1], (budget, counts, n)
        counts.append(n)
    assert counts.count(0) > 0 and counts[-1] == 4  # 0 件の帯と全体の両方を通っている


async def test_t06_exact_section_pages_with_the_budget(fake: Fake) -> None:
    """予算が小さいと本体と補足の欄はページをまたぐ。各ページは予算内・exact のまま、全ページで同じ位置は 1 度だけ、順は
    本体 → 補足（json。compact は上の試験）。"""
    env = await fake.start("publish")
    full = await refs(env)
    budget = full.structured["budget"]["used_tokens_est"] - 40  # 全部は入らない
    seen: list[tuple[str, tuple[Any, ...]]] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = (await refs(env, max_tokens=budget, **({"cursor": cursor} if cursor else {}))).structured
        pages += 1
        assert page["budget"]["used_tokens_est"] <= budget
        assert page["_resolution_meta"]["confidence_level"] == "exact"
        assert page["data"]["totals"]["name_matched"] == 3  # 見出しの数はどのページにも
        seen += [("ref", s) for s in sites(page["data"]["references"])]
        seen += [("name", s) for s in sites(page["data"].get("name_matched", []))]
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break
        assert pages < 10
    assert pages >= 2
    assert seen == [("ref", EXACT)] + [("name", (p, line, col)) for p, line, col, _ in BY_NAME]


async def test_t06_structural_answer_is_unchanged(fake: Fake) -> None:
    """LSP が無い（structural）ときは今までどおり references に印つきで足す。補足の欄は無い。"""
    env = await fake.start(None)
    res = await refs(env)
    data = res.structured["data"]
    assert res.structured["_resolution_meta"]["confidence_level"] == "heuristic"
    assert "name_matched" not in data and "name_matched" not in data["totals"]
    by_name = [(x["path"], x["line"], x["col"]) for x in data["references"] if x.get("match") == "name"]
    assert by_name == [(p, line, col) for p, line, col, _ in BY_NAME]
    assert data["totals"]["by_name"] == 3
    text = (await refs(env, "compact")).text
    assert "not in exact answer" not in text and "(tests: 1; by name: 3)" in text


# --- T-07 ---------------------------------------------------------------------------------------------


async def calls(env: Any, fmt: str = "json", **args: Any) -> Any:
    return ok(
        await env.pipeline.call_tool(
            "query_call_hierarchy",
            {"target": TARGET, "direction": "incoming", "depth": 1, "format": fmt, **args},
        )
    )


async def test_t07_exact_tree_lists_name_matched_callers_separately(fake: Fake) -> None:
    env = await fake.start("publish")
    res = await calls(env)
    meta, data = res.structured["_resolution_meta"], res.structured["data"]
    assert (meta["confidence_level"], meta["is_degraded"], meta["mixed"]) == ("exact", False, False), meta
    assert [n["name"] for n in data["nodes"]] == ["caller"]
    assert [(e["call_site_path"], e["call_site_line"]) for e in data["edges"]] == [("pkg/b.py", 7)]
    got = [(n["name"], n["call_site_path"], n["call_site_line"], n["depth"]) for n in data["name_matched"]]
    assert got == [(name, p, line, 1) for p, line, _, name in BY_NAME]
    for n in data["name_matched"]:
        assert (n["direction"], n["confidence_level"], n["match"], n["parent_urn"]) == (
            "incoming",
            "heuristic",
            "name",
            TARGET["urn"],
        )
    assert "unattributed_calls" not in data
    assert any(
        w.startswith("incoming: 3 call site(s) of .pack() are not in this exact tree")
        for w in res.structured["warnings"]
    ), res.structured["warnings"]

    text = (await calls(env, "compact")).text
    assert g.validate(text) == [], (g.validate(text), text)
    lines = text.splitlines()
    assert " conf=exact " in lines[0]
    head = lines.index(
        "- not in exact tree (matched by name; receiver type unknown to the LSP): 3 callers, 3 sites"
    )
    assert lines.index("<- caller @pkg/b.py:7") < head
    assert lines[head + 1 : head + 4] == [
        f"<- {name} @{p}:{line}  conf=heuristic" for p, line, _, name in BY_NAME
    ]


async def test_t07_exact_section_respects_include_tests_and_graph_marks_it(fake: Fake) -> None:
    env = await fake.start("publish")
    res = await calls(env, include_tests=False)
    assert [n["name"] for n in res.structured["data"]["name_matched"]] == ["render", "again"]
    dot = (await calls(env, "dot")).text
    assert "render" in dot and "test_render" in dot and "dashed" in dot


async def test_t07_structural_tree_is_unchanged(fake: Fake) -> None:
    env = await fake.start(None)
    res = await calls(env)
    data = res.structured["data"]
    assert "name_matched" not in data
    first = {n["name"]: n.get("match") for n in data["nodes"]}
    assert first == {"caller": None, "render": "name", "again": "name", "test_render": "name"}
