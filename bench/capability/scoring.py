"""Scoring of normalised answers against the ground truth in the tasks file (generic, no project knowledge).

Rules (also in README.md):

* site sets (symbol_search, references, rename): precision / recall / F1 over ``path:line``. ``neutral`` sites and
  the target's own definition line are ignored on both sides. ``prod`` and ``tests`` are also scored separately.
  Definition sites (symbol_search) match within ``DEF_TOLERANCE`` lines (decorators may be reported as the start).
* call hierarchy: every reported site (a call site or a definition) is mapped to the innermost enclosing
  ``def`` in the workspace file (Python AST), giving ``path::Qualified.name`` keys; sites outside the workspace
  or not inside any def are dropped as neutral. An adapter may instead report keys directly; a key written
  ``::Qualified.name`` (callee named without its file) matches the expected key with that qualified name.
  Precision / recall / F1 over the keys.
* skeleton: recall of the expected symbol names (multiset of the last name component); extra names are not
  penalised (a skeleton may list constants or nested helpers).
* definition: exact match of ``path:line`` (within ``DEF_TOLERANCE``) and every ``signature_contains`` fragment
  present in the answer text.
* body: share of the expected body's non-blank lines (stripped) that occur in the answer text.
* orientation: share of the expected paths mentioned in the answer text.
* diagnostics: recall over ``required + secondary`` lines of the file; passes when every ``required`` line is hit.
* ``pass`` per kind: F1 >= 0.9 for site sets and call hierarchies, recall >= ``pass_recall`` (default 0.95) for
  skeleton / orientation (task value wins), 1.0 for definition, >= 0.95 for body.
"""

from __future__ import annotations

import ast
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

DEF_TOLERANCE = 3
F1_PASS = 0.9


def is_test(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path


def _site(s: str) -> tuple[str, int]:
    p, n = s.rsplit(":", 1)
    return p, int(n)


def prf(found: set[Any], expected: set[Any]) -> dict[str, Any]:
    tp = len(found & expected)
    p = tp / len(found) if found else (1.0 if not expected else 0.0)
    r = tp / len(expected) if expected else 1.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {
        "tp": tp,
        "fp": len(found - expected),
        "fn": len(expected - found),
        "precision": round(p, 3),
        "recall": round(r, 3),
        "f1": round(f1, 3),
    }


def _with_tolerance(
    found: set[tuple[str, int]], expected: set[tuple[str, int]], tol: int
) -> set[tuple[str, int]]:
    """Snap each found site onto an expected site of the same file within ``tol`` lines."""
    out = set()
    for p, n in found:
        snap = next(((ep, en) for ep, en in expected if ep == p and abs(en - n) <= tol), None)
        out.add(snap or (p, n))
    return out


def score_sites(
    found_list: list[tuple[str, int]], exp: dict[str, Any], *, ignore: set[tuple[str, int]], tol: int = 0
) -> dict[str, Any]:
    prod = {_site(s) for s in exp.get("prod", [])}
    tests = {_site(s) for s in exp.get("tests", [])}
    if "sites" in exp:
        for s in exp["sites"]:
            (tests if is_test(_site(s)[0]) else prod).add(_site(s))
    neutral = {_site(s) for s in exp.get("neutral", [])} | ignore
    expected = prod | tests
    found = {s for s in found_list if s not in neutral}
    if tol:
        found = _with_tolerance(found, expected, tol)
    traps = {_site(s) for s in exp.get("traps", [])}
    res = prf(found, expected)
    res["prod"] = prf({s for s in found if not is_test(s[0])}, prod)
    res["tests"] = prf({s for s in found if is_test(s[0])}, tests)
    res["traps_reported"] = sorted(f"{p}:{n}" for p, n in found & traps)
    res["false_positives"] = sorted(f"{p}:{n}" for p, n in found - expected)[:60]
    res["missed"] = sorted(f"{p}:{n}" for p, n in expected - found)[:60]
    res["pass"] = res["f1"] >= F1_PASS
    return res


@cache
def _tree(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None


def enclosing_key(workspace: Path, rel: str, line: int) -> str | None:
    tree = _tree((workspace / rel).resolve())
    if tree is None:
        return None
    chain: list[str] = []

    def visit(node: ast.AST) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                if start <= line <= (ch.end_lineno or ch.lineno):
                    chain.append(ch.name)
                    visit(ch)
                    return
            elif hasattr(ch, "lineno") and ch.lineno <= line <= (
                getattr(ch, "end_lineno", None) or ch.lineno
            ):
                visit(ch)
                return

    visit(tree)
    return f"{rel}::{'.'.join(chain)}" if chain else None


def score(task: dict[str, Any], answer: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """``answer`` is ``Answer.to_json()`` plus the full ``text`` under ``text``."""
    kind = task["kind"]
    exp = task.get("expected", {})
    if answer.get("unsupported"):
        return {"supported": False, "pass": False, "f1": None, "reason": answer["unsupported"]}
    sites = [_site(s) for s in answer.get("sites", [])]
    text = answer.get("text", "") or ""
    if kind in ("references", "rename", "symbol_search"):
        tgt = task.get("target", {})
        ignore = {(tgt["path"], int(tgt["line"]))} if tgt.get("line") and kind == "references" else set()
        return score_sites(sites, exp, ignore=ignore, tol=DEF_TOLERANCE if kind == "symbol_search" else 0)
    if kind == "call_hierarchy":
        expected = set(exp.get("keys", []))
        tgt = task.get("target", {})
        self_key = enclosing_key(workspace, tgt.get("path", ""), int(tgt.get("line", 0) or 0))
        found = set()
        for p, n in sites:
            k = enclosing_key(workspace, p, n) if (workspace / p).is_file() else None
            if k and k != self_key:
                found.add(k)
        for k in (answer.get("extra") or {}).get(
            "keys", []
        ):  # callee names without a file: "::Qualified.name"
            if k.startswith("::"):
                match = [e for e in expected if e.split("::", 1)[1] == k[2:]]
                found.add(match[0] if match else k)
            else:
                found.add(k)
        res = prf(found, expected)
        res["prod"] = prf({k for k in found if not is_test(k)}, {k for k in expected if not is_test(k)})
        res["tests"] = prf({k for k in found if is_test(k)}, {k for k in expected if is_test(k)})
        if "depth1" in exp:
            d1 = set(exp["depth1"])
            res["depth1_recall"] = round(len(found & d1) / len(d1), 3) if d1 else None
        res["false_positives"] = sorted(found - expected)[:60]
        res["missed"] = sorted(expected - found)[:60]
        res["pass"] = res["f1"] >= F1_PASS
        return res
    if kind == "skeleton":
        want = Counter(s.rsplit(":", 1)[0].split(".")[-1] for s in exp.get("symbols", []))
        got = Counter(answer.get("names", []))
        hit = sum(min(c, got.get(n, 0)) for n, c in want.items())
        total = sum(want.values())
        r = hit / total if total else 1.0
        missing = sorted((want - got).elements())
        return {
            "recall": round(r, 3),
            "f1": round(r, 3),
            "found": hit,
            "expected": total,
            "missed": missing[:60],
            "pass": r >= float(task.get("pass_recall", 0.95)),
        }
    if kind == "definition":
        want = _site(exp["location"])
        loc = answer.get("location")
        loc_ok = False
        if loc:
            p, n = _site(loc)
            loc_ok = p == want[0] and abs(n - want[1]) <= DEF_TOLERANCE
        frags = exp.get("signature_contains", [])
        sig_ok = all(f in text for f in frags)
        ok = loc_ok and sig_ok
        return {
            "location_ok": loc_ok,
            "signature_ok": sig_ok,
            "f1": 1.0 if ok else 0.0,
            "pass": ok,
            "reported": loc,
        }
    if kind == "body":
        lines = (workspace / exp["path"]).read_text(encoding="utf-8").splitlines()
        body = [ln.strip() for ln in lines[int(exp["start"]) - 1 : int(exp["end"])] if ln.strip()]
        hit = sum(1 for ln in body if ln in text)
        r = hit / len(body) if body else 1.0
        return {"recall": round(r, 3), "f1": round(r, 3), "lines": len(body), "found": hit, "pass": r >= 0.95}
    if kind == "orientation":
        norm = text.replace("\\", "/")
        want = exp.get("mentions", [])
        hit = [m for m in want if m in norm]
        r = len(hit) / len(want) if want else 1.0
        return {
            "recall": round(r, 3),
            "f1": round(r, 3),
            "missed": [m for m in want if m not in hit],
            "pass": r >= float(task.get("pass_recall", 0.95)),
        }
    if kind == "diagnostics":
        path = task["path"]
        lines_hit = {n for p, n in sites if p == path}
        req = {int(x) for x in exp.get("required", [])}
        sec = {int(x) for x in exp.get("secondary", [])}
        allx = req | sec
        r = len(lines_hit & allx) / len(allx) if allx else 1.0
        ok = req <= lines_hit
        return {
            "recall": round(r, 3),
            "f1": round(r, 3),
            "required_ok": ok,
            "reported_lines": sorted(lines_hit)[:60],
            "reported": len(sites),
            "pass": ok,
        }
    return {"supported": False, "pass": False, "f1": None, "reason": f"unknown kind {kind}"}
