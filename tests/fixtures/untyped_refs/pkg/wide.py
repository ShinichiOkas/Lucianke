from pkg.counter import Counter


class Wide(Counter):
    def __init__(self):
        super().__init__()
        self.hits = 5
