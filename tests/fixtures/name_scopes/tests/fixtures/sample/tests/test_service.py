class FakeMailer:
    def send(self, text):
        return text


class FakePacker:
    def pack(self, items):
        return "fake"
