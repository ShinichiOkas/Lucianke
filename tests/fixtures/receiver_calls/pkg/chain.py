from __future__ import annotations


class Parsed:
    def copy_with(self):
        return self


class Url:
    parsed: Parsed

    def __init__(self, other: Url | None = None):
        if isinstance(other, Url):
            self.raw = other.raw.copy_with()
            self.parsed = other.parsed.copy_with()
            self.view.copy_with()

    @property
    def view(self) -> Parsed:
        return self.parsed

    def copy_with(self):
        return Url()
