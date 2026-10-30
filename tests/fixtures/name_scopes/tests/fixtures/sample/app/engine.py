"""The sample project's own classes (a fixture: a separate project)."""


class Packer:
    def pack(self, items):
        return "sample"


class Left:
    def settle(self):
        return "left"


class Right:
    def settle(self):
        return "right"
