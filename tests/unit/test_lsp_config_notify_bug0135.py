"""BUG-0135: 根の設定（``pyrightconfig.json`` / ``pyproject.toml``）の変更を、動いている basedpyright / pyright に届け、Lucianke の見る
解析の対象（``LspHandle.analysis_scope``）と設定ファイルの明示の判断（``LspHandle.pinned_config``）を読み直す。

* ``is_pyright_root_config``: 根の 2 つのファイルだけ（下のディレクトリのものはサーバーが読まない）。``is_lsp_config`` は、それと
  どの深さの ``tsconfig.json`` など（索引に載らない設定ファイルの変更も知らせる。``tests/unit/test_tools_runtime.py``）。
* ``LspPool.notify_watched``（偽のサーバーが受けた通知と、``workspace/configuration`` に返った設定を書き残す）:
  明示の判断が変わらなければ ``didChangeWatchedFiles`` だけ（設定ファイルを含む）。変わったら設定を差し替えて
  ``didChangeConfiguration`` を送り、設定ファイルは ``didChangeWatchedFiles`` に入れない。ほかのファイルの変更では読み直さない。
  ruff（``lint``）には ``didChangeConfiguration`` を送らない。
* 実機の basedpyright で届くことは ``tests/contract/test_lsp_config_change_bug0135.py``。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.model import RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.pool import FileChangeType, LspHandle, LspPool
from lucianke.lsp.servers import SERVERS, LspServerDef, is_lsp_config, is_pyright_root_config
from lucianke.workspace.types import Workspace


def server(name: str) -> LspServerDef:
    return next(d for d in SERVERS if d.name == name and d.language_id == "python")


# --- is_pyright_root_config ----------------------------------------------------------------------------


@pytest.mark.parametrize("rel", ["pyrightconfig.json", "pyproject.toml"])
def test_the_root_config_files_are_recognised(rel: str) -> None:
    assert is_pyright_root_config(rel)


@pytest.mark.parametrize(
    "rel", ["src/pyrightconfig.json", "pkg/pyproject.toml", "pyrightconfig.jsonc", "setup.cfg", "tsconfig.json"]
)
def test_other_files_are_not_root_config_files(rel: str) -> None:
    assert not is_pyright_root_config(rel)


def test_case_is_folded_only_on_a_case_insensitive_workspace() -> None:
    assert is_pyright_root_config("PyrightConfig.json", case_insensitive=True)
    assert not is_pyright_root_config("PyrightConfig.json", case_insensitive=False)


@pytest.mark.parametrize(
    "rel",
    ["pyrightconfig.json", "pyproject.toml", "tsconfig.json", "web/tsconfig.json", "tsconfig.build.json",
     "pkg/jsconfig.json", "jsconfig.test.json"],
)  # fmt: skip
def test_lsp_config_files_include_tsserver_configs_at_any_depth(rel: str) -> None:
    """索引に載らない（``.gitignore`` の対象など）ときも LSP に変更を知らせる設定ファイル（``tools/_runtime.IndexerSink``）。"""
    assert is_lsp_config(rel)


@pytest.mark.parametrize("rel", ["pkg/pyproject.toml", "tsconfig.ts", "mytsconfig.json", "tsconfig.json.bak", "a.py"])
def test_other_files_are_not_lsp_config_files(rel: str) -> None:
    assert not is_lsp_config(rel)


# --- LspPool.notify_watched -----------------------------------------------------------------------------

RECORDING_SERVER = r"""
import json, sys
inp, out = sys.stdin.buffer, sys.stdout.buffer
log_path = sys.argv[1]
def log(obj):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
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
    out.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    out.flush()
asked = 0
def ask():
    global asked
    asked += 1
    send({"jsonrpc": "2.0", "id": "cfg%d" % asked, "method": "workspace/configuration",
          "params": {"items": [{"section": "python"}, {"section": "basedpyright"}]}})
while True:
    msg = read()
    if msg is None:
        break
    m, rid = msg.get("method"), msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {"textDocumentSync": 1}}})
    elif m == "initialized":
        ask()
    elif m is None and str(msg.get("id", "")).startswith("cfg"):
        log({"configuration": msg.get("result")})
    elif m in ("workspace/didChangeWatchedFiles", "workspace/didChangeConfiguration"):
        log({"method": m, "params": msg.get("params")})
        if m == "workspace/didChangeConfiguration":
            ask()
    elif m == "shutdown":
        send({"jsonrpc": "2.0", "id": rid, "result": None})
    elif m == "exit":
        sys.exit(0)
    elif m is not None and rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": None})
"""


@pytest.fixture
async def registry():
    reg = ProcessRegistry()
    yield reg
    await reg.terminate_all(300, close=True)


class Recorded:
    def __init__(self, pool: LspPool, ws: Workspace, log: Path) -> None:
        self.pool, self.ws, self.log = pool, ws, log

    @property
    def root(self) -> Path:
        return self.ws.root

    def handle(self, role: str = "types") -> LspHandle:
        return next(h for h in self.pool.handles(self.ws) if h.role == role)

    def lines(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x.strip()]

    async def wait_lines(self, cond: Callable[[list[dict[str, Any]]], bool]) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 15
        while not cond(lines := self.lines()):
            assert time.monotonic() < deadline, lines
            await asyncio.sleep(0.02)
        return lines

    async def change(self, *changes: tuple[str, FileChangeType]) -> list[dict[str, Any]]:
        """``notify_watched`` を送り、偽のサーバーが受けた通知（と、``didChangeConfiguration`` の後に引き直した設定）を返す。"""
        before = len(self.lines())
        await self.pool.notify_watched(self.ws, [(RelPath(r), k) for r, k in changes])
        await self.pool.notify_watched(self.ws, [(RelPath("marker.py"), FileChangeType.CHANGED)])  # 届いた順の区切り

        def settled(ls: list[dict[str, Any]]) -> bool:
            new = ls[before:]
            if not any(_uris(x) == ["marker.py"] for x in new):
                return False
            asked = any(x.get("method") == "workspace/didChangeConfiguration" for x in new)
            return not asked or any("configuration" in x for x in new)  # 引き直した設定の応答も書き残すまで

        lines = await self.wait_lines(settled)
        return [x for x in lines[before:] if _uris(x) != ["marker.py"]]


def _uris(line: dict[str, Any]) -> list[str] | None:
    if line.get("method") != "workspace/didChangeWatchedFiles":
        return None
    return [c["uri"].rsplit("/", 1)[-1] for c in line["params"]["changes"]]


def _config_file(line: dict[str, Any]) -> tuple[Any, Any]:
    python, based = line["configuration"]
    return python.get("analysis", {}).get("configFilePath"), based.get("analysis", {}).get("configFilePath")


@pytest.fixture
async def recorded(registry: ProcessRegistry, tmp_path: Path) -> AsyncIterator[Recorded]:
    """根に ``pyproject.toml`` だけ（明示する起動）のワークスペースで、偽のサーバー（basedpyright の定義）と ruff の定義を起こす。"""
    root = Path(os.path.realpath(tmp_path / "proj"))
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    script = tmp_path / "recording_lsp.py"
    script.write_text(RECORDING_SERVER, encoding="utf-8")
    logs = {"basedpyright": tmp_path / "types.jsonl", "ruff": tmp_path / "lint.jsonl"}
    pool = LspPool(
        registry,
        servers=[server("basedpyright"), server("ruff")],
        resolver=lambda d, w: [sys.executable, str(script), str(logs[d.name])],
        ready_fallback_s=0.2,
        grace_ms=300,
    )
    ws = Workspace(workspace_id="w", root=root, case_insensitive=os.name == "nt")
    try:
        await pool.ensure_started("python", ws)
        assert await pool.wait_ready("python", ws, 15_000), pool.status("python", ws)
        assert await pool.wait_ready("python", ws, 15_000, role="lint"), pool.status("python", ws, "lint")
        rec = Recorded(pool, ws, logs["basedpyright"])
        await rec.wait_lines(lambda ls: any("configuration" in x for x in ls))
        yield rec
    finally:
        await pool.shutdown_all(300)


async def test_creating_a_root_pyrightconfig_unpins_and_sends_did_change_configuration(recorded: Recorded) -> None:
    h = recorded.handle()
    assert h.pinned_config == recorded.root / "pyproject.toml"
    assert h.analysis_scope is not None and not h.analysis_scope.narrowed
    (recorded.root / "pyrightconfig.json").write_text(json.dumps({"include": ["src"]}), encoding="utf-8")
    got = await recorded.change(("pyrightconfig.json", FileChangeType.CREATED), ("src/a.py", FileChangeType.CHANGED))
    assert h.pinned_config is None
    assert h.analysis_scope is not None and h.analysis_scope.narrowed  # Lucianke の見る対象も読み直した
    methods = [x.get("method", "configuration") for x in got]
    # 設定を差し替えて didChangeConfiguration → サーバーが引き直す。設定ファイルは didChangeWatchedFiles に入れない
    assert methods.index("workspace/didChangeConfiguration") < methods.index("workspace/didChangeWatchedFiles")
    assert [_uris(x) for x in got if _uris(x) is not None] == [["a.py"]]
    configuration = [x for x in got if "configuration" in x]
    assert configuration and _config_file(configuration[-1]) == (None, None)


async def test_deleting_the_root_pyrightconfig_pins_the_pyproject_again(recorded: Recorded) -> None:
    (recorded.root / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    await recorded.change(("pyrightconfig.json", FileChangeType.CREATED))
    assert recorded.handle().pinned_config is None
    (recorded.root / "pyrightconfig.json").unlink()
    got = await recorded.change(("pyrightconfig.json", FileChangeType.DELETED))
    assert recorded.handle().pinned_config == recorded.root / "pyproject.toml"
    assert [x["method"] for x in got if "method" in x] == ["workspace/didChangeConfiguration"]
    pyproject = str(recorded.root / "pyproject.toml")
    assert _config_file([x for x in got if "configuration" in x][-1]) == (pyproject, pyproject)


async def test_editing_a_config_without_changing_the_pin_sends_only_the_watched_change(recorded: Recorded) -> None:
    """明示の判断が変わらない（明示した ``pyproject.toml`` に節を足す）: サーバーは ``didChangeWatchedFiles`` で読み直す。対象は読み直す。"""
    h = recorded.handle()
    pyproject = recorded.root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8") + '\n[tool.basedpyright]\ninclude = ["src"]\n', encoding="utf-8")
    got = await recorded.change(("pyproject.toml", FileChangeType.CHANGED))
    assert [_uris(x) for x in got] == [["pyproject.toml"]]
    assert h.pinned_config == pyproject
    assert h.analysis_scope is not None and h.analysis_scope.narrowed


async def test_other_files_do_not_reload_the_config(recorded: Recorded) -> None:
    h = recorded.handle()
    scope = h.analysis_scope
    (recorded.root / "pyrightconfig.json").write_text(json.dumps({"include": ["src"]}), encoding="utf-8")
    got = await recorded.change(("src/pyrightconfig.json", FileChangeType.CREATED), ("src/a.py", FileChangeType.CREATED))
    assert [_uris(x) for x in got] == [["pyrightconfig.json", "a.py"]]
    assert h.analysis_scope is scope and h.pinned_config == recorded.root / "pyproject.toml"


async def test_the_lint_server_gets_only_the_watched_change(recorded: Recorded) -> None:
    """ruff は根の pyright の設定を読まない: ``didChangeConfiguration`` を送らず、設定ファイルの変更も今までどおり届ける。"""
    lint_log = recorded.log.with_name("lint.jsonl")
    (recorded.root / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    await recorded.change(("pyrightconfig.json", FileChangeType.CREATED))
    lines = await Recorded(recorded.pool, recorded.ws, lint_log).wait_lines(
        lambda ls: any(_uris(x) == ["marker.py"] for x in ls)
    )
    sent = [x for x in lines if "method" in x]
    assert [x["method"] for x in sent] == ["workspace/didChangeWatchedFiles"] * 2
    assert [_uris(x) for x in sent] == [["pyrightconfig.json"], ["marker.py"]]
    assert recorded.handle("lint").pinned_config is None and recorded.handle("lint").analysis_scope is None
