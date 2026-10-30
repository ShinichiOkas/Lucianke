from pkg.mid import Mid


class Sub(Mid):
    def run(self):
        return self.helper()

    def loud(self):
        return self.shout()

    @classmethod
    def build(cls):
        return cls.make()

    def tag(self):
        return "sub"

    def own(self):
        return self.tag()


def use(s: Sub):
    return s.helper()


def fresh():
    return Sub().shout()
