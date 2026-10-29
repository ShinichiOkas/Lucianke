"""M1-7（仕様書 §15.2「Windows / macOS / Linux で M1-1〜M1-6 が同じ結果になる」）を機械で判定する比較台本。

3 OS それぞれで ``bench/run_exit_criteria.py`` が書いた ``*-exit.json`` を読み、出口条件 M1-1〜M1-6 の判定が
3 OS で一致するかを調べる。標準ライブラリだけで動く（CI の比較ジョブも、手元でダウンロードした 3 つの JSON も同じ台本）。

使い方::

    python bench/compare_cross_os.py DIR_OR_FILE [DIR_OR_FILE ...]       # 例: CI の成果物を落とした作業用フォルダ
    python bench/compare_cross_os.py                                     # 引数なし = bench/results/
    python bench/compare_cross_os.py a.json b.json c.json --json out.json
    python bench/compare_cross_os.py ... --strict                        # 検査単位の判定の違いも不一致にする

引数がフォルダなら、その下の ``*-exit.json`` を再帰で集める。OS ごとに最新（``finished_at``）の 1 つを使い、
``--only`` の部分実行は数えない（実行器の組み込み検査 ``M1-7.cross-os`` と同じ規則）。

判定:

* 合格の条件は「windows・macos・linux の結果が揃い、M1-1〜M1-6 の各判定（合格・不合格・未実行…の語）が 3 OS で同じ」こと。
  M1-7 は「同じ結果になる」ことの判定であり、各条件が合格かどうかは別（赤でも 3 OS で同じ赤なら一致）。
* 検査（M1-1.e2e-stdio など）ごとの判定の違いは、既定では注意として出す（出口条件の判定が同じでも、中身が OS で割れている印）。
  ``--strict`` ならそれも不一致にする。
* 同じ不合格でも赤いテストが OS で違うとき、実行時のコミットが違うとき、実行条件（``--skip-slow``）が違うときは注意に出す。

終了コード: 0 = 一致、1 = 不一致、2 = 欠け（3 OS の結果が揃わない・読めない）。
出力は Markdown（CI では ``$GITHUB_STEP_SUMMARY`` に追記できる）。``--json`` で機械向けの結果も書く。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_SCHEMA_PREFIX = "lucianke-bench-exit/"
OUT_SCHEMA = "lucianke-bench-cross-os/1"
OSES = ("windows", "macos", "linux")
GATE = tuple(f"M1-{i}" for i in range(1, 7))
ABSENT = "(無)"
WORDS = {
    "pass": "合格", "fail": "不合格", "timeout": "不合格(時間超過)", "error": "不合格(実行できず)",
    "missing": "未整備", "blocked": "前提不足", "skipped": "未実行", "not_run": "未実行", ABSENT: ABSENT,
}  # fmt: skip
VERDICT_WORDS = {"agree": "一致", "differ": "不一致", "incomplete": "欠け"}
EXIT_CODES = {"agree": 0, "differ": 1, "incomplete": 2}
NODE_LIST_LIMIT = 8


@dataclass(frozen=True)
class Loaded:
    """1 つの exit 結果。``candidates`` はその OS で候補になった結果ファイルの数（最新の 1 つを使う）。"""

    path: Path
    doc: dict[str, Any]
    candidates: int = 1


def _when(doc: dict[str, Any]) -> dt.datetime:
    raw = doc.get("finished_at") or doc.get("started_at") or ""
    try:
        t = dt.datetime.fromisoformat(raw)
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def collect_files(paths: Iterable[Path]) -> list[Path]:
    """ファイルはそのまま、フォルダはその下の ``*-exit.json``（再帰）。重複は除く。"""
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        found = sorted(p.rglob("*-exit.json")) if p.is_dir() else [p]
        for f in found:
            key = f.resolve()
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out


def load_results(files: Iterable[Path]) -> tuple[dict[str, Loaded], list[str]]:
    """OS 名 -> 使う結果。読めない・形式が違う・部分実行のものは数えず、理由を ``notes`` に返す。"""
    by_os: dict[str, list[Loaded]] = {}
    notes: list[str] = []
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            notes.append(f"{f}: 読めない（{e}）")
            continue
        if not isinstance(doc, dict) or not str(doc.get("schema", "")).startswith(EXIT_SCHEMA_PREFIX):
            notes.append(
                f"{f}: exit 結果の形式ではない（schema = {doc.get('schema') if isinstance(doc, dict) else None!r}）"
            )
            continue
        if doc.get("args", {}).get("only"):
            notes.append(f"{f}: --only の部分実行なので数えない")
            continue
        os_ = doc.get("machine", {}).get("os")
        if not os_:
            notes.append(f"{f}: machine.os が無い")
            continue
        by_os.setdefault(str(os_), []).append(Loaded(f, doc))
    chosen: dict[str, Loaded] = {}
    for os_, items in by_os.items():
        best = max(items, key=lambda it: _when(it.doc))
        chosen[os_] = Loaded(best.path, best.doc, len(items))
    return chosen, notes


def _commit(doc: dict[str, Any]) -> str:
    return str(doc.get("repo_start", {}).get("commit") or doc.get("lucianke", {}).get("commit") or "?")


def _gate_checks(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        c["id"]: c
        for c in doc.get("checks", [])
        if c.get("criterion") in GATE and c.get("gating", True) and "id" in c
    }


def compare(
    results: dict[str, Loaded],
    *,
    required: Sequence[str] = OSES,
    strict: bool = False,
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """読み込んだ結果を比べる（入出力を持たない。単体テストはここを直接呼ぶ）。"""
    present = [o for o in required if o in results]
    absent = [o for o in required if o not in results]
    extra = sorted(o for o in results if o not in required)
    warnings: list[str] = list(notes)
    if extra:
        warnings.append(f"比べる対象（{', '.join(required)}）の外の OS の結果は数えない: {', '.join(extra)}")
    mismatches: list[dict[str, Any]] = []

    # 出口条件 M1-1〜M1-6 の判定（ゲート）
    criteria: dict[str, dict[str, str]] = {}
    for cid in GATE:
        row = {
            o: str(results[o].doc.get("criteria", {}).get(cid, {}).get("verdict", ABSENT)) for o in present
        }
        criteria[cid] = row
        if len(set(row.values())) > 1:
            mismatches.append({"scope": "criterion", "id": cid, "verdicts": row,
                               "detail": f"{cid} の判定が OS で違う"})  # fmt: skip

    # 検査ごとの判定（M1-1〜M1-6 に属する、出口条件に数える検査）
    per_os_checks = {o: _gate_checks(results[o].doc) for o in present}
    ids: list[str] = []
    for cid in GATE:
        for o in present:
            ids.extend(i for i, c in per_os_checks[o].items() if c.get("criterion") == cid and i not in ids)
    checks: dict[str, dict[str, str]] = {}
    check_diffs: list[dict[str, Any]] = []
    for i in ids:
        row = {
            o: str(per_os_checks[o][i].get("verdict", ABSENT)) if i in per_os_checks[o] else ABSENT
            for o in present
        }
        checks[i] = row
        if len(set(row.values())) > 1:
            check_diffs.append(
                {"scope": "check", "id": i, "verdicts": row, "detail": f"検査 {i} の判定が OS で違う"}
            )
    if strict:
        mismatches.extend(check_diffs)
    else:
        warnings.extend(
            f"検査 {d['id']} の判定が OS で違う（出口条件 {d['id'].split('.', 1)[0]} の判定は同じなので一致扱い。"
            "厳しく見るなら --strict）: "
            + ", ".join(f"{o}={WORDS.get(v, v)}" for o, v in d["verdicts"].items())
            for d in check_diffs
            if _crit_same(criteria, d["id"])
        )

    # 同じ不合格でも、赤いテストが OS で違う
    for i in ids:
        if not checks[i] or any(v not in ("fail",) for v in checks[i].values()):
            continue
        nodes = {
            o: {fn.get("node", "") for fn in per_os_checks[o][i].get("failed_nodes", [])} for o in present
        }
        union = set().union(*nodes.values()) if nodes else set()
        diff = sorted(n for n in union if not all(n in s for s in nodes.values()))
        if diff:
            shown = ", ".join(diff[:NODE_LIST_LIMIT]) + (
                f" ほか {len(diff) - NODE_LIST_LIMIT} 件" if len(diff) > NODE_LIST_LIMIT else ""
            )
            warnings.append(
                f"検査 {i} は全 OS で不合格だが、赤いテストが OS で違う（一部の OS だけ: {shown}）"
            )

    # 実行条件・コミット
    skip = {o: bool(results[o].doc.get("args", {}).get("skip_slow")) for o in present}
    if len(set(skip.values())) > 1:
        warnings.append("実行条件が違う（--skip-slow）: " + ", ".join(f"{o}={v}" for o, v in skip.items()))
    commits = {o: _commit(results[o].doc) for o in present}
    if len(set(commits.values())) > 1:
        warnings.append(
            "実行したコミットが OS で違う: " + ", ".join(f"{o}={c[:10]}" for o, c in commits.items())
        )
    for o in present:
        if results[o].candidates > 1:
            warnings.append(
                f"{o}: 候補が {results[o].candidates} 件あり、最新（{results[o].path.name}）を使った"
            )

    # 参考: 件数・skip の違い（判定には使わない）
    counts: dict[str, dict[str, dict[str, int]]] = {}
    for i in ids:
        row = {}
        for o in present:
            t = per_os_checks[o].get(i, {}).get("tests")
            if t:
                row[o] = {k: int(t.get(k, 0)) for k in ("total", "passed", "skipped")}
        if len(row) == len(present) and len({tuple(v.items()) for v in row.values()}) > 1:
            counts[i] = row

    verdict = "differ" if mismatches else ("incomplete" if absent else "agree")
    return {
        "schema": OUT_SCHEMA,
        "verdict": verdict,
        "required": list(required),
        "absent": absent,
        "strict": strict,
        "present": {
            o: {
                "file": results[o].path.as_posix(),
                "commit": commits[o],
                "finished_at": results[o].doc.get("finished_at"),
                "skip_slow": skip[o],
                "overall": results[o].doc.get("overall"),
                "machine": results[o].doc.get("machine", {}).get("os_detail"),
            }
            for o in present
        },
        "criteria": criteria,
        "checks": checks,
        "mismatches": mismatches,
        "warnings": warnings,
        "counts": counts,
    }


def _crit_same(criteria: dict[str, dict[str, str]], check_id: str) -> bool:
    return len(set(criteria.get(check_id.split(".", 1)[0], {}).values())) <= 1


def _md(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render_markdown(rep: dict[str, Any]) -> str:
    oses = list(rep["present"])
    head = "| " + " | ".join(["", *oses, "一致"]) + " |"
    sep = "|" + "---|" * (len(oses) + 2)
    lines = [f"# M1-7 3 OS の比較: **{VERDICT_WORDS[rep['verdict']]}**", ""]
    if rep["verdict"] == "agree":
        lines.append(f"{' / '.join(rep['required'])} の結果が揃い、M1-1〜M1-6 の判定が 3 OS で同じ。")
    if rep["absent"]:
        lines.append(
            f"- 欠け: {', '.join(rep['absent'])} の exit 結果が無い（成果物が上がっていない・読めない）"
        )
    for m in rep["mismatches"]:
        lines.append(
            f"- 不一致: {m['detail']}（"
            + ", ".join(f"{o}={WORDS.get(v, v)}" for o, v in m["verdicts"].items())
            + "）"
        )
    lines += [
        "",
        "## 結果ファイル",
        "",
        "| OS | コミット | 完了 | --skip-slow | 全体 | 機材 | ファイル |",
        "|---|---|---|---|---|---|---|",
    ]
    for o, p in rep["present"].items():
        lines.append(
            f"| {o} | `{p['commit'][:10]}` | {p['finished_at']} | {p['skip_slow']} | {p['overall']} | {_md(str(p['machine']))} | `{_md(Path(p['file']).name)}` |"
        )

    def table(title: str, rows: dict[str, dict[str, str]]) -> None:
        lines.extend(["", f"## {title}", "", head, sep])
        for key, row in rows.items():
            same = len(set(row.values())) <= 1
            cells = " | ".join(WORDS.get(row.get(o, ABSENT), row.get(o, ABSENT)) for o in oses)
            lines.append(f"| {key} | {cells} | {'=' if same else '**違う**'} |")

    if oses:
        table("出口条件ごとの判定（M1-7 の合否はここ）", rep["criteria"])
        table("検査ごとの判定（M1-1〜M1-6）", rep["checks"])
    if rep["warnings"]:
        lines += ["", "## 注意", ""]
        lines.extend(f"- {_md(w)}" for w in rep["warnings"])
    if rep["counts"]:
        lines += [
            "",
            "## 参考: 件数・skip が OS で違う検査（判定には使わない）",
            "",
            "| 検査 | " + " | ".join(oses) + " |",
            "|---|" + "---|" * len(oses),
        ]
        for i, row in rep["counts"].items():
            cells = " | ".join(
                f"{row[o]['passed']}/{row[o]['total']} (skip {row[o]['skipped']})" if o in row else "-"
                for o in oses
            )
            lines.append(f"| {i} | {cells} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "paths", nargs="*", type=Path, help="*-exit.json かそれを含むフォルダ（既定: bench/results/）"
    )
    ap.add_argument("--json", type=Path, help="比較の結果（機械向け）をこのパスに書く")
    ap.add_argument("--strict", action="store_true", help="検査単位の判定の違いも不一致にする")
    ap.add_argument(
        "--require",
        default=",".join(OSES),
        help="揃っているべき OS（カンマ区切り。既定 windows,macos,linux）",
    )
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    required = tuple(o.strip() for o in args.require.split(",") if o.strip())
    files = collect_files(args.paths or [here / "results"])
    results, notes = load_results(files)
    rep = compare(results, required=required, strict=args.strict, notes=notes)
    print(render_markdown(rep))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return EXIT_CODES[rep["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
