"""T-06・T-07 の LSP ``exact`` 経路: 最初の診断の前の答えを覚えない（20e の 2）、目印の永続化（20e の 3）、T-07 の incoming の事前オープン。

偽の LSP サーバー（ ``FAKE_SERVER`` ）で確かめる。実の basedpyright / typescript-language-server は要らない。

* ``publish``: ``didOpen`` の直後に診断を出す（basedpyright のように、その文書の解析が済んだ合図を出す LSP）。
* ``late``: 診断を 0.8 秒遅れて出し、診断の前の ``references`` / ``incomingCalls`` は空で答える（起動直後の部分的な答え）。
* ``never``: 診断を出さない。答えは常に完全。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_T06_T07_lsp import Env, build_env, close_env

from lucianke.core.procs import ProcessRegistry
from lucianke.core.urn import CodeSel, Urn
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import LspServerDef
from lucianke.tools import _lsp_exact
from lucianke.workspace.types import Workspace

FAKE_SERVER = r"""
import json
import sys
import threading

CFG = json.loads(sys.argv[1])
inp, out = sys.stdin.buffer, sys.stdout.buffer
lock = threading.Lock()
opened, published = set(), set()


def log(line):
    with open(CFG["log"], "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read():
    length = None
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.decode("ascii").partition(":")
        if key.lower() == "content-length":
            length = int(value)
    return json.loads(inp.read(length))


def send(msg):
    body = json.dumps(msg).encode("utf-8")
    with lock:
        out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        out.flush()


def publish(uri):
    published.add(uri)
    diag = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
            "severity": 2, "message": "fake warning"}
    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": [diag]}})


def uri_of(base_uri, rel):
    return base_uri[: base_uri.rindex(CFG["target_rel"])] + rel


def rng(a):
    return {"start": {"line": a[0], "character": a[1]}, "end": {"line": a[2], "character": a[3]}}


def target_item(base):
    return {"name": CFG["name"], "kind": 12, "uri": uri_of(base, CFG["target_rel"]),
            "range": rng(CFG["target_range"]), "selectionRange": rng(CFG["target_sel"])}


def ready_for(uri):
    mode = CFG["mode"]
    if mode == "late":
        return uri in published
    return True


while True:
    msg = read()
    if msg is None:
        break
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        break
    elif method == "textDocument/didOpen":
        uri = params["textDocument"]["uri"]
        opened.add(uri)
        log("open " + uri.rsplit("/", 2)[-2] + "/" + uri.rsplit("/", 1)[-1])
        if CFG["mode"] == "publish":
            publish(uri)
        elif CFG["mode"] == "late":
            threading.Timer(0.8, publish, args=(uri,)).start()
    elif method == "textDocument/references":
        base = params["textDocument"]["uri"]
        log("references")
        locs = []
        if ready_for(base):
            locs = [{"uri": uri_of(base, CFG["caller_rel"]), "range": rng(CFG["call_site"])}]
        send({"jsonrpc": "2.0", "id": mid, "result": locs})
    elif method == "textDocument/prepareCallHierarchy":
        log("prepare")
        send({"jsonrpc": "2.0", "id": mid, "result": [target_item(params["textDocument"]["uri"])]})
    elif method == "callHierarchy/incomingCalls":
        log("incoming")
        item = params["item"]
        base = item["uri"]
        have = CFG.get("needs_open")
        edges = []
        if ready_for(base) and (not have or uri_of(base, have) in opened):
            caller = {"name": CFG["caller_name"], "kind": 12, "uri": uri_of(base, CFG["caller_rel"]),
                      "range": rng(CFG["caller_range"]), "selectionRange": rng(CFG["caller_sel"])}
            edges = [{"from": caller, "fromRanges": [rng(CFG["call_site"])]}]
        send({"jsonrpc": "2.0", "id": mid, "result": edges})
    elif method == "callHierarchy/outgoingCalls":
        send({"jsonrpc": "2.0", "id": mid, "result": []})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": None})
"""

PY_DEF = LspServerDef(
    language_id="python", name="fake", argv_candidates=(("fake",),), locale="en", requests_m1=frozenset()
)
TS_DEF = LspServerDef(
    language_id="typescript",
    name="fake-ts",
    argv_candidates=(("fake-ts",),),
    locale="en",
    requests_m1=frozenset(),
)

PY_FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "def target():\n    return 1\n",
    "pkg/b.py": "from pkg.a import target\n\n\ndef caller():\n    return target()\n",
}
PY_CFG: dict[str, Any] = {
    "name": "target",
    "target_rel": "pkg/a.py",
    "caller_rel": "pkg/b.py",
    "caller_name": "caller",
    "target_range": [0, 0, 1, 12],
    "target_sel": [0, 4, 0, 10],
    "caller_range": [3, 0, 4, 19],
    "caller_sel": [3, 4, 3, 10],
    "call_site": [4, 11, 4, 17],
}
PY_TARGET = {"urn": str(Urn("fk", "pkg/a.py", CodeSel("target")))}

TS_FILES = {
    "package.json": '{ "name": "tsfk", "type": "module" }\n',
    "tsconfig.json": '{ "compilerOptions": { "strict": true }, "include": ["source"] }\n',
    "source/guard.ts": "export function check(value: unknown): boolean {\n\treturn value !== null;\n}\n",
    "test/a.test.ts": (
        "import {check} from '../source/guard.js';\n\n"
        "export function runs(): boolean {\n\treturn check(1);\n}\n"
    ),
}
TS_CFG: dict[str, Any] = {
    "name": "check",
    "target_rel": "source/guard.ts",
    "caller_rel": "test/a.test.ts",
    "caller_name": "runs",
    "target_range": [0, 0, 2, 1],
    "target_sel": [0, 16, 0, 21],
    "caller_range": [2, 0, 4, 1],
    "caller_sel": [2, 16, 2, 20],
    "call_site": [3, 8, 3, 13],
    "needs_open": "test/a.test.ts",
}
TS_TARGET = {"urn": str(Urn("tsfk", "source/guard.ts", CodeSel("check")))}


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


class Fake:
    """偽サーバーを使うワークスペース 1 つ分。``start()`` で LSP 付きの環境、``restart()`` で LSP なしの新しい環境（再起動の模擬）。"""

    def __init__(
        self,
        registry: ProcessRegistry,
        tmp_path: Path,
        *,
        ws_id: str,
        language: str,
        defn: LspServerDef,
        files: dict[str, str],
        cfg: dict[str, Any],
    ) -> None:
        self.registry, self.language, self.defn = registry, language, defn
        self.root = tmp_path / ws_id
        write_tree(self.root, files)
        self.ws = Workspace(workspace_id=ws_id, root=self.root.resolve())
        self.cache = tmp_path / "cache"
        self.cache.mkdir()
        self.log = tmp_path / "fake_lsp.log"
        self.script = tmp_path / "fake_lsp.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.cfg = {**cfg, "log": str(self.log)}
        self.env: Env | None = None

    def lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def pool(self, mode: str | None) -> LspPool:
        if mode is None:  # 起動しない LSP（再起動後に LSP が無い状況）
            return LspPool(
                self.registry,
                servers=[self.defn],
                resolver=lambda d, w: None,
                ready_fallback_s=0.2,
                grace_ms=200,
            )
        argv = [sys.executable, str(self.script), json.dumps({**self.cfg, "mode": mode})]
        return LspPool(
            self.registry,
            servers=[self.defn],
            resolver=lambda d, w: argv,
            ready_fallback_s=0.2,
            grace_ms=200,
        )

    async def start(self, mode: str | None) -> Env:
        pool = self.pool(mode)
        self.env = await build_env(self.registry, self.root, self.cache, pool, self.ws)
        if mode is not None:
            await pool.ensure_started(self.language, self.ws)
            assert await pool.wait_ready(self.language, self.ws, 15_000)
        return self.env

    async def stop(self) -> None:
        if self.env is not None:
            await close_env(self.env, self.registry)
            self.env = None

    def marks(self) -> list[tuple[Any, ...]]:
        assert self.env is not None
        return self.env.sql("SELECT kind, urn FROM _exact_marks ORDER BY 1, 2")


def meta(res: Any) -> dict[str, Any]:
    assert not res.is_error, res.text
    return res.structured["_resolution_meta"]


def ref_sites(res: Any) -> list[tuple[str, int]]:
    return [(r["path"], r["line"]) for r in res.structured["data"]["references"]]


@pytest.fixture
async def py(registry: ProcessRegistry, tmp_path: Path):
    _lsp_exact.MEMO.clear()
    fake = Fake(registry, tmp_path, ws_id="fk", language="python", defn=PY_DEF, files=PY_FILES, cfg=PY_CFG)
    yield fake
    await fake.stop()
    _lsp_exact.MEMO.clear()


@pytest.fixture
async def ts(registry: ProcessRegistry, tmp_path: Path):
    _lsp_exact.MEMO.clear()
    fake = Fake(
        registry, tmp_path, ws_id="tsfk", language="typescript", defn=TS_DEF, files=TS_FILES, cfg=TS_CFG
    )
    yield fake
    await fake.stop()
    _lsp_exact.MEMO.clear()


# --- 2: 最初の診断の前の答えを覚えない ---------------------------------------------------------------


async def test_t06_waits_for_the_first_diagnostic_and_then_answers_exact_and_remembers(py: Fake) -> None:
    """診断の前の ``references`` は空（起動直後の部分的な答え）。ツールは最初の診断を待ってから聞くので、完全な答えが
    ``exact`` で返り、目印が置かれる（以前は 0 件を信用せず structural に降格していた）。"""
    env = await py.start("late")
    res = await env.refs(PY_TARGET)
    m = meta(res)
    assert m["confidence_level"] == "exact" and "fake" in m["engine_used"], m
    assert ref_sites(res) == [("pkg/b.py", 5)]
    assert py.lines()[:2] == ["open pkg/a.py", "references"]  # 開く → (診断を待つ) → 聞く
    assert py.marks() == [("refs", PY_TARGET["urn"])]

    await env.stop_lsp()
    again = await env.refs(PY_TARGET)
    assert meta(again)["engine_used"] == "lsp-cache+tree-sitter" and ref_sites(again) == ref_sites(res)


async def test_t06_does_not_remember_an_answer_given_before_the_first_diagnostic(
    py: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """診断が来ない LSP: 待ちの上限のあと、その回は ``exact`` で答えるが目印は置かない。LSP を止めた 2 回目は PaaD の exact では答えない。"""
    monkeypatch.setattr(_lsp_exact, "FIRST_DIAGNOSTIC_WAIT_S", 0.5)
    env = await py.start("never")
    res = await env.refs(PY_TARGET)
    m = meta(res)
    assert m["confidence_level"] == "exact" and "fake" in m["engine_used"], m
    assert ref_sites(res) == [("pkg/b.py", 5)]
    assert py.marks() == []
    assert _lsp_exact.MEMO.get(next(iter(_memo_keys(env, "refs")))) is None

    await env.stop_lsp()
    again = await env.refs(PY_TARGET)
    assert meta(again)["engine_used"] != "lsp-cache+tree-sitter"
    assert meta(again)["confidence_level"] != "exact"


def _memo_keys(env: Env, kind: str):
    yield _lsp_exact.memo_key(env.db, env.ws.workspace_id, kind, PY_TARGET["urn"])


async def test_t07_does_not_remember_an_answer_given_before_the_first_diagnostic(
    py: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lsp_exact, "FIRST_DIAGNOSTIC_WAIT_S", 0.5)
    env = await py.start("never")
    res = await env.calls(PY_TARGET, "incoming", depth=1)
    m = meta(res)
    assert m["confidence_level"] == "exact" and "fake" in m["engine_used"], m
    assert [n["name"] for n in res.structured["data"]["nodes"]] == ["caller"]
    assert py.marks() == []
    await env.stop_lsp()
    again = await env.calls(PY_TARGET, "incoming", depth=1)
    assert meta(again)["engine_used"] != "lsp-cache+tree-sitter"


async def test_t07_waits_for_the_first_diagnostic_and_remembers(py: Fake) -> None:
    env = await py.start("late")
    res = await env.calls(PY_TARGET, "incoming", depth=1)
    m = meta(res)
    assert m["confidence_level"] == "exact" and "fake" in m["engine_used"], m
    assert [n["name"] for n in res.structured["data"]["nodes"]] == ["caller"]
    assert py.lines()[:2] == ["open pkg/a.py", "prepare"]
    assert py.marks() == [("calls_in", PY_TARGET["urn"])]


# --- 3: 目印の永続化 ---------------------------------------------------------------------------------


async def test_t06_exact_answer_survives_a_restart_without_the_lsp(py: Fake) -> None:
    env = await py.start("publish")
    first = await env.refs(PY_TARGET)
    assert meta(first)["confidence_level"] == "exact" and "fake" in meta(first)["engine_used"]
    assert py.marks() == [("refs", PY_TARGET["urn"])]
    await py.stop()

    _lsp_exact.MEMO.clear()  # プロセスの再起動: 目印はメモリに無い。LSP も起動しない
    env2 = await py.start(None)
    second = await env2.refs(PY_TARGET)
    m = meta(second)
    assert m["confidence_level"] == "exact" and m["engine_used"] == "lsp-cache+tree-sitter", m
    assert ref_sites(second) == ref_sites(first)
    assert env2.pool.status("python", env2.ws).status.value != "ready"
    assert py.lines().count("references") == 1  # 再起動後は LSP に聞いていない


async def test_t07_exact_answer_survives_a_restart_without_the_lsp(py: Fake) -> None:
    env = await py.start("publish")
    first = await env.calls(PY_TARGET, "incoming", depth=1)
    assert meta(first)["confidence_level"] == "exact" and "fake" in meta(first)["engine_used"]
    assert py.marks() == [("calls_in", PY_TARGET["urn"])]
    await py.stop()

    _lsp_exact.MEMO.clear()
    env2 = await py.start(None)
    second = await env2.calls(PY_TARGET, "incoming", depth=1)
    m = meta(second)
    assert m["confidence_level"] == "exact" and m["engine_used"] == "lsp-cache+tree-sitter", m
    assert [n["name"] for n in second.structured["data"]["nodes"]] == ["caller"]
    assert py.lines().count("incoming") == 1


async def test_a_file_changed_while_the_server_was_down_voids_the_stored_mark(py: Fake) -> None:
    env = await py.start("publish")
    assert meta(await env.refs(PY_TARGET))["confidence_level"] == "exact"
    await py.stop()

    path = py.root / "pkg" / "b.py"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n\ndef other():\n    return target()\n", encoding="utf-8"
    )
    _lsp_exact.MEMO.clear()
    env2 = await py.start(None)  # 起動時の走査がファイルの変更を取り込む
    res = await env2.refs(PY_TARGET)
    m = meta(res)
    assert m["engine_used"] != "lsp-cache+tree-sitter", m  # ファイルの指紋が変わった: 目印は使わない
    assert m["confidence_level"] != "exact"


# --- T-07 の incoming: 参照元になりうるファイルを先に開く（tsserver 系。BUG-0047 と同じ制約）---------------------


async def test_t07_incoming_opens_the_files_that_mention_the_name_before_asking(ts: Fake) -> None:
    """tsserver は ``tsconfig`` の ``include`` に入らない ``test/`` を、開かれるまで呼び元に出さない。偽サーバーは ``test/a.test.ts`` が
    ``didOpen`` 済みのときだけ呼び元を返す。ツールが先に開くので ``runs``（テストの関数）が incoming に出る。"""
    env = await ts.start("publish")
    res = await env.calls(TS_TARGET, "incoming", depth=1)
    m = meta(res)
    assert m["confidence_level"] == "exact" and "fake-ts" in m["engine_used"], m
    nodes = res.structured["data"]["nodes"]
    assert [(n["name"], n["is_test"]) for n in nodes] == [("runs", True)], nodes
    lines = ts.lines()
    assert lines.index("open test/a.test.ts") < lines.index("incoming"), lines
    assert not any("were not opened" in w for w in res.structured.get("warnings", []))
    # 診断を待つのは Python だけ（診断の無いファイルに何も届けないサーバーがある）: 目印は置かれ、2 回目は PaaD から
    assert ts.marks() == [("calls_in", TS_TARGET["urn"])]
    await env.stop_lsp()
    again = await env.calls(TS_TARGET, "incoming", depth=1)
    assert meta(again)["engine_used"] == "lsp-cache+tree-sitter"
    assert [n["name"] for n in again.structured["data"]["nodes"]] == ["runs"]


async def test_t07_outgoing_does_not_open_the_other_files(ts: Fake) -> None:
    env = await ts.start("publish")
    res = await env.calls(TS_TARGET, "outgoing", depth=1)
    assert not res.is_error, res.text
    assert "open test/a.test.ts" not in ts.lines()


async def test_t07_incoming_reports_the_files_it_could_not_open(
    ts: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lsp_exact, "PRIME_MAX", 0)
    ts.cfg["needs_open"] = None  # 開かなくても答える LSP: 開けなかった件数の警告だけを確かめる
    env = await ts.start("publish")
    res = await env.calls(TS_TARGET, "incoming", depth=1)
    assert not res.is_error, res.text
    warnings = res.structured.get("warnings", [])
    assert any("were not opened for the LSP" in w for w in warnings), warnings
