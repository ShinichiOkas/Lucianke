class A:
    def hello(self):
        return "A"

    def greet(self):
        return "a"


class B(A):
    def hello(self):
        return "B" + super().hello()


class C(A):
    def hello(self):
        return "C"

    def greet(self):
        return "c"


class D(B, C):
    def hello(self):
        return "D" + super().hello()

    def greet(self):
        return super().greet()
