"""lsp.features（定義・参照・ホバー・呼び出し階層・rename・codeAction・executeCommand）のテスト。

* 実機: ``.venv`` の basedpyright に ``tests/fixtures/poly``（``tmp_path`` に複製）を読ませる。見つからなければ skip。
* 偽サーバー: テスト内の Python スクリプト。応答は JSON の台本（``spec``）で決め、受けた要求は JSON Lines に残す。
  タイムアウト、``command`` だけの codeAction、``resolve`` が要る codeAction、ワークスペース外の URI、小文字ドライブ +
  ``%3A`` の URI、``workspace/applyEdit`` の捕捉を確かめる。
* 子プロセス（LSP とその孫）がテストの後に残らない（``registry.live() == []``）。
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, Position, Range, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.index.ingest import RelationFact
from lucianke.lsp import features
from lucianke.lsp.pool import LspHandle, LspPool, LspState
from lucianke.lsp.servers import LspServerDef, default_server, find_command
from lucianke.workspace.types import PythonEnv, Workspace

REPO = Path(__file__).resolve().parents[2]
POLY = REPO / "tests" / "fixtures" / "poly"

ROUTES = RelPath("src/shop/api/routes.py")
PAYMENT = RelPath("src/shop/services/payment.py")


# --- 共通 -------------------------------------------------------------------------------


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(200, close=True)


def _workspace(root: Path, **kwargs: Any) -> Workspace:
    return Workspace(workspace_id="t", root=root.resolve(), **kwargs)


def pos_of(path: Path, needle: str, *, line: int | None = None, offset: int = 0) -> Position:
    """``path`` の中の ``needle`` の先頭の位置（``line`` 指定なら 0 始まりのその行の中）。"""
    lines = path.read_text(encoding="utf-8").split("\n")
    for i, text in enumerate(lines):
        if (line is None or i == line) and needle in text:
            return Position(i, text.index(needle) + offset)
    raise AssertionError(f"{needle!r} not found in {path}")


# --- 実機: basedpyright -------------------------------------------------------------------

_BASEDPYRIGHT = default_server("python", "types")
_BASEDPYRIGHT_CMD = (
    find_command(_BASEDPYRIGHT, workspace_root=REPO, venv=Path(sys.prefix)) if _BASEDPYRIGHT else None
)

needs_basedpyright = pytest.mark.skipif(_BASEDPYRIGHT_CMD is None, reason="basedpyright is not installed")


class Real:
    def __init__(self, pool: LspPool, handle: LspHandle, ws: Workspace) -> None:
        self.pool, self.handle, self.ws = pool, handle, ws

    def path(self, rel: str) -> Path:
        return self.ws.root / rel


@pytest.fixture
async def real(registry: ProcessRegistry, tmp_path: Path):
    root = tmp_path / "poly"
    shutil.copytree(POLY, root, ignore=shutil.ignore_patterns("__pycache__"))
    ws = _workspace(root, python_env=PythonEnv(interpreter=sys.executable))
    pool = LspPool(
        registry,
        servers=[_BASEDPYRIGHT],
        resolver=lambda defn, w: list(_BASEDPYRIGHT_CMD),
        init_timeout_ms=60_000,
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    await pool.ensure_started("python", ws)
    assert await pool.wait_ready("python", ws, 60_000), pool.status("python", ws)
    handle = pool.get("python", ws)
    assert handle is not None
    await warm_up(handle, ws, [ROUTES, PAYMENT])
    yield Real(pool, handle, ws)
    await pool.shutdown_all(300)
    assert registry.live() == []


async def warm_up(handle: LspHandle, ws: Workspace, rels: list[RelPath]) -> None:
    """文書を開き、サーバーが最初の診断（= その文書の解析の完了）を返すまで待つ。

    ``ready`` の合図（診断・``$/progress end``・猶予時間）は basedpyright の解析の完了を意味しない。何も開いていなければ猶予時間
    （このフィクスチャでは 1 秒）で ``ready`` になるが、そのとき basedpyright はまだ何も解析していない。最初の要求が解析
    そのものを払うので、負荷の下では hover・definition・references の 3 秒の期限（§12.3）を超えうる（BUG-0012）。
    要求の期限を延ばしたり再試行したりせず、サーバーが解析を終えたという合図を待ってから本番の要求に入る。
    """
    for rel in rels:
        text = (ws.root / rel).read_bytes().decode("utf-8")
        handle.documents.sync(rel, text, "python")
    for rel in rels:
        sent_at = handle.documents.sent_at_of(rel)
        assert sent_at is not None
        assert await handle.diagnostics.wait_for(rel, min_version=1, sent_at=sent_at, timeout_ms=60_000), (
            f"basedpyright did not finish analysing {rel}"
        )


@needs_basedpyright
async def test_real_definition_and_references(real: Real):
    routes = real.path(ROUTES)
    call = pos_of(routes, "process_charge", line=15)  # create_order の呼び出し

    defs = await features.definition(real.handle, ROUTES, call)
    assert len(defs) == 1
    loc = defs[0]
    assert loc.rel == PAYMENT and loc.external_uri is None
    expected = pos_of(real.path(PAYMENT), "process_charge", line=38)
    assert loc.range.start == expected
    assert loc.range.end == Position(expected.line, expected.col + len("process_charge"))

    uses = await features.references(real.handle, ROUTES, call, include_declaration=False)
    assert [(r.rel, r.range.start.line) for r in uses] == [(ROUTES, 15), (ROUTES, 24)]
    with_decl = await features.references(real.handle, ROUTES, call, include_declaration=True)
    assert len(with_decl) == 3
    assert (PAYMENT, expected.line) in {(r.rel, r.range.start.line) for r in with_decl}
    assert with_decl == sorted(with_decl, key=lambda r: (r.rel, r.range.start))


@needs_basedpyright
async def test_real_definition_of_a_stdlib_symbol_is_external(real: Real):
    routes = real.path(ROUTES)
    defs = await features.definition(real.handle, ROUTES, pos_of(routes, "Decimal(amount)", line=13))
    assert defs
    assert all(d.rel is None and d.external_uri and d.external_uri.startswith("file:///") for d in defs)
    assert "%3A" not in defs[0].external_uri


@needs_basedpyright
async def test_real_hover_returns_the_type_string(real: Real):
    text = await features.hover(real.handle, ROUTES, pos_of(real.path(ROUTES), "process_charge", line=15))
    assert text is not None
    assert "process_charge" in text and "Receipt" in text
    assert "```" not in text
    nothing = await features.hover(real.handle, ROUTES, Position(1, 0))  # docstring の先頭
    assert nothing is None or isinstance(nothing, str)


@needs_basedpyright
async def test_real_call_hierarchy_incoming_and_outgoing(real: Real):
    decl = pos_of(real.path(PAYMENT), "process_charge", line=38)
    items = await features.prepare_call_hierarchy(real.handle, PAYMENT, decl)
    assert len(items) == 1
    item = items[0]
    assert item.name == "process_charge" and item.rel == PAYMENT
    assert item.range.contains_range(
        item.selection_range
    )  # basedpyright は range も名前の範囲（宣言全体ではない）
    name, rel, rng, sel = item[:4]
    assert (name, rel, rng, sel) == (item.name, item.rel, item.range, item.selection_range)

    incoming = await features.incoming_calls(real.handle, item)
    assert {e.item.name for e in incoming} == {"create_order", "retry_order"}
    for edge in incoming:
        assert edge.item.rel == ROUTES and edge.from_rel == ROUTES
        assert len(edge.from_ranges) == 1
        assert edge.from_ranges[0].start.line == {"create_order": 15, "retry_order": 24}[edge.item.name]

    outgoing = await features.outgoing_calls(real.handle, item)
    assert "_validate_limit" in {e.item.name for e in outgoing}
    validate = next(e for e in outgoing if e.item.name == "_validate_limit")
    assert validate.from_rel == PAYMENT and validate.from_ranges[0].start.line == 49


@needs_basedpyright
async def test_real_rename_returns_a_workspace_edit_without_touching_files(real: Real):
    before = {p: p.read_bytes() for p in real.ws.root.rglob("*.py")}
    edit = await features.rename(
        real.handle, PAYMENT, pos_of(real.path(PAYMENT), "process_charge", line=38), "charge_order"
    )
    assert isinstance(edit, dict)
    changes = edit.get("changes") or {}
    document_changes = edit.get("documentChanges") or []
    n_edits = sum(len(v) for v in changes.values()) + sum(len(dc["edits"]) for dc in document_changes)
    assert n_edits == 3  # 定義 1 + 呼び出し 2
    assert {p: p.read_bytes() for p in real.ws.root.rglob("*.py")} == before


@needs_basedpyright
async def test_real_rename_of_a_keyword_is_rejected_by_prepare_rename(real: Real):
    payment = real.path(PAYMENT)
    with pytest.raises(LuciankeError) as e:
        await features.rename(real.handle, PAYMENT, pos_of(payment, "async def", line=38), "x")
    assert e.value.code is ErrorCode.E_LSP_UNAVAILABLE
    assert e.value.details["reason"] == "prepare_rename_rejected"


@needs_basedpyright
async def test_real_code_action_for_a_type_error(real: Real):
    payment = real.path(PAYMENT)
    line = pos_of(payment, 'factor: Decimal = "1"').line
    await features.hover(real.handle, PAYMENT, Position(line, 4))  # 開く
    sent_at = real.handle.documents.sent_at_of(PAYMENT)
    assert sent_at is not None
    assert await real.handle.diagnostics.wait_for(PAYMENT, min_version=1, sent_at=sent_at, timeout_ms=20_000)
    entry = real.handle.diagnostics.get(PAYMENT)
    assert entry is not None
    diags = [d for d in entry.diagnostics if d["range"]["start"]["line"] == line]
    assert diags, entry.diagnostics

    rng = Range(Position(line, 0), Position(line, len(payment.read_text(encoding="utf-8").split("\n")[line])))
    actions = await features.code_actions(real.handle, PAYMENT, rng, only=["quickfix"], diagnostics=diags)
    assert actions
    assert all(isinstance(a, dict) and a.get("title") for a in actions)
    # basedpyright の quickfix は edit つきで返る（resolve 不要。S-4）。
    assert any(a.get("edit") for a in actions)


# --- 偽サーバー -------------------------------------------------------------------------

FAKE_SERVER = r"""
import json
import sys
import time

inp = sys.stdin.buffer
out = sys.stdout.buffer
spec = json.load(open(sys.argv[1], encoding="utf-8"))
LOG = spec["log"]


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
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"method": msg.get("method"), "params": msg.get("params")}, ensure_ascii=False) + "\n")


while True:
    msg = read()
    if msg is None:
        break
    method = msg.get("method")
    mid = msg.get("id")
    if method in ("textDocument/didOpen", "textDocument/didChange", "$/cancelRequest"):
        log(msg)
        if method == "textDocument/didOpen":
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": msg["params"]["textDocument"]["uri"], "diagnostics": []}})
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": spec.get("capabilities", {})}})
        continue
    if method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
        continue
    if method == "exit":
        break
    if mid is None or method is None:
        continue
    log(msg)
    if method == "workspace/executeCommand" and msg["params"]["command"] == "fake.apply":
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "workspace/applyEdit",
              "params": {"label": "fake", "edit": spec["apply_edit"]}})
        while True:  # 応答が返るまで他は読まない
            reply = read()
            if reply is not None and reply.get("id") == "srv-1" and "method" not in reply:
                break
        send({"jsonrpc": "2.0", "id": mid, "result": {"applied": reply.get("result")}})
        continue
    rule = spec["responses"].get(method, {"result": None})
    if "sleep" in rule:
        time.sleep(rule["sleep"])
        continue
    if "error" in rule:
        send({"jsonrpc": "2.0", "id": mid, "error": rule["error"]})
        continue
    send({"jsonrpc": "2.0", "id": mid, "result": rule.get("result")})
"""

FAKE_DEF = LspServerDef(
    language_id="python", name="fake", argv_candidates=(("fake",),), locale="en", requests_m1=frozenset()
)


class Fake:
    def __init__(self, pool: LspPool, handle: LspHandle, ws: Workspace, log: Path) -> None:
        self.pool, self.handle, self.ws, self.log = pool, handle, ws, log

    def requests(self, method: str) -> list[Any]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [r["params"] for r in rows if r["method"] == method]


FakeFactory = Callable[..., Awaitable[Fake]]


@pytest.fixture
async def fake(registry: ProcessRegistry, tmp_path: Path):
    """``await fake(responses={...}, capabilities={...}, apply_edit={...}, files={...})`` → 偽サーバーが ``ready`` の ``Fake``。"""
    pools: list[LspPool] = []
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    counter = 0

    async def make(
        responses: dict[str, Any] | None = None,
        capabilities: dict[str, Any] | None = None,
        apply_edit: dict[str, Any] | None = None,
        files: dict[str, str] | None = None,
    ) -> Fake:
        nonlocal counter
        counter += 1
        root = tmp_path / f"ws{counter}"
        (root / "src").mkdir(parents=True)
        for name, text in {"src/a.py": "def a():\n    return 1\n", **(files or {})}.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(text.encode("utf-8"))
        ws = _workspace(root)
        base = root.resolve().as_uri()
        log = tmp_path / f"requests{counter}.jsonl"
        spec_text = json.dumps(
            {
                "log": str(log),
                "capabilities": capabilities or {},
                "apply_edit": apply_edit,
                "responses": responses or {},
            }
        ).replace("{ROOT}", base)
        spec_path = tmp_path / f"spec{counter}.json"
        spec_path.write_text(spec_text, encoding="utf-8")
        pool = LspPool(
            registry,
            servers=[FAKE_DEF],
            resolver=lambda defn, w: [sys.executable, str(script), str(spec_path)],
            ready_fallback_s=0.2,
            grace_ms=200,
        )
        pools.append(pool)
        assert await pool.wait_ready("python", ws, 10_000)
        handle = pool.get("python", ws)
        assert handle is not None
        return Fake(pool, handle, ws, log)

    yield make
    for pool in pools:
        await pool.shutdown_all(200)
    assert registry.live() == []


def rng(sl: int, sc: int, el: int, ec: int) -> dict[str, Any]:
    return {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}}


async def test_fake_definition_opens_the_document_and_converts_the_range(fake: FakeFactory):
    f = await fake(
        responses={
            "textDocument/definition": {
                "result": [{"uri": "{ROOT}/src/a.py", "range": rng(0, 4, 0, 5)}],
            }
        }
    )
    defs = await features.definition(f.handle, RelPath("src/a.py"), Position(1, 11))
    assert defs == [features.Location(RelPath("src/a.py"), Range(Position(0, 4), Position(0, 5)), None)]
    opened = f.requests("textDocument/didOpen")
    assert len(opened) == 1 and opened[0]["textDocument"]["text"] == "def a():\n    return 1\n"
    # 2 回目は didOpen を送らない（内容が同じ）
    await features.definition(f.handle, RelPath("src/a.py"), Position(1, 11))
    assert len(f.requests("textDocument/didOpen")) == 1
    assert f.requests("textDocument/definition")[0]["position"] == {"line": 1, "character": 11}


async def test_fake_changed_file_is_sent_as_did_change(fake: FakeFactory):
    f = await fake(
        responses={"textDocument/hover": {"result": {"contents": {"kind": "plaintext", "value": "int"}}}}
    )
    assert await features.hover(f.handle, RelPath("src/a.py"), Position(0, 4)) == "int"
    (f.ws.root / "src" / "a.py").write_bytes(b"def a():\n    return 2\n")
    await features.hover(f.handle, RelPath("src/a.py"), Position(0, 4))
    changes = f.requests("textDocument/didChange")
    assert len(changes) == 1 and changes[0]["contentChanges"] == [{"text": "def a():\n    return 2\n"}]


async def test_fake_location_link_uses_the_selection_range(fake: FakeFactory):
    f = await fake(
        responses={
            "textDocument/definition": {
                "result": [
                    {
                        "targetUri": "{ROOT}/src/a.py",
                        "targetRange": rng(0, 0, 1, 12),
                        "targetSelectionRange": rng(0, 4, 0, 5),
                        "originSelectionRange": rng(1, 11, 1, 12),
                    }
                ]
            }
        }
    )
    [loc] = await features.definition(f.handle, RelPath("src/a.py"), Position(1, 11))
    assert loc.range == Range(Position(0, 4), Position(0, 5))


async def test_fake_utf16_columns_become_codepoint_columns(fake: FakeFactory):
    # "x😀y = 1": 😀 は UTF-16 で 2 単位。y は LSP の列 3、内部の列 2。
    f = await fake(
        files={"src/emoji.py": "x😀y = 1\n"},
        responses={
            "textDocument/definition": {"result": [{"uri": "{ROOT}/src/emoji.py", "range": rng(0, 3, 0, 4)}]}
        },
    )
    [loc] = await features.definition(f.handle, RelPath("src/emoji.py"), Position(0, 2))
    assert loc.range == Range(Position(0, 2), Position(0, 3))
    assert f.requests("textDocument/definition")[0]["position"] == {"line": 0, "character": 3}


async def test_fake_outside_workspace_uri_is_external(fake: FakeFactory, tmp_path: Path):
    outside = tmp_path / "outside" / "ext.py"
    outside.parent.mkdir()
    outside.write_text("x😀y = 1\n", encoding="utf-8")
    f = await fake(
        responses={
            "textDocument/definition": {
                "result": [{"uri": outside.resolve().as_uri(), "range": rng(0, 3, 0, 4)}]
            },
            "textDocument/references": {
                "result": [
                    {"uri": outside.resolve().as_uri(), "range": rng(0, 3, 0, 4)},
                    {"uri": "{ROOT}/src/a.py", "range": rng(1, 11, 1, 12)},
                ]
            },
        }
    )
    [loc] = await features.definition(f.handle, RelPath("src/a.py"), Position(1, 11))
    assert loc.rel is None
    assert loc.external_uri == outside.resolve().as_uri()
    assert loc.range == Range(Position(0, 2), Position(0, 3))  # 外のファイルも UTF-16 → コードポイントに直す
    # references は外を捨てる
    refs = await features.references(f.handle, RelPath("src/a.py"), Position(0, 4))
    assert refs == [features.RefLocation(RelPath("src/a.py"), Range(Position(1, 11), Position(1, 12)))]


async def test_fake_non_file_uri_is_external_as_is(fake: FakeFactory):
    f = await fake(
        responses={
            "textDocument/definition": {"result": {"uri": "untitled:Untitled-1", "range": rng(0, 0, 0, 1)}}
        }
    )
    [loc] = await features.definition(f.handle, RelPath("src/a.py"), Position(0, 4))
    assert (loc.rel, loc.external_uri) == (None, "untitled:Untitled-1")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のドライブ文字の表記")
async def test_fake_lowercase_drive_and_percent_encoded_colon_uri_is_normalized(fake: FakeFactory):
    f = await fake(responses={})
    abs_path = (f.ws.root / "src" / "a.py").as_posix()  # "C:/.../src/a.py"
    drive, rest = abs_path[0], abs_path[2:]
    weird = f"file:///{drive.lower()}%3A{rest}"
    assert weird != (f.ws.root / "src" / "a.py").as_uri()
    locator = features._Locator(f.ws, f.handle.client)
    loc = locator.locate(weird, rng(0, 4, 0, 5))
    assert loc == features.Location(RelPath("src/a.py"), Range(Position(0, 4), Position(0, 5)), None)
    outside = "file:///z%3A/not/in/workspace.py"
    ext = locator.locate(outside, rng(0, 1, 0, 2))
    assert ext.rel is None and ext.external_uri == "file:///Z:/not/in/workspace.py"


async def test_fake_timeout_is_raised_counted_and_cancelled(
    fake: FakeFactory, monkeypatch: pytest.MonkeyPatch
):
    f = await fake(responses={"textDocument/definition": {"sleep": 30}})
    monkeypatch.setattr(features, "LSP_TIMEOUT_DEFINITION_MS", 300)
    with pytest.raises(TimeoutError):
        await features.definition(f.handle, RelPath("src/a.py"), Position(0, 4))
    assert f.handle.timeouts == 1
    assert f.handle.state is LspState.READY  # 1 回では落とさない
    # 時間内に答える要求が数え直す
    f2 = await fake(responses={"textDocument/hover": {"result": None}})
    f2.handle.timeouts = 2
    assert await features.hover(f2.handle, RelPath("src/a.py"), Position(0, 4)) is None
    assert f2.handle.timeouts == 0


async def test_fake_lsp_error_and_disconnect_become_lsp_unavailable(fake: FakeFactory):
    f = await fake(
        responses={"textDocument/references": {"error": {"code": -32603, "message": "boom"}}},
    )
    with pytest.raises(LuciankeError) as e:
        await features.references(f.handle, RelPath("src/a.py"), Position(0, 4))
    assert e.value.code is ErrorCode.E_LSP_UNAVAILABLE
    assert e.value.details["reason"] == "lsp_error" and e.value.details["lsp_code"] == -32603


async def test_fake_not_ready_handle_raises_lsp_unavailable(registry: ProcessRegistry, tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ws = _workspace(tmp_path)
    pool = LspPool(registry, servers=[FAKE_DEF], resolver=lambda d, w: None)
    assert pool.get("python", ws, start_if_needed=False) is None
    [handle] = pool.handles(ws)
    assert handle.state is LspState.STANDBY
    with pytest.raises(LuciankeError) as e:
        await features.definition(handle, RelPath("a.py"), Position(0, 0))
    assert e.value.code is ErrorCode.E_LSP_UNAVAILABLE and e.value.details["reason"] == "standby"
    with pytest.raises(LuciankeError) as e2:
        await features.rename(handle, RelPath("a.py"), Position(0, 0), "y")
    assert e2.value.details["reason"] == "standby"
    await pool.shutdown_all(100)
    assert registry.live() == []


async def test_fake_missing_file_is_target_not_found(fake: FakeFactory):
    f = await fake()
    with pytest.raises(LuciankeError) as e:
        await features.hover(f.handle, RelPath("src/nope.py"), Position(0, 0))
    assert e.value.code is ErrorCode.E_TARGET_NOT_FOUND


async def test_fake_hover_markdown_shapes(fake: FakeFactory):
    md = "```python\n(function) def a() -> int\n```\n---\nDocstring"
    f = await fake(
        responses={"textDocument/hover": {"result": {"contents": {"kind": "markdown", "value": md}}}}
    )
    assert await features.hover(f.handle, RelPath("src/a.py"), Position(0, 4)) == "(function) def a() -> int"
    assert features.hover_text([{"language": "typescript", "value": "function a(): number"}, "docs"]) == (
        "function a(): number"
    )
    assert features.hover_text("plain\n\nsecond") == "plain"
    assert features.hover_text({"kind": "markdown", "value": "  "}) is None
    assert features.hover_text(None) is None


async def test_fake_call_hierarchy_passes_the_raw_item_back(fake: FakeFactory):
    raw = {
        "name": "a",
        "kind": 12,
        "uri": "{ROOT}/src/a.py",
        "range": rng(0, 0, 1, 12),
        "selectionRange": rng(0, 4, 0, 5),
        "data": {"opaque": 7},
    }
    caller = {**raw, "name": "caller", "range": rng(5, 0, 6, 0), "selectionRange": rng(5, 4, 5, 10)}
    f = await fake(
        files={"src/a.py": "def a():\n    return 1\n\n\n\ndef caller():\n    a()\n"},
        responses={
            "textDocument/prepareCallHierarchy": {"result": [raw]},
            "callHierarchy/incomingCalls": {"result": [{"from": caller, "fromRanges": [rng(6, 4, 6, 5)]}]},
            "callHierarchy/outgoingCalls": {"result": [{"to": raw, "fromRanges": [rng(1, 11, 1, 12)]}]},
        },
    )
    [item] = await features.prepare_call_hierarchy(f.handle, RelPath("src/a.py"), Position(0, 4))
    assert item.name == "a" and item.kind == 12
    [edge] = await features.incoming_calls(f.handle, item)
    assert f.requests("callHierarchy/incomingCalls")[0]["item"]["data"] == {"opaque": 7}
    assert edge.item.name == "caller" and edge.from_rel == RelPath("src/a.py")
    assert edge.from_ranges == (Range(Position(6, 4), Position(6, 5)),)
    [out_edge] = await features.outgoing_calls(f.handle, item)
    assert out_edge.from_rel == RelPath("src/a.py") and out_edge.item.name == "a"


async def test_fake_rename_prepare_rejected(fake: FakeFactory):
    f = await fake(
        capabilities={"renameProvider": {"prepareProvider": True}},
        responses={"textDocument/prepareRename": {"result": None}},
    )
    with pytest.raises(LuciankeError) as e:
        await features.rename(f.handle, RelPath("src/a.py"), Position(0, 0), "b")
    assert e.value.code is ErrorCode.E_LSP_UNAVAILABLE
    assert e.value.details["reason"] == "prepare_rename_rejected"
    assert f.requests("textDocument/rename") == []


async def test_fake_rename_returns_the_workspace_edit_as_is(fake: FakeFactory):
    edit = {"changes": {"{ROOT}/src/a.py": [{"range": rng(0, 4, 0, 5), "newText": "b"}]}}
    f = await fake(
        capabilities={"renameProvider": {"prepareProvider": True}},
        responses={
            "textDocument/prepareRename": {"result": rng(0, 4, 0, 5)},
            "textDocument/rename": {"result": edit},
        },
    )
    got = await features.rename(f.handle, RelPath("src/a.py"), Position(0, 4), "b")
    assert got == json.loads(json.dumps(edit).replace("{ROOT}", f.ws.root.as_uri()))
    assert f.requests("textDocument/rename")[0]["newName"] == "b"


async def test_fake_rename_without_prepare_support_skips_prepare(fake: FakeFactory):
    f = await fake(responses={"textDocument/rename": {"result": None}})
    with pytest.raises(LuciankeError) as e:
        await features.rename(f.handle, RelPath("src/a.py"), Position(0, 4), "b")
    assert e.value.details["reason"] == "rename_rejected"
    assert f.requests("textDocument/prepareRename") == []


async def test_fake_code_actions_command_only_and_resolve(fake: FakeFactory):
    with_edit = {"title": "Fix", "kind": "quickfix", "edit": {"changes": {}}}
    needs_resolve = {"title": "Organize", "kind": "source.organizeImports", "data": {"id": 1}}
    command_only = {
        "title": "Run later",
        "kind": "quickfix",
        "command": {"title": "Run later", "command": "x.later", "arguments": [1]},
    }
    bare = {"title": "Bare", "command": "x.bare", "arguments": ["a"]}
    resolved = {**needs_resolve, "edit": {"changes": {"{ROOT}/src/a.py": []}}}
    f = await fake(
        capabilities={"codeActionProvider": {"resolveProvider": True}},
        responses={
            "textDocument/codeAction": {"result": [with_edit, needs_resolve, command_only, bare]},
            "codeAction/resolve": {"result": resolved},
        },
    )
    diag = {"range": rng(0, 0, 0, 3), "message": "m", "severity": 2}
    actions = await features.code_actions(
        f.handle,
        RelPath("src/a.py"),
        Range(Position(0, 0), Position(0, 3)),
        only=["quickfix", "source.organizeImports"],
        diagnostics=[diag],
    )
    assert [a["title"] for a in actions] == ["Fix", "Organize", "Run later", "Bare"]
    assert features.COMMAND_ONLY_KEY not in actions[0]
    assert actions[1]["edit"] == {"changes": {f"{f.ws.root.as_uri()}/src/a.py": []}}
    assert features.COMMAND_ONLY_KEY not in actions[1]
    assert actions[2][features.COMMAND_ONLY_KEY] is True and actions[2]["command"]["command"] == "x.later"
    assert actions[3][features.COMMAND_ONLY_KEY] is True
    assert actions[3]["command"] == {"title": "Bare", "command": "x.bare", "arguments": ["a"]}
    [request] = f.requests("textDocument/codeAction")
    assert request["context"] == {"diagnostics": [diag], "only": ["quickfix", "source.organizeImports"]}
    assert request["range"] == rng(0, 0, 0, 3)
    assert len(f.requests("codeAction/resolve")) == 1  # edit つきと command だけは resolve しない


async def test_fake_code_action_resolve_failure_keeps_the_action(fake: FakeFactory):
    f = await fake(
        capabilities={"codeActionProvider": {"resolveProvider": True}},
        responses={
            "textDocument/codeAction": {"result": [{"title": "T", "data": 1}]},
            "codeAction/resolve": {"error": {"code": -32603, "message": "no"}},
        },
    )
    [action] = await features.code_actions(
        f.handle, RelPath("src/a.py"), Range(Position(0, 0), Position(0, 1))
    )
    assert action == {"title": "T", "data": 1}


async def test_fake_apply_edit_request_is_captured_and_acknowledged(fake: FakeFactory):
    edit = {"changes": {"{ROOT}/src/a.py": [{"range": rng(0, 0, 0, 0), "newText": "import os\n"}]}}
    f = await fake(apply_edit=edit)
    outcome = await features.execute_command(f.handle, "fake.apply", [f.ws.root.as_uri() + "/src/a.py"])
    expected = json.loads(json.dumps(edit).replace("{ROOT}", f.ws.root.as_uri()))
    assert outcome.edits == [expected]  # 書き換えずにそのまま
    assert outcome.result == {"applied": {"applied": True}}  # サーバーには applied: true と答えた
    assert (f.ws.root / "src" / "a.py").read_text(
        encoding="utf-8"
    ) == "def a():\n    return 1\n"  # 書いていない
    again = await features.execute_command(f.handle, "fake.apply")
    assert again.edits == [expected]  # 前回の分は残らない


async def test_fake_apply_edit_outside_a_capture_is_refused(fake: FakeFactory):
    f = await fake()
    client = f.handle.client
    assert client is not None
    features._capture_for(client)
    reply = client._request_handlers["workspace/applyEdit"]({"edit": {"changes": {}}})
    assert reply["applied"] is False


# --- PaaD への書き戻し ------------------------------------------------------------------


def _locate(table: dict[tuple[str, int], str]):
    def locate(rel: RelPath, r: Range) -> str | None:
        return table.get((str(rel), r.start.line))

    return locate


def test_to_exact_relations_outgoing_from_a_definition():
    facts = features.to_exact_relations(
        "lk://t/src/a.py#caller",
        [
            (RelPath("src/b.py"), Range(Position(3, 4), Position(3, 8))),
            (RelPath("ext.py"), Range(Position(0, 0), Position(0, 1))),
        ],
        locate=_locate({("src/b.py", 3): "lk://t/src/b.py#callee"}),
        relation_kind="CALLS",
        use_kind="call",
        call_site=Position(10, 8),
    )
    assert facts == [
        RelationFact(
            source_urn="lk://t/src/a.py#caller",
            target_urn="lk://t/src/b.py#callee",
            relation_kind="CALLS",
            call_site_line=10,
            call_site_col=8,
            use_kind="call",
            confidence=Confidence.EXACT,
        )
    ]


def test_to_exact_relations_incoming_from_references():
    refs = [
        (ROUTES, Range(Position(15, 34), Position(15, 48))),
        (ROUTES, Range(Position(15, 34), Position(15, 48))),  # 重複
        (ROUTES, Range(Position(24, 38), Position(24, 52))),
        (RelPath("tests/x.py"), Range(Position(1, 0), Position(1, 3))),  # シンボル無し
    ]
    facts = features.to_exact_relations(
        "lk://t/src/shop/services/payment.py#PaymentProcessor.process_charge",
        refs,
        locate=_locate(
            {(str(ROUTES), 15): "lk://t/routes#create_order", (str(ROUTES), 24): "lk://t/routes#retry_order"}
        ),
        incoming=True,
    )
    assert [(str(f.source_urn), f.call_site_line, f.call_site_col) for f in facts] == [
        ("lk://t/routes#create_order", 15, 34),
        ("lk://t/routes#retry_order", 24, 38),
    ]
    assert {str(f.target_urn) for f in facts} == {
        "lk://t/src/shop/services/payment.py#PaymentProcessor.process_charge"
    }
    assert all(f.confidence is Confidence.EXACT and f.relation_kind == "CALLS" for f in facts)


# --- S-11: 位置の単位・丸め・vtsls の宣言全体の範囲 -------------------------------------------


async def test_fake_clamped_position_is_not_sent(fake: FakeFactory):
    f = await fake(files={"src/uni.py": "s = '日本語'\n"})
    uni = RelPath("src/uni.py")
    for call in (
        lambda: features.definition(f.handle, uni, Position(0, 99)),  # 行末を越える列
        lambda: features.hover(f.handle, uni, Position(0, 200)),
        lambda: features.code_actions(f.handle, uni, Range(Position(0, 0), Position(0, 99))),
    ):
        with pytest.raises(LuciankeError) as e:
            await call()
        assert e.value.code is ErrorCode.E_LSP_UNAVAILABLE and e.value.details["reason"] == "position_clamped"
    with pytest.raises(LuciankeError) as e2:
        await features.definition(f.handle, uni, Position(5, 0))  # 範囲外の行
    assert e2.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert f.requests("textDocument/definition") == []
    assert f.requests("textDocument/hover") == []
    assert f.requests("textDocument/codeAction") == []
    # 行末ちょうどの列は丸めではないので送る
    await features.hover(f.handle, uni, Position(0, len("s = '日本語'")))
    assert len(f.requests("textDocument/hover")) == 1


async def test_fake_wide_definition_range_is_narrowed_to_the_name(fake: FakeFactory):
    # vtsls は識別子ではなく宣言文全体の範囲を返す（S-11）。
    decl = "export const vAstral = 2;\nexport function other() {}\n"
    f = await fake(
        files={"src/decl.ts": decl, "src/use.ts": "console.log(vAstral);\n"},
        responses={
            "textDocument/definition": {"result": [{"uri": "{ROOT}/src/decl.ts", "range": rng(0, 0, 0, 25)}]}
        },
    )
    [loc] = await features.definition(f.handle, RelPath("src/use.ts"), Position(0, 14))
    assert loc.rel == RelPath("src/decl.ts")
    assert loc.range == Range(Position(0, 13), Position(0, 20))
    # 名前が見つからない（別名など）ときはそのまま
    [same] = await features.definition(f.handle, RelPath("src/use.ts"), Position(0, 3))  # "console"
    assert same.range == Range(Position(0, 0), Position(0, 25))


async def test_fake_multi_line_declaration_range_is_narrowed_to_the_first_name(fake: FakeFactory):
    decl = "function helper(\n  a: number,\n): number {\n  return a;\n}\n"
    f = await fake(
        files={"src/decl.ts": decl, "src/use.ts": "helper(1);\n"},
        responses={
            "textDocument/definition": {"result": [{"uri": "{ROOT}/src/decl.ts", "range": rng(0, 0, 4, 1)}]}
        },
    )
    [loc] = await features.definition(f.handle, RelPath("src/use.ts"), Position(0, 2))
    assert loc.range == Range(Position(0, 9), Position(0, 15))


async def test_fake_references_drop_the_declaration_found_by_definition(fake: FakeFactory):
    f = await fake(
        files={"src/use.py": "from a import a\na()\n"},
        responses={
            "textDocument/definition": {"result": [{"uri": "{ROOT}/src/a.py", "range": rng(0, 4, 0, 5)}]},
            "textDocument/references": {
                "result": [
                    {"uri": "{ROOT}/src/a.py", "range": rng(0, 4, 0, 5)},
                    {"uri": "{ROOT}/src/use.py", "range": rng(1, 0, 1, 1)},
                ]
            },
        },
    )
    use = RelPath("src/use.py")
    got = await features.references(f.handle, use, Position(1, 0))
    assert [(r.rel, r.range.start.line) for r in got] == [(use, 1)]
    assert f.requests("textDocument/references")[0]["context"] == {"includeDeclaration": True}
    both = await features.references(f.handle, use, Position(1, 0), include_declaration=True)
    assert len(both) == 2


# --- 実機: ruff（utf-8 を採るサーバー。codeAction の供給元） -------------------------------------

_RUFF = default_server("python", "lint")
_RUFF_CMD = find_command(_RUFF, workspace_root=REPO, venv=Path(sys.prefix)) if _RUFF else None


@pytest.mark.skipif(_RUFF_CMD is None, reason="ruff is not installed")
async def test_real_ruff_organize_imports_is_resolved_and_positions_are_utf8(
    registry: ProcessRegistry, tmp_path: Path
):
    root = tmp_path / "rws"
    root.mkdir()
    (root / "pyproject.toml").write_bytes(b"[tool.ruff]\n")
    text = "import sys\nimport os\n\nname = '日本語'\nprint(os, sys, name)\n"
    (root / "m.py").write_bytes(text.encode("utf-8"))
    ws = _workspace(root)
    pool = LspPool(
        registry,
        servers=[_RUFF],
        resolver=lambda defn, w: list(_RUFF_CMD),
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    try:
        assert await pool.wait_ready("python", ws, 30_000, role="lint")
        handle = pool.get("python", ws, role="lint")
        assert handle is not None and handle.client is not None
        assert handle.client.negotiated_encoding.value == "utf-8"
        rel = RelPath("m.py")
        whole = Range(Position(0, 0), Position(4, 0))
        actions = await features.code_actions(handle, rel, whole, only=["source.organizeImports"])
        assert actions, "ruff should offer organizeImports"
        organize = actions[0]
        assert organize.get("edit"), organize  # resolve で edit が付いている（S-4）
        # 日本語の後ろの列（コードポイント 14）は utf-8 では 18。文字境界なので送れる。
        after_cjk = Range(Position(3, 0), Position(3, len("name = '日本語'")))
        await features.code_actions(handle, rel, after_cjk, only=["quickfix"])
        sent = handle.client  # 落ちていない（ruff は文字の途中の列でパニックする。S-11）
        assert sent.is_connected
    finally:
        await pool.shutdown_all(300)
    assert registry.live() == []
