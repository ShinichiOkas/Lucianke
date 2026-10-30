from pkg.mid import Mid


class Leaf(Mid):
    def save(self):
        return super().save()

    def load(self):
        return super().load()
