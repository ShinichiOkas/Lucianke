"""NFR-TOK-1 トークン削減率の実行器（仕様書 §14.3 / §14.8）。

使い方（tiktoken はベンチ専用なので ``--with`` で足す。製品の依存には入れない）:
    uv run --with tiktoken python bench/run_tokens.py                    # F-PY・F-TS の S1・S2・S4 を全部
    uv run --with tiktoken python bench/run_tokens.py -f F-PY -s S1      # 絞る
    uv run --with tiktoken python bench/run_tokens.py --baseline-only    # B だけ（サーバーを起こさない）
    uv run --with tiktoken python bench/run_tokens.py -s S1 --variant t04  # S1 の別案（台本の lucianke_t04。無い台本は既定の手順）
    uv run --with tiktoken python bench/run_tokens.py --no-count-schema  # 判定を tools/list のスキーマ無しの L で（既定はスキーマ込み）
    uv run --with tiktoken python bench/run_tokens.py --server-cmd "C:/x/python.exe -P -m lucianke"  # サーバーの起動コマンドの差し替え

台本は bench/scenarios/f-py.toml・f-ts.toml。各シナリオ:
  baseline の手順（基準値 B）:
    op = "grep"   pattern（正規表現）を fixture の scope 配下の対象拡張子から探す。入力 = grep コマンド文字列、出力 = ヒット行
                  （path:line:text）。ヒットしたファイルは全部全文を読む（人手の取捨選択を模さない。同じファイルは 1 回だけ読む）
    op = "read"   ファイル全文（生テキスト。行番号接頭辞は付けない＝B を小さめに見積もる保守側）
    op = "shell"  コマンドを fixture で実行し、その出力を数える（{repo} {path} {diag_cmd} を展開）
    op = "edit"   old/new の文字列（入力）と "ok"（出力）
  lucianke の手順（測定値 L）: tool と args。実プロセスのサーバーに MCP stdio で呼ぶ。入力 = ツール名 + args の JSON、出力 = 応答テキスト。
    測定の前に**条件待ち**をする（固定 sleep ではない。待ちの呼び出しは L に数えない）: (1) ``configure_workspace`` の走査ジョブが done かつ files > 0、
    (2) ``get_diagnostics``（台本の ``warmup_path`` か S4 の ``path``）の応答ヘッダーが ``lsp=ready``（または ``n/a``）。
    手順に ``until = "<正規表現>"`` があれば、その手順を（書き換えるツール以外）応答が一致するまで測定前に呼び直す。
    手順に ``expect = "<正規表現>"`` があれば、測定した応答がそれに一致することを確かめる。外れたら verdict=error（測定不成立。
    「索引にまだ無い」「LSP 起動前の降格応答」のような空の応答を、小さい L として合格に数えないため）。
計数は o200k_base。判定は 1 - L/B >= 0.75。
tools/list のスキーマ記述のトークン（``tools_list_schema_tokens``。ツール名・description・inputSchema の JSON）は、既定では各シナリオの L に
**1 回分含める**（``--count-schema``。厳しめの数え方＝セッション開始時にモデルへ渡るツール一覧の分）。含めた値（``tokens_with_schema``）と
含めない値（``tokens_calls``）は常に両方を結果に書き、削減率・合否も両方（``reduction_with_schema`` / ``reduction_without_schema``、
``verdict_with_schema`` / ``verdict_without_schema``）。``verdict`` / ``reduction`` / ``L.tokens`` は ``--count-schema`` の側（既定はスキーマ込み）。
S4 は固定の注入（inject）を作業コピーに行ってから測り、終わりに ``git checkout`` で戻す。
サーバーは fixture を cwd にして ``python -m lucianke`` で起動する（``server_command``。``tests/_server_cmd.py`` と同じ形。
``uv run ... lucianke`` は ``lucianke.exe`` を握って他の ``uv run`` の同期を壊すので使わない。BUG-0030）。``--server-cmd`` で差し替えられる。
F-TS の S4・S2 は型 LSP（typescript-language-server）が PATH に要る。無いと ``lsp=unavailable``（構文木のみ）になり、S4 は測定不成立、S2 は参照が欠ける。
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


def server_command(override: str | None = None) -> list[str]:
    """サーバーの起動 argv。既定は ``python -m lucianke``（``-P`` は cwd＝fixture を ``sys.path`` に足さない。BUG-0030）。"""
    if override:
        return shlex.split(override, posix=(os.name != "nt"))
    return [sys.executable, "-P", "-m", "lucianke"]


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


LSP_RE = re.compile(r"\blsp=(\S+)")
MUTATING = {"edit_symbol_body", "rename_symbol", "structural_search_replace", "apply_code_action"}
SCAN_TIMEOUT_S = 180.0
LSP_TIMEOUT_S = 180.0
UNTIL_TIMEOUT_S = 90.0


def text_of(res: Any) -> str:
    return "\n".join(getattr(b, "text", "") for b in res.content)


async def warm_up(c: Any, tools: dict[str, Any], sc: dict[str, Any], key: str, warmup_path: str | None) -> dict[str, Any]:
    """測定前の条件待ち（固定 sleep ではない）。待ちの呼び出しは L に数えない。所要と、待ちを打ち切ったかを記録する。"""
    info: dict[str, Any] = {"scan": None, "lsp": None, "until": []}
    t0 = time.perf_counter()
    if "configure_workspace" in tools:
        polls = 0
        while True:
            polls += 1
            res = await c.call_tool("configure_workspace", {"format": "json"})
            done = False
            try:
                data = json.loads(text_of(res)).get("data") or {}
                jobs = data.get("jobs") or []
                done = bool(jobs) and all(j.get("state") == "done" for j in jobs) and (data.get("cache") or {}).get("files", 0) > 0
            except (ValueError, AttributeError):
                pass
            if done or time.perf_counter() - t0 > SCAN_TIMEOUT_S:
                info["scan"] = {"done": done, "polls": polls, "s": round(time.perf_counter() - t0, 1)}
                break
            await asyncio.sleep(0.5)
    path = warmup_path or sc.get("path")
    if path and "get_diagnostics" in tools:
        t1 = time.perf_counter()
        polls = 0
        lsp = None
        while True:
            polls += 1
            res = await c.call_tool("get_diagnostics", {"paths": [path], "min_severity": "error", "max_tokens": 2000})
            m = LSP_RE.search(text_of(res).split("\n", 1)[0])
            lsp = m.group(1) if m else None
            ok = lsp in ("ready", "n/a")
            # unavailable（起動に失敗して再起動も打ち切った）は待っても変わらない。待たずに記録して、expect で測定不成立にする
            if ok or lsp == "unavailable" or time.perf_counter() - t1 > LSP_TIMEOUT_S:
                info["lsp"] = {"ready": ok, "last": lsp, "polls": polls, "s": round(time.perf_counter() - t1, 1)}
                break
            await asyncio.sleep(1.5)
    for st in sc[key]:
        pat = st.get("until")
        if not pat or st["tool"] in MUTATING or st["tool"] not in tools:
            continue
        if (info["lsp"] or {}).get("last") == "unavailable":
            info["until"].append({"tool": st["tool"], "pattern": pat, "matched": False, "polls": 0, "skipped": "lsp unavailable"})
            continue
        t2 = time.perf_counter()
        polls = 0
        while True:
            polls += 1
            res = await c.call_tool(st["tool"], st["args"])
            ok = bool(re.search(pat, text_of(res)))
            if ok or time.perf_counter() - t2 > UNTIL_TIMEOUT_S:
                info["until"].append({"tool": st["tool"], "pattern": pat, "matched": ok, "polls": polls, "s": round(time.perf_counter() - t2, 1)})
                break
            await asyncio.sleep(1.5)
    info["s"] = round(time.perf_counter() - t0, 1)
    return info


async def run_lucianke(
    sc: dict[str, Any], root: Path, cache_dir: Path, key: str, argv: list[str], warmup_path: str | None = None
) -> dict[str, Any]:
    from mcp import Client, StdioServerParameters
    from mcp.shared.exceptions import MCPDeprecationWarning

    warnings.filterwarnings("ignore", category=MCPDeprecationWarning)
    params = StdioServerParameters(
        command=argv[0],
        args=argv[1:],
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
        schema_by_tool = {x["name"]: tok(json.dumps(x, ensure_ascii=False)) for x in listing}
        warm = await warm_up(c, tools, sc, key, warmup_path)
        t0 = time.perf_counter()
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
            text = text_of(res)
            expect = st.get("expect")
            steps.append(
                rec(
                    "tool",
                    name,
                    f"{name} {json.dumps(args, ensure_ascii=False)}",
                    text,
                    is_error=bool(res.is_error),
                    client_ms=ms,
                    header=text.split("\n", 1)[0][:200],
                    expect=expect,
                    expect_met=None if not expect else bool(re.search(expect, text)),
                    response=text[:20000],
                )
            )
    return {
        "tokens_calls": total(steps),
        "tokens_with_schema": total(steps) + schema_tokens,
        "calls": len(steps),
        "steps": steps,
        "skipped_tools": skipped,
        "tools_registered": sorted(tools),
        "warmup": warm,
        "tools_list_schema_tokens": schema_tokens,
        "tools_list_schema_tokens_by_tool": schema_by_tool,
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


def inject(root: Path, sc: dict[str, Any]) -> None:
    """台本の固定の型エラー注入（1 行）を作業コピーに行う。"""
    inj = sc.get("inject")
    if not inj:
        return
    p = root / inj["path"]
    text = read_text(p)
    assert inj["old"] in text, f"inject.old not found in {inj['path']}"
    p.write_bytes(text.replace(inj["old"], inj["new"], 1).encode("utf-8"))


def reset_fixture(root: Path) -> None:
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=root, check=False)


def judge(row: dict[str, Any], count_schema: bool) -> None:
    """row（B と L が入っている）に削減率・合否を書く。スキーマを含める値・含めない値の両方を書き、``verdict`` は ``count_schema`` の側。"""
    L = row["L"]
    basis = "tokens_with_schema" if count_schema else "tokens_calls"
    L["tokens"] = L[basis]
    L["tokens_basis"] = "with_schema" if count_schema else "calls_only"
    errs = [x["label"] for x in L["steps"] if x["is_error"]]
    unmet = [f"{x['label']} expect /{x['expect']}/ header={x['header']}" for x in L["steps"] if x.get("expect_met") is False]
    if errs:
        row.update(verdict="error", reason="ツールがエラーを返した（測定不成立）: " + ", ".join(errs))
    elif unmet:
        row.update(verdict="error", reason="応答が期待と違う（測定不成立）: " + "; ".join(unmet))
    elif L["skipped_tools"]:
        row.update(verdict="skip", reason="ツール未登録: " + ", ".join(sorted(set(L["skipped_tools"]))))
    else:
        for tag, key in (("with_schema", "tokens_with_schema"), ("without_schema", "tokens_calls")):
            red = 1 - L[key] / row["B"]["tokens"]
            row[f"reduction_{tag}"] = round(red, 4)
            row[f"verdict_{tag}"] = "pass" if red >= THRESHOLD else "fail"
        tag = "with_schema" if count_schema else "without_schema"
        row.update(reduction=row[f"reduction_{tag}"], verdict=row[f"verdict_{tag}"])
    if L["tokens_calls"] and row["B"]["tokens"]:
        row["reduction_partial"] = round(1 - L["tokens"] / row["B"]["tokens"], 4)


def judgement(verdicts: list[str]) -> dict[str, Any]:
    return {
        "pass": verdicts.count("pass"),
        "fail": verdicts.count("fail"),
        "skip": verdicts.count("skip"),
        "error": verdicts.count("error"),
        "all_pass": bool(verdicts) and all(v == "pass" for v in verdicts if v != "skip") and "pass" in verdicts,
    }


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def line(row: dict[str, Any]) -> str:
    L = row.get("L")
    head = f"{row['fixture']} {row['scenario']}: B={row['B']['tokens']} ({row['B']['calls']} calls)"
    if not L:
        return f"{head} -> {row['verdict']} {row.get('reason', '')}"
    return (
        f"{head} L={L['tokens_calls']} (+schema {L['tokens_with_schema'] - L['tokens_calls']} = {L['tokens_with_schema']}) "
        f"reduction w/o schema={pct(row.get('reduction_without_schema'))} with schema={pct(row.get('reduction_with_schema'))} "
        f"-> {row['verdict']} {row.get('reason', '')}"
    )


def breakdown(row: dict[str, Any]) -> str:
    """L の内訳（ツール呼び出しごとの入力・出力トークン）と、B の内訳。"""
    L = row["L"]
    schema = L["tokens_with_schema"] - L["tokens_calls"]
    out = [f"  L の内訳 {row['fixture']} {row['scenario']} ({row.get('variant', 'default')}):"]
    out.append(f"    tools/list スキーマ（{len(L['tools_registered'])} 本）: {schema}")
    for st in L["steps"]:
        flag = " [ERROR]" if st["is_error"] else ""
        out.append(f"    {st['label']}: 入力 {st['input_tokens']} + 出力 {st['output_tokens']} = {st['input_tokens'] + st['output_tokens']}{flag}")
    out.append(f"  B の内訳（{row['B']['tokens']}）:")
    for st in row["B"]["steps"]:
        out.append(f"    {st['kind']} {st['label'][:60]}: 入力 {st['input_tokens']} + 出力 {st['output_tokens']}")
    return "\n".join(out)


def summary_table(results: dict[str, Any]) -> str:
    rows = [
        "| fixture | scenario | variant | B | L(呼び出しのみ) | L(+スキーマ) | 削減率(スキーマ無) | 削減率(スキーマ込) | 判定(込) | 判定(無) |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in results["scenarios"]:
        L = r.get("L") or {}
        rows.append(
            f"| {r['fixture']} | {r['scenario']} | {r.get('variant', '-')} | {r.get('B', {}).get('tokens', '-')} | {L.get('tokens_calls', '-')} | {L.get('tokens_with_schema', '-')} "
            f"| {pct(r.get('reduction_without_schema'))} | {pct(r.get('reduction_with_schema'))} | {r.get('verdict_with_schema', r['verdict'])} | {r.get('verdict_without_schema', r['verdict'])} |"
        )
    return "\n".join(rows)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--fixture", action="append", help="F-PY / F-TS（省略で両方）")
    ap.add_argument("-s", "--scenario", action="append", help="S1 / S2 / S4（省略で全部）")
    ap.add_argument("--baseline-only", action="store_true", help="B だけ測る（サーバーを起こさない）")
    ap.add_argument("--variant", default="", help="L の手順の別案（台本の lucianke_<variant>。例: t04 = resolve_symbols 版）")
    ap.add_argument(
        "--count-schema",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="判定の L に tools/list のスキーマ記述のトークンを 1 回分含める（既定オン）。含める値・含めない値は常に両方を結果に書く",
    )
    ap.add_argument("--server-cmd", default=None, help="サーバーの起動コマンド（既定: <この python> -P -m lucianke）")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    fxs = ff.load_fixtures()
    results: dict[str, Any] = {
        "schema": "lucianke-bench-tokens/1",
        "date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "encoding": "o200k_base",
        "threshold": THRESHOLD,
        "count_schema": args.count_schema,
        "server_argv": server_command(args.server_cmd),
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
                inject(root, sc)
                row["B"] = run_baseline(sc, root, scn)
                if not args.baseline_only:
                    # B の edit は作業コピーを実際に直す（次の診断は直した後の出力）ので、L の前に戻して注入し直す。
                    # これをしないと L の最初の診断は 0 件・編集は no-op になり、小さい L を合格に数えてしまう
                    reset_fixture(root)
                    inject(root, sc)
                if args.baseline_only:
                    row.update(verdict="baseline-only")
                else:
                    key = "lucianke" + (f"_{args.variant}" if args.variant and f"lucianke_{args.variant}" in sc else "")
                    row["variant"] = args.variant if key != "lucianke" else "default"
                    row["L"] = asyncio.run(
                        run_lucianke(sc, root, cache_dir, key, server_command(args.server_cmd), scn.get("warmup_path"))
                    )
                    judge(row, args.count_schema)
            finally:
                reset_fixture(root)
            results["scenarios"].append(row)
            print(line(row))
            if row["verdict"] in ("fail", "error") or row.get("verdict_without_schema") == "fail":
                print(breakdown(row))
    results["judgement"] = judgement([r["verdict"] for r in results["scenarios"]])
    results["judgement_with_schema"] = judgement([r.get("verdict_with_schema", r["verdict"]) for r in results["scenarios"]])
    results["judgement_without_schema"] = judgement([r.get("verdict_without_schema", r["verdict"]) for r in results["scenarios"]])
    print(summary_table(results))
    suffix = f"-{args.variant}" if args.variant else ""
    out = args.out or BENCH / "results" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{platform.system()}-tokens{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")
    return 1 if results["judgement"]["fail"] or results["judgement"]["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
