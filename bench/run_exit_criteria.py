"""M1 の出口条件（仕様書 §15.2 の M1-1〜M1-8）の検査を順に走らせ、結果を記録する実行器。

検査の一覧は ``bench/exit_criteria.toml``（1 項目 1 検査以上。判定はすべて機械。人手の項目は無い。§14.1）。

使い方::

    uv run python bench/run_exit_criteria.py                       # 全部
    uv run python bench/run_exit_criteria.py --skip-slow           # slow = true を飛ばす
    uv run python bench/run_exit_criteria.py --only M1-5,M1-8.license-check   # 出口条件・検査の id・接頭辞
    uv run python bench/run_exit_criteria.py --list                # 走らせずに計画だけ見る

結果:

* ``bench/results/<日時>-<OS>-exit.json``（Lucianke のコミット・作業ツリーが dirty か・機材・検査ごとの生データと判定）
* ``.pair-agent/acceptance/M1-exit-<日時>.md``（要約。合否と不合格・未整備の項目を先頭に）

終了コードは出口条件の全部が合格なら 0、そうでなければ 1（赤い項目があってよい。それが現在地）。
標準ライブラリだけで動く（pytest・各台本は子プロセスとして、この実行器と同じ Python で起動する）。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
import _results as R  # noqa: E402
import fetch_fixtures as ff  # noqa: E402

REPO = R.REPO
TOML = BENCH / "exit_criteria.toml"
ACCEPTANCE = REPO / ".pair-agent" / "acceptance"

CRITERIA: dict[str, str] = {
    "M1-1": "14 ツールが第 11 章の事後条件を F-PY、F-TS、F-POLY で満たす",
    "M1-2": "§10.10 の EP-1〜EP-9 を満たす",
    "M1-3": "NFR-PERF-1 を M1 の 14 ツールで満たす（§14.4）",
    "M1-4": "NFR-TOK-1 をシナリオ S1、S2、S4 で満たす（S3 は M2）",
    "M1-5": "NFR-SAFE-1〜4、NFR-OPS-1〜4 を満たす",
    "M1-6": "Claude Code の .mcp.json で接続し、台本化したセッションが完走する",
    "M1-7": "Windows / macOS / Linux で M1-1〜M1-6 が同じ結果になる",
    "M1-8": "L-8 のライセンス検査が CI で通る。LICENSE と THIRD_PARTY_NOTICES.md が配布物に含まれる",
}
FAILING = {"fail", "timeout", "error"}
WORDS = {
    "pass": "合格", "fail": "不合格", "timeout": "不合格(時間超過)", "error": "不合格(実行できず)",
    "missing": "未整備", "blocked": "前提不足", "skipped": "未実行", "not_run": "未実行",
}  # fmt: skip
TAIL_LINES = 40


# --- 前提の確認 -------------------------------------------------------------------------------


def git_tracked(rel: str) -> bool:
    cp = subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel], cwd=REPO, capture_output=True, check=False)  # noqa: S603, S607
    return cp.returncode == 0


def probe_need(need: str) -> str | None:
    """前提が満たされていれば None、無ければ理由。"""
    kind, _, name = need.partition(":")
    if kind == "fixture":
        fxs = ff.load_fixtures()
        fx = fxs.get(name)
        if fx is None:
            return f"{name} が bench/fixtures.toml に無い"
        d = ff.fixture_dir(fx)
        if ff.head_sha(d) != str(fx["sha"]):
            return f"{name} が未取得（uv run --no-project python bench/fetch_fixtures.py {name}）"
        return None
    if kind == "exe":
        return None if shutil.which(name) else f"{name} が PATH に無い"
    return f"未知の前提 {need}"


def missing_reason(check: dict[str, Any]) -> str | None:
    for rel in check.get("requires_files", []):
        if not (REPO / rel).exists():
            return f"未整備: {rel} がまだ無い"
    for rel in check.get("requires_tracked", []):
        if not git_tracked(rel):
            return f"未整備: {rel} は作業ツリーにあるが git の管理下に無い（未コミット）"
    return None


# --- 子プロセス -------------------------------------------------------------------------------


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)  # noqa: S603, S607
        else:
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_argv(argv: list[str], timeout_s: float, scratch: Path, tag: str) -> dict[str, Any]:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    out_path, err_path = scratch / f"{tag}.out", scratch / f"{tag}.err"
    t0 = time.perf_counter()
    timed_out = False
    meter = R.CpuLoadMeter()
    with out_path.open("wb") as fo, err_path.open("wb") as fe:
        try:
            proc = subprocess.Popen(  # noqa: S603
                argv, cwd=REPO, env=env, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                start_new_session=(sys.platform != "win32"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
            )  # fmt: skip
        except OSError as e:
            return {"rc": None, "spawn_error": str(e), "elapsed_s": 0.0, "timed_out": False, "stdout_tail": [], "stderr_tail": [],
                    "load": meter.stop()}  # fmt: skip
        try:
            rc: int | None = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                rc = None
    elapsed = time.perf_counter() - t0

    def tail(p: Path) -> list[str]:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:]

    return {"rc": rc, "elapsed_s": round(elapsed, 1), "timed_out": timed_out, "stdout_tail": tail(out_path),
            "stderr_tail": tail(err_path), "load": meter.stop()}  # fmt: skip


# --- pytest -----------------------------------------------------------------------------------


def _node_id(classname: str, name: str) -> str:
    parts = classname.split(".")
    for i, p in enumerate(parts):
        if p.startswith("test_"):
            mod = "/".join(parts[: i + 1]) + ".py"
            rest = "::".join(parts[i + 1 :])
            return mod + "::" + (rest + "::" if rest else "") + name
    return f"{classname}::{name}"


def parse_junit(path: Path) -> dict[str, Any]:
    tests = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failed_nodes: list[dict[str, str]] = []
    skip_reasons: dict[str, int] = {}
    if not path.is_file():
        return {"tests": tests, "failed_nodes": failed_nodes, "skip_reasons": skip_reasons, "junit": "missing"}
    root = ET.parse(path).getroot()
    for tc in root.iter("testcase"):
        tests["total"] += 1
        node = _node_id(tc.get("classname", ""), tc.get("name", ""))
        bad = tc.find("failure")
        err = tc.find("error")
        skp = tc.find("skipped")
        if bad is not None or err is not None:
            el = bad if bad is not None else err
            assert el is not None
            tests["failed" if bad is not None else "errors"] += 1
            msg = (el.get("message") or "").strip().splitlines()
            failed_nodes.append({"node": node, "kind": "failure" if bad is not None else "error",
                                 "message": (msg[0] if msg else "")[:240]})  # fmt: skip
        elif skp is not None:
            tests["skipped"] += 1
            reason = (skp.get("message") or "").strip().splitlines()
            key = (reason[0] if reason else "")[:160]
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
        else:
            tests["passed"] += 1
    return {"tests": tests, "failed_nodes": failed_nodes, "skip_reasons": skip_reasons}


def run_pytest(check: dict[str, Any], scratch: Path) -> dict[str, Any]:
    xml = scratch / f"{check['id']}.junit.xml"
    argv = [sys.executable, "-m", "pytest", *check["cmd"], "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"]
    res = run_argv(argv, float(check.get("timeout_s", 900)), scratch, check["id"])
    res["argv"] = argv
    res.update(parse_junit(xml))
    t = res["tests"]
    minimum = int(check.get("min_tests", 1))
    if res.get("spawn_error"):
        res.update(verdict="error", reason=f"起動できない: {res['spawn_error']}")
    elif res["timed_out"]:
        res.update(verdict="timeout", reason=f"上限 {check.get('timeout_s')} 秒を超えた（走った {t['total']} 件）")
    elif t["failed"] or t["errors"]:
        res.update(verdict="fail", reason=f"赤 {t['failed']} 件・エラー {t['errors']} 件（走った {t['total']} 件）")
    elif res["rc"] not in (0,):
        res.update(verdict="error", reason=f"pytest の終了コード {res['rc']}（収集の失敗など。走った {t['total']} 件）")
    elif t["total"] < minimum:
        res.update(verdict="fail", reason=f"走った件数 {t['total']} が下限 {minimum} 未満（ノード指定の誤りか、収集されていない）")
    elif check.get("skips") == "fail" and t["skipped"]:
        res.update(verdict="fail", reason=f"skip が {t['skipped']} 件ある（未検証。skips = fail）")
    else:
        res.update(verdict="pass", reason=f"{t['passed']} 件が通った（skip {t['skipped']} 件）")
    return res


# --- 結果 JSON の判定 -------------------------------------------------------------------------


def resolve(obj: Any, path: str) -> tuple[list[Any], bool]:
    cur = [obj]
    wild = False
    for part in path.split("."):
        nxt: list[Any] = []
        for o in cur:
            if part == "*":
                wild = True
                if isinstance(o, dict):
                    nxt.extend(o.values())
                elif isinstance(o, list):
                    nxt.extend(o)
            elif isinstance(o, dict) and part in o:
                nxt.append(o[part])
            elif isinstance(o, list) and part.isdigit() and int(part) < len(o):
                nxt.append(o[int(part)])
        cur = nxt
    return cur, wild


def eval_expect(doc: Any, expects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in expects:
        vals, _ = resolve(doc, e["path"])
        op, want = e["op"], e["value"]
        if op == "eq":
            ok = bool(vals) and all(v == want for v in vals)
        elif op == "in":
            ok = bool(vals) and all(v in want for v in vals)
        elif op == "count_ge":
            ok = len(vals) >= want
        elif op == "contains_all":
            ok = set(want) <= set(vals)
        elif op == "ge":
            ok = bool(vals) and all(isinstance(v, int | float) and v >= want for v in vals)
        elif op == "le":
            ok = bool(vals) and all(isinstance(v, int | float) and v <= want for v in vals)
        else:
            ok = False
        bad = [v for v in vals if (op in ("eq",) and v != want)][:5]
        out.append({"path": e["path"], "op": op, "value": want, "ok": ok, "n": len(vals),
                    "got": bad if bad else (vals[:5] if not ok else None)})  # fmt: skip
    return out


def newest_result(kind: str, since: float) -> Path | None:
    cands = [p for p in R.RESULTS_DIR.glob(f"*-{kind}.json") if p.stat().st_mtime >= since - 2]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def run_script(check: dict[str, Any], scratch: Path) -> dict[str, Any]:
    out_json = scratch / f"{check['id']}.out.json"
    argv = [str(a).replace("{py}", sys.executable).replace("{out}", str(out_json)) for a in check["cmd"]]
    since = time.time()
    res = run_argv(argv, float(check.get("timeout_s", 900)), scratch, check["id"])
    res["argv"] = argv
    expects = check.get("expect", [])
    doc: Any = None
    if "result_kind" in check:
        p = newest_result(check["result_kind"], since)
        res["result_file"] = str(p.relative_to(REPO)) if p else None
        if p:
            doc = json.loads(p.read_text(encoding="utf-8"))
    elif out_json.is_file():
        doc = json.loads(out_json.read_text(encoding="utf-8"))
        res["result_file"] = None
    if res.get("spawn_error"):
        res.update(verdict="error", reason=f"起動できない: {res['spawn_error']}")
        return res
    if res["timed_out"]:
        res.update(verdict="timeout", reason=f"上限 {check.get('timeout_s')} 秒を超えた")
        return res
    if expects:
        if doc is None:
            res.update(verdict="error" if res["rc"] not in (0, 1) else "fail",
                       reason=f"結果ファイルが出ていない（終了コード {res['rc']}）")  # fmt: skip
            return res
        ev = eval_expect(doc, expects)
        res["expect"] = ev
        bad = [x for x in ev if not x["ok"]]
        if bad:
            res.update(verdict="fail", reason="; ".join(f"{x['path']} {x['op']} {x['value']!r}（得た: {x['got']}）" for x in bad)[:600])
        elif res["rc"] != 0:
            res.update(verdict="fail", reason=f"結果は条件を満たすが終了コードが {res['rc']}")
        else:
            res.update(verdict="pass", reason="結果 JSON の条件を満たす")
        if isinstance(doc, dict) and "verdict" in doc:
            res["result_verdict"] = doc["verdict"]
        return res
    res.update(verdict="pass" if res["rc"] == 0 else "fail", reason=f"終了コード {res['rc']}")
    return res


# --- 組み込みの検査 ----------------------------------------------------------------------------


def builtin_ci_runs_license_check(_ctx: dict[str, Any]) -> tuple[str, str]:
    ci = REPO / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        return "missing", "未整備: .github/workflows/ci.yml が無い"
    text = ci.read_text(encoding="utf-8")
    if re.search(r"^\s*-?\s*run:.*check_licenses\.py", text, re.M):
        return "pass", "ci.yml が tools/check_licenses.py を run している"
    return "fail", "ci.yml に tools/check_licenses.py を走らせる run 行が無い"


def builtin_dist_license_files(_ctx: dict[str, Any]) -> tuple[str, str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pats = data.get("project", {}).get("license-files", [])
    want = ["LICENSE", "THIRD_PARTY_NOTICES.md"]
    absent_in_toml = [w for w in want if not any(fnmatch.fnmatch(w, p) for p in pats)]
    if absent_in_toml:
        return "fail", f"pyproject.toml の project.license-files（{pats}）に {absent_in_toml} が無く、配布物に入らない"
    gone = [w for w in want if not (REPO / w).is_file()]
    if gone:
        return "missing", f"未整備: {gone} がリポジトリに無い"
    untracked = [w for w in want if not git_tracked(w)]
    if untracked:
        return "missing", f"未整備: {untracked} は git の管理下に無い（未コミット）"
    return "pass", "license-files が両方を含み、両ファイルが管理下にある"


def builtin_cross_os(ctx: dict[str, Any]) -> tuple[str, str]:
    gate = [f"M1-{i}" for i in range(1, 7)]
    latest: dict[str, tuple[str, dict[str, str]]] = {}
    for p in sorted(R.RESULTS_DIR.glob("*-exit.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        os_ = d.get("machine", {}).get("os")
        crit = {k: v.get("verdict") for k, v in d.get("criteria", {}).items()}
        if os_ and all(g in crit for g in gate) and (os_ not in latest or d.get("finished_at", "") >= latest[os_][0]):
            latest[os_] = (d.get("finished_at", ""), crit)
    sofar = ctx.get("criteria_so_far", {})
    if all(g in sofar for g in gate):
        latest[R.os_name()] = ("9999", dict(sofar))
    need = ["windows", "macos", "linux"]
    absent = [o for o in need if o not in latest]
    if absent:
        have = ", ".join(sorted(latest)) or "なし"
        return "missing", f"未整備: {', '.join(absent)} の exit 結果が bench/results/ に無い（在るのは: {have}）。CI の OS 行列で実行器を回して集める"
    diffs = {g: {o: latest[o][1][g] for o in need} for g in gate if len({latest[o][1][g] for o in need}) > 1}
    if diffs:
        return "fail", "OS 間で判定が違う: " + json.dumps(diffs, ensure_ascii=False)
    return "pass", "3 OS で M1-1〜M1-6 の判定が同じ"


BUILTINS = {
    "ci_runs_license_check": builtin_ci_runs_license_check,
    "dist_license_files": builtin_dist_license_files,
    "cross_os": builtin_cross_os,
}


# --- 判定の集約 -------------------------------------------------------------------------------


def rollup(verdicts: list[str]) -> str:
    if not verdicts:
        return "missing"
    if any(v in FAILING for v in verdicts):
        return "fail"
    if any(v in ("missing", "blocked") for v in verdicts):
        return "missing"
    if any(v == "skipped" for v in verdicts):
        return "not_run"
    return "pass"


def select(checks: list[dict[str, Any]], only: list[str]) -> list[dict[str, Any]]:
    if not only:
        return checks
    return [c for c in checks if any(c["id"] == o or c["id"].startswith(o) or c["criterion"] == o for o in only)]


def run_check(check: dict[str, Any], args: argparse.Namespace, scratch: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = {k: check[k] for k in ("id", "criterion", "spec", "title", "kind", "pass") if k in check}
    base["gating"] = check.get("gating", True)
    base["slow"] = check.get("slow", False)
    base["est_min"] = check.get("est_min")
    base["quiet"] = check.get("quiet", False)
    base["needs"] = check.get("needs", [])
    if check.get("lsp"):
        base["lsp"] = check["lsp"]
    if check.get("slow") and args.skip_slow:
        return {**base, "verdict": "skipped", "reason": "--skip-slow（slow = true）"}
    why = missing_reason(check)
    if why:
        return {**base, "verdict": "missing", "reason": why}
    unmet = [r for n in check.get("needs", []) if (r := probe_need(n))]
    if unmet:
        return {**base, "verdict": "blocked", "reason": "前提が満たせない: " + "; ".join(unmet)}
    kind = check["kind"]
    if kind == "pytest":
        res = run_pytest(check, scratch)
    elif kind == "script":
        res = run_script(check, scratch)
    elif kind == "builtin":
        v, why2 = BUILTINS[check["builtin"]](ctx)
        res = {"verdict": v, "reason": why2}
    else:
        res = {"verdict": "error", "reason": f"未知の kind {kind}"}
    out = {**base, **res}
    if check.get("quiet") and (out.get("load") or {}).get("cpu_busy_pct") not in (None,) and out["load"]["cpu_busy_pct"] > 25:
        out["warning"] = f"静かな状態が前提の検査だが、実行中のシステム CPU 使用率が {out['load']['cpu_busy_pct']}%（他の負荷あり）"
    return out


# --- 要約の Markdown ----------------------------------------------------------------------------


def md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render_markdown(doc: dict[str, Any], stamp: str, json_rel: str) -> str:
    crit = doc["criteria"]
    checks = doc["checks"]
    overall = doc["overall"]
    lines: list[str] = []
    lines.append(f"# M1 出口条件の検査結果 {stamp}")
    lines.append("")
    reds = [k for k, v in crit.items() if v["verdict"] == "fail"]
    mis = [k for k, v in crit.items() if v["verdict"] in ("missing", "not_run")]
    oks = [k for k, v in crit.items() if v["verdict"] == "pass"]
    lines.append(f"**判定: {WORDS.get(overall, overall)}**" + ("" if overall == "pass" else "（M1 はまだ閉じられない）"))
    lines.append("")
    lines.append(f"- 不合格: {', '.join(reds) or 'なし'}")
    lines.append(f"- 未整備・未実行: {', '.join(mis) or 'なし'}")
    lines.append(f"- 合格: {', '.join(oks) or 'なし'}")
    rs, rf = doc["repo_start"], doc["lucianke"]
    lines.append(f"- 実行時点のコミット: `{(rs.get('commit') or '?')[:10]}`"
                 f"（作業ツリー: {'dirty ' + str(rs.get('dirty_paths')) + ' 件。他の役が並行編集中のため、赤は書きかけの影響を含みうる' if rs.get('dirty') else 'clean'}）"
                 f"、終了時点 `{(rf.get('commit') or '?')[:10]}`")  # fmt: skip
    m = doc["machine"]
    lines.append(f"- 機材: {m.get('os_detail')} / {m.get('cpu')} / {m.get('cpu_logical')} 論理 CPU / {m.get('memory_gb')} GB / {m.get('storage')}")
    a = doc["args"]
    lines.append(f"- 実行: {doc['started_at']}、所要 {doc['elapsed_s']} 秒、`--skip-slow` {a['skip_slow']}、`--only` {a['only'] or '(全部)'}")
    lines.append(f"- 生データ: `{json_rel}`")
    lines.append("")
    lines.append("## 出口条件ごとの現在地")
    lines.append("")
    lines.append("| # | 条件 | 判定 | 内訳 |")
    lines.append("|---|---|---|---|")
    for cid, text in CRITERIA.items():
        c = crit.get(cid, {"verdict": "missing", "checks": {}})
        parts = ", ".join(f"{k.split('.', 1)[1]}={WORDS.get(v, v)}" for k, v in c.get("checks", {}).items())
        lines.append(f"| {cid} | {md_escape(text)} | **{WORDS.get(c['verdict'], c['verdict'])}** | {md_escape(parts)} |")
    lines.append("")

    bad = [c for c in checks if c["gating"] and c["verdict"] in FAILING]
    gone = [c for c in checks if c["gating"] and c["verdict"] in ("missing", "blocked")]
    lines.append("## 不合格の項目")
    lines.append("")
    if not bad:
        lines.append("なし。")
    for c in bad:
        lines.append(f"### {c['id']}: {c['title']}")
        lines.append("")
        lines.append(f"- 判定: {WORDS.get(c['verdict'])}（{c.get('reason', '')}）")
        lines.append(f"- 条件: {c['pass']}")
        if c.get("argv"):
            lines.append(f"- 再現: `{' '.join(c['argv'][1:] if c['argv'][0] == sys.executable else c['argv'])}`")
        t = c.get("tests")
        if t:
            lines.append(f"- 件数: 走った {t['total']}・通った {t['passed']}・赤 {t['failed']}・エラー {t['errors']}・skip {t['skipped']}")
        for fn in c.get("failed_nodes", [])[:30]:
            lines.append(f"  - `{fn['node']}` {md_escape(fn.get('message', ''))}")
        if len(c.get("failed_nodes", [])) > 30:
            lines.append(f"  - ほか {len(c['failed_nodes']) - 30} 件（生データ参照）")
        for x in c.get("expect", []):
            if not x["ok"]:
                lines.append(f"  - 条件 `{x['path']} {x['op']} {x['value']!r}` を満たさない（得た: {x['got']}）")
        if c.get("result_file"):
            lines.append(f"- 結果ファイル: `{c['result_file']}`")
        if not c.get("failed_nodes") and c.get("stderr_tail"):
            lines.append("- 標準エラーの末尾:")
            lines.append("")
            lines.append("  ```")
            lines.extend("  " + ln for ln in c["stderr_tail"][-12:])
            lines.append("  ```")
        load = c.get("load") or {}
        if load.get("cpu_busy_pct") is not None:
            lines.append(f"- 実行中のシステム CPU 使用率: {load['cpu_busy_pct']}%")
        lines.append("")
    lines.append("## 未整備・前提不足の項目")
    lines.append("")
    if not gone:
        lines.append("なし。")
    for c in gone:
        lines.append(f"- **{c['id']}** ({WORDS.get(c['verdict'])}): {c['title']} — {c.get('reason', '')}")
    lines.append("")
    skipped = [c for c in checks if c["verdict"] == "skipped"]
    if skipped:
        lines.append("## 今回は走らせていない項目")
        lines.append("")
        for c in skipped:
            lines.append(f"- {c['id']}（目安 {c.get('est_min')} 分）: {c['title']}")
        lines.append("")
    lines.append("## すべての検査")
    lines.append("")
    lines.append("| id | 判定 | 件数・所要 | 理由 |")
    lines.append("|---|---|---|---|")
    for c in checks:
        t = c.get("tests")
        cnt = f"{t['passed']}/{t['total']} (skip {t['skipped']})" if t else ""
        el = f"{c['elapsed_s']} s" if c.get("elapsed_s") is not None else ""
        ref = "" if c["gating"] else "（参考）"
        lines.append(f"| {c['id']}{ref} | {WORDS.get(c['verdict'], c['verdict'])} | {md_escape((cnt + ' ' + el).strip())} | {md_escape(c.get('reason', ''))[:200]} |")
    skips = [(c["id"], c["skip_reasons"]) for c in checks if c.get("skip_reasons")]
    if skips:
        lines.append("")
        lines.append("## skip された試験の理由（未検証の範囲）")
        lines.append("")
        for cid, reasons in skips:
            for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
                lines.append(f"- {cid}: {n} 件 — {r or '(理由なし)'}")
    warns = [c for c in checks if c.get("warning")]
    if warns:
        lines.append("")
        lines.append("## 注意")
        lines.append("")
        for c in warns:
            lines.append(f"- {c['id']}: {c['warning']}")
    lines.append("")
    return "\n".join(lines)


# --- 入口 --------------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="", help="カンマ区切り: 出口条件（M1-5）・検査の id・id の接頭辞")
    ap.add_argument("--skip-slow", action="store_true", help="slow = true の検査を飛ばす")
    ap.add_argument("--list", action="store_true", help="走らせずに計画（検査・状態）だけ表示する")
    ap.add_argument("--no-report", action="store_true", help="結果ファイルと要約を書かない")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    spec = tomllib.loads(TOML.read_text(encoding="utf-8"))
    only = [o.strip() for o in args.only.split(",") if o.strip()]
    checks = select(spec["check"], only)
    if not checks:
        print(f"検査が見つからない: --only {args.only}", file=sys.stderr)
        return 2
    if args.list:
        for c in checks:
            why = missing_reason(c) or ""
            print(f"{c['id']:42s} {c['kind']:8s} slow={str(c.get('slow', False)):5s} est={c.get('est_min')}min {why}")
        return 0

    stamp = R.now_stamp()
    started = R.iso_now()
    repo_start = R.repo_info()
    t0 = time.perf_counter()
    scratch = Path(tempfile.mkdtemp(prefix="lucianke-exit-"))
    results: list[dict[str, Any]] = []
    ctx: dict[str, Any] = {"criteria_so_far": {}}
    try:
        for c in checks:
            print(f"== {c['id']} ({c['kind']}) {c['title']}", flush=True)
            r = run_check(c, args, scratch, ctx)
            results.append(r)
            print(f"   -> {WORDS.get(r['verdict'], r['verdict'])}: {r.get('reason', '')}", flush=True)
            by: dict[str, list[str]] = {}
            for x in results:
                if x["gating"]:
                    by.setdefault(x["criterion"], []).append(x["verdict"])
            ctx["criteria_so_far"] = {k: rollup(v) for k, v in by.items()}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    crit: dict[str, dict[str, Any]] = {}
    for cid in CRITERIA:
        cs = [x for x in results if x["criterion"] == cid and x["gating"]]
        crit[cid] = {"verdict": rollup([x["verdict"] for x in cs]) if (cs or not only) else "not_run",
                     "checks": {x["id"]: x["verdict"] for x in cs}}  # fmt: skip
    if only:
        for cid in CRITERIA:
            if not any(x["criterion"] == cid for x in results):
                crit[cid]["verdict"] = "not_run"
    overall = "pass" if all(v["verdict"] == "pass" for v in crit.values()) else ("fail" if any(v["verdict"] == "fail" for v in crit.values()) else "incomplete")
    payload: dict[str, Any] = {
        "schema": "lucianke-bench-exit/1",
        "spec": spec["meta"]["spec"],
        "args": {"only": only, "skip_slow": args.skip_slow},
        "started_at": started,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "repo_start": repo_start,
        "overall": overall,
        "criteria": crit,
        "checks": results,
    }
    for line in (f"{cid}: {WORDS.get(v['verdict'], v['verdict'])}" for cid, v in crit.items()):
        print(line)
    print(f"overall: {WORDS.get(overall, overall)}")
    if args.no_report:
        return 0 if overall == "pass" else 1
    path = R.write_result("exit", payload, stamp=stamp)
    doc = json.loads(path.read_text(encoding="utf-8"))
    ACCEPTANCE.mkdir(parents=True, exist_ok=True)
    md_path = ACCEPTANCE / f"M1-exit-{stamp}.md"
    md_path.write_text(render_markdown(doc, stamp, path.relative_to(REPO).as_posix()), encoding="utf-8")
    print(f"result: {path}\nsummary: {md_path}")
    return 0 if overall == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
