"""Engine adapters for the capability benchmark (``bench/capability/run.py``)."""

from __future__ import annotations

from .base import Adapter
from .grep import GrepAdapter
from .lucianke import LuciankeAdapter
from .serena import SerenaAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "lucianke": LuciankeAdapter,
    "serena": SerenaAdapter,
    "grep": GrepAdapter,
}
