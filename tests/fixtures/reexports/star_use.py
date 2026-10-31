from pkg import *  # noqa: F403
from pkg.dyn import *  # noqa: F403
from pkg.loose import *  # noqa: F403
from pkg.stars import *  # noqa: F403
from pkg.widgets import *  # noqa: F403

ITEM = Widget()  # noqa: F405


def go():
    flush()  # noqa: F405
    deep_go()  # noqa: F405
    starred()  # noqa: F405
    hidden()  # noqa: F405
    public()  # noqa: F405
    _private()  # noqa: F405
    dyn_fn()  # noqa: F405
    fresh()  # noqa: F405
    Widget().spin()  # noqa: F405
    ITEM.spin()
    TokenEstimator().count("z")  # noqa: F405
    nowhere.attr()  # noqa: F405
    len("x")
    return flush  # noqa: F405


class Mine(Widget):  # noqa: F405
    pass
