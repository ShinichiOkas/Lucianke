"""Lucianke のドッグフーディング: 実際の stdio MCP クライアントで 2 ツールをこのリポジトリ自身に当て、仕様書との差を数える。
使い方:  uv run --quiet --directory S:/work/develop/Lucianke python bench/dogfood/run_dogfood.py [観点番号 ...]
出力:    bench/dogfood/out/obs<N>.json（観点ごとの数と代表例。UTF-8）。観点を省略すると 1〜8 を全部走らせる。
注意:    読むだけ。src/ と tests/ は触らない。標準出力は cp932 で化けるので結果は必ずファイルを読む。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import random
import re
import statistics
import sys
import time
import warnings
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

warnings.filterwarnings("ignore")
REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "bench" / "dogfood" / "out"
sys.path.insert(0, str(REPO / "tests" / "contract"))
import compact_grammar as g  # noqa: E402
from lucianke.mcp.budget import TokenEstimator  # noqa: E402

EST = TokenEstimator()
BIG = 1_000_000


def params() -> StdioServerParameters:
    return StdioServerParameters(
        command="uv", args=["run", "--quiet", "--directory", str(REPO), "lucianke"], env={"PYTHONUTF8": "1"}
    )


def save(n: int, obj: Any) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"obs{n}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


async def call(c: Client, tool: str, args: dict[str, Any]) -> tuple[bool, str, Any, float]:
    t = time.perf_counter()
    try:
        r = await asyncio.wait_for(c.call_tool(tool, args), 25)
    except asyncio.TimeoutError:
        return True, "TIMEOUT>25s " + json.dumps(args, ensure_ascii=False)[:200], None, 25000.0
    dt = (time.perf_counter() - t) * 1000
    text = r.content[0].text if r.content else ""
    return bool(r.is_error), text, r.structured_content, dt


async def jcall(c: Client, tool: str, args: dict[str, Any]) -> tuple[bool, dict, float]:
    err, text, sc, dt = await call(c, tool, {**args, "format": "json"})
    try:
        d = json.loads(text)
    except Exception:
        d = {"_unparsed": text[:300]}
    return err, d, dt


def py_files() -> list[str]:
    out = []
    for top in ("src", "tests"):
        for p in sorted((REPO / top).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            out.append(p.relative_to(REPO).as_posix())
    return out


def ast_defs(rel: str) -> tuple[list[dict], bytes]:
    """モジュール直下とクラス直下（再帰）の関数・クラス。入れ子の関数（関数の中の def）は nested=True で別扱い。"""
    raw = (REPO / rel).read_bytes()
    try:
        tree = ast.parse(raw)
    except SyntaxError:  # 故意に壊した fixture
        return [], raw
    defs: list[dict] = []

    def visit(body_owner: ast.AST, prefix: str, nested: bool, cond: bool) -> None:
        for node in ast.iter_child_nodes(body_owner):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                q = f"{prefix}{node.name}"
                kind = "Class" if isinstance(node, ast.ClassDef) else "Function"
                defs.append({"q": q, "name": node.name, "start": start, "end": node.end_lineno, "kind": kind,
                             "nested": nested, "cond": cond})
                if isinstance(node, ast.ClassDef):
                    visit(node, q + ".", nested, cond)
                else:
                    visit(node, q + ".", True, cond)
            elif isinstance(node, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                visit(node, prefix, nested, True)

    visit(tree, "", False, False)
    return defs, raw


def flatten(symbols: list[dict]) -> list[dict]:
    out = []

    def rec(ss: list[dict]) -> None:
        for s in ss:
            out.append(s)
            rec(s.get("children", []))

    rec(symbols)
    return out


# ---------------------------------------------------------------- 観点 1・2
async def obs12(c: Client) -> None:
    files = py_files()
    miss, extra, rng, errs = [], [], [], []
    grammar = []
    nfiles = nast = nskel = 0
    for rel in files:
        defs, raw = ast_defs(rel)
        sha = hashlib.sha256(raw).hexdigest()
        err, d, _ = await jcall(c, "get_file_skeleton", {"paths": [rel], "max_tokens": BIG})
        if err or not d.get("ok"):
            errs.append({"path": rel, "error": d.get("error") or d})
            continue
        item = d["data"]["items"][0]
        if not item.get("ok"):
            errs.append({"path": rel, "error": item.get("error")})
            continue
        if item.get("file_sha256") != sha:  # 並行編集で内容が変わった
            continue
        nfiles += 1
        syms = [s for s in flatten(item["symbols"]) if s["kind"] in ("Class", "Function", "Method", "Constructor", "Property")]
        sk = [(s["qualified_name"], s["range"]["start_line"], s["range"]["end_line"]) for s in syms]
        skq = {}
        for q, a, b in sk:
            skq.setdefault(q, []).append((a, b))
        astq = {}
        for df in defs:
            astq.setdefault(df["q"], []).append(df)
        nast += sum(1 for df in defs if not df["nested"])
        nskel += len(sk)
        for q, lst in astq.items():
            for df in lst:
                if df["nested"]:
                    continue
                if q not in skq:
                    miss.append({"path": rel, "q": q, "ast": [df["start"], df["end"]], "cond": df["cond"]})
        for q, lst in skq.items():
            if q not in astq:
                extra.append({"path": rel, "q": q, "skel": lst})
        for q, lst in skq.items():
            if q in astq:
                aset = sorted((df["start"], df["end"]) for df in astq[q])
                sset = sorted(lst)
                if aset != sset:
                    rng.append({"path": rel, "q": q, "ast": aset, "skel": sset})
        # 観点 2: compact の文法
        err2, text, _, _ = await call(c, "get_file_skeleton", {"paths": [rel], "max_tokens": BIG})
        v = g.validate(text)
        if v:
            grammar.append({"path": rel, "violations": v[:3], "n": len(v)})
        # compact の @L と json の範囲が一致するか（骨格行の @Ls-e）
        cm = {}
        for ln in text.splitlines():
            m = re.match(r"^(\s*)\[(\w+)\] (?:(?:pub|priv|prot|static|async|abstract|export) )*(.+?) @L(\d+)(?:-(\d+))?", ln)
            if m:
                cm.setdefault(m.group(2), []).append((int(m.group(4)), int(m.group(5) or m.group(4))))
    save(1, {"files_compared": nfiles, "files_total": len(files), "ast_defs_non_nested": nast, "skeleton_defs": nskel,
             "missing_in_skeleton": len(miss), "extra_in_skeleton": len(extra), "range_mismatch": len(rng),
             "errors": len(errs), "missing_examples": miss[:15], "extra_examples": extra[:15],
             "range_examples": rng[:15], "error_examples": errs[:10]})
    save(2, {"files_checked": nfiles, "files_with_violations": len(grammar), "examples": grammar[:15]})


# ---------------------------------------------------------------- 観点 3
def block_names(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("=== ")]


async def obs3(c: Client) -> None:
    # 大きめのファイル 12 本
    allf = sorted(py_files(), key=lambda p: (REPO / p).stat().st_size)
    cand = [p for p in allf if 1500 < (REPO / p).stat().st_size < 5000][:15]
    big = sorted(allf, key=lambda p: -(REPO / p).stat().st_size)[:1]
    res: dict[str, Any] = {"files": cand}
    for mt in (2000, 3000):
        seen = set()
        cursor = None
        trace = []
        for _ in range(6):
            a = {"paths": big, "max_tokens": mt}
            if cursor:
                a["cursor"] = cursor
            e, t, _sc, _ = await call(c, "get_file_skeleton", a)
            mm = re.search(r"^# more: cursor=(\S+)", t, re.M)
            trace.append({"lines": len(t.splitlines()), "head": t.splitlines()[0][:100], "warn": [ln[:160] for ln in t.splitlines() if ln.startswith("! ")], "cursor_same_as_before": bool(mm and mm.group(1) in seen)})
            if not mm:
                break
            seen.add(mm.group(1))
            cursor = mm.group(1)
        res[f"bigfile_{mt}"] = {"path": big[0], "bytes": (REPO / big[0]).stat().st_size, "pages_tried": len(trace), "trace": trace}
    _, full_text, _, _ = await call(c, "get_file_skeleton", {"paths": cand, "max_tokens": BIG})
    res["full_used_header"] = full_text.splitlines()[0]
    full_blocks = full_text.split("\n=== ")
    for mt in (200, 500, 2000):
        pages = []
        cursor = None
        concat = []
        over = []
        while True:
            args = {"paths": cand, "max_tokens": mt}
            if cursor:
                args["cursor"] = cursor
            err, text, _, _ = await call(c, "get_file_skeleton", args)
            est = EST.count(text)
            head = text.splitlines()[0] if text else ""
            m = re.search(r"~(\d+)/(\d+)tok", head)
            pages.append({"err": err, "est": est, "hdr": head[:140], "lines": len(text.splitlines()),
                          "last": text.splitlines()[-1][:120] if text else "", "violations": g.validate(text)[:2]})
            if est > mt:
                over.append({"page": len(pages), "est": est, "max": mt})
            if m and int(m.group(1)) != est:
                pages[-1]["header_used_vs_recount"] = [int(m.group(1)), est]
            concat.append(text)
            mm = re.search(r"^# more: cursor=(\S+)", text, re.M)
            if err or not mm or len(pages) > 80 or (mm and mm.group(1) == cursor):
                break
            cursor = mm.group(1)
        joined_blocks = [ln for t in concat for ln in block_names(t)]
        uniq = set(joined_blocks)
        # シンボル行の重複・欠落: 全ページの "[..]" 行を、無制限の骨格と比べる
        def sym_lines(t: str) -> list[str]:
            return [re.sub(r"\s+\{ \.\.\.\d+ lines omitted \}$", "", ln.strip()) for ln in t.splitlines()
                    if re.match(r"^\s*\[\w+\] ", ln)]
        full_syms = sym_lines(full_text)
        paged_syms = [s for t in concat for s in sym_lines(t)]
        from collections import Counter
        cf, cp = Counter(full_syms), Counter(paged_syms)
        res[str(mt)] = {"pages": len(pages), "over_budget_pages": over, "dup_file_blocks": len(joined_blocks) - len(uniq),
                        "file_blocks_total": len(joined_blocks), "file_blocks_full": len(block_names(full_text)),
                        "missing_symbols_vs_full": sum((cf - cp).values()), "extra_or_dup_symbols_vs_full": sum((cp - cf).values()),
                        "sample_missing": list((cf - cp).elements())[:5], "sample_dup": list((cp - cf).elements())[:5],
                        "page_samples": pages[:4], "has_more_trailer_pages": sum(1 for p in pages if "# more:" in p["last"]) }
    # json の budget/page
    err, d, _ = await jcall(c, "get_file_skeleton", {"paths": cand, "max_tokens": 500})
    res["json_500"] = {"page": d.get("page"), "budget": d.get("budget"), "warnings": d.get("warnings")}
    # 予算が小さすぎる（レコード 0 件）: 最小の 200 で 1 ファイルが入らないケース
    err, text, _, _ = await call(c, "get_file_skeleton", {"paths": cand[:1], "max_tokens": 200})
    res["tiny_single_file_200"] = text[:600]
    # 続きのカーソル: 引数を変えたら E_CURSOR_EXPIRED / E_INVALID_ARGUMENT?
    mm = re.search(r"cursor=(\S+)", text)
    if mm:
        err, t2, _, _ = await call(c, "get_file_skeleton", {"paths": cand[1:3], "max_tokens": 200, "cursor": mm.group(1)})
        res["cursor_with_other_args"] = t2[:300]
    err, t3, _, _ = await call(c, "get_file_skeleton", {"paths": cand[:1], "max_tokens": 200, "cursor": "not-a-cursor"})
    res["cursor_garbage"] = t3[:300]
    save(3, res)


# ---------------------------------------------------------------- 観点 4
async def obs4(c: Client) -> None:
    random.seed(20261029)
    pool = []
    for rel in py_files():
        defs, raw = ast_defs(rel)
        cnt: dict[str, int] = {}
        for df in defs:
            cnt[df["q"]] = cnt.get(df["q"], 0) + 1
        for df in defs:
            if cnt[df["q"]] == 1 and not df["nested"]:
                pool.append((rel, df))
    picks = random.sample(pool, 50)
    bad = []
    stats = {"ok": 0, "err": 0, "code_mismatch": 0, "range_mismatch": 0, "sha_mismatch": 0, "L_not_consecutive": 0,
             "L_text_mismatch": 0, "compact_sha_mismatch": 0, "ctx0_range": 0}
    for rel, df in picks:
        raw = (REPO / rel).read_bytes()
        lines = re.split(r"\r\n|\r|\n", raw.decode("utf-8-sig"))
        sha = hashlib.sha256(raw).hexdigest()
        tgt = {"path": rel, "symbol_name": df["q"]}
        # context_lines=0 の json
        err, d, _ = await jcall(c, "get_symbol_bodies", {"targets": [tgt], "context_lines": 0, "include_referenced_symbols": False})
        if err or not d.get("ok") or not d["data"]["items"][0].get("ok"):
            stats["err"] += 1
            bad.append({"path": rel, "q": df["q"], "kind": "error", "resp": (d.get("error") or d["data"]["items"][0])})
            continue
        it = d["data"]["items"][0]
        rr = it["range_returned"]
        if (rr["start_line"], rr["end_line"]) != (df["start"], df["end"]):
            stats["range_mismatch"] += 1
            bad.append({"path": rel, "q": df["q"], "kind": "range", "got": [rr["start_line"], rr["end_line"]], "ast": [df["start"], df["end"]]})
        if it["code"].split("\n") != lines[rr["start_line"] - 1: rr["end_line"]]:
            stats["code_mismatch"] += 1
            bad.append({"path": rel, "q": df["q"], "kind": "code"})
        if it.get("file_sha256") != sha:
            stats["sha_mismatch"] += 1
            bad.append({"path": rel, "q": df["q"], "kind": "json_sha", "got": it.get("file_sha256"), "want": sha})
        # 既定 context_lines=2 の compact
        err, text, _, _ = await call(c, "get_symbol_bodies", {"targets": [tgt]})
        v = g.validate(text)
        if v:
            bad.append({"path": rel, "q": df["q"], "kind": "grammar", "v": v[:2]})
        m = re.search(r"^  sha: (\w+)$", text, re.M)
        if not m or m.group(1) != sha[:12]:
            stats["compact_sha_mismatch"] += 1
            bad.append({"path": rel, "q": df["q"], "kind": "compact_sha", "got": m and m.group(1), "want": sha[:12]})
        fence = re.search(r"^```\w*\n(.*?)^```", text, re.M | re.S)
        if fence:
            nums = []
            for ln in fence.group(1).splitlines():
                mm = re.match(r"^@L(\d+): ?(.*)$", ln)
                if not mm:
                    continue
                n = int(mm.group(1))
                nums.append(n)
                if mm.group(2) != lines[n - 1] and mm.group(2) != lines[n - 1].rstrip():
                    stats["L_text_mismatch"] += 1
                    bad.append({"path": rel, "q": df["q"], "kind": "L_text", "n": n, "got": mm.group(2)[:60], "want": lines[n - 1][:60]})
                    break
            if nums != list(range(nums[0], nums[0] + len(nums))):
                stats["L_not_consecutive"] += 1
                bad.append({"path": rel, "q": df["q"], "kind": "L_seq"})
            # 既定の context_lines=2: 開始は ast 開始の 2 行手前まで（デコレータ・コメントと重ならない分）
            if nums and (nums[0] > df["start"] or nums[-1] != df["end"]):
                bad.append({"path": rel, "q": df["q"], "kind": "ctx2_range", "got": [nums[0], nums[-1]], "ast": [df["start"], df["end"]]})
        else:
            bad.append({"path": rel, "q": df["q"], "kind": "no_fence", "text": text[:200]})
        stats["ok"] += 1
    save(4, {"picked": 50, "stats": stats, "bad_examples": bad[:30], "bad_count": len(bad)})


# ---------------------------------------------------------------- 観点 5
async def obs5(c: Client) -> None:
    res: dict[str, Any] = {}
    rel = "src/lucianke/core/urn.py"
    err, text, _, _ = await call(c, "get_file_skeleton", {"paths": [rel]})
    res["skel_header"] = text.splitlines()[0]
    h = re.match(r"^# get_file_skeleton ok conf=(\S+) lsp=(\S+) rev=(\S+) ws=(\S+) ~(\d+)/(\d+)tok$", text.splitlines()[0])
    res["skel_header_matches_spec_shape"] = bool(h)
    err, text, _, _ = await call(c, "get_symbol_bodies", {"targets": [{"path": rel, "symbol_name": "workspace_id_from_dirname"}]})
    res["body_header"] = text.splitlines()[0]
    err, d, dt = await jcall(c, "get_file_skeleton", {"paths": [rel, "src/lucianke/core/errors.py"], "max_tokens": 2000})
    res["skel_json_keys"] = sorted(d.keys())
    res["skel_json_meta"] = d.get("_resolution_meta")
    res["skel_json_page"] = d.get("page")
    res["skel_json_budget"] = d.get("budget")
    res["skel_json_timing"] = d.get("timing_ms")
    res["skel_json_warnings"] = d.get("warnings")
    res["skel_json_error"] = d.get("error")
    it = d["data"]["items"][0]
    res["skel_item_keys"] = sorted(it.keys())
    res["skel_item_sha_len"] = len(it.get("file_sha256", ""))
    # json の used_tokens_est が本文の再計算と近いか
    res["json_used_vs_recount"] = [d["budget"]["used_tokens_est"], EST.count(json.dumps(d, ensure_ascii=False))]
    err, d2, _ = await jcall(c, "get_symbol_bodies", {"targets": [{"path": rel, "symbol_name": "workspace_id_from_dirname"}]})
    res["body_json_keys"] = sorted(d2.keys())
    res["body_json_meta"] = d2.get("_resolution_meta")
    res["body_json_item_keys"] = sorted(d2["data"]["items"][0].keys())
    res["body_json_page"] = d2.get("page")
    res["body_json_budget"] = d2.get("budget")
    res["body_json_warnings"] = d2.get("warnings")
    # structured_content が json のとき入るか、compact のとき入らないか
    e, t, sc, _ = await call(c, "get_file_skeleton", {"paths": [rel], "format": "json"})
    res["structured_content_json_present"] = sc is not None
    e, t, sc, _ = await call(c, "get_file_skeleton", {"paths": [rel]})
    res["structured_content_compact_present"] = sc is not None
    # 精度水準: Python は tree-sitter のみなので structural。仕様 T-02 は exact には LSP が要る
    # ヘッダの ~used が実際の本文の推定と合うか
    m = re.search(r"~(\d+)/(\d+)tok", text.splitlines()[0])
    res["compact_used_vs_recount"] = [int(m.group(1)), EST.count(text)] if m else None
    # 他の format
    for fmt in ("dot", "mermaid", "xml"):
        e, t, sc, _ = await call(c, "get_file_skeleton", {"paths": [rel], "format": fmt})
        res[f"format_{fmt}"] = {"is_error": e, "head": t[:200]}
    # revision / workspace_id
    for k, v in (("revision", "HEAD"), ("revision", "no-such-rev-xyz"), ("workspace_id", "nope"), ("revision", "STAGED")):
        e, t, sc, _ = await call(c, "get_file_skeleton", {"paths": [rel], k: v})
        res[f"{k}={v}"] = {"is_error": e, "head": t[:260]}
    save(5, res)


# ---------------------------------------------------------------- 観点 6
async def obs6(c: Client) -> None:
    res: dict[str, Any] = {}
    cases = {
        "skel_nonexistent": ("get_file_skeleton", {"paths": ["src/no/such_file.py"]}),
        "skel_nonexistent_json": ("get_file_skeleton", {"paths": ["src/no/such_file.py"], "format": "json"}),
        "skel_dotdot": ("get_file_skeleton", {"paths": ["../x.py"]}),
        "skel_dotdot_json": ("get_file_skeleton", {"paths": ["../x.py"], "format": "json"}),
        "skel_dotdot_mid": ("get_file_skeleton", {"paths": ["src/../../x.py"]}),
        "skel_abs_outside": ("get_file_skeleton", {"paths": ["C:/Windows/win.ini"]}),
        "skel_abs_inside": ("get_file_skeleton", {"paths": [str(REPO / "src/lucianke/core/urn.py").replace("\\", "/")]}),
        "skel_backslash": ("get_file_skeleton", {"paths": ["src\\lucianke\\core\\urn.py"]}),
        "skel_21": ("get_file_skeleton", {"paths": [f"src/lucianke/core/f{i}.py" for i in range(21)]}),
        "skel_21_json": ("get_file_skeleton", {"paths": ["src/lucianke/core/urn.py"] * 21, "format": "json"}),
        "skel_20_same": ("get_file_skeleton", {"paths": ["src/lucianke/core/urn.py"] * 20}),
        "skel_0": ("get_file_skeleton", {"paths": []}),
        "skel_missing_paths": ("get_file_skeleton", {}),
        "skel_unknown_prop": ("get_file_skeleton", {"paths": ["src/lucianke/core/urn.py"], "bogus": 1}),
        "skel_paths_str": ("get_file_skeleton", {"paths": "src/lucianke/core/urn.py"}),
        "skel_max_tokens_199": ("get_file_skeleton", {"paths": ["src/lucianke/core/urn.py"], "max_tokens": 199}),
        "skel_max_depth_11": ("get_file_skeleton", {"paths": ["src/lucianke/core/urn.py"], "max_depth": 11}),
        "skel_dir": ("get_file_skeleton", {"paths": ["src/lucianke"]}),
        "skel_empty_string": ("get_file_skeleton", {"paths": [""]}),
        "skel_partial": ("get_file_skeleton", {"paths": ["src/lucianke/core/urn.py", "no/such.py"], "format": "json"}),
        "skel_partial_compact": ("get_file_skeleton", {"paths": ["src/lucianke/core/urn.py", "no/such.py"]}),
        "skel_binary": ("get_file_skeleton", {"paths": ["uv.lock"]}),
        "skel_unknown_lang": ("get_file_skeleton", {"paths": ["LICENSE"]}),
        "skel_md": ("get_file_skeleton", {"paths": ["README.md"]}),
        "body_nonexistent_file": ("get_symbol_bodies", {"targets": [{"path": "src/no/such.py", "symbol_name": "x"}]}),
        "body_dotdot": ("get_symbol_bodies", {"targets": [{"path": "../x.py", "symbol_name": "x"}]}),
        "body_ambiguous": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/mcp/budget.py", "symbol_name": "pack"}]}),
        "body_ambiguous_ts": ("get_symbol_bodies", {"targets": [{"path": "tests/fixtures/poly/web/src/payment.ts", "symbol_name": "charge"}]}),
        "body_ambiguous_py": ("get_symbol_bodies", {"targets": [{"path": "tests/unit/test_lsp_client.py", "symbol_name": "__init__"}]}),
        "body_notfound": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "no_such_symbol_xyz"}]}),
        "body_21": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "parse"}] * 21}),
        "body_0": ("get_symbol_bodies", {"targets": []}),
        "body_ctx_21": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "parse"}], "context_lines": 21}),
        "body_ctx_neg": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "parse"}], "context_lines": -1}),
        "body_range_bad": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "parse"}], "line_range": {"start": 9, "end": 3}}),
        "body_xpath_on_py": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "xpath://a"}]}),
        "body_urn": ("get_symbol_bodies", {"targets": [{"urn": "lk://Lucianke/src/lucianke/core/urn.py#parse"}]}),
        "body_urn_missing": ("get_symbol_bodies", {"targets": [{"urn": "lk://Lucianke/src/lucianke/core/urn.py#nope"}]}),
        "body_line": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "line": 60}]}),
        "body_no_target_form": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py"}]}),
        "body_qualified": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer.pack"}]}),
        "body_partial": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "parse"}, {"path": "src/lucianke/core/urn.py", "symbol_name": "nope"}], "format": "json"}),
        "body_partial_compact": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/core/urn.py", "symbol_name": "parse"}, {"path": "src/lucianke/core/urn.py", "symbol_name": "nope"}]}),
        "body_line_range": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer"}], "line_range": {"start": 200, "end": 205}, "context_lines": 0}),
        "body_tiny_budget": ("get_symbol_bodies", {"targets": [{"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer"}], "max_tokens": 200}),
    }
    for name, (tool, args) in cases.items():
        try:
            e, t, sc, _ = await call(c, tool, args)
            v = g.validate(t) if not args.get("format") == "json" else []
            res[name] = {"is_error": e, "text": t[:700], "grammar": v[:2]}
            if args.get("format") == "json":
                try:
                    d = json.loads(t)
                    res[name] = {"is_error": e, "ok": d.get("ok"), "error": d.get("error"), "warnings": d.get("warnings"),
                                 "items": [{k: (v if k != "symbols" and k != "code" else "...") for k, v in i.items()} for i in d.get("data", {}).get("items", [])] if isinstance(d.get("data"), dict) else d.get("data"),
                                 "meta_conf": (d.get("_resolution_meta") or {}).get("confidence_level")}
                except Exception as ex:
                    res[name]["json_parse"] = repr(ex)
        except Exception as ex:
            res[name] = {"exception": repr(ex)[:400]}
    save(6, res)


# ---------------------------------------------------------------- 観点 7
async def obs7(c: Client) -> None:
    res: dict[str, Any] = {}
    rel = "src/lucianke/mcp/budget.py"
    for _ in range(15):
        await call(c, "get_file_skeleton", {"paths": [rel]})
        await call(c, "get_symbol_bodies", {"targets": [{"path": rel, "symbol_name": "Packer.pack"}]})

    def pct(xs: list[float], p: float) -> float:
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]

    for name, tool, args in (
        ("skeleton_1file", "get_file_skeleton", {"paths": [rel]}),
        ("skeleton_5files", "get_file_skeleton", {"paths": py_files()[:5], "max_tokens": 4000}),
        ("bodies_1", "get_symbol_bodies", {"targets": [{"path": rel, "symbol_name": "Packer.pack"}]}),
        ("bodies_5", "get_symbol_bodies", {"targets": [{"path": rel, "symbol_name": s} for s in ("Packer.pack", "CursorCodec.decode", "trailer_lines", "default_summary", "CursorState")]}),
    ):
        rt, srv = [], []
        for i in range(100):
            e, t, sc, dt = await call(c, tool, args)
            rt.append(dt)
            e2, d, dt2 = await jcall(c, tool, args)
            if isinstance(d.get("timing_ms"), (int, float)):
                srv.append(d["timing_ms"])
        res[name] = {"roundtrip_compact_ms": {"p50": round(pct(rt, .5), 1), "p95": round(pct(rt, .95), 1), "max": round(max(rt), 1)},
                     "server_timing_ms_json": {"p50": pct(srv, .5) if srv else None, "p95": pct(srv, .95) if srv else None, "n": len(srv)}}
    save(7, res)


# ---------------------------------------------------------------- 観点 8
async def obs8(c: Client) -> None:
    res: dict[str, Any] = {}
    ts = ["tests/fixtures/poly/web/src/app.ts", "tests/fixtures/poly/web/src/payment.ts"]
    e, t, sc, _ = await call(c, "get_file_skeleton", {"paths": ts})
    res["skeleton"] = t
    res["grammar"] = g.validate(t)
    e, d, _ = await jcall(c, "get_file_skeleton", {"paths": ts})
    res["json_meta"] = d.get("_resolution_meta")
    res["json_items"] = [{k: v for k, v in i.items() if k not in ("symbols",)} for i in d["data"]["items"]]
    res["json_symbols_payment"] = d["data"]["items"][1].get("symbols")
    src = (REPO / ts[1]).read_text(encoding="utf-8").splitlines()
    res["src_payment"] = {i + 1: ln for i, ln in enumerate(src)}
    for sym in ("PaymentClient.charge", "charge", "PaymentClient", "PaymentRequest", "PaymentClient.constructor"):
        e, t, sc, _ = await call(c, "get_symbol_bodies", {"targets": [{"path": ts[1], "symbol_name": sym}]})
        res[f"body:{sym}"] = {"is_error": e, "text": t[:1800], "grammar": g.validate(t)[:3]}
    app_syms = []
    e, t, sc, _ = await call(c, "get_file_skeleton", {"paths": [ts[0]]})
    res["app_src"] = (REPO / ts[0]).read_text(encoding="utf-8")
    # 各 app.ts のシンボルを本体で取って範囲を見る
    e, d2, _ = await jcall(c, "get_file_skeleton", {"paths": [ts[0]]})
    for s in flatten(d2["data"]["items"][0].get("symbols", [])):
        e, bj, _ = await jcall(c, "get_symbol_bodies", {"targets": [{"path": ts[0], "symbol_name": s["qualified_name"]}], "context_lines": 0})
        it = bj.get("data", {}).get("items", [{}])[0] if isinstance(bj.get("data"), dict) else {}
        s_lines = res["app_src"].splitlines()
        rr = it.get("range_returned")
        res.setdefault("app_bodies", []).append({"q": s["qualified_name"], "skel_range": s["range"], "body_range": rr, "ok": it.get("ok"),
                                                  "code_matches_lines": (it.get("code", "").split("\n") == s_lines[rr["start_line"] - 1: rr["end_line"]]) if rr else None,
                                                  "err": it.get("error")})
    save(8, res)


async def main() -> None:
    want = [int(a) for a in sys.argv[1:]] or [1, 3, 4, 5, 6, 7, 8]
    async with Client(params(), mode="legacy", cache=None) as c:
        for n, fn in ((1, obs12), (3, obs3), (4, obs4), (5, obs5), (6, obs6), (7, obs7), (8, obs8)):
            if n in want or (n == 1 and 2 in want):
                t = time.time()
                try:
                    await fn(c)
                    print(f"obs{n} done {time.time() - t:.1f}s")
                except Exception as ex:  # 落ちたら落ちたこと自体を記録
                    import traceback

                    save(n, {"script_exception": traceback.format_exc()})
                    print(f"obs{n} EXC {ex!r}")


if __name__ == "__main__":
    asyncio.run(main())
