"""Classes whose methods are called through receivers of unknown type."""


class Packer:
    def pack(self, items: list[str]) -> str:
        return ",".join(items)

    def run(self) -> str:
        return self.pack(["a"])


class Runner:
    def run(self) -> str:
        return "runner"

    def get(self, key: str) -> str:
        return key


class Mailer:
    def send(self, text: str) -> str:
        return text


def local_typed_miss() -> str:
    r = Runner()
    return r.pack()
