"""編集の構文妥当性の計測台本（仕様書 §14.5、NFR-SAFE-1、EP-6、§10.6 のジャーナル復元）。

F-PY（httpx）と F-TS（ky）のシンボルをシード固定で選び、ツール T-10 ``edit_symbol_body`` で次の操作を行う（既定の規模 1,000 件）::

    恒等 replace_body 300 / 変換つき replace_body 400（ローカル変数の改名・文の末尾追加・早期 return の挿入）
    insert_member 150 / insert_after 100 / delete_symbol 50

各操作のあと、Tree-sitter でファイルを解析してエラーノード数が操作前以下で、LSP が新しい構文エラーを報告しないことを「成功」とする
（F-PY は basedpyright の「コード無しの error」、F-TS は tsserver の ``syntacticDiagnosticsSync``。取れなければ「LSP 未確認」と記録）。
恒等編集はバイト単位で同一であること（EP-6）を別に 100% 要求する。ジャーナルからの復元（``edit.writer.rollback``）は 50 件で確かめる。

編集は **プロセス内** に組んだパイプライン（``tests/contract/test_T10_edit_body.py`` と同じ結線）で行い、速さを取る。そのうえで
stdio の実サーバーに同じ操作の標本（既定 20 件）を投げ、結果が同じになることを確かめる。操作ごとに、触ったファイルを元のバイト列へ戻す
（作業ツリーは ``bench/.cache/<name>/`` の複製で、原本には触らない）。

まだ実装されていない操作（``E_UNSUPPORTED`` + ``details.reason == "operation_not_implemented"``）は「skip: 未実装」として数え、合否から外す。

使い方::

    uv run python bench/run_edit_safety.py                 # 既定（1,000 件 + stdio 標本 20 件）
    uv run python bench/run_edit_safety.py --scale 0.05    # 煙試験（規模を縮める）
    uv run python bench/run_edit_safety.py --no-lsp --no-stdio

結果: ``bench/results/<日時>-<OS>-edit-safety.json``。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import difflib
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
import _results as R  # noqa: E402
import _stdio as S  # noqa: E402
import fetch_fixtures as FF  # noqa: E402

from lucianke.app import build_services  # noqa: E402
from lucianke.core.model import Kind  # noqa: E402
from lucianke.core.procs import ProcessRegistry  # noqa: E402
from lucianke.edit.lock import WorkspaceLocks  # noqa: E402
from lucianke.edit.writer import rollback  # noqa: E402
from lucianke.index.writer import IndexWriter  # noqa: E402
from lucianke.lsp.pool import LspPool  # noqa: E402
from lucianke.mcp.pipeline import Pipeline  # noqa: E402
from lucianke.mcp.registry import ToolRegistry  # noqa: E402
from lucianke.syntax.boundaries import definition_range  # noqa: E402
from lucianke.syntax.parsers import parse_source  # noqa: E402
from lucianke.syntax.support import get_support  # noqa: E402
from lucianke.syntax.symbols import extract, load_query_source  # noqa: E402
from lucianke.tools import register_tools  # noqa: E402
from lucianke.tools._services import ToolServices  # noqa: E402
from lucianke.workspace.types import PythonEnv  # noqa: E402

REPO = R.REPO
DEFAULT_SEED = 20261029
THRESHOLD = 0.999  # NFR-SAFE-1
RESTORE_CHECKS = 50  # §14.5-4
STDIO_SAMPLE = 20
DIFF_CAP = 2000  # 失敗の差分の最大文字数
LSP_TIMEOUT_MS = 8000

# 既定の規模（§14.5-1）。キーは操作の区分
PLAN_COUNTS = {"identity": 300, "transform": 400, "insert_member": 150, "insert_after": 100, "delete_symbol": 50}
OPERATION_OF = {
    "identity": "replace_body",
    "transform": "replace_body",
    "insert_member": "insert_member",
    "insert_after": "insert_after",
    "delete_symbol": "delete_symbol",
}
TRANSFORMS = ("rename_local", "append_statement", "early_return")

LANG_OF_EXT = {".py": "python", ".ts": "typescript", ".tsx": "tsx"}
DEFINITION_KINDS = frozenset(
    {Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR, Kind.CLASS, Kind.INTERFACE, Kind.TYPE, Kind.PROPERTY}
)
FUNCTION_KINDS = frozenset({Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR, Kind.PROPERTY})
CONTAINER_KINDS = frozenset({Kind.CLASS, Kind.INTERFACE})


def _server_argv() -> list[str]:
    """サーバーの起動コマンド（``_stdio`` の版によって ``server_argv`` か ``uv_argv``）。"""
    fn = getattr(S, "server_argv", None)
    return fn() if fn is not None else S.uv_argv(REPO)  # type: ignore[attr-defined]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Tree-sitter ----------------------------------------------------------------------------------------------


def error_node_count(data: bytes, language_id: str) -> int:
    """ERROR / MISSING ノードの数（Lucianke の ``error_ranges`` の併合に頼らず、木を自分で歩いて数える）。"""
    parsed, _ = parse_source(data, language_id, sha256(data))
    root = parsed.tree.root_node
    if not root.has_error:
        return 0
    count = 0
    stack = [root]
    while stack:
        n = stack.pop()
        if n.is_error or n.is_missing:
            count += 1
        for c in n.children:
            if c.has_error or c.is_error or c.is_missing:
                stack.append(c)
    return count


# --- 母集団 ---------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class FileState:
    rel: str
    lang: str
    data: bytes
    parsed: Any
    errors_before: int
    symbols: list[Any]


@dataclass(slots=True)
class Cand:
    """操作の候補になるシンボル 1 件と、そこから作れる新コード。"""

    fx: str
    rel: str
    lang: str
    urn: str
    name: str
    kind: str
    line: int  # 1 始まり
    node: Any
    sym: Any
    file: FileState
    context: str = "top"  # class / interface / top（親の種類）
    transforms: tuple[str, ...] = ()
    parent_ok_for_delete: bool = True


def _node_of(f: FileState, sym: Any) -> Any:
    li = f.parsed.line_index
    start = li.position_to_offset(sym.range.start).value
    end = li.position_to_offset(sym.range.end).value
    return f.parsed.tree.root_node.descendant_for_byte_range(start, end)


def _walk(node: Any):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _context_of(node: Any, lang: str) -> str:
    p = node.parent
    if lang == "python":
        if p is not None and p.type == "decorated_definition":
            p = p.parent
        if p is not None and p.type == "block" and p.parent is not None and p.parent.type == "class_definition":
            return "class"
        return "top"
    while p is not None and p.type in ("export_statement", "decorator"):
        p = p.parent
    if p is None:
        return "top"
    if p.type == "class_body":
        return "class"
    if p.type in ("interface_body", "object_type"):
        return "interface"
    return "top"


def _parent_has_other_members(node: Any, lang: str) -> bool:
    """Python はサーバーが ``pass`` を補わないので、親の本体が空になる削除は候補から外す。"""
    if lang != "python":
        return True
    n = node
    if n.parent is not None and n.parent.type == "decorated_definition":
        n = n.parent
    p = n.parent
    if p is None:
        return True
    siblings = [c for c in p.named_children if c.type != "comment"]
    return len(siblings) >= 2


def scan_fixture(fx: str, root: Path, dirs: list[str], workspace_id: str) -> tuple[dict[str, FileState], list[Cand]]:
    files: dict[str, FileState] = {}
    cands: list[Cand] = []
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        for sub in R.tier1_files(base, exts=tuple(LANG_OF_EXT)):
            rel = f"{d}/{sub}"
            lang = LANG_OF_EXT.get(Path(rel).suffix)
            if lang is None:
                continue
            data = (root / rel).read_bytes()
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            parsed, timed_out = parse_source(data, lang, sha256(data))
            spec = get_support(lang)
            if timed_out or spec is None:
                continue
            syms = extract(
                parsed, language_id=lang, language=spec.grammar(), query_source=load_query_source(lang),
                workspace_id=workspace_id, rel=rel,
            )  # fmt: skip
            f = FileState(rel, lang, data, parsed, error_node_count(data, lang), list(syms))
            files[rel] = f
            if f.errors_before:  # 操作前から壊れているファイルは母集団に入れない（「操作前以下」の比較が曖昧になる）
                continue
            li = parsed.line_index
            for s in syms:
                if s.kind not in DEFINITION_KINDS:
                    continue
                node = _node_of(f, s)
                if node is None:
                    continue
                rng = definition_range(node, parsed, language_id=lang)
                first = li.line_start(rng.start.line)
                if data[first : li.position_to_offset(rng.start).value].strip():  # 同じ行に先行するコードがある
                    continue
                c = Cand(fx, rel, lang, str(s.urn), s.qualified_name, s.kind.value, s.range.start.line + 1, node, s, f)
                c.context = _context_of(node, lang)
                c.parent_ok_for_delete = _parent_has_other_members(node, lang)
                if s.kind in FUNCTION_KINDS:
                    c.transforms = tuple(t for t in TRANSFORMS if make_transform(c, t) is not None)
                cands.append(c)
    return files, cands


# --- 新コードの作り方 -----------------------------------------------------------------------------------------


def _def_slice(c: Cand) -> tuple[int, bytes, Any]:
    """定義の範囲の **行頭から行末まで** のバイト列（先頭行のインデントを含む。恒等編集の ``new_code`` の元）。"""
    f = c.file
    li = f.parsed.line_index
    rng = definition_range(c.node, f.parsed, language_id=c.lang)
    start = li.line_start(rng.start.line)
    end = li.line_end(rng.end.line)
    return start, f.data[start:end], rng


def identity_code(c: Cand) -> str:
    _, text, _ = _def_slice(c)
    return text.decode("utf-8") + "\n"


def _find_function(c: Cand) -> tuple[Any, Any] | None:
    """(関数ノード, 本体のブロック)。本体がブロックでなければ ``None``。"""
    n = c.node
    if c.lang == "python":
        if n.type == "decorated_definition":
            n = n.child_by_field_name("definition")
        if n is not None and n.type == "function_definition":
            body = n.child_by_field_name("body")
            return (n, body) if body is not None and body.type == "block" else None
        return None
    types = {"function_declaration", "generator_function_declaration", "method_definition", "function_expression",
             "arrow_function", "function"}  # fmt: skip
    for x in _walk(n):
        if x.type in types:
            body = x.child_by_field_name("body")
            return (x, body) if body is not None and body.type == "statement_block" else None
    return None


def _line_prefix(c: Cand, node: Any) -> bytes:
    li = c.file.parsed.line_index
    return c.file.data[li.line_start(node.start_point[0]) : node.start_byte]


def _indent_of(prefix: bytes) -> str:
    return prefix.decode("utf-8")


def _unit_for(indent: str, data: bytes = b"") -> str:
    """段の単位。インデントか、ファイル全体の多数派から決める（タブのファイルにスペースを混ぜない）。"""
    if "\t" in indent or (not indent and data.count(b"\n\t") > data.count(b"\n  ")):
        return "\t"
    return "    " if "    " in indent or not indent else "  "


def make_transform(c: Cand, kind: str) -> str | None:
    """変換つきの ``new_code``。適用できなければ ``None``。変更は木のノード単位（正規表現ではない）。"""
    found = _find_function(c)
    if found is None:
        return None
    fn, body = found
    f = c.file
    data = f.data
    li = f.parsed.line_index
    base, text, _ = _def_slice(c)
    edits: list[tuple[int, int, bytes]] = []  # (絶対オフセット, 消す長さ, 入れるバイト列)
    py = c.lang == "python"
    stmts = [x for x in body.named_children if x.type != "comment"]
    if not stmts:
        return None

    if kind == "rename_local":
        target = None
        for st in stmts:
            if py and st.type == "expression_statement" and st.named_children and st.named_children[0].type == "assignment":
                left = st.named_children[0].child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    target = left
                    break
            if not py and st.type in ("lexical_declaration", "variable_declaration"):
                for d in st.named_children:
                    nm = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                    if nm is not None and nm.type == "identifier":
                        target = nm
                        break
                if target is not None:
                    break
        if target is None:
            return None
        old = data[target.start_byte : target.end_byte]
        new = old + b"_lk"
        if new in text:
            return None
        for x in _walk(fn):
            if x.type != "identifier" or data[x.start_byte : x.end_byte] != old:
                continue
            p = x.parent
            if py and p is not None and p.type == "attribute" and p.child_by_field_name("attribute") == x:
                continue
            if py and p is not None and p.type == "keyword_argument" and p.child_by_field_name("name") == x:
                continue
            edits.append((x.start_byte, len(old), new))
        if not edits:
            return None
    elif kind == "append_statement":
        if py:
            first = stmts[0]
            prefix = _line_prefix(c, first)
            if prefix.strip():
                return None
            indent = _indent_of(prefix)
            row = body.end_point[0]
            at = li.line_end(row)
            edits.append((at, 0, f"\n{indent}pass".encode()))
        else:
            close = body.end_byte - 1
            if data[close : close + 1] != b"}":
                return None
            line_pre = data[li.line_start(body.end_point[0]) : close]
            if not line_pre.strip():
                indent = _indent_of(line_pre)
                edits.append((li.line_start(body.end_point[0]), 0, f"{indent}{_unit_for(indent, data)}void 0;\n".encode()))
            else:
                edits.append((close, 0, b" void 0; "))
    elif kind == "early_return":
        if py:
            first = stmts[0]
            is_doc = (
                first.type == "expression_statement"
                and first.named_children
                and first.named_children[0].type == "string"
            )
            if is_doc and len(stmts) < 2:
                return None
            target = stmts[1] if is_doc else first
            prefix = _line_prefix(c, target)
            if prefix.strip():
                return None
            indent = _indent_of(prefix)
            code = f"{indent}if False:\n{indent}{_unit_for(indent, data)}return None\n"
            edits.append((li.line_start(target.start_point[0]), 0, code.encode()))
        else:
            first = stmts[0]
            prefix = _line_prefix(c, first)
            if not prefix.strip() and first.start_point[0] > body.start_point[0]:
                indent = _indent_of(prefix)
                code = f"{indent}if (false) {{\n{indent}{_unit_for(indent, data)}return;\n{indent}}}\n"
                edits.append((li.line_start(first.start_point[0]), 0, code.encode()))
            else:
                edits.append((body.start_byte + 1, 0, b" if (false) { return; } "))
    else:
        raise ValueError(kind)

    out = bytearray(text)
    for off, ln, ins in sorted(edits, key=lambda e: e[0], reverse=True):
        rel = off - base
        if rel < 0 or rel + ln > len(out):
            return None
        out[rel : rel + ln] = ins
    new_text = bytes(out)
    if new_text == text:
        return None
    return new_text.decode("utf-8") + "\n"


def insert_code(c: Cand, op: str) -> str:
    """``insert_member`` / ``insert_after`` で差し込む断片（それ自体が構文として完結している）。"""
    member = op == "insert_member" or c.context in ("class", "interface")
    if c.lang == "python":
        if member and c.context != "top" or op == "insert_member":
            return "def _lk_bench_inserted(self) -> None:\n    return None\n"
        return "def _lk_bench_inserted() -> None:\n    return None\n"
    if op == "insert_member":
        if c.kind == Kind.INTERFACE.value:
            return "lkBenchInserted(): void;\n"
        return "lkBenchInserted(): void {\n  return;\n}\n"
    if c.context == "interface":
        return "lkBenchInserted(): void;\n"
    if c.context == "class":
        return "lkBenchInserted(): void {\n  return;\n}\n"
    return "function lkBenchInserted(): void {\n  return;\n}\n"


# --- 操作の計画 -----------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Op:
    idx: int
    category: str
    operation: str
    cand: Cand
    transform: str | None
    args: dict[str, Any]


def eligible(cat: str, c: Cand) -> bool:
    if cat == "identity":
        return True
    if cat == "transform":
        return bool(c.transforms)
    if cat == "insert_member":
        return c.kind in (Kind.CLASS.value, Kind.INTERFACE.value)
    if cat == "insert_after":
        return True
    if cat == "delete_symbol":
        return c.parent_ok_for_delete
    return False


def build_plan(cands: list[Cand], counts: dict[str, int], seed: int) -> tuple[list[Op], dict[str, Any]]:
    """カテゴリごとに F-PY / F-TS を半々で選ぶ（足りない言語は他方で埋める）。シード固定で決定的。"""
    cands = sorted(cands, key=lambda c: (c.fx, c.rel, c.line, c.urn))
    used: set[str] = set()
    chosen: dict[str, list[Cand]] = {}
    info: dict[str, Any] = {}
    for cat, n in counts.items():
        pool = [c for c in cands if eligible(cat, c)]
        by_fx: dict[str, list[Cand]] = {}
        for c in pool:
            by_fx.setdefault(c.fx, []).append(c)
        rng = random.Random(f"{seed}:{cat}")
        picked: list[Cand] = []
        fxs = sorted(by_fx)
        quota = {fx: n // len(fxs) + (1 if i < n % len(fxs) else 0) for i, fx in enumerate(fxs)} if fxs else {}
        left = n
        for fx in fxs:
            fresh = [c for c in by_fx[fx] if c.urn not in used]
            stale = [c for c in by_fx[fx] if c.urn in used]
            rng.shuffle(fresh)
            rng.shuffle(stale)
            take = (fresh + stale)[: quota[fx]]
            picked += take
            left -= len(take)
        if left > 0:  # 足りない分は残りから
            rest = [c for c in sorted(pool, key=lambda c: (c.fx, c.rel, c.line)) if c not in picked]
            rng.shuffle(rest)
            picked += rest[:left]
        used.update(c.urn for c in picked)
        chosen[cat] = picked
        info[cat] = {
            "requested": n, "selected": len(picked), "eligible": len(pool),
            "by_fixture": {fx: sum(1 for c in picked if c.fx == fx) for fx in fxs},
        }  # fmt: skip
    ops: list[Op] = []
    for cat, picked in chosen.items():
        for i, c in enumerate(picked):
            operation = OPERATION_OF[cat]
            transform: str | None = None
            args: dict[str, Any] = {"target": {"urn": c.urn}, "operation": operation}
            if cat == "identity":
                args["new_code"] = identity_code(c)
            elif cat == "transform":
                order = list(TRANSFORMS)
                start = i % len(order)
                transform = next(t for t in order[start:] + order[:start] if t in c.transforms)
                code = make_transform(c, transform)
                assert code is not None
                args["new_code"] = code
            elif cat in ("insert_member", "insert_after"):
                args["new_code"] = insert_code(c, operation)
                if cat == "insert_member":
                    args["position"] = "end"
            ops.append(Op(0, cat, operation, c, transform, args))
    random.Random(f"{seed}:order").shuffle(ops)  # 区分を混ぜる（復元検査の 50 件が区分に偏らない）
    for i, op in enumerate(ops):
        op.idx = i
    return ops, info


# --- LSP ------------------------------------------------------------------------------------------------------


class PyLsp:
    """basedpyright に直接つなぎ、構文エラー（``code`` の無い error）の数を数える。"""

    def __init__(self, root: Path, ws: Any) -> None:
        self.root = root
        self.ws = dataclasses.replace(ws, python_env=PythonEnv(interpreter=sys.executable))
        self.registry = ProcessRegistry()
        self.pool = LspPool(self.registry, ready_fallback_s=8.0, grace_ms=500)
        self.handle: Any = None
        self.baseline: dict[str, int] = {}
        self.name = "basedpyright"
        self.start_ms: float | None = None

    async def start(self, timeout_ms: int = 180_000) -> bool:
        t0 = time.perf_counter()
        ok = await self.pool.wait_ready("python", self.ws, timeout_ms)
        if not ok:
            return False
        self.handle = self.pool.get("python", self.ws, start_if_needed=False)
        self.start_ms = round((time.perf_counter() - t0) * 1000)
        if self.handle is not None and self.handle.server_name:
            self.name = self.handle.server_name
        return self.handle is not None

    @staticmethod
    def syntax_errors(diags: Any) -> list[dict[str, Any]]:
        out = []
        for d in diags:
            if d.get("severity") in (1, "error", "Error") and not d.get("code"):
                out.append(d)
        return out

    async def _diagnose(self, rel: str, text: str) -> list[dict[str, Any]] | None:
        h = self.handle
        h.touch()
        version = h.documents.sync(rel, text, "python")
        sent_at = h.documents.sent_at_of(rel)
        got = await h.diagnostics.wait_for(
            rel, min_version=version, sent_at=sent_at if sent_at is not None else 0.0, timeout_ms=LSP_TIMEOUT_MS,
            settle_ms=150,
        )  # fmt: skip
        if not got:
            return None
        entry = h.diagnostics.get(rel)
        return None if entry is None else list(entry.diagnostics)

    async def check(self, f: FileState, after: bytes) -> dict[str, Any]:
        """``{"state": ok|new_syntax_errors|unverified, "before": n, "after": n, "new": [...]}``。"""
        rel = f.rel
        try:
            if rel not in self.baseline:
                diags = await self._diagnose(rel, f.data.decode("utf-8"))
                if diags is None:
                    return {"state": "unverified", "reason": "baseline_timeout"}
                self.baseline[rel] = len(self.syntax_errors(diags))
            diags = await self._diagnose(rel, after.decode("utf-8"))
            if diags is None:
                return {"state": "unverified", "reason": "timeout"}
            errs = self.syntax_errors(diags)
            res: dict[str, Any] = {"before": self.baseline[rel], "after": len(errs)}
            if len(errs) > self.baseline[rel]:
                res["state"] = "new_syntax_errors"
                res["new"] = [
                    {"line": e.get("range", {}).get("start", {}).get("line", -1) + 1, "message": e.get("message", "")[:160]}
                    for e in errs[:5]
                ]
            else:
                res["state"] = "ok"
            return res
        except Exception as exc:  # noqa: BLE001  LSP の不調は「未確認」にして計測を続ける
            return {"state": "unverified", "reason": f"{type(exc).__name__}: {exc}"}
        finally:
            with contextlib.suppress(Exception):
                self.handle.documents.sync(rel, f.data.decode("utf-8"), "python")  # 元に戻す（待たない）

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.pool.shutdown_all(500)
        with contextlib.suppress(Exception):
            await self.registry.terminate_all(500, close=True)


class TsServer:
    """``tsserver``（TypeScript 同梱）の ``syntacticDiagnosticsSync``。typescript-language-server / vtsls が無い環境の代わり。"""

    def __init__(self, tsserver_js: Path, root: Path) -> None:
        self.js = tsserver_js
        self.root = root
        self.proc: asyncio.subprocess.Process | None = None
        self.seq = 0
        self.baseline: dict[str, int] = {}
        self.name = "tsserver"
        self.version: str | None = None
        self._buf = b""

    async def start(self) -> bool:
        node = shutil.which("node")
        if not node or not self.js.is_file():
            return False
        self.proc = await asyncio.create_subprocess_exec(
            node, str(self.js), "--useInferredProjectPerProjectRoot",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=1 << 26,
        )  # fmt: skip
        try:
            pkg = json.loads((self.js.parent.parent / "package.json").read_text(encoding="utf-8"))
            self.version = pkg.get("version")
        except (OSError, ValueError):
            pass
        res = await self._request("open", {"file": str(self.root / "__probe__.ts"), "fileContent": "const a = 1;\n",
                                           "scriptKindName": "TS", "projectRootPath": str(self.root)})  # fmt: skip
        return bool(res and res.get("success"))

    async def _read_message(self) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            head, sep, rest = self._buf.partition(b"\r\n\r\n")
            if sep and head.lower().startswith(b"content-length:"):
                n = int(head.split(b":", 1)[1].strip())
                if len(rest) >= n:
                    body, self._buf = rest[:n], rest[n:]
                    return json.loads(body.decode("utf-8"))
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                raise ConnectionError("tsserver closed")
            self._buf += chunk

    async def _request(self, command: str, arguments: dict[str, Any], timeout: float = 30.0) -> dict[str, Any] | None:
        assert self.proc is not None and self.proc.stdin is not None
        self.seq += 1
        seq = self.seq
        self.proc.stdin.write((json.dumps({"seq": seq, "type": "request", "command": command, "arguments": arguments}) + "\n").encode())
        await self.proc.stdin.drain()

        async def wait() -> dict[str, Any]:
            while True:
                msg = await self._read_message()
                if msg.get("type") == "response" and msg.get("request_seq") == seq:
                    return msg

        return await asyncio.wait_for(wait(), timeout)

    async def _syntax_count(self, rel: str, text: str) -> tuple[int, list[dict[str, Any]]]:
        path = str(self.root / rel)
        kind = "TSX" if rel.endswith(".tsx") else "TS"
        await self._request("open", {"file": path, "fileContent": text, "scriptKindName": kind, "projectRootPath": str(self.root)})
        try:
            res = await self._request("syntacticDiagnosticsSync", {"file": path})
        finally:
            with contextlib.suppress(Exception):
                await self._request("close", {"file": path})
        body = (res or {}).get("body") or []
        return len(body), body

    async def check(self, f: FileState, after: bytes) -> dict[str, Any]:
        try:
            if f.rel not in self.baseline:
                self.baseline[f.rel] = (await self._syntax_count(f.rel, f.data.decode("utf-8")))[0]
            n, body = await self._syntax_count(f.rel, after.decode("utf-8"))
            res: dict[str, Any] = {"before": self.baseline[f.rel], "after": n}
            if n > self.baseline[f.rel]:
                res["state"] = "new_syntax_errors"
                res["new"] = [{"line": d.get("start", {}).get("line", -1), "message": str(d.get("text", ""))[:160]} for d in body[:5]]
            else:
                res["state"] = "ok"
            return res
        except Exception as exc:  # noqa: BLE001
            return {"state": "unverified", "reason": f"{type(exc).__name__}: {exc}"}

    async def stop(self) -> None:
        p = self.proc
        if p is None:
            return
        with contextlib.suppress(Exception):
            p.stdin.close()  # type: ignore[union-attr]
        try:
            await asyncio.wait_for(p.wait(), 3.0)
        except (TimeoutError, Exception):  # noqa: BLE001
            with contextlib.suppress(Exception):
                p.kill()


# --- プロセス内の実行環境 ---------------------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class EditServices(ToolServices):
    """``ToolServices`` に編集の依存を足したもの（``tests/contract/test_T10_edit_body.py`` と同じ結線）。"""

    locks: Any = None
    writer_index: Any = None
    journal_dir: Any = None


@dataclass(slots=True)
class Env:
    name: str
    root: Path
    files: dict[str, FileState]
    pipeline: Pipeline
    ws: Any
    writer: IndexWriter
    journal_dir: Path
    lsp: PyLsp | TsServer | None = None
    lsp_info: dict[str, Any] = field(default_factory=dict)
    digest0: str = ""


def copy_fixture(src: Path, dst: Path, dirs: list[str], root_files: list[str]) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("node_modules", ".git", "__pycache__", ".venv", "media")
    for d in dirs:
        if (src / d).is_dir():
            shutil.copytree(src / d, dst / d, ignore=ignore, dirs_exist_ok=True)
    for name in root_files:
        if (src / name).is_file():
            shutil.copy2(src / name, dst / name)


def make_pipeline(root: Path, cache: Path, writer: IndexWriter) -> Pipeline:
    base = build_services(lambda: [str(root)])
    services = EditServices(
        roots=base.roots, trees=base.trees, lines=base.lines, locks=WorkspaceLocks(), writer_index=writer,
        journal_dir=cache / "journal",
    )  # fmt: skip
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=services)


# --- 1 操作 ---------------------------------------------------------------------------------------------------


def unified(before: bytes, after: bytes, rel: str) -> str:
    a = before.decode("utf-8", "replace").splitlines(keepends=True)
    b = after.decode("utf-8", "replace").splitlines(keepends=True)
    d = "".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=2))
    return d if len(d) <= DIFF_CAP else d[:DIFF_CAP] + "\n... (truncated)"


def first_diff(before: bytes, after: bytes) -> dict[str, Any]:
    n = min(len(before), len(after))
    i = next((k for k in range(n) if before[k] != after[k]), n)
    line = before.count(b"\n", 0, i) + 1
    return {"offset": i, "line": line, "before": before[max(0, i - 30) : i + 30].decode("utf-8", "replace"),
            "after": after[max(0, i - 30) : i + 30].decode("utf-8", "replace"),
            "len_before": len(before), "len_after": len(after)}  # fmt: skip


async def run_op(env: Env, op: Op, *, do_rollback: bool, use_lsp: bool) -> dict[str, Any]:
    c = op.cand
    f = env.files[c.rel]
    path = env.root / c.rel
    rec: dict[str, Any] = {
        "idx": op.idx, "category": op.category, "operation": op.operation, "transform": op.transform,
        "fixture": c.fx, "path": c.rel, "symbol": c.name, "kind": c.kind, "line": c.line, "language": c.lang,
    }  # fmt: skip
    t0 = time.perf_counter()
    try:
        res = await env.pipeline.call_tool("edit_symbol_body", {"format": "json", "diagnostics_wait_ms": 0, **op.args})
    except Exception as exc:  # noqa: BLE001
        rec["outcome"] = "fail"
        rec["reason"] = f"exception {type(exc).__name__}: {exc}"
        _restore(path, f.data)
        return rec
    rec["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    after = path.read_bytes()
    changed = after != f.data
    try:
        if res.is_error:
            err = res.structured["error"] if res.structured else {}
            code = err.get("code", "?")
            details = err.get("details") or {}
            rec["error_code"] = code
            if code == "E_UNSUPPORTED" and details.get("reason") == "operation_not_implemented":
                rec["outcome"] = "skip_unimplemented"
            elif changed:
                rec["outcome"] = "fail"
                rec["reason"] = f"error {code} but the file changed (partial write?)"
                rec["diff"] = unified(f.data, after, c.rel)
            else:
                rec["outcome"] = "refused"
                rec["message"] = str(err.get("message", ""))[:200]
                rec["details"] = {k: details[k] for k in ("reason", "hint", "errors") if k in details}
            return rec
        data = res.structured["data"]
        rec["applied"] = bool(data.get("applied"))
        rec["edit_id"] = data.get("edit_id")
        rec["syntax_check"] = data.get("syntax_check")
        fc = data.get("files_changed") or []
        rec["ep1"] = bool(fc) and fc[0].get("sha256_after") == sha256(after)
        if not data.get("applied"):
            rec["outcome"] = "fail" if changed else "refused"
            rec["reason"] = "applied=false" + (" but the file changed (EP-2)" if changed else "")
            return rec
        if op.category == "identity":
            ok = (not changed) and data.get("edit_id") is None
            rec["ep6"] = ok
            rec["outcome"] = "ok" if ok else "fail"
            if not ok:
                rec["reason"] = "identity edit changed the file" if changed else "identity edit returned an edit_id"
                if changed:
                    rec["first_diff"] = first_diff(f.data, after)
                    rec["diff"] = unified(f.data, after, c.rel)
            return rec
        if not changed:
            rec["outcome"] = "fail"
            rec["reason"] = "applied=true but the file did not change"
            return rec
        before_n, after_n = f.errors_before, error_node_count(after, c.lang)
        rec["errors_before"], rec["errors_after"] = before_n, after_n
        if use_lsp and env.lsp is not None:
            rec["lsp"] = await env.lsp.check(f, after)
        else:
            rec["lsp"] = {"state": "unverified", "reason": "lsp_disabled" if not use_lsp else "no_lsp"}
        bad_tree = after_n > before_n
        bad_lsp = rec["lsp"]["state"] == "new_syntax_errors"
        rec["outcome"] = "fail" if (bad_tree or bad_lsp or not rec["ep1"]) else "ok"
        if rec["outcome"] == "fail":
            rec["reason"] = ", ".join(
                x for x, b in (("tree-sitter error nodes grew", bad_tree), ("lsp new syntax error", bad_lsp),
                               ("sha256_after != disk (EP-1)", not rec["ep1"])) if b
            )  # fmt: skip
            rec["diff"] = unified(f.data, after, c.rel)
        if do_rollback and rec["edit_id"]:
            rec["journal_restore"] = restore_from_journal(env, rec["edit_id"], f.data, path)
        return rec
    finally:
        _restore(path, f.data)


def restore_from_journal(env: Env, edit_id: str, pristine: bytes, path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"edit_id": edit_id}
    try:
        paths = rollback(env.journal_dir, edit_id)
        out["restored_paths"] = len(paths)
        out["ok"] = path.read_bytes() == pristine
        if not out["ok"]:
            out["reason"] = "bytes differ after rollback"
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["reason"] = f"{type(exc).__name__}: {exc}"
    return out


def _restore(path: Path, pristine: bytes) -> None:
    try:
        if path.read_bytes() != pristine:
            path.write_bytes(pristine)
    except OSError:
        path.write_bytes(pristine)


# --- 集計 -----------------------------------------------------------------------------------------------------


def rate(num: int, den: int) -> float | None:
    return round(num / den, 5) if den else None


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    def agg(rs: list[dict[str, Any]]) -> dict[str, Any]:
        cnt = {k: sum(1 for r in rs if r["outcome"] == k) for k in ("ok", "fail", "refused", "skip_unimplemented")}
        judged = cnt["ok"] + cnt["fail"]
        return {
            "n": len(rs), **cnt, "judged": judged,
            "success_rate": rate(cnt["ok"], judged),
            "completion_rate": rate(cnt["ok"], judged + cnt["refused"]),
            "lsp_unverified": sum(1 for r in rs if r.get("lsp", {}).get("state") == "unverified"),
        }  # fmt: skip

    by_cat = {cat: agg([r for r in records if r["category"] == cat]) for cat in PLAN_COUNTS}
    by_lang = {lang: agg([r for r in records if r["language"] == lang]) for lang in sorted({r["language"] for r in records})}
    by_transform = {
        t: agg([r for r in records if r.get("transform") == t]) for t in TRANSFORMS if any(r.get("transform") == t for r in records)
    }
    codes: dict[str, int] = {}
    for r in records:
        if r["outcome"] == "refused":
            key = f"{r.get('error_code', 'applied=false')}:{(r.get('details') or {}).get('reason', '')}"
            codes[key] = codes.get(key, 0) + 1
    ident = [r for r in records if r["category"] == "identity" and r["outcome"] in ("ok", "fail")]
    return {
        "total": agg(records), "by_category": by_cat, "by_language": by_lang, "by_transform": by_transform,
        "refused_by_code": codes,
        "ep6_identity": {"n": len(ident), "byte_identical": sum(1 for r in ident if r.get("ep6")),
                         "rate": rate(sum(1 for r in ident if r.get("ep6")), len(ident))},  # fmt: skip
    }  # fmt: skip


# --- stdio の標本 ---------------------------------------------------------------------------------------------


async def run_stdio_sample(ops: list[Op], results: dict[int, dict[str, Any]], envs: dict[str, Env], specs: dict[str, Any],
                           n: int, tmp: Path) -> dict[str, Any]:  # fmt: skip
    """同じ操作を実プロセスのサーバーに投げ、``applied`` / ``syntax_check`` / 書き込み後の sha256 が一致することを確かめる。"""
    done = [o for o in ops if results[o.idx]["outcome"] in ("ok",) and results[o.idx].get("applied")]
    by_cat: dict[str, list[Op]] = {}
    for o in done:
        by_cat.setdefault(o.category, []).append(o)
    sample: list[Op] = []
    cats = sorted(by_cat)
    i = 0
    while len(sample) < n and any(by_cat.values()):
        cat = cats[i % len(cats)]
        i += 1
        if by_cat[cat]:
            sample.append(by_cat[cat].pop(0))
    out: dict[str, Any] = {"requested": n, "selected": len(sample), "compared": 0, "mismatches": [], "errors": []}
    by_fx: dict[str, list[Op]] = {}
    for o in sample:
        by_fx.setdefault(o.cand.fx, []).append(o)
    for fx, fxops in sorted(by_fx.items()):
        spec = specs[fx]
        root = tmp / f"stdio-{spec['name']}" / spec["name"]
        copy_fixture(FF.fixture_dir(spec), root, spec["dirs"], spec["root_files"])
        client = S.McpStdioClient(_server_argv(), cwd=root, env=S.server_env(tmp / f"stdio-cache-{fx}"), roots=[root],
                                  stderr_path=tmp / f"stdio-{fx}.stderr.log")  # fmt: skip
        try:
            await client.start()
            for o in fxops:
                rec = results[o.idx]
                path = root / o.cand.rel
                pristine = envs[fx].files[o.cand.rel].data
                got: dict[str, Any] | None = None
                for attempt in range(3):
                    await asyncio.sleep(0.4)
                    r = await client.call_tool("edit_symbol_body", {"format": "json", "diagnostics_wait_ms": 0, **o.args}, timeout=60)
                    env_ = r.envelope or {}
                    if r.is_error:
                        code = (env_.get("error") or {}).get("code")
                        if code == "E_PRECONDITION_FAILED" and attempt < 2:
                            _restore(path, pristine)
                            continue
                        got = {"error": code or r.text[:200]}
                    else:
                        d = env_.get("data") or {}
                        fc = d.get("files_changed") or [{}]
                        got = {"applied": d.get("applied"), "syntax_check": d.get("syntax_check"),
                               "sha256_after": fc[0].get("sha256_after"), "disk_sha256": sha256(path.read_bytes())}  # fmt: skip
                    break
                _restore(path, pristine)
                want_after = rec.get("sha256_after_inproc")
                out["compared"] += 1
                same = (
                    got is not None
                    and "error" not in got
                    and got["applied"] == rec["applied"]
                    and got["syntax_check"] == rec["syntax_check"]
                    and got["sha256_after"] == want_after
                    and got["disk_sha256"] == want_after
                )
                if not same:
                    out["mismatches"].append({"idx": o.idx, "category": o.category, "path": o.cand.rel, "symbol": o.cand.name,
                                              "inproc": {"applied": rec["applied"], "syntax_check": rec["syntax_check"], "sha256_after": want_after},
                                              "stdio": got})  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{fx}: {type(exc).__name__}: {exc}")
        finally:
            rep = await client.close()
            client.abort()
            out.setdefault("shutdown", {})[fx] = {"exit_code": rep.get("exit_code"), "orphans": len(rep.get("orphans_after_1s", []))}
            out.setdefault("watch_ok", {})[fx] = client.watch_report()["ok"]
    out["ok"] = out["compared"] > 0 and not out["mismatches"] and not out["errors"]
    return out


# --- 本体 -----------------------------------------------------------------------------------------------------


def load_specs(selected: list[str]) -> dict[str, dict[str, Any]]:
    data = tomllib.loads(FF.TOML.read_text(encoding="utf-8"))
    specs: dict[str, dict[str, Any]] = {}
    for fx in selected:
        d = dict(data[fx])
        d["dirs"] = list(d.get("src_dirs", [])) + list(d.get("test_dirs", []))
        cfg = d.get("config", "")
        d["root_files"] = [cfg, "package.json", "tsconfig.json", "pyrightconfig.json"] if cfg else []
        specs[fx] = d
    return specs


async def run_all(args: argparse.Namespace) -> dict[str, Any]:
    specs = load_specs(args.fixtures.split(","))
    tmp = Path(tempfile.mkdtemp(prefix="lucianke-bench-edit-"))
    envs: dict[str, Env] = {}
    cands: list[Cand] = []
    t_start = time.perf_counter()
    meter = R.CpuLoadMeter()
    fixture_infos: dict[str, Any] = {}
    try:
        for fx, spec in specs.items():
            src = FF.fetch(spec) if not (FF.fixture_dir(spec) / ".git").exists() else FF.fixture_dir(spec)
            fixture_infos[fx] = R.fixture_info(fx, src, sha=spec["sha"]) if args.digest else {"name": fx, "root": str(src), "sha": spec["sha"]}
            root = Path(os.path.realpath(tmp / fx / spec["name"]))
            copy_fixture(src, root, spec["dirs"], spec["root_files"])
            cache = tmp / f"cache-{fx}"
            cache.mkdir(parents=True)
            writer = IndexWriter(cache / "paad.sqlite")
            pipeline = make_pipeline(root, cache, writer)
            ws = pipeline.services.workspace(None)
            files, fx_cands = scan_fixture(fx, root, spec["dirs"], ws.workspace_id)
            envs[fx] = Env(fx, root, files, pipeline, ws, writer, cache / "journal", digest0=R.tree_digest(root))
            cands += fx_cands
            print(f"  [{fx}] files={len(files)} candidates={len(fx_cands)}", flush=True)

        counts = {k: max(1, round(v * args.scale)) for k, v in PLAN_COUNTS.items()} if args.scale != 1.0 else dict(PLAN_COUNTS)
        ops, plan_info = build_plan(cands, counts, args.seed)
        print(f"  plan: {len(ops)} ops {json.dumps({k: v['selected'] for k, v in plan_info.items()})}", flush=True)

        # LSP
        if not args.no_lsp:
            for fx, env in envs.items():
                lang = specs[fx]["language"]
                if lang == "python":
                    lsp = PyLsp(env.root, env.ws)
                    ok = await lsp.start()
                    env.lsp, env.lsp_info = (lsp if ok else None), {"server": lsp.name, "available": ok, "start_ms": lsp.start_ms, "kind": "lsp"}
                    if not ok:
                        env.lsp_info["reason"] = "basedpyright did not become ready"
                else:
                    ts = TsServer(FF.fixture_dir(specs[fx]) / "node_modules" / "typescript" / "lib" / "tsserver.js", env.root)
                    ok = False
                    with contextlib.suppress(Exception):
                        ok = await ts.start()
                    env.lsp, env.lsp_info = (ts if ok else None), {"server": "tsserver", "version": ts.version, "available": ok,
                                                                    "kind": "tsserver syntacticDiagnosticsSync"}  # fmt: skip
                    if not ok:
                        env.lsp_info["reason"] = "tsserver.js not found or failed to start (LSP 未確認)"
                print(f"  [{fx}] LSP: {env.lsp_info}", flush=True)
        else:
            for env in envs.values():
                env.lsp_info = {"available": False, "reason": "--no-lsp"}

        results: dict[int, dict[str, Any]] = {}
        restore_done = 0
        t_ops = time.perf_counter()
        for n_done, op in enumerate(ops, 1):
            env = envs[op.cand.fx]
            want_rb = op.category != "identity" and restore_done < RESTORE_CHECKS
            rec = await run_op(env, op, do_rollback=want_rb, use_lsp=not args.no_lsp)
            if "journal_restore" in rec:
                restore_done += 1
            results[op.idx] = rec
            if n_done % 100 == 0:
                print(f"  {n_done}/{len(ops)} ops ({time.perf_counter() - t_ops:.0f}s)", flush=True)
        ops_s = round(time.perf_counter() - t_ops, 1)
        return await _finish(args, specs, envs, ops, results, plan_info, fixture_infos, meter, tmp, t_start, ops_s)
    finally:
        for env in envs.values():
            if env.lsp is not None:
                await env.lsp.stop()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(env.writer.close)
        shutil.rmtree(tmp, ignore_errors=True)


async def _finish(args: argparse.Namespace, specs: dict[str, Any], envs: dict[str, Env], ops: list[Op], results: dict[int, dict[str, Any]],
                  plan_info: dict[str, Any], fixture_infos: dict[str, Any], meter: Any, tmp: Path, t_start: float, ops_s: float) -> dict[str, Any]:  # fmt: skip
    records = [results[o.idx] for o in ops]
    summary = summarize(records)
    restore = [r["journal_restore"] for r in records if "journal_restore" in r]
    restore_info = {"required": RESTORE_CHECKS, "checked": len(restore), "ok": sum(1 for x in restore if x.get("ok")),
                    "failures": [x for x in restore if not x.get("ok")][:10]}  # fmt: skip
    tree_clean = {fx: R.tree_digest(env.root) == env.digest0 for fx, env in envs.items()}
    stdio: dict[str, Any] = {"skipped": True}
    if not args.no_stdio:
        # stdio 比較用に、プロセス内の書き込み後の sha を持ち直す（判定済みの ok の操作だけ）
        for o in ops:
            rec = results[o.idx]
            if rec["outcome"] == "ok" and rec.get("applied"):
                rec["sha256_after_inproc"] = await _sha_after(envs[o.cand.fx], o)
        stdio = await run_stdio_sample(ops, results, envs, specs, args.stdio_sample, tmp)

    judged = summary["total"]["judged"]
    rate_all = summary["total"]["success_rate"]
    skipped = summary["total"]["skip_unimplemented"]
    ep6 = summary["ep6_identity"]
    failures = [r for r in records if r["outcome"] == "fail"]
    refused = [r for r in records if r["outcome"] == "refused"]
    safe_pass = rate_all is not None and rate_all >= THRESHOLD
    ep6_pass = ep6["rate"] == 1.0 if ep6["n"] else None
    restorable = sum(1 for r in records if r["outcome"] == "ok" and r["category"] != "identity" and r.get("edit_id"))
    restore_info["restorable_ops"] = restorable
    restore_pass = (
        restore_info["checked"] >= min(RESTORE_CHECKS, restorable)
        and restore_info["ok"] == restore_info["checked"]
        and restore_info["checked"] > 0
    )
    verdict = "pass" if (safe_pass and ep6_pass and restore_pass and all(tree_clean.values()) and (stdio.get("ok") or stdio.get("skipped"))) else "fail"
    if verdict == "pass" and skipped:
        verdict = "partial"  # 合格だが未実装の操作を合否から外している
    for r in records:
        r.pop("sha256_after_inproc", None)
    return {
        "verdict": verdict,
        "threshold": THRESHOLD,
        "scale": args.scale,
        "seed": args.seed,
        "plan": plan_info,
        "fixtures": fixture_infos,
        "lsp": {fx: env.lsp_info for fx, env in envs.items()},
        "summary": summary,
        "nfr_safe_1": {"judged": judged, "ok": summary["total"]["ok"], "fail": summary["total"]["fail"], "success_rate": rate_all,
                       "pass": safe_pass, "skipped_unimplemented": skipped, "refused": summary["total"]["refused"]},  # fmt: skip
        "ep6": {**ep6, "pass": ep6_pass},
        "journal_restore": {**restore_info, "pass": restore_pass},
        "tree_unchanged_after_run": tree_clean,
        "stdio_sample": stdio,
        "failures": failures[:50],
        "refused_samples": refused[:30],
        "ops": [{k: v for k, v in r.items() if k not in ("diff",)} for r in records],
        "timing": {"ops_s": ops_s, "total_s": round(time.perf_counter() - t_start, 1)},
        "load": meter.stop(),
    }


async def _sha_after(env: Env, op: Op) -> str | None:
    """stdio 比較用: 同じ操作を ``dry_run`` で呼び、書き込み後の sha を得る（ファイルは変えない）。"""
    r = await env.pipeline.call_tool("edit_symbol_body", {"format": "json", "diagnostics_wait_ms": 0, "dry_run": True, **op.args})
    if r.is_error or not r.structured:
        return None
    fc = (r.structured["data"].get("files_changed") or [{}])[0]
    return fc.get("sha256_after")


def main() -> int:
    ap = argparse.ArgumentParser(description="編集の構文妥当性ベンチ（仕様書 §14.5）")
    ap.add_argument("--fixtures", default="F-PY,F-TS")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--scale", type=float, default=1.0, help="規模の倍率（煙試験は 0.05 など）")
    ap.add_argument("--stdio-sample", type=int, default=STDIO_SAMPLE)
    ap.add_argument("--no-stdio", action="store_true")
    ap.add_argument("--no-lsp", action="store_true")
    ap.add_argument("--digest", action="store_true", help="フィクスチャの内容ハッシュも記録する（ky の node_modules で遅い）")
    ap.add_argument("--no-write", action="store_true", help="結果ファイルを書かない")
    args = ap.parse_args()
    for spec in load_specs(args.fixtures.split(",")).values():
        if not (FF.fixture_dir(spec) / ".git").exists():
            FF.fetch(spec)
    payload = asyncio.run(run_all(args))
    s = payload["nfr_safe_1"]
    print(f"verdict={payload['verdict']} success={s['ok']}/{s['judged']} rate={s['success_rate']} refused={s['refused']} "
          f"skipped={s['skipped_unimplemented']} ep6={payload['ep6']['byte_identical']}/{payload['ep6']['n']} "
          f"journal={payload['journal_restore']['ok']}/{payload['journal_restore']['checked']} "
          f"stdio_ok={payload['stdio_sample'].get('ok')}", flush=True)
    if not args.no_write:
        path = R.write_result("edit-safety", payload)
        print(f"wrote {path}", flush=True)
    return 0 if payload["verdict"] in ("pass", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
