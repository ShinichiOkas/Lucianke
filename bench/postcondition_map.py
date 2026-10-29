"""事後条件 -> 契約テストの対応表を機械で作る（M1-1、設計書 R-9 の対策）。

仕様書第 11 章の各ツールの「事後条件」の番号（箇条書きは出現順に 1, 2, ...）と、§10.10 の EP-1〜EP-9 を取り出し、
``tests/`` の試験関数から「どの事後条件を確かめているか」を **名前で** 探して、対応の無い事後条件を一覧にする。

試験が事後条件を名乗る書き方（どれか 1 つでよい。関数の名前・docstring・本体のコメント・直前の ``# --- ... ---`` 見出し）:

* 名前: ``test_postcondition_4_...``、``test_t06_postcondition_4_5_...``、``test_t19_post3_...``、``..._post1_and_2_...``
* docstring・コメント・見出し: ``事後条件 4``、``事後条件 1・2・5``、``事後条件 1〜5``
* 明示の印（別のツールの試験が混ざるファイル向け）: ``PC:T-07#4``
* EP: ``EP-3``、``EP-1〜EP-3``、名前の ``test_ep8_...``

ツールは、ファイル名の ``T02`` / ``T06_T07``、関数名の ``t06_``、docstring・コメント・見出しの ``T-07`` から決める。
決められない試験は ``unattributed`` に出す（対応には数えない）。

限界（R-9 の残り）: 対応は名前の一致だけで、試験の中身が事後条件の文と合っているかは見ない。番号が仕様書の項目数を
超える試験は ``out_of_range`` に出す。中身の突き合わせは、試験に ``PC:T-xx#n`` の印を付けて人が（役が）直す。

使い方::

    uv run python bench/postcondition_map.py                       # 表を標準出力へ
    uv run python bench/postcondition_map.py --json out.json --md out.md
    uv run python bench/postcondition_map.py --strict              # 未対応があれば終了コード 1

標準ライブラリだけ。結果の ``unmapped_total`` は M1 の 14 ツールの未対応数。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
SPEC = REPO / "docs" / "Lucianke仕様書.md"
CONTRACT_DIR = REPO / "tests" / "contract"
# 事後条件の試験を探す場所。契約テスト + EP は編集系の単体・契約
SEARCH_DIRS = [REPO / "tests" / "contract", REPO / "tests" / "e2e"]
EP_DIRS = [REPO / "tests" / "unit", REPO / "tests" / "contract", REPO / "tests" / "invariants"]

_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


# --- 仕様書 -----------------------------------------------------------------------------------


def parse_spec(text: str) -> dict[str, dict[str, Any]]:
    """``### T-NN `name` `` ごとに ``{name, milestone, items: [{n, text}]}`` を返す。"""
    lines = text.splitlines()
    tools: dict[str, dict[str, Any]] = {}
    cur: str | None = None
    i = 0
    in_fence = False
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:  # コード例の中の「# get_file_skeleton ok ...」を見出しと取り違えない
            i += 1
            continue
        m = re.match(r"^### (T-\d\d) `([^`]+)`", ln)
        if m:
            cur = m.group(1)
            tools[cur] = {"name": m.group(2), "milestone": None, "items": [], "line": i + 1}
            i += 1
            continue
        if re.match(r"^#{1,3} ", ln) and not m:
            cur = None
        if cur:
            mm = re.match(r"^\*\*マイルストーン\*\*:\s*(M\d)", ln)
            if mm:
                tools[cur]["milestone"] = mm.group(1)
            if re.match(r"^\*\*事後条件\*\*", ln):
                items: list[dict[str, Any]] = []
                j = i + 1
                auto = 0
                while j < len(lines):
                    s = lines[j]
                    if not s.strip():
                        j += 1
                        continue
                    if s.startswith("---") or s.startswith("#"):
                        break
                    num = re.match(r"^(\d+)\.\s+(.*)", s)
                    bul = re.match(r"^[-*]\s+(.*)", s)
                    if num:
                        auto = int(num.group(1))
                        items.append({"n": auto, "text": num.group(2).strip()})
                    elif bul:
                        auto += 1
                        items.append({"n": auto, "text": bul.group(1).strip()})
                    elif s.startswith((" ", "\t")) and items:
                        items[-1]["text"] += " " + s.strip()
                    else:
                        break
                    j += 1
                tools[cur]["items"] = items
                i = j
                continue
        i += 1
    return tools


def parse_ep(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"^\|\s*(EP-\d)\s*\|\s*(.*?)\s*\|\s*$", text, re.M):
        out[m.group(1)] = m.group(2)
    return out


# --- 試験 -------------------------------------------------------------------------------------


def _expand(spec: str) -> list[int]:
    """``1・2・5`` / ``1〜5`` / ``4, 5`` / ``4_5`` を整数の列にする。"""
    nums: list[int] = []
    toks = re.findall(r"\d+|[〜~\-]", spec.translate(_DIGITS))
    k = 0
    while k < len(toks):
        if toks[k].isdigit():
            n = int(toks[k])
            if k + 2 < len(toks) and toks[k + 1] in "〜~-" and toks[k + 2].isdigit():
                nums.extend(range(n, int(toks[k + 2]) + 1))
                k += 3
                continue
            nums.append(n)
        k += 1
    return nums


_RE_JP = re.compile(r"事後条件\s*([0-9０-９]+(?:\s*(?:[・,、〜~\-]|と|及び)\s*[0-9０-９]+)*)")
_RE_NAME = re.compile(r"(?<![A-Za-z])(?:postcondition|post|pc)_?(\d+)((?:_(?:and_)?\d+)*)")
_RE_MARK = re.compile(r"PC:T-(\d\d)#(\d+)")
_RE_EP = re.compile(r"\bEP-(\d)(?:\s*[〜~\-]\s*EP-(\d))?")
_RE_EP_NAME = re.compile(r"(?:^|_)ep(\d)(?:_|$)")


def numbers_in_text(text: str) -> list[int]:
    out: list[int] = []
    for m in _RE_JP.finditer(text):
        sep = re.sub(r"と|及び", "・", m.group(1))
        out.extend(_expand(sep))
    return out


def numbers_in_name(name: str) -> list[int]:
    out: list[int] = []
    for m in _RE_NAME.finditer(name):
        out.append(int(m.group(1)))
        out.extend(int(x) for x in re.findall(r"\d+", m.group(2)))
    return out


def comment_text(src_lines: list[str]) -> str:
    out = []
    for ln in src_lines:
        if "#" in ln:
            out.append(ln.split("#", 1)[1])
    return "\n".join(out)


class TestRef:
    __slots__ = ("node", "file", "name", "lineno", "tools", "pcs", "eps", "evidence", "fixtures")

    def __init__(self) -> None:
        self.tools: set[str] = set()
        self.pcs: dict[str, set[int]] = {}
        self.eps: set[int] = set()
        self.evidence: set[str] = set()


def fixtures_used(src: str) -> list[str]:
    """試験ファイルが使うフィクスチャ（F-PY / F-TS は bench/.cache の実物を読む試験だけ）。"""
    used = []
    if re.search(r"fixtures[/\x5c]poly|make_fpoly", src) or re.search(r'"fixtures"\s*/\s*"poly"', src):
        used.append("F-POLY")
    if re.search(r"fetch_fixtures|fixtures\.toml|bench[/\x5c]\.cache", src) or re.search(r'"bench"\s*/\s*"\.cache"', src):
        used.extend(["F-PY", "F-TS"])
    return used


def scan_file(path: Path) -> tuple[list[TestRef], list[str]]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    file_tools = {f"T-{n}" for n in re.findall(r"T(\d\d)", path.stem)}
    headers = [(i + 1, ln) for i, ln in enumerate(lines) if re.match(r"^# -{3,}", ln)]
    fx = fixtures_used(src)
    refs: list[TestRef] = []

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + "::")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test"):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                seg = lines[first - 1 : (node.end_lineno or first)]
                doc = ast.get_docstring(node) or ""
                comments = comment_text(seg)
                hdr = ""
                for ln_no, hl in headers:
                    if ln_no < first:
                        hdr = hl
                    else:
                        break
                r = TestRef()
                r.node = f"{path.relative_to(REPO).as_posix()}::{prefix}{node.name}"
                r.file, r.name, r.lineno = path.relative_to(REPO).as_posix(), node.name, node.lineno
                r.fixtures = fx
                blob = doc + "\n" + comments
                # --- ツール
                tools = set(file_tools)
                if len(tools) != 1:
                    tools = {f"T-{n}" for n in re.findall(r"(?<![A-Za-z0-9])t(\d\d)(?:_|$)", node.name)}
                    if not tools:
                        tools = set(re.findall(r"T-\d\d", blob))
                    if not tools:
                        tools = set(re.findall(r"T-\d\d", hdr)) | {f"T-{n}" for n in re.findall(r"(?<![A-Za-z0-9])t(\d\d)(?![0-9])", hdr)}
                    tools &= file_tools or tools
                r.tools = tools
                # --- 事後条件の番号（根拠ごと）
                # 根拠は強い順に見て、最初に番号が出た段で止める（名前が 4 を名乗るのに、見出しの 1〜5 を全部引き受けない）
                nums: dict[int, set[str]] = {}
                for level, found in (
                    ("name", numbers_in_name(node.name)),
                    ("doc", numbers_in_text(doc)),
                    ("comment", numbers_in_text(comments)),
                    ("section", numbers_in_text(hdr)),
                ):
                    if found:
                        for n in found:
                            nums.setdefault(n, set()).add(level)
                        break
                for t, n in _RE_MARK.findall(blob):
                    r.pcs.setdefault(f"T-{t}", set()).add(int(n))
                    r.evidence.add("mark")
                if nums:
                    for t in r.tools:
                        r.pcs.setdefault(t, set()).update(nums)
                    for ev in nums.values():
                        r.evidence |= ev
                    if not r.tools:
                        r.pcs.setdefault("?", set()).update(nums)
                # --- EP
                for m in _RE_EP.finditer(blob + "\n" + hdr):
                    a = int(m.group(1))
                    b = int(m.group(2)) if m.group(2) else a
                    r.eps.update(range(a, b + 1))
                for m in _RE_EP_NAME.finditer(node.name):
                    r.eps.add(int(m.group(1)))
                refs.append(r)

    visit(tree.body, "")
    return refs, sorted(file_tools)


# --- 突き合わせ -------------------------------------------------------------------------------


def build(m1_only: bool) -> dict[str, Any]:
    text = SPEC.read_text(encoding="utf-8")
    spec = parse_spec(text)
    ep_spec = parse_ep(text)
    refs: list[TestRef] = []
    for d in SEARCH_DIRS:
        for p in sorted(d.glob("test_*.py")):
            refs.extend(scan_file(p)[0])
    ep_refs: list[TestRef] = []
    for d in EP_DIRS:
        for p in sorted(d.glob("test_*.py")):
            ep_refs.extend(r for r in scan_file(p)[0] if r.eps)

    unattributed = sorted({r.node for r in refs if "?" in r.pcs})
    tools_out: dict[str, Any] = {}
    out_of_range: list[dict[str, Any]] = []
    for tid, info in spec.items():
        items = info["items"]
        cnt = len(items)
        pcs = []
        for it in items:
            n = it["n"]
            hit = [r for r in refs if n in r.pcs.get(tid, ())]
            fx = sorted({f for r in hit for f in r.fixtures}) if hit else []
            pcs.append({"n": n, "text": it["text"][:200],
                        "tests": [{"node": r.node, "evidence": sorted(r.evidence)} for r in hit], "fixtures": fx})  # fmt: skip
        for r in refs:
            for n in r.pcs.get(tid, ()):
                if n > cnt:
                    out_of_range.append({"tool": tid, "n": n, "spec_count": cnt, "node": r.node})
        unmapped = [p["n"] for p in pcs if not p["tests"]]
        tools_out[tid] = {"name": info["name"], "milestone": info["milestone"], "spec_count": cnt, "postconditions": pcs,
                          "unmapped": unmapped, "fixtures": sorted({f for p in pcs for f in p["fixtures"]})}  # fmt: skip
    m1 = {t: v for t, v in tools_out.items() if v["milestone"] == "M1"}
    scope = m1 if m1_only else tools_out
    ep_out = []
    for ep_id, txt in ep_spec.items():
        n = int(ep_id.split("-")[1])
        hit = [r for r in ep_refs if n in r.eps]
        ep_out.append({"id": ep_id, "text": txt[:160], "tests": [r.node for r in hit]})
    ep_unmapped = [e["id"] for e in ep_out if not e["tests"]]
    fx_matrix = {t: {f: (f in v["fixtures"]) for f in ("F-POLY", "F-PY", "F-TS")} for t, v in m1.items()}
    return {
        "schema": "lucianke-postcondition-map/1",
        "spec": SPEC.relative_to(REPO).as_posix(),
        "m1_tools": sorted(m1),
        "spec_total": sum(v["spec_count"] for v in m1.values()),
        "unmapped_total": sum(len(v["unmapped"]) for v in m1.values()),
        "unmapped_total_all_tools": sum(len(v["unmapped"]) for v in tools_out.values()),
        "tools": scope,
        "out_of_range": out_of_range,
        "unattributed": unattributed,
        "ep": {"spec_total": len(ep_out), "unmapped_total": len(ep_unmapped), "unmapped": ep_unmapped, "items": ep_out},
        "fixture_matrix_m1": fx_matrix,
        "fixture_gaps": {f: [t for t, row in fx_matrix.items() if not row[f]] for f in ("F-POLY", "F-PY", "F-TS")},
    }


def render_md(d: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("# 事後条件 -> 契約テストの対応表")
    L.append("")
    L.append(f"M1 の 14 ツールの事後条件 {d['spec_total']} 件のうち、**対応するテストが無いもの {d['unmapped_total']} 件**。"
             f"EP-1〜EP-9 のうち未対応 {d['ep']['unmapped_total']} 件。対応は名前の一致だけを見ている（中身の突き合わせではない）。")  # fmt: skip
    L.append("")
    L.append("| ツール | 事後条件 | 未対応の番号 | F-POLY | F-PY | F-TS |")
    L.append("|---|---|---|---|---|---|")
    for tid, v in d["tools"].items():
        fx = d["fixture_matrix_m1"].get(tid)
        cols = [("o" if fx[f] else "-") for f in ("F-POLY", "F-PY", "F-TS")] if fx else ["", "", ""]
        L.append(f"| {tid} `{v['name']}` ({v['milestone']}) | {v['spec_count']} | {', '.join(map(str, v['unmapped'])) or '-'} | " + " | ".join(cols) + " |")
    L.append("")
    L.append("## 未対応の事後条件")
    L.append("")
    any_un = False
    for tid, v in d["tools"].items():
        for p in v["postconditions"]:
            if not p["tests"]:
                any_un = True
                L.append(f"- {tid} 事後条件 {p['n']}: {p['text']}")
    if not any_un:
        L.append("なし。")
    if d["out_of_range"]:
        L.append("")
        L.append("## 番号が仕様書の項目数を超える試験")
        L.append("")
        for o in d["out_of_range"]:
            L.append(f"- {o['tool']} 事後条件 {o['n']}（項目数 {o['spec_count']}）: `{o['node']}`")
    if d["unattributed"]:
        L.append("")
        L.append("## ツールを決められなかった試験（番号だけある）")
        L.append("")
        L.extend(f"- `{n}`" for n in d["unattributed"])
    L.append("")
    L.append("## EP-1〜EP-9")
    L.append("")
    for e in d["ep"]["items"]:
        L.append(f"- {e['id']}: " + (", ".join(f"`{t.split('::', 1)[1]}`" for t in e["tests"][:4]) + (" ほか" if len(e["tests"]) > 4 else "") if e["tests"] else "**未対応**"))
    L.append("")
    L.append("## フィクスチャの被覆（M1 の 14 ツール）")
    L.append("")
    for f, ts in d["fixture_gaps"].items():
        L.append(f"- {f} で事後条件を試験していないツール: {', '.join(ts) if ts else 'なし'}")
    L.append("")
    L.append("## 対応の詳細")
    L.append("")
    for tid, v in d["tools"].items():
        L.append(f"### {tid} `{v['name']}`")
        for p in v["postconditions"]:
            ts = ", ".join(f"`{t['node'].split('::', 1)[1]}`" for t in p["tests"][:3]) + (" ほか" if len(p["tests"]) > 3 else "")
            L.append(f"- {p['n']}. {p['text'][:80]} -> {ts or '**未対応**'}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", type=Path, help="結果を JSON で書く")
    ap.add_argument("--md", type=Path, help="結果を Markdown で書く")
    ap.add_argument("--m1-only", action="store_true", help="表示・JSON の tools を M1 の 14 ツールに絞る")
    ap.add_argument("--ep-only", action="store_true", help="（互換）EP だけ見る用。JSON にはいつも ep が入る")
    ap.add_argument("--strict", action="store_true", help="未対応があれば終了コード 1")
    ap.add_argument("--quiet", action="store_true", help="表を標準出力に出さない（1 行の要約だけ）")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    d = build(args.m1_only)
    md = render_md(d)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(md + "\n", encoding="utf-8")
    if not args.quiet:
        print(md)
    print(f"postcondition_map: M1 事後条件 {d['spec_total']} 件、未対応 {d['unmapped_total']} 件、EP 未対応 {d['ep']['unmapped_total']} 件、"
          f"範囲外 {len(d['out_of_range'])} 件、ツール不明 {len(d['unattributed'])} 件")  # fmt: skip
    bad = d["unmapped_total"] or d["ep"]["unmapped_total"]
    return 1 if (args.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
