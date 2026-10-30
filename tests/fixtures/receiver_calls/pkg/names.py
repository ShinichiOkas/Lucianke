from pkg import names_mod


class Ledger:
    def persist(self):
        return 1


def untyped(x):
    return x.persist()


def annotated(x: Ledger):
    return x.persist()


def built():
    x = Ledger()
    return x.persist()


def module_function():
    return names_mod.persist()
