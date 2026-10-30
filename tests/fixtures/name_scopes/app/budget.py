"""Production classes whose methods are called through receivers of unknown type."""


class Packer:
    def pack(self, items: list[str]) -> str:
        return ",".join(items)

    def settle(self) -> str:
        return ""


class Mailer:
    def send(self, text: str) -> str:
        return text


class Calc:
    def compute(self) -> int:
        return 1

    def reset(self) -> None:
        return None
