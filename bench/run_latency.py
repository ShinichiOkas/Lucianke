"""レイテンシの計測台本（仕様書 §14.4、NFR-PERF-1・NFR-PERF-2、M1-3）。

実プロセスのサーバー（``python -m lucianke``。``_stdio.server_argv``。BUG-0030）に stdio でつなぎ、ツールごとに
Tier 1 のファイルからシード固定でランダムに選んだ対象で 200 回呼ぶ。サーバー内の ``timing_ms``（json エンベロープ）と
クライアント往復の両方を記録し、判定はサーバー内時間で行う。

使い方::

    uv run python bench/run_latency.py                       # F-POLY と、取得済みの F-PY / F-TS
    uv run python bench/run_latency.py --fixtures poly,self  # F-POLY とこのリポジトリ自身
    uv run python bench/run_latency.py --calls 50 --tools get_file_skeleton

集計する母集団（判定に使う）は「応答が成功し、``_resolution_meta.is_degraded`` が false」の呼び出し。これは「そのツールの
到達しうる最高水準で答えた」呼び出しで（仕様書 §5.5）、T-02・T-05 のように最高水準が ``structural`` のツールでも空にならない。
``confidence_level == "exact"`` だけの集計と、降格応答の集計も別に出す。測定中の ``stdout`` 純度と子プロセスは常に監視し、
違反があればその実行（フィクスチャ）全体を不合格にする（§14.2）。

結果: ``bench/results/<日時>-<OS>-latency.json``。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import random
import re
import shutil
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
import _results as R  # noqa: E402
import _stdio as S  # noqa: E402

REPO = R.REPO
CACHE = BENCH / ".cache"

DEFAULT_CALLS = 200
DEFAULT_SEED = 20261029
PRIME_CALLS = 5  # 計測に数えない準備呼び出し（構文木・SQLite のページキャッシュを温める。生データには残す）
CALL_TIMEOUT_S = 30.0

FAST_MS = 50  # NFR-PERF-1: p95 < 50 ms
VERIFY_MS = 500  # NFR-PERF-2: p95 < 500 ms

# M1 の 14 ツール（§15.2）。M1-3 の被覆を出す
M1_TOOLS = (
    "configure_workspace", "get_repo_map", "get_file_skeleton", "search_workspace_symbols", "resolve_symbols",
    "get_symbol_bodies", "find_references_and_implementations", "query_call_hierarchy", "get_diagnostics",
    "edit_symbol_body", "rename_symbol", "apply_code_action", "get_project_db_schema", "query_project_db",
)  # fmt: skip


# --- 対象の母集団 ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sym:
    path: str
    name: str
    qualified: str
    kind: str  # "function" | "class" | "method"


_TS_DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?P<kw>function|class)\s+(?P<name>[A-Za-z_$][\w$]*)", re.M
)


def extract_symbols(root: Path, rel: str) -> list[Sym]:
    """サーバーに頼らずにファイルからシンボル名を拾う（測定対象を被測定物から独立に決めるため）。"""
    p = root / rel
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    out: list[Sym] = []
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                out.append(Sym(rel, node.name, node.name, "function"))
            elif isinstance(node, ast.ClassDef):
                out.append(Sym(rel, node.name, node.name, "class"))
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                        out.append(Sym(rel, sub.name, f"{node.name}.{sub.name}", "method"))
    else:
        for m in _TS_DECL.finditer(text):
            kind = "function" if m["kw"] == "function" else "class"
            out.append(Sym(rel, m["name"], m["name"], kind))
    return out


@dataclass(slots=True)
class Pool:
    files: list[str]
    symbols: list[Sym]

    @property
    def callables(self) -> list[Sym]:
        return [s for s in self.symbols if s.kind in ("function", "method")]


def build_pool(root: Path) -> Pool:
    files = R.tier1_files(root)
    symbols: list[Sym] = []
    for rel in files:
        symbols.extend(extract_symbols(root, rel))
    return Pool(files=files, symbols=symbols)


# --- ツール台本 -----------------------------------------------------------------------------

ArgsFn = Callable[[random.Random, Pool], tuple[dict[str, Any], str]]  # (引数, 対象の説明)


def _target(s: Sym) -> dict[str, Any]:
    return {"path": s.path, "symbol_name": s.qualified}


def _a_skeleton(rng: random.Random, pool: Pool) -> tuple[dict[str, Any], str]:
    f = rng.choice(pool.files)
    return {"paths": [f]}, f


def _a_search(rng: random.Random, pool: Pool) -> tuple[dict[str, Any], str]:
    s = rng.choice(pool.symbols)
    # テスト側のシンボルは既定（include_tests=false）では 0 件になり、Tier 4 の grep に落ちて降格応答になる（判定の母集団から外れる）
    return {"query": s.name, "include_tests": True}, s.name


def _a_resolve(rng: random.Random, pool: Pool) -> tuple[dict[str, Any], str]:
    s = rng.choice(pool.symbols)
    return {"targets": [_target(s)]}, f"{s.path}#{s.qualified}"


def _a_refs(rng: random.Random, pool: Pool) -> tuple[dict[str, Any], str]:
    s = rng.choice(pool.callables or pool.symbols)
    return {"target": _target(s), "query_type": "references"}, f"{s.path}#{s.qualified}"


def _a_calls(rng: random.Random, pool: Pool) -> tuple[dict[str, Any], str]:
    s = rng.choice(pool.callables or pool.symbols)
    return {"target": _target(s), "direction": "incoming", "depth": 2}, f"{s.path}#{s.qualified}"


def _a_diag(rng: random.Random, pool: Pool) -> tuple[dict[str, Any], str]:
    f = rng.choice(pool.files)
    return {"paths": [f]}, f


def _a_schema(rng: random.Random, pool: Pool) -> tuple[dict[str, Any], str]:
    return {}, "-"


_SQL_SYMBOLS = (
    "SELECT s.qualified_name, s.kind, s.start_line, f.path FROM symbols s "
    "JOIN files f ON f.file_id = s.file_id WHERE s.name = :n LIMIT 50"
)


def _a_sql(rng: random.Random, pool: Pool) -> tuple[dict[str, Any], str]:
    s = rng.choice(pool.symbols)
    return {"query": _SQL_SYMBOLS, "params": {"n": s.name}}, s.name


@dataclass(frozen=True, slots=True)
class Plan:
    tool: str
    path: str  # "fast" | "verification"
    args: ArgsFn


PLANS: tuple[Plan, ...] = (
    Plan("get_file_skeleton", "fast", _a_skeleton),
    Plan("search_workspace_symbols", "fast", _a_search),
    Plan("resolve_symbols", "fast", _a_resolve),
    Plan("get_symbol_bodies", "fast", _a_resolve),
    Plan("find_references_and_implementations", "fast", _a_refs),
    Plan("query_call_hierarchy", "fast", _a_calls),
    Plan("get_diagnostics", "fast", _a_diag),
    Plan("get_project_db_schema", "fast", _a_schema),
    Plan("query_project_db", "fast", _a_sql),
)
THRESHOLD_MS = {"fast": FAST_MS, "verification": VERIFY_MS}


# --- 1 呼び出し -----------------------------------------------------------------------------


def _rows(env: dict[str, Any]) -> list[Any]:
    """``query_project_db`` の結果行（json のエンベロープの ``data``。キー名は実装によるので順に探す）。"""
    data = env.get("data") or {}
    for key in ("rows", "items", "records"):
        rows = data.get(key)
        if rows:
            return list(rows)
    return []


def _first_cell(env: dict[str, Any]) -> int | None:
    """``query_project_db`` の結果の先頭行の先頭の値（整数）。ウォームアップの件数確認用。"""
    rows = _rows(env)
    if not rows:
        return None
    cell = next(iter(rows[0].values())) if isinstance(rows[0], dict) else rows[0][0]
    try:
        return int(cell)
    except (TypeError, ValueError):
        return None


async def call_once(
    client: S.McpStdioClient, tool: str, args: dict[str, Any], target: str, *, want_rows: bool = False
) -> dict[str, Any]:
    """json 形式で 1 回呼び、記録 1 件にする。``want_rows`` のときは ``query_project_db`` の先頭行を ``rows`` に入れる。"""
    rec: dict[str, Any] = {"target": target, "rtt_ms": None, "server_ms": None, "ok": False, "confidence": None,
                           "degraded": None, "lsp": None, "error": None, "scalar": None}  # fmt: skip
    try:
        res = await client.call_tool(tool, {**args, "format": "json"}, timeout=CALL_TIMEOUT_S)
    except TimeoutError:
        rec["error"] = "CLIENT_TIMEOUT"
        return rec
    except ConnectionError:
        rec["error"] = "CONNECTION_CLOSED"
        return rec
    rec["rtt_ms"] = round(res.rtt_ms, 3)
    if res.rpc_error is not None:
        rec["error"] = f"RPC_{res.rpc_error.get('code')}"
        return rec
    env = res.envelope
    if env is None:
        rec["error"] = "NO_ENVELOPE"
        return rec
    meta = env.get("_resolution_meta") or {}
    rec["server_ms"] = env.get("timing_ms")
    rec["ok"] = bool(env.get("ok")) and not res.is_error
    rec["confidence"] = meta.get("confidence_level")
    rec["degraded"] = meta.get("is_degraded")
    rec["lsp"] = meta.get("lsp_status")
    if rec["ok"] and tool == "configure_workspace":
        jobs = (env.get("data") or {}).get("jobs") or []
        rec["jobs_running"] = sum(1 for j in jobs if str(j.get("state")) in ("queued", "running"))
    if rec["ok"] and tool == "query_project_db":
        rec["scalar"] = _first_cell(env)
        if want_rows:
            rec["rows"] = _rows(env)
    if not rec["ok"]:
        err = env.get("error") or {}
        rec["error"] = err.get("code") or "ERROR"
        rec["error_message"] = str(err.get("message", ""))[:160]
        rec["error_reason"] = (err.get("details") or {}).get("reason")
    return rec


# --- ウォームアップ -------------------------------------------------------------------------


async def warm_up(client: S.McpStdioClient, tools: set[str], pool: Pool, t_spawn: float, timeout_s: float) -> dict[str, Any]:
    """LSP が ``ready``（または適用外）で、索引の件数が安定するまで待つ。所要を記録する（合否には使わない）。"""
    info: dict[str, Any] = {"initialize_ms": round(client.init_ms, 1), "ready_ms": None, "timed_out": False,
                            "polls": 0, "lsp_status": None, "index": {"state": "unknown"}}  # fmt: skip
    deadline = time.perf_counter() + timeout_s
    stable = 0
    last_n: int | None = None
    standby_since: float | None = None
    while True:
        info["polls"] += 1
        lsp: str | None = None
        index_state = "unknown"
        n: int | None = None
        if "query_project_db" in tools:
            r = await call_once(client, "query_project_db", {"query": "SELECT count(*) AS n FROM files"}, "files-count")
            lsp = r["lsp"]
            if r["ok"]:
                n = r["scalar"]
                index_state = "indexed" if (n or 0) > 0 else "empty"
            else:
                if r["error"] == "E_WORKSPACE_NOT_CONFIGURED":
                    # 「サーバーに DB が結線されていない」は待っても変わらない。「まだ DB ファイルが無い」は索引の途中
                    index_state = "index_missing" if r.get("error_reason") == "index_missing" else "not_wired"
                else:
                    index_state = f"error:{r['error']}"
        # LSP の状態は、LSP を使うツールの応答で見る（T-20 は適用外の ``n/a`` を返す）。standby の LSP はこの呼び出しで遅延起動される
        for cand in ("resolve_symbols", "get_diagnostics", "get_file_skeleton"):
            if cand in tools and pool.symbols:
                probe_plan = next(p for p in PLANS if p.tool == cand)
                a, tgt = probe_plan.args(random.Random(0), pool)
                r = await call_once(client, cand, a, tgt)
                if r["lsp"] is not None:
                    lsp = r["lsp"]
                if r["error"] in ("CLIENT_TIMEOUT", "CONNECTION_CLOSED"):
                    info["server_unresponsive_during_warmup"] = True
                    info["ready_ms"] = round((time.perf_counter() - t_spawn) * 1000, 1)
                    return info
                break
        jobs_running: int | None = None
        if "configure_workspace" in tools:
            st = await call_once(client, "configure_workspace", {"action": "status"}, "status")
            if st["ok"]:
                jobs_running = st.get("jobs_running")
        info["lsp_status"] = lsp
        info["index"] = {"state": index_state, "files": n, "jobs_running": jobs_running}
        if lsp == "standby":
            standby_since = standby_since or time.perf_counter()
        else:
            standby_since = None
        # standby が 15 秒続くなら LSP は起動されない（待っても変わらない）: そこで待つのをやめて報告に残す
        lsp_done = lsp in (None, "ready", "n/a", "unavailable") or (
            standby_since is not None and time.perf_counter() - standby_since > 15.0
        )
        if index_state in ("not_wired", "unknown") or index_state.startswith("error:"):
            index_done = True  # 索引が結線されていない（または読めない）: 待っても変わらない
        else:
            stable = stable + 1 if (n is not None and n == last_n and n > 0) else 0
            # 件数が安定していても、索引ジョブが走っているうちは終わりではない（バッチ〈50 ファイル〉ごとのコミットの合間に
            # 件数が止まって見え、索引が途中のまま計測に入ると、後のバッチが検索の 0 件と GIL の取り合いになる。BUG-0027）
            index_done = stable >= 3 and not jobs_running
        last_n = n
        if lsp_done and index_done:
            break
        if time.perf_counter() > deadline:
            info["timed_out"] = True
            break
        await asyncio.sleep(0.5)
    info["ready_ms"] = round((time.perf_counter() - t_spawn) * 1000, 1)
    return info


# --- 集計 -----------------------------------------------------------------------------------


def _pop(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(samples),
        "server_ms": R.summarize([s["server_ms"] for s in samples if s["server_ms"] is not None]),
        "rtt_ms": R.summarize([s["rtt_ms"] for s in samples if s["rtt_ms"] is not None]),
    }


def judge(plan: Plan, samples: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [s for s in samples if s["ok"]]
    by_conf: dict[str, int] = {}
    for s in ok:
        by_conf[str(s["confidence"])] = by_conf.get(str(s["confidence"]), 0) + 1
    errors: dict[str, int] = {}
    for s in samples:
        if not s["ok"]:
            errors[str(s["error"])] = errors.get(str(s["error"]), 0) + 1
    judged = [s for s in ok if s["degraded"] is False]
    exact = [s for s in ok if s["confidence"] == "exact"]
    degraded = [s for s in ok if s["degraded"] is not False]
    threshold = THRESHOLD_MS[plan.path]
    jp = _pop(judged)
    p95 = jp["server_ms"].get("p95")
    if not judged:
        verdict, reason = "no_samples", "no successful non-degraded responses to judge"
    elif p95 is not None and p95 < threshold:
        verdict, reason = "pass", f"server p95 {p95} ms < {threshold} ms"
    else:
        verdict, reason = "fail", f"server p95 {p95} ms >= {threshold} ms"
    return {
        "path": plan.path,
        "threshold_ms": threshold,
        "verdict": verdict,
        "reason": reason,
        "low_population": 0 < len(judged) < 100,
        "calls": len(samples),
        "ok": len(ok),
        "errors": errors,
        "by_confidence": by_conf,
        "judged": jp,
        "exact_only": _pop(exact),
        "degraded": _pop(degraded),
        "all_ok": _pop(ok),
    }


# --- フィクスチャごとの実行 ------------------------------------------------------------------


@dataclass(slots=True)
class FixtureSpec:
    name: str
    root: Path
    sha: str | None = None


def resolve_fixtures(names: list[str]) -> tuple[list[FixtureSpec], list[dict[str, str]]]:
    specs: list[FixtureSpec] = []
    skipped: list[dict[str, str]] = []
    toml_path = BENCH / "fixtures.toml"
    toml: dict[str, Any] = tomllib.loads(toml_path.read_text(encoding="utf-8")) if toml_path.is_file() else {}
    for raw in names:
        key = raw.strip()
        low = key.lower()
        if low in ("poly", "f-poly"):
            specs.append(FixtureSpec("F-POLY", REPO / "tests" / "fixtures" / "poly"))
        elif low in ("self", "lucianke"):
            specs.append(FixtureSpec("SELF", REPO))
        elif key in toml and isinstance(toml[key], dict):
            fx = toml[key]
            root = CACHE / str(fx.get("name", key))
            if root.is_dir():
                specs.append(FixtureSpec(key, root, sha=str(fx.get("sha") or "") or None))
            else:
                skipped.append({"fixture": key, "reason": f"not fetched: {root} (run bench/fetch_fixtures.py)"})
        else:
            skipped.append({"fixture": key, "reason": "unknown fixture name"})
    return specs, skipped


def default_fixture_names() -> list[str]:
    names = ["poly"]
    toml_path = BENCH / "fixtures.toml"
    if toml_path.is_file():
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        names += [k for k, v in data.items() if k != "meta" and isinstance(v, dict)]
    return names


async def run_fixture(spec: FixtureSpec, args: argparse.Namespace) -> dict[str, Any]:
    pool = build_pool(spec.root)
    out: dict[str, Any] = {
        "fixture": R.fixture_info(spec.name, spec.root, sha=spec.sha),
        "population": {"tier1_files": len(pool.files), "symbols": len(pool.symbols)},
        "tools": {},
    }
    if not pool.files or not pool.symbols:
        out["verdict"] = "skip"
        out["reason"] = "no Tier 1 files or symbols found"
        return out
    tmp = Path(tempfile.mkdtemp(prefix="lucianke-bench-lat-"))
    client = S.McpStdioClient(S.server_argv(), cwd=spec.root, env=S.server_env(tmp / "cache"), roots=[spec.root],
                              stderr_path=tmp / "server.stderr.log")  # fmt: skip
    meter = R.CpuLoadMeter()
    t_spawn = time.perf_counter()
    try:
        await client.start()
        tools = await client.list_tools()
        names = {t["name"] for t in tools}
        out["tools_listed"] = sorted(names)
        out["server"] = client.server_info
        out["warmup"] = await warm_up(client, names, pool, t_spawn, args.warmup_timeout)
        planned = {p.tool for p in PLANS}
        hung = ""  # 応答が止まったサーバー（直前のツール名）。以降のツールは待たずにスキップする
        if out["warmup"].get("server_unresponsive_during_warmup"):
            hung = "warm-up probe"
        out["unplanned_tools"] = sorted(names - planned)
        for plan in PLANS:
            if args.tools and plan.tool not in args.tools:
                continue
            if plan.tool not in names:
                out["tools"][plan.tool] = {"verdict": "skip", "reason": "not_in_tools_list", "path": plan.path}
                continue
            if hung:
                out["tools"][plan.tool] = {"verdict": "skip", "path": plan.path,
                                           "reason": f"server unresponsive (no reply to {hung} within {CALL_TIMEOUT_S:.0f}s)"}  # fmt: skip
                print(f"  [{spec.name}] {plan.tool}: skip {out['tools'][plan.tool]['reason']}", flush=True)
                continue
            out["tools"][plan.tool] = await run_tool(client, plan, pool, spec.name, args)
            t = out["tools"][plan.tool]
            if "CLIENT_TIMEOUT" in str(t.get("reason", "")) or t.get("errors", {}).get("CLIENT_TIMEOUT", 0) >= 3:
                hung = plan.tool
            print(f"  [{spec.name}] {plan.tool}: {t['verdict']} {t.get('reason', '')}", flush=True)
    finally:
        rep = await client.close()
        client.abort()
        out["load"] = meter.stop()
        out["watch"] = client.watch_report()
        out["shutdown"] = rep
        with contextlib.suppress(OSError):
            lines = (tmp / "server.stderr.log").read_text(encoding="utf-8", errors="replace").splitlines()
            bad = [ln for ln in lines if " ERROR " in ln or "Traceback" in ln]
            out["server_stderr_errors"] = bad[:20]  # サーバーのログに出たエラー行（E_INTERNAL の原因の手がかり）
            out["server_stderr_tail"] = lines[-15:] if (bad or not out["watch"]["ok"]) else []
        shutil.rmtree(tmp, ignore_errors=True)
    ran = [t["verdict"] for t in out["tools"].values()]
    if not out["watch"]["ok"]:
        out["verdict"], out["reason"] = "fail", "stdout purity or child-process check failed (§14.2)"
    elif "fail" in ran:
        out["verdict"], out["reason"] = "fail", "a tool exceeded its p95 threshold"
    elif "pass" in ran:
        out["verdict"], out["reason"] = "pass", f"{ran.count('pass')} tool(s) passed, none failed"
    else:
        out["verdict"], out["reason"] = "skip", "no tool could be judged"
    return out


async def run_tool(client: S.McpStdioClient, plan: Plan, pool: Pool, fixture: str, args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(f"{args.seed}:{fixture}:{plan.tool}")  # 文字列の種は決定的
    # 1) 探り: 最初の呼び出し（コールド）。全部失敗するならスキップとして記録する
    first_ms: float | None = None
    probe_errors: list[dict[str, Any]] = []
    for attempt in range(6):
        a, tgt = plan.args(rng, pool)
        rec = await call_once(client, plan.tool, a, tgt)
        if attempt == 0:
            first_ms = rec["rtt_ms"]
        if rec["ok"]:
            break
        probe_errors.append({"target": tgt, "error": rec["error"], "message": rec.get("error_message")})
        if rec["error"] in ("E_WORKSPACE_NOT_CONFIGURED", "E_UNSUPPORTED", "CONNECTION_CLOSED", "CLIENT_TIMEOUT"):
            break
    if not rec["ok"]:
        return {"verdict": "skip", "reason": f"probe failed: {probe_errors[-1]['error']}", "path": plan.path,
                "probe_errors": probe_errors, "first_call_rtt_ms": first_ms}  # fmt: skip
    # 2) 準備（数えない）
    prime: list[dict[str, Any]] = []
    for _ in range(PRIME_CALLS):
        a, tgt = plan.args(rng, pool)
        prime.append(await call_once(client, plan.tool, a, tgt))
    # 3) 計測
    samples: list[dict[str, Any]] = []
    timeouts = 0
    for i in range(args.calls):
        a, tgt = plan.args(rng, pool)
        rec = await call_once(client, plan.tool, a, tgt)
        rec["i"] = i
        samples.append(rec)
        timeouts = timeouts + 1 if rec["error"] in ("CLIENT_TIMEOUT", "CONNECTION_CLOSED") else 0
        if timeouts >= 3:
            break
    res = judge(plan, samples)
    res["first_call_rtt_ms"] = first_ms
    res["raw"] = {"prime": prime, "samples": samples}
    return res


# --- 入口 -----------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixtures", default=None, help="カンマ区切り: poly, self, F-PY, F-TS（既定: poly と fixtures.toml の全件）")
    ap.add_argument("--calls", type=int, default=DEFAULT_CALLS, help="ツールごとの計測回数（既定 200）")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--tools", default="", help="カンマ区切りで対象ツールを絞る")
    ap.add_argument("--warmup-timeout", type=float, default=120.0, help="ウォームアップの待ち上限（秒）")
    ap.add_argument("--note", default="", help="結果に残すメモ（例: 負荷あり）")
    args = ap.parse_args()
    args.tools = {t for t in args.tools.split(",") if t}
    names = [n for n in (args.fixtures or ",".join(default_fixture_names())).split(",") if n.strip()]
    specs, skipped = resolve_fixtures(names)

    stamp = R.now_stamp()
    t0 = time.perf_counter()

    def build(runs: list[dict[str, Any]], *, complete: bool) -> dict[str, Any]:
        verdicts = [r["verdict"] for r in runs]
        overall = "fail" if "fail" in verdicts else ("pass" if "pass" in verdicts else "skip")
        m1: dict[str, str] = {}
        for tool in M1_TOOLS:
            states = [r["tools"][tool]["verdict"] for r in runs if tool in r.get("tools", {})]
            m1[tool] = (
                "not_implemented" if not states else "fail" if "fail" in states else "pass" if "pass" in states else states[0]
            )
        return {
            "spec": {"section": "14.4", "nfr": ["NFR-PERF-1", "NFR-PERF-2"], "exit_condition": "M1-3",
                     "population_rule": "ok && !is_degraded (at the tool's ceiling, §5.5); exact-only and degraded reported separately",
                     "judged_on": "server timing_ms (integer ms); client round trip is reported alongside",
                     "thresholds_ms": THRESHOLD_MS, "calls_per_tool": args.calls, "seed": args.seed, "prime_calls": PRIME_CALLS},  # fmt: skip
            "note": args.note,
            "complete": complete,
            "verdict": overall,
            "m1_3_by_tool": m1,
            "skipped_fixtures": skipped,
            "elapsed_s": round(time.perf_counter() - t0, 1),
            "runs": runs,
        }

    async def go() -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for spec in specs:
            print(f"== {spec.name} ({spec.root})", flush=True)
            runs.append(await run_fixture(spec, args))
            R.write_result("latency", build(runs, complete=False), stamp=stamp)  # 途中でも残す
        return runs

    runs = asyncio.run(go())
    payload = build(runs, complete=True)
    path = R.write_result("latency", payload, stamp=stamp)
    print(f"verdict: {payload['verdict']}", f"result: {path}", sep="\n")
    return 0 if payload["verdict"] != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())
