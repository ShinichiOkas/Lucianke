class Base:
    def reset(self):
        return None


class TokenEstimator(Base):
    def count(self, text):
        return len(text)
