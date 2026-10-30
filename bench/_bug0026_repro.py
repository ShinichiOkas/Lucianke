"""BUG-0026 reproduction check script.

Runs bench/run_latency.py with --fixtures poly --warmup-timeout 90 up to 3 times.
For each run, monitors for CLIENT_TIMEOUT occurrences in the output.
If a hang is detected, attempts to take a py-spy dump of the python.exe child process.

Usage:
    PYTHONUTF8=1 python bench/_bug0026_repro.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH = Path(__file__).resolve().parent
RESULTS = BENCH / "results"


def find_python_child(parent_pid: int) -> int | None:
    """Find child python.exe process of the given parent pid on Windows."""
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             f"Get-WmiObject Win32_Process | Where-Object {{ $_.ParentProcessId -eq {parent_pid} -and $_.Name -like '*python*' }} | Select-Object -First 1 ProcessId | Format-Table -HideTableHeaders"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip()
        if output:
            for line in output.splitlines():
                line = line.strip()
                if line.isdigit():
                    return int(line)
    except Exception as e:
        print(f"[find_python_child] error: {e}", flush=True)
    return None


def find_lucianke_python_children(parent_pid: int) -> list[int]:
    """Find all python.exe children recursively under parent_pid."""
    pids = []
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-WmiObject Win32_Process | Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json"],
            capture_output=True, text=True, timeout=15
        )
        procs = json.loads(result.stdout)
        if isinstance(procs, dict):
            procs = [procs]
        proc_map: dict[int, list[int]] = {}
        proc_names: dict[int, str] = {}
        for p in procs:
            pid = int(p.get("ProcessId", 0))
            ppid = int(p.get("ParentProcessId", 0))
            name = str(p.get("Name", ""))
            proc_names[pid] = name
            if ppid not in proc_map:
                proc_map[ppid] = []
            proc_map[ppid].append(pid)

        queue = [parent_pid]
        visited = set()
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            for child_pid in proc_map.get(current, []):
                name = proc_names.get(child_pid, "")
                if "python" in name.lower():
                    pids.append(child_pid)
                queue.append(child_pid)
    except Exception as e:
        print(f"[find_lucianke_python_children] error: {e}", flush=True)
    return pids


def take_pyspy_dump(pid: int, run_idx: int) -> str | None:
    """Take a py-spy dump of the given pid. Returns dump text or None."""
    out_file = BENCH / f"_pyspy_dump_run{run_idx}_{int(time.time())}.txt"
    try:
        print(f"  [py-spy] dumping pid={pid} -> {out_file}", flush=True)
        result = subprocess.run(
            ["uvx", "py-spy", "dump", "--pid", str(pid)],
            capture_output=True, text=True, timeout=30
        )
        dump_text = result.stdout + result.stderr
        out_file.write_text(dump_text, encoding="utf-8")
        print(f"  [py-spy] dump saved ({len(dump_text)} chars)", flush=True)
        return dump_text
    except Exception as e:
        print(f"  [py-spy] error: {e}", flush=True)
        return None


def run_once(run_idx: int) -> dict:
    """Run latency bench once and return result dict."""
    print(f"\n=== BUG-0026 repro run {run_idx}/3 ===", flush=True)
    cmd = [
        sys.executable, "-m", "uv", "run", "python",
        str(BENCH / "run_latency.py"),
        "--fixtures", "poly",
        "--warmup-timeout", "90",
        "--tools", "resolve_symbols",
        "--calls", "200",
        "--note", f"BUG-0026-repro-run{run_idx}",
    ]
    # Use uv run directly
    cmd = [
        "uv", "run", "python",
        str(BENCH / "run_latency.py"),
        "--fixtures", "poly",
        "--warmup-timeout", "90",
        "--tools", "resolve_symbols",
        "--calls", "200",
        "--note", f"BUG-0026-repro-run{run_idx}",
    ]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    output_lines: list[str] = []
    timeout_count = [0]
    hung = [False]
    pyspy_dump = [None]
    proc_pid = [None]

    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    proc_pid[0] = proc.pid
    print(f"  [run] pid={proc.pid}", flush=True)

    def read_stdout():
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            output_lines.append(line)
            print(f"  [bench] {line}", flush=True)
            if "CLIENT_TIMEOUT" in line:
                timeout_count[0] += 1
                if timeout_count[0] >= 3 and not hung[0]:
                    hung[0] = True
                    print("  [detect] CLIENT_TIMEOUT x3 - attempting py-spy dump", flush=True)
                    # Find the python child process
                    children = find_lucianke_python_children(proc.pid)
                    print(f"  [detect] python children: {children}", flush=True)
                    if children:
                        dump = take_pyspy_dump(children[0], run_idx)
                        pyspy_dump[0] = dump
                    else:
                        print("  [detect] no python child found", flush=True)

    t = threading.Thread(target=read_stdout, daemon=True)
    t.start()

    try:
        proc.wait(timeout=600)
    except subprocess.TimeoutExpired:
        print("  [run] total timeout 600s expired, killing", flush=True)
        proc.kill()

    t.join(timeout=10)

    # Find latest result JSON
    result_files = sorted(RESULTS.glob("*-latency.json"), key=lambda p: p.stat().st_mtime)
    result_path = str(result_files[-1]) if result_files else "(no result file)"

    return {
        "run_idx": run_idx,
        "returncode": proc.returncode,
        "timeout_count": timeout_count[0],
        "hung": hung[0],
        "pyspy_dump": pyspy_dump[0],
        "result_path": result_path,
        "output_tail": output_lines[-20:],
    }


def main():
    print(f"[BUG-0026] repo={REPO}", flush=True)
    print(f"[BUG-0026] HEAD commit: ", end="", flush=True)
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True, text=True)
    head_sha = r.stdout.strip()
    print(head_sha, flush=True)

    results = []
    for i in range(1, 4):
        res = run_once(i)
        results.append(res)
        print(f"\n  [summary run {i}] hung={res['hung']} timeouts={res['timeout_count']} result={res['result_path']}", flush=True)

    print("\n\n=== BUG-0026 REPRO SUMMARY ===", flush=True)
    print(f"HEAD: {head_sha}", flush=True)
    for res in results:
        status = "HUNG (reproduced)" if res["hung"] else "OK (no hang)"
        print(f"  Run {res['run_idx']}: {status} timeouts={res['timeout_count']} result={res['result_path']}", flush=True)
        if res["pyspy_dump"]:
            print(f"    py-spy dump (first 2000 chars):", flush=True)
            print(res["pyspy_dump"][:2000], flush=True)

    any_hung = any(r["hung"] for r in results)
    if any_hung:
        print("\n[RESULT] BUG-0026 REPRODUCED", flush=True)
    else:
        print("\n[RESULT] BUG-0026 NOT reproduced in 3 runs", flush=True)

    return results, head_sha


if __name__ == "__main__":
    main()
