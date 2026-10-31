"""Baseline: what an agent does with plain Grep/Read and no semantic tools.

The searches are a Python re-implementation of ``rg -n -w -t py`` (hidden directories and virtualenv/cache
directories are skipped, like rg's defaults), so the baseline needs no external binary. Reads are modelled as the
agent reading the exact lines it needs (a body, a signature, the lines from a hit up to its enclosing ``def``).
Every search result and every read is text the agent ingests and is counted as tokens. Search output is cut at
``HEAD_LIMIT`` lines, like the agent's own Grep tool; matches past the cut are neither seen nor counted.

Per task kind:

* orientation: a ``tree -d -L 3``-style directory listing plus the top-level files;
* symbol_search / definition / skeleton: a regex for ``def`` / ``class`` lines;
* body: find the ``def``, then read until the indentation drops back;
* references / rename: every line where the name occurs as a whole word (strings and comments included);
* incoming calls: the reference search, then for each hit scan upward to its enclosing ``def`` and read from that
  ``def`` to the hit (at most 40 lines); depth 2 repeats this for each caller's name, skipping names with more than
  ``MAX_CALLER_HITS`` hits (too generic to chase; only the search output is read);
* outgoing calls: read the body, take every ``name(`` and search for ``def name``;
* diagnostics: unsupported (Grep/Read cannot type-check; an agent would have to run a linter or type checker).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import Adapter, Answer, Call, Timer, count_tokens

SKIP_DIRS = {"venv", ".venv", "env", "node_modules", "__pycache__", "build", "dist", "site-packages"}
_DEF_RE = re.compile(r"^(\s*)(?:async\s+def|def|class)\s+(\w+)")
_IMPORT_RE = re.compile(r"^\s*(?:from\s+\S+\s+import\b|import\s)")
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_KEYWORDS = {
    "if",
    "elif",
    "while",
    "for",
    "return",
    "and",
    "or",
    "not",
    "in",
    "is",
    "with",
    "assert",
    "yield",
    "await",
    "lambda",
    "print",
    "except",
    "raise",
    "del",
    "def",
    "class",
    "async",
    "from",
    "import",
}
HEAD_LIMIT = 250  # lines of search output shown to the agent (Claude Code's Grep default head_limit)
MAX_CALLER_HITS = 200  # a caller name with more hits than this is too generic to chase at depth 2


class GrepAdapter(Adapter):
    name = "grep"

    def version(self) -> dict[str, Any]:
        return {"engine": "grep baseline (Python re-implementation of rg -n -w -t py + line reads)"}

    # --- primitives ---------------------------------------------------------------------------

    def _files(self) -> list[Path]:
        out: list[Path] = []
        stack = [self.workspace]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(d.iterdir())
            except OSError:
                continue
            for e in entries:
                if e.is_dir():
                    if e.name.startswith(".") or e.name in SKIP_DIRS:
                        continue
                    stack.append(e)
                elif e.suffix == ".py":
                    out.append(e)
        return sorted(out)

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.workspace).as_posix()

    def _lines(self, rel: str) -> list[str]:
        return (self.workspace / rel).read_text(encoding="utf-8", errors="replace").splitlines()

    def _search(self, pattern: re.Pattern[str], calls: list[Call], label: str) -> list[tuple[str, int, str]]:
        """``rg -n``: every matching line as (rel, line, text). Adds one call with the printed output."""
        whole_file = re.compile(pattern.pattern, pattern.flags | re.M)  # cheap per-file prefilter
        with Timer() as t:
            hits: list[tuple[str, int, str]] = []
            for f in self._files():
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not whole_file.search(text):
                    continue
                rel = self._rel(f)
                for i, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        hits.append((rel, i, line))
            total = len(hits)
            hits = hits[:HEAD_LIMIT]
            out = "\n".join(f"{r}:{n}:{s}" for r, n, s in hits)
            if total > HEAD_LIMIT:
                out += f"\n[{total - HEAD_LIMIT} more matches not shown]"
        args = {"pattern": pattern.pattern, "for": label, "matches": total}
        calls.append(Call("rg", args, t.ms, out, count_tokens(out)))
        return hits

    def _read(self, rel: str, start: int, end: int, calls: list[Call], label: str) -> str:
        """Read lines ``start..end`` (1-based, inclusive) with ``cat -n``-style numbers. Adds one call."""
        with Timer() as t:
            lines = self._lines(rel)
            end = min(end, len(lines))
            out = "\n".join(f"{n:6d}\t{lines[n - 1]}" for n in range(max(1, start), end + 1))
        calls.append(
            Call("read", {"path": rel, "lines": f"{start}-{end}", "for": label}, t.ms, out, count_tokens(out))
        )
        return out

    @staticmethod
    def _block_end(lines: list[str], def_line: int) -> int:
        """Last line of the block opened at ``def_line`` (indentation rule; decorators are above, not below)."""
        head = lines[def_line - 1]
        indent = len(head) - len(head.lstrip())
        last = def_line
        depth = 0
        n = def_line
        # the signature may span lines: skip to the line that closes it
        while n <= len(lines):
            depth += lines[n - 1].count("(") - lines[n - 1].count(")")
            if depth <= 0 and lines[n - 1].rstrip().endswith(":"):
                break
            n += 1
        for i in range(n + 1, len(lines) + 1):
            s = lines[i - 1]
            if not s.strip():
                continue
            if len(s) - len(s.lstrip()) <= indent:
                break
            last = i
        return last

    @staticmethod
    def _enclosing_def(lines: list[str], line: int) -> int | None:
        """Scan upward from ``line`` for the nearest ``def`` with less indentation (what a reader does by eye)."""
        s = lines[line - 1]
        indent = len(s) - len(s.lstrip())
        for i in range(line - 1, 0, -1):
            t = lines[i - 1]
            if not t.strip() or t.lstrip().startswith("#"):
                continue
            ti = len(t) - len(t.lstrip())
            if ti >= indent:
                continue
            if _DEF_RE.match(t):
                return None if t.lstrip().startswith("class") else i
            if ti == 0 and not t.lstrip().startswith(("@", ")", "]", "}")):
                return None  # a module-level statement encloses the hit
            indent = ti  # a less-indented block opener (if/for/with/try): keep climbing from its level
        return None

    # --- task kinds ---------------------------------------------------------------------------

    async def kind_orientation(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        with Timer() as t:
            rows: list[str] = []

            def walk(d: Path, depth: int) -> None:
                for e in sorted(d.iterdir()):
                    if e.name.startswith(".") or e.name in SKIP_DIRS:
                        continue
                    if e.is_dir():
                        rows.append(self._rel(e) + "/")
                        if depth < 3:
                            walk(e, depth + 1)
                    elif depth == 1:
                        rows.append(self._rel(e))

            walk(self.workspace, 1)
            out = "\n".join(rows)
        calls.append(Call("tree", {"depth": 3}, t.ms, out, count_tokens(out)))
        return calls, Answer(text=out)

    async def kind_symbol_search(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        q = re.escape(task["query"])
        pat = re.compile(rf"^\s*(?:async\s+def|def|class)\s+\w*{q}\w*")
        hits = self._search(pat, calls, "symbol search")
        return calls, Answer(sites=[(r, n) for r, n, _ in hits])

    async def kind_skeleton(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        rel = task["path"]
        with Timer() as t:
            hits = [(i, ln) for i, ln in enumerate(self._lines(rel), 1) if _DEF_RE.match(ln)]
            out = "\n".join(f"{i}:{ln}" for i, ln in hits)
        calls.append(Call("rg", {"pattern": _DEF_RE.pattern, "path": rel}, t.ms, out, count_tokens(out)))
        names = [m.group(2) for _, ln in hits if (m := _DEF_RE.match(ln))]
        return calls, Answer(names=names, text=out)

    async def kind_definition(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        name = re.escape(task["name"])
        hits = self._search(re.compile(rf"^\s*(?:async\s+def|def|class)\s+{name}\b"), calls, "definition")
        if not hits:
            return calls, Answer(notes=["no definition line found"])
        rel, line, _ = hits[0]
        lines = self._lines(rel)
        n, depth = line, 0
        while n <= len(lines):
            depth += lines[n - 1].count("(") - lines[n - 1].count(")")
            if depth <= 0 and lines[n - 1].rstrip().endswith(":"):
                break
            n += 1
        sig = self._read(rel, line, min(n, line + 15), calls, "signature")
        notes = [f"{len(hits)} definition lines matched; took the first"] if len(hits) > 1 else []
        return calls, Answer(location=(rel, line), text=sig, notes=notes)

    async def kind_body(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        tgt = task["target"]
        name = re.escape(tgt["symbol"].split(".")[-1])
        rel = tgt["path"]
        pat = re.compile(rf"^\s*(?:async\s+def|def|class)\s+{name}\b")
        with Timer() as t:
            lines = self._lines(rel)
            hits = [(i, ln) for i, ln in enumerate(lines, 1) if pat.match(ln)]
            out = "\n".join(f"{i}:{ln}" for i, ln in hits)
        calls.append(Call("rg", {"pattern": pat.pattern, "path": rel}, t.ms, out, count_tokens(out)))
        if not hits:
            return calls, Answer(notes=["definition not found"])
        start = hits[0][0]
        body = self._read(rel, start, self._block_end(lines, start), calls, "body")
        return calls, Answer(text=body, location=(rel, start))

    async def kind_references(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        name = re.escape(task["target"]["name"])
        hits = self._search(re.compile(rf"\b{name}\b"), calls, "references")
        return calls, Answer(sites=[(r, n) for r, n, _ in hits])

    async def kind_rename(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls, ans = await self.kind_references(task)
        ans.notes.append("sed-style: every whole-word occurrence in .py files would be edited")
        return calls, ans

    def _callers(self, name: str, calls: list[Call], label: str) -> tuple[list[tuple[str, int, str]], int]:
        """Enclosing defs (rel, def line, def name) of every non-definition, non-import hit of ``name``."""
        hits = self._search(re.compile(rf"\b{re.escape(name)}\b"), calls, label)
        total = int(calls[-1].args["matches"])
        found: dict[tuple[str, int], str] = {}
        if total > MAX_CALLER_HITS:  # too generic a name to chase: the search output is all the agent reads
            return [], total
        for rel, line, text in hits:
            if _IMPORT_RE.match(text):
                continue
            m = _DEF_RE.match(text)
            if m and m.group(2) == name:
                continue
            lines = self._lines(rel)
            d = self._enclosing_def(lines, line)
            if d is None:
                continue
            if (rel, d) not in found:
                self._read(rel, d, min(line, d + 40), calls, f"enclosing def of {rel}:{line}")
                dm = _DEF_RE.match(lines[d - 1])
                found[(rel, d)] = dm.group(2) if dm else "?"
        return [(r, n, nm) for (r, n), nm in found.items()], total

    async def kind_call_hierarchy(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        calls: list[Call] = []
        tgt = task["target"]
        if task["direction"] == "incoming":
            level, _ = self._callers(tgt["name"], calls, "callers depth 1")
            sites = [(r, n) for r, n, _ in level]
            notes: list[str] = []
            if int(task.get("depth", 1)) >= 2:
                for nm in sorted({nm for _, _, nm in level}):
                    if nm.startswith("test"):
                        continue
                    nxt, nhits = self._callers(nm, calls, f"callers of {nm}")
                    if nhits > MAX_CALLER_HITS:
                        notes.append(f"skipped depth-2 chase of '{nm}': {nhits} hits")
                        continue
                    sites.extend((r, n) for r, n, _ in nxt)
            return calls, Answer(sites=sorted(set(sites)), notes=notes)
        # outgoing: read the body, then look up a definition for every called name
        rel = tgt["path"]
        lines = self._lines(rel)
        start = int(tgt["line"])
        end = self._block_end(lines, start)
        body = self._read(rel, start, end, calls, "body")
        called = []
        for n in range(start + 1, end + 1):
            for m in _CALL_RE.finditer(lines[n - 1]):
                if m.group(1) not in _KEYWORDS and m.group(1) not in called:
                    called.append(m.group(1))
        sites: list[tuple[str, int]] = []
        for nm in called:
            hits = self._search(
                re.compile(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(nm)}\b"), calls, f"def {nm}"
            )
            sites.extend((r, n) for r, n, _ in hits)
        _ = body
        return calls, Answer(sites=sorted(set(sites)), extra={"called_names": called})

    async def kind_diagnostics(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        return [], Answer(unsupported="Grep/Read cannot produce type or lint diagnostics")
