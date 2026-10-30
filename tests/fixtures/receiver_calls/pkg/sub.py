from pkg.base import Base


class Sub(Base):
    def __init__(self):
        super().__init__()

    def save(self):
        return super().save()

    def load(self):
        return super(Sub, self).load()  # noqa: UP008
