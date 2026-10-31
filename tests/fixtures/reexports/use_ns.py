import nsp.tools as nt
import nsp.tools.fmt
from nsp import other, tools


def run():
    nsp.tools.fmt.render()
    nt.fmt.render()
    tools.fmt.render()
    other.go()
    return nsp, nsp.tools, nt, tools
