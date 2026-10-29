"""Lucianke のドッグフーディング 2 周目: M1 の残りのツールを実 stdio クライアントで F-POLY（のコピー）とリポジトリ自身に当てる。
使い方:  <WT>/.venv/Scripts/python.exe bench/dogfood/run_dogfood2.py --repo <WT> [--poly <F-POLY のコピー>] [観点名 ...]
  --repo  使い捨て作業ツリー（push 済みの最新コミット）。サーバーはここで ``python -m lucianke`` として起こす（BUG-0030）。
  --poly  編集系が書き換える F-POLY のコピー（省略時は <WT>/../lucianke-dogfood2-poly に <WT>/tests/fixtures/poly を複製）。
観点名: cfg t03 t04 t06 t07 t08 t10 t11 t13 t19 t20 unsup bom burst   （省略で全部）
出力:    bench/dogfood/out/obs2_<観点名>.json（UTF-8）。標準出力は cp932 で化けるので結果はファイルを読む。
注意:    このリポジトリ自身は編集しない（読むだけ）。編集系はコピーにだけ当てる。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import re
import shutil
import sys
import time
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")
from mcp import Client, StdioServerParameters  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
ap = argparse.ArgumentParser()
ap.add_argument("--repo", required=True)
ap.add_argument("--poly")
ap.add_argument("obs", nargs="*")
ARGS = ap.parse_args()
REPO = Path(ARGS.repo).resolve()
POLY_SRC = REPO / "tests" / "fixtures" / "poly"
POLY = Path(ARGS.poly).resolve() if ARGS.poly else REPO.parent / "lucianke-dogfood2-poly"
sys.path.insert(0, str(REPO / "tests" / "contract"))
import compact_grammar as g  # noqa: E402

BIG = 1_000_000
FINDINGS: dict[str, list[str]] = {}


def note(obs: str, msg: str) -> None:
    FINDINGS.setdefault(obs, []).append(msg)
    print(f"[{obs}] {msg}"[:300].encode("ascii", "replace").decode())


def fresh_poly() -> Path:
    if POLY.exists():
        shutil.rmtree(POLY, ignore_errors=True)
    shutil.copytree(POLY_SRC, POLY)
    return POLY


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


class Sess:
    def __init__(self) -> None:
        self.client: Client | None = None
        self.n = 0

    async def __aenter__(self) -> Sess:
        p = StdioServerParameters(command=str(REPO / ".venv" / "Scripts" / "python.exe"), args=["-m", "lucianke"],
                                  env={"PYTHONUTF8": "1"}, cwd=str(REPO))
        self._cm = Client(p)
        self.client = await self._cm.__aenter__()
        return self

    async def __aexit__(self, *a: Any) -> None:
        await self._cm.__aexit__(*a)

    async def raw(self, tool: str, args: dict[str, Any], timeout: float = 30) -> tuple[bool, str, float]:
        t = time.perf_counter()
        self.n += 1
        try:
            r = await asyncio.wait_for(self.client.call_tool(tool, args), timeout)
        except TimeoutError:
            return True, "TIMEOUT", timeout * 1000
        except Exception as e:  # noqa: BLE001
            return True, f"EXC {type(e).__name__}: {e}"[:300], (time.perf_counter() - t) * 1000
        return bool(r.is_error), (r.content[0].text if r.content else ""), (time.perf_counter() - t) * 1000

    async def j(self, tool: str, args: dict[str, Any]) -> tuple[dict, float]:
        err, text, dt = await self.raw(tool, {**args, "format": "json"})
        try:
            return json.loads(text), dt
        except Exception:
            return {"_unparsed": text[:400], "_is_error": err}, dt

    async def c(self, tool: str, args: dict[str, Any]) -> tuple[str, float]:
        err, text, dt = await self.raw(tool, args)
        return text, dt

    async def setup(self, root: Path) -> dict:
        d, dt = await self.j("configure_workspace", {"action": "set_roots", "root_paths": [root.as_posix()]})
        self.set_roots_ms = dt
        for _ in range(60):
            s, _ = await self.j("configure_workspace", {"action": "status"})
            jobs = (s.get("data") or {}).get("jobs") or []
            if jobs and all(j.get("state") in ("done", "failed") for j in jobs):
                break
            await asyncio.sleep(0.5)
        return s


def save(name: str, obj: Any) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"obs2_{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def meta_ok(d: dict) -> list[str]:
    bad = []
    for k in ("ok", "data", "_resolution_meta", "page", "budget", "timing_ms", "warnings", "error"):
        if k not in d:
            bad.append(f"missing key {k}")
    return bad


def check_compact(obs: str, tool: str, text: str) -> None:
    v = g.validate(text)
    if v:
        note(obs, f"compact grammar violation {tool}: {v[:2]}")


def budget_check(obs: str, tool: str, d: dict, mt: int) -> None:
    b = d.get("budget") or {}
    u = b.get("used_tokens_est")
    if isinstance(u, int) and u > mt:
        note(obs, f"{tool} used_tokens_est {u} > max_tokens {mt}")


# ------------------------------------------------------------------ 共通の補助
PAY = "src/shop/services/payment.py"
LIM = "src/shop/services/limits.py"
REC = "src/shop/models/receipt.py"
CRLF = "src/shop/models/crlf_bom.py"
BOM_LINT = "src/shop/models/bom_lint.py"  # BOM + CRLF の 1 行目に未使用 import（ruff の列の検査用。コピーにだけ置く）


def prep_poly(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(POLY_SRC, root)
    (root / BOM_LINT).write_bytes(b"\xef\xbb\xbfimport os, sys\r\n")
    (root / "src/shop/models/lint_plain.py").write_bytes(b"import os, sys\n")  # 対照（BOM も CRLF も無い）
    return root


def ast_defs(path: Path) -> dict[str, tuple[int, int]]:
    tree = ast.parse(path.read_bytes())
    out: dict[str, tuple[int, int]] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + ch.name
                start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                out[q] = (start, ch.end_lineno or ch.lineno)
                visit(ch, q + ".")

    visit(tree, "")
    return out


def tgt(path: str, name: str) -> dict:
    return {"path": path, "symbol_name": name}


async def wait_lsp(s: Sess, secs: float = 40) -> str:
    st = "?"
    t0 = time.time()
    while time.time() - t0 < secs:
        d, _ = await s.j("get_diagnostics", {"paths": [PAY], "min_severity": "hint"})
        st = (d.get("_resolution_meta") or {}).get("lsp_status", "?")
        srcs = [(x["server"], x["status"]) for x in ((d.get("data") or {}).get("lsp_sources") or [])]
        if srcs and all(x[1] in ("ready", "unavailable") for x in srcs):
            return f"{st} {srcs}"
        await asyncio.sleep(2)
    return f"timeout {st}"


def refs_by_ast(root: Path, name: str) -> set[tuple[str, int]]:
    """py ファイルの識別子の一致（Name・Attribute・import の別名）を (path, line) で集める。"""
    out = set()
    for p in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(p.read_bytes())
        except SyntaxError:
            continue
        rel = p.relative_to(root).as_posix()
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and n.id == name:
                out.add((rel, n.lineno))
            elif isinstance(n, ast.Attribute) and n.attr == name:
                out.add((rel, n.lineno))
            elif isinstance(n, ast.alias) and n.name == name:
                out.add((rel, n.lineno))
    return out


def read_lines(root: Path, rel: str) -> list[str]:
    return (root / rel).read_text(encoding="utf-8-sig").splitlines()


# ------------------------------------------------------------------ 読み取り系（セッション 1）
async def run_readonly(root: Path) -> dict[str, Any]:
    res: dict[str, Any] = {}
    before = tree_hashes(root)
    async with Sess() as s:
        await s.setup(root)
        res["first_set_roots_ms"] = round(s.set_roots_ms)
        if s.set_roots_ms > 3000:
            note("cfg", f"最初の set_roots が {round(s.set_roots_ms)} ms かかった（roots 未宣言のクライアント）")
        res["lsp_wait"] = await wait_lsp(s)
        note("lsp", f"wait_lsp: {res['lsp_wait']}")
        for fn in (obs_t03, obs_t04, obs_t06, obs_t07, obs_t08, obs_t19_t20, obs_unsup):
            try:
                await fn(s, root, res)
            except Exception as e:  # noqa: BLE001
                note(fn.__name__, f"台本が例外で止まった: {type(e).__name__}: {e}")
    after = tree_hashes(root)
    if before != after:
        note("ro", f"読み取り系の呼び出しでファイルが変わった: {[k for k in after if before.get(k) != after[k]]}")
    return res


async def obs_t03(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {}
    d, _ = await s.j("search_workspace_symbols", {"query": "process_charge", "match": "exact"})
    names = [x["name"] for x in d["data"]["symbols"]]
    r["exact"] = names
    if any(n != "process_charge" for n in names) or not names:
        note("t03", f"match=exact なのに name が query と違う/空: {names}")
    d, _ = await s.j("search_workspace_symbols", {"query": "e", "limit": 3})
    if d["page"]["returned"] > 3:
        note("t03", f"limit=3 なのに returned={d['page']['returned']}")
    d, _ = await s.j("search_workspace_symbols", {"query": "a", "symbol_kinds": ["Class"], "limit": 50})
    ks = {x["kind"] for x in d["data"]["symbols"]}
    r["kinds"] = sorted(ks)
    if ks - {"Class"}:
        note("t03", f"symbol_kinds=[Class] なのに {ks}")
    d, _ = await s.j("search_workspace_symbols", {"query": "retry"})
    r["rank_retry"] = [(x["qualified_name"], x.get("score")) for x in d["data"]["symbols"]][:6]
    if d["data"]["symbols"] and d["data"]["symbols"][0]["name"] != "retry":
        note("t03", f"fuzzy 'retry' の先頭が完全一致でない: {r['rank_retry'][:3]}")
    sc = [x.get("score") for x in d["data"]["symbols"]]
    if sc != sorted(sc, reverse=True):
        note("t03", f"score が降順でない: {sc}")
    d, _ = await s.j("search_workspace_symbols", {"query": "Decimal(amount)", "match": "exact"})
    r["fallback_probe"] = {"fallback_used": (d.get("data") or {}).get("fallback_used"), "degraded": d["_resolution_meta"]["is_degraded"], "page": d["page"]}
    if (d.get("data") or {}).get("fallback_used") and not d["_resolution_meta"]["is_degraded"]:
        note("t03", "fallback_used=true なのに is_degraded=false")
    txt, _ = await s.c("search_workspace_symbols", {"query": "Decimal(amount)", "match": "exact"})
    if (d.get("data") or {}).get("fallback_used") and not txt.split("\n")[1].startswith("! "):
        note("t03", f"fallback_used=true の compact でヘッダ直後が ! 行でない: {txt.splitlines()[1][:80]!r}")
    for q in ("payment", "Receipt", "retry", "Decimal(amount)"):
        txt, _ = await s.c("search_workspace_symbols", {"query": q})
        check_compact("t03", f"search({q})", txt)
    for a, label in (({"query": ""}, "empty_query"), ({"query": "(", "match": "regex"}, "bad_regex"),
                     ({"query": "x", "scope_paths": ["../x"]}, "scope_outside"), ({"query": "x", "limit": 0}, "limit0"),
                     ({"query": "x", "symbol_kinds": ["NoSuchKind"]}, "bad_kind")):
        d, _ = await s.j("search_workspace_symbols", a)
        r.setdefault("errors", {})[label] = (d.get("ok"), (d.get("error") or {}).get("code"))
    seen, cur, pages = [], None, 0
    while pages < 40:
        a = {"query": "e", "limit": 200, "max_tokens": 300}
        if cur:
            a["cursor"] = cur
        d, _ = await s.j("search_workspace_symbols", a)
        if not d.get("ok"):
            note("t03", f"cursor 走査でエラー {d.get('error')}")
            break
        budget_check("t03", "search(max_tokens=300)", d, 300)
        seen += [x["urn"] for x in d["data"]["symbols"]]
        pages += 1
        cur = d["page"].get("next_cursor")
        if not cur:
            break
    d, _ = await s.j("search_workspace_symbols", {"query": "e", "limit": 200, "max_tokens": BIG})
    full = [x["urn"] for x in d["data"]["symbols"]]
    r["pages"] = {"n": pages, "got": len(seen), "dups": len(seen) - len(set(seen)), "full": len(full)}
    if len(seen) != len(set(seen)) or set(seen) != set(full):
        note("t03", f"cursor 走査と一括が違う: pages={pages} got={len(seen)} uniq={len(set(seen))} full={len(full)}")
    ac: dict[str, set[str]] = {}
    for p in root.rglob("*.py"):
        for q in ast_defs(p):
            ac.setdefault(q.split(".")[-1], set()).add(p.relative_to(root).as_posix())
    for name in ("LimitTable", "retry", "Receipt", "crlf_function"):
        d, _ = await s.j("search_workspace_symbols", {"query": name, "match": "exact"})
        got = {x["path"] for x in d["data"]["symbols"]}
        if got != ac.get(name, set()):
            note("t03", f"exact {name}: 返り {got} と ast {ac.get(name)} が違う")
    res["t03"] = r
    save("t03", r)


async def obs_t04(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {"checked": 0, "range_mismatch": [], "bodies_mismatch": []}
    for rel in (PAY, LIM, REC):
        for q, (a, b) in ast_defs(root / rel).items():
            d, _ = await s.j("resolve_symbols", {"targets": [tgt(rel, q)]})
            it = (d.get("data") or {}).get("items", [{}])[0]
            if not it.get("ok"):
                note("t04", f"{rel}#{q} が解決できない: {it.get('error') or d.get('error')}")
                continue
            r["checked"] += 1
            rg = it["definition"]["range"]
            if (rg["start_line"], rg["end_line"]) != (a, b):
                r["range_mismatch"].append((rel, q, (rg["start_line"], rg["end_line"]), (a, b)))
            urn = it["symbol"]["urn"]
            bd, _ = await s.j("get_symbol_bodies", {"targets": [{"urn": urn}], "context_lines": 0})
            bi = (bd.get("data") or {}).get("items", [{}])[0]
            rr = bi.get("range_returned")
            if rr and (rr["start_line"], rr["end_line"]) != (rg["start_line"], rg["end_line"]):
                r["bodies_mismatch"].append((rel, q, rr, rg))
            for td in it.get("type_definitions") or []:
                d2, _ = await s.j("resolve_symbols", {"targets": [{"urn": td["urn"]}]})
                i2 = (d2.get("data") or {}).get("items", [{}])[0]
                if not i2.get("ok") and not td.get("is_external"):
                    note("t04", f"type_definitions の urn が解決できない: {td['urn']} {i2.get('error')}")
    for k in ("range_mismatch", "bodies_mismatch"):
        if r[k]:
            note("t04", f"{k} {len(r[k])} 件: {r[k][:2]}")
    d, _ = await s.j("resolve_symbols", {"targets": [tgt("web/src/payment.ts", "charge"), tgt(PAY, "PaymentProcessor"), tgt(PAY, "NoSuch")]})
    its = (d.get("data") or {}).get("items") or []
    r["mixed"] = [(i.get("ok"), (i.get("error") or {}).get("code"), len((i.get("error") or {}).get("candidates") or i.get("candidates") or [])) for i in its]
    if len(its) != 3 or its[1].get("ok") is not True:
        note("t04", f"混在呼び出しの形が想定と違う: {r['mixed']}")
    if its and (its[0].get("error") or {}).get("code") != "E_TARGET_AMBIGUOUS":
        note("t04", f"TS の charge が E_TARGET_AMBIGUOUS でない: {its[0].get('error')}")
    if len(its) > 2 and (its[2].get("error") or {}).get("code") != "E_TARGET_NOT_FOUND":
        note("t04", f"存在しないシンボルが E_TARGET_NOT_FOUND でない: {its[2].get('error')}")
    for a, label in (({"targets": []}, "empty"), ({"targets": [{"path": "../x.py", "symbol_name": "a"}]}, "outside"),
                     ({"targets": [{}]}, "no_selector"), ({"targets": [{"urn": "lk://nope"}]}, "bad_urn"),
                     ({"targets": [{"path": PAY, "symbol_name": "PaymentProcessor", "line": 0}]}, "line0"),
                     ({"targets": [tgt(PAY, "PaymentProcessor")] * 21}, "21_targets")):
        d, _ = await s.j("resolve_symbols", a)
        code = (d.get("error") or {}).get("code") or (((d.get("data") or {}).get("items") or [{}])[0].get("error") or {}).get("code")
        r.setdefault("errors", {})[label] = (d.get("ok"), code)
    d, _ = await s.j("resolve_symbols", {"targets": [tgt(PAY, "PaymentProcessor.process_charge")]})
    m = d["_resolution_meta"]
    r["meta"] = m
    if m["lsp_status"] == "ready" and m["confidence_level"] != "exact":
        note("t04", f"lsp ready なのに confidence={m['confidence_level']}")
    if m["lsp_status"] != "ready" and m["confidence_level"] == "exact":
        note("t04", f"lsp={m['lsp_status']} なのに confidence=exact")
    if (m["confidence_level"] != "exact") != m["is_degraded"]:
        note("t04", f"confidence={m['confidence_level']} と is_degraded={m['is_degraded']} が噛み合わない")
    txt, _ = await s.c("resolve_symbols", {"targets": [tgt(PAY, "PaymentProcessor"), tgt(REC, "Receipt")]})
    check_compact("t04", "resolve_symbols", txt)
    res["t04"] = r
    save("t04", r)


async def obs_t06(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {}
    for rel, q in ((PAY, "PaymentProcessor"), (PAY, "PaymentProcessor.process_charge"), (LIM, "LimitTable"), (REC, "Receipt"),
                   (LIM, "retry"), (PAY, "PaymentRequest")):
        name = q.split(".")[-1]
        d, _ = await s.j("find_references_and_implementations", {"target": tgt(rel, q), "query_type": "references", "include_declaration": True, "max_tokens": BIG})
        if not d.get("ok"):
            note("t06", f"{q}: {d.get('error')}")
            continue
        data = d["data"]
        refs = data.get("references") or []
        got = {(x["path"], x["line"]) for x in refs}
        exp = refs_by_ast(root, name)
        r[q] = {"got": len(got), "ast": len(exp), "missing": sorted(exp - got)[:6], "extra": sorted(got - exp)[:6],
                "level": d["_resolution_meta"]["confidence_level"], "keys": sorted(data.keys())}
        if exp - got:
            note("t06", f"{q}: ast の識別子参照が返りに無い {sorted(exp - got)[:4]}")
        for x in refs:
            line = read_lines(root, x["path"])[x["line"] - 1]
            if name not in line:
                note("t06", f"{q}: {x['path']}:{x['line']} の行に {name} が無い: {line.strip()[:60]}")
            col = x.get("col") or x.get("character")
            if col and line[col - 1:col - 1 + len(name)] != name:
                note("t06", f"{q}: {x['path']}:{x['line']}:{col} の列が名前を指さない: {line.strip()[:60]}")
        uses = {x.get("use") for x in refs}
        if uses - {"r", "w", "rw", "call", "type", "import", "impl", None}:
            note("t06", f"{q}: 不明な use {uses}")
        d2, _ = await s.j("find_references_and_implementations", {"target": tgt(rel, q), "query_type": "references", "include_tests": False, "max_tokens": BIG})
        if d2.get("ok") and any(x.get("is_test") or x["path"].startswith("tests/") for x in d2["data"].get("references") or []):
            note("t06", f"{q}: include_tests=false なのにテストの行がある")
        d3, _ = await s.j("find_references_and_implementations", {"target": tgt(rel, q), "query_type": "references", "include_declaration": False, "max_tokens": BIG})
        if d3.get("ok") and len(d3["data"].get("references") or []) > len(refs):
            note("t06", f"{q}: include_declaration=false の方が件数が多い")
        seen, cur, n = [], None, 0
        while n < 40:
            a = {"target": tgt(rel, q), "query_type": "references", "max_tokens": 250}
            if cur:
                a["cursor"] = cur
            dp, _ = await s.j("find_references_and_implementations", a)
            if not dp.get("ok"):
                note("t06", f"{q}: cursor 走査エラー {dp.get('error')}")
                break
            budget_check("t06", f"{q} max_tokens=250", dp, 250)
            seen += [(x["path"], x["line"], x.get("col")) for x in dp["data"].get("references") or []]
            n += 1
            cur = dp["page"].get("next_cursor")
            if not cur:
                break
        r[q]["pages"] = {"n": n, "seen": len(seen), "dups": len(seen) - len(set(seen))}
        if len(seen) != len(set(seen)):
            note("t06", f"{q}: cursor 走査で重複 {len(seen) - len(set(seen))}")
        txt, _ = await s.c("find_references_and_implementations", {"target": tgt(rel, q)})
        check_compact("t06", f"compact({q})", txt)
    for a, label in (({"target": {}}, "empty_target"), ({"target": tgt(PAY, "NoSuch")}, "nosuch"),
                     ({"target": tgt("../x.py", "a")}, "outside"), ({"target": tgt(PAY, "PaymentProcessor"), "query_type": "bogus"}, "bad_query_type"),
                     ({"target": tgt(PAY, "PaymentProcessor"), "use_kinds": ["call"]}, "use_kinds_call")):
        d, _ = await s.j("find_references_and_implementations", a)
        r.setdefault("errors", {})[label] = (d.get("ok"), (d.get("error") or {}).get("code"), len((d.get("data") or {}).get("references") or []))
    d, _ = await s.j("find_references_and_implementations", {"target": {"urn": "lk://_ext/decimal#Decimal"}})
    r["external_decimal"] = (d.get("ok"), (d.get("error") or {}).get("code"), len((d.get("data") or {}).get("references") or []))
    if not d.get("ok") or not (d.get("data") or {}).get("references"):
        note("t06", f"外部シンボル lk://_ext/decimal#Decimal の参照が返らない: {d.get('error')}")
    res["t06"] = r
    save("t06", r)


async def obs_t07(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {}
    cases = [(LIM, "retry"), (LIM, "schedule"), (PAY, "PaymentProcessor._validate_limit"), (PAY, "PaymentProcessor.process_charge"), (LIM, "LimitTable.get")]
    for rel, q in cases:
        for direction in ("incoming", "outgoing", "both"):
            for depth in (1, 3):
                d, _ = await s.j("query_call_hierarchy", {"target": tgt(rel, q), "direction": direction, "depth": depth, "max_tokens": BIG})
                label = f"{q}/{direction}/d{depth}"
                if not d.get("ok"):
                    note("t07", f"{label}: {d.get('error')}")
                    continue
                nodes = d["data"].get("nodes") or []
                if any(n["depth"] > depth for n in nodes):
                    note("t07", f"{label}: depth 超過 {max(n['depth'] for n in nodes)}")
                childcount: dict[tuple[str, int], int] = {}
                for n in nodes:
                    if n.get("parent_urn"):
                        k2 = (n["parent_urn"], n["depth"] - 1)
                        childcount[k2] = childcount.get(k2, 0) + 1
                for n in nodes:
                    if n.get("cycle") and childcount.get((n["urn"], n["depth"])):
                        note("t07", f"{label}: cycle=true のノード {n['name']}(depth {n['depth']}) が children を持つ")
                    if n.get("is_external") and childcount.get((n["urn"], n["depth"])):
                        note("t07", f"{label}: external の葉 {n['name']} が children を持つ")
                    cp, cl = n.get("call_site_path"), n.get("call_site_line")
                    if cp and cl:
                        line = read_lines(root, cp)[cl - 1]
                        par = next((m for m in nodes if m["urn"] == n.get("parent_urn")), None)
                        tname = n["name"] if n["direction"] == "outgoing" else ((par or {}).get("name") or d["data"]["root"]["name"])
                        tname = tname.split(".")[-1]
                        if tname not in line:
                            note("t07", f"{label}: {cp}:{cl} の行に {tname} が無い: {line.strip()[:60]}")
                    if not n.get("is_external"):
                        d2, _ = await s.j("resolve_symbols", {"targets": [{"urn": n["urn"]}]})
                        it = (d2.get("data") or {}).get("items", [{}])[0]
                        if not it.get("ok"):
                            note("t07", f"{label}: ノード {n['urn']} が resolve_symbols で解決できない {it.get('error')}")
                budget_check("t07", label, d, 2000)
                r[label] = len(nodes)
    txt, _ = await s.c("query_call_hierarchy", {"target": tgt(LIM, "retry"), "direction": "both", "depth": 3})
    check_compact("t07", "compact(retry)", txt)
    r["retry_compact"] = txt[:900]
    d, _ = await s.j("query_call_hierarchy", {"target": tgt(PAY, "PaymentProcessor.process_charge"), "direction": "incoming", "depth": 1})
    r["when"] = {n["name"]: n.get("when") for n in d["data"]["nodes"]}
    for k, v in {"create_order": "req.amount > 0 and not paid", "retry_order": "attempt < 3"}.items():
        if r["when"].get(k) != v:
            note("t07", f"when: {k} 期待 {v!r} / 実測 {r['when'].get(k)!r}")
    d, _ = await s.j("query_call_hierarchy", {"target": tgt("src/shop/api/routes.py", "create_order"), "direction": "outgoing", "depth": 2, "exclude_external": False})
    r["external"] = [(n["name"], n.get("is_external"), n["urn"][:40]) for n in d["data"]["nodes"]] if d.get("ok") else d.get("error")
    for a, label in (({"target": tgt(PAY, "NoSuch"), "direction": "both"}, "nosuch"), ({"target": tgt(LIM, "retry"), "direction": "up"}, "bad_dir"),
                     ({"target": tgt(LIM, "retry"), "direction": "both", "depth": 0}, "depth0"), ({"target": tgt(LIM, "retry"), "direction": "both", "depth": 6}, "depth6"),
                     ({"target": tgt(PAY, "PaymentRequest"), "direction": "both"}, "class_target")):
        d, _ = await s.j("query_call_hierarchy", a)
        r.setdefault("errors", {})[label] = (d.get("ok"), (d.get("error") or {}).get("code"))
    res["t07"] = r
    save("t07", r)


async def obs_t08(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {}
    before = tree_hashes(root)
    d, _ = await s.j("get_diagnostics", {"paths": [PAY, BOM_LINT, CRLF, "src/shop/models/lint_plain.py"], "min_severity": "hint", "include_code_actions": True})
    r["lsp_sources"] = (d.get("data") or {}).get("lsp_sources") or d
    r["meta"] = d.get("_resolution_meta")
    diags = (d.get("data") or {}).get("diagnostics") or []
    r["n"] = len(diags)
    r["diags"] = diags[:12]
    order = {"error": 0, "warning": 1, "information": 2, "hint": 3}
    for x in diags:
        lines = read_lines(root, x["path"])
        if not (1 <= x["line"] <= len(lines)):
            note("t08", f"診断の line が範囲外 {x['path']}:{x['line']}")
            continue
        if x.get("snippet") is not None and x["snippet"].strip() != lines[x["line"] - 1].strip():
            note("t08", f"snippet が行と違う {x['path']}:{x['line']} {x['snippet']!r} / {lines[x['line'] - 1]!r}")
    bl = [x for x in diags if x["path"] == BOM_LINT]
    r["bom_lint"] = [(x.get("line"), x.get("character") or x.get("col") or x.get("column"), x.get("code"), x.get("source")) for x in bl]
    if not bl:
        note("t08", f"BOM+CRLF の {BOM_LINT}（1 行目 `import os, sys`）に診断が 0 件（lsp_sources={r['lsp_sources']}）")
    for x in bl:
        col = x.get("character") or x.get("col") or x.get("column")
        if x.get("code") == "F401" and "os" in (x.get("message") or "") and col != 8:
            note("t08", f"BOM 付き行 0 の F401(os) の列が 8 でない: {col}")
        if x.get("code") == "F401" and "sys" in (x.get("message") or "") and col != 12:
            note("t08", f"BOM 付き行 0 の F401(sys) の列が 12 でない: {col}")
    pe = [x for x in diags if x["path"] == PAY]
    r["payment"] = [(x["line"], x.get("severity"), x.get("source"), x.get("code")) for x in pe]
    if not any(x["line"] == 62 for x in pe):
        note("t08", f"payment.py 62 行（意図的な型エラー）の診断が無い（返り {[(x['line'], x.get('code')) for x in pe]}）")
    for sev in ("error", "warning", "information", "hint"):
        d2, _ = await s.j("get_diagnostics", {"paths": [PAY, BOM_LINT], "min_severity": sev})
        bad = [x for x in (d2.get("data") or {}).get("diagnostics") or [] if order[x["severity"]] > order[sev]]
        if bad:
            note("t08", f"min_severity={sev} なのに下位の診断: {[(x['severity'], x['line']) for x in bad][:3]}")
        r[f"sev_{sev}"] = len((d2.get("data") or {}).get("diagnostics") or [])
    ids = [(x["path"], x["line"], ca.get("id")) for x in diags for ca in (x.get("code_actions") or [])]
    r["code_action_ids"] = ids[:6]
    for pth, ln, cid in ids[:3]:
        d3, _ = await s.j("apply_code_action", {"path": pth, "line": ln, "action_kind_or_id": cid, "dry_run": True})
        if (d3.get("error") or {}).get("code") == "E_TARGET_NOT_FOUND":
            note("t08", f"診断の code_actions id {cid} が apply_code_action で E_TARGET_NOT_FOUND")
    for a, label in (({"paths": ["../x.py"]}, "outside"), ({"paths": ["nope.py"]}, "missing"), ({"min_severity": "fatal"}, "bad_sev"),
                     ({"sources": ["nope"]}, "bad_source")):
        d4, _ = await s.j("get_diagnostics", a)
        r.setdefault("errors", {})[label] = (d4.get("ok"), (d4.get("error") or {}).get("code"))
    txt, _ = await s.c("get_diagnostics", {"paths": [PAY, BOM_LINT], "min_severity": "hint"})
    check_compact("t08", "get_diagnostics", txt)
    r["compact"] = txt[:1500]
    if before != tree_hashes(root):
        note("t08", "get_diagnostics の前後でファイルが変わった")
    res["t08"] = r
    save("t08", r)


async def obs_t19_t20(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {}
    d, _ = await s.j("get_project_db_schema", {"include_sample_queries": True, "include_stats": True, "max_tokens": BIG})
    tables = d["data"]["tables"]
    names = {t["name"] for t in tables}
    r["views"] = sorted(names)
    if len(names) != 10:
        note("t19", f"tables 省略時のビュー数が 10 でない: {len(names)}")
    for t in tables:
        q, _ = await s.j("query_project_db", {"query": f"SELECT COUNT(*) AS n FROM {t['name']}"})
        n = (q.get("data") or {}).get("rows", [{}])[0].get("n") if q.get("ok") else None
        if t.get("row_count_exact") and n != t["row_count"]:
            note("t19", f"{t['name']}: row_count={t['row_count']} と COUNT(*)={n} が違う")
        cols = [c["name"] if isinstance(c, dict) else c for c in t["columns"]]
        q2, _ = await s.j("query_project_db", {"query": f"SELECT * FROM {t['name']} LIMIT 1"})
        got = [c["name"] for c in (q2.get("data") or {}).get("columns") or []]
        if q2.get("ok") and set(got) != set(cols):
            note("t19", f"{t['name']}: columns の差 {sorted(set(cols) ^ set(got))}（スキーマ記述と実ビュー）")
    cov = d["data"]["coverage"]
    if round(sum(cov.values()), 3) != 1.0:
        note("t19", f"coverage の和が 1.0 でない: {cov} = {sum(cov.values())}")
    for sq in d["data"].get("sample_queries") or []:
        q, _ = await s.j("query_project_db", {"query": sq["sql"], "params": sq.get("params_example") or {}})
        if not q.get("ok"):
            note("t19", f"sample_queries {sq.get('id')} が T-20 で失敗: {(q.get('error') or {}).get('code')} {str(q.get('error'))[:140]}")
    d2, _ = await s.j("get_project_db_schema", {"tables": ["files", "nosuch"]})
    r["tables_filter"] = (d2.get("ok"), (d2.get("error") or {}).get("code"), [t["name"] for t in (d2.get("data") or {}).get("tables") or []])
    txt, _ = await s.c("get_project_db_schema", {})
    check_compact("t19", "get_project_db_schema", txt)
    rej = ["INSERT INTO files(path) VALUES('x')", "UPDATE files SET path='x'", "DELETE FROM files", "CREATE TABLE z(a)", "DROP VIEW files",
           "ALTER TABLE files ADD COLUMN z", "ATTACH DATABASE 'x.db' AS x", "PRAGMA table_info(files)", "SELECT 1; SELECT 2", "SELECT 1; DROP VIEW files",
           "  insert into files values(1)", "WITH x AS (SELECT 1) DELETE FROM files", "/* c */ DELETE FROM files", "SELECT load_extension('x')",
           "VACUUM", "REPLACE INTO files(path) VALUES('x')"]
    r["reject"] = {}
    for q in rej:
        qd, _ = await s.j("query_project_db", {"query": q})
        code = (qd.get("error") or {}).get("code")
        r["reject"][q] = (qd.get("ok"), code)
        if qd.get("ok") or code != "E_QUERY_REJECTED":
            note("t20", f"拒否すべき文が {('ok' if qd.get('ok') else code)}: {q!r}")
    for q in ("SELECT * FROM sqlite_master", "SELECT * FROM files_raw", "SELECT name FROM pragma_table_info('files')"):
        qd, _ = await s.j("query_project_db", {"query": q})
        r["reject"][q] = (qd.get("ok"), (qd.get("error") or {}).get("code"), (qd.get("data") or {}).get("row_count"))
        if qd.get("ok"):
            note("t20", f"公開ビュー以外 {q!r} が ok（row_count={(qd.get('data') or {}).get('row_count')}）")
    full_n = (await s.j("query_project_db", {"query": "SELECT COUNT(*) AS n FROM symbols"}))[0]["data"]["rows"][0]["n"]
    for lim in (1, 5, full_n - 1, full_n, full_n + 1):
        q, _ = await s.j("query_project_db", {"query": "SELECT urn FROM symbols", "limit": lim, "max_tokens": BIG})
        if not q.get("ok"):
            note("t20", f"limit={lim}: {q.get('error')}")
            continue
        dq = q["data"]
        if dq["row_count"] > lim or dq["truncated"] != (full_n > lim):
            note("t20", f"limit={lim} full={full_n}: row_count={dq['row_count']} truncated={dq['truncated']}")
    pcases = [({"query": "SELECT * FROM files WHERE path = :p", "params": {"p": "README.md"}}, "ok"),
              ({"query": "SELECT * FROM files WHERE path = :p", "params": {}}, "missing_param"),
              ({"query": "SELECT * FROM files WHERE path = :p", "params": {"p": "x", "extra": 1}}, "extra_param"),
              ({"query": "SELECT * FROM files WHERE path = ?", "params": {"p": "x"}}, "qmark"),
              ({"query": "SELECT 1", "limit": 0}, "limit0"), ({"query": "SELECT 1", "timeout_ms": 0}, "timeout0"),
              ({"query": "SELECT nope FROM files"}, "bad_column"), ({"query": "SELECT * FROM nosuch"}, "bad_table"), ({"query": ""}, "empty")]
    r["params"] = {}
    for a, label in pcases:
        q, _ = await s.j("query_project_db", a)
        r["params"][label] = (q.get("ok"), (q.get("error") or {}).get("code"))
    for label, want in (("missing_param", "E_INVALID_ARGUMENT"), ("extra_param", "E_INVALID_ARGUMENT")):
        if r["params"][label][1] != want:
            note("t20", f"{label}: 期待 {want} / 実測 {r['params'][label]}")
    q, dt = await s.j("query_project_db", {"query": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c", "timeout_ms": 200})
    r["timeout"] = (q.get("ok"), (q.get("error") or {}).get("code"), (q.get("data") or {}).get("elapsed_ms"), round(dt))
    if q.get("ok"):
        note("t20", "無限再帰 CTE が timeout_ms=200 で打ち切られず ok を返した")
    elif dt > 1500:
        note("t20", f"timeout_ms=200 の無限再帰 CTE の応答に {round(dt)} ms かかった: {q.get('error')}")
    txt, _ = await s.c("query_project_db", {"query": "SELECT path, line_count FROM files ORDER BY line_count DESC LIMIT 3"})
    check_compact("t20", "query_project_db", txt)
    res["t19_t20"] = r
    save("t19_t20", r)


UNSUP = [("analyze_change_impact", {"target": tgt(PAY, "PaymentProcessor")}),
         ("structural_search_replace", {"language": "python", "pattern": "print($X)"}),
         ("find_code_paths", {"source": tgt(PAY, "PaymentProcessor.process_charge"), "sink": tgt(LIM, "LimitTable.get")}),
         ("extract_code_contracts", {"path": PAY}),
         ("verify_contract", {"target": tgt(PAY, "PaymentProcessor.apply_discount"), "postconditions": ["result >= 0"]}),
         ("get_symbol_history", {"target": tgt(PAY, "PaymentProcessor")}),
         ("analyze_git_diff_symbols", {})]


async def obs_unsup(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {}
    for tool, a in UNSUP:
        d, dt = await s.j(tool, a)
        e = d.get("error") or {}
        txt, _ = await s.c(tool, a)
        r[tool] = {"ok": d.get("ok"), "code": e.get("code"), "message": (e.get("message") or "")[:120], "details": e.get("details"),
                   "hint": e.get("hint"), "ms": round(dt), "compact": txt[:400]}
        if e.get("code") != "E_UNSUPPORTED":
            note("unsup", f"{tool}: E_UNSUPPORTED でない: {d.get('ok')} {e.get('code')}")
        elif (e.get("details") or {}).get("reason") != "not_in_milestone" or not (e.get("details") or {}).get("milestone"):
            note("unsup", f"{tool}: details.reason/milestone が仕様（not_in_milestone）と違う: {e}")
        if not txt.startswith("# " + tool + " error E_UNSUPPORTED"):
            note("unsup", f"{tool}: compact のヘッダが想定と違う: {txt[:100]!r}")
        check_compact("unsup", tool, txt)
    res["unsup"] = r
    save("unsup", r)




# ------------------------------------------------------------------ 編集系（セッション 2。コピーにだけ書く）
def eol_bom(b: bytes) -> tuple[int, int, bool]:
    crlf = b.count(b"\r\n")
    lf = b.count(b"\n") - crlf
    return crlf, lf, b.startswith(b"\xef\xbb\xbf")


async def body_of(s: Sess, rel: str, q: str) -> tuple[str, dict]:
    d, _ = await s.j("get_symbol_bodies", {"targets": [tgt(rel, q)], "context_lines": 0, "with_line_numbers": False, "include_referenced_symbols": False})
    it = d["data"]["items"][0]
    return it["code"], it


def check_applied(obs: str, label: str, root: Path, d: dict, before: dict[str, str]) -> None:
    """applied:true の事後条件: files_changed の sha256_after = 実ファイル、他のファイルは不変。"""
    data = d.get("data") or {}
    fc = data.get("files_changed") or []
    after = tree_hashes(root)
    changed = {k for k in after if before.get(k) != after[k]} | {k for k in before if k not in after}
    if data.get("applied"):
        listed = {f["path"] for f in fc}
        if listed != changed:
            note(obs, f"{label}: files_changed {sorted(listed)} と実際に変わったファイル {sorted(changed)} が違う")
        for f in fc:
            if after.get(f["path"]) != f.get("sha256_after"):
                note(obs, f"{label}: {f['path']} の sha256_after が実ファイルと違う")
            if before.get(f["path"]) != f.get("sha256_before"):
                note(obs, f"{label}: {f['path']} の sha256_before が編集前と違う")
    elif changed:
        note(obs, f"{label}: applied=false/エラーなのにファイルが変わった {sorted(changed)}")


async def run_edit(root: Path) -> dict[str, Any]:
    res: dict[str, Any] = {}
    async with Sess() as s:
        await s.setup(root)
        res["lsp_wait"] = await wait_lsp(s)
        note("lsp", f"edit-session wait_lsp: {res['lsp_wait']}")
        for fn in (obs_t10, obs_t11, obs_t13, obs_bom_edit):
            try:
                await fn(s, root, res)
            except Exception as e:  # noqa: BLE001
                note(fn.__name__, f"台本が例外で止まった: {type(e).__name__}: {e}")
    return res


async def obs_t10(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {}
    # 1 恒等編集（LF の py と BOM+CRLF の py）でバイト一致
    for rel, q in ((PAY, "PaymentProcessor._validate_limit"), (PAY, "PaymentRequest"), (LIM, "retry"), (CRLF, "crlf_function"), (CRLF, "CrlfClass")):
        before = tree_hashes(root)
        code, it = await body_of(s, rel, q)
        d, _ = await s.j("edit_symbol_body", {"target": tgt(rel, q), "operation": "replace_body", "new_code": code})
        label = f"identity {rel}#{q}"
        data = d.get("data") or {}
        r[label] = {"ok": d.get("ok"), "applied": data.get("applied"), "err": (d.get("error") or {}).get("code"), "syntax": data.get("syntax_check")}
        if not d.get("ok"):
            note("t10", f"{label}: {d.get('error')}")
            continue
        after = tree_hashes(root)
        if before != after:
            note("t10", f"{label}: 恒等編集でバイトが変わった {[k for k in after if before.get(k) != after[k]]}")
        check_applied("t10", label, root, d, before)
    # 2 dry_run
    before = tree_hashes(root)
    d, _ = await s.j("edit_symbol_body", {"target": tgt(PAY, "PaymentProcessor.apply_discount"), "operation": "replace_body",
                                           "new_code": "def apply_discount(self, price: Decimal, rate: int) -> Decimal:\n    return price\n", "dry_run": True})
    r["dry_run"] = {"ok": d.get("ok"), "applied": (d.get("data") or {}).get("applied"), "err": d.get("error")}
    if before != tree_hashes(root):
        note("t10", "dry_run でファイルが変わった")
    if d.get("ok") and (d["data"].get("applied") is not False):
        note("t10", f"dry_run なのに applied={d['data'].get('applied')}")
    # 3 実適用: replace_body → 事後条件 1・3
    new = "def apply_discount(self, price: Decimal, rate: int) -> Decimal:\n    \"\"\"割引後の価格。\"\"\"\n    return price * (100 - rate) / 100\n"
    before = tree_hashes(root)
    d, _ = await s.j("edit_symbol_body", {"target": tgt(PAY, "PaymentProcessor.apply_discount"), "operation": "replace_body", "new_code": new})
    data = d.get("data") or {}
    r["apply"] = {"ok": d.get("ok"), "applied": data.get("applied"), "err": d.get("error"), "range": data.get("new_range"), "syntax": data.get("syntax_check"), "diag": data.get("new_diagnostics")}
    if d.get("ok") and data.get("applied"):
        check_applied("t10", "replace_body apply_discount", root, d, before)
        d2, _ = await s.j("resolve_symbols", {"targets": [{"urn": data["target_urn"]}]})
        it = (d2.get("data") or {}).get("items", [{}])[0]
        rg = (it.get("definition") or {}).get("range")
        if not it.get("ok") or not rg or (rg["start_line"], rg["end_line"]) != (data["new_range"]["start_line"], data["new_range"]["end_line"]):
            note("t10", f"適用後の resolve_symbols の範囲 {rg} が new_range {data.get('new_range')} と違う ({it.get('error')})")
        defs = ast_defs(root / PAY)
        if defs.get("PaymentProcessor.apply_discount") != (data["new_range"]["start_line"], data["new_range"]["end_line"]):
            note("t10", f"new_range {data['new_range']} と ast の範囲 {defs.get('PaymentProcessor.apply_discount')} が違う")
        # 型エラー行が消えて診断が減る（new_diagnostics）
        txt = (root / PAY).read_text(encoding="utf-8")
        if "意図的な型エラー" in txt:
            note("t10", "replace_body 後も旧本体が残っている")
    else:
        note("t10", f"replace_body が適用されない: {d.get('error')} {data.get('applied')}")
    # 4 insert_after / insert_before / insert_member / delete_symbol
    steps = [("insert_after", PAY, "PaymentProcessor.apply_discount", "def helper_after(self) -> int:\n    return 1\n"),
             ("insert_before", PAY, "PaymentProcessor.apply_discount", "def helper_before(self) -> int:\n    return 2\n"),
             ("insert_member", PAY, "PaymentProcessor", "def member_end(self) -> int:\n    return 3\n"),
             ("delete_symbol", PAY, "PaymentProcessor.helper_after", None)]
    for op, rel, q, code in steps:
        before = tree_hashes(root)
        a = {"target": tgt(rel, q), "operation": op}
        if code:
            a["new_code"] = code
        d, _ = await s.j("edit_symbol_body", a)
        data = d.get("data") or {}
        r[op] = {"ok": d.get("ok"), "applied": data.get("applied"), "err": d.get("error"), "range": data.get("new_range")}
        if not d.get("ok") or not data.get("applied"):
            note("t10", f"{op}: 適用されない {d.get('error')}")
            continue
        check_applied("t10", op, root, d, before)
        try:
            ast.parse((root / PAY).read_bytes())
        except SyntaxError as e:
            note("t10", f"{op} 後の payment.py が構文エラー: {e}")
        if op != "delete_symbol":
            defs = ast_defs(root / PAY)
            nr = data.get("new_range")
            hit = [k for k, v in defs.items() if nr and v == (nr["start_line"], nr["end_line"])]
            if not hit:
                note("t10", f"{op}: new_range {nr} に一致する ast の定義が無い")
        else:
            d2, _ = await s.j("resolve_symbols", {"targets": [{"urn": data["target_urn"]}]})
            if (d2.get("data") or {}).get("items", [{}])[0].get("ok"):
                note("t10", "delete_symbol 後も target_urn が解決できる")
    # 5 エラー
    errs = {}
    for label, a in (("bad_syntax", {"target": tgt(LIM, "retry"), "operation": "replace_body", "new_code": "def retry(:\n  pass\n"}),
                     ("no_new_code", {"target": tgt(LIM, "retry"), "operation": "replace_body"}),
                     ("bad_op", {"target": tgt(LIM, "retry"), "operation": "explode", "new_code": "x"}),
                     ("path_only", {"target": {"path": LIM}, "operation": "delete_symbol"}),
                     ("nosuch", {"target": tgt(LIM, "nosuch"), "operation": "delete_symbol"}),
                     ("stale_sha", {"target": tgt(LIM, "retry"), "operation": "replace_body", "new_code": "def retry(attempt: int) -> int:\n    return attempt\n", "expected_file_sha256": "0" * 64}),
                     ("tab_space_mix", {"target": tgt(LIM, "retry"), "operation": "replace_body", "new_code": "def retry(attempt: int) -> int:\n\tif attempt:\n        return attempt\n    return 0\n"}),
                     ("outside", {"target": tgt("../x.py", "a"), "operation": "delete_symbol"}),
                     ("member_on_function", {"target": tgt(LIM, "retry"), "operation": "insert_member", "new_code": "x = 1\n"})):
        before = tree_hashes(root)
        d, _ = await s.j("edit_symbol_body", a)
        errs[label] = (d.get("ok"), (d.get("error") or {}).get("code"), (d.get("data") or {}).get("applied"))
        if before != tree_hashes(root):
            note("t10", f"エラー系 {label} でファイルが変わった")
        if d.get("ok") and (d.get("data") or {}).get("applied"):
            note("t10", f"エラーのはずの {label} が適用された")
    r["errors"] = errs
    want = {"bad_syntax": "E_SYNTAX_INVALID", "no_new_code": "E_INVALID_ARGUMENT", "bad_op": "E_INVALID_ARGUMENT", "path_only": "E_INVALID_ARGUMENT",
            "nosuch": "E_TARGET_NOT_FOUND", "stale_sha": "E_PRECONDITION_FAILED", "tab_space_mix": "E_INVALID_ARGUMENT", "outside": "E_INVALID_ARGUMENT",
            "member_on_function": "E_INVALID_ARGUMENT"}
    for k, v in want.items():
        if errs[k][1] != v:
            note("t10", f"{k}: 期待 {v} / 実測 {errs[k][1]}")
    txt, _ = await s.c("edit_symbol_body", {"target": tgt(LIM, "retry"), "operation": "replace_body", "new_code": "def retry(attempt: int) -> int:\n    if attempt >= 3:\n        return attempt\n    return schedule(attempt + 1)\n", "dry_run": True})
    check_compact("t10", "edit_symbol_body", txt)
    r["compact"] = txt[:700]
    res["t10"] = r
    save("t10", r)


async def obs_bom_edit(s: Sess, root: Path, res: dict) -> None:
    """BOM+CRLF のファイルへの編集で BOM・CRLF が保たれる（T-10 の事後条件 5）。"""
    r: dict[str, Any] = {}
    p = root / CRLF
    b0 = p.read_bytes()
    d, _ = await s.j("edit_symbol_body", {"target": tgt(CRLF, "crlf_function"), "operation": "replace_body",
                                           "new_code": "def crlf_function(x: int) -> int:\n    \"\"\"Triples x.\"\"\"\n    return x * 3\n"})
    b1 = p.read_bytes()
    r["before"], r["after"] = eol_bom(b0), eol_bom(b1)
    r["resp"] = {"ok": d.get("ok"), "applied": (d.get("data") or {}).get("applied"), "err": d.get("error")}
    c0, l0, bom0 = r["before"]
    c1, l1, bom1 = r["after"]
    if not d.get("ok"):
        note("bom", f"BOM+CRLF への replace_body が失敗: {d.get('error')}")
    else:
        if bom0 != bom1:
            note("bom", f"編集後に BOM が変わった {bom0}->{bom1}")
        if l1 > 0 or c1 == 0:
            note("bom", f"編集後の改行が CRLF のままでない（CRLF {c1} / LF {l1}。編集前 CRLF {c0} / LF {l0}）")
        if b"\r\r" in b1:
            note("bom", "編集後に CR CR がある")
    res["bom_edit"] = r
    save("bom_edit", r)


async def obs_t11(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {}
    before = tree_hashes(root)
    old, new = "PaymentRequest", "PaymentReq"
    refs_by_ast(root, old)
    d, _ = await s.j("find_references_and_implementations", {"target": tgt(PAY, old), "query_type": "references", "include_declaration": False, "max_tokens": BIG})
    nrefs = len((d.get("data") or {}).get("references") or [])
    r["t06_refs"] = nrefs
    r["lsp_status"] = (d.get("_resolution_meta") or {}).get("lsp_status")
    d, _ = await s.j("rename_symbol", {"target": tgt(PAY, old), "new_name": new, "dry_run": True})
    data = d.get("data") or {}
    r["dry"] = {"ok": d.get("ok"), "applied": data.get("applied"), "edits": data.get("edits_count"), "err": d.get("error"), "level": (d.get("_resolution_meta") or {}).get("confidence_level")}
    if before != tree_hashes(root):
        note("t11", "dry_run でファイルが変わった")
    for label, a in (("bad_name", {"target": tgt(PAY, old), "new_name": "1bad"}), ("keyword", {"target": tgt(PAY, old), "new_name": "class"}),
                     ("empty", {"target": tgt(PAY, old), "new_name": ""}), ("same", {"target": tgt(PAY, old), "new_name": old}),
                     ("conflict", {"target": tgt(PAY, old), "new_name": "PaymentProcessor"}), ("nosuch", {"target": tgt(PAY, "NoSuch"), "new_name": "X"}),
                     ("space", {"target": tgt(PAY, old), "new_name": "a b"})):
        b2 = tree_hashes(root)
        dd, _ = await s.j("rename_symbol", {**a, "dry_run": False})
        r.setdefault("errors", {})[label] = (dd.get("ok"), (dd.get("error") or {}).get("code"), (dd.get("data") or {}).get("applied"))
        if b2 != tree_hashes(root):
            note("t11", f"エラー系 {label} でファイルが変わった")
            before = tree_hashes(root)
        elif dd.get("ok") and (dd.get("data") or {}).get("applied"):
            note("t11", f"拒否すべき new_name {a['new_name']!r} が適用された")
    before = tree_hashes(root)
    d, _ = await s.j("rename_symbol", {"target": tgt(PAY, old), "new_name": new})
    data = d.get("data") or {}
    r["apply"] = {"ok": d.get("ok"), "applied": data.get("applied"), "edits": data.get("edits_count"), "err": d.get("error"), "urn_map": data.get("urn_map"),
                  "files": [f["path"] for f in data.get("files_changed") or []], "level": (d.get("_resolution_meta") or {}).get("confidence_level")}
    if d.get("ok") and data.get("applied"):
        check_applied("t11", "rename PaymentRequest", root, d, before)
        left = refs_by_ast(root, old)
        if left:
            note("t11", f"rename 後も旧名 {old} の識別子が残っている: {sorted(left)[:5]}")
        got = refs_by_ast(root, new)
        r["after_refs"] = len(got)
        if (d.get("_resolution_meta") or {}).get("confidence_level") == "exact" and data.get("edits_count") != nrefs + 1:
            note("t11", f"edits_count={data.get('edits_count')} が T-06 件数+1={nrefs + 1} と違う（exact）")
        um = data.get("urn_map") or {}
        for k, v in um.items():
            d2, _ = await s.j("resolve_symbols", {"targets": [{"urn": v}]})
            if not (d2.get("data") or {}).get("items", [{}])[0].get("ok"):
                note("t11", f"urn_map の値 {v} が解決できない")
            d3, _ = await s.j("resolve_symbols", {"targets": [{"urn": k}]})
            if (d3.get("data") or {}).get("items", [{}])[0].get("ok"):
                note("t11", f"urn_map の旧キー {k} が rename 後も解決できる")
        for f in root.rglob("*.py"):
            try:
                ast.parse(f.read_bytes())
            except SyntaxError as e:
                note("t11", f"rename 後に構文エラー {f.name}: {e}")
        # 文字列・コメント中の旧名（構造的 rename は触らないはず）
        # テストの呼び出しも変わっているか
        if old in (root / "tests/test_payment.py").read_text(encoding="utf-8"):
            note("t11", "tests/test_payment.py に旧名が残っている")
    else:
        note("t11", f"rename が適用されない: {d.get('error')} applied={data.get('applied')} level={r['apply']['level']}")
    txt, _ = await s.c("rename_symbol", {"target": tgt(PAY, new), "new_name": old, "dry_run": True})
    check_compact("t11", "rename_symbol", txt)
    r["compact"] = txt[:600]
    res["t11"] = r
    save("t11", r)


async def obs_t13(s: Sess, root: Path, res: dict) -> None:
    r: dict[str, Any] = {}
    # ruff の QuickFix（BOM+CRLF の 1 行目 / BOM なし LF の 1 行目）
    for rel in (BOM_LINT, "src/shop/models/lint_plain.py"):
        before = tree_hashes(root)
        dg, _ = await s.j("get_diagnostics", {"paths": [rel], "min_severity": "hint", "include_code_actions": True})
        diags = (dg.get("data") or {}).get("diagnostics") or []
        r[f"diag:{rel}"] = [(x["line"], x.get("character") or x.get("col") or x.get("column"), x.get("code"), [c.get("id") for c in x.get("code_actions") or []]) for x in diags]
        if not diags:
            note("t13", f"{rel}: get_diagnostics が 0 件（lsp_sources={(dg.get('data') or {}).get('lsp_sources')}）")
        d, _ = await s.j("apply_code_action", {"path": rel, "line": 1, "character": 8, "action_kind_or_id": "quickfix", "dry_run": True})
        data = d.get("data") or {}
        r[f"dry:{rel}"] = {"ok": d.get("ok"), "applied": data.get("applied"), "err": d.get("error"), "diff": (data.get("diff") or "")[:400]}
        if before != tree_hashes(root):
            note("t13", f"{rel}: dry_run でファイルが変わった")
        before = tree_hashes(root)
        b0 = (root / rel).read_bytes()
        d, _ = await s.j("apply_code_action", {"path": rel, "line": 1, "character": 8, "action_kind_or_id": "quickfix", "dry_run": True})
        cands = (d.get("error") or {}).get("candidates") or []
        ids = [c["id"] for c in cands]
        r[f"cands:{rel}"] = [(c["id"], c["title"]) for c in cands]
        if len(ids) != len(set(ids)):
            note("t13", f"{rel}: 候補の id が重複（{[(c['id'], c['title']) for c in cands]}）。メッセージの件数 {(d.get('error') or {}).get('message', '')[:12]!r}")
        pick = next((c["id"] for c in cands if "Remove unused import" in c["title"]), "quickfix")
        r[f"pick:{rel}"] = pick
        d, _ = await s.j("apply_code_action", {"path": rel, "line": 1, "action_kind_or_id": pick})
        data = d.get("data") or {}
        b1 = (root / rel).read_bytes()
        r[f"apply:{rel}"] = {"ok": d.get("ok"), "applied": data.get("applied"), "err": d.get("error"), "before": b0.decode("utf-8", "replace"), "after": b1.decode("utf-8", "replace"),
                             "target_resolved": data.get("target_diagnostic_resolved")}
        if d.get("ok") and data.get("applied"):
            check_applied("t13", f"apply {rel}", root, d, before)
            if eol_bom(b0)[2] != eol_bom(b1)[2]:
                note("t13", f"{rel}: 適用後に BOM が変わった")
            if b"\r\n" in b0 and (b1.count(b"\n") - b1.count(b"\r\n")) > 0:
                note("t13", f"{rel}: 適用後に CRLF でない改行が混ざった {b1!r}")
            txt1 = b1.decode("utf-8-sig")
            if "import os" in txt1 and "os" in txt1.split("import", 1)[-1].split(",")[0:1][0:1] and False:
                pass
            # F401(os) の QuickFix は os だけを消す。残りは sys か、全部消えて空
            ok_forms = {"import sys\n", "import sys\r\n", "", "\n", "\r\n"}
            if txt1 not in ok_forms and "import" in txt1 and "os" in txt1:
                note("t13", f"{rel}: QuickFix 後の内容が想定外: {b1!r}（元 {b0!r}）")
        elif not d.get("ok"):
            note("t13", f"{rel}: apply_code_action 失敗: {d.get('error')}")
    # エラー
    for label, a in (("bad_kind", {"path": PAY, "line": 62, "action_kind_or_id": "no.such.kind"}),
                     ("line_out", {"path": PAY, "line": 9999, "action_kind_or_id": "quickfix"}),
                     ("outside", {"path": "../x.py", "line": 1, "action_kind_or_id": "quickfix"}),
                     ("missing_file", {"path": "nope.py", "line": 1, "action_kind_or_id": "quickfix"}),
                     ("stale_sha", {"path": PAY, "line": 62, "action_kind_or_id": "quickfix", "expected_file_sha256": "0" * 64})):
        before = tree_hashes(root)
        d, _ = await s.j("apply_code_action", a)
        r.setdefault("errors", {})[label] = (d.get("ok"), (d.get("error") or {}).get("code"), (d.get("data") or {}).get("applied"))
        if before != tree_hashes(root):
            note("t13", f"エラー系 {label} でファイルが変わった")
    txt, _ = await s.c("apply_code_action", {"path": "src/shop/models/lint_plain.py", "line": 1, "action_kind_or_id": "quickfix", "dry_run": True})
    check_compact("t13", "apply_code_action", txt)
    r["compact"] = txt[:600]
    res["t13"] = r
    save("t13", r)


# ------------------------------------------------------------------ T-00 の設定操作（セッション 3）
async def run_cfg(root: Path) -> dict[str, Any]:
    r: dict[str, Any] = {}
    async with Sess() as s:
        time.perf_counter()
        d, dt = await s.j("configure_workspace", {"action": "status"})
        r["status_before_roots_ms"] = round(dt)
        r["status_before"] = {"ok": d.get("ok"), "err": d.get("error")}
        await s.setup(root)
        r["set_roots_ms"] = round(s.set_roots_ms)
        st0, _ = await s.j("configure_workspace", {"action": "status"})
        wid = st0["data"]["active_workspace_id"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", wid or ""):
            note("cfg", f"workspace_id {wid!r} が文字集合に一致しない")
        ws0 = [w["workspace_id"] for w in st0["data"]["workspaces"]]
        # 存在しないパス → E_INVALID_ARGUMENT、登録は不変
        d, _ = await s.j("configure_workspace", {"action": "set_roots", "root_paths": [root.as_posix(), (root / "no_such_dir").as_posix()]})
        st1, _ = await s.j("configure_workspace", {"action": "status"})
        r["bad_roots"] = (d.get("ok"), (d.get("error") or {}).get("code"))
        if (d.get("error") or {}).get("code") != "E_INVALID_ARGUMENT":
            note("cfg", f"存在しないパスを含む set_roots が E_INVALID_ARGUMENT でない: {r['bad_roots']}")
        if [w["workspace_id"] for w in st1["data"]["workspaces"]] != ws0 or st1["data"]["active_workspace_id"] != wid:
            note("cfg", "存在しないパスを含む set_roots の後で workspaces/active が変わった")
        # reindex ≤ 100ms・job_id・queued/running
        d, dt = await s.j("configure_workspace", {"action": "reindex"})
        jid = (d.get("data") or {}).get("job_id") or ((d.get("data") or {}).get("job_ids") or [None])[0]
        st2, _ = await s.j("configure_workspace", {"action": "status"})
        jobs = {j["job_id"]: j for j in st2["data"].get("jobs") or []}
        r["reindex"] = {"timing_ms": d.get("timing_ms"), "rtt_ms": round(dt), "job_id": jid, "state_after": (jobs.get(jid) or {}).get("state")}
        if not jid:
            note("cfg", f"reindex の応答に job_id が無い: {str(d)[:200]}")
        if (d.get("timing_ms") or 0) > 100:
            note("cfg", f"reindex の timing_ms={d.get('timing_ms')} > 100")
        if jid and (jobs.get(jid) or {}).get("state") not in ("queued", "running"):
            r["reindex"]["note"] = "直後の status で queued/running でない（小さい対象で即 done の可能性）"
        # job 指定
        d, _ = await s.j("configure_workspace", {"action": "status", "job_id": "job_nope"})
        r["unknown_job"] = (d.get("ok"), (d.get("error") or {}).get("code"))
        if (d.get("error") or {}).get("code") != "E_WORKSPACE_NOT_CONFIGURED":
            note("cfg", f"未知の job_id が E_WORKSPACE_NOT_CONFIGURED でない: {r['unknown_job']}")
        d, _ = await s.j("configure_workspace", {"action": "status", "workspace_id": "no_such_ws"})
        r["unknown_ws"] = (d.get("ok"), (d.get("error") or {}).get("code"))
        # 必須引数の欠落
        for a, label in (({"action": "set_roots"}, "set_roots_no_paths"), ({"action": "set_python_env"}, "pyenv_no_arg"),
                         ({"action": "set_excludes"}, "excludes_no_arg"), ({"action": "bogus"}, "bad_action"),
                         ({"action": "set_python_env", "python_interpreter": (root / "nope" / "python.exe").as_posix()}, "pyenv_missing")):
            d, _ = await s.j("configure_workspace", a)
            r.setdefault("errors", {})[label] = (d.get("ok"), (d.get("error") or {}).get("code"))
            if d.get("ok"):
                note("cfg", f"{label} が ok を返した")
        # set_excludes は置換
        files0 = st2["data"]["cache"]["files"]
        d, _ = await s.j("configure_workspace", {"action": "set_excludes", "exclude_globs": ["docs/**", "tests/**"]})
        await asyncio.sleep(2)
        st3, _ = await s.j("configure_workspace", {"action": "status"})
        r["excludes"] = {"files_before": files0, "files_after": st3["data"]["cache"]["files"], "globs": st3["data"]["workspaces"][0].get("exclude_globs"), "ok": d.get("ok"), "err": d.get("error")}
        if st3["data"]["cache"]["files"] >= files0:
            note("cfg", f"set_excludes(docs/**, tests/**) の後も cache.files が減らない {files0}->{st3['data']['cache']['files']}")
        q, _ = await s.j("query_project_db", {"query": "SELECT COUNT(*) AS n FROM files WHERE path LIKE 'tests/%' OR path LIKE 'docs/%'"})
        r["excluded_still_in_db"] = ((q.get("data") or {}).get("rows") or [{}])[0].get("n")
        if r["excluded_still_in_db"]:
            note("cfg", f"set_excludes 後も files に除外対象 {r['excluded_still_in_db']} 件が残る")
        d, _ = await s.j("configure_workspace", {"action": "set_excludes", "exclude_globs": []})
        # set_python_env（このリポジトリの .venv）
        py = (REPO / ".venv" / "Scripts" / "python.exe").as_posix()
        time.time()
        d, _ = await s.j("configure_workspace", {"action": "set_python_env", "python_interpreter": py})
        await asyncio.sleep(3)
        st4, _ = await s.j("configure_workspace", {"action": "status"})
        pe = st4["data"]["workspaces"][0]["python_env"]
        r["python_env"] = {"resp_ok": d.get("ok"), "err": d.get("error"), "job_id": (d.get("data") or {}).get("job_id"), "env": pe,
                           "lsp_started": [(x["server"], x.get("started_at")) for x in st4["data"]["lsp_daemons"] if x["language"] == "python"]}
        if d.get("ok"):
            if os_norm(pe.get("interpreter")) != os_norm(py):
                note("cfg", f"set_python_env 後の python_env.interpreter {pe.get('interpreter')!r} が指定値 {py!r} の正規化形と違う")
            if not (d.get("data") or {}).get("job_id"):
                note("cfg", "set_python_env の応答に job_id が無い")
        else:
            note("cfg", f"set_python_env が失敗: {d.get('error')}")
        txt, _ = await s.c("configure_workspace", {"action": "status"})
        check_compact("cfg", "configure_workspace", txt)
        r["compact"] = txt[:800]
        # 出力の ws=（BUG-0019 の確認）
        # 呼び出し順: status をルート登録前に呼んだ場合
    save("cfg", r)
    return r


def os_norm(p: str | None) -> str:
    return (p or "").replace("\\", "/").lower().rstrip("/")


# ------------------------------------------------------------------ 長い連続呼び出し
async def run_burst() -> dict[str, Any]:
    r: dict[str, Any] = {}
    root = prep_poly(POLY.with_name(POLY.name + "-burst"))
    async with Sess() as s:
        await s.setup(root)
        calls = [("search_workspace_symbols", {"query": "retry"}), ("resolve_symbols", {"targets": [tgt(PAY, "PaymentProcessor")]}),
                 ("find_references_and_implementations", {"target": tgt(LIM, "retry")}), ("query_call_hierarchy", {"target": tgt(LIM, "retry"), "direction": "both"}),
                 ("get_diagnostics", {"paths": [PAY]}), ("query_project_db", {"query": "SELECT COUNT(*) AS n FROM symbols"}),
                 ("get_symbol_bodies", {"targets": [tgt(PAY, "PaymentProcessor")]}), ("get_file_skeleton", {"paths": [PAY]})]
        t0 = time.time()
        n = 0
        stalled = None
        lat: list[float] = []
        while n < 400 and time.time() - t0 < 150:
            tool, a = calls[n % len(calls)]
            err, text, dt = await s.raw(tool, a, timeout=20)
            lat.append(dt)
            n += 1
            if text == "TIMEOUT" or text.startswith("EXC"):
                stalled = (n, tool, text[:120])
                break
        r = {"calls": n, "elapsed_s": round(time.time() - t0, 1), "stalled": stalled, "p50": round(sorted(lat)[len(lat) // 2]), "max": round(max(lat))}
        if stalled:
            note("burst", f"{n} 回目 {stalled[1]} で停止/切断: {stalled[2]}")
    save("burst", r)
    return r




# ------------------------------------------------------------------ このリポジトリ自身（読むだけ）
async def run_self() -> dict[str, Any]:
    r: dict[str, Any] = {}
    root = REPO
    py = [p for top in ("src", "tests") for p in sorted((root / top).rglob("*.py")) if "__pycache__" not in p.parts]
    defs: dict[str, set[str]] = {}
    for p in py:
        try:
            for q in ast_defs(p):
                defs.setdefault(q.split(".")[-1], set()).add(p.relative_to(root).as_posix())
        except SyntaxError:
            pass
    async with Sess() as s:
        await s.setup(root)
        await s.setup(root)
        r["files"] = ((await s.j("configure_workspace", {"action": "status"}))[0].get("data") or {}).get("cache")
        for name in ("LuciankeError", "TokenEstimator", "resolve_target", "open_reader", "confine", "LineIndex"):
            d, _ = await s.j("search_workspace_symbols", {"query": name, "match": "exact", "limit": 200, "symbol_kinds": ["Class", "Function"]})
            if not d.get("data"):
                note("self", f"T-03 exact {name}: data が無い応答 {str(d)[:300]}")
                continue
            got = {x["path"] for x in d["data"]["symbols"]}
            exp = defs.get(name, set())
            r[f"t03:{name}"] = (len(got), len(exp), sorted(got ^ exp)[:4])
            if got != exp:
                note("self", f"T-03 exact {name}: 返り {len(got)} 件と ast {len(exp)} 件が違う（差 {sorted(got ^ exp)[:3]}）")
        for name, rel in (("LuciankeError", "src/lucianke/core/errors.py"), ("resolve_target", "src/lucianke/tools/_nav.py"),
                          ("confine", "src/lucianke/workspace/paths.py")):
            d, _ = await s.j("find_references_and_implementations", {"target": tgt(rel, name), "query_type": "references", "include_declaration": True, "max_tokens": BIG})
            if not d.get("ok"):
                note("self", f"T-06 {name}: {d.get('error')}")
                continue
            refs = d["data"].get("references") or []
            got = {(x["path"], x["line"]) for x in refs}
            exp = refs_by_ast(root, name)
            exp = {e for e in exp if e[0].startswith(("src/", "tests/"))}
            bad = []
            for x in refs:
                try:
                    line = read_lines(root, x["path"])[x["line"] - 1]
                except Exception:
                    bad.append((x["path"], x["line"], "unreadable"))
                    continue
                if name not in line:
                    bad.append((x["path"], x["line"], line.strip()[:50]))
            r[f"t06:{name}"] = {"got": len(got), "ast": len(exp), "missing": len(exp - got), "extra": len(got - exp), "line_without_name": len(bad), "level": d["_resolution_meta"]["confidence_level"]}
            if exp - got:
                note("self", f"T-06 {name}: ast にあって返りに無い {len(exp - got)} 件（例 {sorted(exp - got)[:3]}）")
            if bad:
                note("self", f"T-06 {name}: 行に名前が無い参照 {len(bad)} 件（例 {bad[:2]}）")
        d, _ = await s.j("query_call_hierarchy", {"target": tgt("src/lucianke/tools/_nav.py", "resolve_target"), "direction": "incoming", "depth": 1, "max_children": 500, "max_tokens": BIG})
        if d.get("ok"):
            nodes = d["data"].get("nodes") or []
            bad = []
            for n in nodes:
                if n.get("call_site_path"):
                    line = read_lines(root, n["call_site_path"])[n["call_site_line"] - 1]
                    if "resolve_target" not in line:
                        bad.append((n["name"], n["call_site_path"], n["call_site_line"], line.strip()[:50]))
            r["t07:resolve_target"] = {"nodes": len(nodes), "bad_lines": len(bad), "level": d["_resolution_meta"]["confidence_level"]}
            if bad:
                note("self", f"T-07 resolve_target incoming: 呼び出し行に名前が無い {len(bad)} 件（例 {bad[:2]}）")
        else:
            note("self", f"T-07 resolve_target: {d.get('error')}")
    save("self", r)
    return r


# ------------------------------------------------------------------ main
async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # 応答の日本語や → を cp932 のパイプへ出して化けさせない（BUG-0038）
    want = ARGS.obs or ["ro", "edit", "cfg", "burst"]
    summary: dict[str, Any] = {}
    if "ro" in want:
        summary["ro"] = await run_readonly(prep_poly(POLY))
    if "edit" in want:
        summary["edit"] = await run_edit(prep_poly(POLY.with_name(POLY.name + "-edit")))
    if "cfg" in want:
        summary["cfg"] = await run_cfg(prep_poly(POLY.with_name(POLY.name + "-cfg")))
    if "burst" in want:
        summary["burst"] = await run_burst()
    if "self" in want:
        summary["self"] = await run_self()
    save("findings_" + "_".join(want), FINDINGS)
    print("FINDINGS", json.dumps({k: len(v) for k, v in FINDINGS.items()}))


if __name__ == "__main__":
    asyncio.run(main())
