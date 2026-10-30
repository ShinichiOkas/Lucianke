from pkg.diamond import B, C


class E(B, C):
    def greet(self):
        return super().greet()
