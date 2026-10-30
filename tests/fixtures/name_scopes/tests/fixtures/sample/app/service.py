"""Callers inside the sample project."""


def render(packer):
    return packer.pack(["c"])


def drain(sink):
    return sink.settle()


def notify(mailer):
    return mailer.send("hi")


def total(calc):
    return calc.compute()
