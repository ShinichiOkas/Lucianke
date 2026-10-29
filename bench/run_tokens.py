"""NFR-TOK-1 トークン削減率の実行器（仕様書 §14.3 / §14.8）。

使い方（tiktoken はベンチ専用なので ``--with`` で足す。製品の依存には入れない）:
    uv run --with tiktoken python bench/run_tokens.py                    # F-PY・F-TS の S1・S2・S4 を全部
    uv run --with tiktoken python bench/run_tokens.py -f F-PY -s S1      # 絞る
    uv run --with tiktoken python bench/run_tokens.py --baseline-only    # B だけ（サーバーを起こさない）

台本は bench/scenarios/f-py.toml・f-ts.toml。各シナリオ:
  baseline の手順（基準値 B）:
    op = "grep"   pattern（正規表現）を fixture の scope 配下の対象拡張子から探す。入力 = grep コマンド文字列、出力 = ヒット行
                  （path:line:text）。ヒットしたファイルは全部全文を読む（人手の取捨選択を模さない。同じファイルは 1 回だけ読む）
    op = "read"   ファイル全文（生テキスト。行番号接頭辞は付けない＝B を小さめに見積もる保守側）
    op = "shell"  コマンドを fixture で実行し、その出力を数える（{repo} {path} {diag_cmd} を展開）
    op = "edit"   old/new の文字列（入力）と "ok"（出力）
  lucianke の手順（測定値 L）: tool と args。実プロセスのサーバーに MCP stdio で呼ぶ。入力 = ツール名 + args の JSON、出力 = 応答テキスト。
計数は o200k_base。判定は 1 - L/B >= 0.75。tools/list のスキーマ記述のトークンは別欄（参考）に記録し、L には入れない。
S4 は固定の注入（inject）を作業コピーに行ってから測り、終わりに ``git checkout`` で戻す。
サーバーは fixture を cwd にして ``uv run --project <repo> lucianke`` で起動する（``--directory`` は cwd を変えるため使えない）。
ツールが未登録の手順があるシナリオの L は「skip: ツール未登録」とし、そこまでの呼び出しは記録する。落ちない。
結果: bench/results/<日時>-<OS>-tokens.json（フィクスチャ SHA、Lucianke のコミット、機材、生データ、判定）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
import tomllib
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import tiktoken

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
sys.path.insert(0, str(BENCH))
import fetch_fixtures as ff  # noqa: E402

THRESHOLD = 0.75
ENC = tiktoken.get_encoding("o200k_base")
FIXTURE_FILES = {"F-PY": "f-py.toml", "F-TS": "f-ts.toml"}


def tok(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def rec(kind: str, label: str, inp: str, out: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "input_tokens": tok(inp),
        "output_tokens": tok(out),
        "input_chars": len(inp),
        "output_chars": len(out),
        **extra,
    }


def total(steps: list[dict[str, Any]]) -> int:
    return sum(s["input_tokens"] + s["output_tokens"] for s in steps)


# --- 基準値 B ------------------------------------------------------------------------------------


def fixture_files(root: Path, scope: list[str], globs: list[str]) -> list[Path]:
    out: list[Path] = []
    for d in scope:
        for g in globs:
            out += [p for p in (root / d).rglob(g) if p.is_file() and "node_modules" not in p.parts]
    return sorted(set(out))


def read_text(p: Path) -> str:
    return p.read_bytes().decode("utf-8", errors="replace")


def run_baseline(sc: dict[str, Any], root: Path, scn: dict[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    files = fixture_files(root, scn["scope"], scn["src_glob"])
    read_done: set[str] = set()
    ctx = {"repo": str(REPO), "path": sc.get("path", ""), "diag_cmd": ""}
    ctx["diag_cmd"] = sc.get("diag_cmd", "").format(**ctx)
    for st in sc["baseline"]:
        op = st["op"]
        if op == "grep":
            pat = re.compile(st["pattern"])
            hits: list[str] = []
            hit_files: list[str] = []
            for f in files:
                rel = f.relative_to(root).as_posix()
                lines = read_text(f).splitlines()
                got = [f"{rel}:{i}:{ln}" for i, ln in enumerate(lines, 1) if pat.search(ln)]
                if got:
                    hits += got
                    hit_files.append(rel)
            cmd = f"grep -rnE {shlex.quote(st['pattern'])} {' '.join(scn['scope'])}"
            steps.append(rec("grep", st["pattern"], cmd, "\n".join(hits), hit_files=len(hit_files), hit_lines=len(hits)))
            for rel in hit_files:  # ヒットしたファイルは全部全文を読む
                if rel not in read_done:
                    read_done.add(rel)
                    steps.append(rec("read", rel, f"read {rel}", read_text(root / rel)))
        elif op == "read":
            rel = st["path"]
            if rel not in read_done:
                read_done.add(rel)
                steps.append(rec("read", rel, f"read {rel}", read_text(root / rel)))
        elif op == "shell":
            cmd = st["cmd"].format(**ctx)
            p = subprocess.run(  # noqa: S602  台本のコマンド（bench/scenarios）を実行する
                cmd,
                cwd=root,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONUTF8": "1", "LANG": "en_US.UTF-8"},
                check=False,
            )
            steps.append(rec("shell", cmd, cmd, (p.stdout + p.stderr).strip(), exit_code=p.returncode))
        elif op == "edit":
            target = root / st["path"]
            before = read_text(target)
            assert st["old"] in before, f"edit.old not found in {st['path']}"
            target.write_bytes(before.replace(st["old"], st["new"], 1).encode("utf-8"))  # 実際に直す（次の診断は直した後の出力）
            payload = json.dumps({"path": st["path"], "old": st["old"], "new": st["new"]}, ensure_ascii=False)
            steps.append(rec("edit", st["path"], payload, "ok"))
        else:
            raise ValueError(f"unknown baseline op: {op}")
    return {"tokens": total(steps), "calls": len(steps), "steps": steps}


# --- 測定値 L ------------------------------------------------------------------------------------


def original_symbol_text(root: Path, inj: dict[str, str]) -> str:
    """inject.old の行から、次の列 0 の ``};`` までを元のファイル（HEAD）から取る（TS の関数用）。"""
    orig = subprocess.run(
        ["git", "show", f"HEAD:{inj['path']}"], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.splitlines()
    i = next(k for k, ln in enumerate(orig) if ln == inj["old"])
    j = next(k for k in range(i, len(orig)) if orig[k].startswith("};"))
    return "\n".join(orig[i : j + 1])


async def run_lucianke(sc: dict[str, Any], root: Path, cache_dir: Path, key: str) -> dict[str, Any]:
    from mcp import Client, StdioServerParameters
    from mcp.shared.exceptions import MCPDeprecationWarning

    warnings.filterwarnings("ignore", category=MCPDeprecationWarning)
    params = StdioServerParameters(
        command="uv",
        args=["run", "--project", str(REPO), "lucianke"],
        cwd=str(root),
        env={**os.environ, "PYTHONUTF8": "1", "LUCIANKE_CACHE_DIR": str(cache_dir)},
    )
    steps: list[dict[str, Any]] = []
    skipped: list[str] = []
    t0 = time.perf_counter()
    async with Client(params, mode="legacy", cache=None) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        listing = [{"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in tools.values()]
        schema_tokens = tok(json.dumps(listing, ensure_ascii=False))
        for st in sc[key]:
            name = st["tool"]
            if name not in tools:
                skipped.append(name)
                continue
            args = json.loads(json.dumps(st["args"]))
            if args.get("new_code") == "@ORIGINAL@":
                args["new_code"] = original_symbol_text(root, sc["inject"])
            t1 = time.perf_counter()
            res = await c.call_tool(name, args)
            ms = round((time.perf_counter() - t1) * 1000, 1)
            text = "\n".join(getattr(b, "text", "") for b in res.content)
            steps.append(
                rec(
                    "tool",
                    name,
                    f"{name} {json.dumps(args, ensure_ascii=False)}",
                    text,
                    is_error=bool(res.is_error),
                    client_ms=ms,
                    response=text[:20000],
                )
            )
    return {
        "tokens": total(steps),
        "calls": len(steps),
        "steps": steps,
        "skipped_tools": skipped,
        "tools_registered": sorted(tools),
        "tools_list_schema_tokens": schema_tokens,
        "wall_s": round(time.perf_counter() - t0, 2),
    }


# --- 実行 ----------------------------------------------------------------------------------------


def machine() -> dict[str, Any]:
    mem = None
    if sys.platform == "win32":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("p", ctypes.c_ulong)] + [(f"x{i}", ctypes.c_ulonglong) for i in range(7)]

            m = MS()
            m.l = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):  # type: ignore[attr-defined]
                mem = round(m.x0 / 2**30, 1)
        except Exception:  # noqa: BLE001
            pass
    return {
        "os": platform.system(),
        "os_release": platform.platform(),
        "cpu": platform.processor(),
        "cpu_count": os.cpu_count(),
        "memory_gib": mem,
        "python": platform.python_version(),
        "storage": "unrecorded",
    }


def lucianke_commit() -> dict[str, Any]:
    def run(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, check=False).stdout.strip()

    return {"sha": run("rev-parse", "HEAD"), "src_dirty": bool(run("status", "--porcelain", "--", "src"))}


def reset_fixture(root: Path) -> None:
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=root, check=False)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--fixture", action="append", help="F-PY / F-TS（省略で両方）")
    ap.add_argument("-s", "--scenario", action="append", help="S1 / S2 / S4（省略で全部）")
    ap.add_argument("--baseline-only", action="store_true", help="B だけ測る（サーバーを起こさない）")
    ap.add_argument("--variant", default="", help="L の手順の別案（台本の lucianke_<variant>。例: t04 = resolve_symbols 版）")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    fxs = ff.load_fixtures()
    results: dict[str, Any] = {
        "schema": "lucianke-bench-tokens/1",
        "date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "encoding": "o200k_base",
        "threshold": THRESHOLD,
        "machine": machine(),
        "lucianke": lucianke_commit(),
        "fixtures": {},
        "scenarios": [],
    }
    cache_dir = ff.CACHE / "lk-cache"
    for fname in args.fixture or list(FIXTURE_FILES):
        fx = fxs[fname]
        root = ff.fetch(fx)
        scn = tomllib.loads((BENCH / "scenarios" / FIXTURE_FILES[fname]).read_text(encoding="utf-8"))
        results["fixtures"][fname] = {"name": fx["name"], "url": fx["url"], "sha": fx["sha"]}
        if fname == "F-PY":
            (root / "pyrightconfig.json").write_text('{"typeCheckingMode": "standard"}\n', encoding="utf-8")
        for sid in args.scenario or ["S1", "S2", "S3", "S4"]:
            sc = scn.get(sid)
            if sc is None:
                continue
            row: dict[str, Any] = {"fixture": fname, "scenario": sid, "title": sc.get("title", "")}
            if "skip" in sc:
                row.update(verdict="skip", reason=sc["skip"])
                results["scenarios"].append(row)
                continue
            try:
                if "inject" in sc:
                    inj = sc["inject"]
                    p = root / inj["path"]
                    text = read_text(p)
                    assert inj["old"] in text, f"inject.old not found in {inj['path']}"
                    p.write_bytes(text.replace(inj["old"], inj["new"], 1).encode("utf-8"))
                row["B"] = run_baseline(sc, root, scn)
                if args.baseline_only:
                    row.update(verdict="baseline-only")
                else:
                    key = "lucianke" + (f"_{args.variant}" if args.variant else "")
                    row["variant"] = args.variant or "default"
                    row["L"] = asyncio.run(run_lucianke(sc, root, cache_dir, key))
                    errs = [x["label"] for x in row["L"]["steps"] if x["is_error"]]
                    if errs:
                        row.update(verdict="error", reason="ツールがエラーを返した（測定不成立）: " + ", ".join(errs))
                    elif row["L"]["skipped_tools"]:
                        row.update(verdict="skip", reason="ツール未登録: " + ", ".join(sorted(set(row["L"]["skipped_tools"]))))
                    else:
                        red = 1 - row["L"]["tokens"] / row["B"]["tokens"]
                        row.update(reduction=round(red, 4), verdict="pass" if red >= THRESHOLD else "fail")
                    if row["L"]["tokens"] and row["B"]["tokens"]:
                        row["reduction_partial"] = round(1 - row["L"]["tokens"] / row["B"]["tokens"], 4)
            finally:
                reset_fixture(root)
            results["scenarios"].append(row)
            lt = row.get("L", {}).get("tokens")
            red = row.get("reduction", row.get("reduction_partial"))
            print(f"{fname} {sid}: B={row['B']['tokens']} ({row['B']['calls']} calls) L={lt} reduction={red} -> {row['verdict']} {row.get('reason', '')}")
    verdicts = [r["verdict"] for r in results["scenarios"]]
    results["judgement"] = {
        "pass": verdicts.count("pass"),
        "fail": verdicts.count("fail"),
        "skip": verdicts.count("skip"),
        "error": verdicts.count("error"),
        "all_pass": bool(verdicts) and all(v == "pass" for v in verdicts if v != "skip") and "pass" in verdicts,
    }
    out = args.out or BENCH / "results" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{platform.system()}-tokens.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")
    return 1 if results["judgement"]["fail"] or results["judgement"]["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
