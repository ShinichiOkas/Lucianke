"""Production callers through receivers of unknown type."""

from typing import Any


def render(packer: Any) -> str:
    return packer.pack(["a"])


def query(conn) -> object:
    return conn.execute("SELECT 1")


def launch(job) -> str:
    return job.start()
