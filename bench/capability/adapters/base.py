"""Common types for the capability benchmark adapters.

An adapter answers one benchmark task with the closest tools its engine has and returns:

* ``calls``: every tool call it made (tool, arguments, wall time, the text the agent would ingest, its tokens);
* an ``Answer``: what the engine claims, normalised so ``scoring.py`` can compare engines on the same footing
  (workspace-relative POSIX paths, 1-based lines).

Tokens are counted with Lucianke's ``TokenEstimator`` for every engine (the same estimator for all, for fairness).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from lucianke.core.tokens import TokenEstimator

_EST = TokenEstimator()


def count_tokens(text: str) -> int:
    return _EST.count(text)


@dataclass(slots=True)
class Call:
    tool: str
    args: dict[str, Any]
    ms: float
    text: str
    tokens: int
    error: str | None = None

    def to_json(self, *, keep_text: bool) -> dict[str, Any]:
        d = asdict(self)
        if not keep_text:
            d["text"] = self.text[:400]
        return d


@dataclass(slots=True)
class Answer:
    """What the engine answered, normalised. Unused fields stay empty."""

    sites: list[tuple[str, int]] = field(default_factory=list)  # (rel posix path, 1-based line)
    names: list[str] = field(default_factory=list)  # symbol names (skeleton)
    text: str = ""  # decoded text for substring checks (orientation, body, definition)
    location: tuple[str, int] | None = None  # definition site
    unsupported: str | None = None  # reason, when the engine has no equivalent tool
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "sites": [f"{p}:{n}" for p, n in self.sites],
            "names": self.names,
            "text_head": self.text[:300],
            "location": f"{self.location[0]}:{self.location[1]}" if self.location else None,
            "unsupported": self.unsupported,
            "notes": self.notes,
            "extra": self.extra,
        }


class Timer:
    def __enter__(self) -> Timer:
        self.t0 = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = (time.perf_counter() - self.t0) * 1000


def norm_path(path: str, workspace: Path) -> str:
    """Workspace-relative POSIX path (accepts absolute paths, backslashes, ``file://`` URIs)."""
    p = path.strip()
    if p.startswith("file:///"):
        p = p[len("file:///") :]
    p = p.replace("\\", "/")
    ws = workspace.resolve().as_posix()
    low_p, low_ws = p.lower(), ws.lower()
    if low_p.startswith(low_ws + "/"):
        p = p[len(ws) + 1 :]
    while p.startswith("./"):
        p = p[2:]
    return p


class Adapter:
    """Base class. Subclasses implement one coroutine per task kind (``kind_<kind>``)."""

    name = "base"

    def __init__(self, workspace: Path, work_dir: Path, options: dict[str, Any]) -> None:
        self.workspace = workspace
        self.work_dir = work_dir
        self.options = options

    async def setup(self) -> dict[str, Any]:
        return {}

    async def close(self) -> dict[str, Any]:
        return {}

    def version(self) -> dict[str, Any]:
        return {}

    def can_repeat(self, task: dict[str, Any]) -> bool:
        return True

    async def run(self, task: dict[str, Any]) -> tuple[list[Call], Answer]:
        fn = getattr(self, f"kind_{task['kind']}", None)
        if fn is None:
            return [], Answer(unsupported=f"{self.name} has no mapping for kind {task['kind']}")
        return await fn(task)
