"""Observe the exact Runtime used by the meter without changing its behavior."""

from collections import Counter
import re
import time
from urllib.parse import unquote, urlsplit


def request_path(method, path):
    path = unquote(urlsplit(path).path)
    path = re.sub(r"(?<=/)[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(?=/|$)", "<id>", path)
    path = re.sub(r"(/v1/projects/)[^/]+", r"\1<project>", path)
    path = re.sub(r"(/v1/tasks/)(?!claim(?:/|$)|wait(?:/|$))[^/]+", r"\1<task>", path)
    path = re.sub(r"(/v1/attempts/)[^/]+", r"\1<attempt>", path)
    path = re.sub(r"(?<=/)(?:sha256:)?[0-9a-f]{64}(?=/|$)", "<digest>", path)
    return method + " " + path


class RuntimeObserver:
    def __init__(self, daemon, process):
        self.daemon, self.process = daemon, process
        self.requests = []
        self.database = str(daemon.service.store.db_path)
        process.runtime_connection = daemon.service.store.conn

    def sqlite_mode(self):
        store = self.daemon.service.store
        with store._mutex:
            return {name: store.conn.execute("PRAGMA " + name).fetchone()[0]
                    for name in ("journal_mode", "synchronous", "wal_autocheckpoint")}

    def install(self):
        from runtime_protocol.server import RuntimeHandler
        from runtime_protocol import platform

        owner, original = self, RuntimeHandler._dispatch

        def dispatch(handler):
            if handler.server is not owner.daemon.httpd:
                return original(handler)
            route = request_path(handler.command, handler.path)
            owner.requests.append((time.monotonic_ns(), route))
            previous = getattr(owner.process.context, "route", None)
            owner.process.context.route = route
            try:
                return original(handler)
            finally:
                owner.process.context.route = previous

        self.process.patches.setattr(RuntimeHandler, "_dispatch", dispatch)
        if hasattr(platform, "_move_file_write_through"):
            move = platform._move_file_write_through

            def write_through(*args, **kwargs):
                start = time.time_ns()
                success = False
                try:
                    result = move(*args, **kwargs)
                    success = True
                    return result
                finally:
                    owner.process.event("platform_write_through_" + ("success" if success else "error"),
                        epoch_start_ns=start, epoch_end_ns=time.time_ns(),
                        route=getattr(owner.process.context, "route", None))
            self.process.patches.setattr(platform, "_move_file_write_through", write_through)
        return self

    def report(self, start_ns, reply_ns):
        return dict(sorted(Counter(route for at, route in self.requests
                                   if start_ns <= at <= reply_ns).items()))
