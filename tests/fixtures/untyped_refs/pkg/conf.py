from threading import Lock


class Settings:
    debug: bool = False

    def reload(self):
        return 1


def make():
    return Settings()


settings = Settings()
typed: Settings = make()
loose = make()
lock = Lock()
