from pathlib import Path


def tick(mon):
    mon.stdout_lines += 1
    mon.samples = mon.samples + 1
    if mon.total:
        mon.violate()
    return mon.name


def wide(c):
    c.hits += 1


def where(p: Path, node):
    return p.parent, node.parent
