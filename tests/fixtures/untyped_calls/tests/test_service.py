from app.service import render


class FakePacker:
    def pack(self, items):
        return "fake"


def test_render():
    assert render(FakePacker(), ["a"]) == "fake"
