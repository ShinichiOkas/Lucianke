"""M2・M3 のツールの入口（T-09・T-12・T-14〜T-18。設計書 §12.2 の注、``tools/_unsupported.py``）の契約テスト。

* 7 本は自動登録で登録表に載り、入力スキーマが仕様書の必須引数・範囲・既定値を持つ。``tools/list`` には**既定では載らない**
  （M1 では呼べない入口が約 4,000 トークンの固定費になるため。23a）。設定 ``LUCIANKE_LIST_PLANNED_TOOLS=1`` で載せる。
  載せなくても呼べば ``E_UNSUPPORTED``。
* 既定のパイプライン（M1）は、ハンドラの前に ``E_UNSUPPORTED``（``details.reason: "not_in_milestone"``, ``details.milestone``）を返す。
* ``current_milestone`` を進めたパイプラインでは入口のハンドラが動き、``details.hint``（M1 の代わりのツール）を足して同じ
  ``E_UNSUPPORTED`` を返す。引数の検証（スキーマ）はその前に効く。
* 実プロセスの ``lucianke`` に 7 本を呼んでも ``stdout`` は JSON-RPC のまま（NFR-OPS-1）。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from lucianke import tools
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry

# name -> (T 番号, マイルストーン, 仕様書の必須引数, 呼べる最小の引数, M1 の代わりに使えるツール〔hint に出る名前。無ければ None〕)
_TARGET = {"path": "src/a.py", "symbol_name": "f"}
PLANNED: dict[str, tuple[str, str, set[str], dict[str, Any], str | None]] = {
    "analyze_change_impact": ("T-09", "M2", {"target"}, {"target": _TARGET}, "find_references_and_implementations"),
    "structural_search_replace": (
        "T-12", "M2", {"language", "pattern"}, {"language": "python", "pattern": "print($MSG)"}, "edit_symbol_body",
    ),  # fmt: skip
    "find_code_paths": ("T-14", "M2", {"source", "sink"}, {"source": _TARGET, "sink": _TARGET}, "query_call_hierarchy"),
    "extract_code_contracts": ("T-15", "M3", {"path"}, {"path": "src/a.py"}, None),
    "verify_contract": ("T-16", "M3", {"target", "postconditions"}, {"target": _TARGET, "postconditions": ["return > 0"]}, None),
    "get_symbol_history": ("T-17", "M2", {"target"}, {"target": _TARGET}, None),
    "analyze_git_diff_symbols": ("T-18", "M2", set(), {}, None),
}  # fmt: skip
NAMES = sorted(PLANNED)
GRAPH_TOOLS = {"analyze_change_impact", "find_code_paths", "analyze_git_diff_symbols"}
DEFAULT_TOKENS = {
    "analyze_change_impact": 2500, "structural_search_replace": 2000, "find_code_paths": 2500,
    "extract_code_contracts": 2000, "verify_contract": 2000, "get_symbol_history": 2000,
    "analyze_git_diff_symbols": 3000,
}  # fmt: skip


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    tools.register_tools(reg)  # 本番と同じ自動登録
    return reg


def schema_of(registry: ToolRegistry, name: str) -> dict[str, Any]:
    return registry.input_schema(name)


# --- tools/list ---------------------------------------------------------------------------------


def test_the_seven_planned_tools_are_listed_when_asked(registry: ToolRegistry) -> None:
    listed = {d.name: d for d in registry.descriptors(include_planned=True)}
    assert set(NAMES) <= set(listed)  # 件数は決め打ちしない（ツールは増えていく）
    for name, (tid, milestone, _req, _min, hint_tool) in PLANNED.items():
        spec = registry.get(name)
        assert spec is not None and spec.tid == tid and spec.milestone == milestone
        description = listed[name].description
        assert f"Planned for {milestone}" in description and "E_UNSUPPORTED" in description, name
        assert (
            (f"Meanwhile: {hint_tool}" in description or hint_tool in description)
            if hint_tool
            else "Meanwhile" not in description
        )
        assert listed[name].read_only is (name != "structural_search_replace")  # 書き込みは T-12 だけ


def test_planned_tools_are_left_out_of_the_default_listing(registry: ToolRegistry) -> None:
    default = {d.name for d in registry.descriptors(include_planned=False)}
    assert not (set(NAMES) & default)  # 7 本は載らない
    m1 = {s.name for s in registry if s.milestone == "M1"}
    assert m1 and m1 <= default  # M1 のツールは全部載る
    assert default == m1
    for name in NAMES:
        assert name in registry and registry.get(name) is not None  # 登録表には残る（呼べば E_UNSUPPORTED）
    # マイルストーンが進んだパイプラインなら、先のツールではなくなるので載る
    assert set(NAMES) <= {d.name for d in registry.descriptors(include_planned=False, current_milestone="M3")}
    assert {d.name for d in registry.descriptors(include_planned=False, current_milestone="M2")} == default | {
        n for n in NAMES if PLANNED[n][1] == "M2"
    }


@pytest.mark.parametrize("name", NAMES)
def test_schema_has_the_required_arguments_of_the_spec(registry: ToolRegistry, name: str) -> None:
    schema = schema_of(registry, name)
    required = set(schema.get("required", []))
    assert required == PLANNED[name][2], name
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    props = schema["properties"]
    assert {"workspace_id", "revision", "format", "max_tokens", "cursor"} <= set(props)  # 共通パラメータ
    assert props["max_tokens"]["default"] == DEFAULT_TOKENS[name] and props["max_tokens"]["minimum"] == 200
    formats = set(props["format"]["enum"])
    assert ({"dot", "mermaid"} <= formats) is (name in GRAPH_TOOLS), name


def test_schema_ranges_and_defaults_follow_the_spec(registry: ToolRegistry) -> None:
    def prop(name: str, key: str) -> dict[str, Any]:
        return schema_of(registry, name)["properties"][key]

    impact = "analyze_change_impact"
    assert prop(impact, "change_kind")["enum"] == ["signature_change", "behavior_change", "deletion"]
    assert prop(impact, "change_kind")["default"] == "signature_change"
    assert (
        prop(impact, "max_depth")["minimum"],
        prop(impact, "max_depth")["maximum"],
        prop(impact, "max_depth")["default"],
    ) == (1, 6, 3)
    assert (
        prop(impact, "min_coupling_ratio")["default"] == 0.3
        and prop(impact, "include_tests")["default"] is True
    )

    sr = "structural_search_replace"
    assert prop(sr, "dry_run")["default"] is True and prop(sr, "max_matches")["default"] == 200
    assert (
        prop(sr, "max_matches")["minimum"] == 1 and prop(sr, "allow_heuristic_mutation")["default"] is False
    )
    assert "expected_file_sha256s" in schema_of(registry, sr)["properties"]

    paths = "find_code_paths"
    assert (
        prop(paths, "flow_type")["enum"] == ["control", "data", "both"]
        and prop(paths, "flow_type")["default"] == "both"
    )
    assert (
        prop(paths, "max_hops")["minimum"],
        prop(paths, "max_hops")["maximum"],
        prop(paths, "max_hops")["default"],
    ) == (1, 20, 8)
    assert (prop(paths, "max_paths")["maximum"], prop(paths, "max_paths")["default"]) == (200, 20)
    assert (prop(paths, "timeout_ms")["minimum"], prop(paths, "timeout_ms")["maximum"]) == (50, 5000)
    source_props = schema_of(registry, paths)["$defs"]["PathEndpoint"]["properties"]
    assert {"urn", "path", "symbol_name", "line", "character", "variable_name"} <= set(source_props)

    contracts = "extract_code_contracts"
    assert (
        prop(contracts, "include_smt")["default"] is True
        and prop(contracts, "link_external_specs")["default"] is True
    )
    assert prop(contracts, "sources")["items"]["enum"] == ["TYPE", "GUARD", "DOCSTRING", "OPENAPI", "GHERKIN"]

    verify = "verify_contract"
    assert prop(verify, "postconditions")["minItems"] == 1
    assert (prop(verify, "loop_unroll_depth")["minimum"], prop(verify, "loop_unroll_depth")["maximum"]) == (
        0,
        10,
    )
    assert prop(verify, "check_implicit_safety")["default"] is True

    history = "get_symbol_history"
    assert (prop(history, "max_commits")["maximum"], prop(history, "max_commits")["default"]) == (50, 5)
    assert (prop(history, "co_change_limit")["maximum"], prop(history, "co_change_limit")["default"]) == (
        20,
        5,
    )
    assert prop(history, "include_body_diff")["default"] is False

    diff = "analyze_git_diff_symbols"
    assert (
        prop(diff, "base_ref")["default"] == "HEAD" and prop(diff, "target_ref")["default"] == "WORKING_TREE"
    )
    assert prop(diff, "change_types")["items"]["enum"] == [
        "ADDED",
        "MODIFIED_BODY",
        "MODIFIED_SIG",
        "DELETED",
        "RENAMED",
    ]
    assert (prop(diff, "impact_max_depth")["minimum"], prop(diff, "impact_max_depth")["maximum"]) == (1, 5)
    assert "Ignored" in prop(diff, "revision")["description"]  # T-18 は revision を使わない


# --- 呼ぶ（パイプライン） -------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["compact", "json"])
@pytest.mark.parametrize("name", NAMES)
async def test_default_pipeline_answers_e_unsupported_before_the_handler(
    registry: ToolRegistry, name: str, fmt: str
) -> None:
    milestone = PLANNED[name][1]
    r = await Pipeline(registry).call_tool(
        name, {"format": fmt}
    )  # M1。引数の検証より前に止まる（必須引数が無くても同じ）
    assert r.is_error is True
    if fmt == "json":
        assert r.structured is not None and r.structured["ok"] is False and r.structured["tool"] == name
        error = r.structured["error"]
        assert error["code"] == "E_UNSUPPORTED" and error["details"] == {
            "reason": "not_in_milestone",
            "milestone": milestone,
        }
        assert r.structured["data"] is None
    else:
        assert r.text.startswith(f"# {name} error") and "E_UNSUPPORTED" in r.text and milestone in r.text, (
            r.text
        )


@pytest.mark.parametrize("fmt", ["compact", "json"])
@pytest.mark.parametrize("name", NAMES)
async def test_handler_returns_e_unsupported_with_hint_once_the_milestone_is_reached(
    registry: ToolRegistry, name: str, fmt: str
) -> None:
    _tid, milestone, _required, minimal, hint_tool = PLANNED[name]
    pipeline = Pipeline(registry, current_milestone="M3")  # ゲートを通り、入口のハンドラが呼ばれる
    r = await pipeline.call_tool(name, {**minimal, "format": fmt})
    assert r.is_error is True
    if fmt == "json":
        assert r.structured is not None
        error = r.structured["error"]
        assert error["code"] == "E_UNSUPPORTED"
        assert error["details"]["reason"] == "not_in_milestone" and error["details"]["milestone"] == milestone
        if hint_tool is None:
            assert "hint" not in error["details"]  # 代わりの M1 ツールが無いものに hint を作らない
        else:
            assert hint_tool in error["details"]["hint"]
    else:
        assert "E_UNSUPPORTED" in r.text and milestone in r.text
        assert (hint_tool in r.text) if hint_tool else ("meanwhile" not in r.text)


async def test_arguments_are_validated_before_the_handler_when_the_milestone_is_reached(
    registry: ToolRegistry,
) -> None:
    pipeline = Pipeline(registry, current_milestone="M3")
    bad = {
        ("verify_contract", "postconditions"): {"target": _TARGET, "postconditions": []},
        ("analyze_change_impact", "max_depth"): {"target": _TARGET, "max_depth": 7},
        ("find_code_paths", "max_hops"): {"source": _TARGET, "sink": _TARGET, "max_hops": 21},
        ("structural_search_replace", "max_matches"): {
            "language": "python",
            "pattern": "x",
            "max_matches": 0,
        },
        ("get_symbol_history", "co_change_limit"): {"target": _TARGET, "co_change_limit": 21},
        ("analyze_git_diff_symbols", "change_types"): {"change_types": ["MOVED"]},
        ("extract_code_contracts", "path"): {},  # 必須の欠落
    }
    for (name, param), args in bad.items():
        r = await pipeline.call_tool(name, {"format": "json", **args})
        assert r.is_error and r.structured is not None, (name, param)
        error = r.structured["error"]
        assert error["code"] == "E_INVALID_ARGUMENT", (name, param, error)
        assert param in json.dumps(error), (name, param, error)


async def test_calling_does_not_write_to_stdout(
    registry: ToolRegistry, capfd: pytest.CaptureFixture[str]
) -> None:
    for pipeline in (Pipeline(registry), Pipeline(registry, current_milestone="M3")):
        for name in NAMES:
            await pipeline.call_tool(name, dict(PLANNED[name][3]))
    assert capfd.readouterr().out == ""


# --- 実プロセス（stdio） --------------------------------------------------------------------------


class _Raw:
    """最小の生 JSON-RPC クライアント（SDK を通さず、stdout の全行を記録する）。"""

    def __init__(self, cache: Path, *, list_planned: bool = False) -> None:
        env = {**os.environ, "LUCIANKE_CACHE_DIR": str(cache), "PYTHONUTF8": "1"}
        env.pop("LUCIANKE_DEV_TOOLS", None)
        env.pop("LUCIANKE_LIST_PLANNED_TOOLS", None)
        if list_planned:
            env["LUCIANKE_LIST_PLANNED_TOOLS"] = "1"
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "lucianke"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )  # fmt: skip
        self.lines: list[bytes] = []
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.next_id = 1
        self.threads = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._drain_stderr, daemon=True),
        ]
        for t in self.threads:
            t.start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(self.proc.stdout.readline, b""):
            self.lines.append(line)
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                self.inbox.put({"__unparsable__": line.decode("utf-8", "replace")})
        self.inbox.put(None)

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        while self.proc.stderr.read1(4096):  # type: ignore[attr-defined]
            pass

    def send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            msg = self.inbox.get(timeout=60)
            assert msg is not None, f"server closed stdout before answering {method}"
            if msg.get("id") == rid and "method" not in msg:
                return msg

    def close(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=20)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
            for t in self.threads:
                t.join(timeout=5)


def _stdio_session(tmp_path: Path, *, list_planned: bool) -> tuple[dict[str, Any], int, list[bytes]]:
    """実プロセスで ``tools/list`` と 7 本の呼び出しを通す。``(tools/list の名前→定義, 終了コード, stdout の全行)``。"""
    raw = _Raw(tmp_path / "cache", list_planned=list_planned)
    try:
        init = raw.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "raw", "version": "0"},
            },
        )
        assert init["result"]["serverInfo"]["name"] == "lucianke"
        raw.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = {t["name"]: t for t in raw.request("tools/list", {})["result"]["tools"]}
        for name in NAMES:  # 載せていなくても、呼べば E_UNSUPPORTED（未知のツールではない）
            res = raw.request("tools/call", {"name": name, "arguments": dict(PLANNED[name][3])})["result"]
            assert res["isError"] is True, name
            assert "E_UNSUPPORTED" in res["content"][0]["text"], res
    finally:
        rc = raw.close()
    return listed, rc, raw.lines


def _assert_pure_stdout(rc: int, lines: list[bytes]) -> None:
    assert rc == 0
    assert lines, "server wrote nothing to stdout"
    for line in lines:
        assert line.endswith(b"\n") and not line.endswith(b"\r\n"), line
        msg = json.loads(line)
        assert msg.get("jsonrpc") == "2.0" and ("method" in msg or "result" in msg or "error" in msg), line


def test_stdio_server_leaves_the_planned_tools_out_of_tools_list_but_still_answers_them(tmp_path: Path) -> None:
    listed, rc, lines = _stdio_session(tmp_path, list_planned=False)
    assert not (set(NAMES) & set(listed)), sorted(set(NAMES) & set(listed))
    assert {"get_file_skeleton", "edit_symbol_body", "rename_symbol"} <= set(listed)  # M1 は載る
    _assert_pure_stdout(rc, lines)


def test_stdio_server_lists_the_planned_tools_with_the_setting(tmp_path: Path) -> None:
    listed, rc, lines = _stdio_session(tmp_path, list_planned=True)
    assert set(NAMES) <= set(listed)
    for name in NAMES:
        assert listed[name]["inputSchema"]["additionalProperties"] is False
        assert set(listed[name]["inputSchema"].get("required", [])) == PLANNED[name][2]
    _assert_pure_stdout(rc, lines)
