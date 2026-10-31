"""BUG-0132: basedpyright / pyright に根の ``pyproject.toml`` を設定ファイルとして明示し、親のディレクトリの設定を採らせない。

* ``pinned_pyright_config``: 根に ``pyrightconfig.json`` があれば明示しない（利用者の設定を尊重）、``pyproject.toml`` だけならそれ、
  どちらも無ければ明示しない（サーバーは親を探さない）。
* ``build_settings(config_file=...)``: ``analysis.configFilePath`` を ``python`` と ``basedpyright`` の節に入れる（Python の型解決の
  サーバーだけ）。
* ``LspPool`` が起動のときに、それを ``workspace/configuration`` の応答で返す（偽のサーバーが問い合わせて書き残す）。
* 実機の basedpyright で親の設定に引きずられないことは ``tests/contract/test_lsp_ancestor_pyright_config_bug0132.py``。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.procs import ProcessRegistry
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import SERVERS, LspServerDef, build_settings, pinned_pyright_config
from lucianke.workspace.types import Workspace


def server(name: str, language_id: str = "python") -> LspServerDef:
    return next(d for d in SERVERS if d.name == name and d.language_id == language_id)


# --- pinned_pyright_config ---------------------------------------------------------------------------


def test_a_root_with_only_pyproject_pins_it(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert pinned_pyright_config(tmp_path) == tmp_path / "pyproject.toml"


def test_a_root_pyproject_with_a_pyright_section_is_pinned_too(tmp_path: Path) -> None:
    """節があればサーバーは同じ設定を読む（明示しても尊重は変わらない）。"""
    (tmp_path / "pyproject.toml").write_text('[tool.basedpyright]\ninclude = ["src"]\n', encoding="utf-8")
    assert pinned_pyright_config(tmp_path) == tmp_path / "pyproject.toml"


def test_a_root_pyrightconfig_is_respected(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (tmp_path / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    assert pinned_pyright_config(tmp_path) is None


def test_a_root_without_config_files_is_not_pinned(tmp_path: Path) -> None:
    """どちらも無いときはサーバーが親を探さない。存在しないパスを渡すとディレクトリを設定として読もうとしてエラーを出す。"""
    assert pinned_pyright_config(tmp_path) is None
    (tmp_path / "pyproject.toml").mkdir()  # ファイルでなければ明示しない
    assert pinned_pyright_config(tmp_path) is None


def test_a_missing_root_is_not_pinned(tmp_path: Path) -> None:
    assert pinned_pyright_config(tmp_path / "gone") is None


# --- build_settings ------------------------------------------------------------------------------------


def test_basedpyright_gets_the_config_file_under_both_sections(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    settings = build_settings(server("basedpyright"), config_file=pyproject)
    # basedpyright は basedpyright.analysis があれば python.analysis を読まないので両方に入れる
    assert settings["basedpyright"]["analysis"]["configFilePath"] == str(pyproject)
    assert settings["python"]["analysis"]["configFilePath"] == str(pyproject)
    assert settings["basedpyright"]["analysis"]["typeCheckingMode"] == "standard"  # 既存の設定は残る


def test_pyright_gets_the_config_file_under_python_only(tmp_path: Path) -> None:
    settings = build_settings(server("pyright"), config_file=tmp_path / "pyproject.toml")
    assert settings["python"]["analysis"]["configFilePath"] == str(tmp_path / "pyproject.toml")
    assert "basedpyright" not in settings


def test_other_servers_do_not_get_the_config_file(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    assert build_settings(server("ruff"), config_file=pyproject) == build_settings(server("ruff"))
    tsls = server("typescript-language-server", "typescript")
    assert build_settings(tsls, config_file=pyproject) == build_settings(tsls)


def test_without_a_config_file_the_settings_are_unchanged() -> None:
    plain = build_settings(server("basedpyright"))
    assert "configFilePath" not in plain["python"]["analysis"]
    assert "configFilePath" not in plain["basedpyright"]["analysis"]
    assert "configFilePath" not in server("basedpyright").settings["basedpyright"]["analysis"]  # 定義を書き換えない


def test_config_file_and_extra_paths_coexist(tmp_path: Path) -> None:
    settings = build_settings(
        server("basedpyright"), extra_paths=[tmp_path / "src"], config_file=tmp_path / "pyproject.toml"
    )
    for section in ("python", "basedpyright"):
        assert settings[section]["analysis"]["extraPaths"] == [str(tmp_path / "src")]
        assert settings[section]["analysis"]["configFilePath"] == str(tmp_path / "pyproject.toml")


# --- LspPool が workspace/configuration で返す --------------------------------------------------------

ASKING_SERVER = r"""
import json, sys
inp, out = sys.stdin.buffer, sys.stdout.buffer
log_path = sys.argv[1]
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
while True:
    msg = read()
    if msg is None:
        break
    m, rid = msg.get("method"), msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {"textDocumentSync": 1}}})
    elif m == "initialized":
        send({"jsonrpc": "2.0", "id": "cfg", "method": "workspace/configuration",
              "params": {"items": [{"section": "python"}, {"section": "basedpyright"}]}})
    elif m is None and msg.get("id") == "cfg":
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(msg.get("result"), f)
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


async def _answered_configuration(registry: ProcessRegistry, tmp_path: Path, root: Path) -> list[Any]:
    script = tmp_path / "asking_lsp.py"
    script.write_text(ASKING_SERVER, encoding="utf-8")
    log = tmp_path / "configuration.json"
    pool = LspPool(
        registry,
        servers=[server("basedpyright")],
        resolver=lambda d, w: [sys.executable, str(script), str(log)],
        ready_fallback_s=0.2,
        grace_ms=300,
    )
    ws = Workspace(workspace_id="w", root=root, case_insensitive=os.name == "nt")
    try:
        await pool.ensure_started("python", ws)
        deadline = time.monotonic() + 15
        while not log.exists():
            assert time.monotonic() < deadline, pool.status("python", ws)
            await asyncio.sleep(0.02)
        return json.loads(log.read_text(encoding="utf-8"))
    finally:
        await pool.shutdown_all(300)


async def test_the_pool_answers_the_pinned_config_file(registry: ProcessRegistry, tmp_path: Path) -> None:
    root = Path(os.path.realpath(tmp_path / "proj"))
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    python, based = await _answered_configuration(registry, tmp_path, root)
    assert based["analysis"]["configFilePath"] == str(root / "pyproject.toml")
    assert python["analysis"]["configFilePath"] == str(root / "pyproject.toml")


async def test_the_pool_leaves_a_root_pyrightconfig_to_the_server(
    registry: ProcessRegistry, tmp_path: Path
) -> None:
    root = Path(os.path.realpath(tmp_path / "proj"))
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (root / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    python, based = await _answered_configuration(registry, tmp_path, root)
    assert "configFilePath" not in based["analysis"] and "configFilePath" not in python["analysis"]
