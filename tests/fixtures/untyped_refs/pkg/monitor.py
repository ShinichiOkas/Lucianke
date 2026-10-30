class Monitor:
    stdout_lines: int = 0
    total: int = 0
    name: str = ""
    parent: str = ""

    def __init__(self):
        self.samples = 0

    def violate(self):
        return self.samples
