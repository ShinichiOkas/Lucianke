class FakeConn:
    def execute(self, sql):
        return sql


def test_render(packer):
    assert packer.pack(["b"])


class TestQuery:
    def setup_method(self, method):
        self.conn = method.connect()

    def test_query(self):
        assert self.conn.execute("q")
