"""M1-6 の台本化セッション実行器（仕様書 §15.2 M1-6）。

``.mcp.json`` に書かれた ``command`` と ``args`` をそのまま使って ``lucianke`` を起動する（Claude Code と同じ起動のしかた。
cwd は ``.mcp.json`` のあるリポジトリのルート）。MCP の stdio クライアント（SDK）でつなぎ、``session.toml`` の台本
（tools/list -> 骨格 -> 本体 -> 編集 -> 診断ゼロ確認）を順に呼んで、各段の応答と事後条件の合否を
``bench/e2e/records/<日時>-<OS>.jsonl`` に 1 段 1 行で記録する。最後の行が全体の合否。

* ワークスペースの根は F-POLY（``tests/fixtures/poly``）の tmp コピー。既定では MCP の ``roots/list`` で向ける
  （Claude Code と同じ）。``--via set_roots`` なら roots 能力を宣言せず、T-00 ``configure_workspace set_roots`` で向ける。
* 待ちは固定 sleep ではなく条件待ち（台本の ``wait_until``）。止まった段は「未成立」（``unmet``）と記録し、先へ進む
  （前提の段が未成立の段だけ ``blocked``）。
* 索引キャッシュは tmp（``LUCIANKE_CACHE_DIR``）。``.mcp.json`` に無い環境変数はこれと ``PYTHONUTF8`` だけ。
  ``.mcp.json`` の ``--directory`` が存在しないパス（別の OS や別のチェックアウト）なら、このリポジトリに置き換えて記録する。
* ``claude`` CLI は起動しない（モデルの呼び出しと費用が伴うため）。

使い方::

    uv run --no-sync python bench/e2e/run_session.py [--via roots|set_roots] [--records-dir DIR]

終了コード: 0 = 全段が成立、1 = 未成立の段がある、2 = 実行器の失敗（起動できない等）。標準出力は ASCII だけ
（日本語は記録のファイルに UTF-8 で書く）。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import difflib
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mcp.types as mt
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning

warnings.filterwarnings("ignore", category=MCPDeprecationWarning)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "bench"))
sys.path.insert(0, str(REPO / "tests" / "contract"))
import _results  # noqa: E402  標準ライブラリだけの共通部品（結果ファイル名・OS 名）
import _stdio  # noqa: E402  プロセス表（子孫の観測と片付け）だけを使う
import compact_grammar as g  # noqa: E402  Compact DSL の文法検査器

SCRIPT = HERE / "session.toml"
RECORDS = HERE / "records"
MCP_JSON = REPO / ".mcp.json"
SEV_ORDER = {"E": 0, "W": 1, "I": 2, "H": 3}
MIN_SEV = {"error": "E", "warning": "W", "information": "I", "hint": "H"}
STALE_BANNER = "No fresh diagnostics"

HEADER_RE = re.compile(
    r"^# (?P<tool>\S+) (?P<status>ok|error)(?: (?P<code>E_[A-Z0-9_]+))?(?P<kvs>(?: [A-Za-z_]\w*=\S+)*)"
)
DIAG_RE = re.compile(
    r"^(?P<sev>[EWIH]) (?P<path>\S.*?):(?P<line>\d+)(?::(?P<col>\d+))? "
    r"(?:\[(?P<source>[^:\]\s]+):(?P<code>[^\]\s]+)\] )?(?P<msg>\S.*?)(?:  fix=(?P<fix>\S+))?$"
)
FILE_RE = re.compile(r"^=== (?P<path>.+) \((?P<lines>\d+) lines.*?, sha=(?P<sha>[0-9a-f]{12})\) ===$")
RANGE_RE = re.compile(r"@L(?P<start>\d+)(?:-(?P<end>\d+))?")


def ascii_safe(s: object) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")


# --- 応答 ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class Resp:
    tool: str
    args: dict[str, Any]
    is_error: bool
    text: str
    structured: dict[str, Any] | None
    rtt_ms: float
    rpc_error: str | None = None

    @property
    def header(self) -> dict[str, str]:
        """compact の先頭行を ``status`` / ``code`` / ``conf`` / ``lsp`` ... に分ける。compact でなければ空。"""
        line = self.text.split("\n", 1)[0] if self.text else ""
        m = HEADER_RE.match(line)
        if not m:
            return {}
        out = {"status": m["status"], "code": m["code"] or ""}
        for kv in m["kvs"].split():
            k, _, v = kv.partition("=")
            out[k] = v
        return out

    @property
    def is_json(self) -> bool:
        return self.text.startswith("{")

    @property
    def envelope(self) -> dict[str, Any]:
        if self.structured is not None:
            return self.structured
        with contextlib.suppress(ValueError):
            obj = json.loads(self.text)
            if isinstance(obj, dict):
                return obj
        return {}

    @property
    def ok(self) -> bool:
        if self.rpc_error is not None or self.is_error:
            return False
        if self.tool == "tools/list":
            return True
        if self.is_json:
            return bool(self.envelope.get("ok"))
        return self.header.get("status") == "ok"

    @property
    def error_code(self) -> str:
        if self.rpc_error:
            return f"rpc:{self.rpc_error}"[:80]
        if self.is_json:
            return str((self.envelope.get("error") or {}).get("code", ""))
        return self.header.get("code", "")

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")


@dataclass(slots=True)
class Diag:
    sev: str
    path: str
    line: int
    col: int
    source: str
    code: str
    msg: str


def parse_diags(lines: list[str]) -> list[Diag]:
    out: list[Diag] = []
    for ln in lines:
        m = DIAG_RE.match(ln)
        if m:
            out.append(Diag(m["sev"], m["path"], int(m["line"]), int(m["col"] or 0), m["source"] or "", m["code"] or "", m["msg"]))
    return out


# --- 実行文脈 -----------------------------------------------------------------------------------


@dataclass
class Ctx:
    cfg: dict[str, Any]
    checks: dict[str, dict[str, Any]]
    ws: Path
    vars: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, str] = field(default_factory=dict)
    out: dict[str, Resp] = field(default_factory=dict)
    parsed: dict[str, dict[str, Any]] = field(default_factory=dict)
    created: set[str] = field(default_factory=set)

    @property
    def file(self) -> Path:
        return self.ws / self.cfg["file"]


def make_manifest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            try:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                out[rel] = "unreadable"
    return out


def subst(obj: Any, vars_: dict[str, Any]) -> Any:
    """``"$名前"`` だけの文字列を ``vars_`` の値に置き換える（入れ子の表・配列も）。"""
    if isinstance(obj, str) and obj.startswith("$") and obj[1:] in vars_:
        return vars_[obj[1:]]
    if isinstance(obj, dict):
        return {k: subst(v, vars_) for k, v in obj.items()}
    if isinstance(obj, list):
        return [subst(v, vars_) for v in obj]
    return obj


# --- 各段の応答から取り出すもの -------------------------------------------------------------------


_SYM_NAME_RE = re.compile(r"^(?P<indent>\s*)\[\w+\] (?:(?:pub|priv|prot|static|async|abstract|export) )*(?P<name>[^\s(]+)")


def x_skeleton(c: Ctx, r: Resp) -> dict[str, Any]:
    """骨格から対象の記号の範囲と URN を読む。URN は ``urn:`` 行があればそれ、無ければ仕様書 §6.1 規約 2 のとおり
    ``lk://<ヘッダの ws>/<ブロックのパス>#<入れ子をたどった修飾名>`` で導く（BUG-0069: 導ける urn: 行は出ない）。"""
    lines = r.lines
    info: dict[str, Any] = {}
    short = c.cfg["symbol"].rsplit(".", 1)[-1]
    wm = re.search(r" ws=(\S+) ", lines[0]) if lines else None
    block: str | None = None
    chain: list[tuple[int, str]] = []  # (インデント, 行が示す名前)
    for i, ln in enumerate(lines):
        m = FILE_RE.match(ln)
        if m:
            block, chain = m["path"], []
            if m["path"] == c.cfg["file"]:
                info.update(file_lines=int(m["lines"]), sha=m["sha"])
        nm = _SYM_NAME_RE.match(ln)
        if nm:
            indent = len(nm["indent"])
            while chain and chain[-1][0] >= indent:
                chain.pop()
            chain.append((indent, nm["name"]))
        if re.match(rf"^\s*\[(Method|Function)\] .*\b{re.escape(short)}\(", ln):
            rm = RANGE_RE.search(ln)
            if rm:
                indent = len(ln) - len(ln.lstrip())
                info.update(sym_line=ln.strip(), sym_start=int(rm["start"]), sym_end=int(rm["end"] or rm["start"]))
                if wm is not None and block is not None:
                    info["urn"] = f"lk://{wm[1]}/{block}#{'.'.join(n for _, n in chain)}"
                for nxt in lines[i + 1 :]:
                    if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                        break
                    um = re.match(r"^\s*urn: (lk://\S+)$", nxt)
                    if um:
                        info["urn"] = um[1]
                        break
                info["folded"] = re.search(r"\{ \.\.\.(\d+) lines omitted \}", ln) is not None
    return info


def x_bodies(c: Ctx, r: Resp) -> dict[str, Any]:
    info: dict[str, Any] = {"code": []}
    in_fence = False
    for ln in r.lines:
        hm = re.match(r"^\[\w+\] (\S+?)#(\S+) @L(\d+)(?:-(\d+))?$", ln)
        if hm and "path" not in info:
            info.update(path=hm[1], symbol=hm[2], start=int(hm[3]), end=int(hm[4] or hm[3]))
        sm = re.match(r"^\s+sha: ([0-9a-f]{12})$", ln)
        if sm:
            info["sha"] = sm[1]
        if ln.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            cm = re.match(r"^@L(\d+):(?: (.*))?$", ln)
            if cm:
                info["code"].append((int(cm[1]), cm[2] or ""))
    return info


def x_edit(c: Ctx, r: Resp) -> dict[str, Any]:
    info: dict[str, Any] = {"added": [], "resolved": []}
    section = ""
    in_fence = False
    for ln in r.lines:
        if ln.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if (m := re.match(r"^applied: (true|false)\b(?:.*?\bop=(\w+))?", ln)) is not None:
            info.update(applied=m[1] == "true", op=m[2] or "")
        elif (m := re.match(r"^target: (lk://\S+) @L(\d+)(?:-(\d+))?", ln)) is not None:
            info.update(urn=m[1], start=int(m[2]), end=int(m[3] or m[2]))
        elif (m := re.match(r"^files_changed: (\S+) \(\+(\d+) -(\d+)\)\s+sha256 ([0-9a-f]+)…→([0-9a-f]+)…", ln)) is not None:
            info.update(path=m[1], plus=int(m[2]), minus=int(m[3]), sha_before=m[4], sha_after=m[5])
        elif (m := re.match(r"^syntax_check: (\S+)", ln)) is not None:
            info["syntax_check"] = m[1]
        elif (m := re.match(r"^new_diagnostics: (\w+)(?: \(\+(\d+) -(\d+)\))?", ln)) is not None:
            info.update(nd_status=m[1], nd_plus=m[2], nd_minus=m[3])
            section = ""
        elif ln in ("added:", "resolved:"):
            section = ln[:-1]
        elif section and (d := parse_diags([ln])):
            info[section].append(d[0])
        elif ln and not ln.startswith(" "):
            section = ""
    return info


EXTRACT: dict[str, Callable[[Ctx, Resp], dict[str, Any]]] = {
    "get_file_skeleton": x_skeleton,
    "get_symbol_bodies": x_bodies,
    "edit_symbol_body": x_edit,
}


def after_extract(c: Ctx, stage: str, info: dict[str, Any]) -> None:
    if stage == "skeleton":
        c.vars.update({k: info[k] for k in ("urn", "sha", "sym_start", "sym_end") if k in info})
    elif stage == "edit" and "urn" in info:
        c.vars["new_urn"] = info["urn"]


# --- 事後条件 -----------------------------------------------------------------------------------

Result = tuple[bool, str]
CheckFn = Callable[[Ctx, dict[str, Any], Resp], Result]
CHECKS: dict[str, CheckFn] = {}


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def deco(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn

    return deco


def _file_bytes(c: Ctx) -> bytes:
    return c.file.read_bytes()


def _sha12(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:12]


def _def_range(c: Ctx) -> tuple[int, int]:
    """Python の ast で測った、対象シンボル定義の範囲（デコレータを含む）。"""
    tree = ast.parse(_file_bytes(c).decode("utf-8"))
    node: ast.AST = tree
    for part in c.cfg["symbol"].split("."):
        node = next(
            n for n in ast.iter_child_nodes(node) if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and n.name == part
        )
    assert isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
    return start, int(node.end_lineno or node.lineno)


@check("tools_listed")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    names = {t["name"] for t in (r.structured or {}).get("tools", [])}
    need = {"configure_workspace", "get_file_skeleton", "get_symbol_bodies", "edit_symbol_body", "get_diagnostics"}
    missing = sorted(need - names)
    return (not missing, f"{len(names)} tools; missing={missing}")


@check("schemas_closed")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    bad = [t["name"] for t in (r.structured or {}).get("tools", []) if t.get("additionalProperties") is not False]
    return (not bad, f"not closed: {bad}" if bad else "all closed")


@check("workspace_is_fixture")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    data = r.envelope.get("data") or {}
    wss = data.get("workspaces") or []
    active = data.get("active_workspace_id")
    for w in wss:
        root = w.get("root_path") or w.get("root") or ""
        with contextlib.suppress(OSError):
            if os.path.samefile(root, c.ws):
                same_id = w.get("workspace_id") == active
                charset = re.fullmatch(r"[A-Za-z0-9._-]+", str(w.get("workspace_id"))) is not None
                return (same_id and charset, f"id={w.get('workspace_id')} active={active} root={root}")
    return (False, f"fixture copy {c.ws} not among workspaces: {[w.get('root_path') for w in wss]}")


@check("scan_done")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    data = r.envelope.get("data") or {}
    jobs = data.get("jobs") or []
    files = (data.get("cache") or {}).get("files", 0)
    done = bool(jobs) and all(j.get("state") == "done" for j in jobs)
    return (done and files > 0, f"jobs={[(j.get('job_id'), j.get('state')) for j in jobs]} files={files}")


@check("skeleton_header")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["skeleton"]
    data = _file_bytes(c)
    n = len(data.decode("utf-8").splitlines())
    ok = info.get("file_lines") == n and info.get("sha") == _sha12(data)
    return (ok, f"header lines={info.get('file_lines')} sha={info.get('sha')}; disk lines={n} sha={_sha12(data)}")


@check("skeleton_symbol")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["skeleton"]
    want = _def_range(c)
    got = (info.get("sym_start"), info.get("sym_end"))
    urn_ok = str(info.get("urn", "")).endswith("#" + c.cfg["symbol"])
    return (got == want and urn_ok, f"@L{got} vs ast {want}; urn={info.get('urn')}")


@check("skeleton_folds_body")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["skeleton"]
    leaked = "factor" in r.text
    return (bool(info.get("folded")) and not leaked, f"folded={info.get('folded')} body_text_leaked={leaked}")


@check("bodies_match_file")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["bodies"]
    disk = _file_bytes(c).decode("utf-8").split("\n")
    code = info.get("code", [])
    if not code:
        return (False, "no @L lines in the code block")
    nums = [n for n, _ in code]
    seq = nums == list(range(nums[0], nums[0] + len(nums)))
    rng = (nums[0], nums[-1]) == (info.get("start"), info.get("end"))
    same = all(0 < n <= len(disk) and disk[n - 1] == t for n, t in code)
    has_def = any(t.lstrip().startswith("def apply_discount") for _, t in code)
    return (seq and rng and same and has_def, f"range={info.get('start')}-{info.get('end')} lines={len(code)} sequential={seq} equals_disk={same}")


@check("bodies_sha")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["bodies"]
    disk = _sha12(_file_bytes(c))
    sk = c.parsed["skeleton"].get("sha")
    return (info.get("sha") == disk == sk, f"bodies={info.get('sha')} disk={disk} skeleton={sk}")


def _hdr_exact(r: Resp) -> Result:
    h = r.header
    return (h.get("conf") == "exact" and h.get("lsp") == "ready", f"conf={h.get('conf')} lsp={h.get('lsp')}")


@check("diag_exact")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    return _hdr_exact(r)


@check("diag_min_severity")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    floor = SEV_ORDER[MIN_SEV[st["args"]["min_severity"]]]
    ds = parse_diags(r.lines)
    bad = [d for d in ds if SEV_ORDER[d.sev] > floor]
    return (not bad, f"{len(ds)} diagnostics; below floor: {len(bad)}")


@check("diag_positions")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    bad: list[str] = []
    for d in parse_diags(r.lines):
        p = c.ws / d.path
        n = len(p.read_bytes().decode("utf-8").split("\n")) if p.is_file() else 0
        if not (p.is_file() and 1 <= d.line <= n):
            bad.append(f"{d.path}:{d.line}")
    return (not bad, f"invalid positions: {bad}" if bad else "all positions valid")


@check("diag_type_error")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    ds = [d for d in parse_diags(r.lines) if d.sev == "E" and d.code == c.cfg["error_code"]]
    s, e = c.vars.get("sym_start", 0), c.vars.get("sym_end", 0)
    hit = [d for d in ds if d.line == c.cfg["error_line"] and d.source.endswith("pyright")]
    inside = bool(hit) and (not s or s <= hit[0].line <= e)
    return (bool(hit) and inside, f"{[(d.source, d.code, d.line) for d in ds]}; symbol range {s}-{e}")


def _expected_new_lines(c: Ctx, st: dict[str, Any]) -> list[str]:
    code = textwrap.dedent(st["args"]["new_code"]).strip("\n")
    first = c.parsed["_before_lines"][c.vars["sym_start"] - 1]
    indent = first[: len(first) - len(first.lstrip())]
    return [indent + ln if ln.strip() else ln for ln in code.split("\n")]


@check("edit_applied")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["edit"]
    ok = info.get("applied") is True and info.get("op") == "replace_body" and info.get("syntax_check") == "ok"
    return (ok, f"applied={info.get('applied')} op={info.get('op')} syntax_check={info.get('syntax_check')}")


@check("edit_bytes")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["edit"]
    before: list[str] = c.parsed["_before_lines"]
    new_lines = _expected_new_lines(c, st)
    s, e = c.vars["sym_start"], c.vars["sym_end"]
    expected = before[: s - 1] + new_lines + before[e:]
    disk_b = _file_bytes(c)
    disk = disk_b.decode("utf-8").split("\n")
    diff = list(difflib.unified_diff(before, disk, lineterm="", n=0))
    plus = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    minus = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    eol_ok = b"\r" not in disk_b and not disk_b.startswith(b"\xef\xbb\xbf") and disk_b.endswith(b"\n")
    same = disk == expected
    counts = (info.get("plus"), info.get("minus")) == (plus, minus)
    return (same and eol_ok and counts, f"equals_expected={same} lf_nobom_finalnl={eol_ok} reported=(+{info.get('plus')} -{info.get('minus')}) actual=(+{plus} -{minus})")


@check("edit_sha_chain")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["edit"]
    before12 = _sha12(c.parsed["_before_bytes"])
    after12 = _sha12(_file_bytes(c))
    ok = info.get("sha_before") == before12 == c.vars.get("sha") and info.get("sha_after") == after12
    return (ok, f"reported {info.get('sha_before')}->{info.get('sha_after')}; disk {before12}->{after12}")


@check("edit_new_diagnostics")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    s = c.parsed["edit"].get("nd_status")
    return (s in ("complete", "pending", "not_computed"), f"status={s} header lsp={r.header.get('lsp')}")


@check("edit_resolves_error")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["edit"]
    status = info.get("nd_status")
    if status == "pending":
        return (True, "status=pending (follow up with get_diagnostics)")
    got = [d for d in info["resolved"] if d.code == c.cfg["error_code"]]
    return (
        bool(got),
        f"status={status} (+{info.get('nd_plus')} -{info.get('nd_minus')}) resolved={[(d.code, d.line) for d in info['resolved']]} header lsp={r.header.get('lsp')}",
    )


@check("reread_range")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["reread"]
    edit = c.parsed["edit"]
    new_lines = _expected_new_lines(c, st | {"args": c.parsed["_edit_args"]})
    code = [t for _, t in info.get("code", [])]
    rng = (info.get("start"), info.get("end")) == (edit.get("start"), edit.get("end"))
    return (rng and code == new_lines, f"reread @L{info.get('start')}-{info.get('end')} vs edit @L{edit.get('start')}-{edit.get('end')}; code_equals_new_code={code == new_lines}")


@check("diag_zero_errors")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    zero = any(re.match(r"^diagnostics: 0 in\b", ln) for ln in r.lines)
    stale = STALE_BANNER in r.text
    errs = [d for d in parse_diags(r.lines) if d.sev == "E"]
    return (zero and not stale and not errs, f"zero_line={zero} stale_banner={stale} errors={len(errs)}")


@check("diag_checker_alive")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    ds = parse_diags(r.lines)
    alive = [d for d in ds if d.source == "basedpyright"]
    errs = [d for d in ds if d.sev == "E"]
    gone = not [d for d in ds if d.code == c.cfg["error_code"]]
    return (bool(alive) and not errs and gone, f"basedpyright diagnostics={len(alive)} errors={len(errs)} {c.cfg['error_code']}_gone={gone}")


@check("diag_no_new_warnings")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    ws = [d for d in parse_diags(r.lines) if d.sev == "W"]
    extra = [(d.source, d.code, d.line) for d in ws if (d.source, d.code) != ("ruff", "F401")]
    return (not extra, f"warnings={[(d.source, d.code, d.line) for d in ws]}")


@check("edit_diff_vs_diagnostics")
def _(c: Ctx, st: dict[str, Any], r: Resp) -> Result:
    info = c.parsed["edit"]
    if info.get("nd_status") != "complete":
        return (True, f"new_diagnostics status={info.get('nd_status')} (EP-7 applies to complete only)")
    now = {(d.path, d.source, d.code) for d in parse_diags(r.lines)}
    added_missing = [d.code for d in info["added"] if (d.path, d.source, d.code) not in now]
    resolved_present = [d.code for d in info["resolved"] if (d.path, d.source, d.code) in now]
    return (not added_missing and not resolved_present, f"added_missing={added_missing} resolved_still_present={resolved_present}")


# --- 条件待ちの述語 ------------------------------------------------------------------------------


def _w_scan_done(c: Ctx, r: Resp) -> bool:
    return r.ok and CHECKS["scan_done"](c, {}, r)[0]


def _w_type_error_visible(c: Ctx, r: Resp) -> bool:
    if not r.ok or not _hdr_exact(r)[0]:
        return False
    return any(d.sev == "E" and d.code == c.cfg["error_code"] and d.line == c.cfg["error_line"] for d in parse_diags(r.lines))


def _w_no_error_fresh(c: Ctx, r: Resp) -> bool:
    return r.ok and _hdr_exact(r)[0] and CHECKS["diag_zero_errors"](c, {}, r)[0]


def _w_typechecker_alive(c: Ctx, r: Resp) -> bool:
    return r.ok and _hdr_exact(r)[0] and STALE_BANNER not in r.text and any(d.source == "basedpyright" for d in parse_diags(r.lines))


WAITS: dict[str, Callable[[Ctx, Resp], bool]] = {
    "scan_done": _w_scan_done,
    "type_error_visible": _w_type_error_visible,
    "no_error_fresh": _w_no_error_fresh,
    "typechecker_alive": _w_typechecker_alive,
}


# --- 記録 ---------------------------------------------------------------------------------------


class Recorder:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("w", encoding="utf-8", newline="\n")

    def emit(self, obj: dict[str, Any]) -> None:
        self._fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def git_head() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10, check=False)  # noqa: S607
        return cp.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_launch(cache_dir: Path) -> tuple[StdioServerParameters, dict[str, Any]]:
    """``.mcp.json`` の ``lucianke`` の command と args をそのまま使う（``--directory`` が無いパスのときだけ置き換えて記録する）。"""
    spec = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"]["lucianke"]
    args = list(spec.get("args", []))
    substituted: dict[str, str] | None = None
    if "--directory" in args:
        i = args.index("--directory") + 1
        if not Path(args[i]).is_dir():
            substituted = {"from": args[i], "to": str(REPO)}
            args[i] = str(REPO)
    overrides = {"PYTHONUTF8": "1", "LUCIANKE_CACHE_DIR": str(cache_dir)}
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"} | dict(spec.get("env", {})) | overrides
    params = StdioServerParameters(command=spec["command"], args=args, env=env, cwd=str(REPO))
    info = {
        "mcp_json": {"command": spec["command"], "args": spec.get("args", []), "env": spec.get("env", {})},
        "launched": {"command": spec["command"], "args": args, "cwd": str(REPO)},
        "env_overrides": overrides,
        "env_removed": ["VIRTUAL_ENV"],
        "directory_substituted": substituted,
    }
    return params, info


# --- 実行 ---------------------------------------------------------------------------------------


async def call(client: Client, tool: str, args: dict[str, Any], timeout: float) -> Resp:
    t0 = time.perf_counter()
    try:
        res = await asyncio.wait_for(client.call_tool(tool, args), timeout)
    except TimeoutError:
        return Resp(tool, args, True, "", None, (time.perf_counter() - t0) * 1000, rpc_error=f"timeout>{timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001  SDK の例外（JSON-RPC エラーなど）も 1 段の未成立として記録する
        return Resp(tool, args, True, "", None, (time.perf_counter() - t0) * 1000, rpc_error=f"{type(exc).__name__}: {exc}"[:300])
    text = res.content[0].text if res.content and hasattr(res.content[0], "text") else ""
    return Resp(tool, args, bool(res.is_error), text, res.structured_content, (time.perf_counter() - t0) * 1000)


async def list_tools(client: Client) -> Resp:
    t0 = time.perf_counter()
    tools: list[dict[str, Any]] = []
    try:
        cursor: str | None = None
        while True:
            res = await asyncio.wait_for(client.list_tools(cursor=cursor) if cursor else client.list_tools(), 60)
            tools += [
                {"name": t.name, "additionalProperties": (t.input_schema or {}).get("additionalProperties")} for t in res.tools
            ]
            cursor = getattr(res, "next_cursor", None)
            if not cursor:
                break
    except Exception as exc:  # noqa: BLE001
        return Resp("tools/list", {}, True, "", None, (time.perf_counter() - t0) * 1000, rpc_error=f"{type(exc).__name__}: {exc}"[:300])
    text = json.dumps({"tools": [t["name"] for t in tools]}, ensure_ascii=False)
    return Resp("tools/list", {}, False, text, {"tools": tools}, (time.perf_counter() - t0) * 1000)


def digest(r: Resp) -> dict[str, Any]:
    return {"is_error": r.is_error, "rpc_error": r.rpc_error, "rtt_ms": round(r.rtt_ms, 1), "text": r.text,
            "structured": r.structured if r.is_json or r.tool == "tools/list" else None}  # fmt: skip


async def run_stage(client: Client, c: Ctx, st: dict[str, Any], rec: Recorder, started: float, total_deadline: float) -> dict[str, Any]:
    name = st["name"]
    t_stage = time.perf_counter()
    args = subst(st.get("args", {}), c.vars)
    st = {**st, "args": args}
    if name == "edit":
        c.parsed["_before_bytes"] = _file_bytes(c)
        c.parsed["_before_lines"] = c.parsed["_before_bytes"].decode("utf-8").split("\n")
        c.parsed["_edit_args"] = args
    polls: list[dict[str, Any]] = []
    waited_ok: bool | None = None
    if st.get("call") == "tools/list":
        r = await list_tools(client)
    else:
        r = await call(client, st["tool"], args, c.cfg["call_timeout_s"])
        wait = st.get("wait_until")
        if wait:
            pred = WAITS[wait]
            limit = min(time.perf_counter() + float(st.get("timeout_s", 60)), total_deadline)
            interval = float(st.get("interval_s", 0.5 if wait == "scan_done" else 1.0))
            same_err = 0
            n = 0
            while True:
                n += 1
                polls.append({"n": n, "t_s": round(time.perf_counter() - t_stage, 1), "rtt_ms": round(r.rtt_ms, 1),
                              "head": r.text.split("\n", 1)[0][:120], "tail": r.text.rstrip().split("\n")[-1][:160],
                              "error": r.error_code or None})  # fmt: skip
                if pred(c, r):
                    waited_ok = True
                    break
                same_err = same_err + 1 if (not r.ok and r.error_code) else 0
                if time.perf_counter() >= limit or same_err >= 20:
                    waited_ok = False
                    break
                await asyncio.sleep(interval)
                r = await call(client, st["tool"], args, c.cfg["call_timeout_s"])
    c.out[name] = r
    # 取り出し
    extracted: dict[str, Any] = {}
    if r.ok and not r.is_json and r.tool in EXTRACT:
        extracted = EXTRACT[r.tool](c, r)
        c.parsed[name] = extracted
        after_extract(c, name, extracted)
    # 書き込みの変化
    now = make_manifest(c.ws)
    prev = c.manifest
    changed = {k for k in prev if now.get(k) != prev[k]}
    created = {k for k in now if k not in prev}
    c.created |= created
    c.manifest = now
    writes = {str(subst(w, c.vars)) for w in st.get("writes", [])}
    results: list[dict[str, Any]] = []

    def add(cid: str, res: Result) -> None:
        meta = c.checks.get(cid, {})
        results.append({"id": cid, "level": meta.get("level", "must"), "spec": meta.get("spec", ""), "ok": bool(res[0]), "detail": res[1]})

    add("tool_ok", (r.ok, "ok" if r.ok else f"error {r.error_code or '?'}: {r.text.splitlines()[1][:160] if len(r.text.splitlines()) > 1 else (r.rpc_error or '')}"))
    add("writes_as_declared", (changed <= writes, f"changed={sorted(changed)} created={sorted(created)} declared={sorted(writes)}"))
    if waited_ok is not None:
        add("wait_satisfied", (waited_ok, f"{st['wait_until']}: {len(polls)} polls, {time.perf_counter() - t_stage:.1f}s"))
    if r.ok:
        if not r.is_json and r.tool != "tools/list":
            problems = g.validate(r.text)
            add("compact_grammar", (not problems, "; ".join(problems[:3]) if problems else "valid"))
        for cid in st.get("checks", []):
            try:
                add(cid, CHECKS[cid](c, st, r))
            except Exception as exc:  # noqa: BLE001  事後条件の評価側の失敗も「満たせなかった」として記録する
                add(cid, (False, f"check raised {type(exc).__name__}: {exc}"))
    hard_failed = [x["id"] for x in results if x["level"] == "must" and not x["ok"]]
    soft_failed = [x["id"] for x in results if x["level"] != "must" and not x["ok"]]
    status = "pass" if not hard_failed else "unmet"
    rec.emit({
        "type": "stage", "name": name, "status": status, "tool": r.tool, "request": args,
        "response": digest(r), "polls": polls if len(polls) <= 40 else polls[:5] + polls[-30:], "poll_count": len(polls),
        "extracted": {k: v for k, v in extracted.items() if k != "code"} if extracted else None,
        "checks": results, "elapsed_s": round(time.perf_counter() - t_stage, 2), "t_since_start_s": round(time.perf_counter() - started, 2),
    })  # fmt: skip
    return {"name": name, "status": status, "hard_failed": hard_failed, "soft_failed": soft_failed,
            "error_code": "" if r.ok else r.error_code, "results": results}  # fmt: skip


async def run(script: Path = SCRIPT, records_dir: Path = RECORDS, via: str | None = None) -> dict[str, Any]:
    """台本を 1 回走らせ、記録を書いて、結果（``ok``・段ごとの状態）を返す。"""
    t_start = time.perf_counter()
    doc = tomllib.loads(script.read_text(encoding="utf-8"))
    cfg: dict[str, Any] = doc["session"]
    if via:
        cfg["workspace_via"] = via
    stages: list[dict[str, Any]] = list(doc["stage"])
    if cfg["workspace_via"] == "set_roots":
        stages.insert(1, {"name": "set_roots", "tool": "configure_workspace",
                          "args": {"action": "set_roots", "root_paths": ["$ws"], "format": "json"}, "checks": []})  # fmt: skip
        for s in stages:
            if s["name"] == "workspace":
                s["needs"] = ["set_roots"]
    rec = Recorder(records_dir / f"{_results.now_stamp()}-{_results.os_name()}.jsonl")
    total_deadline = time.perf_counter() + float(cfg["total_timeout_s"])
    outcomes: list[dict[str, Any]] = []
    harness_error: str | None = None
    leftover: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="lucianke-m16-", ignore_cleanup_errors=True) as tmp_s:
        tmp = Path(tmp_s)
        ws = tmp / "ws"
        import shutil

        shutil.copytree(REPO / cfg["fixture"], ws)
        params, launch = build_launch(tmp / "cache")
        c = Ctx(cfg=cfg, checks=doc.get("check", {}), ws=ws, vars={"file": cfg["file"], "symbol": cfg["symbol"], "ws": str(ws)})
        c.manifest = make_manifest(ws)
        rec.emit({
            "type": "session", "name": cfg["name"], "spec": cfg["spec"], "started_at": _results.iso_now(), "os": _results.os_name(),
            "platform": sys.platform, "python": sys.version.split()[0], "mcp_sdk": importlib.metadata.version("mcp"),
            "git_head": git_head(), "workspace_via": cfg["workspace_via"], "fixture": cfg["fixture"], "workspace_copy": str(ws),
            "launch": launch, "script": script.name, "fixture_files": len(c.manifest),
        })  # fmt: skip
        before_children = set(_stdio.descendants(os.getpid(), _stdio.snapshot()))

        async def roots_cb(_ctx: Any) -> mt.ListRootsResult:
            return mt.ListRootsResult(roots=[mt.Root(uri=ws.resolve().as_uri(), name=ws.name)])

        kwargs: dict[str, Any] = {"mode": "legacy", "cache": None}
        if cfg["workspace_via"] == "roots":
            kwargs["list_roots_callback"] = roots_cb
        status: dict[str, str] = {}
        try:
            async with Client(params, **kwargs) as client:
                info = client.server_info
                rec.emit({"type": "server", "name": getattr(info, "name", None), "version": getattr(info, "version", None),
                          "init_s": round(time.perf_counter() - t_start, 1)})  # fmt: skip
                for st in stages:
                    needs = [n for n in st.get("needs", []) if status.get(n) != "pass"]
                    if needs:
                        status[st["name"]] = "blocked"
                        rec.emit({"type": "stage", "name": st["name"], "status": "blocked", "blocked_by": needs})
                        outcomes.append({"name": st["name"], "status": "blocked", "hard_failed": [], "soft_failed": [], "error_code": "", "blocked_by": needs})
                        continue
                    out = await run_stage(client, c, st, rec, t_start, total_deadline)
                    status[st["name"]] = out["status"]
                    outcomes.append(out)
        except BaseException as exc:  # noqa: BLE001  接続できない・途中で切れた等。記録して、あとは片付け
            harness_error = f"{type(exc).__name__}: {exc}"[:500]
            for st in stages:
                if st["name"] not in status:
                    outcomes.append({"name": st["name"], "status": "unmet", "hard_failed": ["not_run"], "soft_failed": [], "error_code": "harness", "blocked_by": []})
        # 自分が起こした子孫（サーバーと LSP）が消えるのを待ち、残ったら落とす
        deadline = time.perf_counter() + 5.0
        while True:
            left = {p: e for p, e in _stdio.descendants(os.getpid(), _stdio.snapshot()).items() if p not in before_children}
            if not left or time.perf_counter() > deadline:
                break
            await asyncio.sleep(0.2)
        if left:
            leftover = [{"pid": p, "exe": e} for p, e in left.items()]
            _stdio.kill_pids(list(left))
    unmet = [o for o in outcomes if o["status"] != "pass"]
    ok = not unmet and harness_error is None
    summary = {
        "type": "summary", "ok": ok, "stages": {o["name"]: o["status"] for o in outcomes},
        "unmet": [{"stage": o["name"], "status": o["status"], "error_code": o.get("error_code", ""), "failed_checks": o["hard_failed"]} for o in unmet],
        "soft_findings": {o["name"]: o["soft_failed"] for o in outcomes if o["soft_failed"]},
        "created_files_in_workspace": sorted(c.created) if "c" in locals() else [],
        "leftover_processes_after_exit": leftover, "harness_error": harness_error,
        "elapsed_s": round(time.perf_counter() - t_start, 1), "finished_at": _results.iso_now(),
    }  # fmt: skip
    rec.emit(summary)
    rec.close()
    return {**summary, "record": str(rec.path), "outcomes": outcomes}


def report(result: dict[str, Any]) -> str:
    lines = [f"M1-6 session: {'PASS' if result['ok'] else 'FAIL'} in {result['elapsed_s']}s  record={ascii_safe(result['record'])}"]
    for o in result["outcomes"]:
        lines.append(f"  {o['name']:<12} {o['status']}" + (f"  blocked_by={o['blocked_by']}" if o.get("blocked_by") else ""))
        for x in o.get("results", []):
            if not x["ok"]:
                lines.append(f"      [{x['level']}] {x['id']}: {ascii_safe(x['detail'])[:300]}")
    if result["harness_error"]:
        lines.append(f"  harness_error: {ascii_safe(result['harness_error'])}")
    if result["leftover_processes_after_exit"]:
        lines.append(f"  leftover processes killed: {result['leftover_processes_after_exit']}")
    return "\n".join(lines)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # 応答の日本語や → を cp932 のパイプへ出して化けさせない（BUG-0038）
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--via", choices=["roots", "set_roots"], default=None, help="how the workspace root is pointed (default: session.toml)")
    ap.add_argument("--records-dir", type=Path, default=RECORDS)
    ap.add_argument("--script", type=Path, default=SCRIPT)
    ns = ap.parse_args()
    try:
        result = asyncio.run(run(ns.script, ns.records_dir, ns.via))
    except Exception as exc:  # noqa: BLE001
        print(f"harness failure: {ascii_safe(type(exc).__name__)}: {ascii_safe(exc)}", file=sys.stderr)
        return 2
    print(report(result))
    return 0 if result["ok"] else (2 if result["harness_error"] else 1)


if __name__ == "__main__":
    sys.exit(main())
