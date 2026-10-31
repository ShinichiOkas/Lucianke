"""Capability benchmark runner: the same agent tasks answered by several code-intelligence engines.

Generic: the tasks and their ground truth live with the analysed project (``--tasks``), the workspace path comes
from ``--workspace`` or ``LUCIANKE_BENCH_MNEMO`` (default ``../Mnemo``). Nothing project-specific is in this file.

    uv run python bench/capability/run.py --tasks <tasks.toml> --workspace <dir> \
        --engines lucianke,serena,grep --out <dir> [--serena-cmd "<serena executable>"]
    uv run python bench/capability/run.py --report --tasks <tasks.toml> --out <dir> [--notes <file.md>]

Per engine it writes ``<out>/<date>-<engine>.json``; ``--report`` (also run at the end of a measuring run) writes
``<out>/<date>.md``. Each task is run cold (first time that query is asked) and then warm (the same calls again),
except tasks an adapter marks as non-repeatable. See README.md for the metrics and the scoring rules.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import scoring  # noqa: E402
from adapters import ADAPTERS  # noqa: E402

SCHEMA = 1


def _git_head(path: Path) -> str | None:
    try:
        cp = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,  # noqa: S603
                            timeout=20, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout.strip() or None


def _load_tasks(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _probe_path(tasks: list[dict[str, Any]], workspace: Path) -> str | None:
    """A source file the engines can open to start their language server (the first existing path a task names)."""
    for t in tasks:
        path = t.get("target", {}).get("path") or t.get("path")
        if path and (workspace / path).is_file():
            return path
    return None


async def run_engine(
    engine: str, args: argparse.Namespace, spec: dict[str, Any], workspace: Path, work: Path
) -> dict[str, Any]:
    tasks = [t for t in spec["task"] if not args.only or t["id"] in args.only.split(",")]
    options = {
        "timeout": args.timeout,
        "serena_cmd": args.serena_cmd,
        "lucianke_exe": args.lucianke_exe,
        "probe_path": _probe_path(spec["task"], workspace),
        "ready_timeout": args.ready_timeout,
        "poll_s": args.poll_s,
    }
    adapter = ADAPTERS[engine](workspace, work / engine, options)
    (work / engine).mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "engine": engine,
        "date": args.date,
        "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": platform.platform(),
        "workspace": {
            "name": spec.get("meta", {}).get("project"),
            "revision": spec.get("meta", {}).get("revision"),
        },
        "lucianke_commit": _git_head(REPO),
        "tasks": [],
    }
    t0 = time.perf_counter()
    try:
        result["setup"] = await adapter.setup()
        print(f"[{engine}] setup: {json.dumps(result['setup'], ensure_ascii=False)[:300]}", flush=True)
        for task in tasks:
            entry: dict[str, Any] = {"id": task["id"], "kind": task["kind"], "title": task.get("title")}
            try:
                cold_calls, answer = await adapter.run(task)
                warm_calls = None
                if adapter.can_repeat(task) and cold_calls and not answer.unsupported:
                    warm_calls, _ = await adapter.run(task)
            except Exception as exc:  # noqa: BLE001  one task's failure must not lose the others
                entry.update(
                    {"error": f"{type(exc).__name__}: {exc}"[:500], "score": {"pass": False, "f1": 0.0}}
                )
                result["tasks"].append(entry)
                print(f"[{engine}] {task['id']} ERROR {exc}", flush=True)
                continue
            ans = answer.to_json()
            full = dict(ans)
            full["text"] = answer.text
            sc = scoring.score(task, full, workspace)
            entry.update(
                {
                    "cold_ms": round(sum(c.ms for c in cold_calls), 1),
                    "warm_ms": round(sum(c.ms for c in warm_calls), 1) if warm_calls else None,
                    "calls": len(cold_calls),
                    "tokens": sum(c.tokens for c in cold_calls),
                    "call_log": [c.to_json(keep_text=args.keep_text) for c in cold_calls],
                    "errors": [c.error for c in cold_calls if c.error],
                    "answer": ans,
                    "score": sc,
                }
            )
            raw = work / engine / f"{task['id']}.txt"
            raw.write_bytes(
                "\n\n----- next call -----\n\n".join(
                    f"# {c.tool} {json.dumps(c.args, ensure_ascii=False)}\n{c.text}" for c in cold_calls
                ).encode("utf-8")
            )
            result["tasks"].append(entry)
            print(
                f"[{engine}] {task['id']} {task['kind']}: f1={sc.get('f1')} pass={sc.get('pass')} "
                f"tokens={entry['tokens']} cold={entry['cold_ms']}ms warm={entry['warm_ms']}ms",
                flush=True,
            )
    finally:
        result["close"] = await adapter.close()
        result["version"] = adapter.version()
        result["total_s"] = round(time.perf_counter() - t0, 1)
    return result


# --- report ---------------------------------------------------------------------------------------


def _fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def summarize(res: dict[str, Any]) -> dict[str, Any]:
    tasks = res["tasks"]
    supported = [t for t in tasks if t.get("score", {}).get("f1") is not None]
    f1s = [t["score"]["f1"] for t in supported]
    f1_all = [t["score"]["f1"] if t.get("score", {}).get("f1") is not None else 0.0 for t in tasks]
    cold = [t["cold_ms"] for t in tasks if t.get("cold_ms") is not None and t.get("calls")]
    warm = [t["warm_ms"] for t in tasks if t.get("warm_ms") is not None]
    return {
        "tasks": len(tasks),
        "supported": len(supported),
        "passed": sum(1 for t in tasks if t.get("score", {}).get("pass")),
        "mean_f1_supported": round(statistics.mean(f1s), 3) if f1s else None,
        "mean_f1_all": round(statistics.mean(f1_all), 3) if f1_all else None,
        "tokens": sum(t.get("tokens", 0) for t in tasks),
        "median_cold_ms": statistics.median(cold) if cold else None,
        "median_warm_ms": statistics.median(warm) if warm else None,
        "setup_ms": (res.get("setup") or {}).get("setup_ms"),
    }


def write_report(args: argparse.Namespace, spec: dict[str, Any]) -> Path:
    out = Path(args.out)
    results = {}
    for eng in args.engines.split(","):
        p = out / f"{args.date}-{eng}.json"
        if p.is_file():
            results[eng] = json.loads(p.read_text(encoding="utf-8"))
    engines = list(results)
    meta = spec.get("meta", {})
    lines = [
        f"# Capability benchmark: {meta.get('project', '?')} @ {meta.get('revision', '?')} ({args.date})",
        "",
    ]
    lines.append(
        "Generated by `bench/capability/run.py` in the Lucianke repository. Each cell: score / response "
        "tokens / cold latency (warm latency). Score is F1 for site sets and call hierarchies, recall for "
        "skeleton / body / orientation / diagnostics, 1 or 0 for the definition. `unsup.` = the engine "
        "has no equivalent tool. ✓ = the task's pass rule is met."
    )
    lines.append("")
    vers = []
    for e in engines:
        r = results[e]
        v = r.get("version", {})
        extra = f", Lucianke commit `{(r.get('lucianke_commit') or '?')[:10]}`" if e == "lucianke" else ""
        vers.append(
            f"- **{e}**: {v.get('engine', e)} {v.get('detail', '') or ''}{extra} — run {r.get('started')}"
        )
    lines += vers + [""]
    head = "| Task | " + " | ".join(engines) + " |"
    lines += [head, "|" + "---|" * (len(engines) + 1)]
    by_id: dict[str, dict[str, Any]] = {}
    for e in engines:
        for t in results[e]["tasks"]:
            by_id.setdefault(t["id"], {})[e] = t
    for task in spec["task"]:
        row = [f"{task['id']} {task.get('title', '')}"]
        for e in engines:
            t = by_id.get(task["id"], {}).get(e)
            if t is None:
                row.append("-")
                continue
            sc = t.get("score", {})
            if sc.get("supported") is False:
                row.append("unsup.")
                continue
            if t.get("error"):
                row.append(f"error: {t['error'][:60]}")
                continue
            f1 = sc.get("f1")
            mark = " ✓" if sc.get("pass") else ""
            row.append(
                f"{f1:.2f}{mark} / {t.get('tokens', 0):,} tok / {_fmt_ms(t.get('cold_ms'))}"
                f" ({_fmt_ms(t.get('warm_ms'))})"
            )
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## Totals", "", "| | " + " | ".join(engines) + " |", "|" + "---|" * (len(engines) + 1)]
    sums = {e: summarize(results[e]) for e in engines}
    rows = [
        ("Tasks supported", lambda s: f"{s['supported']}/{s['tasks']}"),
        ("Tasks passed", lambda s: f"{s['passed']}/{s['tasks']}"),
        (
            "Mean score (supported tasks)",
            lambda s: f"{s['mean_f1_supported']:.3f}" if s["mean_f1_supported"] is not None else "-",
        ),
        (
            "Mean score (all tasks, unsupported = 0)",
            lambda s: f"{s['mean_f1_all']:.3f}" if s["mean_f1_all"] is not None else "-",
        ),
        ("Total response tokens", lambda s: f"{s['tokens']:,}"),
        ("Median latency, cold", lambda s: _fmt_ms(s["median_cold_ms"])),
        ("Median latency, warm", lambda s: _fmt_ms(s["median_warm_ms"])),
        ("Setup (ready to answer)", lambda s: _fmt_ms(s["setup_ms"])),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(sums[e]) for e in engines) + " |")
    lines += ["", "## Setup details", ""]
    for e in engines:
        lines.append(f"- **{e}**: `{json.dumps(results[e].get('setup', {}), ensure_ascii=False)[:600]}`")
    lines += ["", "## Per-task details (misses and false positives)", ""]
    for task in spec["task"]:
        parts = []
        for e in engines:
            t = by_id.get(task["id"], {}).get(e)
            if not t:
                continue
            sc = t.get("score", {})
            bits = []
            for k in ("precision", "recall"):
                if k in sc:
                    bits.append(f"{k[0].upper()}={sc[k]}")
            if "prod" in sc and isinstance(sc["prod"], dict):
                bits.append(f"prod F1={sc['prod']['f1']}")
            if "tests" in sc and isinstance(sc["tests"], dict):
                bits.append(f"tests F1={sc['tests']['f1']}")
            if sc.get("traps_reported"):
                bits.append(f"traps={len(sc['traps_reported'])}")
            if sc.get("missed"):
                bits.append(f"missed {len(sc['missed'])}: " + ", ".join(map(str, sc["missed"][:6])))
            if sc.get("false_positives"):
                bits.append(
                    f"FP {sc.get('fp', len(sc['false_positives']))}: " + ", ".join(sc["false_positives"][:4])
                )
            if sc.get("reason"):
                bits.append(sc["reason"])
            if t.get("errors"):
                bits.append("tool errors: " + "; ".join(e2[:80] for e2 in t["errors"][:2]))
            notes = t.get("answer", {}).get("notes") or []
            if notes:
                bits.append("notes: " + "; ".join(notes)[:200])
            parts.append(f"  - {e}: " + "; ".join(bits))
        lines.append(f"- **{task['id']}** {task.get('title', '')}")
        lines += parts
    if args.notes and Path(args.notes).is_file():
        lines += ["", Path(args.notes).read_text(encoding="utf-8").rstrip()]
    md = out / f"{args.date}.md"
    md.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return md


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", required=True, type=Path)
    ap.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="analysed project (default: $LUCIANKE_BENCH_MNEMO or ../Mnemo)",
    )
    ap.add_argument("--engines", default="lucianke,serena,grep")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--only", default="", help="comma-separated task ids")
    ap.add_argument(
        "--work-dir", type=Path, default=None, help="caches, logs and raw answers (default: a temp dir)"
    )
    ap.add_argument(
        "--serena-cmd",
        default="uvx --from serena-agent==2.0.0 serena",
        help="command that runs the serena CLI",
    )
    ap.add_argument(
        "--lucianke-exe", default=None, help="use a built lucianke executable instead of the source"
    )
    ap.add_argument("--timeout", type=float, default=1800.0, help="per tool call, seconds")
    ap.add_argument(
        "--ready-timeout", type=float, default=7200.0, help="Lucianke index/LSP readiness, seconds"
    )
    ap.add_argument("--poll-s", type=float, default=30.0, help="Lucianke readiness poll interval, seconds")
    ap.add_argument("--keep-text", action="store_true", help="keep the full response text in the JSON")
    ap.add_argument("--report", action="store_true", help="only (re)write <out>/<date>.md from existing JSON")
    ap.add_argument("--notes", default=None, help="a Markdown file appended to the report")
    args = ap.parse_args()
    spec = _load_tasks(args.tasks)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.report:
        print(write_report(args, spec))
        return 0
    workspace = (args.workspace or Path(os.environ.get("LUCIANKE_BENCH_MNEMO", "../Mnemo"))).resolve()
    if not workspace.is_dir():
        print(f"workspace not found: {workspace}", file=sys.stderr)
        return 2
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="capbench-"))
    work.mkdir(parents=True, exist_ok=True)
    for engine in args.engines.split(","):
        if engine not in ADAPTERS:
            print(f"unknown engine {engine}; known: {', '.join(ADAPTERS)}", file=sys.stderr)
            return 2
        res = asyncio.run(run_engine(engine, args, spec, workspace, work))
        res["summary"] = summarize(res)
        p = args.out / f"{args.date}-{engine}.json"
        p.write_bytes((json.dumps(res, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        print(f"wrote {p}", flush=True)
    print(write_report(args, spec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
