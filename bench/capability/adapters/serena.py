"""Serena (https://github.com/oraios/serena) over stdio MCP, mapped to its closest tools per task kind.

Serena is started with the project activated at startup (``--project``), the one-tool-per-operation interface
(``--agent-interface tools``), the full toolset context (``--context desktop-app``) and the web dashboard and GUI
log window off. ``SERENA_HOME`` points into the work directory so its config, logs and memories stay there. No
credentials are given.

Mapping (Serena lines are 0-based; they are converted to 1-based):

* orientation: ``list_dir(".")`` and ``list_dir`` of each top-level ``src/<pkg>`` directory (2+ calls);
* symbol_search: ``find_symbol(query, substring_matching=True)``;
* skeleton: ``get_symbols_overview(path, depth=1)``;
* definition: ``find_symbol(name, include_info=True)``;
* body: ``find_symbol(name, relative_path=path, include_body=True)``;
* references: ``find_referencing_symbols(name_path, relative_path)``;
* incoming calls: ``find_referencing_symbols`` (depth 1); depth 2 chains it on every referencing function/method
  (composed: several calls, tokens and time summed);
* outgoing calls: unsupported (no callee query);
* diagnostics: ``get_diagnostics_for_file(path)``;
* rename: ``rename_symbol`` has no dry-run. It is applied to the workspace copy, the edit sites are taken from the
  file diff, and the files are restored byte-for-byte afterwards. It is not repeated for the warm pass.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from .base import Adapter, Answer, Call, count_tokens, norm_path

_BENCH = Path(__file__).resolve().parents[2]
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))
import _stdio as S  # noqa: E402

_REF_LINE = re.compile(r"^\s*>\s*(\d+):", re.M)
_KIND_KEYS = {
    "File",
    "Module",
    "Namespace",
    "Package",
    "Class",
    "Method",
    "Property",
    "Field",
    "Constructor",
    "Enum",
    "Interface",
    "Function",
    "Variable",
    "Constant",
    "String",
    "Number",
    "Boolean",
    "Array",
    "Object",
    "Key",
    "Null",
    "EnumMember",
    "Struct",
    "Event",
    "Operator",
    "TypeParameter",
}


class SerenaAdapter(Adapter):
    name = "serena"

    def __init__(self, workspace: Path, work_dir: Path, options: dict[str, Any]) -> None:
        super().__init__(workspace, work_dir, options)
        self.client: S.McpStdioClient | None = None
        self.timeout = float(options.get("timeout", 1800))
        self._server_info: dict[str, Any] = {}

    def version(self) -> dict[str, Any]:
        return {
            "engine": "serena",
            "command": self.options.get("serena_cmd"),
            "server_info": self._server_info,
        }

    def can_repeat(self, task: dict[str, Any]) -> bool:
        return task["kind"] != "rename"

    # --- lifecycle ----------------------------------------------------------------------------

    async def setup(self) -> dict[str, Any]:
        cmd = shlex.split(self.options["serena_cmd"], posix=os.name != "nt")
        home = self.work_dir / "serena-home"
        home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        env.update({"SERENA_HOME": str(home), "PYTHONUTF8": "1"})
        argv = [
            *cmd,
            "start-mcp-server",
            "--project",
            str(self.workspace),
            "--context",
            "desktop-app",
            "--agent-interface",
            "tools",
            "--enable-web-dashboard",
            "false",
            "--enable-gui-log-window",
            "false",
            "--open-web-dashboard",
            "false",
            "--tool-timeout",
            str(int(self.timeout)),
        ]
        t0 = time.perf_counter()
        self.client = S.McpStdioClient(
            argv,
            cwd=self.workspace,
            env=env,
            roots=None,
            stderr_path=self.work_dir / "serena-stderr.txt",
            max_children=64,
        )
        await self.client.start(timeout=900)
        self._server_info = self.client.server_info
        init_ms = self.client.init_ms
        probe = self.options.get("probe_path")
        ready_ms = None
        probe_err = None
        if probe:
            r = await self.client.call_tool(
                "get_symbols_overview", {"relative_path": probe}, timeout=self.timeout
            )
            ready_ms = (time.perf_counter() - t0) * 1000
            if r.is_error or r.rpc_error:
                probe_err = (r.text or json.dumps(r.rpc_error))[:500]
        return {
            "initialize_ms": round(init_ms, 1),
            "ls_ready_ms": round(ready_ms, 1) if ready_ms else None,
            "setup_ms": round(ready_ms or init_ms, 1),
            "probe_path": probe,
            "probe_error": probe_err,
            "what": "spawn -> initialize (project activated at startup) -> first symbol tool answered "
            "(language server started)",
        }

    async def close(self) -> dict[str, Any]:
        if self.client is None:
            return {}
        rep = await self.client.close()
        return {"exit": rep}

    # --- helpers ------------------------------------------------------------------------------

    async def _call(self, calls: list[Call], tool: str, args: dict[str, Any]) -> str:
        assert self.client is not None
        r = await self.client.call_tool(tool, args, timeout=self.timeout)
        text = r.text if not r.rpc_error else json.dumps(r.rpc_error, ensure_ascii=False)
        err = None
        if r.rpc_error:
            err = "rpc_error"
        elif r.is_error:
            err = text[:300]
        calls.append(Call(tool, args, r.rtt_ms, text, count_tokens(text), err))
        return text

    @staticmethod
    def _json(text: str) -> Any:
        try:
            return json.loads(text)
        except ValueError:
            return None

    def _p(self, path: str) -> str:
        return norm_path(path, self.workspace)

    @staticmethod
    def _name_path(symbol: str) -> str:
        return symbol.replace(".", "/")

    def _ref_sites(self, data: Any) -> tuple[list[tuple[str, int]], list[tuple[str, str, str]]]:
        """Sites and referencing symbols (path, name_path, kind) from a find_referencing_symbols answer."""
        sites: list[tuple[str, int]] = []
        syms: list[tuple[str, str, str]] = []
        if isinstance(data, dict):
            for path, by_kind in data.items():
                if not isinstance(by_kind, dict):
                    continue
                for kind, entries in by_kind.items():
                    for e in entries if isinstance(entries, list) else []:
                        if not isinstance(e, dict):
                            continue
                        ctx = e.get("content_around_reference") or ""
                        lines = [int(m.group(1)) + 1 for m in _REF_LINE.finditer(ctx)]
                        if not lines and isinstance(e.get("body_location"), dict):
                            lines = [int(e["body_location"].get("start_line", 0)) + 1]
                        for n in lines:
                            sites.append((self._p(path), n))
                        syms.append((self._p(path), str(e.get("name_path", "")), kind))
        elif isinstance(data, list):  # older shape: a flat list of referencing symbols
            for e in data:
                if isinstance(e, dict):
                    path = e.get("relative_path", "")
                    ctx = e.get("content_around_reference") or ""
                    for m in _REF_LINE.finditer(ctx):
                        sites.append((self._p(path), int(m.group(1)) + 1))
                    syms.append((self._p(path), str(e.get("name_path", "")), str(e.get("kind", ""))))
        return sites, syms

    # --- task kinds ---------------------------------------------------------------------------

    async def kind_orientation(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        texts = [await self._call(calls, "list_dir", {"relative_path": ".", "recursive": False})]
        root = self._json(texts[0]) or {}
        dirs = [d for d in root.get("dirs", []) if isinstance(d, str)]
        if "src" in [Path(d).name for d in dirs]:
            src = (
                self._json(await self._call(calls, "list_dir", {"relative_path": "src", "recursive": False}))
                or {}
            )
            for pkg in src.get("dirs", []):
                rel = str(pkg).replace("\\", "/")
                rel = rel if rel.startswith("src/") else f"src/{rel}"
                texts.append(await self._call(calls, "list_dir", {"relative_path": rel, "recursive": False}))
        return calls, Answer(text="\n".join(t.replace("\\\\", "/").replace("\\", "/") for t in texts))

    async def kind_symbol_search(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(
            calls,
            "find_symbol",
            {"name_path_pattern": task["query"], "substring_matching": task.get("match") == "substring"},
        )
        data = self._json(text)
        sites = []
        for e in data if isinstance(data, list) else []:
            loc = e.get("body_location") or e.get("location") or {}
            if "start_line" in loc:
                sites.append((self._p(e.get("relative_path", "")), int(loc["start_line"]) + 1))
        return calls, Answer(sites=sites)

    async def kind_skeleton(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(calls, "get_symbols_overview", {"relative_path": task["path"], "depth": 1})
        names: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, str):
                names.append(node)
            elif isinstance(node, list):
                for x in node:
                    walk(x)
            elif isinstance(node, dict):
                for k, v in node.items():
                    if k not in _KIND_KEYS:
                        names.append(k)
                    walk(v)

        walk(self._json(text))
        return calls, Answer(names=names, text=text)

    async def kind_definition(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(
            calls, "find_symbol", {"name_path_pattern": task["name"], "include_info": True}
        )
        data = self._json(text)
        loc = None
        info = ""
        if isinstance(data, list) and data:
            e = data[0]
            bl = e.get("body_location") or {}
            if "start_line" in bl:
                loc = (self._p(e.get("relative_path", "")), int(bl["start_line"]) + 1)
            info = str(e.get("info", ""))
        notes = [f"{len(data)} matches; took the first"] if isinstance(data, list) and len(data) > 1 else []
        return calls, Answer(location=loc, text=info or text, notes=notes)

    async def kind_body(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        tgt = task["target"]
        text = await self._call(
            calls,
            "find_symbol",
            {
                "name_path_pattern": self._name_path(tgt["symbol"]),
                "relative_path": tgt["path"],
                "include_body": True,
            },
        )
        data = self._json(text)
        body = ""
        if isinstance(data, list) and data:
            body = str(data[0].get("body", ""))
        return calls, Answer(text=body or text)

    async def kind_references(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        tgt = task["target"]
        text = await self._call(
            calls,
            "find_referencing_symbols",
            {"name_path": self._name_path(tgt["symbol"]), "relative_path": tgt["path"]},
        )
        sites, _ = self._ref_sites(self._json(text))
        notes = [] if self._json(text) is not None else ["answer is not JSON (shortened or error)"]
        return calls, Answer(sites=sorted(set(sites)), notes=notes)

    async def kind_call_hierarchy(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        if task["direction"] != "incoming":
            return [], Answer(unsupported="Serena has no outgoing-call (callee) query")
        calls: list[Call] = []
        tgt = task["target"]
        text = await self._call(
            calls,
            "find_referencing_symbols",
            {"name_path": self._name_path(tgt["symbol"]), "relative_path": tgt["path"]},
        )
        sites, syms = self._ref_sites(self._json(text))
        all_sites = list(sites)
        if int(task.get("depth", 1)) >= 2:
            seen: set[tuple[str, str]] = set()
            for path, name_path, kind in syms:
                if kind not in ("Function", "Method"):
                    # e.g. "func/local_var": the reference sits in a local variable; chase the enclosing function
                    if "/" not in name_path:
                        continue
                    name_path = name_path.rsplit("/", 1)[0]
                leaf = name_path.split("/")[-1]
                if leaf.startswith("test") or (path, name_path) in seen:
                    continue
                seen.add((path, name_path))
                t2 = await self._call(
                    calls, "find_referencing_symbols", {"name_path": name_path, "relative_path": path}
                )
                s2, _ = self._ref_sites(self._json(t2))
                all_sites.extend(s2)
        return calls, Answer(sites=sorted(set(all_sites)), extra={"composed_calls": len(calls)})

    async def kind_diagnostics(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(calls, "get_diagnostics_for_file", {"relative_path": task["path"]})
        sites: list[tuple[str, int]] = []

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                if "range" in node and isinstance(node["range"], dict):
                    sites.append((path, int(node["range"]["start"]["line"]) + 1))
                    return
                for v in node.values():
                    walk(v, path)
            elif isinstance(node, list):
                for v in node:
                    walk(v, path)

        data = self._json(text)
        if isinstance(data, dict):
            for path, v in data.items():
                walk(v, self._p(path))
        return calls, Answer(sites=sites, extra={"diagnostics": len(sites)})

    async def kind_rename(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        tgt = task["target"]
        old = tgt["name"]
        files = [p for p in self.workspace.rglob("*.py") if ".serena" not in p.parts]
        before: dict[Path, bytes] = {}
        stat0: dict[Path, tuple[int, int]] = {}
        for p in files:
            st = p.stat()
            stat0[p] = (st.st_mtime_ns, st.st_size)
            data = p.read_bytes()
            if old.encode() in data:
                before[p] = data
        await self._call(
            calls,
            "rename_symbol",
            {
                "name_path": self._name_path(tgt["symbol"]),
                "relative_path": tgt["path"],
                "new_name": task["new_name"],
            },
        )
        sites: list[tuple[str, int]] = []
        changed: list[str] = []
        for p in files:
            st = p.stat()
            if (st.st_mtime_ns, st.st_size) == stat0[p]:
                continue
            old_bytes = before.get(p)
            new_text = p.read_text(encoding="utf-8", errors="replace")
            rel = p.relative_to(self.workspace).as_posix()
            changed.append(rel)
            if old_bytes is None:
                continue
            a = old_bytes.decode("utf-8", errors="replace").splitlines()
            b = new_text.splitlines()
            for op, i1, i2, _j1, _j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
                if op in ("replace", "delete"):
                    sites.extend((rel, n + 1) for n in range(i1, i2))
            p.write_bytes(old_bytes)  # restore byte-for-byte
        return calls, Answer(
            sites=sorted(set(sites)),
            notes=["applied (no dry-run), sites from the diff, restored"],
            extra={"changed_files": changed},
        )
