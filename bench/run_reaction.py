"""外部変更の反映時間の計測台本（仕様書 §14.6、NFR-PERF-3）。

別プロセスがワークスペース内の Tier 1 ファイルを 1 つ書き換え、書き込み完了の時刻を記録する。Lucianke 側では
``query_project_db``（T-20）で ``files.sha256`` を読み続け、新しい内容の SHA-256 になった時刻を記録する。これを
100 回繰り返し、p95 が 500 ms 以内かを判定する。

* 共有の作業ツリーを書き換えないよう、フィクスチャは一時ディレクトリに **コピー** してから動かす。
* 時刻は両プロセスとも ``time.time_ns()``（壁時計。同一機材なので比較できる）。観測側は応答を受け取った時刻なので、
  反映時間は **最大でポーリング 1 回分（往復時間）の過大評価** になる（往復の中央値を結果に併記する）。
* 索引が動いていない（``query_project_db`` が無い・DB 未結線・ファイルが索引に現れない）ときは ``skip`` として記録し、落ちない。
* LSP への ``didChange`` / ``didChangeWatchedFiles`` の送信時刻（§14.6 の 2）は、サーバーのログにイベントが出るようになってから
  取る。いまは ``stderr`` のログに該当行があるかを数えるだけ。

使い方::

    uv run python bench/run_reaction.py                       # F-POLY のコピーで 100 回
    uv run python bench/run_reaction.py --fixture F-PY --iterations 100

結果: ``bench/results/<日時>-<OS>-reaction.json``。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
import _results as R  # noqa: E402
import _stdio as S  # noqa: E402
import run_latency as L  # noqa: E402

REPO = R.REPO
THRESHOLD_MS = 500.0
DEFAULT_ITERATIONS = 100

# 別プロセスの書き手。標準入力の JSON 行 {"path", "text_hex"} を受けて書き、書き終えた時刻（壁時計 ns）を返す。
_WRITER = r"""
import json, os, sys, time
for line in sys.stdin:
    req = json.loads(line)
    data = bytes.fromhex(req["data_hex"])
    path = req["path"]
    if req.get("atomic"):
        tmp = path + ".tmp-bench"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    else:
        with open(path, "wb") as f:
            f.write(data)
    sys.stdout.write(json.dumps({"t_ns": time.time_ns()}) + "\n")
    sys.stdout.flush()
"""

_SQL = "SELECT sha256, indexed_at FROM files WHERE path = :p"


def copy_workspace(src: Path, dst: Path) -> None:
    """フィクスチャを ``dst`` にコピーする（``.git``・``node_modules`` などは除く）。"""
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*sorted(R.SKIP_DIRS), "*.pyc"))


def pick_target(ws: Path, seed: str) -> str:
    """書き換える Tier 1 ファイル（``.py`` を優先し、50〜400 行のもの。シード固定）。"""
    files = R.tier1_files(ws)
    cands: list[str] = []
    for rel in files:
        if not rel.endswith((".py", ".ts")):
            continue
        try:
            n = len((ws / rel).read_bytes().splitlines())
        except OSError:
            continue
        if 50 <= n <= 400:
            cands.append(rel)
    pool = cands or files
    if not pool:
        raise SystemExit("no Tier 1 file in the fixture")
    return random.Random(seed).choice(sorted(pool))


async def read_sha(client: S.McpStdioClient, rel: str) -> tuple[dict[str, Any], str | None, str | None]:
    """``files.sha256`` と ``indexed_at`` を読む。``(記録, sha, indexed_at)``。"""
    rec = await L.call_once(client, "query_project_db", {"query": _SQL, "params": {"p": rel}}, rel, want_rows=True)
    sha = at = None
    rows = rec.pop("rows", None)
    if rows:
        row = rows[0]
        if isinstance(row, dict):
            sha, at = row.get("sha256"), row.get("indexed_at")
        else:
            sha, at = row[0], row[1] if len(row) > 1 else None
    return rec, sha, at


async def run(args: argparse.Namespace) -> dict[str, Any]:
    specs, skipped = L.resolve_fixtures([args.fixture])
    if not specs:
        return {"verdict": "skip", "reason": f"fixture unavailable: {skipped}", "iterations": 0}
    spec = specs[0]
    tmp = Path(tempfile.mkdtemp(prefix="lucianke-bench-react-"))
    ws = tmp / "ws"
    out: dict[str, Any] = {"fixture": R.fixture_info(spec.name, spec.root, sha=spec.sha), "mode": args.mode}
    try:
        copy_workspace(spec.root, ws)
        rel = pick_target(ws, f"{args.seed}:{spec.name}")
        out["target_file"] = rel
        argv = S.server_argv() if not args.server_argv else args.server_argv.split()
        client = S.McpStdioClient(argv, cwd=ws, env=S.server_env(tmp / "cache"), roots=[ws],
                                  stderr_path=tmp / "server.stderr.log")  # fmt: skip
        meter = R.CpuLoadMeter()
        t_spawn = time.perf_counter()
        writer: asyncio.subprocess.Process | None = None
        try:
            await client.start()
            names = {t["name"] for t in await client.list_tools()}
            out["server"] = client.server_info
            if "query_project_db" not in names:
                out.update(verdict="skip", reason="query_project_db (T-20) is not in tools/list", iterations=0)
                return out
            # 索引にファイルが現れるまで待つ（ウォームアップ。所要は報告値）
            deadline = time.perf_counter() + args.warmup_timeout
            sha0: str | None = None
            while True:
                rec, sha0, _ = await read_sha(client, rel)
                if sha0:
                    break
                if rec["error"] == "E_WORKSPACE_NOT_CONFIGURED" and rec.get("error_reason") != "index_missing":
                    out.update(verdict="skip", reason="index database is not wired into the server (E_WORKSPACE_NOT_CONFIGURED)",
                               iterations=0, probe=rec)  # fmt: skip
                    return out
                if time.perf_counter() > deadline:
                    out.update(verdict="skip", reason=f"{rel} never appeared in files within {args.warmup_timeout:.0f}s "
                                                      f"(last: {rec['error'] or 'no row'})", iterations=0, probe=rec)  # fmt: skip
                    return out
                await asyncio.sleep(0.5)
            out["warmup_ready_ms"] = round((time.perf_counter() - t_spawn) * 1000, 1)
            await asyncio.sleep(1.0)  # 起動直後の索引の余波が収まるのを待つ（反映時間の測定に混ぜない）

            writer = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", _WRITER, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            )  # fmt: skip
            assert writer.stdin is not None and writer.stdout is not None
            target = ws / rel
            base = target.read_bytes()
            comment = "//" if rel.endswith((".ts", ".tsx", ".js")) else "#"
            nonce = hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:8]
            samples: list[dict[str, Any]] = []
            poll_rtts: list[float] = []
            for i in range(args.iterations):
                data = base + f"\n{comment} bench-reaction {nonce} {i}\n".encode()
                want = hashlib.sha256(data).hexdigest()
                req = {"path": str(target), "data_hex": data.hex(), "atomic": args.mode == "atomic"}
                writer.stdin.write((json.dumps(req) + "\n").encode())
                await writer.stdin.drain()
                t_write_ns = json.loads(await writer.stdout.readline())["t_ns"]
                polls = 0
                seen_at: str | None = None
                observed_ms: float | None = None
                while True:
                    rec, sha, seen_at = await read_sha(client, rel)
                    t_obs_ns = time.time_ns()
                    polls += 1
                    if rec["rtt_ms"] is not None:
                        poll_rtts.append(rec["rtt_ms"])
                    elapsed_ms = (t_obs_ns - t_write_ns) / 1e6
                    if sha == want:
                        observed_ms = elapsed_ms
                        break
                    if elapsed_ms > args.timeout_ms:
                        break
                    await asyncio.sleep(args.poll_ms / 1000)  # サーバーを busy にして索引の更新を邪魔しない
                samples.append({"i": i, "reaction_ms": round(observed_ms, 2) if observed_ms is not None else None,
                                "timeout": observed_ms is None, "polls": polls, "indexed_at": seen_at})  # fmt: skip
                await asyncio.sleep(args.gap_ms / 1000)
            vals = [s["reaction_ms"] if s["reaction_ms"] is not None else float(args.timeout_ms) for s in samples]
            summ = R.summarize(vals)
            timeouts = sum(1 for s in samples if s["timeout"])
            p95 = summ.get("p95")
            ok = bool(samples) and timeouts == 0 and p95 is not None and p95 <= THRESHOLD_MS
            out.update(
                iterations=len(samples), timeouts=timeouts, reaction_ms=summ,
                poll_rtt_ms=R.summarize(poll_rtts), threshold_ms=THRESHOLD_MS,
                verdict="pass" if ok else "fail",
                reason=f"p95 {p95} ms {'<=' if ok else '>'} {THRESHOLD_MS:.0f} ms (timeouts={timeouts})",
                raw=samples,
                lsp_sync={"measured": False, "reason": "no didChange / didChangeWatchedFiles log event is defined yet (§14.6 item 2)"},
            )  # fmt: skip
        finally:
            if writer is not None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    writer.kill()
            shutdown = await client.close()
            client.abort()
            out["load"] = meter.stop()
            out["watch"] = client.watch_report()
            out["shutdown"] = shutdown
            with contextlib.suppress(OSError):
                log = (tmp / "server.stderr.log").read_text(encoding="utf-8", errors="replace").splitlines()
                out["server_log_didchange_lines"] = sum(1 for ln in log if "didChange" in ln or "did_change" in ln)
        if out.get("watch") and not out["watch"]["ok"]:
            out["verdict"], out["reason"] = "fail", "stdout purity or child-process check failed (§14.2)"
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixture", default="poly", help="poly | self は不可（書き換えるため）| F-PY | F-TS")
    ap.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    ap.add_argument("--gap-ms", type=float, default=300.0, help="観測後、次の書き換えまで空ける時間")
    ap.add_argument("--poll-ms", type=float, default=10.0, help="ポーリングの間隔（過大評価は最大でこの値＋往復）")
    ap.add_argument("--timeout-ms", type=float, default=5000.0, help="1 回の観測の待ち上限（超えたら timeout）")
    ap.add_argument("--warmup-timeout", type=float, default=120.0)
    ap.add_argument("--mode", choices=("inplace", "atomic"), default="inplace", help="書き換え方: 既存ファイルへ上書き / 一時ファイル + rename")
    ap.add_argument("--seed", type=int, default=L.DEFAULT_SEED)
    ap.add_argument("--note", default="")
    ap.add_argument("--server-argv", default="", help="（テスト用）サーバーの起動コマンドを差し替える")
    args = ap.parse_args()
    if args.fixture.lower() in ("self", "lucianke"):
        ap.error("the reaction script rewrites a file; run it on a fixture copy (poly, F-PY, F-TS)")
    t0 = time.perf_counter()
    body = asyncio.run(run(args))
    payload = {
        "spec": {"section": "14.6", "nfr": "NFR-PERF-3", "threshold_ms": THRESHOLD_MS,
                 "clock": "time.time_ns() in both processes", "observation": "files.sha256 via query_project_db polling (sequential, poll_ms sleep between polls)"},  # fmt: skip
        "note": args.note,
        "verdict": body.get("verdict"),
        "elapsed_s": round(time.perf_counter() - t0, 1),
        **{k: v for k, v in body.items() if k != "verdict"},
    }
    path = R.write_result("reaction", payload)
    print(f"verdict: {payload['verdict']} ({body.get('reason')})\nresult: {path}")
    return 1 if payload["verdict"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
