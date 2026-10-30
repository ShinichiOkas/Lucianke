"""Two production methods named start, and a class that delegates to the object it wraps."""


class Runner:
    def start(self) -> str:
        return "runner"


class Engine:
    def start(self) -> str:
        return "engine"


class Session:
    def __init__(self, proc) -> None:
        self.proc = proc

    def terminate(self) -> None:
        self.proc.terminate()

    def reset(self) -> None:
        self.proc.reset()
