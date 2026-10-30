import threading
from abc import ABC

from pkg.base import Base


class Plain(Base, threading.Thread):
    def go(self):
        return self.helper()


class Worker(threading.Thread, Base):
    def go(self):
        return self.helper()


class Shape(ABC, Base):
    def go(self):
        return self.helper()
