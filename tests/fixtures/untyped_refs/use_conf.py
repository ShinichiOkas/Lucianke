from pkg import shared
from pkg.conf import lock, loose, settings, typed


def apply():
    settings.reload()
    typed.reload()
    loose.reload()
    lock.acquire()
    shared.reload()
    return settings.debug
