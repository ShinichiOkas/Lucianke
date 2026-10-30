"""T-08 ``get_diagnostics`` の契約テスト（仕様書 §11.4 の事後条件 1〜5 ほか）。

F-POLY を ``tmp`` にコピーし、診断を足したファイルを加えて、実 ``Indexer`` で PaaD に取り込む（構文エラーは索引時に入る）。
LSP 経路は **偽の LSP サーバー**（内容から診断を作る Python スクリプト。UTF-16 の位置・版付きの ``publishDiagnostics``・
``codeAction`` を返す）で、実際の basedpyright / ruff でも 1 本ずつ確かめる（見つからなければ skip）。

サービスは ``db_path``・``lines``・``workspace()`` に ``lsp_pool``・``code_action_store``・``trees`` を足した偽物
（``tools/__init__.py`` と ``app.py`` の結線には依存しない）。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import compact_grammar as g
import pytest

from lucianke.core.procs import ProcessRegistry
from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.lsp.action_store import CodeActionStore
from lucianke.lsp.pool import LspPool, default_resolver
from lucianke.lsp.servers import LspServerDef, servers_for
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t08_get_diagnostics as t08
from lucianke.tools._base import Services
from lucianke.workspace.types import PythonEnv, Workspace

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS_ID = "poly"
IS_WINDOWS = os.name == "nt"
PAY = "src/shop/services/payment.py"
BROKEN = "src/shop/broken.py"
UNI = "src/shop/uni.py"
MANY = "src/shop/many.py"
MUTABLE = "src/shop/mutable.py"
LINTY = "src/shop/linty.py"

EXTRA_FILES: dict[str, str] = {
    BROKEN: "def broken(:\n    pass\n",
    UNI: (
        '"""unicode"""\ns = "日本語😀"  # BAD_one\nt = "😀😀😀"; WARN_two\nINFO_three = 1\nHINT_four = 2\n'
    ),
    MANY: "".join(f"BAD_{i:02d} = {i}\n" for i in range(60)),
    MUTABLE: "x = 1\n",
    LINTY: "import os, sys\n",  # ruff: F401（os・sys が未使用）と 1 行 import の指摘（E401 / I001。版で違う）
}

FAKE_LSP = r"""
import argparse, json, re, sys, threading, time

ap = argparse.ArgumentParser()
ap.add_argument("--name", default="fakels")
ap.add_argument("--kinds", default="BAD,INFO,HINT")
ap.add_argument("--static")
ap.add_argument("--action-delay", type=float, default=0.0)
ap.add_argument("--init-delay", type=float, default=0.0)
ap.add_argument("--no-version", action="store_true")
args = ap.parse_args()
KINDS = args.kinds.split(",")
PATTERNS = {"BAD": (1, "E100"), "WARN": (2, "W200"), "INFO": (3, "I300"), "HINT": (4, "H400")}
STATIC = json.load(open(args.static)) if args.static else {}

inp, out = sys.stdin.buffer, sys.stdout.buffer
lock = threading.Lock()
texts = {}


def read():
    headers = {}
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        k, _, v = line.partition(b":")
        headers[k.strip().lower()] = v.strip()
    return json.loads(inp.read(int(headers[b"content-length"])))


def send(obj):
    data = json.dumps(obj).encode()
    with lock:
        out.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        out.flush()


def u16(s):
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def diags_for(uri, text):
    found = []
    for ln, line in enumerate(text.replace("\r", "").split("\n")):
        for kind in KINDS:
            sev, code = PATTERNS[kind]
            for m in re.finditer(r"\b%s\w*" % kind, line):
                start = u16(line[: m.start()])
                found.append({
                    "range": {"start": {"line": ln, "character": start},
                              "end": {"line": ln, "character": start + u16(m.group())}},
                    "severity": sev, "code": code, "source": args.name,
                    "message": "%s found: %s" % (args.name, m.group()),
                })
    for suffix, items in STATIC.items():
        if uri.endswith(suffix):
            found.extend(items)
    return found


def publish(uri, version):
    p = {"uri": uri, "diagnostics": diags_for(uri, texts[uri])}
    if version is not None and not args.no_version:
        p["version"] = version
    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": p})


def answer_code_action(rid, params):
    ctx = params.get("context", {})
    uri = params["textDocument"]["uri"]
    result = []
    for d in ctx.get("diagnostics", []):
        if d.get("severity", 1) <= 2:
            result.append({"title": "Fix %s" % d.get("code"), "kind": "quickfix", "diagnostics": [d],
                           "edit": {"changes": {uri: [{"range": d["range"], "newText": "fixed"}]}}})
    send({"jsonrpc": "2.0", "id": rid, "result": result})


while True:
    msg = read()
    if msg is None:
        break
    m, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        time.sleep(args.init_delay)
        send({"jsonrpc": "2.0", "id": rid,
              "result": {"capabilities": {"textDocumentSync": 1, "codeActionProvider": True}}})
    elif m == "shutdown":
        send({"jsonrpc": "2.0", "id": rid, "result": None})
    elif m == "exit":
        sys.exit(0)
    elif m in ("textDocument/didOpen", "textDocument/didChange"):
        td = params["textDocument"]
        if m == "textDocument/didOpen":
            texts[td["uri"]] = td["text"]
        else:
            texts[td["uri"]] = params["contentChanges"][-1]["text"]
        publish(td["uri"], td.get("version"))
    elif m == "textDocument/codeAction":
        if args.action_delay > 0:
            threading.Timer(args.action_delay, answer_code_action, (rid, params)).start()
        else:
            answer_code_action(rid, params)
    elif m is not None and rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": None})
"""

TYPES = LspServerDef(language_id="python", name="fakels", argv_candidates=(("fakels",),), role="types")
LINT = LspServerDef(language_id="python", name="fakelint", argv_candidates=(("fakelint",),), role="lint")


# --- 環境 ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class FakeServices(Services):
    db_path: Path
    lines: ModuleType
    root: Path
    lsp_pool: LspPool | None = None
    code_action_store: CodeActionStore | None = None
    trees: TreeCache | None = None
    diagnostics_wait_ms: int = 5000

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        return Workspace(
            workspace_id=WS_ID,
            root=self.root,
            python_env=PythonEnv(interpreter=sys.executable),
            case_insensitive=IS_WINDOWS,
        )

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        return WS_ID


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Path]]:
    """F-POLY のコピー（診断を足したファイル入り）と、取り込み済みの PaaD。"""
    base = tmp_path_factory.mktemp("t08")
    root = Path(os.path.realpath(base / WS_ID))
    shutil.copytree(POLY, root)
    for rel, text in EXTRA_FILES.items():
        (root / rel).write_text(text, encoding="utf-8", newline="")
    db = base / "db" / "paad.sqlite"

    async def scan() -> None:
        writer = IndexWriter(db)
        readers = ReaderPool(db, size=2)
        try:
            indexer = Indexer(
                Workspace(workspace_id=WS_ID, root=root, case_insensitive=IS_WINDOWS),
                writer,
                readers,
                TreeCache(),
            )
            report = await indexer.scan()
            assert not report.errors, report.errors
            await indexer.aclose()
        finally:
            readers.close()
            writer.close()

    asyncio.run(scan())
    yield root, db


def snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def make_pipeline(
    indexed: tuple[Path, Path],
    *,
    pool: LspPool | None = None,
    store: CodeActionStore | None = None,
    **kw: Any,
) -> Pipeline:
    root, db = indexed
    registry = ToolRegistry()
    registry.register(t08.SPEC)
    services = FakeServices(
        db_path=db, lines=compact, root=root, lsp_pool=pool, code_action_store=store, trees=TreeCache(), **kw
    )
    return Pipeline(registry, services=services)


async def call(pipeline: Pipeline, fmt: str = "json", **args: Any):
    return await pipeline.call_tool("get_diagnostics", {"format": fmt, **args})


def diags_of(r: Any) -> list[dict[str, Any]]:
    assert not r.is_error, r.text
    return list(r.structured["data"]["diagnostics"])


@dataclass
class LspEnv:
    root: Path
    script: Path
    registry: ProcessRegistry
    pools: list[LspPool]
    static: Path

    def pool(
        self, *, servers: tuple[LspServerDef, ...] = (TYPES, LINT), extra: tuple[str, ...] = (), **kw: Any
    ) -> LspPool:
        def resolver(defn: LspServerDef, ws: Workspace) -> list[str]:
            kinds = "WARN" if defn.role == "lint" else "BAD,INFO,HINT"
            return [sys.executable, str(self.script), "--name", defn.name, "--kinds", kinds,
                    "--static", str(self.static), *extra]  # fmt: skip

        kw.setdefault("ready_fallback_s", 0.2)
        kw.setdefault("grace_ms", 300)
        pool = LspPool(self.registry, servers=servers, resolver=resolver, **kw)
        self.pools.append(pool)
        return pool

    def ws(self) -> Workspace:
        return Workspace(workspace_id=WS_ID, root=self.root, case_insensitive=IS_WINDOWS)


@pytest.fixture
async def lsp(indexed: tuple[Path, Path], tmp_path: Path) -> AsyncIterator[LspEnv]:
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_LSP, encoding="utf-8")
    static = tmp_path / "static.json"
    static.write_text("{}", encoding="utf-8")
    registry = ProcessRegistry()
    env = LspEnv(indexed[0], script, registry, [], static)
    yield env
    for pool in env.pools:
        await pool.shutdown_all(300)
    await registry.terminate_all(300, close=True)


async def ready(pool: LspPool, ws: Workspace) -> None:
    assert await pool.wait_ready("python", ws, 20_000)
    assert await pool.wait_ready("python", ws, 20_000, role="lint")


def line_of(root: Path, path: str, line: int) -> str:
    return (root / path).read_text(encoding="utf-8").split("\n")[line - 1]


# --- 構文木だけ（LSP なし）--------------------------------------------------------------------


async def test_t08_is_listed(indexed: tuple[Path, Path]) -> None:
    names = [d.name for d in make_pipeline(indexed).registry.descriptors()]
    assert "get_diagnostics" in names


async def test_structural_syntax_errors_without_lsp(indexed: tuple[Path, Path]) -> None:
    r = await call(make_pipeline(indexed), "compact", paths=[BROKEN])
    assert not r.is_error, r.text
    assert g.validate(r.text) == [], r.text
    lines = r.text.splitlines()
    assert lines[0].startswith(
        "# get_diagnostics ok conf=structural lsp=unavailable rev=WORKING_TREE ws=poly "
    )
    assert any(ln.startswith("! DEGRADED: confidence=structural") for ln in lines)
    assert "型・未定義参照・Lint の診断は含まれていません" in r.text  # accuracy_warning / warnings
    assert any(ln.startswith("diagnostics: 1 in 1 file (1 E; min_severity=warning)") for ln in lines)
    diag = next(ln for ln in lines if ln.startswith("E "))
    assert diag.startswith(f"E {BROKEN}:1:") and "[tree-sitter:syntax] syntax error" in diag
    assert "fix=" not in diag

    j = await call(make_pipeline(indexed), paths=[BROKEN])
    d = diags_of(j)[0]
    assert (d["source"], d["code"], d["severity"], d["confidence_level"]) == (
        "tree-sitter",
        "syntax",
        "error",
        "structural",
    )
    meta = j.structured["_resolution_meta"]
    assert meta["confidence_level"] == "structural" and meta["is_degraded"] is True
    assert meta["lsp_status"] == "unavailable" and meta["accuracy_warning"]
    assert d["code_actions"] == []
    assert d["enclosing_urn"] is None or d["enclosing_urn"].startswith(f"lk://{WS_ID}/{BROKEN}")


async def test_structural_without_pool_has_no_type_errors(indexed: tuple[Path, Path]) -> None:
    """F-POLY の意図的な型エラー（payment.py の 63 行）は LSP が無ければ返らない（構文木だけ）。"""
    r = await call(make_pipeline(indexed), paths=[PAY], min_severity="hint")
    assert diags_of(r) == []
    assert r.structured["_resolution_meta"]["confidence_level"] == "structural"


# --- 事後条件 1〜5 ----------------------------------------------------------------------------


@pytest.mark.parametrize("min_severity", ["error", "warning", "information", "hint"])
async def test_postcondition_1_severity_is_at_least_min_severity(
    indexed: tuple[Path, Path], lsp: LspEnv, min_severity: str
) -> None:
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    pipe = make_pipeline(indexed, pool=pool, store=CodeActionStore())
    r = await call(pipe, paths=[UNI, BROKEN], min_severity=min_severity)
    order = ["error", "warning", "information", "hint"]
    got = diags_of(r)
    assert got and all(order.index(d["severity"]) <= order.index(min_severity) for d in got)
    expected = {
        "error": {"error"},
        "warning": {"error", "warning"},
        "information": {"error", "warning", "information"},
        "hint": set(order),
    }[min_severity]
    assert {d["severity"] for d in got} == expected
    # 重大度の高い順 → パス → 行
    keys = [(order.index(d["severity"]), d["path"], d["line"]) for d in got]
    assert keys == sorted(keys)


async def test_postcondition_2_lines_exist_and_snippets_match(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    pipe = make_pipeline(indexed, pool=pool, store=CodeActionStore())
    r = await call(pipe, paths=["src/shop"], min_severity="hint", max_tokens=20000)
    got = diags_of(r)
    assert {d["path"] for d in got} >= {UNI, BROKEN, MANY}
    root = lsp.root
    for d in got:
        text = (root / d["path"]).read_text(encoding="utf-8")
        rows = text.split("\n")
        n_lines = len(rows) - 1 if text.endswith("\n") else len(rows)
        assert 1 <= d["line"] <= n_lines, d
        assert d["snippet"] == rows[d["line"] - 1], d
        assert d["character"] >= 1 and (d["end_line"], d["end_character"]) >= (d["line"], d["character"])


async def test_postcondition_4_and_5_no_writes_and_no_child_processes(
    indexed: tuple[Path, Path], lsp: LspEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    pipe = make_pipeline(indexed, pool=pool, store=CodeActionStore())
    await call(
        pipe, paths=[UNI]
    )  # LSP に開かせておく（ここまでは LSP の子プロセス以外を起こしてよいか見ない）
    before = snapshot(lsp.root)
    live_before = sorted(mp.pid for mp in lsp.registry.live())
    spawned: list[str] = []
    real_popen = subprocess.Popen.__init__
    real_exec = asyncio.create_subprocess_exec

    def spy_popen(self: Any, *a: Any, **k: Any) -> None:
        spawned.append(f"Popen {a[:1]}")
        real_popen(self, *a, **k)

    async def spy_exec(*a: Any, **k: Any) -> Any:
        spawned.append(f"exec {a[:1]}")
        return await real_exec(*a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", spy_popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)
    for kwargs in ({"paths": [UNI, BROKEN]}, {}, {"paths": ["src/shop"], "include_code_actions": False}):
        assert not (await call(pipe, **kwargs)).is_error
    monkeypatch.undo()
    assert spawned == []
    assert snapshot(lsp.root) == before  # 事後条件 4
    assert sorted(mp.pid for mp in lsp.registry.live()) == live_before  # 事後条件 5: LSP 以外が増えていない


async def test_postcondition_5_structural_route_spawns_nothing(
    indexed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[str] = []
    real_popen = subprocess.Popen.__init__

    def spy_popen(self: Any, *a: Any, **k: Any) -> None:
        spawned.append(f"Popen {a[:1]}")
        real_popen(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", spy_popen)
    before = snapshot(indexed[0])
    assert not (await call(make_pipeline(indexed))).is_error
    monkeypatch.undo()
    assert spawned == [] and snapshot(indexed[0]) == before


# --- LSP 経路（偽の LSP）-----------------------------------------------------------------------


async def test_exact_route_with_fake_lsp(indexed: tuple[Path, Path], lsp: LspEnv) -> None:
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    pipe = make_pipeline(indexed, pool=pool, store=CodeActionStore())
    r = await call(pipe, "compact", paths=[UNI], min_severity="hint", include_code_actions=False)
    assert not r.is_error, r.text
    assert g.validate(r.text) == [], r.text
    assert r.text.splitlines()[0].startswith(
        "# get_diagnostics ok conf=exact lsp=ready rev=WORKING_TREE ws=poly "
    )
    assert "DEGRADED" not in r.text
    assert "diagnostics: 4 in 1 file (1 E, 1 W, 1 I, 1 H; min_severity=hint)" in r.text
    assert f"E {UNI}:2:" in r.text and "[fakels:E100] fakels found: BAD_one" in r.text
    assert f"W {UNI}:3:" in r.text and "[fakelint:W200]" in r.text  # lint 役のハブも集める

    j = await call(pipe, paths=[UNI], min_severity="hint", include_code_actions=False)
    got = {d["code"]: d for d in diags_of(j)}
    assert set(got) == {"E100", "W200", "I300", "H400"}
    assert all(d["confidence_level"] == "exact" for d in got.values())
    meta = j.structured["_resolution_meta"]
    assert (meta["confidence_level"], meta["is_degraded"], meta["lsp_status"]) == ("exact", False, "ready")
    assert meta["accuracy_warning"] is None
    summary = j.structured["data"]["summary"]
    assert summary["files"] == 1 and summary["by_severity"] == {
        "error": 1,
        "warning": 1,
        "information": 1,
        "hint": 1,
    }
    assert summary["by_source"] == {"fakels": 3, "fakelint": 1}
    servers = {(s["language"], s["role"]): s for s in j.structured["data"]["lsp_sources"]}
    assert (
        servers[("python", "types")]["status"] == "ready"
        and servers[("python", "lint")]["server"] == "fakelint"
    )


async def test_positions_are_converted_from_utf16_to_codepoints(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    """絵文字（UTF-16 で 2 単位）の後ろの識別子の列が、コードポイントの 1 始まりで返る。"""
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    r = await call(
        make_pipeline(indexed, pool=pool), paths=[UNI], min_severity="hint", include_code_actions=False
    )
    by_code = {d["code"]: d for d in diags_of(r)}
    line2 = line_of(lsp.root, UNI, 2)
    e = by_code["E100"]
    assert e["line"] == 2 and e["character"] == line2.index("BAD_one") + 1
    assert e["end_character"] == e["character"] + len("BAD_one")
    line3 = line_of(lsp.root, UNI, 3)
    w = by_code["W200"]
    assert w["line"] == 3 and w["character"] == line3.index("WARN_two") + 1
    assert (
        len(line3[: w["character"] - 1].encode("utf-16-le")) // 2 > w["character"] - 1
    )  # 絵文字がある行である


async def test_lsp_wins_over_tree_sitter_at_the_same_position(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    root, db = indexed
    conn = __import__("sqlite3").connect(db)
    try:
        row = conn.execute(
            "SELECT d.start_line, d.start_col, d.message FROM diagnostics d JOIN files f USING(file_id) "
            "WHERE f.path = ? AND d.source = 'tree-sitter'",
            (BROKEN,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    line, col, message = row
    # 偽 LSP が同じ位置・同じメッセージの診断を出す（LSP の位置は 0 始まり）
    lsp.static.write_text(
        json.dumps(
            {
                BROKEN: [
                    {
                        "range": {"start": {"line": line - 1, "character": col - 1}, "end": {"line": line - 1, "character": col}},
                        "severity": 1, "code": "syn", "source": "fakels", "message": message,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )  # fmt: skip
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    r = await call(make_pipeline(indexed, pool=pool), paths=[BROKEN], include_code_actions=False)
    got = [d for d in diags_of(r) if (d["line"], d["character"]) == (line, col)]
    assert len(got) == 1 and got[0]["source"] == "fakels" and got[0]["confidence_level"] == "exact"
    # LSP 経路のファイルでは、構文エラーの行も exact 経路のレコード（_resolution_meta は exact）
    assert r.structured["_resolution_meta"]["confidence_level"] == "exact"


async def test_sources_filter_and_unknown_source_warning(indexed: tuple[Path, Path], lsp: LspEnv) -> None:
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    pipe = make_pipeline(indexed, pool=pool)
    r = await call(
        pipe, paths=[UNI, BROKEN], min_severity="hint", sources=["fakelint"], include_code_actions=False
    )
    assert {d["source"] for d in diags_of(r)} == {"fakelint"}
    r = await call(pipe, paths=[BROKEN], sources=["tree-sitter"], include_code_actions=False)
    assert {d["source"] for d in diags_of(r)} == {"tree-sitter"}
    r = await call(pipe, paths=[UNI], sources=["nope", "fakels"], include_code_actions=False)
    assert {d["source"] for d in diags_of(r)} == {"fakels"}
    assert any("nope" in w for w in r.structured["warnings"])


async def test_lsp_indexing_degrades_to_syntax_errors_then_exact(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    """LSP が起動中の呼び出しは待たずに構文エラーだけで答え（lsp=indexing）、ready になれば exact。"""
    pool = lsp.pool(extra=("--init-delay", "1.5"))
    pipe = make_pipeline(indexed, pool=pool)
    r = await call(pipe, "compact", paths=[UNI, BROKEN])
    assert not r.is_error, r.text
    assert r.text.splitlines()[0].startswith("# get_diagnostics ok conf=structural lsp=indexing ")
    assert "DEGRADED" in r.text and "起動・索引中" in r.text
    j = await call(pipe, paths=[UNI, BROKEN])
    assert {d["source"] for d in diags_of(j)} == {"tree-sitter"}
    await ready(pool, lsp.ws())
    j = await call(pipe, paths=[UNI, BROKEN], include_code_actions=False)
    assert j.structured["_resolution_meta"]["confidence_level"] == "exact"
    assert "fakels" in {d["source"] for d in diags_of(j)}


async def test_unavailable_lsp_degrades_without_error(indexed: tuple[Path, Path], lsp: LspEnv) -> None:
    pool = lsp.pool(servers=(TYPES, LINT))
    pool._resolver = lambda defn, ws: None  # type: ignore[assignment]  # not_found → unavailable
    assert not await pool.wait_ready("python", lsp.ws(), 5000)
    r = await call(make_pipeline(indexed, pool=pool), paths=[BROKEN])
    meta = r.structured["_resolution_meta"]
    assert (meta["lsp_status"], meta["confidence_level"], meta["is_degraded"]) == (
        "unavailable",
        "structural",
        True,
    )
    assert {d["source"] for d in diags_of(r)} == {"tree-sitter"}


async def test_files_without_a_grammar_report_zero_diagnostics(indexed: tuple[Path, Path]) -> None:
    r = await call(make_pipeline(indexed), paths=["legacy/calc.cob"], min_severity="hint")
    assert diags_of(r) == []
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "lexical_fallback" and meta["is_degraded"] is True
    assert r.structured["data"]["summary"]["by_source"] == {"n/a": 0}
    assert any("no diagnostic source" in w for w in r.structured["warnings"])


# --- QuickFix ---------------------------------------------------------------------------------


async def test_postcondition_3_code_action_ids_are_issued_and_resolvable(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    store = CodeActionStore()
    pipe = make_pipeline(indexed, pool=pool, store=store)
    r = await call(pipe, "compact", paths=[UNI], min_severity="hint")
    assert g.validate(r.text) == [], r.text
    e_line = next(ln for ln in r.text.splitlines() if ln.startswith(f"E {UNI}:2:"))
    assert " fix=qf_" in e_line
    i_line = next(ln for ln in r.text.splitlines() if ln.startswith(f"I {UNI}:"))
    assert "fix=" not in i_line  # QuickFix は error / warning だけ

    j = await call(pipe, paths=[UNI], min_severity="hint")
    got = diags_of(j)
    with_fix = [d for d in got if d["severity"] in ("error", "warning")]
    assert with_fix and all(d["code_actions"] for d in with_fix)
    ids = [a["id"] for d in with_fix for a in d["code_actions"]]
    assert len(set(ids)) == len(ids)  # セッション内で一意
    sha = hashlib.sha256((lsp.root / UNI).read_bytes()).hexdigest()
    for d in with_fix:
        for a in d["code_actions"]:
            assert a["id"].startswith("qf_") and a["kind"] == "quickfix" and a["title"].startswith("Fix ")
            stored = store.get(a["id"])  # T-13 が引ける（E_TARGET_NOT_FOUND にならない）
            assert stored is not None and stored.path == UNI and store.is_current(stored, sha)
            assert stored.action["edit"]["changes"]  # 適用に要る edit が原本のまま残っている
    assert all(d["code_actions"] == [] for d in got if d["severity"] in ("information", "hint"))
    # 同じ問い合わせは同じ ID（台帳を膨らませない）
    again = await call(pipe, paths=[UNI], min_severity="hint")
    assert [a["id"] for d in diags_of(again) for a in d["code_actions"]] == ids


async def test_include_code_actions_false_issues_nothing(indexed: tuple[Path, Path], lsp: LspEnv) -> None:
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    store = CodeActionStore()
    r = await call(
        make_pipeline(indexed, pool=pool, store=store), "compact", paths=[UNI], include_code_actions=False
    )
    assert "fix=" not in r.text and len(store) == 0
    j = await call(make_pipeline(indexed, pool=pool, store=store), paths=[UNI], include_code_actions=False)
    assert all(d["code_actions"] == [] for d in diags_of(j))


async def test_missing_store_warns_instead_of_issuing_unresolvable_ids(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    r = await call(make_pipeline(indexed, pool=pool, store=None), paths=[UNI])
    assert all(d["code_actions"] == [] for d in diags_of(r))
    assert any("code action store" in w for w in r.structured["warnings"])


async def test_code_action_lookup_over_3s_is_e_timeout_with_partial_result(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    pool = lsp.pool(extra=("--action-delay", "6"))
    await ready(pool, lsp.ws())
    r = await call(make_pipeline(indexed, pool=pool, store=CodeActionStore()), paths=[UNI])
    assert r.is_error and r.structured["error"]["code"] == "E_TIMEOUT"
    assert r.structured["error"]["details"]["partial"] is True
    got = r.structured["data"]["diagnostics"]  # 得られた分（診断そのもの）は data に入る
    assert {d["code"] for d in got} >= {"E100", "W200"} and all(d["code_actions"] == [] for d in got)


# --- 古い索引・範囲・予算 ------------------------------------------------------------------------


async def test_stale_index_is_reparsed_for_explicit_paths(indexed: tuple[Path, Path]) -> None:
    root, _ = indexed
    path = root / MUTABLE
    original = path.read_bytes()
    pipe = make_pipeline(indexed)
    try:
        assert diags_of(await call(pipe, paths=[MUTABLE])) == []
        path.write_bytes(b"def f(:\n    pass\n")  # 索引はまだ古い
        got = diags_of(await call(pipe, paths=[MUTABLE]))
        assert len(got) == 1 and got[0]["source"] == "tree-sitter" and got[0]["snippet"] == "def f(:"
        assert got[0]["line"] == 1
        # 逆に、索引に構文エラーが残っているファイルを直せば、それは返らない
        broken = root / BROKEN
        saved = broken.read_bytes()
        try:
            broken.write_bytes(b"def ok():\n    pass\n")
            assert diags_of(await call(pipe, paths=[BROKEN])) == []
        finally:
            broken.write_bytes(saved)
    finally:
        path.write_bytes(original)


async def test_directory_and_workspace_wide_scopes(indexed: tuple[Path, Path]) -> None:
    pipe = make_pipeline(indexed)
    whole = diags_of(await call(pipe))
    assert {d["path"] for d in whole} == {BROKEN}
    under = diags_of(await call(pipe, paths=["src/shop/models"]))
    assert under == []
    both = diags_of(await call(pipe, paths=["src/shop", "."]))
    assert [d["path"] for d in both] == [BROKEN]


async def test_budget_is_respected_and_pages_continue(indexed: tuple[Path, Path], lsp: LspEnv) -> None:
    pool = lsp.pool()
    await ready(pool, lsp.ws())
    pipe = make_pipeline(indexed, pool=pool, store=CodeActionStore())
    r = await call(pipe, "compact", paths=[MANY], max_tokens=300, include_code_actions=False)
    assert not r.is_error, r.text
    assert g.validate(r.text) == [], r.text
    assert "diagnostics: 60 in 1 file (60 E; min_severity=warning)" in r.text
    assert "# summary:" in r.text and "# more: cursor=" in r.text
    shown = sum(1 for ln in r.text.splitlines() if ln.startswith("E "))
    assert 0 < shown < 60
    j = await call(pipe, paths=[MANY], max_tokens=800, include_code_actions=False)  # json は封筒と data の全項目も数える（BUG-0017）
    assert j.structured["budget"]["used_tokens_est"] <= 800
    assert j.structured["page"]["next_cursor"] and j.structured["page"]["total_estimate"] == 60
    assert j.structured["data"]["summary"]["by_severity"]["error"] == 60  # 切り詰め前の総数
    nxt = await call(
        pipe,
        paths=[MANY],
        max_tokens=800,
        include_code_actions=False,
        cursor=j.structured["page"]["next_cursor"],
    )
    first = [d["line"] for d in j.structured["data"]["diagnostics"]]
    second = [d["line"] for d in nxt.structured["data"]["diagnostics"]]
    assert first == list(range(1, len(first) + 1)) and second[0] == len(first) + 1


# --- 入力の検査 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"paths": ["../outside.py"]}, "E_INVALID_ARGUMENT"),
        ({"paths": [BROKEN] * 51}, "E_INVALID_ARGUMENT"),
        ({"min_severity": "fatal"}, "E_INVALID_ARGUMENT"),
        ({"paths": ["src/shop/nope.py"]}, "E_TARGET_NOT_FOUND"),
        ({"revision": "HEAD"}, "E_REVISION_NOT_FOUND"),  # フィクスチャは Git のワーキングコピーではない（BUG-0018）
    ],
)
async def test_errors(indexed: tuple[Path, Path], args: dict[str, Any], code: str) -> None:
    r = await call(make_pipeline(indexed), **args)
    assert r.is_error and r.structured["error"]["code"] == code
    assert r.structured["data"] is None


async def test_index_missing_is_workspace_not_configured(tmp_path: Path) -> None:
    pipe = make_pipeline((tmp_path, tmp_path / "none.sqlite"))
    r = await call(pipe)
    assert r.is_error and r.structured["error"]["code"] == "E_WORKSPACE_NOT_CONFIGURED"


# --- 実際の LSP（basedpyright / ruff）-----------------------------------------------------------

_VENV = Path(sys.executable).parent
_HAVE_REAL = any((_VENV / f"basedpyright-langserver{ext}").exists() for ext in ("", ".exe", ".cmd"))


@pytest.mark.skipif(not _HAVE_REAL, reason="basedpyright-langserver is not installed in this environment")
async def test_real_basedpyright_and_ruff(indexed: tuple[Path, Path]) -> None:
    root, _ = indexed
    registry = ProcessRegistry()
    pool = LspPool(registry, ready_fallback_s=5.0, grace_ms=500)
    ws = Workspace(
        workspace_id=WS_ID,
        root=root,
        python_env=PythonEnv(interpreter=sys.executable),
        case_insensitive=IS_WINDOWS,
    )
    store = CodeActionStore()
    try:
        pipe = make_pipeline(indexed, pool=pool, store=store, diagnostics_wait_ms=60_000)
        assert await pool.wait_ready("python", ws, 120_000)
        have_ruff = await pool.wait_ready("python", ws, 60_000, role="lint")
        before = snapshot(root)
        r = await call(pipe, paths=[PAY], min_severity="hint")
        got = diags_of(r)
        assert r.structured["_resolution_meta"]["lsp_status"] == "ready"
        errors = [d for d in got if d["severity"] == "error"]
        # 意図的な型エラー: 63 行 `factor: Decimal = "1"`（str を Decimal に）
        e63 = next(d for d in errors if d["line"] == 63)
        assert e63["source"] == "basedpyright" and e63["code"] == "reportAssignmentType"
        assert "is not assignable to declared type" in e63["message"]  # locale: en
        assert e63["snippet"] == line_of(root, PAY, 63)
        assert e63["character"] == line_of(root, PAY, 63).index('"1"') + 1
        assert e63["enclosing_urn"] == f"lk://{WS_ID}/{PAY}#PaymentProcessor.apply_discount"
        assert e63["confidence_level"] == "exact"
        if have_ruff:
            f401 = next(d for d in got if d["source"] == "ruff" and d["code"] == "F401")
            assert f401["line"] == 5 and "`os` imported but unused" in f401["message"]
            assert f401["code_actions"] and store.get(f401["code_actions"][0]["id"]) is not None
        assert snapshot(root) == before
    finally:
        await pool.shutdown_all(500)
        await registry.terminate_all(500, close=True)


# --- 型の LSP が落ちていても lint の LSP が ready なら lint の診断を返す（BUG-0039）-------------------


async def _only_lint_ready(pool: LspPool, ws: Workspace, *, lint_ms: int = 60_000) -> None:
    assert not await pool.wait_ready("python", ws, 5000)  # types 役: unavailable
    assert pool.status("python", ws, "types").status.value == "unavailable"
    assert await pool.wait_ready("python", ws, lint_ms, role="lint")


async def test_types_unavailable_lint_ready_returns_lint_diagnostics(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    """型の LSP が unavailable で lint の LSP が ready なら、lint の診断を返し、水準と警告は実際の経路を言う。"""
    pool = lsp.pool()
    original = pool._resolver
    pool._resolver = lambda defn, ws: None if defn.role == "types" else original(defn, ws)  # type: ignore[assignment]
    await _only_lint_ready(pool, lsp.ws(), lint_ms=20_000)
    pipe = make_pipeline(indexed, pool=pool, store=CodeActionStore())

    r = await call(pipe, "compact", paths=[UNI], min_severity="hint")
    assert not r.is_error, r.text
    assert g.validate(r.text) == [], r.text
    head = r.text.splitlines()[0]
    assert head.startswith("# get_diagnostics ok conf=structural lsp=unavailable rev=WORKING_TREE ws=poly ")
    assert f"W {UNI}:3:" in r.text and "[fakelint:W200]" in r.text  # lint 役の診断は返る
    assert "[fakels:" not in r.text  # 型役の診断は無い
    assert "fix=qf_" in next(ln for ln in r.text.splitlines() if ln.startswith(f"W {UNI}:3:"))
    # 警告は「構文エラーだけ」とは言わず、無いもの（型）と有るもの（Lint・構文）を分けて言う
    assert "構文エラー（Tree-sitter）だけを返しています" not in r.text
    assert "型の LSP" in r.text and "Lint" in r.text and "含まれていません" in r.text

    j = await call(pipe, paths=[UNI, BROKEN], min_severity="hint")
    got = diags_of(j)
    assert {d["source"] for d in got} == {"fakelint", "tree-sitter"}
    lint = next(d for d in got if d["source"] == "fakelint")
    assert lint["confidence_level"] == "exact" and lint["code_actions"]
    meta = j.structured["_resolution_meta"]
    assert (meta["lsp_status"], meta["confidence_level"], meta["is_degraded"]) == (
        "unavailable",
        "structural",
        True,
    )
    assert meta["accuracy_warning"] and "型の LSP" in meta["accuracy_warning"]
    assert "Lint の診断は含まれていません" not in meta["accuracy_warning"]
    servers = {(s["language"], s["role"]): s["status"] for s in j.structured["data"]["lsp_sources"]}
    assert servers == {("python", "types"): "unavailable", ("python", "lint"): "ready"}
    assert j.structured["data"]["summary"]["by_source"] == {"fakelint": 1, "tree-sitter": 1}


async def test_types_indexing_lint_ready_returns_lint_diagnostics(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    """初回の呼び出し（型の LSP は起動中・ruff はもう ready）でも lint の診断を返す。型は完了後に exact になる。"""
    pool = lsp.pool()
    base = pool._resolver

    def slow_types(defn: LspServerDef, ws: Workspace) -> list[str] | None:
        argv = base(defn, ws)
        return argv + ["--init-delay", "2.5"] if defn.role == "types" and argv else argv

    pool._resolver = slow_types  # type: ignore[assignment]
    assert await pool.wait_ready("python", lsp.ws(), 20_000, role="lint")
    pipe = make_pipeline(indexed, pool=pool, store=CodeActionStore())
    r = await call(pipe, "compact", paths=[UNI], min_severity="hint", include_code_actions=False)
    assert r.text.splitlines()[0].startswith("# get_diagnostics ok conf=structural lsp=indexing ")
    assert f"W {UNI}:3:" in r.text and "[fakelint:W200]" in r.text and "[fakels:" not in r.text
    assert "起動・索引中" in r.text and "型の LSP" in r.text and "構文エラー（Tree-sitter）だけ" not in r.text
    await ready(pool, lsp.ws())
    j = await call(pipe, paths=[UNI], min_severity="hint", include_code_actions=False)
    assert j.structured["_resolution_meta"]["confidence_level"] == "exact"
    assert {d["source"] for d in diags_of(j)} == {"fakels", "fakelint"}


async def test_types_ready_lint_unavailable_says_lint_is_missing(
    indexed: tuple[Path, Path], lsp: LspEnv
) -> None:
    """逆の組: 型の LSP が ready で lint が unavailable なら、型の診断を返し、Lint が無いことを警告に書く。"""
    pool = lsp.pool()
    original = pool._resolver
    pool._resolver = lambda defn, ws: None if defn.role == "lint" else original(defn, ws)  # type: ignore[assignment]
    assert await pool.wait_ready("python", lsp.ws(), 20_000)
    assert not await pool.wait_ready("python", lsp.ws(), 5000, role="lint")
    r = await call(
        make_pipeline(indexed, pool=pool), paths=[UNI], min_severity="hint", include_code_actions=False
    )
    got = diags_of(r)
    assert {d["source"] for d in got} == {"fakels"}
    meta = r.structured["_resolution_meta"]
    assert meta["lsp_status"] == "ready"
    warnings = r.structured["warnings"]
    assert any("Lint" in w and "含まれていません" in w for w in warnings), warnings


_HAVE_RUFF = any((_VENV / f"ruff{ext}").exists() for ext in ("", ".exe", ".cmd"))


@pytest.mark.skipif(not _HAVE_RUFF, reason="ruff is not installed in this environment")
async def test_real_ruff_ready_with_types_lsp_unavailable(indexed: tuple[Path, Path]) -> None:
    """実の ruff だけが ready のとき（型の LSP は起動に失敗）、ruff の診断（E401・F401）が返る。BUG-0039。"""
    root, _ = indexed
    registry = ProcessRegistry()
    defs = (servers_for("python", "types")[0], servers_for("python", "lint")[0])  # basedpyright, ruff

    def resolver(defn: LspServerDef, ws: Workspace) -> list[str] | None:
        return None if defn.role == "types" else default_resolver(defn, ws)  # 型の LSP は見つからない

    pool = LspPool(registry, servers=defs, resolver=resolver, ready_fallback_s=5.0, grace_ms=500)
    ws = Workspace(
        workspace_id=WS_ID,
        root=root,
        python_env=PythonEnv(interpreter=sys.executable),
        case_insensitive=IS_WINDOWS,
    )
    try:
        await _only_lint_ready(pool, ws)
        pipe = make_pipeline(indexed, pool=pool, store=CodeActionStore(), diagnostics_wait_ms=60_000)
        before = snapshot(root)
        r = await call(pipe, paths=[LINTY], min_severity="hint")
        got = diags_of(r)
        # ruff の既定の規則は版で違う（0.17 は E401 を既定にせず I001 を足す）。F401 と、1 行 import の指摘をもう 1 つ見る
        codes = {d["code"] for d in got}
        assert all(d["source"] == "ruff" for d in got) and "F401" in codes and codes & {"E401", "I001"}, got
        f401 = next(d for d in got if d["code"] == "F401")
        assert f401["line"] == 1 and f401["snippet"] == "import os, sys"
        assert f401["confidence_level"] == "exact" and f401["code_actions"]
        meta = r.structured["_resolution_meta"]
        assert (meta["lsp_status"], meta["confidence_level"]) == ("unavailable", "structural")
        assert "構文エラー（Tree-sitter）だけを返しています" not in " ".join(r.structured["warnings"])
        servers = {(s["language"], s["role"]): s["status"] for s in r.structured["data"]["lsp_sources"]}
        assert servers == {("python", "types"): "unavailable", ("python", "lint"): "ready"}
        default = diags_of(await call(pipe, paths=[LINTY]))  # 既定の min_severity（warning）でも見える
        assert "F401" in {d["code"] for d in default}
        assert snapshot(root) == before
    finally:
        await pool.shutdown_all(500)
        await registry.terminate_all(500, close=True)



# ``LspPool`` の ``ready_fallback_s`` の既定（BUG-0050 の退行はこの秒数まで ruff の診断を返せない）
_DEFAULT_READY_FALLBACK_S = inspect.signature(LspPool.__init__).parameters["ready_fallback_s"].default


@pytest.mark.skipif(not _HAVE_RUFF, reason="ruff is not installed in this environment")
async def test_real_ruff_answers_within_one_second_of_start_with_types_lsp_down(
    indexed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """型の LSP が起動に失敗しても、最初の ``get_diagnostics`` から 1 秒以内に ruff の診断が返る。BUG-0050。

    ruff は ``$/progress`` を出さず、診断も文書を開くまで出さない。``ready`` の合図が「最初の ``publishDiagnostics`` か
    ``ready_fallback_s``」だった間は、``ready`` でないハンドルに文書を開かせない T-08 が約 15 秒（既定の
    ``ready_fallback_s``。ここでも既定のまま）ruff の診断を返せなかった。``initialize`` の応答を合図にして、それが無くなった。

    合否は「既定の ``ready_fallback_s``（15 秒）の半分より十分前に返る」で見る（BUG-0089）。実時間 1 秒の上限は負荷の高い
    全体テストで 1.1 秒になって落ちた（揺れる）。退行（約 15 秒）とは 7.5 秒の境で確実に分かれる。1 秒の目標は計測（bench）で見る。"""
    root, _ = indexed
    for var in ("LUCIANKE_LSP_BASEDPYRIGHT_CMD", "LUCIANKE_LSP_PYRIGHT_CMD"):
        monkeypatch.setenv(var, str(root / "no-such-language-server"))  # 型の LSP は init_failed
    registry = ProcessRegistry()
    pool = LspPool(registry, grace_ms=500)  # ready_fallback_s は既定（15 秒）のまま
    try:
        pipe = make_pipeline(indexed, pool=pool, store=CodeActionStore(), diagnostics_wait_ms=10_000)
        t0 = time.perf_counter()
        await call(pipe, paths=[LINTY], min_severity="hint")  # 起動の契機（この呼び出し自体は降格でよい）
        got: list[dict[str, Any]] = []
        r: Any = None
        limit_s = _DEFAULT_READY_FALLBACK_S / 2  # 退行（ready_fallback_s まで待つ）なら必ず超える。負荷では揺れない
        while time.perf_counter() - t0 < limit_s:
            r = await call(pipe, paths=[LINTY], min_severity="hint")
            got = diags_of(r)
            if any(d["source"] == "ruff" for d in got):
                break
            await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - t0
        assert any(d["source"] == "ruff" and d["code"] == "F401" for d in got), (elapsed, got)
        assert elapsed < limit_s, elapsed
        servers = {(s["language"], s["role"]): s["status"] for s in r.structured["data"]["lsp_sources"]}
        assert servers == {("python", "types"): "unavailable", ("python", "lint"): "ready"}
    finally:
        await pool.shutdown_all(500)
        await registry.terminate_all(500, close=True)
