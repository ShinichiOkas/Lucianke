class Base:
    name = "base"

    def helper(self):
        return 1

    def shout(self):
        return "base"

    @classmethod
    def make(cls):
        return cls()
