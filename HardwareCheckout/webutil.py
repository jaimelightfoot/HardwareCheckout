from base64 import b64decode
from contextlib import contextmanager

from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.web import RequestHandler, URLSpec
from tornado_sqlalchemy import SessionMixin

from .models import db


class UserBaseHandler(SessionMixin, RequestHandler):
    def get_current_user(self):
        """
        Not allowed to be async
        """
        try:
            user_cookie = int(self.get_secure_cookie("user", max_age_days=2))
            return user_cookie
        except Exception:
            return False


class Blueprint:
    def __init__(self):
        self.routes = []

    def route(self, path, kwargs=None, name=None):
        def decorator(cls):
            self.routes.append(
                {
                    "pattern": [part for part in path.split("/") if part],
                    "handler": cls,
                    "kwargs": kwargs,
                    "name": name,
                }
            )
            return cls

        return decorator

    def publish(self, base):
        finalRoutes = []
        base = [part for part in base.split("/") if part]
        for route in self.routes:
            route["pattern"] = "/" + "/".join(base + route["pattern"])
            finalRoutes.append(URLSpec(**route))
        return finalRoutes


class Waiters:
    def __init__(self):
        self.waiters = dict()

    def __getitem__(self, id):
        if id not in self.waiters:
            self.waiters[id] = WaiterBucket()
        return self.waiters[id]

    def broadcast(self, message):
        """
        Not async. Send returns a future, just ignore it.
        """
        for bucket in self.waiters.values():
            bucket.send(message)


class WaiterBucket:
    def __init__(self):
        self.bucket = set()

    def __getattr__(self, name):
        return getattr(self.bucket, name)

    def remove(self, waiter):
        if waiter in self.bucket:
            self.bucket.remove(waiter)

    def send(self, message):
        for waiter in self.bucket:
            waiter.send(message)


class Timer:
    __instance = None
    __timer = None
    __callback = None
    __repeat = True
    __args = []
    __kwargs = {}

    def __init__(self, func, repeat=True, timeout=10, args=None, kwargs=None):
        self.__callback = func
        self.__repeat = repeat
        self.__timeout = timeout
        self.__args = args if args else list()
        self.__kwargs = kwargs if kwargs else dict()
        if self.__repeat:
            self.__timer = PeriodicCallback(
                self.__callback_wrapper, self.__timeout * 1000
            )
            self.__timer.start()
        else:
            self.__timer = IOLoop.current().call_later(
                self.__timeout, self.__callback_wrapper
            )

    def restart(self):
        if self.__timer is not None:
            self.__stop()
            self.__start()

    def start(self):
        if self.__timer is None:
            self.__start()

    def stop(self):
        if self.__timer is not None:
            self.__stop()

    def __start(self):
        if self.__repeat and not self.__timer.is_running():
            self.__timer.start()
        # else:
        #     print("calling later")
        #     self.__timer = IOLoop.current().call_later(self.__timeout, self.__callback_wrapper, self)

    def __stop(self):
        if self.__repeat:
            self.__timer.stop()
        else:
            IOLoop.current().remove_timeout(self.__timer)

    def __callback_wrapper(self):
        IOLoop.current().add_callback(self.__callback, *self.__args, **self.__kwargs)
        if self.__repeat:
            self.__start()


@contextmanager
def make_session(engine=db):
    session = None
    try:
        session = engine.sessionmaker()
        yield session
    except Exception:
        if session:
            session.rollback()
        raise
    else:
        session.commit()
    finally:
        if session:
            session.close()
