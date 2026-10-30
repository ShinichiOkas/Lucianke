import nsp.tools.fmt
import pkg.helpers
import pkg.sub as ps
import pkg.sub.deep
from pkg import conf


def run():
    pkg.helpers.flush()
    pkg.sub.deep.go()
    ps.deep.go()
    nsp.tools.fmt.render()
    return pkg.helpers


def via_module():
    conf.settings.reload()
    return conf.Settings.debug
