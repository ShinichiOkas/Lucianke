from pkg import Lock, TokenEstimator, _private, absolute, deep_go, flush, hidden, loop, public, starred
from pkg.alias import fl

EST = TokenEstimator()


def run():
    flush()
    deep_go()
    fl()
    absolute()
    starred()
    hidden()
    public()
    _private()
    loop()
    TokenEstimator().count("x")
    EST.count("y")
    EST.reset()
    Lock()
    return flush


class Sub(TokenEstimator):
    pass
