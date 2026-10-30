class Base:
    note: str = ""

    def save(self):
        return 1


def f(x):
    x.save()
    return x.note
