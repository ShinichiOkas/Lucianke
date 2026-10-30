"""Callers of Packer.pack and friends, mostly through receivers of unknown type."""

from typing import Any

from app.engine import Packer, Runner


def render(packer: Any, items: list[str]) -> str:
    if items:
        return packer.pack(items)
    return ""


def render_untyped(packer, items):
    out = packer.pack(items)
    return out


class Holder:
    def __init__(self, inner):
        self.inner = inner

    def flush(self) -> str:
        return self.inner.pack(["z"])


def from_factory(make) -> str:
    return make().pack(["y"])


def start(job) -> str:
    return job.run()


def lookup(store, key):
    return store.get(key)


def typed(runner: Runner) -> str:
    return runner.pack()


def known() -> str:
    return Packer().pack(["x"])


def notify(mailer) -> str:
    return mailer.send("hi")
