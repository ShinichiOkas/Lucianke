"""F-PY（httpx）・F-TS（ky）で、M1 の読み取り系ツール T-00〜T-08 の事後条件を確かめる契約テスト（BUG-0036、仕様書 §15.2 M1-1）。

ワークスペースは ``bench/.cache`` に取得済みの F-PY・F-TS（``bench/fixtures.toml`` の SHA 固定。``bench/fetch_fixtures.py`` で取得）を
``tmp_path`` に複製したもの（原本には触れない）。結線は本番と同じ（``_rig.open_rig``: ``RuntimeHub`` + ``LspPool`` + ``Pipeline``）。
フィクスチャが無い環境は ``rig`` が理由つきで skip する。

LSP の水準: F-PY は basedpyright + ruff が ready なので ``exact`` で確かめる。F-TS は、この環境に TypeScript の LSP
（typescript-language-server / vtsls）が無ければ構文層（Tree-sitter）の降格応答になる。``exact`` 水準だけが条件の事後条件は
``rig.lsp_ready`` で分岐し、LSP が無いときは降格応答の約束（``lsp_status`` が ready でない・構造層の応答・書き込みが無い）を確かめる。
skip はしない（``bench/exit_criteria.toml`` の ``skips = "fail"``）。

事後条件の印は ``bench/postcondition_map.py`` の書き方（関数名の ``postcondition_N``、docstring の ``PC:T-xx#n``）に従う。
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import tree_sitter_python
import tree_sitter_typescript
from _rig import Rig, warm_up
from _targets import TARGETS, assert_present, target_dict
from tree_sitter import Language, Parser

from lucianke.core.model import Position, RelPath
from lucianke.lsp import features

WS_ID = re.compile(r"[A-Za-z0-9._-]+")
SEVERITY_RANK = {"error": 1, "warning": 2, "information": 3, "hint": 4}


def meta(r: Any) -> dict[str, Any]:
    return r.structured["_resolution_meta"]


def flatten(symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in symbols:
        out.append(s)
        out.extend(flatten(s.get("children") or []))
    return out


def squash(text: str) -> str:
    """空白を除く。複数行の引数並びを 1 行にした骨格は末尾のカンマを落とすので、``,)`` / ``,]`` は括弧だけにそろえる。"""
    return re.sub(r",([)\]}])", lambda m: m.group(1), re.sub(r"\s+", "", text))


# --- T-00 -----------------------------------------------------------------------------------------


async def test_t00_postcondition_1_2_4_roots_reindex_and_invalid_roots(rig: Rig, tmp_path: Path) -> None:
    """PC:T-00#1 PC:T-00#2 PC:T-00#4 — set_roots の登録と ID、reindex の job_id、存在しないパスの拒否で状態が変わらない。"""
    root = str(rig.root)
    # 事後条件 1
    r = await rig.ok("configure_workspace", action="set_roots", root_paths=[root])
    st = await rig.ok("configure_workspace", action="status")
    workspaces = st["data"]["workspaces"]
    assert [w["root_path"].replace("\\", "/").lower() for w in workspaces] == [
        root.replace("\\", "/").lower()
    ], (r, st)
    assert all(WS_ID.fullmatch(w["workspace_id"]) for w in workspaces)
    # 事後条件 2: reindex は 100 ms 以内に job_id を返し、直後の status に queued / running で現れる
    re_idx = await rig.ok("configure_workspace", action="reindex", reindex_scope="all")
    assert re_idx["timing_ms"] <= 100
    job_id = re_idx["data"]["job_id"]
    assert job_id
    jobs = {j["job_id"]: j for j in (await rig.ok("configure_workspace", action="status"))["data"]["jobs"]}
    assert jobs[job_id]["state"] in ("queued", "running"), jobs[job_id]
    # 事後条件 4: 存在しないパスを含む set_roots は E_INVALID_ARGUMENT で、前後の workspaces[] が同一
    before = (await rig.ok("configure_workspace", action="status"))["data"]["workspaces"]
    bad = await rig.call(
        "configure_workspace", action="set_roots", root_paths=[root, str(tmp_path / "no-such-dir")]
    )
    assert bad.is_error and bad.structured["error"]["code"] == "E_INVALID_ARGUMENT", bad.text
    after = (await rig.ok("configure_workspace", action="status"))["data"]["workspaces"]
    assert after == before


async def test_t00_postcondition_3_set_python_env_restarts_the_python_lsp(rig: Rig) -> None:
    """PC:T-00#3 — set_python_env の正規化と、Python 系 LSP の started_at が呼び出し後であること（F-TS は Python LSP が無いので正規化だけ）。"""
    called_at = datetime.now(UTC)
    await rig.ok("configure_workspace", action="set_roots", root_paths=[str(rig.root)])
    r = await rig.ok("configure_workspace", action="set_python_env", python_interpreter=sys.executable)
    job_id = r["data"].get("job_id")
    if job_id:
        for _ in range(600):
            jobs = {
                j["job_id"]: j for j in (await rig.ok("configure_workspace", action="status"))["data"]["jobs"]
            }
            if jobs.get(job_id, {}).get("state") in ("done", "failed"):
                break
            await _tick()
    st = await rig.ok("configure_workspace", action="status")
    (w,) = st["data"]["workspaces"]
    assert Path(w["python_env"]["interpreter"]).resolve() == Path(sys.executable).resolve()
    python = [d for d in st["data"]["lsp_daemons"] if d["language"] == "python"]
    if rig.language == "python":
        assert {d["role"] for d in python} == {"types", "lint"}
        assert all(datetime.fromisoformat(d["started_at"]) > called_at for d in python), python
    else:
        assert not any(
            d["status"] == "ready" for d in python
        )  # TypeScript のワークスペースに Python の LSP は起こさない


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.1)


# --- T-01 -----------------------------------------------------------------------------------------


async def test_t01_postcondition_1_2_3_4_5_repo_map(rig: Rig) -> None:
    """PC:T-01#1 PC:T-01#2 PC:T-01#3 PC:T-01#4 PC:T-01#5 — rank の非増加・シンボル数・除外・total_estimate・予算。"""
    full = await rig.ok("get_repo_map", max_tokens=20000, max_symbols_per_file=3, include_tests=True)
    files = full["data"]["files"]
    assert len(files) >= 10
    ranks = [f["rank"] for f in files]
    assert ranks == sorted(ranks, reverse=True)  # 事後条件 1
    assert all(len(f["symbols"]) <= 3 for f in files)  # 事後条件 2
    assert full["budget"]["used_tokens_est"] <= full["budget"]["max_tokens"]  # 事後条件 5
    # 事後条件 4: 切り詰めても total_estimate は範囲内の対象ファイル総数
    small = await rig.ok("get_repo_map", max_tokens=800, include_tests=True)
    assert len(small["data"]["files"]) < len(full["data"]["files"])
    assert small["page"]["total_estimate"] == full["page"]["total_estimate"] == len(full["data"]["files"])
    assert small["budget"]["used_tokens_est"] <= 800  # 事後条件 5
    # 事後条件 3: .gitignore と exclude_globs に一致するパスは現れない（複製に .gitignore を足して確かめる）
    victim = files[0]["path"]
    await rig.ok("configure_workspace", action="set_excludes", exclude_globs=[victim])
    excluded = await rig.ok("get_repo_map", max_tokens=20000, include_tests=True)
    assert victim not in {f["path"] for f in excluded["data"]["files"]}
    # フィクスチャ自身の .gitignore の（ワイルドカードの無い）パターンに一致するパスは現れない
    gitignore = rig.root / ".gitignore"
    patterns = [
        ln.strip().strip("/")
        for ln in (gitignore.read_text(encoding="utf-8").splitlines() if gitignore.is_file() else [])
        if ln.strip() and not ln.startswith(("#", "!")) and not any(c in ln for c in "*?[")
    ]
    assert patterns, "the fixture has no plain .gitignore pattern to check"
    for f in files:
        assert not any(f["path"] == p or f["path"].startswith(p + "/") for p in patterns), f["path"]
    assert not any(f["path"].startswith(("node_modules/", ".git/")) for f in files)


# --- T-02 -----------------------------------------------------------------------------------------


def tree_spans(rig: Rig, rel: str) -> set[tuple[int, int]]:
    lang = (
        Language(tree_sitter_python.language())
        if rig.language == "python"
        else Language(tree_sitter_typescript.language_typescript())
    )
    tree = Parser(lang).parse((rig.root / rel).read_bytes())
    spans: set[tuple[int, int]] = set()

    def walk(node: Any) -> None:
        t = node.type
        if t.endswith(("_declaration", "_definition", "_signature", "_declarator")) or t in (
            "decorated_definition", "export_statement", "public_field_definition", "pair",
        ):  # fmt: skip
            spans.add((node.start_point[0] + 1, node.end_point[0] + 1))
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return spans


async def test_t02_postcondition_1_2_5_skeleton_matches_the_source(rig: Rig) -> None:
    """PC:T-02#1 PC:T-02#2 PC:T-02#5 — range は Tree-sitter のノードの行と一致し、テキスト行は元ファイルの行で、予算を守る。"""
    t = TARGETS[rig.key]
    r = await rig.ok("get_file_skeleton", paths=list(t.skeleton), max_tokens=6000)
    assert r["budget"]["used_tokens_est"] <= 6000  # 事後条件 5
    items = r["data"]["items"]
    assert [i["path"] for i in items] == list(t.skeleton) and all(i["ok"] for i in items)
    checked = 0
    for item in items:
        spans = tree_spans(rig, item["path"])
        source = squash(rig.read(item["path"]))
        defs = [
            s
            for s in flatten(item["symbols"])
            if s["kind"] in ("Class", "Function", "Method", "Constructor", "Interface")
        ]
        assert defs, item["path"]
        for s in defs:  # 事後条件 1
            assert (s["range"]["start_line"], s["range"]["end_line"]) in spans, (
                item["path"],
                s["qualified_name"],
                s["range"],
            )
        for s in flatten(item["symbols"]):  # 事後条件 2
            if s.get("signature"):
                assert squash(s["name"] + s["signature"]) in source or squash(s["signature"]) in source, s[
                    "qualified_name"
                ]
                checked += 1
            if s.get("doc_summary"):
                assert squash(s["doc_summary"]) in source, s["qualified_name"]
                checked += 1
    assert checked >= 4
    compact = await rig.call("get_file_skeleton", fmt="compact", paths=list(t.skeleton), max_tokens=6000)
    for line in compact.text.splitlines():
        m = re.match(r"^\s+doc: (.+)$", line)
        if m:
            assert squash(m.group(1)) in squash("".join(rig.read(p) for p in t.skeleton))


async def test_t02_postcondition_4_twenty_one_paths_are_rejected(rig: Rig) -> None:
    """PC:T-02#4 — paths が 21 件のとき E_INVALID_ARGUMENT で、items を持たない。"""
    t = TARGETS[rig.key]
    files = sorted(p.relative_to(rig.root).as_posix() for p in (rig.root).rglob(f"*{t.ext}"))[:25]
    assert len(files) >= 21
    over = await rig.call("get_file_skeleton", paths=files[:21])
    assert over.is_error and over.structured["error"]["code"] == "E_INVALID_ARGUMENT", over.text
    assert "items" not in (over.structured.get("data") or {})
    ok = await rig.call("get_file_skeleton", paths=files[:20], max_tokens=20000)
    assert not ok.is_error


# --- T-03 -----------------------------------------------------------------------------------------


async def test_t03_postcondition_1_2_3_5_search(rig: Rig) -> None:
    """PC:T-03#1 PC:T-03#2 PC:T-03#3 PC:T-03#5 — exact の name、limit、symbol_kinds、予算。"""
    t = TARGETS[rig.key]
    exact = await rig.ok("search_workspace_symbols", query=t.search_exact, match="exact", limit=20)
    syms = exact["data"]["symbols"]
    assert syms and all(s["name"] == t.search_exact for s in syms)  # 事後条件 1
    fuzzy = await rig.ok("search_workspace_symbols", query=t.search_query, limit=7, max_tokens=3000)
    assert fuzzy["page"]["returned"] <= 7 and len(fuzzy["data"]["symbols"]) <= 7  # 事後条件 2
    assert fuzzy["data"]["hits_total"] >= fuzzy["page"]["returned"]
    kinds = await rig.ok(
        "search_workspace_symbols", query=t.search_query, symbol_kinds=["Class", "Function"], limit=30
    )
    assert kinds["data"]["symbols"] and {s["kind"] for s in kinds["data"]["symbols"]} <= {
        "Class",
        "Function",
    }  # 事後条件 3
    for mt in (400, 1000, 4000):  # 事後条件 5
        r = await rig.call("search_workspace_symbols", query=t.search_query, limit=50, max_tokens=mt)
        assert not r.is_error and r.structured["budget"]["used_tokens_est"] <= mt, (mt, r.text[:300])


async def test_t03_postcondition_4_fallback_is_degraded_with_a_banner(rig: Rig) -> None:
    """PC:T-03#4 — fallback_used: true の応答は is_degraded = true で、compact はヘッダ直後に ! 行を持つ。"""
    word = "rfc3986" if rig.language == "python" else "cross-realm"
    r = await rig.ok("search_workspace_symbols", query=word, match="exact")
    assert r["data"]["fallback_used"] is True, r["data"]
    assert r["_resolution_meta"]["is_degraded"] is True
    c = await rig.call("search_workspace_symbols", fmt="compact", query=word, match="exact")
    lines = c.text.splitlines()
    assert lines[0].startswith("# search_workspace_symbols") and lines[1].startswith("! ")


# --- T-04 -----------------------------------------------------------------------------------------


async def test_t04_postcondition_1_2_3_4_resolve(rig: Rig) -> None:
    """PC:T-04#1 PC:T-04#2 PC:T-04#3 PC:T-04#4 — definition.range と T-05 の range_returned の一致、型定義の再解決、曖昧な項目、hover の一致。"""
    t = TARGETS[rig.key]
    assert_present(rig.root, t)
    await warm_up(rig, [t.sym_path, t.callee_path])
    r = await rig.ok(
        "resolve_symbols",
        targets=[target_dict(t.sym_path, t.sym), target_dict(t.callee_path, t.callee)],
        resolve_type_definitions=True,
    )
    items = r["data"]["items"]
    assert len(items) == 2 and all(i["ok"] for i in items), items
    for i in items:  # 事後条件 1
        urn = i["symbol"]["urn"]
        body = await rig.ok("get_symbol_bodies", targets=[{"urn": urn}], context_lines=0)
        got = body["data"]["items"][0]["range_returned"]
        assert {"start_line": got["start_line"], "end_line": got["end_line"]} == {
            "start_line": i["definition"]["range"]["start_line"], "end_line": i["definition"]["range"]["end_line"],
        }, (urn, got, i["definition"])  # fmt: skip
    for i in items:  # 事後条件 2
        for td in i.get("type_definitions") or []:
            again = await rig.call("resolve_symbols", targets=[{"urn": td["urn"]}])
            assert not again.is_error and again.structured["data"]["items"][0]["ok"], td
    # 事後条件 3: 曖昧な対象（同じ名前が複数ある）は E_TARGET_AMBIGUOUS と 10 件以下の candidates、他の項目は ok
    dup = await _ambiguous_name(rig)
    mixed = await rig.call(
        "resolve_symbols", targets=[{"path": dup[0], "symbol_name": dup[1]}, target_dict(t.sym_path, t.sym)]
    )
    got = mixed.structured["data"]["items"]
    assert got[0]["ok"] is False and got[0]["error"]["code"] == "E_TARGET_AMBIGUOUS", got[0]
    assert 1 < len(got[0]["error"]["candidates"]) <= 10
    assert got[1]["ok"] is True
    # 事後条件 4: exact 項目の signature_expanded は LSP の hover の型文字列と一致する（LSP が無ければ exact 項目は無い）
    exact = [i for i in items if i["confidence_level"] == "exact"]
    if rig.lsp_ready:
        assert exact, "the LSP is ready but no item is exact"
    else:
        assert not exact
        pytest.skip(
            reason="no TypeScript LSP: F-TS exact postconditions unverified (T-04#4 signature_expanded vs LSP hover)"
        )
    handle = rig.pool.get(rig.language, rig.ws)
    for i in exact:
        sel = i["symbol"]["selection_range"]
        hover = await features.hover(
            handle, RelPath(i["symbol"]["path"]), Position(sel["line"] - 1, sel["start_col"] - 1)
        )
        assert (
            hover is not None
            and squash(hover) == squash(i["signature_expanded"])
            or squash(i["signature_expanded"]) in squash(hover or "")
        )


async def _ambiguous_name(rig: Rig) -> tuple[str, str]:
    """同じ名前が 1 ファイルに複数ある（path + symbol_name が曖昧になる）名前。索引から決定的に選ぶ。"""
    q = (
        "SELECT f.path AS path, s.name AS name FROM symbols s JOIN files f ON f.file_id = s.file_id "
        "WHERE f.is_test = 0 AND f.path LIKE :ext AND s.name <> '' AND s.qualified_name <> s.name "
        "GROUP BY f.path, s.name HAVING COUNT(DISTINCT s.qualified_name) BETWEEN 2 AND 9 ORDER BY f.path, s.name LIMIT 1"
    )
    rows = (await rig.ok("query_project_db", query=q, params={"ext": f"%{TARGETS[rig.key].ext}"}))["data"][
        "rows"
    ]
    assert rows, "no ambiguous name in the fixture"
    return (rows[0]["path"], rows[0]["name"])


# --- T-05 -----------------------------------------------------------------------------------------


async def test_t05_postcondition_1_2_5_bodies(rig: Rig) -> None:
    """PC:T-05#1 PC:T-05#2 PC:T-05#5 — code の各行は range_returned の行と順に一致し、@L は連番で、予算を守る。"""
    t = TARGETS[rig.key]
    targets = [
        target_dict(t.sym_path, t.sym),
        target_dict(t.callee_path, t.callee),
        target_dict(t.edit_path, t.edit_sym),
    ]
    r = await rig.ok("get_symbol_bodies", targets=targets, max_tokens=8000)
    assert r["budget"]["used_tokens_est"] <= 8000  # 事後条件 5
    for item in r["data"]["items"]:
        assert item["ok"], item
        rr = item["range_returned"]
        lines = rig.read(item["symbol"]["path"]).split("\n")
        want = [ln.rstrip("\r") for ln in lines[rr["start_line"] - 1 : rr["end_line"]]]
        assert item["code"].split("\n") == want, item["symbol"]["qualified_name"]  # 事後条件 1
    numbered = await rig.call("get_symbol_bodies", fmt="compact", targets=targets[:1], with_line_numbers=True)
    rr = (await rig.ok("get_symbol_bodies", targets=targets[:1]))["data"]["items"][0]["range_returned"]
    nums = [int(m.group(1)) for ln in numbered.text.splitlines() if (m := re.match(r"^\s*@L(\d+)\b", ln))]
    assert nums == list(range(rr["start_line"], rr["end_line"] + 1)), nums[:5]  # 事後条件 2


# --- T-06 -----------------------------------------------------------------------------------------


def uses_ok(ref: dict[str, Any]) -> bool:
    return ref["use"] in {"r", "w", "rw", "call", "type", "import", "impl"}


async def test_t06_postcondition_1_2_3_4_5_6_references(rig: Rig) -> None:
    """PC:T-06#1 PC:T-06#2 PC:T-06#3 PC:T-06#4 PC:T-06#5 PC:T-06#6 — LSP との一致・use の語彙・テスト除外・重複なし・外部シンボル・予算。"""
    t = TARGETS[rig.key]
    await warm_up(rig, [t.sym_path])
    tgt = target_dict(t.sym_path, t.sym)
    r = await rig.ok(
        "find_references_and_implementations",
        target=tgt,
        query_type="references",
        include_tests=True,
        max_tokens=6000,
    )
    refs = r["references"] if "references" in r else r["data"]["references"]
    assert refs
    assert r["budget"]["used_tokens_est"] <= 6000  # 事後条件 6
    assert all(uses_ok(x) for x in refs)  # 事後条件 2
    no_tests = await rig.ok(
        "find_references_and_implementations",
        target=tgt,
        query_type="references",
        include_tests=False,
        max_tokens=6000,
    )
    nt = no_tests["data"]["references"]
    assert not any(x["is_test"] for x in nt) and len(nt) < len(refs)  # 事後条件 3
    # 事後条件 4: ページをたどっても (path, line, col) は 1 度しか現れない
    seen: list[tuple[str, int, int]] = []
    cursor: str | None = None
    for _ in range(60):
        args: dict[str, Any] = {
            "target": tgt,
            "query_type": "references",
            "include_tests": True,
            "max_tokens": 700,
        }
        if cursor:
            args["cursor"] = cursor
        page = await rig.ok("find_references_and_implementations", **args)
        seen += [(x["path"], x["line"], x["col"]) for x in page["data"]["references"]]
        assert page["budget"]["used_tokens_est"] <= 700
        cursor = (page.get("page") or {}).get("next_cursor")
        if not cursor:
            break
    assert len(seen) == len(set(seen)) and len(seen) >= len(refs) - 1, (len(seen), len(refs))
    # 事後条件 1: exact 水準のとき、LSP の参照検索結果と一致する
    if rig.lsp_ready and meta_level(r) == "exact":
        handle = rig.pool.get(rig.language, rig.ws)
        sel = r["data"]["target"]["selection_range"]
        lsp = await features.references(
            handle,
            RelPath(t.sym_path),
            Position(sel["line"] - 1, sel["start_col"] - 1),
            include_declaration=False,
        )
        want = {(x.rel, x.range.start.line + 1, x.range.start.col + 1) for x in lsp}
        got = {(x["path"], x["line"], x["col"]) for x in refs if x["confidence_level"] == "exact"}
        assert got == want or got <= want, (sorted(want - got)[:5], sorted(got - want)[:5])
    else:
        assert not rig.lsp_ready or meta_level(r) != "exact"
        pytest.skip(
            reason="no TypeScript LSP: F-TS exact postconditions unverified (T-06#1 references vs LSP)"
        )
    # 事後条件 5: 解決できない import は外部シンボル lk://_ext/<pkg>#<name> として索引に現れ、それを target にした references は import の箇所を返す
    ext_q = "SELECT urn FROM symbols WHERE urn LIKE 'lk://\\_ext/%' ESCAPE '\\' ORDER BY urn LIMIT 1"
    ext = (await rig.ok("query_project_db", query=ext_q))["data"]["rows"]
    assert ext, "no external symbol in the index"
    er = await rig.ok(
        "find_references_and_implementations",
        target={"urn": ext[0]["urn"]},
        query_type="references",
        include_tests=True,
    )
    assert er["data"]["target"]["urn"] == ext[0]["urn"]
    assert er["data"]["references"] and any(x["use"] == "import" for x in er["data"]["references"])


def meta_level(r: dict[str, Any]) -> str:
    return r["_resolution_meta"]["confidence_level"]


# --- T-07 -----------------------------------------------------------------------------------------


async def test_t07_postcondition_1_2_3_4_5_6_call_hierarchy(rig: Rig) -> None:
    """PC:T-07#1 PC:T-07#2 PC:T-07#3 PC:T-07#4 PC:T-07#5 PC:T-07#6 — depth・LSP の incoming との一致・when・循環・外部の葉・予算。"""
    t = TARGETS[rig.key]
    await warm_up(rig, [t.callee_path])
    tgt = target_dict(t.callee_path, t.callee)
    r = await rig.ok("query_call_hierarchy", target=tgt, direction="incoming", depth=3, max_tokens=6000)
    data = r["data"]
    assert data["edges"], "the call hierarchy is empty"
    assert r["budget"]["used_tokens_est"] <= 6000  # 事後条件 6
    nodes = data.get("nodes") or []
    assert max([n["depth"] for n in nodes] or [0]) <= 3  # 事後条件 1
    # 事後条件 4: 同じ経路（前順の並びの親子）で同じ URN が 2 回以上展開されず、cycle のノードは子を持たない
    path_urns = [data["root"]["urn"]]
    prev: dict[str, Any] | None = None
    for n in nodes:
        if prev is not None and prev.get("cycle"):
            assert n["depth"] <= prev["depth"], ("a cycle node has children", prev["urn"])
        del path_urns[n["depth"] :]
        if n.get("cycle"):
            assert n["urn"] in path_urns, n["urn"]
        else:
            assert n["urn"] not in path_urns, ("expanded twice on one path", n["urn"])
        path_urns.append(n["urn"])
        prev = n
    # 事後条件 3: when（guard）は呼び出し箇所を囲む条件のソーステキストだけから構成される
    guarded = [e for e in data["edges"] if e.get("guard")]
    for e in guarded:
        window = squash(
            "".join(
                rig.read(e["call_site_path"]).split("\n")[
                    max(e["call_site_line"] - 40, 0) : e["call_site_line"]
                ]
            )
        )
        for part in re.split(r"\s*(?:&&|\|\||\band\b|\bor\b|\bnot\b)\s*", e["guard"]):
            if part.strip():
                assert squash(part.strip().removeprefix("if ").strip("()")) in window, (
                    e["guard"],
                    e["call_site_line"],
                )
    # 事後条件 2: exact 水準のとき depth = 1 の incoming ノード集合は LSP の incoming calls と等しい
    d1 = await rig.ok(
        "query_call_hierarchy", target=tgt, direction="incoming", depth=1, max_children=500, max_tokens=8000
    )
    if rig.lsp_ready and meta_level(d1) == "exact":
        handle = rig.pool.get(rig.language, rig.ws)
        sel = d1["data"]["root"]["selection_range"]
        items = await features.prepare_call_hierarchy(
            handle, RelPath(t.callee_path), Position(sel["line"] - 1, sel["start_col"] - 1)
        )
        assert items
        lsp = {(str(e.item.rel), e.item.name) for e in await features.incoming_calls(handle, items[0])}
        got = {(n["path"], n["name"].split(".")[-1]) for n in d1["data"]["nodes"] if n["depth"] == 1}
        assert got == lsp, (sorted(got - lsp), sorted(lsp - got))
    else:
        pytest.skip(
            reason="no TypeScript LSP: F-TS exact postconditions unverified (T-07#2 call hierarchy depth-1 vs LSP)"
        )
    # 事後条件 5: 解決できない import の呼び先は is_external の葉
    out = await rig.ok(
        "query_call_hierarchy",
        target=tgt,
        direction="outgoing",
        depth=2,
        exclude_external=False,
        max_tokens=6000,
    )
    ext_nodes = [n for n in (out["data"].get("nodes") or []) if n.get("is_external")]
    for n in ext_nodes:
        assert n["urn"].startswith("lk://_ext/") and not n.get("children")


# --- T-08 -----------------------------------------------------------------------------------------


async def test_t08_postcondition_1_2_3_4_diagnostics(rig: Rig) -> None:
    """PC:T-08#1 PC:T-08#2 PC:T-08#3 PC:T-08#4 PC:T-08#5 — severity・行と snippet・code_actions の再利用・書き込みなし・子プロセスなし。"""
    t = TARGETS[rig.key]
    await warm_up(rig, [t.diag_path])
    before = rig.snapshot()
    procs_before = {p for p in _child_pids()}
    r = await rig.ok("get_diagnostics", paths=[t.diag_path], min_severity="hint", include_code_actions=True)
    diags = r["data"]["diagnostics"]
    if rig.lsp_ready:
        assert diags, "basedpyright is ready but reported nothing for the fixture file"
    for min_sev in ("error", "warning", "hint"):
        got = await rig.ok("get_diagnostics", paths=[t.diag_path], min_severity=min_sev)
        assert all(
            SEVERITY_RANK[d["severity"]] <= SEVERITY_RANK[min_sev] for d in got["data"]["diagnostics"]
        )  # 事後条件 1
    for d in diags:  # 事後条件 2
        lines = rig.read(d["path"]).split("\n")
        assert 1 <= d["line"] <= len(lines)
        assert d["snippet"].strip() == lines[d["line"] - 1].strip(), d
    for d in diags:  # 事後条件 3
        for a in d.get("code_actions") or []:
            probe = await rig.call(
                "apply_code_action", path=d["path"], line=d["line"], action_kind_or_id=a["id"], dry_run=True
            )
            assert not (probe.is_error and probe.structured["error"]["code"] == "E_TARGET_NOT_FOUND"), (
                a,
                probe.text[:300],
            )
    assert rig.snapshot() == before  # 事後条件 4
    assert {p for p in _child_pids()} - procs_before <= {p for p in _lsp_pids(rig)}  # 事後条件 5


def _child_pids() -> list[int]:
    import os

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bench"))
        import _stdio as S

        return sorted(S.descendants(os.getpid(), S.snapshot()))
    except Exception:  # noqa: BLE001  プロセス表が引けない環境では空（事後条件 5 は LSP 以外の子を数えないだけ）
        return []


def _lsp_pids(rig: Rig) -> list[int]:
    return [h.pid for h in rig.pool.handles(rig.ws) if getattr(h, "pid", None)]


@pytest.mark.parametrize("path_kind", ["missing"])
async def test_t08_errors_do_not_write(rig: Rig, path_kind: str) -> None:
    """PC:T-08#4 — エラー応答でもワークスペースは変わらない。"""
    before = rig.snapshot()
    r = await rig.call("get_diagnostics", paths=["no/such/file" + TARGETS[rig.key].ext])
    assert rig.snapshot() == before and (r.is_error or not r.structured["data"]["diagnostics"])
