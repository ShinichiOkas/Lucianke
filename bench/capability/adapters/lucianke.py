"""Lucianke over stdio MCP (``python -P -m lucianke`` from the source tree, or a built executable).

The server runs with the workspace as its cwd and root, and ``LUCIANKE_CACHE_DIR`` set to a fresh directory, so
every run indexes from scratch. Setup waits until the index job is finished and the Python type server is ready
(status is polled every ``poll_s`` seconds, never faster than 30 s by default, so slow calls do not pile up).

All task calls use the default ``compact`` format (what an agent reads), and ``# more: cursor=...`` pages are
followed (each page is a call: its time and tokens count). Mapping:

* orientation: ``get_repo_map``;
* symbol_search: ``search_workspace_symbols`` with its default (fuzzy) match;
* skeleton: ``get_file_skeleton``;
* definition: ``search_workspace_symbols(match=exact)``: the hit line carries path, line range and signature
  (``resolve_symbols``, which needs a path or URN, only when the hit has no signature);
* body: ``get_symbol_bodies``;
* references: ``find_references_and_implementations(query_type=references, include_tests=true)``; lines in the
  ``name_matched`` supplement (marked ``conf=heuristic``) are counted as reported, and also kept apart in ``extra``;
* call hierarchy: ``query_call_hierarchy`` (incoming: call sites; outgoing: callee qualified names);
* diagnostics: ``get_diagnostics``;
* rename: ``rename_symbol(dry_run=true)``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .base import Adapter, Answer, Call, count_tokens, norm_path

_BENCH = Path(__file__).resolve().parents[2]
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))
import _stdio as S  # noqa: E402

_CURSOR = re.compile(r"^# more: cursor=(\S+)", re.M)
_REF = re.compile(r"^\s*([^\s:#][^\s:]*\.\w+):(\d+):(\d+)\s", re.M)  # path:line:col  (references, rename)
_DIAG = re.compile(r"^[EWIH] (\S+?):(\d+):(\d+) ", re.M)
_SEARCH = re.compile(r"^\[\w+\] (\S+?)#([\w.]+)(.*?) @L(\d+)", re.M)  # [Kind] path#Qual(sig) @Lx-y
_SKEL = re.compile(r"^\s*\[(\w+)\]\s+(?:pub|priv)\s+(?:async\s+)?([A-Za-z_]\w*)", re.M)
_INCOMING = re.compile(r"<-\s+(\S+)(?:\s+\(external\))?\s+@(\S+?):(\d+)")
_OUTGOING = re.compile(r"->\s+(\S+)(\s+\(external\))?\s+@(\S+?):(\d+)")
_DEF_AT = re.compile(r"(\S+?\.\w+)#([\w.]+).*? @L(\d+)")


class LuciankeAdapter(Adapter):
    name = "lucianke"

    def __init__(self, workspace: Path, work_dir: Path, options: dict[str, Any]) -> None:
        super().__init__(workspace, work_dir, options)
        self.client: S.McpStdioClient | None = None
        self.timeout = float(options.get("timeout", 1800))
        self._server_info: dict[str, Any] = {}

    def version(self) -> dict[str, Any]:
        how = self.options.get("lucianke_exe") or "python -P -m lucianke (source)"
        return {"engine": "lucianke", "detail": f"({how})", "server_info": self._server_info}

    # --- lifecycle ----------------------------------------------------------------------------

    async def _status(self) -> tuple[dict[str, Any], float]:
        assert self.client is not None
        r = await self.client.call_tool(
            "configure_workspace", {"action": "status", "format": "json"}, timeout=self.timeout
        )
        env = r.envelope or {}
        return (env.get("data") or {}) if isinstance(env, dict) else {}, r.rtt_ms

    async def setup(self) -> dict[str, Any]:
        exe = self.options.get("lucianke_exe")
        argv = [exe] if exe else [sys.executable, "-P", "-m", "lucianke"]
        cache = self.work_dir / "cache"
        env = S.server_env(cache)
        poll = max(float(self.options.get("poll_s", 30.0)), 1.0)
        deadline_s = float(self.options.get("ready_timeout", 7200.0))
        t0 = time.perf_counter()
        self.client = S.McpStdioClient(
            argv,
            cwd=self.workspace,
            env=env,
            roots=[self.workspace],
            stderr_path=self.work_dir / "lucianke-stderr.txt",
        )
        await self.client.start(timeout=900)
        self._server_info = self.client.server_info
        info: dict[str, Any] = {
            "initialize_ms": round(self.client.init_ms, 1),
            "index_ready_ms": None,
            "lsp_ready_ms": None,
            "status_polls": 0,
            "status_rtt_max_ms": 0.0,
            "lsp_trigger_ms": None,
            "timed_out": False,
        }
        probe = self.options.get("probe_path")
        triggered = False
        standby_after_trigger = 0
        while True:
            data, rtt = await self._status()
            el = (time.perf_counter() - t0) * 1000
            info["status_polls"] += 1
            info["status_rtt_max_ms"] = round(max(info["status_rtt_max_ms"], rtt), 1)
            jobs = data.get("jobs") or []
            running = [j for j in jobs if j.get("state") in ("running", "queued", "pending")]
            cache_stats = data.get("cache") or {}
            if not running and (cache_stats.get("files") or 0) > 0 and info["index_ready_ms"] is None:
                info["index_ready_ms"] = round(el, 1)
                info["index"] = cache_stats
            daemons = [
                d
                for d in data.get("lsp_daemons") or []
                if d.get("language") == "python" and d.get("role") == "types"
            ]
            status = daemons[0].get("status") if daemons else None
            info["lsp_status"] = status
            if triggered and status == "standby":
                standby_after_trigger += 1
            if (
                standby_after_trigger > 10
            ):  # the trigger did not start the type server: stop waiting, report it
                info["lsp_ready_ms"] = None
                break
            if info["index_ready_ms"] is not None and status == "standby" and not triggered and probe:
                tr = await self.client.call_tool("get_diagnostics", {"paths": [probe]}, timeout=self.timeout)
                info["lsp_trigger_ms"] = round(tr.rtt_ms, 1)
                triggered = True
                continue
            if info["index_ready_ms"] is not None and (status in ("ready", "unavailable", None) or not probe):
                info["lsp_ready_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                break
            if el / 1000 > deadline_s:
                info["timed_out"] = True
                break
            await asyncio.sleep(poll)
        ws = (data.get("workspaces") or [{}])[0]
        info["python_env"] = ws.get("python_env")
        info["setup_ms"] = info["lsp_ready_ms"] or round((time.perf_counter() - t0) * 1000, 1)
        info["what"] = (
            "spawn -> initialize -> index job finished -> Python type server ready "
            f"(status polled every {poll:.0f}s, so readiness is known to within one poll)"
        )
        return info

    async def close(self) -> dict[str, Any]:
        if self.client is None:
            return {}
        return {"exit": await self.client.close()}

    # --- helpers ------------------------------------------------------------------------------

    async def _call(self, calls: list[Call], tool: str, args: dict[str, Any], *, pages: int = 10) -> str:
        """One compact call, following ``# more: cursor=`` up to ``pages`` pages. Returns all pages' text."""
        assert self.client is not None
        texts: list[str] = []
        cursor: str | None = None
        for _ in range(pages):
            a = dict(args)
            if cursor:
                a["cursor"] = cursor
            r = await self.client.call_tool(tool, a, timeout=self.timeout)
            text = r.text if not r.rpc_error else json.dumps(r.rpc_error, ensure_ascii=False)
            err = "rpc_error" if r.rpc_error else (text.splitlines()[0][:300] if r.is_error else None)
            calls.append(Call(tool, a, r.rtt_ms, text, count_tokens(text), err))
            texts.append(text)
            m = _CURSOR.search(text)
            if not m or err:
                break
            cursor = m.group(1)
        return "\n".join(texts)

    def _p(self, path: str) -> str:
        return norm_path(path, self.workspace)

    @staticmethod
    def _target(tgt: dict[str, Any]) -> dict[str, Any]:
        return {"path": tgt["path"], "symbol_name": tgt["symbol"]}

    # --- task kinds ---------------------------------------------------------------------------

    async def kind_orientation(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(calls, "get_repo_map", {}, pages=1)
        return calls, Answer(text=text)

    async def kind_symbol_search(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        # the default (fuzzy) match: ``regex`` is applied to the qualified name, so every member of a matching
        # class would match too; the default is also what an agent calls first
        text = await self._call(calls, "search_workspace_symbols", {"query": task["query"]})
        sites = [(self._p(m.group(1)), int(m.group(4))) for m in _SEARCH.finditer(text)]
        return calls, Answer(sites=sites)

    async def kind_skeleton(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(calls, "get_file_skeleton", {"paths": [task["path"]]})
        return calls, Answer(names=[m.group(2) for m in _SKEL.finditer(text)], text=text)

    async def kind_definition(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        """Exact-name search; its hit line already carries path, line range and signature. ``resolve_symbols``
        (which needs a path or URN) is called only when the hit has no signature."""
        calls: list[Call] = []
        found = await self._call(
            calls, "search_workspace_symbols", {"query": task["name"], "match": "exact"}, pages=1
        )
        hits = list(_SEARCH.finditer(found))
        if not hits:
            return calls, Answer(notes=["search returned no hit"], text=found)
        path, qual, sig, line = (
            self._p(hits[0].group(1)),
            hits[0].group(2),
            hits[0].group(3),
            int(hits[0].group(4)),
        )
        notes = [f"{len(hits)} search hits; took the first"] if len(hits) > 1 else []
        if "(" in sig:
            return calls, Answer(location=(path, line), text=found, notes=notes)
        text = await self._call(
            calls, "resolve_symbols", {"targets": [{"path": path, "symbol_name": qual}]}, pages=1
        )
        m = _DEF_AT.search(text)
        loc = (self._p(m.group(1)), int(m.group(3))) if m else (path, line)
        return calls, Answer(location=loc, text=f"{found}\n{text}", notes=notes)

    async def kind_body(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(calls, "get_symbol_bodies", {"targets": [self._target(task["target"])]})
        body = "\n".join(re.sub(r"^@L\d+: ?", "", ln) for ln in text.splitlines())
        return calls, Answer(text=body)

    async def kind_references(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(
            calls,
            "find_references_and_implementations",
            {"target": self._target(task["target"]), "query_type": "references", "include_tests": True},
        )
        sites, heur = [], []
        for line in text.splitlines():
            m = _REF.match(line)
            if not m:
                continue
            s = (self._p(m.group(1)), int(m.group(2)))
            sites.append(s)
            if "conf=heuristic" in line:
                heur.append(f"{s[0]}:{s[1]}")
        head = next((ln for ln in text.splitlines() if ln.startswith("references:")), "")
        return calls, Answer(
            sites=sorted(set(sites)),
            extra={
                "heuristic_sites": heur,
                "header": head,
                "first_line": text.splitlines()[0] if text else "",
            },
        )

    async def kind_call_hierarchy(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        direction = task["direction"]
        text = await self._call(
            calls,
            "query_call_hierarchy",
            {
                "target": self._target(task["target"]),
                "direction": direction,
                "depth": int(task.get("depth", 1)),
                "include_tests": True,
            },
        )
        if direction == "incoming":
            sites = [(self._p(m.group(2)), int(m.group(3))) for m in _INCOMING.finditer(text)]
            return calls, Answer(
                sites=sorted(set(sites)), extra={"first_line": text.splitlines()[0] if text else ""}
            )
        keys = sorted({f"::{m.group(1)}" for m in _OUTGOING.finditer(text) if not m.group(2)})
        external = sorted({m.group(1) for m in _OUTGOING.finditer(text) if m.group(2)})
        return calls, Answer(
            extra={"keys": keys, "external": external, "first_line": text.splitlines()[0] if text else ""}
        )

    async def kind_diagnostics(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(calls, "get_diagnostics", {"paths": [task["path"]]})
        sites = [(self._p(m.group(1)), int(m.group(2))) for m in _DIAG.finditer(text)]
        return calls, Answer(
            sites=sites, extra={"diagnostics": len(sites), "first_line": text.splitlines()[0] if text else ""}
        )

    async def kind_rename(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        text = await self._call(
            calls,
            "rename_symbol",
            {"target": self._target(task["target"]), "new_name": task["new_name"], "dry_run": True},
        )
        sites = [(self._p(m.group(1)), int(m.group(2))) for m in _REF.finditer(text)]
        return calls, Answer(
            sites=sorted(set(sites)), extra={"first_line": text.splitlines()[0] if text else ""}
        )
