"""bench/compare_cross_os.py（M1-7: 3 OS で M1-1〜M1-6 が同じ結果になる）の判定を、合成した exit 結果で確かめる。"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("compare_cross_os", REPO / "bench" / "compare_cross_os.py")
assert _spec and _spec.loader
cx = importlib.util.module_from_spec(_spec)
sys.modules["compare_cross_os"] = cx
_spec.loader.exec_module(cx)

# (検査 id, 出口条件, 判定)。出口条件の判定は検査の rollup（実行器と同じ規則: 不合格 > 未整備 > 未実行 > 合格）
CHECKS = [
    ("M1-1.contract-fpoly", "M1-1", "pass"),
    ("M1-1.postcondition-map", "M1-1", "fail"),
    ("M1-2.ep-transaction", "M1-2", "pass"),
    ("M1-3.latency", "M1-3", "skipped"),
    ("M1-4.tokens", "M1-4", "skipped"),
    ("M1-5.safe2-db-readonly", "M1-5", "pass"),
    ("M1-6.e2e-session", "M1-6", "pass"),
    ("M1-7.cross-os", "M1-7", "missing"),
    ("M1-8.license-check", "M1-8", "pass"),
]


def _rollup(verdicts: list[str]) -> str:
    if any(v in ("fail", "timeout", "error") for v in verdicts):
        return "fail"
    if any(v in ("missing", "blocked") for v in verdicts):
        return "missing"
    if any(v == "skipped" for v in verdicts):
        return "not_run"
    return "pass"


def make_doc(
    os_: str, *, changes: dict[str, str] | None = None, commit: str = "a" * 40, **extra: Any
) -> dict[str, Any]:
    """``changes`` は検査 id -> 判定の上書き。出口条件の判定は検査から組み直す。"""
    checks = []
    for cid, crit, verdict in CHECKS:
        v = (changes or {}).get(cid, verdict)
        c: dict[str, Any] = {"id": cid, "criterion": crit, "gating": True, "verdict": v}
        if v == "fail":
            c["failed_nodes"] = [{"node": "tests/x.py::test_a", "kind": "failure", "message": ""}]
        checks.append(c)
    crit: dict[str, Any] = {}
    for n in range(1, 9):
        cid = f"M1-{n}"
        vs = [c["verdict"] for c in checks if c["criterion"] == cid]
        crit[cid] = {
            "verdict": _rollup(vs),
            "checks": {c["id"]: c["verdict"] for c in checks if c["criterion"] == cid},
        }
    doc = {
        "schema": "lucianke-bench-exit/1",
        "kind": "exit",
        "finished_at": "2026-10-29T15:15:00+09:00",
        "started_at": "2026-10-29T15:00:00+09:00",
        "machine": {"os": os_, "os_detail": f"{os_}-detail"},
        "args": {"only": [], "skip_slow": True},
        "repo_start": {"commit": commit, "dirty": False},
        "lucianke": {"commit": commit},
        "overall": "fail",
        "criteria": crit,
        "checks": checks,
    }
    doc.update(extra)
    return doc


def write(dirpath: Path, name: str, doc: dict[str, Any]) -> Path:
    p = dirpath / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def run(tmp_path: Path, docs: dict[str, dict[str, Any]], *argv: str) -> tuple[dict[str, Any], int]:
    """OS ごとの成果物フォルダ（CI の download-artifact の形）に置いて、台本の入口から走らせる。"""
    for name, doc in docs.items():
        write(tmp_path / f"exit-{name}", f"20261029-151500-{name}-exit.json", doc)
    out = tmp_path / "out.json"
    rc = cx.main([str(tmp_path), "--json", str(out), *argv])
    return json.loads(out.read_text(encoding="utf-8")), rc


def three(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o: overrides.get(o) or make_doc(o) for o in ("windows", "macos", "linux")}


def test_three_oses_agree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rep, rc = run(tmp_path, three())
    assert rc == 0
    assert rep["verdict"] == "agree"
    assert rep["absent"] == [] and rep["mismatches"] == [] and rep["warnings"] == []
    # 赤（M1-1 の不合格）や未実行（M1-3）でも、3 OS で同じなら一致（M1-7 は「同じ結果」の判定）
    assert {v for row in rep["criteria"].values() for v in row.values()} == {"fail", "pass", "not_run"}
    assert list(rep["criteria"]) == [f"M1-{i}" for i in range(1, 7)]
    # M1-7 / M1-8 の検査は比べない
    assert not any(i.startswith(("M1-7", "M1-8")) for i in rep["checks"])
    out = capsys.readouterr().out
    assert "一致" in out and "M1-1.contract-fpoly" in out and "windows" in out


def test_one_criterion_differs(tmp_path: Path) -> None:
    docs = three(linux=make_doc("linux", changes={"M1-6.e2e-session": "fail"}))
    rep, rc = run(tmp_path, docs)
    assert rc == 1
    assert rep["verdict"] == "differ"
    assert [(m["scope"], m["id"]) for m in rep["mismatches"]] == [("criterion", "M1-6")]
    assert rep["mismatches"][0]["verdicts"] == {"windows": "pass", "macos": "pass", "linux": "fail"}
    assert rep["checks"]["M1-6.e2e-session"]["linux"] == "fail"


def test_check_level_difference_is_a_warning_unless_strict(tmp_path: Path) -> None:
    # M1-1 は postcondition-map の赤で 3 OS とも不合格のまま。中身（contract-fpoly）だけ linux で割れる
    docs = three(linux=make_doc("linux", changes={"M1-1.contract-fpoly": "fail"}))
    rep, rc = run(tmp_path, docs)
    assert rc == 0 and rep["verdict"] == "agree"
    assert any("M1-1.contract-fpoly" in w and "一致扱い" in w for w in rep["warnings"])

    strict_dir = tmp_path / "strict"
    rep2, rc2 = run(strict_dir, docs, "--strict")
    assert rc2 == 1 and rep2["verdict"] == "differ"
    assert [(m["scope"], m["id"]) for m in rep2["mismatches"]] == [("check", "M1-1.contract-fpoly")]


def test_missing_os_is_incomplete(tmp_path: Path) -> None:
    docs = three()
    del docs["macos"]
    rep, rc = run(tmp_path, docs)
    assert rc == 2
    assert rep["verdict"] == "incomplete"
    assert rep["absent"] == ["macos"]
    assert set(rep["present"]) == {"windows", "linux"}


def test_mismatch_wins_over_missing(tmp_path: Path) -> None:
    docs = three(linux=make_doc("linux", changes={"M1-2.ep-transaction": "fail"}))
    del docs["macos"]
    rep, rc = run(tmp_path, docs)
    assert rc == 1 and rep["verdict"] == "differ" and rep["absent"] == ["macos"]


def test_partial_runs_and_garbage_do_not_count(tmp_path: Path) -> None:
    docs = three()
    del docs["linux"]
    part = make_doc("linux")
    part["args"]["only"] = ["M1-5"]
    write(tmp_path / "x", "20261029-160000-linux-exit.json", part)
    write(tmp_path / "x", "20261029-160001-linux-exit.json", {"schema": "other/1"})
    (tmp_path / "x" / "20261029-160002-linux-exit.json").write_text("{not json", encoding="utf-8")
    rep, rc = run(tmp_path, docs)
    assert rc == 2 and rep["absent"] == ["linux"]
    joined = "\n".join(rep["warnings"])
    assert "部分実行" in joined and "形式ではない" in joined and "読めない" in joined


def test_newest_result_per_os_is_used(tmp_path: Path) -> None:
    old = make_doc("windows", changes={"M1-6.e2e-session": "fail"})
    old["finished_at"] = "2026-10-29T10:00:00+09:00"
    write(tmp_path / "old", "20261029-100000-windows-exit.json", old)
    rep, rc = run(tmp_path, three())  # 新しい windows（15:15）は M1-6 が合格
    assert rc == 0 and rep["verdict"] == "agree"
    assert any("windows: 候補が 2 件" in w for w in rep["warnings"])


def test_failed_nodes_commit_and_conditions_are_warnings(tmp_path: Path) -> None:
    lin = make_doc("linux", commit="b" * 40)
    lin["args"]["skip_slow"] = False
    lin["checks"][1]["failed_nodes"] = [{"node": "tests/x.py::test_b", "kind": "failure", "message": ""}]
    rep, rc = run(tmp_path, three(linux=lin))
    assert rc == 0
    joined = "\n".join(rep["warnings"])
    assert "コミットが OS で違う" in joined
    assert "--skip-slow" in joined
    assert (
        "赤いテストが OS で違う" in joined
        and "tests/x.py::test_a" in joined
        and "tests/x.py::test_b" in joined
    )


def test_counts_that_differ_are_reference_only(tmp_path: Path) -> None:
    def with_tests(doc: dict[str, Any], skipped: int) -> dict[str, Any]:
        doc = copy.deepcopy(doc)
        doc["checks"][0]["tests"] = {
            "total": 10,
            "passed": 10 - skipped,
            "failed": 0,
            "errors": 0,
            "skipped": skipped,
        }
        return doc

    docs = {o: with_tests(make_doc(o), 2 if o == "linux" else 0) for o in ("windows", "macos", "linux")}
    rep, rc = run(tmp_path, docs)
    assert rc == 0 and rep["verdict"] == "agree"
    assert rep["counts"]["M1-1.contract-fpoly"]["linux"]["skipped"] == 2
    assert rep["counts"]["M1-1.contract-fpoly"]["windows"]["skipped"] == 0


def test_require_option_allows_a_local_pair(tmp_path: Path) -> None:
    docs = three()
    del docs["macos"]
    rep, rc = run(tmp_path, docs, "--require", "windows,linux")
    assert rc == 0 and rep["verdict"] == "agree" and rep["required"] == ["windows", "linux"]


def test_real_windows_result_shape_is_readable() -> None:
    """リポジトリにある実行器の本物の出力（Windows）を読めること。形式が変わったらここが先に落ちる。"""
    files = sorted((REPO / "bench" / "results").glob("*-windows-exit.json"))
    if not files:
        pytest.skip("bench/results/ に windows の exit 結果が無い")
    results, notes = cx.load_results(files)
    if "windows" not in results:
        pytest.skip(f"数えられる windows の exit 結果が無い（部分実行のみ?）: {notes}")
    rep = cx.compare(results, required=("windows",))
    assert rep["verdict"] == "agree" and list(rep["criteria"]) == [f"M1-{i}" for i in range(1, 7)]
    assert all(set(row) == {"windows"} for row in rep["criteria"].values())
