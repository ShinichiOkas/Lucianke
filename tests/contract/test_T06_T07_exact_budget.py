"""BUG-0091: T-06・T-07 の exact 経路が LSP の時間の上限で切れたときの答えと警告、連続時間切れの数え（偽 LSP で遅れを決めて起こす）。

* 偽サーバー（``SCRIPTED_SERVER``）は呼び出し階層と references だけに答える（references の遅れは ``refs_<名前>``）。遅れはノードの名前ごとに設定ファイル（要求のたびに読み直す）で決める。
  遅い答えは別のスレッドで返すので、待っている間も次の要求を受ける（取り消された要求の遅い答えはクライアントが捨てる）。
  ``didOpen`` にはすぐ空の診断を返す（最初の診断の待ちは一瞬）。予算・期限は負荷があっても根の要求が切れない大きさにし、
  遅いノードはそれより十分遅くする（``SLOW_S``）。
* 木: ``target`` <- ``caller``（``pkg/b.py``）<- ``top``（``pkg/c.py``）・``other``（``pkg/d.py``）。索引（structural）にも同じ辺がある。
* 確かめること:
  1. 1 問い合わせの予算（``EXACT_BUDGET_S``）が切れた: LSP が答えたノードは exact のまま、残りのノードの子を構文木で補う（mixed）。
     警告は予算とその値・どこまで exact か・呼び直すことを書き、要求の期限を理由にしない。呼び直すと exact で答えきる。
  2. 深いノードの要求が要求 1 つの期限（``LSP_TIMEOUT_CALL_HIERARCHY_MS``）で切れた: 警告はその要求・期限の値・ノードを書き、
     予算を理由にしない。
  3. 根の子が取れない（予算の内に根が答えない）: 木ごと structural。警告は予算の値と、呼び直すことを書く。
  4. 根の要求は要求 1 つの期限ではなく予算の残りまで待つ。予算の内に遅れて答えた根・予算が期限より短くて切れた根は連続時間切れ
     （3 回で unavailable）に数えない。要求 1 つの期限以上待っても答えなかった根（本当の無応答）は数える。
  5. T-06 の references も同じ: 要求 1 つの期限（3000 ms）ではなく予算の残りまで待ち、予算の内の遅れは数えない。予算の内に答えない
     ときは本当の無応答として数え、警告は予算とその値・呼び直すことを書く。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_T06_T07_lsp import Env, opened_env

from lucianke.core.model import RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.core.urn import CodeSel, Urn
from lucianke.lsp import features
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import LspServerDef
from lucianke.tools import _lsp_exact
from lucianke.workspace.paths import to_file_uri
from lucianke.workspace.types import Workspace

READY_WAIT_MS = 60_000
BUDGET_S = 3.0  # 試験の 1 問い合わせの予算（根の要求は負荷があってもこの内に答える）
REQUEST_MS = 1500  # 試験の要求 1 つの期限（同上）
SLOW_S = 30.0  # 遅いノードの答えの遅れ（予算・期限より十分長い）

SCRIPTED_SERVER = r"""
import json
import sys
import threading
import time

inp, out = sys.stdin.buffer, sys.stdout.buffer
CONFIG = sys.argv[1]
LOCK = threading.Lock()


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
    with LOCK:
        out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        out.flush()


def answer_later(mid, seconds, result):
    time.sleep(seconds)
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


while True:
    msg = read()
    if msg is None:
        break
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"callHierarchyProvider": True}}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        break
    elif method == "textDocument/didOpen":
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": params["textDocument"]["uri"], "diagnostics": []}})
    elif method == "textDocument/prepareCallHierarchy":
        cfg = config()
        uri, line = params["textDocument"]["uri"], params["position"]["line"]
        found = [it for it in cfg["items"].values()
                 if it["uri"] == uri and it["range"]["start"]["line"] <= line <= it["range"]["end"]["line"]]
        send({"jsonrpc": "2.0", "id": mid, "result": found})
    elif method == "textDocument/references":
        cfg = config()
        uri, line = params["textDocument"]["uri"], params["position"]["line"]
        name = next((n for n, it in cfg["items"].items()
                     if it["uri"] == uri and it["range"]["start"]["line"] <= line <= it["range"]["end"]["line"]), "")
        locs = cfg["refs"].get(name, [])
        delay = cfg["delay"].get("refs_" + name, 0.0)
        if delay:
            threading.Thread(target=answer_later, args=(mid, delay, locs), daemon=True).start()
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": locs})
    elif method == "callHierarchy/incomingCalls":
        cfg = config()
        name = params["item"]["name"]
        calls = [{"from": cfg["items"][c["from"]], "fromRanges": c["ranges"]} for c in cfg["incoming"].get(name, [])]
        delay = cfg["delay"].get(name, 0.0)
        if delay:
            threading.Thread(target=answer_later, args=(mid, delay, calls), daemon=True).start()
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": calls})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": None})
"""

SCRIPTED_DEF = LspServerDef(
    language_id="python",
    name="scripted",
    argv_candidates=(("scripted",),),
    locale="en",
    requests_m1=frozenset(),
)

FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "def target():\n    return 1\n",
    "pkg/b.py": "from pkg.a import target\n\n\ndef caller():\n    return target()\n",
    "pkg/c.py": "from pkg.b import caller\n\n\ndef top():\n    return caller()\n",
    "pkg/d.py": "from pkg.b import caller\n\n\ndef other():\n    return caller()\n",
}
# 名前 → (ファイル, def の行〔0 始まり〕, 呼び出す相手, 呼び出しの行)
DEFS = {
    "target": ("pkg/a.py", 0, None, None),
    "caller": ("pkg/b.py", 3, "target", 4),
    "top": ("pkg/c.py", 3, "caller", 4),
    "other": ("pkg/d.py", 3, "caller", 4),
}
CALL_COL = len("    return ")
TARGET = {"urn": str(Urn("fk", "pkg/a.py", CodeSel("target")))}
COMPACT = {"direction": "incoming", "depth": 2, "format": "compact"}


def _rng(line: int, start: int, end: int) -> dict[str, Any]:
    return {"start": {"line": line, "character": start}, "end": {"line": line, "character": end}}


def write_config(path: Path, ws: Workspace, delay: dict[str, float]) -> None:
    items: dict[str, Any] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    refs: dict[str, list[dict[str, Any]]] = {}
    for name, (rel, line, callee, call_line) in DEFS.items():
        items[name] = {
            "name": name,
            "kind": 12,
            "uri": to_file_uri(ws, RelPath(rel)),
            "range": {"start": {"line": line, "character": 0}, "end": {"line": line + 1, "character": 99}},
            "selectionRange": _rng(line, 4, 4 + len(name)),
        }
        if callee is not None and call_line is not None:
            site = _rng(call_line, CALL_COL, CALL_COL + len(callee))
            incoming.setdefault(callee, []).append({"from": name, "ranges": [site]})
            refs.setdefault(callee, []).append({"uri": items[name]["uri"], "range": site})
    body = {"items": items, "incoming": incoming, "refs": refs, "delay": delay}
    path.write_text(json.dumps(body), encoding="utf-8")


class Scripted:
    def __init__(self, env: Env, config: Path) -> None:
        self.env, self.config = env, config

    def delay(self, **seconds: float) -> None:
        write_config(self.config, self.env.ws, seconds)

    async def tree(self, depth: int = 2, root: str = "target") -> dict[str, Any]:
        rel = DEFS[root][0]
        r = await self.env.calls({"urn": str(Urn("fk", rel, CodeSel(root)))}, "incoming", depth=depth)
        assert not r.is_error, r.text
        return r.structured

    async def refs(self, root: str = "target") -> dict[str, Any]:
        r = await self.env.refs({"urn": str(Urn("fk", DEFS[root][0], CodeSel(root)))})
        assert not r.is_error, r.text
        return r.structured

    def timeouts(self) -> int:
        handle = self.env.pool.get("python", self.env.ws)
        assert handle is not None
        return handle.timeouts


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


@pytest.fixture
async def scripted(registry: ProcessRegistry, tmp_path: Path):
    _lsp_exact.MEMO.clear()
    root = tmp_path / "fk"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    script = tmp_path / "scripted_lsp.py"
    script.write_text(SCRIPTED_SERVER, encoding="utf-8")
    config = tmp_path / "scripted.json"
    ws = Workspace(workspace_id="fk", root=root.resolve())
    write_config(config, ws, {})
    pool = LspPool(
        registry,
        servers=[SCRIPTED_DEF],
        resolver=lambda defn, w: [sys.executable, str(script), str(config)],
        ready_fallback_s=0.2,
        grace_ms=200,
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    async with opened_env(registry, root, cache, pool, ws) as env:
        await pool.ensure_started("python", ws)
        assert await pool.wait_ready("python", ws, READY_WAIT_MS)
        yield Scripted(env, config)


def levels(r: dict[str, Any]) -> dict[str, str]:
    return {n["name"]: n["confidence_level"] for n in r["data"]["nodes"]}


def node(r: dict[str, Any], name: str) -> dict[str, Any]:
    return next(n for n in r["data"]["nodes"] if n["name"] == name)


def note(r: dict[str, Any], prefix: str) -> str:
    return next(w for w in r["warnings"] if w.startswith(prefix))


async def test_fake_lsp_answers_the_whole_tree_exact_without_limits(scripted: Scripted) -> None:
    """前提: 遅れが無ければ木全体が exact（偽 LSP の結線の確かめ）。"""
    r = await scripted.tree()
    meta = r["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["accuracy_warning"] is None, meta
    assert levels(r) == {"caller": "exact", "top": "exact", "other": "exact"}
    assert "children_confidence_level" not in node(r, "caller")


async def test_budget_cut_keeps_answered_nodes_exact_and_names_the_budget(
    scripted: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1: caller の incomingCalls が予算（3 秒）の内に答えない → caller（depth 1）は exact、その子は構文木で補う。"""
    monkeypatch.setattr(_lsp_exact, "EXACT_BUDGET_S", BUDGET_S)
    scripted.delay(caller=SLOW_S)
    r = await scripted.tree()
    meta = r["_resolution_meta"]
    assert levels(r) == {"caller": "exact", "top": "structural", "other": "structural"}
    assert meta["confidence_level"] == "structural" and meta["is_degraded"] is True, meta
    assert meta["mixed"] is True and meta["confidence_breakdown"] == {"exact": 1, "structural": 2}, meta
    assert meta["lsp_status"] == "ready", meta  # LSP は落とさない
    assert node(r, "caller")["children_confidence_level"] == "structural"
    warning = meta["accuracy_warning"] or ""
    # 実際に切れた上限（予算 3 秒）を書き、要求の期限を理由にしない
    assert "予算 3 秒（この問い合わせで LSP に使える時間の合計）を使い切りました" in warning, warning
    assert "内に答えませんでした" not in warning and "内に得られなかった" not in warning, warning
    assert "2 ノードのうち 1 ノード" in warning and "depth 1 まで" in warning and "caller" in warning, warning
    assert "呼び直す" in warning, warning
    line = note(r, "exact LSP answers")
    assert "1/2 expanded nodes" in line and "complete to depth 1" in line, line
    assert "3 s exact budget" in line and "call again" in line, line
    assert scripted.timeouts() == 0  # 予算で取り消した要求は、連続時間切れ（3 回で unavailable）に数えない
    # compact でも、子を構文木で補ったノードの下に印が出る（どこから下が structural か読める）
    text = (await scripted.env.pipeline.call_tool("query_call_hierarchy", {**COMPACT, "target": TARGET})).text
    lines = text.splitlines()
    at = next(i for i, x in enumerate(lines) if x.startswith("<- caller "))
    assert (
        lines[at + 1].strip() == "- children: from the syntax index (conf=structural; the LSP did not answer)"
    ), text
    assert "! DEGRADED" in text and "予算 3 秒" in text, text

    # 呼び直すと（LSP が速く答えるようになった）、残りのノードを聞いて exact で答えきる
    scripted.delay()
    again = await scripted.tree()
    meta2 = again["_resolution_meta"]
    assert meta2["confidence_level"] == "exact" and meta2["accuracy_warning"] is None, meta2
    assert levels(again) == {"caller": "exact", "top": "exact", "other": "exact"}
    assert not [w for w in again["warnings"] if w.startswith("exact LSP answers")]


async def test_request_timeout_names_the_request_and_its_limit(
    scripted: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2: caller の incomingCalls が要求の期限（1500 ms）の内に答えない → 警告は incomingCalls・1500 ms・caller。予算ではない。"""
    monkeypatch.setattr(features, "LSP_TIMEOUT_CALL_HIERARCHY_MS", REQUEST_MS)
    scripted.delay(caller=SLOW_S)
    r = await scripted.tree()
    meta = r["_resolution_meta"]
    assert levels(r) == {"caller": "exact", "top": "structural", "other": "structural"}
    assert meta["is_degraded"] is True and meta["lsp_status"] == "ready", meta
    warning = meta["accuracy_warning"] or ""
    assert "callHierarchy/incomingCalls" in warning, warning
    assert "要求 1 つの期限 1500 ms 内に答えませんでした" in warning, warning
    assert "（caller。" in warning and "予算は 10 秒" in warning, warning
    assert "使い切りました" not in warning and "5000 ms" not in warning, warning
    line = note(r, "exact LSP answers")
    assert "1500 ms per-request limit" in line and "callHierarchy/incomingCalls" in line, line


async def test_root_budget_cut_degrades_the_whole_tree_and_names_the_budget(
    scripted: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3: 根（target）の incomingCalls が予算（3 秒）の内に答えない → 木ごと structural、警告は予算 3 秒。"""
    monkeypatch.setattr(_lsp_exact, "EXACT_BUDGET_S", BUDGET_S)
    scripted.delay(target=SLOW_S)
    r = await scripted.tree()
    meta = r["_resolution_meta"]
    assert levels(r) == {"caller": "structural", "top": "structural", "other": "structural"}
    assert meta["is_degraded"] is True and meta["mixed"] is False and meta["lsp_status"] == "ready", meta
    warning = meta["accuracy_warning"] or ""
    assert (
        "予算 3 秒（この問い合わせで LSP に使える時間の合計）を使い切りました" in warning
        and "木全体を構文木" in warning
    ), warning
    assert "5000 ms" not in warning and "内に得られなかった" not in warning, warning
    assert "呼び直す" in warning, warning
    line = note(r, "exact call hierarchy unavailable")
    assert "3 s exact budget" in line and "call again" in line, line
    assert scripted.timeouts() == 0


async def test_a_cold_root_answering_in_7_seconds_is_exact_and_not_a_strike(scripted: Scripted) -> None:
    """4: 製品の既定（予算 10 秒・要求の期限 5000 ms）で、根の incomingCalls が 7 秒で答える → exact、連続時間切れに数えない。"""
    assert _lsp_exact.EXACT_BUDGET_S == 10.0 and features.LSP_TIMEOUT_CALL_HIERARCHY_MS == 5000
    scripted.delay(target=7.0)
    started = time.monotonic()
    r = await scripted.tree()
    assert time.monotonic() - started >= 7.0
    meta = r["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and meta["accuracy_warning"] is None, meta
    assert levels(r) == {"caller": "exact", "top": "exact", "other": "exact"}
    assert scripted.timeouts() == 0
    assert scripted.env.pool.status("python", scripted.env.ws).status.value == "ready"


async def test_three_slow_cold_roots_in_a_row_keep_the_lsp_available(
    scripted: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4: 根の要求が要求 1 つの期限（1000 ms に縮めた）を超えて予算（4 秒）の内に答えるのが 3 回続く → 3 回とも exact、LSP は ready。

    以前は根の要求も要求 1 つの期限で切れ、3 回続くと LSP を落としていた（timeout_3x）。"""
    monkeypatch.setattr(features, "LSP_TIMEOUT_CALL_HIERARCHY_MS", 1000)
    monkeypatch.setattr(_lsp_exact, "EXACT_BUDGET_S", 4.0)
    scripted.delay(target=2.0, caller=2.0, top=2.0)
    for root in ("target", "caller", "top"):  # 別々の根なので、どれも LSP に聞く（PaaD の目印は無い）
        r = await scripted.tree(depth=1, root=root)
        meta = r["_resolution_meta"]
        assert meta["confidence_level"] == "exact" and "scripted" in meta["engine_used"], (root, meta)
        assert scripted.timeouts() == 0, root
    assert scripted.env.pool.status("python", scripted.env.ws).status.value == "ready"


async def test_a_root_hanging_past_the_budget_still_counts_as_a_strike(
    scripted: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4: 根の incomingCalls が予算（3 秒）の内に答えず、要求 1 つの期限（1500 ms）以上待った → 本当の無応答として 1 回数える。
    木ごと structural、警告は予算 3 秒。"""
    monkeypatch.setattr(features, "LSP_TIMEOUT_CALL_HIERARCHY_MS", REQUEST_MS)
    monkeypatch.setattr(_lsp_exact, "EXACT_BUDGET_S", BUDGET_S)
    scripted.delay(target=SLOW_S)
    r = await scripted.tree()
    meta = r["_resolution_meta"]
    assert levels(r) == {"caller": "structural", "top": "structural", "other": "structural"}
    assert meta["lsp_status"] == "ready", meta  # 1 回では落とさない
    warning = meta["accuracy_warning"] or ""
    assert "予算 3 秒（この問い合わせで LSP に使える時間の合計）を使い切りました" in warning, warning
    assert "（target を聞いている途中）" in warning and "木全体を構文木" in warning, warning
    assert "1500 ms" not in warning, warning  # 根は要求の期限で切っていない
    assert scripted.timeouts() == 1


# --- T-06: references も 1 問い合わせの予算（BUG-0091） ------------------------------------------------------------


async def test_t06_references_answering_in_6_seconds_is_exact_and_not_a_strike(scripted: Scripted) -> None:
    """5: 製品の既定（予算 10 秒・references の期限 3000 ms）で references が 6 秒で答える → exact、連続時間切れに数えない。"""
    assert _lsp_exact.EXACT_BUDGET_S == 10.0 and features.LSP_TIMEOUT_REFERENCES_MS == 3000
    scripted.delay(refs_target=6.0)
    started = time.monotonic()
    r = await scripted.refs("target")
    assert time.monotonic() - started >= 6.0
    meta = r["_resolution_meta"]
    assert meta["confidence_level"] == "exact" and "scripted" in meta["engine_used"], meta
    assert {x["path"] for x in r["data"]["references"]} == {"pkg/b.py"}
    assert scripted.timeouts() == 0


async def test_t06_three_slow_cold_references_in_a_row_keep_the_lsp_ready(
    scripted: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5: references が要求の期限（1000 ms に縮めた）を超えて予算（4 秒）の内に答えるのが 3 回続く → 3 回とも exact、LSP は ready。

    以前は 3 回とも期限切れになり、3 回目で LSP を落としていた（timeout_3x）。"""
    monkeypatch.setattr(features, "LSP_TIMEOUT_REFERENCES_MS", 1000)
    monkeypatch.setattr(_lsp_exact, "EXACT_BUDGET_S", 4.0)
    scripted.delay(refs_target=2.0, refs_caller=2.0, refs_top=2.0)
    for root in ("target", "caller", "top"):  # 別々の対象なので、どれも LSP に聞く（PaaD の目印は無い）
        r = await scripted.refs(root)
        meta = r["_resolution_meta"]
        assert meta["confidence_level"] == "exact" and "scripted" in meta["engine_used"], (root, meta)
        assert scripted.timeouts() == 0, root
    assert scripted.env.pool.status("python", scripted.env.ws).status.value == "ready"


async def test_t06_a_references_hang_past_the_budget_counts_and_names_the_budget(
    scripted: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5: references が予算（3 秒）の内に答えず、要求の期限（1500 ms）以上待った → 1 回数える。structural、警告は予算 3 秒。"""
    monkeypatch.setattr(features, "LSP_TIMEOUT_REFERENCES_MS", REQUEST_MS)
    monkeypatch.setattr(_lsp_exact, "EXACT_BUDGET_S", BUDGET_S)
    scripted.delay(refs_target=SLOW_S)
    r = await scripted.refs("target")
    meta = r["_resolution_meta"]
    assert meta["confidence_level"] == "structural" and meta["is_degraded"] is True, meta
    assert meta["lsp_status"] == "ready", meta  # 1 回では落とさない
    warning = meta["accuracy_warning"] or ""
    assert "LSP（scripted）が textDocument/references に" in warning, warning
    assert "予算 3 秒（この問い合わせで LSP に使える時間の合計）の内に答えませんでした" in warning, warning
    assert "呼び直すと exact になる見込み" in warning and "1500 ms" not in warning, warning
    line = note(r, "exact references unavailable")
    assert "3 s exact budget" in line and "textDocument/references" in line and "call again" in line, line
    assert scripted.timeouts() == 1
