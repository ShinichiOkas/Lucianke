import threading

from pkg.base import Base


class Worker(threading.Thread, Base):
    def save(self):
        return super().save()


class Plain(Base, threading.Thread):
    def save(self):
        return super().save()


class Runner(threading.Thread):
    def run(self):
        return super().run()
