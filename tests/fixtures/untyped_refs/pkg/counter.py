class Counter:
    def __init__(self):
        self.total = 0
        self.hits = 0


class Tally(Counter):
    def bump(self):
        self.hits = self.hits + 1
